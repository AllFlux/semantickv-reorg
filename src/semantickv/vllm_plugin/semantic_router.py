# -*- coding: utf-8 -*-
"""SemanticRouter —— vLLM 插件的压缩路由层（H-DS15 / H-DS17 / DS-20261018-011-WB1）。

职责（阶段级，非 token 级）：
  prefill 完成 → 隐状态/缓存 K 聚类分组（默认 GSC 数学聚类，
  可被 load_external_router 挂载的学习型 Router 替换）→
  产出每层 GroupAssignment → KVIndexRewriter 据此重写 KV 索引。

WB2 预留接口（cross_talk_to_wb2 承诺项，DS-20261018-011 定案）：
  router = SemanticRouter.load_external_router("wb2_router.pt")
  —— 加载 learned_router.SemanticRouter(nn.Module)（DS 规格：纯
  Linear+ReLU，q_dim/hidden_dim/num_groups 可配，默认 1024/256/64）。
  WB2 训练完毕后直接 load_state_dict 即可，无需改本文件。

.pt blob 约定（torch.save 的 dict）：
  { "state_dict": <DS 规格网络权重>,
    "config": {"arch": "ds011_linear_relu_linear",
               "q_dim": int, "hidden_dim": int, "num_groups": int} }
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

    # ---- WB2 挂载点（cross_talk 承诺项 · DS-20261018-011 定案） ----
    @classmethod
    def load_external_router(cls, weights_path: str, **kwargs) -> "SemanticRouter":
        """挂载 WB2 训练的 DS 规格路由网络（learned_router.SemanticRouter）。

        weights_path 约定（torch.save 的 dict，见 learned_router.save_pt）：
          { "state_dict": <nn.Module 权重>,
            "config": {"arch": "ds011_linear_relu_linear",
                       "q_dim": int, "hidden_dim": int, "num_groups": int} }
        加载后外部路由经 route(hidden) -> labels (N,) int64 替代 GSC 聚类。
        """
        router = cls(**kwargs)
        p = Path(weights_path)
        if not p.exists():
            raise FileNotFoundError(f"外部路由模型不存在: {p}")
        from .learned_router import LearnedSemanticRouter

        blob = torch.load(p, map_location="cpu", weights_only=False)
        cfg = blob.get("config", {}) if isinstance(blob, dict) else {}
        net = LearnedSemanticRouter(
            q_dim=cfg.get("q_dim", 1024),
            hidden_dim=cfg.get("hidden_dim", 256),
            num_groups=cfg.get("num_groups", 64))
        net.load_state_dict(blob["state_dict"])
        net.eval()

        class _Loaded:
            """DS 规格网络 → ExternalRouter 协议适配（hidden → labels）。"""

            def __init__(self, net):
                self.net = net

            def route(self, hidden):
                x = torch.as_tensor(hidden)
                if x.dtype != next(self.net.parameters()).dtype:
                    x = x.to(next(self.net.parameters()).dtype)
                with torch.no_grad():
                    return self.net.route(x).cpu().numpy()

        router._external = _Loaded(net)
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
