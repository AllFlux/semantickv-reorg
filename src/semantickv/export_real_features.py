#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  export_real_features.py — 导出 7B Prefill 真实 Query 隐状态 + GSC 分组标签
#                             (DS-20261019-029-WB1 步骤 2)
# ----------------------------------------------------------------------------
#  为什么需要：WB2 的 semantic_router.pt 是在**合成数据**上训的
#  （proxy_data=true：64 个正交质心 + 高斯噪声，标签来自离线 K-means）。
#  这构成循环论证——它拟合的是自己造的分组，不代表真实语义结构。
#  本脚本把 7B 在真实 Prefill 阶段的隐状态（维度 3584）连同**真实 GSC 分组标签**
#  导出成 .npz，交给 WB2 做「真实特征 + 真实真值」的重训。
#
#  输出：
#    <out>.npz    hidden_states(N,D) float16 / labels(N,) int32 / meta(json string)
#                 键名可由 --key-name hs 切换；只写一份，不复制数组
#    <out>.json   元数据（模型、真实维度、群数、参数、环境、provenance）
#
#  用法（Kaggle 7B）：
#    python3 src/semantickv/export_real_features.py \
#      --model Qwen/Qwen2.5-7B-Instruct --load-in-4bit --device cuda \
#      --ctx 8192 --out results/features_7b_prefill
#
#  本地冒烟（0.5B CPU，仅验证管线，不产出 7B 特征）：
#    python3 src/semantickv/export_real_features.py \
#      --model Qwen/Qwen2.5-0.5B --device cpu --ctx 512 \
#      --out /tmp/features_0.5b_smoke
#
#  诚实边界：0.5B 导出的维度是 896，**不是** WB2 要的 3584。只有 7B 实机导出的
#  .npz 才可交付；文件名与 meta 均记录真实维度，避免混淆。
# ============================================================================
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "semantickv"))
sys.path.insert(0, str(ROOT / "src"))

