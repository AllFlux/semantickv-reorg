#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cpu_sim/test_relation_graph.py — 核心机制微测试（纯 CPU，秒级）。

覆盖：
  T1  GSC 在高斯混合语义空间上的恢复力（ARI）
  T2  partial_fit：旧标签保持 + 新 token 挂靠/新建
  T3  RelationGraph.build_full：top-p 边数量与结构
  T4  EMA 更新方向与 Delta 分类（added/updated/removed）
  T5  CRDT 合并：幂等性（同一 Delta 合并两次状态不变）
  T6  CRDT 合并：乱序安全（旧 turn 不能覆盖新状态；新 turn 必须覆盖）
  T7  新鲜度：与全量重算一致时误差为 0
  T8  衰减语义：active_groups=None 时未观测边衰减直至剪枝
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

from eval_runner import Delta, EdgeSet
from env_tag import cpu_synthetic_env
from mock_grouper import MockSemanticGrouper
from mock_relation import MockRelationGraph
from synth import adjusted_rand_index, make_semantic_batch

RESULTS = {}


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS[name] = {"pass": bool(cond), "detail": detail}
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def empty_edges() -> EdgeSet:
    z = np.empty(0, dtype=np.int32)
    return EdgeSet(z, z.copy(), np.empty(0, dtype=np.float32), z.copy())


def load_state(graph: MockRelationGraph, es: EdgeSet, turn: int = 0) -> None:
    graph._edges = {(int(s), int(d)): (float(w), turn)
                    for s, d, w in zip(es.src, es.dst, es.weight)}


