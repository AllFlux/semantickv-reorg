#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_runner.py — SemanticKV-Reorg 实验运行器：接口骨架 + CLI

=====================================================================
A. 接口骨架（§1）—— 换到有卡环境时唯一需要遵守的契约
=====================================================================
三个核心组件的输入/输出签名在下方定义。有卡环境落地时：
  1. 保持这些签名不变；
  2. 把 Mock 实现（src/semantickv/mock_*.py）替换为真实实现；
  3. 用 --check-interfaces 校验签名一致后再跑真实实验。

=====================================================================
B. CLI（§2）
=====================================================================
    # 校验 Mock 实现是否满足接口签名
    python3 eval_runner.py --check-interfaces

    # CPU 冒烟（合成数据；只验证流水线机械正确性，不作为假设证据）
    python3 eval_runner.py --card experiments/E1_semantic_grouping.yaml --mock --seeds 3

    # 真实模型模式（无 GPU 也可：小模型 + CPU 慢速验证器）
    python3 eval_runner.py --model Qwen/Qwen2.5-0.5B --ctx 2048

    # 真实运行（需 GPU + V4.1-Flash-1017；当前无卡环境会明确报错而非静默失败）
    python3 eval_runner.py --card experiments/E1_semantic_grouping.yaml
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src" / "semantickv"))


# =====================================================================
# §1 接口骨架：数据结构
# =====================================================================

@dataclass
class GroupAssignment:
    """SemanticGrouper.fit / partial_fit 的输出。

    labels    : (N,)   int32   每个 token 的群编号（0..n_groups-1，紧凑连续）
    centroids : (G, d) float32 群质心（L2 归一化后的均值再归一化）
    sizes     : (G,)   int64   每群 token 数
    n_groups  : int            群数量
    meta      : dict           算法元数据（耗时、k、阈值、ARI 等）
    """
    labels: np.ndarray
    centroids: np.ndarray
    sizes: np.ndarray
    n_groups: int
    meta: dict = field(default_factory=dict)

    def group_of(self, token_idx: np.ndarray) -> np.ndarray:
        return self.labels[token_idx]


@dataclass
class EdgeSet:
    """RelationGraph 的边集（群间关系）。weight 为关系强度，turn 为逻辑时钟。"""
    src: np.ndarray       # (E,) int32
    dst: np.ndarray       # (E,) int32
    weight: np.ndarray    # (E,) float32
    turn: np.ndarray      # (E,) int32  产生/更新该边的轮次


@dataclass
class Delta:
    """增量边集（CRDT 思路）：自上次 checkpoint 以来的 added/removed/updated。"""
    added: EdgeSet
    removed: EdgeSet
    updated: EdgeSet      # 仅权重变化的边（新权重）

    def __len__(self) -> int:
        return len(self.added.src) + len(self.removed.src) + len(self.updated.src)


@dataclass
class ReorgDecision:
    """ReorgScheduler.should_reorg 的输出。"""
    action: str           # "full"（全量重组）| "partial"（局部增量）| "none"
    reason: str           # 触发原因（写入日志，供失败归因）
    est_cost_s: float     # 预估耗时（秒）


@dataclass
class TurnStats:
    """一轮对话的统计量，供调度器决策。"""
    turn: int
    n_tokens_total: int
    n_new_tokens: int
    drift: float          # 语义漂移度量 ∈ [0,1]，见 mock_scheduler.drift_measure


# =====================================================================
# §1 接口骨架：三个核心组件
# =====================================================================

class SemanticGrouper(ABC):
    """语义分组器：把 token embedding 序列划分为语义群。

    有卡环境实现要求：
      - fit()        全量聚类，输出紧凑群编号；
      - partial_fit() 增量聚类（新 token 挂靠/新建群 + 局部标签传播），
                      不得为了 O(1) 更新而重算全量 kNN；
      - centroids()  当前群质心，供 RelationGraph 使用。
    """

    @abstractmethod
    def fit(self, embeddings: np.ndarray) -> GroupAssignment:
        """全量语义分组。

        输入: embeddings (N, d) float32，L2 归一化
        输出: GroupAssignment
        """

    @abstractmethod
    def partial_fit(self,
                    new_embeddings: np.ndarray,
                    prev: GroupAssignment) -> GroupAssignment:
        """增量语义分组（多轮对话场景）。

        输入: new_embeddings (N_new, d) float32；prev 上一轮 GroupAssignment
        输出: 覆盖旧+新全部 token 的 GroupAssignment
        """

    @abstractmethod
    def centroids(self) -> np.ndarray:
        """当前群质心 (G, d) float32。"""


