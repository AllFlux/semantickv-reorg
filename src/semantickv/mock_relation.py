# -*- coding: utf-8 -*-
"""mock_relation.py — RelationGraph 的 Mock 实现。

机制：
  - build_full()   全量建 top-p 稀疏边（质心距离 -> 边权），作为新鲜度参照；
  - observe()      EMA 增量更新（w ← α·w_obs + (1-α)·w_old），未观测边衰减，
                   低于 prune_thr 的边删除；产出 Delta（added/removed/updated）；
  - merge()        CRDT 式合并：LWW-Register（turn 为逻辑时钟），
                   幂等、可乱序、可重复合并；
  - freshness_error()  增量状态 vs 全量重算的相对 L2 误差（缺失边按 0 计）。

说明：Mock 用 dict 存边（群数 G 在合成数据里很小，直观优先）。
生产实现应换 CSR/COO 数组；接口不变。
"""

from __future__ import annotations

import time
from typing import Dict, Tuple

import numpy as np

from eval_runner import EdgeSet, Delta


def _edge_arrays(edges: Dict[Tuple[int, int], Tuple[float, int]]):
    if not edges:
        z_i = np.empty(0, dtype=np.int32)
        return EdgeSet(z_i, z_i.copy(), np.empty(0, dtype=np.float32), np.empty(0, dtype=np.int32))
    keys = np.array(list(edges.keys()), dtype=np.int64)
    vals = np.array(list(edges.values()), dtype=np.float64)
    return EdgeSet(
        src=keys[:, 0].astype(np.int32),
        dst=keys[:, 1].astype(np.int32),
        weight=vals[:, 0].astype(np.float32),
        turn=vals[:, 1].astype(np.int32),
    )


