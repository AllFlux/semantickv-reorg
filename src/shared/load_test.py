#!/usr/bin/env python3
"""load_test.py —— router_weights.pt 纯 CPU 空壳加载证明（DS-20261019-015-WB2 · H-DS24）。

验收标准（DS 原文）：
  - 「在 src/shared/ 中出现 .pt 文件，且提供 load_test.py 证明纯 CPU 环境可加载」
  - falsifiable_criterion：「若导出的 .pt 文件依赖了 WB2 特有的自定义包导致 WB1 无法
    torch.load，或推理延迟 > 1ms，则接口协议失败，需降级为纯数学的 GSC 质心检索」

本脚本的设计约束：
  1. **零 WB2 依赖**——不 import harness_v2 或本项目任何模块；架构按 .pt 内嵌的
     `arch` 元数据内联重建（与 RelationModelV2 readout="attention" 逐层对应）。
  2. **纯 CPU**——`map_location="cpu"`，无 GPU 代码路径。
  3. **可复现**——`torch.set_num_threads(1)`（REP-1 条款），延迟报中位数。

五项检查：torch.load / state_dict strict / score_sign 元数据 / 前向契约 / CPU 延迟。

用法：python3 load_test.py [--pt PATH] [--n N]
退出码：0 = 全部检查通过；1 = 有检查失败。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import torch
import torch.nn as nn

torch.set_num_threads(1)  # REP-1：显式固定线程数，延迟读数跨进程可比

HERE = Path(__file__).parent
DEFAULT_PT = HERE / "router_weights.pt"
DS_LATENCY_FAIL_MS = 1.0      # DS falsifiable_criterion：>1ms ⟹ 接口协议失败
DS_LATENCY_TARGET_MS = 0.5    # DS prediction：路由延迟 < 0.5ms（目标值，非否证线）


# ─────────────────── 内联架构重建（与 harness_v2.RelationModelV2 对应） ───────────────────
class RouterShell(nn.Module):
    """RelationModelV2(readout="attention") 的最小空壳复刻。

    逐层对应关系（以 .pt 内 arch 元数据为参数源）：
      theta_proj / phi_proj / feat_proj : Linear(→d_model)
      readout      : TransformerEncoder(nlayers 层, nhead 头, FF=2·d_model,
                                       norm_first=True, batch_first=True)
      query        : Parameter (n_query=4, d_model)   ← readout="attention"
      readout_attn : MultiheadAttention(d_model, nhead, batch_first=True)
      head_delta / head_score : Linear(K·D→D) → GELU → Linear(D→1)
    forward 与 harness_v2.py:381-407 完全一致。
    """

    def __init__(self, arch: dict):
        super().__init__()
        assert arch["readout_mode"] == "attention", \
            f"本空壳只复刻 attention 读出，收到 {arch['readout_mode']!r}"
        d_theta, h_phi, feat_dim = arch["d_theta"], arch["h_phi"], arch["feat_dim"]
        d_model, nhead, nlayers = arch["d_model"], arch["nhead"], arch["nlayers"]
        n_query = 4  # readout="attention" 的默认 K（harness_v2.py:339）

        self.theta_proj = nn.Linear(d_theta, d_model)
        self.phi_proj = nn.Linear(h_phi, d_model)
        self.feat_proj = nn.Linear(feat_dim, d_model)
        enc_layer = nn.TransformerEncoderLayer(
            d_model, nhead, dim_feedforward=d_model * 2,
            dropout=0.0, batch_first=True, norm_first=True)
        self.readout = nn.TransformerEncoder(enc_layer, nlayers)
        self.query = nn.Parameter(torch.randn(n_query, d_model) * 0.02)
        self.readout_attn = nn.MultiheadAttention(d_model, num_heads=nhead, batch_first=True)
        head_in = d_model * n_query
        self.head_delta = nn.Sequential(nn.Linear(head_in, d_model), nn.GELU(), nn.Linear(d_model, 1))
        self.head_score = nn.Sequential(nn.Linear(head_in, d_model), nn.GELU(), nn.Linear(d_model, 1))

    def forward(self, h_theta_seq, h_phi_seq, feats):
        B, T, _ = h_theta_seq.shape
        z = self.theta_proj(h_theta_seq)
        z = z + self.phi_proj(h_phi_seq).mean(dim=1, keepdim=True)
        z = z + self.feat_proj(feats).unsqueeze(1)
        z = self.readout(z)
        q = self.query.unsqueeze(0).expand(B, -1, -1)
        summary, _ = self.readout_attn(q, z, z)
        pooled = summary.reshape(B, -1)
        return (self.head_delta(pooled).squeeze(-1),
                self.head_score(pooled).squeeze(-1))


def latency_ms(model, ht, hp, ft, n=1000):
    """纯 CPU 前向延迟（n 次取中位数，毫秒）。"""
    with torch.no_grad():
        for _ in range(20):                       # 预热（JIT/缓存）
            model(ht, hp, ft)
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            model(ht, hp, ft)
            ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return ts[len(ts) // 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pt", default=str(DEFAULT_PT))
    ap.add_argument("--n", type=int, default=1000, help="延迟计时迭代次数")
    args = ap.parse_args()

    print("=" * 78)
    print("load_test.py —— router_weights.pt 纯 CPU 空壳加载证明（H-DS24）")
    print("=" * 78)

    checks = []

    # ── 检查 1：纯 torch.load（无自定义类、无 harness 导入）──
    t0 = time.time()
    try:
        payload = torch.load(args.pt, map_location="cpu")
        checks.append(("torch.load(map_location='cpu') 成功", True,
                       f"{time.time()-t0:.3f}s，keys={sorted(payload.keys())}"))
    except Exception as e:  # noqa: BLE001
        checks.append(("torch.load(map_location='cpu') 成功", False, repr(e)))
        payload = None

    # ── 检查 2：state_dict 完整性与架构重建 ──
    model = None
    if payload is not None:
        try:
            model = RouterShell(payload["arch"])
            missing, unexpected = model.load_state_dict(payload["state_dict"], strict=True)
            n_params = sum(p.numel() for p in model.parameters())
            checks.append(("state_dict strict 加载（0 missing / 0 unexpected）", True,
                           f"missing={list(missing)} unexpected={list(unexpected)}；"
                           f"参数量 {n_params}"))
        except Exception as e:  # noqa: BLE001
            checks.append(("state_dict strict 加载", False, repr(e)))

    # ── 检查 2.5：元数据完整性（score_sign 约定，DS-017 执行指示 3）──
    if payload is not None:
        try:
            sign = payload.get("score_sign")
            ok = (sign == -1)
            checks.append(("元数据 score_sign=-1（高分=高训练价值约定）", ok,
                           f"score_sign={sign}；regime={payload.get('regime')}；"
                           f"note={payload.get('score_sign_note', '（无）')[:60]}"))
        except Exception as e:  # noqa: BLE001
            checks.append(("元数据 score_sign", False, repr(e)))

    # ── 检查 3：合成输入前向（契约形状）──
    if model is not None:
        try:
            B, T, L = 4, 16, 8
            g = torch.Generator().manual_seed(0)
            ht = torch.randn(B, T, payload["arch"]["d_theta"], generator=g)
            hp = torch.randn(B, L, payload["arch"]["h_phi"], generator=g)
            ft = torch.randn(B, payload["arch"]["feat_dim"], generator=g)
            model.eval()
            with torch.no_grad():
                delta_hat, score = model(ht, hp, ft)
            ok = (delta_hat.shape == (B,) and score.shape == (B,)
                  and torch.isfinite(delta_hat).all() and torch.isfinite(score).all())
            checks.append(("前向：输出 (delta_hat (B,), score (B,)) 且全部有限", bool(ok),
                           f"delta_hat{tuple(delta_hat.shape)} score{tuple(score.shape)}；"
                           f"score[:3]={score[:3].tolist()}"))
        except Exception as e:  # noqa: BLE001
            checks.append(("前向", False, repr(e)))

    # ── 检查 4：CPU 推理延迟（DS 判据：>1ms ⟹ 失败；预测目标 <0.5ms）──
    lat = {}
    if model is not None:
        try:
            for bs in (1, 64):
                ht = torch.randn(bs, 16, payload["arch"]["d_theta"])
                hp = torch.randn(bs, 8, payload["arch"]["h_phi"])
                ft = torch.randn(bs, payload["arch"]["feat_dim"])
                lat[f"batch{bs}_ms"] = round(latency_ms(model, ht, hp, ft, n=args.n), 4)
            b1 = lat["batch1_ms"]
            verdict = ("PASS（<否证线 1ms）" if b1 < DS_LATENCY_FAIL_MS else "FAIL（>1ms）")
            target = ("达到预测目标 <0.5ms" if b1 < DS_LATENCY_TARGET_MS
                      else "未达预测目标 0.5ms，但在否证线内")
            checks.append((f"CPU 延迟 batch1={b1}ms ⟹ {verdict}", b1 < DS_LATENCY_FAIL_MS,
                           f"{target}；batch64={lat['batch64_ms']}ms"))
        except Exception as e:  # noqa: BLE001
            checks.append(("CPU 延迟", False, repr(e)))

    # ── 汇总 ──
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= bool(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"         {detail}")
    print("-" * 78)
    if payload is not None:
        print(f"  arch   : {json.dumps(payload['arch'], ensure_ascii=False)}")
        print(f"  regime : {payload['regime']}")
        print(f"  score_sign : {payload.get('score_sign')}（-1=高分高训练价值；head_score 末层已取负）")
        print(f"  score_stats（训练池上实测，取反后口径）: {payload['score_stats']}")
    print(f"  延迟（中位数, {args.n} 次）: {lat}")
    print("-" * 78)
    print(f"⟹ 总体：{'全部通过 ✅' if all_ok else '存在失败 ❌'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
