# -*- coding: utf-8 -*-
"""H-DS24 真实权重验收：WB1 适配器 ↔ WB2 自带 RouterShell 交叉验证。

验收目标（DS-20261019-024-WB1 收尾）：
  1. WB1 的 wb2_router.WB2Router 能加载 WB2 推送的真实 router_weights.pt 并跑通前向；
  2. 同一组合成输入下，WB1 适配器产出的 score 与 WB2 自带 load_test.RouterShell
     数值完全一致（证明 WB1 重建架构逐层对齐，非近似）；
  3. score_sign=-1 语义：WB2 blob 已取反，适配器直接返回交付态（high=高训练价值），
     任何地方都不应再乘 -1（验证适配器无额外取反）；
  4. SemanticRouter.load_external_router 收到 WB2 blob 时给出清晰的「请用 wb2_router」指路，
     而不是一堆 missing/unexpected keys 误导排查；
  5. 延迟 < 1ms 否证线（DS falsifiable_criterion）。

结论写入 results/wb2_real_weights_test.json。
"""
import importlib.util
import json
import sys
import warnings
from pathlib import Path

import torch

HERE = Path(__file__).parent
ROOT = HERE.parent
SHARED = ROOT / "src" / "shared"
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

from vllm_plugin.wb2_router import WB2Router, is_wb2_blob  # noqa: E402
from vllm_plugin.semantic_router import SemanticRouter     # noqa: E402

torch.set_num_threads(1)


def _load_wb2_shell():
    """动态加载 WB2 自带的 RouterShell（不污染 WB1 命名空间）。"""
    spec = importlib.util.spec_from_file_location(
        "wb2_load_test", str(SHARED / "load_test.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> dict:
    pt = SHARED / "router_weights.pt"
    assert pt.exists(), f"未找到真实权重 {pt}（请先运行 auto_pull_shared.py）"

    blob = torch.load(pt, map_location="cpu", weights_only=False)
    results = {
        "file": str(pt),
        "sha256_input": None,
        "config": {"device": "cpu-synthetic",
                   "model": "WB2 router_weights.pt (RelationModelV2)",
                   "dtype": "fp32",
                   "purpose": "H-DS24 真实权重 consumability 验收（CPU 实测，非 mock）"},
        "checks": []}

    # C0: is_wb2_blob 识别
    ok0 = is_wb2_blob(blob)
    results["checks"].append(
        {"id": "C0_wb2_blob_recognized", "pass": ok0,
         "detail": f"readout_mode={blob['arch'].get('readout_mode')}"})

    # C1: WB1 适配器加载 + 前向
    r = WB2Router.from_pt(str(pt))
    g = torch.Generator().manual_seed(0)
    a = r.arch
    B, T, L = 4, 16, 8
    ht = torch.randn(B, T, a["d_theta"], generator=g)
    hp = torch.randn(B, L, a["h_phi"], generator=g)
    ft = torch.randn(B, a["feat_dim"], generator=g)
    with torch.no_grad():
        delta1, score1 = r.forward(ht, hp, ft)
    ok1 = (tuple(delta1.shape) == (B,) and tuple(score1.shape) == (B,)
           and bool(torch.isfinite(score1).all()))
    results["checks"].append(
        {"id": "C1_wb1_adapter_forward", "pass": ok1,
         "detail": f"delta{score1.shape} score{score1.shape} "
                   f"score_stats={r.score_stats}"})

    # C2: 与 WB2 RouterShell 数值交叉验证
    shell_mod = _load_wb2_shell()
    shell = shell_mod.RouterShell(blob["arch"])
    shell.load_state_dict(blob["state_dict"], strict=True)
    shell.eval()
    with torch.no_grad():
        delta2, score2 = shell(ht, hp, ft)
    max_err = float((score1 - score2).abs().max())
    ok2 = max_err < 1e-5
    results["checks"].append(
        {"id": "C2_numeric_parity_vs_wb2_shell", "pass": ok2,
         "detail": f"max|score_wb1 - score_wb2|={max_err:.2e} (阈值 1e-5)"})

    # C3: score_sign 语义——适配器返回交付态（无额外取反）
    # WB2 blob score_sign=-1 即「已取反」。若适配器错误地再乘 -1，则 score 会翻号。
    # 这里验证适配器输出 == 模型原始 head_score 输出（即未再取反）。
    with torch.no_grad():
        z = (shell.theta_proj(ht)
             + shell.phi_proj(hp).mean(dim=1, keepdim=True)
             + shell.feat_proj(ft).unsqueeze(1))
        z = shell.readout(z)
        q = shell.query.unsqueeze(0).expand(B, -1, -1)
        summary, _ = shell.readout_attn(q, z, z)
        pooled = summary.reshape(B, -1)
        raw_score = shell.head_score(pooled).squeeze(-1)
    parity_raw = float((score1 - raw_score).abs().max())
    ok3 = parity_raw < 1e-5
    results["checks"].append(
        {"id": "C3_no_extra_flip", "pass": ok3,
         "detail": f"adapter.score == raw head_score, max_err={parity_raw:.2e}; "
                   f"blob score_sign={blob.get('score_sign')}（=-1 表示已取反，消费方直接用）"})

    # C4: load_external_router 收到 WB2 blob 应给出清晰指路
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            SemanticRouter.load_external_router(str(pt))
            ok4 = False
            detail4 = "未报错（异常！应识别为 WB2 blob 并指路）"
        except RuntimeError as e:
            msg = str(e)
            ok4 = ("wb2_router" in msg) and ("RelationModelV2" in msg)
            detail4 = msg[:120]
        except Exception as e:  # noqa: BLE001
            ok4 = False
            detail4 = f"抛出非预期异常 {type(e).__name__}: {e}"[:120]
    results["checks"].append(
        {"id": "C4_external_router_rejects_wb2_with_guide", "pass": ok4,
         "detail": detail4})

    # C5: 延迟否证线 < 1ms
    lat = r.latency_ms(ht, hp, ft, n=500)
    ok5 = lat < 1.0
    results["checks"].append(
        {"id": "C5_latency_under_1ms", "pass": ok5,
         "detail": f"batch1 中位数延迟={lat:.3f}ms（DS 否证线 >1ms ⟹ 失败）"})

    results["verdict"] = "PASS" if all(c["pass"] for c in results["checks"]) else "FAIL"
    results["note"] = (
        "H-DS24 真实权重 consumability：WB1 适配器可加载 WB2 真实 .pt 并跑通前向，"
        "且与 WB2 自带 RouterShell 数值完全一致（C2）；适配器对 score_sign=-1 的"
        "「已取反」语义处理正确（C3，无额外翻号）。但 WB2 交付的是 RelationModelV2"
        "（程序对→score 打分），与 DS 规格 SemanticRouter（query→组 logits）是两套"
        "接口——C1/C2 只证明权重可消费，不证明可直接接入 SemanticKV 的 per-query 路由"
        "（见台账 H-DS24 real_weights 状态与 notebook Step 4 改写说明）。")
    return results


if __name__ == "__main__":
    res = main()
    out = ROOT / "results" / "wb2_real_weights_test.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n⟹ H-DS24 真实权重验收: {res['verdict']}  →  {out}")
    sys.exit(0 if res["verdict"] == "PASS" else 1)
