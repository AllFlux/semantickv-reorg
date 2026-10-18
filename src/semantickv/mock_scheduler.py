# -*- coding: utf-8 -*-
"""mock_scheduler.py — ReorgScheduler 的 Mock 实现。

三种模式（对应方案 E4 的对比臂）：
  - per_turn   : 每轮对话边界全量重组（语义最新，成本最高）
  - periodic   : 每 k 轮全量重组一次
  - threshold  : 漂移超过阈值才全量；超过半个阈值做局部增量；否则不动

drift 度量（模块级函数，供实验脚本调用）：
  drift = max(新 token 占比, 质心漂移)
  质心漂移 = 旧质心到最近新质心的平均余弦距离
"""

from __future__ import annotations

import numpy as np

from eval_runner import ReorgDecision, TurnStats


def drift_measure(old_centroids: np.ndarray, new_centroids: np.ndarray,
                  n_new: int, n_total: int) -> float:
    """语义漂移度量 ∈ [0,1]：max(新 token 占比, 平均质心余弦距离)。"""
    frac_new = n_new / max(n_total, 1)
    if old_centroids is None or len(old_centroids) == 0:
        return frac_new
    sims = old_centroids @ new_centroids.T          # 余弦（均已归一化）
    nearest = sims.max(axis=1)
    shift = float(np.mean(1.0 - nearest))
    return float(max(frac_new, shift))


class MockReorgScheduler:
    """满足 eval_runner.ReorgScheduler 接口的 Mock 实现。"""

    def __init__(self, mode: str = "threshold", k_turns: int = 4,
                 drift_threshold: float = 0.15, partial_ratio: float = 0.5,
                 min_interval: int = 1, max_stale_turns: int = None):
        """
        mode           : "per_turn" | "periodic" | "threshold"
        k_turns        : periodic 模式的全量重组周期
        drift_threshold: threshold 模式的全量重组触发线
        partial_ratio  : 局部增量的触发线（相对 drift_threshold 的比例）
        min_interval   : 两次全量重组之间的最小轮数
        max_stale_turns: 最大陈旧轮次硬约束（threshold 模式）。
                        距上次 full 超过 K 轮即强制 full，无论 drift 多小。
                        动机：CPU 模拟发现纯增量 stretch 的边新鲜度误差
                        （0.226）超出 E4 通过线（0.05），必须周期性兜底。
        """
        assert mode in ("per_turn", "periodic", "threshold")
        self.mode = mode
        self.k_turns = k_turns
        self.drift_threshold = drift_threshold
        self.partial_ratio = partial_ratio
        self.min_interval = min_interval
        self.max_stale_turns = max_stale_turns

        self._last_full_turn = 0        # 第 0 轮 init fit 视为一次 full
        self._last_full_cost_s: float = 0.0
        self._turns_seen = 0

    # ------------------------------------------------------------ 接口

    def should_reorg(self, turn: int, drift: float, stats: TurnStats) -> ReorgDecision:
        est = self._estimate_cost(stats)
        if self.mode == "per_turn":
            return ReorgDecision("full", "per_turn 模式：每轮边界全量重组", est)

        if self.mode == "periodic":
            if turn - self._last_full_turn >= self.k_turns:
                return ReorgDecision("full", f"periodic 模式：间隔 {self.k_turns} 轮到期", est)
            if drift >= self.partial_ratio * self.drift_threshold:
                return ReorgDecision("partial", "periodic 模式：漂移超半阈值，局部增量", est * 0.2)
            return ReorgDecision("none", "periodic 模式：未到期且漂移低", 0.0)

        # threshold 模式
        stale = turn - self._last_full_turn
        forced = (self.max_stale_turns is not None
                  and stale >= self.max_stale_turns)
        if (drift >= self.drift_threshold or forced) and stale >= self.min_interval:
            if forced and drift < self.drift_threshold:
                reason = (f"max_stale 兜底：距上次 full 已 {stale} 轮 "
                          f"≥ K={self.max_stale_turns}（drift={drift:.3f} 未触线）")
            else:
                reason = f"threshold 模式：漂移 {drift:.3f} ≥ 阈值 {self.drift_threshold}"
            return ReorgDecision("full", reason, est)
        if drift >= self.partial_ratio * self.drift_threshold:
            return ReorgDecision("partial", f"threshold 模式：漂移 {drift:.3f} 超半阈值", est * 0.2)
        return ReorgDecision("none", f"threshold 模式：漂移 {drift:.3f} 低于触发线", 0.0)

    def on_turn_boundary(self, turn: int) -> None:
        self._turns_seen += 1

    # ------------------------------------------------------------ 辅助

    def note_full_cost(self, cost_s: float, turn: int) -> None:
        """记录一次全量重组实测耗时与发生轮次，用于后续预估校准。"""
        self._last_full_cost_s = cost_s
        self._last_full_turn = turn

    def _estimate_cost(self, stats: TurnStats) -> float:
        """按 token 数线性外估（全量重组成本 ≈ O(N·log N)~O(N²) 的粗估）。"""
        if self._last_full_cost_s <= 0:
            return float("nan")
        return self._last_full_cost_s * stats.n_tokens_total / max(stats.n_tokens_total - stats.n_new_tokens, 1)