from v41_torch_native_runner import (  # noqa: E402
    load_model, build_structured_input, PrefillInterceptor,
    hidden_from_capture, metric_transform, smooth_hidden,
)
from mock_grouper import MockSemanticGrouper  # noqa: E402


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="导出真实 Prefill 隐状态 + GSC 标签（DS-029）")
    ap.add_argument("--model", default="Qwen/Qwen2.5-3B-Instruct")
    ap.add_argument("--hf-endpoint", default=os.environ.get("HF_ENDPOINT", ""))
    ap.add_argument("--dtype", default="float16", choices=["float32", "bfloat16", "float16"])
    ap.add_argument("--load-in-4bit", action="store_true")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--paragraphs", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--target-layer", type=int, default=12)
    ap.add_argument("--group-layer", default="final")
    ap.add_argument("--smooth-window", type=int, default=8)
    ap.add_argument("--metric", default="centered", choices=["cosine", "centered", "l2"])
    ap.add_argument("--gsc-k", type=int, default=16)
    ap.add_argument("--gsc-tau", type=float, default=0.97)
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="只导出前 N 个 token（0=全部；用于控制 .npz 体积）")
    ap.add_argument("--out", default=None,
                    help="输出前缀（缺省按模型规模：results/real_features_{3b|7b}）")
    ap.add_argument("--environment", default=None,
                    help="环境标签：kaggle-7b / autodl-7b（权威）；缺省自动判定")
    ap.add_argument("--expect-dim", type=int, default=0,
                    help="权威标签下强制校验的维度；0=按模型规模自动（3B→2048, 7B→3584）")
    ap.add_argument("--key-name", default="hidden_states",
                    choices=["hidden_states", "hs"],
                    help="npz 中隐状态数组的键名（DS-038：默认 hidden_states，可选 hs）")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.hf_endpoint:
        os.environ["HF_ENDPOINT"] = args.hf_endpoint
    torch.set_num_threads(int(os.environ.get("SEMANTICKV_THREADS", "8")))

    print(f"[load] {args.model}", flush=True)
    tok, model = load_model(args.model, args.dtype, load_in_4bit=args.load_in_4bit)
    model.eval()

    true_dim = int(model.config.hidden_size)

    # 规模档与期望维度（DS-040：3B 为正式口径，7B 仍兼容）
    SIZE_DIMS = {"3B": 2048, "7B": 3584}
    sz = next((s for s in ("7B", "3B") if s in str(args.model).upper()), None)
    if args.expect_dim == 0:
        args.expect_dim = SIZE_DIMS.get(sz, true_dim)
    if args.out is None:
        args.out = str(ROOT / "results" / f"real_features_{(sz or '0.5B').lower()}")
    n_layers = int(model.config.num_hidden_layers)
    print(f"[arch] hidden_size={true_dim}  layers={n_layers}  "
          f"kv_heads={getattr(model.config, 'num_key_value_heads', '?')}", flush=True)

    ids, truth_topic, truth_occ, _, _ = build_structured_input(
        tok, args.paragraphs, args.repeats, args.ctx)
    N = int(ids.shape[1])
    print(f"[input] N={N} tokens", flush=True)

    target = min(args.target_layer, n_layers - 1)
    icept = PrefillInterceptor(model, target)
    icept.install()
    try:
        with torch.no_grad():
            model(input_ids=ids, use_cache=False)
    finally:
        icept.uninstall()

    H_raw = hidden_from_capture(icept.cap, args.group_layer)   # 原始逐 token 隐状态
    # 平滑 + 归一化**只作用于聚类输入**（提高分组质量）；
    # 交付给 WB2 的是 H_raw —— 未平滑、未归一化的真实幅值。
    # 理由（DS-044）：WB2 训练分布必须等于推理时喂给 Router 的分布，
    # 若导出归一化/平滑后的向量，重训出的 Router 在真实推理时会遇到分布偏移。
    H_clu = smooth_hidden(H_raw, args.smooth_window, renormalize=False)
    Hz = metric_transform(H_clu, args.metric).astype(np.float32)
    ga = MockSemanticGrouper(k=args.gsc_k, tau=args.gsc_tau, metric=args.metric).fit(Hz)
    labels = ga.labels[:N].astype(np.int32)
    G = int(ga.n_groups)

    if H_raw.shape[1] != true_dim:
        print(f"[FATAL] 隐状态维度 {H_raw.shape[1]} != config.hidden_size {true_dim}", file=sys.stderr)
        return 3

    take = N if args.max_tokens <= 0 else min(N, args.max_tokens)
    hidden = H_raw[:take].astype(np.float16)
    labels = labels[:take]

    env = args.environment or (
        f"kaggle-{sz.lower()}" if (args.load_in_4bit and sz)
        else f"local-sandbox-cpu-proxy-{sz or '0.5B'}")

    # 权威标签下强制维度校验（DS-035/040：严格 (N, 2048) 或 (N, 3584)）
    # 两类标签分开看（DS-20261019-044）：
    #   AUTHORITATIVE —— GPU 实机 + 真实模型，设备与模型双权威
    #   REAL_MODEL    —— 真实模型推理但跑在 CPU（无 GPU / 无量化）；
    #                    模型是真的、隐状态是真的，只是设备非 GPU。
    # 二者都要求「声明了就维度必须对」，区别只在 dim_check 的措辞。
    AUTHORITATIVE = {"kaggle-7b", "autodl-7b", "kaggle-3b", "autodl-3b"}
    REAL_MODEL = {"local-sandbox-cpu-3b", "local-sandbox-cpu-7b",
                  "local-cpu-3b", "local-cpu-7b"}
    STRICT_ENVS = AUTHORITATIVE | REAL_MODEL

    if env in STRICT_ENVS and true_dim != args.expect_dim:
        print(f"[FATAL] 声明 {env}，但 model.config.hidden_size={true_dim} "
              f"!= 期望 {args.expect_dim}。拒绝产出（DS-035/044 验收标准）。",
              file=sys.stderr)
        return 4

    # 维度判定与设备权威性解耦：模型是真的且维度对，就不该被贬成 PROXY。
    if true_dim != args.expect_dim:
        dim_check = "FAIL" if env in AUTHORITATIVE else "PROXY"
    elif env in AUTHORITATIVE:
        dim_check = "PASS"
    elif env in REAL_MODEL:
        dim_check = "PASS_DIM_CPU"      # 真实模型·维度对·设备为 CPU
    else:
        dim_check = "PROXY_DIM_OK"      # 小模型代理但维度恰好对上

    meta = {
        "purpose": "WB2 真实特征重训数据（DS-20261019-029-WB1 步骤 2）",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model,
        "hidden_size": true_dim,
        "expected_dim": args.expect_dim,
        "dim_check": dim_check,
        "n_tokens": int(take),
        "n_groups": G,
        "label_method": f"GSC offline clustering (k={args.gsc_k}, tau={args.gsc_tau}, metric={args.metric})",
        "group_layer": args.group_layer,
        "target_layer": target,
        "feature_dtype": "float16",
        # 保真声明（DS-044）：导出的是未平滑、未归一化的原始隐状态；
        # 平滑与归一化仅用于生成 labels，不影响交付数组。
        "raw_hidden_exported": True,
        "l2_normalized": False,
        "smoothed_for_export": False,
        "smoothing_used_for_labels_only": int(args.smooth_window),
        # D3 纪律要求：results/*.json 必须有顶层 environment 块与 config.device。
        "environment": {
            "kind": env,
            "model": args.model,
            "device": args.device,
            "dtype": args.dtype,
            "load_in_4bit": bool(args.load_in_4bit),
            "note": ("真实目标模型 + CPU + 无量化；隐状态来自真实前向，"
                     "设备非 GPU 是唯一差异。")
                    if env in REAL_MODEL else
                    ("GPU 实机权威结果。" if env in AUTHORITATIVE
                     else "小模型代理冒烟。"),
        },
        "config": {
            "model": args.model,
            "device": args.device,
            "dtype": args.dtype,
            "load_in_4bit": bool(args.load_in_4bit),
            "ctx": args.ctx,
            "paragraphs": args.paragraphs,
            "repeats": args.repeats,
            "target_layer": target,
            "group_layer": args.group_layer,
            "smooth_window": args.smooth_window,
            "metric": args.metric,
            "gsc_k": args.gsc_k,
            "gsc_tau": args.gsc_tau,
            "expect_dim": args.expect_dim,
            "out": str(args.out),
            "environment": env,
        },
        "env": {"kind": env, "device": args.device, "load_in_4bit": args.load_in_4bit},
        "provenance": {
            "hidden_state_source": "REAL model prefill (not synthetic)",
            "real_hidden_state_available": True,
            "real_gsc_available": True,
            "model_is_real_target": env in STRICT_ENVS,
            "device_is_gpu": env in AUTHORITATIVE,
            "note": (
                "真实目标模型推理产物，设备为 GPU，可作 WB2 交付物。"
                if env in AUTHORITATIVE else
                "真实目标模型推理产物（CPU / 无量化）。隐状态与分组标签均来自真实前向，"
                "可作为 WB2 重训输入；仅『设备非 GPU』一点与实机不同，故标 PASS_DIM_CPU。"
                if env in REAL_MODEL else
                "本地小模型冒烟，维度与目标不符，不可作为 WB2 交付物。"
            ),
        },
        "consumption_contract": {
            "expected_by_wb2": "Linear(hidden_size, 256) 首层；hidden_size 必须等于本文件 hidden_size",
            "wb2_current_mismatch": ("WB2 semantic_router.pt 为 input_dim=1024，"
                                     "与 7B 的 3584 不符（GAP-DS26-01）"),
        },
    }

    # ── 键名统一（DS-20261019-038-WB1）─────────────────────────────────
    # 主键 hidden_states（与 DS 口径及 anchor_grouper 命名一致）；
    # 可选 --key-name hs 兼容 WB2 的短名。**只写一份**，避免把 58MB 数组存两遍。
    key = (args.key_name or "hidden_states").strip()
    if key not in ("hidden_states", "hs"):
        print(f"[FATAL] 不支持的键名 {key!r}；只允许 hidden_states / hs", file=sys.stderr)
        return 5

    meta["npz_contract"] = {
        "key_hidden": key,
        "key_labels": "labels",
        "key_meta": "meta",
        "hidden_shape": [int(hidden.shape[0]), int(hidden.shape[1])],
        "hidden_dtype": str(hidden.dtype),
        "labels_shape": [int(labels.shape[0])],
        "labels_dtype": str(labels.dtype),
        "accepted_aliases": ["hidden_states", "hs", "hidden"],
        "note": "单键写入（不复制数组）；WB2 侧请按 accepted_aliases 容错读取。",
    }

    out = Path(args.out)
    # ⚠ 不能用 Path.with_suffix：当 out 形如 "real_features_0.5b" 时，
    # with_suffix(".npz") 会把 ".5b" 当成后缀替换掉 → 变成 real_features_0.npz。
    # 这里按字符串拼接，并容忍用户显式传入 .npz/.json。
    if out.suffix.lower() in (".npz", ".json"):
        out = Path(str(out)[: -len(out.suffix)])
    out.parent.mkdir(parents=True, exist_ok=True)
    npz_path = Path(str(out) + ".npz")
    json_path = Path(str(out) + ".json")
    np.savez_compressed(npz_path,
                        labels=labels,
                        meta=np.array(json.dumps(meta, ensure_ascii=False)),
                        **{key: hidden})
    json_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 回读自校验：键名/形状/dtype 必须与契约一致
    with np.load(npz_path) as chk:
        got = set(chk.files)
        assert key in got and "labels" in got and "meta" in got, f"npz 键缺失: {got}"
        assert chk[key].shape == hidden.shape, "npz 隐状态形状不符"
        assert chk["labels"].shape == labels.shape, "npz 标签形状不符"

    size_mb = npz_path.stat().st_size / 1024 / 1024
    print(f"\n[done] {npz_path}  ({size_mb:.1f} MB)")
    print(f"       keys = {sorted(set(np.load(npz_path).files))}")
    print(f"       {key} {hidden.shape} = (tokens, {true_dim})   labels {labels.shape}   G={G}")
    print(f"       env={env}   dim_check={dim_check}")
    if dim_check in ("PASS", "PASS_DIM_CPU"):
        print("       ✔ 可交付 WB2：真实模型前向的真实隐状态 + 真实 GSC 标签。")
        if dim_check == "PASS_DIM_CPU":
            print("         （设备为 CPU / bf16 无量化；分布来自真实模型，非合成件。）")
    else:
        print(f"       ⚠ dim_check={dim_check}：不可作为 WB2 交付物。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
