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

    # ---- WB2 挂载点（cross_talk 承诺项 · DS-20261018-011 定案 / DS-20261019-018 增强） ----
    @classmethod
    def load_external_router(cls, weights_path: str, *,
                             score_sign: int = -1,
                             expected_feature_dim: Optional[int] = None,
                             inference_tau: Optional[float] = None,
                             **kwargs) -> "SemanticRouter":
        """挂载 WB2 训练的 DS 规格路由网络（learned_router.SemanticRouter）。

        weights_path 约定（torch.save 的 dict，见 learned_router.save_pt
        与 src/shared/README.md §2）：
          { "state_dict": <nn.Module 权重>,
            "config": {"arch": "ds011_linear_relu_linear",
                       "q_dim": int, "hidden_dim": int, "num_groups": int,
                       "score_sign": int, "feature_dim": int, "train_tau": float} }
        加载后外部路由经 route(hidden) -> labels (N,) int64 替代 GSC 聚类。

        DS-20261019-018 三项防护：
          1. score_sign（负相关修正）：WB2 模型 Pearson r=-0.80，直接以 score
             选群会选出最烂的群 → 默认 -1（对分数取负后 argmax）。blob 内
             "score_sign" 字段优先于本参数；若 WB2 重新导出翻转权重的 .pt，
             须显式传 +1——两种修正方式不可叠加。
          2. Train-Serve Skew（feature_dim）：route 时校验输入最后一维，
             不符立即报错而非静默错配（DS 警告 τ=200 训练 vs τ=1 推理会致
             路由准确率归零；维度/温度约定见 src/shared/README.md §3）。
          3. 结构诊断：load_state_dict 失败时输出 missing/unexpected keys
             对照，直接定位 H-DS24 的「WB2 结构与预期 MLP 不符」失败模式。
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
        try:
            net.load_state_dict(blob["state_dict"])
        except Exception as e:                      # noqa: BLE001
            expected = set(net.state_dict().keys())
            got = set(blob["state_dict"].keys())
            raise RuntimeError(
                f"Router 权重与 DS 规格 MLP 结构不符（H-DS24 失败模式①）\n"
                f"  缺失 keys: {sorted(expected - got) or '无'}\n"
                f"  多余 keys: {sorted(got - expected) or '无'}\n"
                f"  原始异常: {type(e).__name__}: {e}") from e
        net.eval()

        # blob 内字段优先；否则用调用参数
        sign = int(cfg.get("score_sign", score_sign))
        feat_dim = cfg.get("feature_dim", expected_feature_dim)
        train_tau = cfg.get("train_tau")
        if train_tau is not None and inference_tau is not None \
                and abs(float(train_tau) - float(inference_tau)) > 1e-9:
            import warnings
            warnings.warn(
                f"Train-Serve Skew 风险（DS-20261019-018）：Router 训练温度 "
                f"τ={train_tau} ≠ 推理温度 τ={inference_tau}，输入特征分布外，"
                f"路由准确率可能归零。请与 WB2 约定隐状态提取维度/温度"
                f"（src/shared/README.md §3）。", RuntimeWarning, stacklevel=2)

        class _Loaded:
            """DS 规格网络 → ExternalRouter 协议适配（hidden → labels）。"""

            def __init__(self, net, sign, feat_dim, train_tau):
                self.net = net
                self.sign = sign
                self.feat_dim = feat_dim
                self.train_tau = train_tau

            def route(self, hidden):
                x = torch.as_tensor(hidden)
                w_dtype = next(self.net.parameters()).dtype
                if x.dtype != w_dtype:
                    x = x.to(w_dtype)
                if self.feat_dim is not None and x.shape[-1] != self.feat_dim:
                    raise ValueError(
                        f"Train-Serve Skew 防护：输入特征维度 {x.shape[-1]} "
                        f"≠ Router 训练维度 {self.feat_dim}。请按 src/shared/README.md "
                        f"§3 与 WB2 约定隐状态提取方式后再调用。")
                with torch.no_grad():
                    logits = self.net(x)
                    # 负相关修正：sign=-1 时对分数取负后 argmax（DS-20261019-018）
                    return (self.sign * logits).argmax(dim=-1).to(torch.int64).cpu().numpy()

        router._external = _Loaded(net, sign, feat_dim, train_tau)
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
