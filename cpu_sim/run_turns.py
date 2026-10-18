#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cpu_sim/run_turns.py — 10 轮对话模拟：全量重组 vs 阈值触发 vs 周期重组的开销对比。

对应预注册卡 E4 的三个重组臂。每轮追加 2k 新 token（含可控比例的「新话题」
漂移源），测量：
  - 每轮重组耗时 / 峰值 RSS；
  - 调度器决策序列（full / partial / none 及原因）；
  - 关系图增量更新（observe -> Delta）vs 全量重建的新鲜度误差；
  - 10 轮总计：总耗时、峰值内存、全量重组次数、最终新鲜度误差。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

from eval_runner import TurnStats
from env_tag import cpu_synthetic_env
from mock_grouper import MemTracker, MockSemanticGrouper, rss_mb
from mock_relation import MockRelationGraph
from mock_scheduler import MockReorgScheduler

TOPIC_STD = None  # 由 d 决定：base/sqrt(d)，见 synth.make_semantic_batch 的说明
BASE_TOPIC_STD = 0.65


def _norm(X: np.ndarray) -> np.ndarray:
    return X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-12)


class TopicSpace:
    """可控话题空间：固定基础话题质心，可随时追加新话题（漂移源）。"""

    def __init__(self, d: int, n_base: int, seed: int):
        rng = np.random.default_rng(seed)
        self.d = d
        self.centers = _norm(rng.standard_normal((n_base, d)).astype(np.float32))

    def sample(self, rng: np.random.Generator, n: int, topic_ids) -> np.ndarray:
        ids = np.asarray(topic_ids, dtype=np.int64)
        labels = rng.choice(ids, size=n)
        std = BASE_TOPIC_STD / np.sqrt(self.d)   # 与 synth 一致的 1/sqrt(d) 缩放
        X = self.centers[labels] + std * rng.standard_normal((n, self.d), dtype=np.float32)
        return _norm(X.astype(np.float32))

    def new_topics(self, rng: np.random.Generator, m: int):
        C = _norm(rng.standard_normal((m, self.d)).astype(np.float32))
        self.centers = np.vstack([self.centers, C])
        return list(range(len(self.centers) - m, len(self.centers)))