class MockRelationGraph:
    """满足 eval_runner.RelationGraph 接口的 Mock 实现。"""

    def __init__(self, top_p: int = 10, ema_alpha: float = 0.3,
                 prune_thr: float = 0.05, delta_eps: float = 1e-3,
                 weight_fn: str = "inv_dist"):
        """
        top_p      : 每群保留的最强关系边数
        ema_alpha  : EMA 系数（新观测权重占比）
        prune_thr  : 边权衰减低于该值 -> 删除
        delta_eps  : |Δw| 大于该值才记入 updated
        weight_fn  : "inv_dist" -> w = 1/(1+d)；"exp" -> w = exp(-d)
        """
        self.top_p = top_p
        self.ema_alpha = ema_alpha
        self.prune_thr = prune_thr
        self.delta_eps = delta_eps
        self.weight_fn = weight_fn
        # 状态: (i,j) i<j -> [weight, turn]
        self._edges: Dict[Tuple[int, int], Tuple[float, int]] = {}
        # 本轮活跃群（observe 前由调用方设置）：仅活跃群关联的边参与衰减/剪枝；
        # None 表示全部边参与衰减（默认行为，见 test_relation_graph.py 的对照实验）
        self.active_groups = None
        # 计时（供报告）
        self.timings = {"build_full_s": 0.0, "observe_s": 0.0, "merge_s": 0.0}

    # ------------------------------------------------------------ 内部

    def _pairwise_dist(self, centroids: np.ndarray) -> np.ndarray:
        c = np.asarray(centroids, dtype=np.float64)
        sq = np.sum(c * c, axis=1)
        d2 = sq[:, None] + sq[None, :] - 2.0 * (c @ c.T)
        return np.sqrt(np.maximum(d2, 0.0))

    def _w(self, dist: np.ndarray) -> np.ndarray:
        if self.weight_fn == "exp":
            return np.exp(-dist)
        return 1.0 / (1.0 + dist)

    # ------------------------------------------------------------ 接口

    def install_full(self, centroids: np.ndarray, turn: int) -> EdgeSet:
        """全量重建并**替换**图状态（full reorg 语义）。

        与 build_full 的区别：build_full 是纯计算（返回边集、不动状态），
        用于生成「全量重算参照」；install_full 把结果灌入状态，
            对应「这一轮做了全量重组」。
        """
        es = self.build_full(centroids, turn)
        self._edges = {(int(s), int(d)): (float(w), int(turn))
                       for s, d, w in zip(es.src, es.dst, es.weight)}
        return es

    def build_full(self, centroids: np.ndarray, turn: int) -> EdgeSet:
        """全量建 top-p 边（无向，取两方向边权的较大者）。"""
        t0 = time.perf_counter()
        D = self._pairwise_dist(centroids)
        G = D.shape[0]
        W = self._w(D)
        p = min(self.top_p, max(G - 1, 0))
        edges: Dict[Tuple[int, int], Tuple[float, int]] = {}
        if p > 0:
            # 每行取 top-p 近邻（排除自身：把对角设为 +inf）
            Dm = D.copy()
            np.fill_diagonal(Dm, np.inf)
            nbr = np.argpartition(Dm, p - 1, axis=1)[:, :p]      # (G, p)
            for i in range(G):
                for j in nbr[i]:
                    if j == i:
                        continue
                    a, b = (i, int(j)) if i < j else (int(j), i)
                    w = max(W[i, j], W[j, i])
                    edges[(a, b)] = (float(w), int(turn))
        self.timings["build_full_s"] += time.perf_counter() - t0
        return _edge_arrays(edges)

    def observe(self, observed: EdgeSet, turn: int) -> Delta:
        """EMA 增量更新并产出 Delta。"""
        t0 = time.perf_counter()
        alpha = self.ema_alpha
        added: Dict[Tuple[int, int], Tuple[float, int]] = {}
        updated: Dict[Tuple[int, int], Tuple[float, int]] = {}
        removed: Dict[Tuple[int, int], Tuple[float, int]] = {}
        seen = set()

        for s, d, w in zip(observed.src.tolist(), observed.dst.tolist(), observed.weight.tolist()):
            key = (s, d) if s < d else (d, s)
            seen.add(key)
            if key in self._edges:
                w_old, t_old = self._edges[key]
                w_new = alpha * w + (1.0 - alpha) * w_old
                if abs(w_new - w_old) > self.delta_eps:
                    updated[key] = (w_new, int(turn))
                self._edges[key] = (w_new, int(turn))
            else:
                added[key] = (float(w), int(turn))
                self._edges[key] = (float(w), int(turn))

        # 未观测边衰减；低于阈值 -> 删除。
        # 仅当边的端点属于 active_groups（或 active_groups is None）时才衰减：
        # 语义上「关系状态只在该群活跃时才可能变陈」。
        active = self.active_groups
        for key in list(self._edges.keys()):
            if key in seen:
                continue
            if active is not None and key[0] not in active and key[1] not in active:
                continue
            w_old, t_old = self._edges[key]
            w_new = (1.0 - alpha) * w_old
            if w_new < self.prune_thr:
                removed[key] = (w_new, int(turn))
                del self._edges[key]
            else:
                self._edges[key] = (w_new, t_old)

        self.timings["observe_s"] += time.perf_counter() - t0
        return Delta(added=_edge_arrays(added),
                     removed=_edge_arrays(removed),
                     updated=_edge_arrays(updated))

    def merge(self, delta: Delta) -> None:
        """CRDT 式合并（LWW-Register，turn 为逻辑时钟；幂等、可乱序）。"""
        t0 = time.perf_counter()
        for s, d, w, t in zip(delta.added.src.tolist(), delta.added.dst.tolist(),
                              delta.added.weight.tolist(), delta.added.turn.tolist()):
            key = (s, d) if s < d else (d, s)
            cur = self._edges.get(key)
            if cur is None or t > cur[1]:
                self._edges[key] = (float(w), int(t))
        for s, d, w, t in zip(delta.updated.src.tolist(), delta.updated.dst.tolist(),
                              delta.updated.weight.tolist(), delta.updated.turn.tolist()):
            key = (s, d) if s < d else (d, s)
            cur = self._edges.get(key)
            if cur is None or t > cur[1]:
                self._edges[key] = (float(w), int(t))
        for s, d, w, t in zip(delta.removed.src.tolist(), delta.removed.dst.tolist(),
                              delta.removed.weight.tolist(), delta.removed.turn.tolist()):
            key = (s, d) if s < d else (d, s)
            cur = self._edges.get(key)
            if cur is not None and t >= cur[1]:
                del self._edges[key]
        self.timings["merge_s"] += time.perf_counter() - t0

    def freshness_error(self, reference: EdgeSet) -> float:
        """增量状态 vs 全量重算的相对 L2 误差（缺失边按 0 计）。"""
        keys = set(self._edges.keys())
        for s, d in zip(reference.src.tolist(), reference.dst.tolist()):
            keys.add((s, d) if s < d else (d, s))
        if not keys:
            return 0.0
        ref: Dict[Tuple[int, int], float] = {}
        for s, d, w in zip(reference.src.tolist(), reference.dst.tolist(), reference.weight.tolist()):
            ref[(s, d) if s < d else (d, s)] = float(w)
        a = np.array([self._edges.get(k, (0.0, 0))[0] for k in keys], dtype=np.float64)
        b = np.array([ref.get(k, 0.0) for k in keys], dtype=np.float64)
        denom = float(np.linalg.norm(b))
        return float(np.linalg.norm(a - b) / denom) if denom > 0 else 0.0

    def structure_error(self, reference: EdgeSet) -> float:
        """结构误差：1 - Jaccard(当前边集, 参照边集)。

        区分两类陈旧：
          - freshness_error 高但 structure_error 低 -> 纯权重滞后（EMA 收敛慢），
            提高 alpha 或缩短重组间隔即可；
          - 两者都高 -> 结构陈旧（边集本身该删未删/该建未建），
            只能靠 full 重组修复。
        """
        ref_keys = set()
        for s, d in zip(reference.src.tolist(), reference.dst.tolist()):
            ref_keys.add((s, d) if s < d else (d, s))
        if not ref_keys and not self._edges:
            return 0.0
        inter = len(set(self._edges.keys()) & ref_keys)
        union = len(set(self._edges.keys()) | ref_keys)
        return 1.0 - (inter / union) if union else 0.0

    # ------------------------------------------------------------ 辅助

    @property
    def n_edges(self) -> int:
        return len(self._edges)

    def snapshot(self) -> EdgeSet:
        return _edge_arrays(self._edges)
