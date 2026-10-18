#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cpu_sim/run_all.py — 一键运行全部 CPU 模拟并汇总。

顺序执行：
  1) test_relation_graph.py   机制微测试（秒级）
  2) run_scaling.py           GSC 规模基准（分钟级）
  3) run_turns.py             10 轮对话重组对比（分钟级）

结果统一落盘到 results/，供 CPU_SIM_REPORT.md 引用。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(script: str, *args: str) -> int:
    print(f"\n{'=' * 70}\n>>> {script} {' '.join(args)}\n{'=' * 70}", flush=True)
    proc = subprocess.run([sys.executable, str(ROOT / "cpu_sim" / script), *args])
    if proc.returncode != 0:
        print(f"[run_all] {script} 失败 (exit={proc.returncode})")
    return proc.returncode


def main() -> int:
    rc = 0
    rc |= run("test_relation_graph.py")
    rc |= run("run_scaling.py")
    rc |= run("run_turns.py")
    print("\n[run_all] 全部完成" + ("" if rc == 0 else f"（存在失败，rc={rc}）"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
