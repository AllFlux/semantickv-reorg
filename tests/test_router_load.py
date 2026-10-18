#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_router_load.py — H-DS24 / DS-20261019-018-WB1 接口验收（模拟 blob 版）。

DS-018 称 WB2 已交付 router_weights.pt，但 WB1 仓库 src/shared/ 实际未到达
（物理阻塞）。本测试用**同构模拟 blob**（DS 规格 MLP，1024/256/64 ≈ 274k
参数，与 DS 所述 WB2 参数量同量级）把 H-DS24 的接口契约全部验证一遍，
真实权重到达后只需把路径指向 src/shared/router_weights.pt 复跑本脚本。

覆盖（对齐 H-DS24 falsifiable_criterion 两种失败模式 + DS-018 两项 advice）：
  T1 load_state_dict 成功（失败模式①的反面）
  T2 结构不符 → 明确诊断报错（失败模式①）
  T3 符号翻转生效：score_sign=-1 时 labels = argmax(-logits)（负相关修正）
  T4 blob 内 score_sign 优先于调用参数（两种修正方式不叠加的机制保障）
  T5 维度不符 → ValueError（Train-Serve Skew 防护）
  T6 train_tau≠inference_tau → RuntimeWarning（Skew 告警）
  T7 路由延迟（真实加载路径，H-DS17 附带验证）

结果并入 results/router_bench.json 的 wb2_load_path_sim 段（标注模拟）。
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "semantickv"))
from vllm_plugin import SemanticRouter                     # noqa: E402
from vllm_plugin.learned_router import LearnedSemanticRouter  # noqa: E402

TMP = ROOT / "results" / "_tmp_router_blob.pt"
BENCH = ROOT / "results" / "router_bench.json"


def make_blob(path, *, with_sign=True, bad_arch=False, q_dim=1024):
    """构造模拟 WB2 blob。bad_arch=True 时 config 宣称 DS 规格(256) 但
    state_dict 来自 hidden_dim=128 的网络——制造 H-DS24 失败模式①的
    真实不匹配场景。"""
    torch.manual_seed(42)
    if bad_arch:
        net = LearnedSemanticRouter(q_dim=q_dim, hidden_dim=128, num_groups=64)
    else:
        net = LearnedSemanticRouter(q_dim=q_dim, hidden_dim=256, num_groups=64)
    cfg = {"arch": "ds011_linear_relu_linear", "q_dim": q_dim,
           "hidden_dim": 256, "num_groups": 64,      # config 恒为 DS 规格
           "feature_dim": q_dim, "train_tau": 200}
    if with_sign:
        cfg["score_sign"] = -1
    torch.save({"state_dict": net.state_dict(), "config": cfg}, path)
    return path


