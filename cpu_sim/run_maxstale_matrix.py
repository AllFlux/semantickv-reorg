#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_maxstale_matrix.py — max_stale × EMA α 参数矩阵（CPU 合成，带环境标签）。

背景（v0.2.1 纪律机制化）：原 results/turns_maxstale_matrix.json 由临时命令
生成，缺 environment 环境标签（纪律 D3 要求）且缺 K=3/α=0.9 一格。本脚本将其
机制化：全网格 K∈{2,3} × α∈{0.3,0.8,0.9} 一次性生成，seed=7 与原矩阵一致，
前 5 行应与原文件数值逐位一致（复现性校验）。

用法：
    python3 cpu_sim/run_maxstale_matrix.py            # 生成并写 results/
    python3 cpu_sim/run_maxstale_matrix.py --check    # 只校验复现性不覆盖
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

from env_tag import cpu_synthetic_env  # noqa: E402
from run_turns import run_mode  # noqa: E402

GRID = [(k, a) for k in (2, 3) for a in (0.3, 0.8, 0.9)]
OUT = ROOT / "results" / "turns_maxstale_matrix.json"

# 复现性校验只比对实质指标：计时（total_time_s/graph_timings_s）与内存
# （peak_rss_mb）随机器负载自然波动，不作为复现性判据（纪律 2：科学内容
# 必须逐位一致，环境噪声合理容差）。
NOISY_FIELDS = {"total_time_s", "graph_timings_s", "peak_rss_mb"}


def _strip_noise(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k in NOISY_FIELDS:
            continue  # 顶层噪声字段（含嵌套计时 dict）整体剔除
        if isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if kk not in NOISY_FIELDS}
        else:
            out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只校验与原矩阵的复现性，不覆盖文件")
    args = ap.parse_args()

    old = {}
    if OUT.exists():
        for m in json.loads(OUT.read_text(encoding="utf-8")).get("modes", []):
            key = (m["params"]["max_stale_turns"], m["params"]["ema_alpha"])
            old[key] = m

    t0 = time.time()
    modes = []
    mismatches = []
    for k, a in GRID:
        res = run_mode("threshold", max_stale_turns=k, ema_alpha=a)
        modes.append(res)
        if (k, a) in old:
            o = old[(k, a)]
            for field in ("summary", "per_turn"):
                if json.dumps(_strip_noise(res.get(field) or {}), sort_keys=True) != \
                        json.dumps(_strip_noise(o.get(field) or {}), sort_keys=True):
                    mismatches.append((k, a, field))
        print(f"[matrix] K={k} alpha={a}: "
              f"fresh_err={res['summary']['max_freshness_err']:.4f} "
              f"struct_err={res['summary']['max_structure_err']:.4f} "
              f"{'复现一致' if (k, a) in old and (k, a, 'summary') not in mismatches else ''}",
              flush=True)

    if mismatches:
        print(f"[matrix] ⚠ 复现性校验失败: {mismatches}（seed/参数可能被改过，"
              f"按纪律 2 需人工确认后再覆盖）")
        return 1

    payload = {"environment": cpu_synthetic_env(
        note="合成数据 CPU 模拟；max_stale × α 参数矩阵（seed=7 与原矩阵一致）"),
        "grid": {"max_stale_turns": [2, 3], "ema_alpha": [0.3, 0.8, 0.9]},
        "modes": modes, "wall_time_s": round(time.time() - t0, 1)}
    if args.check:
        print(f"[matrix] --check 模式：复现性通过（{len(old)} 旧行一致），不覆盖")
        return 0
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"[matrix] 结果写入 {OUT}（{len(modes)} 行，总耗时 {payload['wall_time_s']}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