class RelationGraph(ABC):
    """群间关系图：细粒度 top-p 稀疏边 + EMA 权重 + Delta/CRDT 合并。

    有卡环境实现要求：
      - build_full()   全量建图（作为新鲜度参照）；
      - observe()      EMA 增量更新并产出 Delta；
      - merge()        CRDT 式合并（LWW-Register，turn 为逻辑时钟）；
      - freshness_error() 增量状态 vs 全量重算的相对误差（E4 主指标之一）。
    """

    @abstractmethod
    def build_full(self, centroids: np.ndarray, turn: int) -> EdgeSet:
        """全量建 top-p 边。

        输入: centroids (G, d)；turn 逻辑时钟
        输出: EdgeSet（每条边 weight = f(质心距离)，见实现）
        """

    @abstractmethod
    def observe(self, observed: EdgeSet, turn: int) -> Delta:
        """EMA 增量更新并计算 Delta。

        输入: observed 本轮观测到的边（来自新质心/新共现）；turn
        输出: Delta（added/removed/updated）
        """

    @abstractmethod
    def merge(self, delta: Delta) -> None:
        """CRDT 式合并 Delta（幂等、可乱序）。"""

    @abstractmethod
    def freshness_error(self, reference: EdgeSet) -> float:
        """增量图状态 vs 全量重图的相对 L2 误差（缺失边按 0 计）。"""


class ReorgScheduler(ABC):
    """重组调度器：决定何时做全量/局部/不重组。

    有卡环境实现要求：
      - should_reorg() 在每轮边界调用，返回决策（含原因，供归因）；
      - on_turn_boundary() 维护内部轮次状态。
    """

    @abstractmethod
    def should_reorg(self, turn: int, drift: float, stats: TurnStats) -> ReorgDecision:
        """是否重组。

        输入: turn 轮次；drift 语义漂移；stats 本轮统计
        输出: ReorgDecision
        """

    @abstractmethod
    def on_turn_boundary(self, turn: int) -> None:
        """轮次边界钩子（推进逻辑时钟、重置统计）。"""


# =====================================================================
# §1 接口骨架：签名校验
# =====================================================================

EXPECTED_SIGNATURES = {
    "SemanticGrouper": {
        "fit": ["self", "embeddings"],
        "partial_fit": ["self", "new_embeddings", "prev"],
        "centroids": ["self"],
    },
    "RelationGraph": {
        "build_full": ["self", "centroids", "turn"],
        "observe": ["self", "observed", "turn"],
        "merge": ["self", "delta"],
        "freshness_error": ["self", "reference"],
    },
    "ReorgScheduler": {
        "should_reorg": ["self", "turn", "drift", "stats"],
        "on_turn_boundary": ["self", "turn"],
    },
}


def check_interfaces() -> bool:
    """校验 mock 实现与接口骨架签名一致。返回 True 表示全部通过。"""
    from mock_grouper import MockSemanticGrouper
    from mock_relation import MockRelationGraph
    from mock_scheduler import MockReorgScheduler

    candidates = {
        "SemanticGrouper": MockSemanticGrouper,
        "RelationGraph": MockRelationGraph,
        "ReorgScheduler": MockReorgScheduler,
    }
    ok = True
    for iface_name, methods in EXPECTED_SIGNATURES.items():
        cls = candidates[iface_name]
        for m, params in methods.items():
            fn = getattr(cls, m, None)
            if fn is None:
                print(f"[FAIL] {iface_name}.{m} 缺失")
                ok = False
                continue
            actual = list(inspect.signature(fn).parameters)
            if actual != params:
                print(f"[FAIL] {iface_name}.{m} 签名 {actual} != 期望 {params}")
                ok = False
            else:
                print(f"[ OK ] {iface_name}.{m}{tuple(params[1:])}")

    # 额外校验：真实环境空壳 AnchorBasedSemanticGrouper 同样满足
    # SemanticGrouper 签名（有卡环境填充 hook 逻辑时不会破坏契约）。
    try:
        from anchor_grouper import AnchorBasedSemanticGrouper
        for m, params in EXPECTED_SIGNATURES["SemanticGrouper"].items():
            fn = getattr(AnchorBasedSemanticGrouper, m, None)
            actual = list(inspect.signature(fn).parameters) if fn else None
            if actual != params:
                print(f"[FAIL] AnchorBasedSemanticGrouper.{m} 签名 {actual} != 期望 {params}")
                ok = False
            else:
                print(f"[ OK ] AnchorBasedSemanticGrouper.{m}{tuple(params[1:])}")
    except ImportError as e:
        print(f"[FAIL] 无法导入 anchor_grouper: {e}")
        ok = False

    print("\n接口校验结果:", "全部通过 ✔" if ok else "存在不一致 ✘")
    return ok


# =====================================================================
# §2 CLI：加载预注册卡 -> 运行 -> 序贯决策 -> 落盘
# =====================================================================

