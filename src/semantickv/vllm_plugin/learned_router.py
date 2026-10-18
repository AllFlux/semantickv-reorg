#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""learned_router.py — DS 规格 Router 空壳（DS-20261018-011-WB1 / H-DS15 / H-DS17）。

本文件中的 `SemanticRouter(nn.Module)` 按 DS-20261018-011-WB1 `cross_talk_to_wb2`
提供的类模板**逐字实现**（结构、默认参数、forward 语义一字不差）：

    class SemanticRouter(nn.Module):
        def __init__(self, q_dim=1024, hidden_dim=256, num_groups=64):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(q_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, num_groups)
            )
        def forward(self, query_embeds):
            # 输出 shape: (batch, num_groups) 的 logits
            return self.net(query_embeds)

设计约束（DS 原文要求）：
  - 纯 PyTorch 实现（Linear + ReLU），严禁任何复杂算子；
  - 权重以 .pt 导出，WB2 训练完毕后直接 `load_state_dict` 即可用；
  - 参数量 ≪ 50M（默认配置约 0.28M），单次前向预期 < 0.5ms（H-DS17 通过线）。

命名说明：
  DS 规格类名为 `SemanticRouter`；为避免与本包
  `semantic_router.SemanticRouter`（GSC 数学聚类路由层）同名冲突，
  本模块同时导出别名 `LearnedSemanticRouter = SemanticRouter`，
  包外引用请用别名；`load_external_router` 内部按 DS 规格名加载。

q_dim 选型（按挂载模型的 hidden_size 传参，默认 1024 为 DS 给定值）：
  Qwen2.5-0.5B → 896；Qwen2.5-1.5B → 1536；Qwen2.5-7B → 3584。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn


class SemanticRouter(nn.Module):
    """DS-20261018-011-WB1 规格的学习型群路由网络（空壳待 WB2 训练）。

    输入 query_embeds: (batch, q_dim) 或 (batch, N, q_dim) 的 query 隐状态
      （post-smoothing；WB1 侧约定 = metric_transform + smooth_hidden 后的表示）
    输出: (batch, num_groups) 或 (batch, N, num_groups) 的 logits，
      argmax(-1) 即每 token/每 query 的群 id。
    """

    def __init__(self, q_dim: int = 1024, hidden_dim: int = 256,
                 num_groups: int = 64):
        super().__init__()
        self.q_dim = q_dim
        self.hidden_dim = hidden_dim
        self.num_groups = num_groups
        self.net = nn.Sequential(
            nn.Linear(q_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, num_groups)
        )

    def forward(self, query_embeds: torch.Tensor) -> torch.Tensor:
        # 输出 shape: (batch, num_groups) 的 logits
        return self.net(query_embeds)

    # ---- WB2 侧约定接口（load_state_dict 之外的便捷层，非 DS 规格必需） ----
    @torch.no_grad()
    def route(self, query_embeds: torch.Tensor) -> torch.Tensor:
        """logits → 群 id (..., ) int64（推理路径，禁梯度）。"""
        return self.forward(query_embeds).argmax(dim=-1).to(torch.int64)

    def num_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters())

    def save_pt(self, path: str, config: Optional[dict] = None) -> Path:
        """按 load_external_router 约定的 blob 格式导出。"""
        blob = {"state_dict": self.state_dict(),
                "config": {"arch": "ds011_linear_relu_linear",
                           "q_dim": self.q_dim, "hidden_dim": self.hidden_dim,
                           "num_groups": self.num_groups, **(config or {})}}
        p = Path(path)
        torch.save(blob, p)
        return p

    @classmethod
    def from_pt(cls, path: str, map_location: str = "cpu") -> "SemanticRouter":
        blob = torch.load(Path(path), map_location=map_location,
                          weights_only=False)
        cfg = blob.get("config", {}) if isinstance(blob, dict) else {}
        net = cls(q_dim=cfg.get("q_dim", 1024),
                  hidden_dim=cfg.get("hidden_dim", 256),
                  num_groups=cfg.get("num_groups", 64))
        net.load_state_dict(blob["state_dict"])
        net.eval()
        return net


# 导出别名：避免与 semantic_router.SemanticRouter（GSC 路由层）同名
LearnedSemanticRouter = SemanticRouter

__all__ = ["SemanticRouter", "LearnedSemanticRouter"]
