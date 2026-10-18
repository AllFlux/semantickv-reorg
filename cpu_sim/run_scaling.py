#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cpu_sim/run_scaling.py — CPU 规模基准：GSC 聚类的时间/内存随 N 的缩放。

回答两个问题：
  1) GSC 聚类在纯 CPU 上的时间复杂度（实测 total_s 随 N 的曲线）；
  2) N=100k / 500k 时内存会不会撑爆 123GB（实测 peak_rss + 解析外推）。

策略：
  - N ≤ 200k：全 768 维精确 kNN（faiss IndexFlatIP，32 线程）；
  - N > 200k：默认加 128 维高斯随机投影（JL 保距，把 O(N²·d) 压低 6 倍），
    可用 --exact-large 强制全维（慢，用于验证投影是否显著损失 ARI）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

import numpy as np

from synth import adjusted_rand_index, make_semantic_batch
from env_tag import cpu_synthetic_env
from mock_grouper import MemTracker, MockSemanticGrouper, cgroup_mem_limit_mb, rss_mb


def run_one(N: int, d: int = 768, topics: int = 32, k: int = 16,
            project_dim=None, backend: str = "auto", seed: int = 42) -> dict:
    X, y = make_semantic_batch(N, d, topics, rng=np.random.default_rng(seed))
    base = rss_mb()
    g = MockSemanticGrouper(k=k, project_dim=project_dim, backend=backend)
    with MemTracker() as mt:
        ga = g.fit(X)
    ari = adjusted_rand_index(y, ga.labels)
    return {
        "N": N, "d": d, "project_dim": project_dim, "k": k, "topics": topics,
        "total_s": ga.meta["timings"]["total"],
        "stage_s": ga.meta["timings"],
        "n_groups": ga.n_groups,
        "n_mutual_edges": ga.meta["n_mutual_edges"],
        "intra_sim": ga.meta["intra_sim"],
        "ari_vs_truth": ari,
        "peak_rss_mb": round(mt.peak_mb, 1),
        "rss_delta_mb": round(mt.peak_mb - base, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=str, default="10000,50000,100000,128000,500000")
    ap.add_argument("--exact-large", action="store_true",
                    help="N>200k 也跑全 768 维（慢；用于对照投影损失）")
    ap.add_argument("--out", type=str, default=str(ROOT / "results" / "scaling_results.json"))
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    results = []
    t_start = time.time()
    for N in sizes:
        # N > 200k 默认加 128 维随机投影（JL 保距），压低 O(N²·d) 成本；
        # --exact-large 时追加一次全 768 维对照。
        project = None if N <= 200_000 else 128
        print(f"[scaling] N={N:>7}  project_dim={project}  ...", flush=True)
        r = run_one(N, project_dim=project)
        results.append(r)
        print(
            f"           total={r['total_s']:8.2f}s  peak_rss={r['peak_rss_mb']:8.1f}MB  "
            f"groups={r['n_groups']:4d}  edges={r['n_mutual_edges']:>9}  "
            f"ari={r['ari_vs_truth']:.3f}  intra_sim={r['intra_sim']:.3f}",
            flush=True,
        )
        if args.exact_large and N > 200_000:
            print(f"[scaling] N={N:>7}  project_dim=None (exact) ...", flush=True)
            r2 = run_one(N, project_dim=None)
            results.append(r2)
            print(
                f"           total={r2['total_s']:8.2f}s  peak_rss={r2['peak_rss_mb']:8.1f}MB  "
                f"groups={r2['n_groups']:4d}  ari={r2['ari_vs_truth']:.3f}",
                flush=True,
            )

    payload = {
        "meta": {
            "machine": "AMD EPYC 9K65 192-core (32 visible), no GPU",
            "mem_note": "free 显示 123GB（宿主），但 cgroup 上限 8GB——"
                        "500k 全量生成曾在此触发 OOM(kill 137)，已改分块生成",
            "cgroup_limit_mb": round(cgroup_mem_limit_mb(), 1),
            "backend": "torch matmul + topk（8 线程；实测快于 faiss-cpu 5-14 倍）",
            "threads": int(os.environ.get("SEMANTICKV_THREADS", "8")),
            "wall_time_s": round(time.time() - t_start, 1),
        },
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"environment": cpu_synthetic_env(), **payload}, f,
              ensure_ascii=False, indent=2)
    print(f"\n[scaling] 结果写入 {out}（总耗时 {payload['meta']['wall_time_s']}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