def load_card(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_card(card: dict, mock: bool, seeds: int, out_dir: Path) -> dict:
    """按预注册卡运行一个实验。

    mock=True   -> 使用 src/semantickv/mock_*.py + 合成数据（CPU 冒烟）
    mock=False  -> 需要真实 V4.1-Flash 推理环境；当前环境明确报错
    """
    import metrics

    exp_id = card["meta"]["id"]
    direction = card["metrics"]["direction"]

    if mock:
        # mock 模式：用卡片内的代理阈值（intra_sim，越高越好），
        # 因为真实主指标（相位差/吞吐等）在无卡环境不可测。
        pass_line = float(card["mock"]["pass_line"])
        falsify_line = float(card["mock"]["falsify_line"])
        direction = "higher_is_better"
    else:
        pass_line = float(card["thresholds"]["pass_line"])
        falsify_line = float(card["thresholds"]["falsify_line"])

    if not mock:
        raise RuntimeError(
            f"[{exp_id}] 真实运行需要 GPU + V4.1-Flash-1017 推理环境，"
            "当前沙箱无 GPU。请在有卡环境运行，或先用 --mock 做流水线冒烟。"
        )

    # ---- mock 模式：用合成数据驱动完整流水线 ----
    from mock_grouper import MockSemanticGrouper
    from synth import make_semantic_batch

    mock_cfg = card.get("mock", {})
    samples = []
    t0 = time.time()
    for s in range(seeds):
        X, _ = make_semantic_batch(
            n=int(mock_cfg.get("n_tokens", 20_000)),
            d=int(mock_cfg.get("dim", 768)),
            n_topics=int(mock_cfg.get("n_topics", 32)),
            rng=np.random.default_rng(1000 + s),
        )
        g = MockSemanticGrouper(k=16)
        ga = g.fit(X)
        # mock 主指标：以「群内余弦相似度均值」代理任务准确率（仅验证流水线）
        samples.append(float(ga.meta["intra_sim"]))
    mean, lo, hi = metrics.mean_ci(samples)
    if direction == "higher_is_better":
        decision = metrics.sequential_decision(samples, pass_line, falsify_line)
    else:  # lower_is_better：取负后走同一决策逻辑
        decision = metrics.sequential_decision([-s for s in samples], -pass_line, -falsify_line)

    result = {
        "experiment": exp_id,
        "mode": "mock",
        "note": "mock 指标仅验证流水线机械正确性，不构成对 H0/H1 的证据",
        "samples": samples,
        "mean": mean, "ci95": [lo, hi],
        "pass_line": pass_line, "falsify_line": falsify_line,
        "decision": decision,
        "wall_time_s": round(time.time() - t0, 2),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"{exp_id}_mock_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="SemanticKV-Reorg 实验运行器")
    ap.add_argument("--card", type=str, help="预注册卡路径 (experiments/Ex_*.yaml)")
    ap.add_argument("--mock", action="store_true", help="CPU 合成数据冒烟模式")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--check-interfaces", action="store_true", help="校验 Mock 实现签名")
    ap.add_argument("--out", type=str, default=str(ROOT / "results"))
    # ---- 真实模型模式（DeepSeek 方案一：小模型 + CPU/GPU 慢速验证器） ----
    ap.add_argument("--model", type=str, default="",
                    help="真实模型 id 或本地路径（HF AutoModelForCausalLM，"
                         "如 Qwen/Qwen2.5-0.5B）；提供后进入真实模型验证模式")
    ap.add_argument("--ctx", type=int, default=2048, help="真实模型模式的上下文长度")
    ap.add_argument("--needle-positions", type=str, default="0.5",
                    help="needle 相对位置，逗号分隔")
    ap.add_argument("--skip-needle", action="store_true", help="跳过 needle 检索测试")
    ap.add_argument("--hf-endpoint", type=str, default="",
                    help="HF 镜像（国内可用 https://hf-mirror.com）")
    args = ap.parse_args()

    if args.check_interfaces:
        return 0 if check_interfaces() else 1

    if args.model:
        # 真实模型模式：转交慢速验证器（src/semantickv/v41_torch_native_runner.py）
        import importlib.util
        runner_path = ROOT / "src" / "semantickv" / "v41_torch_native_runner.py"
        spec = importlib.util.spec_from_file_location("v41_torch_native_runner", runner_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.argv = ["v41_torch_native_runner.py",
                    "--model", args.model, "--ctx", str(args.ctx),
                    "--needle-positions", args.needle_positions,
                    "--out", str(Path(args.out) / "real_model_validation.json")]
        if args.skip_needle:
            sys.argv.append("--skip-needle")
        if args.hf_endpoint:
            sys.argv += ["--hf-endpoint", args.hf_endpoint]
        return mod.main()

    if not args.card:
        ap.error("必须指定 --card / --check-interfaces / --model")
    card = load_card(Path(args.card))
    run_card(card, mock=args.mock, seeds=args.seeds, out_dir=Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
