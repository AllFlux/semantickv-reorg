#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_router.py — H-DS17 Router 空壳基准（DS-20261018-011-WB1）。

验证 DS 规格 SemanticRouter（learned_router.SemanticRouter，纯 Linear+ReLU）：
  1. 参数量 < 50M（默认 1024/256/64 与 7B 3584/256/64 两档配置）；
  2. 单次前向延迟（H-DS17 通过线 <0.5ms，证伪线 >2ms）；
  3. Router 权重显存占 7B KV cache 比例（证伪线 >10% → 退回 GSC 质心检索）。

CPU 冒烟数据即可完成 1/3 与量级判断；GPU(T4) 终测由 run_on_kaggle.ipynb
Step 3 产出 results/kaggle_7b_vllm_plugin_bench.json。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "semantickv" / "vllm_plugin"))
from learned_router import LearnedSemanticRouter  # noqa: E402

PARAM_LIMIT = 50_000_000        # H-DS17: <50M
LATENCY_PASS_MS = 0.5           # H-DS17 通过线: <0.5ms
LATENCY_FAIL_MS = 2.0           # H-DS17 证伪线: >2ms
KV_MEM_FAIL_PCT = 10.0          # H-DS17 证伪线: >10% KV 显存


def bench_forward(net: torch.nn.Module, q_dim: int, n_tok: int,
                  reps: int = 200) -> dict:
    """(1, n_tok, q_dim) 的前向延迟统计（ms）。"""
    x = torch.randn(1, n_tok, q_dim)
    with torch.no_grad():
        for _ in range(20):                       # warmup
            net(x)
        ts = []
        for _ in range(reps):
            t0 = time.perf_counter()
            net(x)
            ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return {"p50_ms": round(ts[len(ts) // 2], 4),
            "p99_ms": round(ts[min(len(ts) - 1, int(len(ts) * 0.99))], 4),
            "max_ms": round(ts[-1], 4),
            "reps": reps}


def main() -> int:
    torch.manual_seed(0)
    # 小矩阵（2048×1024→256）多线程切换开销反而更大，单线程最稳
    torch.set_num_threads(1)
    out = {
        "config": {"device": "cpu", "dtype": "float32",
                   "purpose": "H-DS17 Router 空壳基准（DS-20261018-011-WB1）",
                   "reps": 200},
        "environment": {"kind": "cpu-synthetic+real-arch-smoke",
                        "note": "CPU 冒烟（单线程）；延迟判定以 Kaggle T4 GPU 实测为准"},
        "arch": "ds011_linear_relu_linear (q_dim->hidden->num_groups)",
        "rows": [],
    }

    configs = [("default(DS 规格默认)", 1024, 256, 64),
               ("qwen2.5-7b(hidden=3584)", 3584, 256, 64)]
    for name, q_dim, hidden, groups in configs:
        net = LearnedSemanticRouter(q_dim=q_dim, hidden_dim=hidden,
                                    num_groups=groups)
        net.eval()
        n_params = net.num_parameters()
        # 两个场景：N=1 单 query（decode 路径，H-DS17「一次群路由推理」口径）
        #          N=2048 整段（prefill 后一次性路由）
        lat_1 = bench_forward(net, q_dim, 1)
        lat_2048 = bench_forward(net, q_dim, 2048)
        weight_mb = n_params * 4 / 1024 / 1024    # fp32 权重字节
        # 7B KV cache 估算（Qwen2.5-7B: 28层, 8 KV头, head_dim=128, fp16 KV, ctx=4096）
        kv_bytes = 28 * 2 * 8 * 128 * 2 * 4096
        kv_pct = 100.0 * weight_mb * 1024 * 1024 / kv_bytes
        row = {
            "config_name": name, "q_dim": q_dim, "hidden_dim": hidden,
            "num_groups": groups,
            "params": n_params, "params_under_50m": n_params < PARAM_LIMIT,
            "latency_single_query_N1": lat_1,
            "latency_prefill_N2048": lat_2048,
            "latency_note": ("CPU 参考值；H-DS17 通过线 <0.5ms / 证伪线 >2ms "
                             "以 Kaggle T4 GPU 实测（kaggle_7b_vllm_plugin_bench.json）为准"),
            "weight_mb_fp32": round(weight_mb, 4),
            "kv_cache_mb_7b_ctx4096_fp16": round(kv_bytes / 1024 / 1024, 2),
            "kv_mem_pct": round(kv_pct, 6),
            "kv_mem_fail_10pct": kv_pct > KV_MEM_FAIL_PCT,
        }
        out["rows"].append(row)

    # 判定：参数量与 KV 显存占比与设备无关，CPU 即可终判；
    # 延迟因 CPU/GPU 差 1-2 个数量级，不在此处终判，留待 Kaggle GPU。
    hw_ok = (all(r["params_under_50m"] for r in out["rows"])
             and not any(r["kv_mem_fail_10pct"] for r in out["rows"]))
    out["summary"] = {
        "verdict": "PASS(硬件无关项)" if hw_ok else "FAIL",
        "params_under_50m": all(r["params_under_50m"] for r in out["rows"]),
        "kv_mem_under_10pct": not any(r["kv_mem_fail_10pct"] for r in out["rows"]),
        "latency_gpu_pending": True,
        "note": ("参数量 ~0.28M~0.93M（≪50M）、权重占 7B KV cache <1%（≪10%）——"
                 "硬件无关项在 CPU 上已终判 PASS；单 query 前向延迟 CPU 参考值见 "
                 "latency_single_query_N1，GPU 终测待 Kaggle T4"),
    }
    dest = Path(__file__).resolve().parent.parent / "results" / "router_bench.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n-> {dest}")
    return 0 if hw_ok else 1


if __name__ == "__main__":
    sys.exit(main())
