#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
#  fixed_route_assault.py — 7B 混合保护「固定路由」突击测试（DS-20261019-026-WB1）
# ----------------------------------------------------------------------------
#  背景：WB2 旧权重被 DS 裁定不可当 per-query 路由，新 MLP 权重待到达。DS 要求
#  WB1 不等权重，直接用「GSC 离线质心 / 空壳固定路由」跑通 7B 端到端，先验证
#  混合保护（Sink+Win+TopK）能否在 8K 下稳住 45% 以上的 Argmax 底线。
#
#  本脚本**不依赖任何训练好的 Router**：复用 v41_torch_native_runner 的
#  CompressedAttentionPatcher（混合分段注意力，hybrid / blockdiag），仅把「分组标签」
#  的来源做成可插拔的 group-mode：
#    gsc     —— GSC 离线质心（对隐状态做 kNN 连通分量聚类，离线、无学习）
#    uniform —— 顺序定长分块（最朴素的「固定路由」：token 0..N/G-1→群0 …）
#    qk      —— 对 post-RoPE 的 K 做相似度聚类（"Query/Key 简单点积"式路由）
#
#  输出 kaggle_7b_fixed_route.json：baseline 门禁 + 各路由策略的 Argmax，
#  按 H-DS10 口径标注 MAINTAIN(>45%) / ROPE_FRACTURE(<45%)。
#
#  环境标签（D3 隔离）：本脚本既可在 Kaggle 7B 实机跑（--environment kaggle-7b），
#  也可在沙盒用 0.5B CPU 做管线代理校验（默认自动识别）。两种结果必须分别标注，
#  0.5B 代理 ≠ 7B 结论。
# ============================================================================
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

from v41_torch_native_runner import (  # noqa: E402
    load_model, build_structured_input, PrefillInterceptor,
    CompressedAttentionPatcher, hidden_from_capture, metric_transform,
    smooth_hidden, parse_layer_spec, cosine_per_position,
)
from mock_grouper import MockSemanticGrouper  # noqa: E402


def needle_hit(answer: str, target: str = "42") -> bool:
    """归一化后的精确匹配（避免 '1.414213' 含 '42' 的子串误判）。"""
    ans = answer.strip().strip("。，.,!！?？:：\"'""")
    nums = re.findall(r"\d+(?:\.\d+)?", ans)
    return target in nums or ans == target


