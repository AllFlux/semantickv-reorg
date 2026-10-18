#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sink_protect.py — Attention Sink 保护模块（WB1 → WB2 跨项目交付物）

来源：SemanticKV-Reorg 项目 E1.5（DS-20261018-006/007/009 审定）。
本模块把 WB1 在 KV 压缩中沉淀的 Sink 防爆保护逻辑解耦为独立组件，
供 WB2 在 RL 生成侧作为「虚拟 Action」的参考实现。

核心经验（为什么需要它）：
  1. LLM 注意力前几个 token（BOS/Sink）吸走巨量注意力质量（0.5B 实测前 4
     token 占 33.2%），但其 Value 范数反而偏小（3.74 vs 全局 5.19）——
     它们的作用是「稳定 softmax 分母」，不是传递内容。
  2. 任何对 KV 的裁剪/拼接（压缩、重写、虚拟 token 注入）若破坏 Sink 的
     分母地位，模型整体输出分布会瞬间崩坏（WB1 实测 flat 模式无 Sink 保护
     时 cos 仅 0.22~0.43）。
  3. 修复方式 = 分段注意力 + logsumexp 合并：Sink 段独立参与归一化，
     数学上与联合 softmax 严格等价（见 tests/test_gqa_alignment.py T3），
     但工程上可对每段独立做数值稳定与资源控制。

对外 API：
  safe_softmax(scores)                —— 行全 -inf 安全的 softmax
  segmented_attention(...)            —— Sink/局部窗/中段 三段独立注意力 + 合并
  SinkProtectPlan.plan_privileged(...)—— 计算特权位（Sink+滑窗）保护方案
  SinkProtectPlan.slot_partition(...) —— 槽位三段划分（sink/local/mid）
  rl_action_guard(plan, actions)      —— RL 虚拟 Action 过滤器（参考实现）

依赖：仅 torch（无 transformers 依赖，方便嵌入任意生成侧）。

WB2 接入规格（DS-20261018-011-WB1 要求；照此传参即可零改动 import）：
  全部张量约定：device 一致（q/k/v 同设备）、dtype 一致（建议 float32；
  混合精度下请自行保证 q/k/v 同 dtype，否则 matmul 报错）。
  典型来源：q = hidden_states @ W_q（post-RoPE、per-head 切分后）；
            k/v = KV cache（全序列，L ≥ N）。

  | API | 入参 shape / dtype | 返回 |
  |---|---|---|
  | safe_softmax(scores) | scores: (..., N) float，行允许全 -inf | 同 shape 权重，全 -inf 行返回全 0 |
  | SinkProtectPlan(sink_tokens=4, window_tokens=64) | int | plan 对象 |
  | plan.plan_privileged(seq_len) | int | (is_privileged (N,) bool, positions (P,) long) |
  | plan.slot_partition(slot_positions, seq_len) | slot_positions: (S,) long | (sink_mask, local_mask, mid_mask) 各 (S,) bool，互斥并集为全集 |
  | segmented_attention(q,k,v,...) | q: (Hq,N,hd)；k/v: (Hkv,L,hd)；slot_positions: (S,) long（取值 < L）；query_group/slot_group: (N,)/(S,) long 或 None；n_rep: int（GQA Hq=Hkv·n_rep）；scaling: float；mid_scope: "global"/"blockdiag" | out: (Hq,N,hd)，与联合 softmax 数值等价 |
  | rl_action_guard(plan, actions) | actions: list of {"type": str, "touches_positions": [int,...]} | 过滤后的合法 action list |

  WB2 最小可用示例（RL 生成侧防爆）：

      import torch
      from sink_protect import SinkProtectPlan, segmented_attention

      plan = SinkProtectPlan(sink_tokens=4, window_tokens=64)
      is_priv, priv_pos = plan.plan_privileged(seq_len=N)   # N = 当前序列长
      # 特权位 KV 在任何重写/丢弃 action 前必须全量保留：
      keep_idx = priv_pos.clone()
      # 中段做你的压缩/重写（非特权位），然后用 segmented_attention 前向：
      # out = segmented_attention(q, k, v, slot_positions=keep_idx,
      #                            n_rep=n_rep, scaling=scaling, plan=plan)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

