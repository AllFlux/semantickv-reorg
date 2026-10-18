# -*- coding: utf-8 -*-
"""metrics.py — 统计工具：95% CI 与序贯决策规则（对应方案 §4.2）。"""

from __future__ import annotations

from typing import Sequence, Tuple

import numpy as np
from scipy import stats


def mean_ci(samples: Sequence[float], ci: float = 0.95) -> Tuple[float, float, float]:
    """样本均值与置信区间。

    逻辑：mean ± t_{1-(1-ci)/2, n-1} * s / sqrt(n)
      - s 为样本标准差（ddof=1）；
      - t 分位数由 scipy.stats.t.ppf 给出；
      - n=1 时无区间概念，返回 (mean, mean, mean)。
    """
    x = np.asarray(samples, dtype=np.float64)
    n = x.size
    if n == 0:
        raise ValueError("samples 为空")
    mean = float(x.mean())
    if n == 1:
        return mean, mean, mean
    s = float(x.std(ddof=1))
    t = float(stats.t.ppf(1 - (1 - ci) / 2, df=n - 1))
    half = t * s / np.sqrt(n)
    return mean, mean - half, mean + half


def sequential_decision(
    samples: Sequence[float],
    pass_line: float,
    falsify_line: float,
    ci: float = 0.95,
) -> str:
    """序贯决策规则（方案 §4.2）。

    规则：
      - 95% CI 完全高于 pass_line      -> "pass"（通过）
      - 95% CI 完全低于 falsify_line   -> "fail"（证伪）
      - 否则                           -> "continue"（继续采样）
    调用方负责在达到最大样本量后把 "continue" 改写为 "inconclusive"。
    """
    _, lo, hi = mean_ci(samples, ci=ci)
    if lo > pass_line:
        return "pass"
    if hi < falsify_line:
        return "fail"
    return "continue"


def relative_err(x: np.ndarray, ref: np.ndarray) -> float:
    """相对 L2 误差：||x - ref|| / ||ref||（用于边新鲜度等指标）。"""
    x = np.asarray(x, dtype=np.float64).ravel()
    ref = np.asarray(ref, dtype=np.float64).ravel()
    n = min(x.size, ref.size)
    x, ref = x[:n], ref[:n]
    denom = float(np.linalg.norm(ref))
    if denom == 0.0:
        return 0.0
    return float(np.linalg.norm(x - ref) / denom)