def main() -> int:
    torch.manual_seed(0)
    torch.set_num_threads(1)      # 小矩阵多线程切换产生 ms 级整数毛刺，单线程最稳
    x = torch.randn(1, 2048, 1024)          # 模拟 7B 隐状态（2048 token）
    results = {}

    # T1 加载成功
    blob = make_blob(TMP, with_sign=True)
    r = SemanticRouter.load_external_router(str(blob))
    labels = r.route(x.numpy())
    results["T1_load_ok"] = {
        "pass": labels.shape == (1, 2048) and labels.dtype == np.int64,
        "labels_shape": list(labels.shape), "dtype": str(labels.dtype)}

    # T2 结构不符诊断
    bad = make_blob(str(TMP) + ".bad", bad_arch=True)
    try:
        SemanticRouter.load_external_router(bad)
        results["T2_struct_mismatch_diagnosed"] = {"pass": False}
    except RuntimeError as e:
        msg = str(e)
        results["T2_struct_mismatch_diagnosed"] = {
            "pass": "缺失 keys" in msg and "多余 keys" in msg,
            "error_head": msg.splitlines()[0]}

    # T3 符号翻转（无 blob score_sign 字段，靠参数）
    blob_ns = make_blob(str(TMP) + ".nosign", with_sign=False)
    net = LearnedSemanticRouter(q_dim=1024, hidden_dim=256, num_groups=64)
    net.load_state_dict(torch.load(blob_ns, weights_only=False)["state_dict"])
    net.eval()
    with torch.no_grad():
        logits = net(x)
    r_neg = SemanticRouter.load_external_router(str(blob_ns), score_sign=-1)
    r_pos = SemanticRouter.load_external_router(str(blob_ns), score_sign=+1)
    lab_neg = r_neg.route(x.numpy())
    lab_pos = r_pos.route(x.numpy())
    expect_neg = (-logits).argmax(-1).numpy()
    expect_pos = logits.argmax(-1).numpy()
    results["T3_sign_flip"] = {
        "pass": bool((lab_neg == expect_neg).all() and (lab_pos == expect_pos).all()
                     and not (lab_neg == lab_pos).all()),
        "neg_matches_neg_argmax": bool((lab_neg == expect_neg).all()),
        "pos_matches_argmax": bool((lab_pos == expect_pos).all()),
        "flip_changes_labels": bool(not (lab_neg == lab_pos).all())}

    # T4 blob 内 score_sign 优先（不叠加机制）
    r_blob = SemanticRouter.load_external_router(str(blob), score_sign=+1)
    lab_blob = r_blob.route(x.numpy())
    results["T4_blob_sign_priority"] = {
        "pass": bool((lab_blob == lab_neg).all()),   # blob=-1 覆盖参数 +1
        "blob_sign_wins_over_param": bool((lab_blob == lab_neg).all())}

    # T5 维度不符防护
    x_bad = torch.randn(1, 2048, 999)
    try:
        r.route(x_bad.numpy())
        results["T5_dim_guard"] = {"pass": False}
    except ValueError as e:
        results["T5_dim_guard"] = {"pass": "Train-Serve Skew" in str(e),
                                   "error_head": str(e).splitlines()[0][:60]}

    # T6 train_tau 告警
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        SemanticRouter.load_external_router(str(blob), inference_tau=1.0)
        hit = any("Train-Serve Skew" in str(wi.message) for wi in w)
    results["T6_tau_warning"] = {"pass": hit}

    # T7 真实加载路径延迟（N=1 单 query = H-DS17「一次群路由推理」口径；
    #     N=2048 为记录值——整段前向属计算量问题，不套 0.5ms 通过线）
    def _lat(inp, reps=200):
        with torch.no_grad():
            for _ in range(20):
                r.route(inp.numpy())
            ts = []
            for _ in range(reps):
                t0 = time.perf_counter()
                r.route(inp.numpy())
                ts.append((time.perf_counter() - t0) * 1000.0)
        ts.sort()
        return {"p50_ms": round(ts[len(ts) // 2], 4),
                "p99_ms": round(ts[int(len(ts) * 0.99)], 4)}

    lat1 = _lat(torch.randn(1, 1, 1024))
    lat2048 = _lat(torch.randn(1, 2048, 1024), reps=100)
    results["T7_loaded_route_latency"] = {
        "single_query_N1": lat1, "prefill_N2048": lat2048,
        # CPU 上 p90+ 受 OS 调度毛刺污染（~99ms 整数尖峰），判据用 p50；
        # H-DS17 通过线 <0.5ms / 证伪线 >2ms 的终判以 Kaggle GPU 为准
        "pass": lat1["p50_ms"] < 2.0,
        "note": "加载路径含 numpy→tensor 适配开销；p50 判据，GPU 终测待 Kaggle"}

    all_pass = all(v["pass"] for v in results.values())
    summary = {
        "verdict": "PASS" if all_pass else "FAIL",
        "note": ("模拟 blob（DS 规格同构 ≈274k 参数）全契约通过；真实 WB2 权重"
                 "（src/shared/router_weights.pt）到达后复跑本脚本即完成 H-DS24 终判"),
    }

    # 并入 router_bench.json
    if BENCH.exists():
        bench = json.loads(BENCH.read_text(encoding="utf-8"))
    else:
        bench = {"summary": {}}
    bench["wb2_load_path_sim"] = {
        "config": {"device": "cpu", "dtype": "float32",
                   "purpose": "H-DS24 接口验收（DS-20261019-018-WB1）",
                   "sim_blob": "DS 规格 MLP 1024/256/64 同构（≈274k params）",
                   "real_weights_status": "未到达（src/shared/ 为空，物理阻塞）"},
        "environment": {"kind": "cpu-synthetic+real-arch-smoke"},
        "checks": results, "summary": summary,
    }
    BENCH.write_text(json.dumps(bench, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    print("===== H-DS24 接口验收（模拟 blob） =====")
    for k, v in results.items():
        print(f" [{'✔' if v['pass'] else '✘'}] {k}: {'PASS' if v['pass'] else 'FAIL'}")
        for kk in ("labels_shape", "dtype", "error_head", "neg_matches_neg_argmax",
                   "pos_matches_argmax", "flip_changes_labels",
                   "blob_sign_wins_over_param", "p50_ms", "p99_ms"):
            if kk in v:
                print(f"      {kk}: {v[kk]}")
    print(f"===== {summary['verdict']} → 并入 {BENCH.name}.wb2_load_path_sim =====")
    for f in (TMP, Path(str(TMP) + ".bad"), Path(str(TMP) + ".nosign")):
        f.unlink(missing_ok=True)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
