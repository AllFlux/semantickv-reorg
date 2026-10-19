#!/usr/bin/env python3
"""semantic_router_loadtest.py —— semantic_router.pt 纯 CPU 空壳加载证明（DS-20261019-025-WB2 · H-DS33）。

验收标准（DS 原文）：在 src/shared/ 中出现 .pt 文件，且提供 load_test 证明纯 CPU 环境可加载；
falsifiable_criterion：推理延迟 > 1ms 或 Top-1 < GSC 纯数学聚类准确率 ⟹ 废弃 Router。

设计约束（仿 router_weights 的 load_test.py 范式）：
  1. 零 WB2 依赖——不 import 本项目任何模块；架构按 .pt 内嵌 arch 元数据内联重建。
  2. 纯 CPU——map_location="cpu"，无 GPU 代码路径。
  3. 可复现——torch.set_num_threads(1)（REP-1），延迟报中位数。

五项检查：torch.load / state_dict strict / 前向契约 / CPU 延迟 / 数据代理元数据声明。

⚠️ 数据真实性：本 .pt 是【代理训练件】（合成 dim=1024 向量 + 离线 K-means 64 群标签，
非真实 LLM 隐状态 / 真实 WB1-GSC）。本脚本验证的是【结构/接口合规】（可加载、可前向、
延迟合规），不代表真实语义路由质量；与真实 GSC 的 Top-1 对比待 WB1 真实环境验证。

用法：python3 semantic_router_loadtest.py [--pt PATH] [--n N]
退出码：0 = 全部检查通过；1 = 有检查失败。
"""
import argparse
import json
import time
from pathlib import Path

import torch
import torch.nn as nn

torch.set_num_threads(1)  # REP-1

HERE = Path(__file__).parent
DEFAULT_PT = HERE / "semantic_router.pt"
DS_LATENCY_FAIL_MS = 1.0      # DS falsifiable_criterion：>1ms ⟹ 接口协议失败


# ─────────────────── 内联架构重建（与 train_semantic_router.py 的 SemanticRouter 对应） ───────────────────
class SemanticRouterShell(nn.Module):
    """DS 严格规格：Linear(in_dim, hidden) -> ReLU -> Linear(hidden, out)。纯 torch，零自定义算子。"""

    def __init__(self, arch: dict):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(arch["in_dim"], arch["hidden"]),
            nn.ReLU(),
            nn.Linear(arch["hidden"], arch["out"]),
        )

    def forward(self, x):
        return self.net(x)


def latency_ms(model, dim: int, n: int = 1000):
    """纯 CPU 前向延迟（n 次取中位数，毫秒）。"""
    model.eval()
    with torch.no_grad():
        for _ in range(20):
            model(torch.randn(1, dim))
        ts = []
        for _ in range(n):
            x = torch.randn(1, dim)
            t0 = time.perf_counter()
            model(x)
            ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return ts[len(ts) // 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pt", default=str(DEFAULT_PT))
    ap.add_argument("--n", type=int, default=1000)
    args = ap.parse_args()

    print("=" * 78)
    print("semantic_router_loadtest.py —— semantic_router.pt 纯 CPU 空壳加载证明（H-DS33）")
    print("=" * 78)

    checks = []

    # ── 检查 1：纯 torch.load ──
    t0 = time.time()
    try:
        payload = torch.load(args.pt, map_location="cpu")
        checks.append(("torch.load(map_location='cpu') 成功", True,
                       f"{time.time()-t0:.3f}s，keys={sorted(payload.keys())}"))
    except Exception as e:  # noqa: BLE001
        checks.append(("torch.load(map_location='cpu') 成功", False, repr(e)))
        payload = None

    model = None
    if payload is not None:
        # ── 检查 2：state_dict strict ──
        try:
            model = SemanticRouterShell(payload["arch"])
            missing, unexpected = model.load_state_dict(payload["state_dict"], strict=True)
            n_params = sum(p.numel() for p in model.parameters())
            checks.append(("state_dict strict 加载（0 missing / 0 unexpected）", True,
                           f"missing={list(missing)} unexpected={list(unexpected)}；"
                           f"参数量 {n_params}；arch={json.dumps(payload['arch'], ensure_ascii=False)}"))
        except Exception as e:  # noqa: BLE001
            checks.append(("state_dict strict 加载", False, repr(e)))

        # ── 检查 3：前向契约（B, in_dim）→（B, out）──
        if model is not None:
            try:
                B = 4
                x = torch.randn(B, payload["arch"]["in_dim"])
                model.eval()
                with torch.no_grad():
                    y = model(x)
                ok = (y.shape == (B, payload["arch"]["out"]) and torch.isfinite(y).all())
                checks.append((f"前向：({B}, {payload['arch']['in_dim']}) → ({B}, {payload['arch']['out']}) 且有限",
                               bool(ok), f"y{tuple(y.shape)}；y[0][:4]={y[0][:4].tolist()}"))
            except Exception as e:  # noqa: BLE001
                checks.append(("前向契约", False, repr(e)))

        # ── 检查 4：CPU 延迟（DS 否证线 1ms）──
        if model is not None:
            try:
                b1 = latency_ms(model, payload["arch"]["in_dim"], n=args.n)
                verdict = "PASS（<否证线 1ms）" if b1 < DS_LATENCY_FAIL_MS else "FAIL（>1ms）"
                checks.append((f"CPU 延迟 batch1={b1:.4f}ms ⟹ {verdict}", b1 < DS_LATENCY_FAIL_MS,
                               f"否证线 {DS_LATENCY_FAIL_MS}ms"))
            except Exception as e:  # noqa: BLE001
                checks.append(("CPU 延迟", False, repr(e)))

        # ── 检查 5：数据代理元数据声明（诚信项）──
        dp = payload.get("data_provenance", {})
        checks.append(("数据代理声明（proxy_data 元数据存在且 True）", bool(dp.get("proxy_data") is True),
                       f"proxy_data={dp.get('proxy_data')}；hidden_state_source={dp.get('hidden_state_source')}；"
                       f"real_hidden_state_available={dp.get('real_hidden_state_available')}；"
                       f"label_source={dp.get('label_source')}"))

    # ── 汇总 ──
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= bool(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"         {detail}")
    print("-" * 78)
    if payload is not None:
        print(f"  arch        : {json.dumps(payload['arch'], ensure_ascii=False)}")
        print(f"  num_groups  : {payload.get('num_groups')}（DS 规格输出 64 个语义群 logits）")
        print(f"  input_dim   : {payload.get('input_dim')}（DS 规格 Query 隐状态维度）")
        dp = payload.get("data_provenance", {})
        print(f"  ⚠️ 数据代理   : proxy_data={dp.get('proxy_data')}；真实 hidden_state/GSC 不可得"
              f"（本沙盒无 GPU/真实 LLM）→ 此为代理训练件，真实验收待 WB1")
    print(f"  延迟（中位数, {args.n} 次）见上")
    print("-" * 78)
    print(f"⟹ 总体：{'全部通过 ✅' if all_ok else '存在失败 ❌'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
