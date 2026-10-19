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
#    <out>.npz    hidden(N,D) float16 / labels(N,) int32 / meta(json string)
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
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B")
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
    ap.add_argument("--out", default=str(ROOT / "results" / "features_7b_prefill"))
    ap.add_argument("--environment", default=None, help="环境标签，如 kaggle-7b")
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

    H = hidden_from_capture(icept.cap, args.group_layer)      # (N, true_dim)
    H = smooth_hidden(H, args.smooth_window)
    Hz = metric_transform(H, args.metric).astype(np.float32)
    ga = MockSemanticGrouper(k=args.gsc_k, tau=args.gsc_tau, metric=args.metric).fit(Hz)
    labels = ga.labels[:N].astype(np.int32)
    G = int(ga.n_groups)

    if H.shape[1] != true_dim:
        print(f"[FATAL] 隐状态维度 {H.shape[1]} != config.hidden_size {true_dim}", file=sys.stderr)
        return 3

    take = N if args.max_tokens <= 0 else min(N, args.max_tokens)
    hidden = H[:take].astype(np.float16)
    labels = labels[:take]

    env = args.environment or (
        "kaggle-7b" if (args.load_in_4bit and "7B" in args.model)
        else f"local-sandbox-cpu-proxy-{'7B' if '7B' in args.model else '0.5B'}")

    meta = {
        "purpose": "WB2 真实特征重训数据（DS-20261019-029-WB1 步骤 2）",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": args.model,
        "hidden_size": true_dim,
        "n_tokens": int(take),
        "n_groups": G,
        "label_method": f"GSC offline clustering (k={args.gsc_k}, tau={args.gsc_tau}, metric={args.metric})",
        "group_layer": args.group_layer,
        "target_layer": target,
        "feature_dtype": "float16",
        "env": {"kind": env, "device": args.device, "load_in_4bit": args.load_in_4bit},
        "provenance": {
            "hidden_state_source": "REAL model prefill (not synthetic)",
            "real_hidden_state_available": True,
            "real_gsc_available": True,
            "note": "这是真实 7B 隐状态与真实 GSC 标签；非合成代理件。"
                    if env == "kaggle-7b" else
                    "本次为本地小模型冒烟，维度与 7B 不符，不可作为 WB2 交付物。",
        },
        "consumption_contract": {
            "expected_by_wb2": "Linear(hidden_size, 256) 首层；hidden_size 必须等于本文件 hidden_size",
            "wb2_current_mismatch": ("WB2 semantic_router.pt 为 input_dim=1024，"
                                     "与 7B 的 3584 不符（GAP-DS26-01）"),
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out.with_suffix(".npz"),
                        hidden=hidden, labels=labels,
                        meta=np.array(json.dumps(meta, ensure_ascii=False)))
    out.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    size_mb = out.with_suffix(".npz").stat().st_size / 1024 / 1024
    print(f"\n[done] {out.with_suffix('.npz')}  ({size_mb:.1f} MB)")
    print(f"       hidden {hidden.shape} = (tokens, {true_dim})   labels {labels.shape}   G={G}")
    print(f"       env={env}")
    if env != "kaggle-7b":
        print("       ⚠ 非 7B 实机产物：仅用于管线冒烟，不可交付 WB2。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
