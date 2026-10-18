# -*- coding: utf-8 -*-
"""SemanticRouter —— vLLM 插件的压缩路由层（H-DS15 / DS-20261018-009-WB1）。

职责（阶段级，非 token 级）：
  prefill 完成 → 隐状态/缓存 K 聚类分组（默认 GSC 数学聚类，
  可被 load_external_router 挂载的学习型 Router 替换）→
  产出每层 GroupAssignment → KVIndexRewriter 据此重写 KV 索引。

WB2 预留接口（cross_talk_to_wb2 承诺项）：
  router = SemanticRouter.load_external_router("wb2_router.pt")
  —— WB2 训练的轻量注意力路由模型（输入 hidden/K，输出每 token 群 id
  或每槽位重要性分布）可直接挂载，替代 GSC 数学聚类。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

import numpy as np
import torch


class ExternalRouter(Protocol):
    """WB2 学习型路由模型的最小协议。

    route(hidden: np.ndarray (N,d) 或 torch.Tensor) -> labels (N,) int64
    """

    def route(self, hidden) -> np.ndarray: ...


class SemanticRouter:
    """压缩路由层：决定「哪些 token 的 KV 保留、以何种粒度保留」。"""

    def __init__(self, k: int = 8, tau: float = 0.97, metric: str = "centered",
                 sink_tokens: int = 4, window_tokens: int = 128):
        self.k, self.tau, self.metric = k, tau, metric
        self.plan = SinkProtectPlanLite(sink_tokens, window_tokens)
        self._external: Optional[ExternalRouter] = None

    # ---- WB2 挂载点（cross_talk 承诺项） ----
    @classmethod
    def load_external_router(cls, weights_path: str, **kwargs) -> "SemanticRouter":
        """挂载 WB2 训练的轻量注意力路由模型。

        weights_path 约定（torch.save 的 dict）：
          { "state_dict": ..., "config": {"input_dim": d, "arch": "..."} }
        对象须实现 route(hidden) -> labels (N,)。
        """
        router = cls(**kwargs)
        p = Path(weights_path)
        if not p.exists():
            raise FileNotFoundError(f"外部路由模型不存在: {p}")
        blob = torch.load(p, map_location="cpu", weights_only=False)

        class _Loaded:
            def __init__(self, blob):
                self.blob = blob
                # TODO(WB2): 按 blob["config"]["arch"] 重建网络并 load_state_dict
                raise NotImplementedError(
                    "等待 WB2 的路由模型 arch 定义；接口已就绪")

            def route(self, hidden):
                raise NotImplementedError

        router._external = _Loaded(blob)
        return router

    # ---- 默认路径：GSC 数学聚类（runner 的 MockSemanticGrouper） ----
    def route(self, hidden: np.ndarray) -> np.ndarray:
        if self._external is not None:
            return np.asarray(self._external.route(hidden), dtype=np.int64)
        from mock_grouper import MockSemanticGrouper
        from v41_torch_native_runner import metric_transform, smooth_hidden
        H = smooth_hidden(metric_transform(hidden, self.metric), 8,
                          renormalize=(self.metric != "l2"))
        g = MockSemanticGrouper(k=self.k, tau=self.tau, metric=self.metric).fit(H)
        return g.labels

    def assign(self, labels: np.ndarray, seq_len: int) -> np.ndarray:
        """应用特权位保护（Sink+滑窗 独占槽位）后返回最终分组。"""
        return self.plan.apply(labels, seq_len)


class SinkProtectPlanLite:
    """sink_protect.SinkProtectPlan 的轻量别名（避免插件依赖完整 runner）。"""

    def __init__(self, sink_tokens: int, window_tokens: int):
        self.sink_tokens, self.window_tokens = sink_tokens, window_tokens

    def apply(self, labels: np.ndarray, seq_len: int) -> np.ndarray:
        from v41_torch_native_runner import apply_privileged_tokens
        return apply_privileged_tokens(labels, self.sink_tokens,
                                       self.window_tokens, force_bos=True)