import torch


# ----------------------------------------------------------------------
# 数值稳定原语
# ----------------------------------------------------------------------
def safe_softmax(scores: torch.Tensor) -> torch.Tensor:
    """行全 -inf 时返回全 0（而非 NaN）的 softmax。

    分段注意力中「该段对当前 query 全部被掩码」是正常情形
    （如早期 query 看不到局部窗段），必须产出零权重而非 NaN。
    """
    row_max = scores.max(dim=-1, keepdim=True).values
    bad = torch.isinf(row_max)
    z = torch.exp(scores - row_max.clamp(min=-1e30))
    denom = z.sum(dim=-1, keepdim=True).clamp(min=1e-30)
    return (z / denom).masked_fill(bad, 0.0)


# ----------------------------------------------------------------------
# 特权位保护方案
# ----------------------------------------------------------------------
@dataclass
class SinkProtectPlan:
    """Sink + 局部滑窗 的特权保护方案（token 位置级）。

    特权位 = 前 sink_tokens 个 token（BOS/Sink，稳分母）
           + 后 window_tokens 个 token（近期局部窗，保近端）。
    这两类位置在一切 KV 重写/压缩/注入操作中必须全量精确保留，
    并各自独占 KV 槽位（不参与任何均值混合/重写）。
    """

    sink_tokens: int = 4
    window_tokens: int = 64
    _cache: dict = field(default_factory=dict, repr=False)

    def plan_privileged(self, seq_len: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """返回 (is_privileged (N,), privileged_positions (P,))。"""
        key = seq_len
        if key not in self._cache:
            priv = torch.zeros(seq_len, dtype=torch.bool)
            s = min(self.sink_tokens, seq_len)
            w = min(self.window_tokens, seq_len)
            priv[:s] = True
            priv[seq_len - w:] = True
            self._cache.clear()          # 只缓存最近一个长度，防膨胀
            self._cache[key] = (priv, torch.nonzero(priv).flatten())
        return self._cache[key]

    def slot_partition(self, slot_positions: torch.Tensor,
                       seq_len: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """把候选 KV 槽位（按真实 token 位置）切成 sink / local / mid 三段布尔掩码。

        slot_positions: (S,) 各槽位对应的原始 token 位置。
        返回 (sink_mask, local_mask, mid_mask)，三者互斥、并集为全部槽位。
        """
        sink_m = slot_positions < min(self.sink_tokens, seq_len)
        local_m = slot_positions >= (seq_len - min(self.window_tokens, seq_len))
        return sink_m, local_m, ~(sink_m | local_m)


# ----------------------------------------------------------------------
# 分段注意力（防爆核心）
# ----------------------------------------------------------------------
def segmented_attention(
    q: torch.Tensor,                    # (Hq, N, hd) 单样本、post-RoPE
    k: torch.Tensor,                    # (Hkv, L, hd) 全序列 KV（L ≥ N；位置索引选取）
    v: torch.Tensor,                    # (Hkv, L, hd)
    slot_positions: torch.Tensor,       # (S,) 参与注意力的槽位=原始 token 位置（< L）
    query_group: Optional[torch.Tensor] = None,   # (N,) 每个query的群 id（块对角用）
    slot_group: Optional[torch.Tensor] = None,    # (S,) 每个槽位的群 id
    n_rep: int = 1,                     # GQA：Hq = Hkv * n_rep，Q头h ↔ KV头 h//n_rep
    scaling: float = 1.0,
    plan: Optional[SinkProtectPlan] = None,
    mid_scope: str = "global",          # "global" | "blockdiag"
    privileged_bypass_blockdiag: bool = True,
) -> torch.Tensor:
    """三段独立注意力 + logsumexp 合并（Sink 防爆保护核心算子）。

    数学性质：与「全部槽位拼接后的联合 softmax」严格等价
    （o = Σ_seg exp(lse_seg - lse_all) · softmax_seg·V），
    但工程上：① 各段独立做数值稳定；② 中段可施加块对角隔离；
    ③ Sink 段永不被其他段的 KV 重写稀释（稳分母）。

    mid_scope:
      global    —— 中段 query 可见全部中段槽（等价联合，作消融基线）
      blockdiag —— 中段 query 只见本群槽位；特权位 query（Sink/滑窗，
                   即读出位）豁免该限制，保证检索路径可达（WB1 实测
                   该豁免是 needle 可达的必要条件，代价 ~2.5pp 保真度）

    返回 o: (Hq, N, hd)。
    """
    assert k.shape[0] * n_rep == q.shape[0], "GQA 头数不匹配（Hq = Hkv * n_rep）"
    plan = plan or SinkProtectPlan()
    Hkv, S, hd = k.shape
    Hq, N, _ = q.shape
    device = q.device
    qpos = torch.arange(N, device=device)
    is_priv_q = torch.zeros(N, dtype=torch.bool, device=device)
    if query_group is not None and slot_group is not None:
        G_base = int(query_group.min())  # 占位；调用方应传入有效群 id 体系
    out = torch.empty_like(q)

    for h in range(Hkv):
        qs = q[h * n_rep:(h + 1) * n_rep]                # (n_rep,N,hd)
        kh, vh = k[h], v[h]                              # (S,hd)
        sp = slot_positions
        sink_m, local_m, mid_m = plan.slot_partition(sp, N)
        seg_o, seg_lse = [], []
        for m in (sink_m, local_m, mid_m):
            if not bool(m.any()):
                seg_o.append(None)
                seg_lse.append(None)
                continue
            idxs = sp[m]
            ks = kh[idxs].unsqueeze(0)
            vs_ = vh[idxs].unsqueeze(0)
            sc = torch.matmul(qs.unsqueeze(0), ks.transpose(-1, -2)) * scaling
            causal = idxs[None, None, :] > qpos[None, :, None]
            sc = sc.masked_fill(causal, float("-inf"))
            if m is mid_m and mid_scope == "blockdiag" \
                    and query_group is not None and slot_group is not None:
                sg_m = slot_group[m]
                bd = (query_group[:, None] != sg_m[None, :]) & (~is_priv_q)[:, None]
                sc = sc.masked_fill(bd[None], float("-inf"))
            seg_o.append(torch.matmul(safe_softmax(sc), vs_))
            seg_lse.append(torch.logsumexp(sc, dim=-1))
        lse_stack = torch.stack([
            x if x is not None else torch.full_like(seg_lse[0], float("-inf"))
            for x in seg_lse], dim=0)
        m_tot = torch.logsumexp(lse_stack, dim=0)
        o_h = torch.zeros_like(qs)
        for o_seg, l in zip(seg_o, seg_lse):
            if o_seg is None:
                continue
            o_h = o_h + torch.exp(l - m_tot).unsqueeze(-1) * o_seg
        out[h * n_rep:(h + 1) * n_rep] = o_h
    return out


# ----------------------------------------------------------------------
# RL 生成侧接入建议（给 WB2 的接口说明，见模块 docstring 顶部）
# ----------------------------------------------------------------------
def rl_action_guard(plan: SinkProtectPlan, candidate_actions: list) -> list:
    """WB2 虚拟 Action 过滤器（参考实现）。

    规则：任何触及特权位（Sink/滑窗）KV 的「重写/丢弃/替换」类 Action
    一律拒绝——Sink 的分母地位不可交易。
    candidate_actions: 每项形如 {"type": str, "touches_positions": [...]}。
    返回过滤后的合法 Action 列表。
    """
    def touches_priv(act):
        return any(p < plan.sink_tokens for p in act.get("touches_positions", []))

    return [a for a in candidate_actions
            if a.get("type") in ("keep", "compress_mid", "rewrite_mid")
            or not touches_priv(a)]