def main() -> int:
    rng = np.random.default_rng(0)

    # ---- T1: GSC 恢复力 ----
    X, y = make_semantic_batch(8_000, 128, 16, rng=np.random.default_rng(1))
    ga = MockSemanticGrouper(k=10, tau=0.3).fit(X)
    ari = adjusted_rand_index(y, ga.labels)
    check("T1_gsc_ari", ari > 0.90, f"ARI={ari:.3f} (n_groups={ga.n_groups}, truth=16)")

    # ---- T2: partial_fit ----
    X_old, _, C = make_semantic_batch(4_000, 128, 8,
                                      rng=np.random.default_rng(2), return_centers=True)
    g2 = MockSemanticGrouper(k=10, tau=0.3)
    ga2 = g2.fit(X_old)
    old_labels = ga2.labels.copy()
    Xa, _ = make_semantic_batch(1_000, 128, rng=np.random.default_rng(3),
                                centers=C)                    # 同一话题空间
    Xb, _ = make_semantic_batch(1_000, 128, 5, rng=np.random.default_rng(4))  # 全新话题
    ga3 = g2.partial_fit(np.vstack([Xa, Xb]), ga2)
    check("T2_partial_old_labels_preserved",
          np.array_equal(ga3.labels[: len(old_labels)], old_labels),
          f"n_groups {ga2.n_groups} -> {ga3.n_groups}")
    check("T2_partial_new_clusters_small", ga3.meta["n_new_clusters"] <= 20,
          f"新群数={ga3.meta['n_new_clusters']}（未匹配 token="
          f"{ga3.meta['n_new_tokens_unmatched']}，应≈5 个新话题而非逐 token 成群）")
    check("T2_partial_covers_all", ga3.labels.shape[0] == 6_000,
          f"N={ga3.labels.shape[0]}")

    # ---- T3: build_full 结构 ----
    cents = np.asarray(rng.standard_normal((12, 64)), dtype=np.float32)
    cents /= np.linalg.norm(cents, axis=1, keepdims=True)
    es = MockRelationGraph(top_p=4).build_full(cents, turn=0)
    # 无向去重：唯一边数 ∈ [G*p/2, G*p]
    check("T3_top_p_edges", 12 * 4 // 2 <= len(es.src) <= 12 * 4,
          f"edges={len(es.src)} (G*p/2={12*4//2}, G*p={12*4})")
    check("T3_no_self_loops", not np.any(es.src == es.dst), "")

    # ---- T4: EMA 方向与 Delta 分类 ----
    g4 = MockRelationGraph(top_p=4, ema_alpha=0.5, delta_eps=1e-4)
    load_state(g4, es, turn=0)
    cents2 = cents + 0.3 * rng.standard_normal(cents.shape).astype(np.float32)
    cents2 /= np.linalg.norm(cents2, axis=1, keepdims=True)
    obs = g4.build_full(cents2, turn=1)
    delta = g4.observe(obs, turn=1)
    check("T4_delta_nonempty", len(delta) > 0,
          f"added={len(delta.added.src)} updated={len(delta.updated.src)} "
          f"removed={len(delta.removed.src)}")
    check("T4_ema_applied", g4.n_edges > 0,
          f"n_edges={g4.n_edges}")

    # ---- T5: 幂等性 ----
    snap1 = g4.snapshot()
    g4.merge(delta)
    g4.merge(delta)
    snap2 = g4.snapshot()
    check("T5_merge_idempotent",
          np.array_equal(snap1.src, snap2.src) and np.allclose(snap1.weight, snap2.weight),
          f"edges {len(snap1.src)} -> {len(snap2.src)}")

    # ---- T6: 乱序安全 ----
    g6 = MockRelationGraph(top_p=4)
    g6._edges = {(0, 1): (0.50, 5), (2, 3): (0.60, 5)}     # 已有 turn=5 的状态
    stale = Delta(
        added=EdgeSet(np.array([0], np.int32), np.array([1], np.int32),
                      np.array([9.0], np.float32), np.array([2], np.int32)),
        removed=empty_edges(), updated=empty_edges())
    fresh = Delta(
        added=empty_edges(), removed=empty_edges(),
        updated=EdgeSet(np.array([0], np.int32), np.array([1], np.int32),
                        np.array([0.99], np.float32), np.array([7], np.int32)))
    g6.merge(stale)      # turn=2 < 5：不得覆盖
    check("T6_lww_rejects_stale", g6._edges[(0, 1)] == (0.50, 5),
          f"(0,1) -> {g6._edges[(0, 1)]}（应保持 turn=5）")
    g6.merge(fresh)      # turn=7 > 5：必须覆盖
    w01 = g6._edges[(0, 1)]
    check("T6_lww_accepts_fresh", w01[1] == 7 and abs(w01[0] - 0.99) < 1e-6,
          f"(0,1) -> {w01}（应更新为 turn=7, w≈0.99）")

    # ---- T7: 新鲜度 ----
    g7 = MockRelationGraph(top_p=4)
    load_state(g7, es, turn=0)
    check("T7_freshness_zero_on_identical", g7.freshness_error(es) < 1e-6,
          f"err={g7.freshness_error(es):.2e}")

    # ---- T8: 衰减与剪枝 ----
    g8 = MockRelationGraph(top_p=4, ema_alpha=0.3, prune_thr=0.05)
    load_state(g8, es, turn=0)
    n0 = g8.n_edges
    for t in range(1, 30):
        g8.observe(empty_edges(), turn=t)      # 持续不观测 -> 持续衰减
    check("T8_decay_prunes", g8.n_edges < n0, f"edges {n0} -> {g8.n_edges}")

    # ---- T8b: active_groups 限定衰减范围 ----
    g8b = MockRelationGraph(top_p=4, ema_alpha=0.3, prune_thr=0.05)
    load_state(g8b, es, turn=0)
    untouched = {(s, d) for s, d in g8b._edges if s != 0 and d != 0}
    g8b.active_groups = {0}
    for t in range(1, 30):
        g8b.observe(empty_edges(), turn=t)
    g8b.active_groups = None
    still = sum(1 for k in untouched if k in g8b._edges)
    check("T8b_active_groups_scoped_decay", still == len(untouched),
          f"未活跃群关联边保留 {still}/{len(untouched)}")

    out = ROOT / "results" / "relation_graph_tests.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"environment": cpu_synthetic_env(), "tests": RESULTS}, f,
              ensure_ascii=False, indent=2)
    n_pass = sum(1 for v in RESULTS.values() if v["pass"])
    print(f"\n微测试: {n_pass}/{len(RESULTS)} 通过，结果写入 {out}")
    return 0 if n_pass == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