def make_labels(mode: str, H: np.ndarray, k_post: torch.Tensor, N: int,
                G: int, metric: str, gsc_k: int, gsc_tau: float,
                qk_tau: float) -> Tuple[np.ndarray, int]:
    """按 group-mode 生成「基础分组标签」（不含 sink/window 特权扩展，
    特权扩展由 CompressedAttentionPatcher.set_groups 内部完成）。

    固定确定性配置（不依赖 ground-truth 扫描，契合「固定路由」语义）：
      uniform —— 顺序定长分块（最朴素固定路由）
      gsc     —— 对隐状态做离线 GSC 聚类（gsc_k / gsc_tau）
      qk      —— 对 post-RoPE 的 K 跨头均值后做 GSC 聚类（cosine / qk_tau）
    返回 (labels (N,), n_groups)。
    """
    if mode == "uniform":
        lab = (np.arange(N) * G // N).astype(np.int64)
        return lab, int(lab.max()) + 1

    if mode == "qk":
        # post-RoPE K: (1, Hkv, N, hd) -> 跨头均值 -> (N, hd)，再 GSC 聚类
        Kh = k_post[0].reshape(k_post.shape[1], N, -1)          # (Hkv, N, hd)
        # bf16 张量不能直接 .numpy()（numpy 无 bfloat16），先升 fp32
        Km = Kh.mean(axis=0).float().numpy().astype(np.float32)   # (N, hd)
        ga = MockSemanticGrouper(k=gsc_k, tau=qk_tau, metric="cosine").fit(Km)
        return ga.labels[:N].copy(), int(ga.n_groups)

    # gsc（默认）：对隐状态做离线 GSC 聚类
    ga = MockSemanticGrouper(k=gsc_k, tau=gsc_tau, metric=metric).fit(H)
    return ga.labels[:N].copy(), int(ga.n_groups)


def run_needle(model, tok, args, ids_n: torch.Tensor,
               mode: Optional[str], G: int, metric: str,
               compress_layers, n_layers: int) -> Dict:
    """对单条（含 needle 的）上下文跑一次推理，返回命中与保真度。

    mode=None → 无压缩基线（直接 generate）。
    否则：① 在该 needle 上下文上重算分组标签（关键：标签长度必须与序列长度一致）；
         ② 用 patcher 做混合压缩推理；③ 同上下文原始 hidden 做保真度对照。
    """
    Nn = ids_n.shape[1]
    attn_mask = torch.ones_like(ids_n)
    device = next(model.parameters()).device
    ids_n = ids_n.to(device)

    if mode is None:
        with torch.no_grad():
            out = model.generate(ids_n, attention_mask=attn_mask,
                                 max_new_tokens=args.max_new_tokens,
                                 do_sample=False, use_cache=True,
                                 pad_token_id=tok.eos_token_id or tok.pad_token_id)
        ans = tok.decode(out[0][Nn:], skip_special_tokens=True)
        return {"hit": needle_hit(ans), "answer": ans.strip()}

    # ① 原始（无压缩）prefill，捕获该上下文原始 final_hidden（保真度参照）
    icept_o = PrefillInterceptor(model, args.target_layer)
    icept_o.install()
    icept_o.run_prefill(ids_n, need_hidden_states=(args.group_layer != "final"))
    orig_hidden = icept_o.cap["final_hidden"]
    icept_o.uninstall()

    # ② 在该 needle 上下文上重算分组标签（长度 = Nn）
    Hn = smooth_hidden(metric_transform(
        hidden_from_capture(icept_o.cap, args.group_layer), args.metric),
        args.smooth_window, renormalize=(args.metric != "l2"))
    kp = icept_o.cap["k_post"]
    labels, G_actual = make_labels(mode, Hn, kp, Nn, G, args.metric,
                                   args.gsc_k, args.gsc_tau, args.qk_tau)

    # ③ 压缩 prefill（构造压缩 KV 集）+ 捕获压缩后 final_hidden
    cap_hidden = {}
    def cap_norm(_m, _inp, out):
        cap_hidden["h"] = out.detach()
    h_norm = model.model.norm.register_forward_hook(cap_norm)
    patcher = CompressedAttentionPatcher(
        model, mode=args.compress_mode, topk_per_group=args.topk_per_group,
        sink_tokens=args.sink_tokens, window_tokens=args.window_tokens,
        compress_layers=compress_layers, kmean_renorm=args.kmean_renorm,
        group_score_prior=args.group_score_prior, mid_scope=args.mid_scope)
    patcher.set_groups(labels, Nn)
    patcher.install()
    try:
        with torch.no_grad():
            model(ids_n, attention_mask=attn_mask, use_cache=False)
    finally:
        patcher.uninstall()
        h_norm.remove()
    with torch.no_grad():
        out = model.generate(ids_n, attention_mask=attn_mask,
                             max_new_tokens=args.max_new_tokens,
                             do_sample=False, use_cache=True,
                             pad_token_id=tok.eos_token_id or tok.pad_token_id)
    ans = tok.decode(out[0][Nn:], skip_special_tokens=True)
    cos = cosine_per_position(cap_hidden["h"], orig_hidden)
    return {"hit": needle_hit(ans), "answer": ans.strip(),
            "n_groups": int(G_actual),
            "prefill_final_hidden_cos": cos}


# ── 权威环境标签与守卫（DS-029 / DS-035 / DS-040 3B 降级）──────────────
# kaggle-7b / autodl-7b ：7B（hidden 3584）
# kaggle-3b / autodl-3b ：3B（hidden 2048）——DS-040 起为省算力的正式口径
AUTHORITATIVE_ENVS = {"kaggle-7b", "autodl-7b", "kaggle-3b", "autodl-3b"}
# 真实目标模型但跑在 CPU（无 GPU / 无量化，DS-20261019-044 沙盒闭环）：
# 模型与隐状态是真的，只有「设备非 GPU」一点不同，不应被说成小模型代理。
REAL_MODEL_ENVS = {"local-sandbox-cpu-3b", "local-sandbox-cpu-7b",
                   "local-cpu-3b", "local-cpu-7b"}
SIZE_DIMS = {"3B": 2048, "7B": 3584}


def model_size(model: str) -> Optional[str]:
    """从模型名推断规模档（'3B' / '7B' / None）。"""
    m = str(model).upper()
    for s in ("7B", "3B"):
        if s in m:
            return s
    return None


def compute_env_claims(model: str, load_in_4bit: bool, ctx: int,
                       device: str, cuda_available: bool) -> dict:
    """把「是否真的是 3B/7B 实机」拆成四条可核验断言。"""
    return {
        "model_size_in_3b_7b": model_size(model) is not None,
        "load_in_4bit": bool(load_in_4bit),
        "ctx_ge_4096": int(ctx) >= 4096,
        "device_uses_cuda": (device == "cuda") or bool(cuda_available),
    }


def validate_env_label(env_kind: str, claims: dict) -> Tuple[bool, List[str]]:
    """权威标签必须四条断言全绿；代理标签一律放行。

    返回 (ok, failed)。ok=False 时调用方必须拒绝落盘。
    """
    if env_kind not in AUTHORITATIVE_ENVS:
        return True, []
    failed = [k for k, v in claims.items() if not v]
    return (not failed), failed


def main() -> int:
    ap = argparse.ArgumentParser(description="7B 混合保护固定路由突击测试（DS-026）")
    ap.add_argument("--model", default="Qwen/Qwen2.5-3B-Instruct")
    ap.add_argument("--hf-endpoint", default=__import__("os").environ.get("HF_ENDPOINT", ""),
                    help="HF 镜像源，如 https://hf-mirror.com（国内加速）")
    ap.add_argument("--dtype", default="float32", choices=["float32", "bfloat16", "float16"])
    ap.add_argument("--load-in-4bit", action="store_true")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--paragraphs", type=int, default=8)
    ap.add_argument("--repeats", type=int, default=6)
    ap.add_argument("--target-layer", type=int, default=12)
    ap.add_argument("--group-layer", default="final")
    ap.add_argument("--smooth-window", type=int, default=8)
    ap.add_argument("--group-mode", default="gsc,uniform,qk",
                    help="逗号分隔的非学习路由：gsc(离线质心)/uniform(顺序分块)/qk(K点积聚类)")
    ap.add_argument("--n-groups", type=int, default=16, help="uniform 模式的群数")
    ap.add_argument("--metric", default="centered", choices=["cosine", "centered", "l2"])
    ap.add_argument("--gsc-k", type=int, default=16, help="gsc/qk 模式的 kNN 近邻数")
    ap.add_argument("--gsc-tau", type=float, default=0.97, help="gsc（隐状态）聚类的连通阈值")
    ap.add_argument("--qk-tau", type=float, default=0.90, help="qk（K 点积聚类）的连通阈值")
    ap.add_argument("--compress-mode", default="hybrid", choices=["flat_mean", "weighted", "topk", "hybrid"])
    ap.add_argument("--topk-per-group", type=int, default=8)
    ap.add_argument("--sink-tokens", type=int, default=4)
    ap.add_argument("--window-tokens", type=int, default=64)
    ap.add_argument("--mid-scope", default="blockdiag", choices=["blockdiag", "global"])
    ap.add_argument("--compress-layers", default="0.25-0.75")
    ap.add_argument("--kmean-renorm", default="none", choices=["none", "member"])
    ap.add_argument("--group-score-prior", default="logcount", choices=["none", "logcount"])
    ap.add_argument("--needle", default="请注意，一个重要事实：小王最喜欢的数字是 42。")
    ap.add_argument("--needle-positions", default="0.15,0.5,0.85")
    ap.add_argument("--question", default="小王最喜欢的数字是多少？请只回答数字：")
    ap.add_argument("--max-new-tokens", type=int, default=8)
    ap.add_argument("--out", default=None,
                    help="缺省按模型规模命名：results/kaggle_{3b|7b}_fixed_route.json")
    ap.add_argument("--environment", default=None,
                    help="环境标签覆盖：如 kaggle-7b / local-sandbox-cpu-proxy-0.5B。默认自动识别")
    args = ap.parse_args()

    import torch as _t
    import os as _os
    if args.hf_endpoint:
        _os.environ["HF_ENDPOINT"] = args.hf_endpoint
    _t.set_num_threads(int(_os.environ.get("SEMANTICKV_THREADS", "8")))

    group_modes = [m.strip() for m in args.group_mode.split(",") if m.strip()]

    print(f"[load] {args.model}", flush=True)
    tok, model = load_model(args.model, args.dtype, load_in_4bit=args.load_in_4bit)
    if not args.load_in_4bit:
        model.to(args.device)
    n_layers = model.config.num_hidden_layers
    print(f"[load] 层 {n_layers} hidden {model.config.hidden_size}", flush=True)

    # ---- 结构化输入 + 标准 prefill（仅用于报告模型规格，分组在每个 needle
    #      上下文内独立重算，保证标签长度 = 序列长度，避免拼接错位）----
    print(f"[prefill] {args.ctx} token 上下文（仅规格检查）", flush=True)
    input_ids, truth, truth_occ, _, _ = build_structured_input(
        tok, args.paragraphs, args.repeats, args.ctx, seed=7)
    N = input_ids.shape[1]
    compress_layers = parse_layer_spec(args.compress_layers, n_layers)

    # ---- 基线门禁：无压缩 needle 命中 ----
    print("[baseline] 无压缩基线门禁（目标 ≥2/3）", flush=True)
    baseline = {"positions": [], "hits": [], "answers": []}
    for frac in [float(x) for x in args.needle_positions.split(",")]:
        ids_n, _, _, npos, _ = build_structured_input(
            tok, args.paragraphs, args.repeats, args.ctx,
            needle=args.needle, needle_frac=frac, question=args.question, seed=7)
        r = run_needle(model, tok, args, ids_n, None, args.n_groups, args.metric,
                       compress_layers, n_layers)
        baseline["positions"].append(frac)
        baseline["hits"].append(bool(r["hit"]))
        baseline["answers"].append(r["answer"])
        print(f"  pos={frac:.2f} 命中={r['hit']} -> {r['answer']!r}", flush=True)
    base_rate = sum(baseline["hits"]) / max(len(baseline["hits"]), 1)
    gate_pass = base_rate >= 2 / 3
    print(f"[baseline] 命中率 {base_rate:.3f} 门禁={'PASS' if gate_pass else 'FAIL'}", flush=True)

    # ---- 固定路由突击：每种 group-mode 跑混合压缩（标签在该上下文内重算）----
    assault: List[Dict] = []
    any_rope_fracture = False
    for mode in group_modes:
        print(f"[assault:{mode}] 混合保护（{args.compress_mode}/"
              f"{args.mid_scope}）+ 固定路由", flush=True)
        positions, hits, coss, gs = [], [], [], []
        for frac in [float(x) for x in args.needle_positions.split(",")]:
            ids_n, _, _, npos, _ = build_structured_input(
                tok, args.paragraphs, args.repeats, args.ctx,
                needle=args.needle, needle_frac=frac, question=args.question, seed=7)
            r = run_needle(model, tok, args, ids_n, mode, args.n_groups, args.metric,
                           compress_layers, n_layers)
            positions.append(frac)
            hits.append(bool(r["hit"]))
            coss.append(r.get("prefill_final_hidden_cos", {}))
            gs.append(r.get("n_groups"))
            print(f"  pos={frac:.2f} 命中={r['hit']} cos_mean="
                  f"{r.get('prefill_final_hidden_cos', {}).get('mean')}", flush=True)
        argmax = sum(hits) / max(len(hits), 1)
        rope_fracture = (argmax < 0.45)
        any_rope_fracture = any_rope_fracture or rope_fracture
        assault.append({
            "group_mode": mode, "n_groups": int(max(gs) if gs else args.n_groups),
            "positions": positions, "compressed_hits": hits,
            "argmax": round(argmax, 4),
            "rope_fracture": rope_fracture,
            "prefill_final_hidden_cos_mean": [c.get("mean") for c in coss],
            "prefill_final_hidden_cos_p05": [c.get("p05") for c in coss],
        })
        print(f"[assault:{mode}] Argmax={argmax:.3f} "
              f"{'ROPE_FRACTURE' if rope_fracture else 'OK(>45%)'}", flush=True)

    # ---- 结论 ----
    if not gate_pass:
        verdict = "INCONCLUSIVE"
        note = "基线门禁未过（无压缩命中 <2/3）：模型容量/加载异常，固定路由结论无效。"
    else:
        verdict = "ROPE_FRACTURE" if any_rope_fracture else "MAINTAIN"
        _sz = model_size(args.model) or "目标模型"
        note = (f"所有固定路由下 {_sz} 混合保护 Argmax >45%：混合保护在多段拼接下仍稳住底线，"
                "维持方案（H-DS10 DS-026 口径）。" if verdict == "MAINTAIN"
                else f"存在固定路由下 {_sz} 混合保护 Argmax <45%：崩盘非路由差，而是重组在多段拼接时"
                     "破坏了 RoPE 连续性（标记 ROPE_FRACTURE，H-DS10 DS-026 口径）。")

    sz = model_size(args.model)
    if args.out is None:
        args.out = str(ROOT / "results" / f"kaggle_{(sz or '0.5B').lower()}_fixed_route.json")
    env_kind = args.environment or (
        f"kaggle-{sz.lower()}" if (args.load_in_4bit or
                                   (torch.cuda.is_available() and sz))
        else f"local-sandbox-cpu-proxy-{sz or '0.5B'}")

    # ── 环境口径守卫（DS-20261019-029 验收项；DS-035 兼容 autodl-7b）──────
    # 权威文件必须标 kaggle-7b 或 autodl-7b，且不得把代理结果冒称为 7B 权威结论。
    claims = compute_env_claims(args.model, args.load_in_4bit, args.ctx,
                                args.device, torch.cuda.is_available())
    ok, failed = validate_env_label(env_kind, claims)
    if not ok:
        raise SystemExit(
            f"[FATAL] 环境标签不一致：声明 {env_kind}，但以下条件不满足 {failed}。\n"
            f"        model={args.model} ctx={args.ctx} device={args.device} "
            f"4bit={args.load_in_4bit}\n"
            f"        拒绝落盘，避免把代理结果误标为 7B 权威结论（DS-029/035 验收标准）。\n"
            f"        若只是本地冒烟，请去掉 --environment {env_kind}。")

    env_note = (f"{sz or ''} 实机权威结果（{env_kind}）。" if env_kind in AUTHORITATIVE_ENVS
                else f"真实 {sz or ''} 模型推理，但设备为 CPU / 无量化（{env_kind}）："
                     "路由与保真度数字来自真实前向，可直接用于方案判断；"
                     "与 GPU 实机的差异仅在数值精度与算速。"
                if env_kind in REAL_MODEL_ENVS
                else "0.5B/小模型 CPU 代理仅校验管线；权威 3B/7B 数字需实机"
                     "（--environment kaggle-3b / autodl-3b）或真实模型 CPU（local-sandbox-cpu-3b）。")

    report = {
        "config": vars(args),
        "environment": {
            "kind": env_kind,
            "model": args.model, "device": args.device,
            "load_in_4bit": args.load_in_4bit,
            "ctx": args.ctx,
            "claims_checked": {n: bool(v) for n, v in claims.items()},
            "note": env_note,
        },
        "gate": {"baseline_hit_rate": round(base_rate, 4), "gate_pass": gate_pass,
                 "baseline_positions": baseline["positions"],
                 "baseline_hits": baseline["hits"]},
        "fixed_route_assault": assault,
        "verdict": verdict,
        "note": note,
    }
    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[done] {p} | verdict={verdict} | baseline={base_rate:.3f} "
          f"| modes={[a['group_mode'] for a in assault]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