def run_mode(mode: str, turns: int = 10, N0: int = 20_000, per_turn: int = 2_000,
             d: int = 768, base_topics: int = 32, k: int = 16,
             drift_threshold: float = 0.15, periodic_k: int = 4, seed: int = 7,
             max_stale_turns: int = None, ema_alpha: float = 0.3) -> dict:
    rng = np.random.default_rng(seed)
    space = TopicSpace(d, base_topics, seed)

    grouper = MockSemanticGrouper(k=k)
    graph = MockRelationGraph(top_p=10, ema_alpha=ema_alpha)
    scheduler = MockReorgScheduler(
        mode=mode, k_turns=periodic_k, drift_threshold=drift_threshold,
        partial_ratio=0.5, max_stale_turns=max_stale_turns
    )
    label = mode if max_stale_turns is None else f"{mode}:K={max_stale_turns}"
    if ema_alpha != 0.3:
        label += f":a={ema_alpha}"

    # ---- 第 0 轮：初始 fit + 建图 ----
    X_all = space.sample(rng, N0, range(base_topics))
    with MemTracker() as mt:
        ga = grouper.fit(X_all)
    graph.install_full(ga.centroids, turn=0)   # 初始状态灌入（不是纯计算）
    peak = mt.peak_mb
    rows = [{"turn": 0, "action": "init_fit", "time_s": 0.0,
             "n_groups": ga.n_groups, "n_edges": graph.n_edges, "freshness": None,
             "peak_rss_mb": round(peak, 1)}]

    prev_cents = ga.centroids
    # 漂移计划：多数轮 5% 新话题，第 4/8 轮 40% 新话题（话题切换爆发）
    drift_plan = {4: 0.40, 8: 0.40}

    for t in range(1, turns + 1):
        new_frac = drift_plan.get(t, 0.05)
        n_drift = int(per_turn * new_frac)
        new_ids = space.new_topics(rng, 3) if n_drift > 0 else []
        X_new = space.sample(rng, per_turn, range(base_topics))
        if n_drift > 0:
            X_new[:n_drift] = space.sample(rng, n_drift, new_ids)
        X_all = np.vstack([X_all, X_new])

        # 漂移信号（便宜且诚实）：新 token 对旧质心的未匹配比例
        sims = X_new @ prev_cents.T
        unmatched = float(np.mean(sims.max(axis=1) < grouper.tau))
        drift = max(unmatched, per_turn / X_all.shape[0])

        stats = TurnStats(turn=t, n_tokens_total=X_all.shape[0],
                          n_new_tokens=per_turn, drift=drift)
        decision = scheduler.should_reorg(t, drift, stats)

        t0 = time.perf_counter()
        freshness = None
        with MemTracker() as mt:
            if decision.action == "full":
                ga = grouper.fit(X_all)
                graph.install_full(ga.centroids, turn=t)   # 全量重组：替换图状态
                scheduler.note_full_cost(time.perf_counter() - t0, t)
            elif decision.action == "partial":
                ga = grouper.partial_fit(X_new, ga)
                # 变化群 = 质心移动 > 1e-3 的旧群 ∪ 本轮新增的群
                G_old = prev_cents.shape[0]
                moved = np.flatnonzero(
                    np.linalg.norm(ga.centroids[:G_old] - prev_cents, axis=1) > 1e-3)
                if ga.n_groups > G_old:
                    moved = np.concatenate([moved, np.arange(G_old, ga.n_groups)])
                touched = np.unique(np.concatenate([ga.labels[-per_turn:], moved]))
                # 全量建图在此很便宜（G 很小），但只把「变化群」的边交给 observe
                full = graph.build_full(ga.centroids, turn=t)
                mask = np.isin(full.src, touched) | np.isin(full.dst, touched)
                from eval_runner import EdgeSet
                observed = EdgeSet(full.src[mask], full.dst[mask],
                                   full.weight[mask], full.turn[mask])
                graph.active_groups = set(touched.tolist())
                graph.observe(observed, turn=t)
                graph.active_groups = None
            # none：什么都不做
        peak = max(peak, mt.peak_mb)
        elapsed = time.perf_counter() - t0

        # 每轮校准新鲜度：增量状态 vs 全量重算（G 很小，成本 <1ms）
        reference = graph.build_full(ga.centroids, turn=t)
        freshness = graph.freshness_error(reference)
        structure = graph.structure_error(reference)

        rows.append({
            "turn": t, "drift": round(drift, 4), "decision": decision.action,
            "reason": decision.reason, "time_s": round(elapsed, 3),
            "n_groups": ga.n_groups, "n_edges": graph.n_edges,
            "freshness": round(freshness, 5),
            "structure": round(structure, 5),
            "peak_rss_mb": round(peak, 1),
        })
        prev_cents = ga.centroids
        scheduler.on_turn_boundary(t)
        print(f"  [{label:>12}] turn={t:2d} drift={drift:.3f} -> {decision.action:>7} "
              f"{elapsed:6.2f}s groups={ga.n_groups:3d} edges={graph.n_edges:3d} "
              f"fresh={freshness:.4f} struct={structure:.4f}", flush=True)

    # 终局：全量重建一次作为新鲜度参照
    reference = graph.build_full(ga.centroids, turn=turns)
    final_fresh = graph.freshness_error(reference)
    final_struct = graph.structure_error(reference)
    # 注意：rows[0] 是 init 行，没有 "decision" 键，必须跳过
    n_full = sum(1 for r in rows[1:] if r["decision"] == "full")
    n_partial = sum(1 for r in rows[1:] if r["decision"] == "partial")
    n_none = sum(1 for r in rows[1:] if r["decision"] == "none")
    total_time = sum(r.get("time_s", 0.0) for r in rows[1:])
    fresh_series = [r["freshness"] for r in rows[1:] if r["freshness"] is not None]
    struct_series = [r["structure"] for r in rows[1:] if r.get("structure") is not None]

    label = mode if max_stale_turns is None else f"{mode}:K={max_stale_turns}"
    return {
        "mode": label,
        "params": {"turns": turns, "N0": N0, "per_turn": per_turn, "d": d,
                   "base_topics": base_topics, "k": k,
                   "drift_threshold": drift_threshold, "periodic_k": periodic_k,
                   "max_stale_turns": max_stale_turns, "ema_alpha": ema_alpha},
        "rows": rows,
        "summary": {
            "total_time_s": round(total_time, 2),
            "peak_rss_mb": round(peak, 1),
            "n_full": n_full, "n_partial": n_partial, "n_none": n_none,
            "final_freshness_err": round(final_fresh, 5),
            "max_freshness_err": round(max(fresh_series), 5) if fresh_series else None,
            "final_structure_err": round(final_struct, 5),
            "max_structure_err": round(max(struct_series), 5) if struct_series else None,
            "final_n_groups": ga.n_groups,
            "final_n_edges": graph.n_edges,
            "graph_timings_s": {k2: round(v, 4) for k2, v in graph.timings.items()},
        },
    }


def parse_mode_spec(spec: str):
    """解析 'threshold:max_stale=2:alpha=0.8' / 'periodic:k=4' / 'per_turn' 规格。

    键名映射：max_stale -> max_stale_turns，alpha -> ema_alpha。
    """
    parts = spec.split(":")
    mode = parts[0].strip()
    key_map = {"max_stale": "max_stale_turns", "alpha": "ema_alpha"}
    params = {}
    for p in parts[1:]:
        key, _, val = p.partition("=")
        key = key_map.get(key.strip(), key.strip())
        try:
            params[key] = int(val)
        except ValueError:
            params[key] = float(val)
    return mode, params


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--modes", type=str,
                    default="per_turn,threshold,periodic,threshold:max_stale=2,threshold:max_stale=3")
    ap.add_argument("--out", type=str, default=str(ROOT / "results" / "turns_results.json"))
    args = ap.parse_args()

    out = {"modes": []}
    t_start = time.time()
    for spec in [m.strip() for m in args.modes.split(",") if m.strip()]:
        mode, params = parse_mode_spec(spec)
        print(f"[turns] mode={spec}", flush=True)
        res = run_mode(mode, turns=args.turns, **params)
        out["modes"].append(res)
        print(f"[turns]   summary: {res['summary']}", flush=True)
    out["wall_time_s"] = round(time.time() - t_start, 1)

    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"environment": cpu_synthetic_env(), **out}, f,
              ensure_ascii=False, indent=2)
    print(f"\n[turns] 结果写入 {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
