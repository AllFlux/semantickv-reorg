# -*- coding: utf-8 -*-
# ============================================================================
#  🔒 封存存档（SEALED · 待 WB2 新 MLP 权重到达后启用）
# ----------------------------------------------------------------------------
#  DS-20261019-026-WB1 正式裁定：WB2 此前交付的 RelationModelV2 无法直接作为
#  SemanticKV 的 per-query 路由；DS 已让 WB2 按严格 MLP 规格重训 SemanticRouter
#  并推送。本模块（RelationModelV2 适配器）的任务——「证明旧权重可消费」——已完成，
#  但旧权重不再进入路由链路，故按 DS 指令「暂时封存」：代码保留、加此横幅，
#  **不要删除**。待 WB2 新 MLP 权重到达后，本文件可被新适配器取代或复用其测试骨架。
#  封存判定：H-DS24 → blocked（ds_admitted_refutation=true）。
# ============================================================================
"""wb2_router —— WB2 真实交付权重（RelationModelV2）的 WB1 侧加载/运行适配器。

背景（DS-20261019-024-WB1 收尾 · H-DS24 真实权重验收）：
  WB2 在 DS-20261019-023 经 REST 备选通道推送了 src/shared/router_weights.pt。
  实测该 .pt 的架构是 RelationModelV2（readout="attention"），**并非** DS 规格的
  SemanticRouter MLP（Linear(q_dim)→ReLU→Linear(num_groups)）。

  两套接口解决的子问题不同，不能互相替换：
    · DS 规格 SemanticRouter : query_embeds (B,q_dim) ─► (B,num_groups) 组 logits（查询→KV 组路由）
    · WB2 RelationModelV2    : (h_theta_seq, h_phi_seq, feats) ─► (delta_hat, score)
                                 （学习器程序 + 生成器编码 + 标量特征 ─► 训练价值打分）

  本模块只负责「把 WB2 的 .pt 在 WB1 侧原样加载并跑通前向」，是 H-DS24 真实权重
  consumability 的诚实证明；**并不**把它伪装成 SemanticKV 的 per-query 路由层——
  那一层集成需要 WB1 与 WB2/DS 就契约达成一致（见 results/hypothesis_ledger.yaml
  H-DS24 的 real_weights 状态）。

score_sign 语义（务必注意双向陷阱）：
  WB2 blob 的 score_sign == -1 含义是「head_score 末层权重已取负，交付态即
  high score = 高训练价值」，即**消费方无需再取反**。这与 SemanticRouter.load_external_router
  的 score_sign==-1（「消费方对分数取负后 argmax」）**恰好相反**。因此本适配器
  直接返回交付态 score，任何地方都**不要再乘 -1**。
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import torch
import torch.nn as nn


class RelationModelV2(nn.Module):
    """RelationModelV2(readout="attention") 的最小复刻。

    逐层与 WB2 harness_v2.RelationModelV2 对应，参数由 .pt 内 arch 元数据驱动。
    与 WB2 src/shared/load_test.py 的 RouterShell 保持数值等价（tests/test_wb2_load.py
    会交叉验证）。
    """

    def __init__(self, arch: dict):
        super().__init__()
        if arch.get("readout_mode") != "attention":
            raise ValueError(
                f"本适配器只复刻 attention 读出，收到 readout_mode={arch.get('readout_mode')!r}")
        d_theta, h_phi, feat_dim = arch["d_theta"], arch["h_phi"], arch["feat_dim"]
        d_model, nhead, nlayers = arch["d_model"], arch["nhead"], arch["nlayers"]
        self.n_query = 4  # readout="attention" 默认 K（harness_v2.py:339）

        self.theta_proj = nn.Linear(d_theta, d_model)
        self.phi_proj = nn.Linear(h_phi, d_model)
        self.feat_proj = nn.Linear(feat_dim, d_model)
        enc_layer = nn.TransformerEncoderLayer(
            d_model, nhead, dim_feedforward=d_model * 2,
            dropout=0.0, batch_first=True, norm_first=True)
        self.readout = nn.TransformerEncoder(enc_layer, nlayers)
        self.query = nn.Parameter(torch.randn(self.n_query, d_model) * 0.02)
        self.readout_attn = nn.MultiheadAttention(
            d_model, num_heads=nhead, batch_first=True)
        head_in = d_model * self.n_query
        self.head_delta = nn.Sequential(
            nn.Linear(head_in, d_model), nn.GELU(), nn.Linear(d_model, 1))
        self.head_score = nn.Sequential(
            nn.Linear(head_in, d_model), nn.GELU(), nn.Linear(d_model, 1))

    def forward(self, h_theta_seq, h_phi_seq, feats):
        """前向（与 harness_v2.py:381-407 一致）。

        h_theta_seq : (B, T, d_theta)  学习器全序列隐状态
        h_phi_seq   : (B, L, h_phi)    生成器全序列隐状态
        feats       : (B, feat_dim)    六维标量特征
        返回 (delta_hat (B,), score (B,))。score 已是交付态（high=高训练价值）。
        """
        B = h_theta_seq.shape[0]
        z = self.theta_proj(h_theta_seq)
        z = z + self.phi_proj(h_phi_seq).mean(dim=1, keepdim=True)
        z = z + self.feat_proj(feats).unsqueeze(1)
        z = self.readout(z)
        q = self.query.unsqueeze(0).expand(B, -1, -1)
        summary, _ = self.readout_attn(q, z, z)
        pooled = summary.reshape(B, -1)
        return (self.head_delta(pooled).squeeze(-1),
                self.head_score(pooled).squeeze(-1))


class WB2Router:
    """WB2 RelationModelV2 权重的薄封装。

    只暴露「加载 + 前向 + 取 score」；不假装它是 SemanticKV 的 per-query 路由层。
    """

    def __init__(self, model: RelationModelV2, *, arch: dict,
                 score_sign=None, regime=None, score_stats=None,
                 source_path: str = ""):
        self.model = model
        self.arch = arch
        self.score_sign = score_sign          # WB2: -1 == 已取反，消费方直接用
        self.regime = regime
        self.score_stats = score_stats
        self.source_path = source_path
        self.model.eval()

    @classmethod
    def from_pt(cls, path: str, map_location: str = "cpu") -> "WB2Router":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"WB2 Router 权重不存在: {p}")
        blob = torch.load(p, map_location=map_location, weights_only=False)
        arch = blob.get("arch")
        if not isinstance(arch, dict) or arch.get("readout_mode") != "attention":
            raise ValueError(
                f"{path} 不是 WB2 RelationModelV2 blob（缺少 arch.readout_mode='attention'）。"
                f"若是 DS 规格 SemanticRouter 权重，请改用 SemanticRouter.load_external_router。")
        model = RelationModelV2(arch)
        missing, unexpected = model.load_state_dict(blob["state_dict"], strict=True)
        if missing or unexpected:
            raise RuntimeError(
                f"WB2 Router state_dict 与重建架构不符："
                f"missing={missing} unexpected={unexpected}")
        return cls(
            model, arch=arch,
            score_sign=blob.get("score_sign"),
            regime=blob.get("regime"),
            score_stats=blob.get("score_stats"),
            source_path=str(p))

    def forward(self, h_theta_seq, h_phi_seq, feats):
        with torch.no_grad():
            return self.model(h_theta_seq, h_phi_seq, feats)

    def score(self, h_theta_seq, h_phi_seq, feats):
        """返回交付态 score (B,)——high=高训练价值，**请勿再取反**。

        score_sign==-1（WB2 默认）即表示权重已取负；即便某次 WB2 改导未取反的
        权重（score_sign==+1），也由调用方显式处理，本方法恒定返回「交付态」。
        """
        _, s = self.forward(h_theta_seq, h_phi_seq, feats)
        return s

    def latency_ms(self, ht, hp, ft, n: int = 1000) -> float:
        import time
        with torch.no_grad():
            for _ in range(20):
                self.model(ht, hp, ft)
            ts = []
            for _ in range(n):
                t0 = time.perf_counter()
                self.model(ht, hp, ft)
                ts.append((time.perf_counter() - t0) * 1000.0)
        ts.sort()
        return ts[len(ts) // 2]

    def summary(self) -> dict:
        return {
            "source": self.source_path,
            "arch": self.arch,
            "regime": self.regime,
            "score_sign": self.score_sign,
            "score_stats": self.score_stats,
            "params": sum(p.numel() for p in self.model.parameters()),
        }


def is_wb2_blob(blob: dict) -> bool:
    """快速判定一个 torch.load 出的对象是否为 WB2 RelationModelV2 blob。"""
    return (isinstance(blob, dict) and isinstance(blob.get("arch"), dict)
            and blob["arch"].get("readout_mode") == "attention")


def load_wb2_router(path: str, map_location: str = "cpu") -> WB2Router:
    return WB2Router.from_pt(path, map_location=map_location)


if __name__ == "__main__":
    import sys
    pt = sys.argv[1] if len(sys.argv) > 1 else "src/shared/router_weights.pt"
    r = WB2Router.from_pt(pt)
    print(json.dumps(r.summary(), ensure_ascii=False, indent=2))
    g = torch.Generator().manual_seed(0)
    B, T, L = 4, 16, 8
    a = r.arch
    ht = torch.randn(B, T, a["d_theta"], generator=g)
    hp = torch.randn(B, L, a["h_phi"], generator=g)
    ft = torch.randn(B, a["feat_dim"], generator=g)
    delta, score = r.forward(ht, hp, ft)
    print(f"delta_hat{delta.shape} score{score.shape}")
    print(f"score[:3]={score[:3].tolist()}")
    print(f"latency batch1 ≈ {r.latency_ms(ht, hp, ft):.3f} ms")
