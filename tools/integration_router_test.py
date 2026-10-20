#!/usr/bin/env python3
"""integration_router_test.py —— 端到端集成测试（DS-20261020-052-WB1 §三 授权）。

DS-052 §三：收到 WB2 最终 Router 权重后，跑一次端到端集成测试，确认路由挂载后
Argmax 依然及格；确认无误后正式归档。

本脚本做三件事：
  1. 结构/加载/延迟校验：加载 src/shared/semantic_router.pt，验证 arch、前向契约、CPU 延迟。
  2. 出厂 router 拟合一致性：把 real_features_3b.npz 的真实隐状态喂进出厂 router，
     预测群 vs GSC 真值标签算 Top-1（证明该 .pt 是在真实特征上训出的、非占位）。
  3. 泛化 Top-1（H-DS37 诚实证词）：对真实特征做 5 折交叉验证，每折新训一个同构
     MLP(2048→512→42)，在留出折上测 Top-1 —— 避免「在训练集上自测」的高估。

判定（H-DS37）：泛化 Top-1 显著高于随机线（42 群均匀随机 ≈ 2.38%；DS 口径 64 群
1.56%）⟹ 3B 口径成立，H-DS37 可 confirmed；否则 pending / 回 7B。

输出：results/router_integration_3b.json（含 environment 块，符合 D3）。

用法：python3 tools/integration_router_test.py
退出码：0 = 集成测试通过（Top-1 显著高于随机线）；4 = 未达标。
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

ROOT = Path(__file__).resolve().parent.parent
NPZ = ROOT / "src" / "shared" / "real_features_3b.npz"
PT = ROOT / "src" / "shared" / "semantic_router.pt"
OUT = ROOT / "results" / "router_integration_3b.json"
MODEL = "/workspace/models/Qwen2.5-3B-Instruct"
RANDOM_42 = 1.0 / 42        # 42 群均匀随机基线
RANDOM_64_LINE = 0.0156     # DS 口径 64 群随机线（H-DS37 原文）


class Router(nn.Module):
    """与 .pt 内嵌 arch 对应的 MLP；用 self.net 承载，使 state_dict 键为 net.0.weight/net.2.weight。"""

    def __init__(self, arch: dict):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(arch["in_dim"], arch["hidden"]),
            nn.ReLU(),
            nn.Linear(arch["hidden"], arch["out"]),
        )

    def forward(self, x):
        return self.net(x)


def build_mlp(arch: dict) -> nn.Module:
    return Router(arch)


def load_shipped_router():
    payload = torch.load(PT, map_location="cpu", weights_only=False)
    arch = payload["arch"]
    sd = {k: v.float() for k, v in payload["state_dict"].items()}  # fp16→fp32
    model = build_mlp(arch)
    model.load_state_dict(sd, strict=True)
    model.eval()
    return payload, arch, model


def top1(model, X: torch.Tensor, y: torch.Tensor) -> float:
    with torch.no_grad():
        pred = model(X).argmax(dim=1)
    return float((pred == y).float().mean().item())


def kfold_cv(X: np.ndarray, y: np.ndarray, arch: dict, k: int = 5,
             epochs: int = 60, lr: float = 1e-3, seed: int = 0):
    """5 折交叉验证：每折新训同构 MLP，在留出折测 Top-1。返回每折 acc 与均值。"""
    rng = np.random.default_rng(seed)
    n = len(y)
    idx = rng.permutation(n)
    folds = np.array_split(idx, k)
    accs = []
    for i in range(k):
        te = folds[i]
        tr = np.concatenate([folds[j] for j in range(k) if j != i])
        torch.manual_seed(seed + i)
        model = build_mlp(arch)
        opt = torch.optim.Adam(model.parameters(), lr=lr)
        lossf = nn.CrossEntropyLoss()
        Xtr = torch.from_numpy(X[tr])
        ytr = torch.from_numpy(y[tr].astype(np.int64))
        model.train()
        for _ in range(epochs):
            opt.zero_grad()
            loss = lossf(model(Xtr), ytr)
            loss.backward()
            opt.step()
        model.eval()
        accs.append(top1(model, torch.from_numpy(X[te]),
                         torch.from_numpy(y[te].astype(np.int64))))
    return accs, float(np.mean(accs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=60)
    args = ap.parse_args()

    print("=" * 78)
    print("integration_router_test.py —— 端到端集成测试（DS-052 §三）")
    print("=" * 78)

    # 载入真实特征
    with np.load(NPZ) as d:
        hs = d["hidden_states"].astype(np.float32)   # (2586,2048) fp16→fp32
        labels = d["labels"].astype(np.int64)         # (2586,)
    n, dim = hs.shape
    n_groups = int(labels.max()) + 1
    print(f"[features] {NPZ.name}: hidden_states={hs.shape} labels={labels.shape} "
          f"n_groups={n_groups}")
    X = torch.from_numpy(hs)
    y = torch.from_numpy(labels)

    checks = []

    # ── 检查 1：结构 / 加载 / 前向契约 / CPU 延迟 ──
    t0 = time.time()
    payload, arch, model = load_shipped_router()
    n_params = sum(p.numel() for p in model.parameters())
    checks.append(("torch.load + state_dict strict 加载（fp16→fp32）", True,
                   f"{time.time()-t0:.3f}s；参数量 {n_params}；arch={json.dumps(arch, ensure_ascii=False)}"))
    w0 = model.net[0].weight
    checks.append((f"net.0.weight 形状={list(w0.shape)}，输入维={arch['in_dim']}",
                   arch["in_dim"] == dim and arch["out"] == n_groups,
                   f"期望输入维={dim}（真实特征）、输出={n_groups}（真实群数）"))
    with torch.no_grad():
        ylog = model(X[:8])
    checks.append((f"前向 ({n},{dim})→(·,{arch['out']}) 且有限",
                   bool(torch.isfinite(ylog).all()),
                   f"y[:4]={ylog[0][:4].tolist()}"))

    # ── 检查 2：出厂 router 拟合一致性（Top-1 vs GSC 真值）──
    shipped_top1 = top1(model, X, y)
    checks.append((f"出厂 router Top-1 vs GSC 真值 = {shipped_top1*100:.2f}%",
                   shipped_top1 > 0.5,
                   f"随机线 {RANDOM_42*100:.2f}%（42 群）/ {RANDOM_64_LINE*100:.2f}%（DS 64 群口径）"))

    # ── 检查 3：泛化 Top-1（5 折 CV 新训同构 MLP，H-DS37 诚实证词）──
    print(f"[cv] 5 折交叉验证（每折新训 2048→512→{n_groups}，epochs={args.epochs}）...")
    cv_accs, cv_mean = kfold_cv(hs, labels, arch, k=5, epochs=args.epochs)
    cv_std = float(np.std(cv_accs))
    majority = float(np.bincount(labels).max()) / n
    checks.append((f"泛化 Top-1（5 折 CV）= {cv_mean*100:.2f}% ± {cv_std*100:.2f}%",
                   cv_mean > max(RANDOM_42, RANDOM_64_LINE) * 3,
                   f"各折={[f'{a*100:.1f}%' for a in cv_accs]}；"
                   f"随机线 {RANDOM_42*100:.2f}%/1.56%；majority 基线 {majority*100:.2f}%"))

    # ── 汇总 ──
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= bool(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"         {detail}")

    # ── 判定 H-DS37 ──
    sig = cv_mean > max(RANDOM_42, RANDOM_64_LINE) * 3 and shipped_top1 > 0.5
    if sig:
        h_ds37 = "confirmed"
        verdict = "INTEGRATION_PASS"
    elif cv_mean > max(RANDOM_42, RANDOM_64_LINE):
        h_ds37 = "pending"
        verdict = "INTEGRATION_WEAK"
    else:
        h_ds37 = "pending"
        verdict = "INTEGRATION_FAIL"

    print("-" * 78)
    print(f"  出厂 router Top-1 : {shipped_top1*100:.2f}%")
    print(f"  泛化 Top-1 (5CV)  : {cv_mean*100:.2f}% ± {cv_std*100:.2f}%")
    print(f"  majority 基线     : {majority*100:.2f}%")
    print(f"  H-DS37 判定       : {h_ds37}（泛化 Top-1 {'显著' if sig else '未'}高于随机线）")
    print(f"⟹ 总体：{'全部通过 ✅' if all_ok else '存在失败 ❌'}   verdict={verdict}")

    # ── 写 results（D3 环境标签）──
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = {
        "purpose": "DS-20261020-052 §三 端到端集成测试：路由挂载真实 3B 特征后 Top-1/Argmax 判定",
        "created_at": ts,
        "model": MODEL,
        "environment": {
            "kind": "local-sandbox-cpu-3b",
            "model": MODEL,
            "device": "cpu",
            "dtype": "float32",
            "load_in_4bit": False,
            "note": "真实 3B 隐状态 + 真实数据微调 router；设备 CPU 是唯一与实机差异。",
        },
        "config": {
            "npz": str(NPZ.relative_to(ROOT)),
            "router_pt": str(PT.relative_to(ROOT)),
            "router_arch": arch,
            "router_data_provenance": payload.get("data_provenance", {}),
            "cv_folds": 5,
            "cv_epochs": args.epochs,
            "features_shape": [n, dim],
            "n_groups": n_groups,
            "model": MODEL,
            "device": "cpu",
            "dtype": "float32",
            "load_in_4bit": False,
        },
        "results": {
            "shipped_router_top1": shipped_top1,
            "generalization_top1_mean": cv_mean,
            "generalization_top1_std": cv_std,
            "generalization_top1_folds": cv_accs,
            "majority_baseline": majority,
            "random_baseline_42": RANDOM_42,
            "random_line_64_hds37": RANDOM_64_LINE,
            "router_n_params": n_params,
        },
        "verdict": verdict,
        "h_ds37": h_ds37,
        "h_ds37_criterion": ("泛化 Top-1 显著高于随机线（42 群 2.38% / DS 64 群 1.56%）"
                             "且出厂 router Top-1 > 50% ⟹ 3B 口径成立"),
        "note": ("出厂 router 由 WB2 在 real_features_3b.npz 上真实微调"
                 "（proxy_data=false, proxy_mode=real_wb1_npz）；"
                 "泛化 Top-1 用 5 折 CV 新训同构 MLP 估计，避免训练集自测高估。"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] 结果写入 {OUT.relative_to(ROOT)}")
    return 0 if all_ok else 4


if __name__ == "__main__":
    import sys
    sys.exit(main())
