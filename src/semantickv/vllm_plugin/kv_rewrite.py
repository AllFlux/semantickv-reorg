# -*- coding: utf-8 -*-
"""KVIndexRewriter —— 槽位选择与 KV 索引重写（H-DS15，LOCKS 式「摘要路由+索引重写」）。

与版本无关的两阶段抽象：
  Phase A（路由）: SemanticRouter 产出 GroupAssignment（谁和谁语义同群）
  Phase B（重写）: 本模块按Assignment 选出保留槽位并产出「新索引表」，
                   交给 vLLM 侧执行物理重写/截断。

WB1 已验证的三种重写策略（0.5B 实测结论，7B 上按 H-DS10 门禁复验）：
  select_topk        —— 每群注意力入度 top-K 真实 KV（主策略）
  select_hybrid      —— Sink(4)+滑窗(128) 全量 + 中段 K 代表（E1.5，0.5B 被否，
                        7B 复验中；块对角取舍见 sink_protect.segmented_attention）
  select_flat        —— 群均值槽（v0.1.x，已被数据否定，仅存档）
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
import torch


class KVIndexRewriter:
    def __init__(self, plan, topk_per_group: int = 4):
        """plan: SinkProtectPlanLite（特权位方案）。"""
        self.plan = plan
        self.K = max(1, int(topk_per_group))

    def select_topk(self, importance: np.ndarray, labels: np.ndarray,
                    n_groups: int) -> np.ndarray:
        """每群按注意力入度选 top-K 位置（与 runner.select_topk_per_group 同语义，
        去 per-KV-head 循环的简化版：importance 传入已按头平均）。"""
        sel = []
        for g in range(n_groups):
            members = np.flatnonzero(labels == g)
            if members.size <= self.K:
                sel.append(members)
            else:
                sel.append(members[np.argsort(-importance[members])[:self.K]])
        return np.sort(np.concatenate(sel))

    def select_hybrid(self, importance: np.ndarray, labels: np.ndarray,
                      n_groups: int) -> Tuple[np.ndarray, dict]:
        """Sink(4)+滑窗(plan.window_tokens) 全量 + 中段每群 K 代表。

        返回 (保留位置, meta)；meta 含 mid_kv_retention_ratio（DS-006 要求的记录项）。
        """
        N = len(labels)
        priv_pos, _ = self.plan_plan(N)
        G_base = n_groups
        mid = []
        for g in range(G_base):
            members = np.flatnonzero(labels == g)
            if members.size <= self.K:
                mid.append(members)
            else:
                mid.append(members[np.argsort(-importance[members])[:self.K]])
        mid_pos = np.sort(np.concatenate(mid)) if mid else np.array([], dtype=int)
        keep = np.sort(np.unique(np.concatenate([priv_pos, mid_pos])))
        n_mid_tokens = max(N - len(priv_pos), 1)
        meta = {"mid_slots": int(len(mid_pos)),
                "mid_kv_retention_ratio": round(float(len(mid_pos)) / n_mid_tokens, 4),
                "privileged_slots": int(len(priv_pos))}
        return keep, meta

    def plan_plan(self, seq_len: int) -> Tuple[np.ndarray, np.ndarray]:
        s = min(self.plan.sink_tokens, seq_len)
        w = min(self.plan.window_tokens, seq_len)
        priv = np.zeros(seq_len, dtype=bool)
        priv[:s] = True
        priv[seq_len - w:] = True
        return np.flatnonzero(priv), priv

    def rewrite_index(self, keep_positions: np.ndarray) -> torch.Tensor:
        """产出新索引表（交给 vLLM 侧 gather/截断的物理执行依据）。"""
        return torch.as_tensor(keep_positions, dtype=torch.long)
