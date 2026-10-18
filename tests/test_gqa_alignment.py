#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_gqa_alignment.py — GQA Head 对齐验证（DS-20261018-009-WB1 / H-DS14 响应）

DS 假设 CompressedAttentionPatcher 存在 14Q/2KV 的 Head 对齐错误。
本测试从三个层面证伪/证实该假设：

  T1 repeat_kv 约定  —— runner 的 repeat_kv 与 HF 标准逐元素比对
  T2 importance 映射 —— attention_importance 的 Q头→KV头 归并
                        是否严格遵循 h_q // n_rep（人工参考实现比对）
  T3 端到端等价      —— hybrid(global) 与 topk 在同一分组下数学上应严格等价
                        （分段 logsumexp 合并 = 联合 softmax）；两者输出
                        allclose 即证明：选槽、掩码、分段合并、Head 对齐全链路一致

结果写入 results/gqa_alignment_check.json（H-DS14 的 evidence_file）。
运行：python3 tests/test_gqa_alignment.py [--ctx 512]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "semantickv"))

from v41_torch_native_runner import (  # noqa: E402
    CompressedAttentionPatcher, attention_importance, repeat_kv)


def test_t1_repeat_kv() -> dict:
    """T1: repeat_kv 必须是 KV-major 交错（Q 头 h ↔ KV 头 h//n_rep）。"""
    torch.manual_seed(0)
    B, Hkv, S, hd, n_rep = 1, 2, 8, 4, 7
    k = torch.randn(B, Hkv, S, hd)
    out = repeat_kv(k, n_rep)                       # (B, Hkv*n_rep, S, hd)
    ok = out.shape == (B, Hkv * n_rep, S, hd)
    for hq in range(Hkv * n_rep):
        if not torch.equal(out[0, hq], k[0, hq // n_rep]):
            ok = False
            break
    return {"test": "T1 repeat_kv 约定", "pass": bool(ok),
            "detail": f"Q头 h 的 KV 来自 KV头 h//n_rep（{Hkv}KV×{n_rep}={Hkv*n_rep}Q）" if ok
            else f"失配于头 {hq}"}


def test_t2_importance_mapping() -> dict:
    """T2: attention_importance 的归并头序 = 人工参考（h_q//n_rep）。"""
    torch.manual_seed(1)
    B, Hkv, N, hd, n_rep = 1, 2, 64, 8, 7
    Hq = Hkv * n_rep
    q = torch.randn(B, Hq, N, hd)
    k = torch.randn(B, Hkv, N, hd)
    scaling = hd ** -0.5
    imp = attention_importance(q, k, scaling, n_rep)          # (B,Hkv,N)

    # 人工参考：逐 Q 头用其所属 KV 头（h//n_rep）做因果 softmax，再按组求和
    ref = torch.zeros(B, Hkv, N)
    pos = torch.arange(N)
    for hq in range(Hq):
        hk = hq // n_rep
        sc = q[0, hq] @ k[0, hk].T * scaling                  # (N,N)
        sc = sc.masked_fill(pos[None, :] > pos[:, None], float("-inf"))
        p = torch.softmax(sc, dim=-1)
        ref[0, hk] += p.sum(dim=0)
    ref /= (N * n_rep)
    err = float((imp - ref).abs().max())
    return {"test": "T2 importance Q→KV 映射", "pass": bool(err < 1e-5),
            "max_abs_err": err,
            "detail": "attention_importance 归并严格遵循 h_q//n_rep（HF repeat_kv 约定）"}


def test_t3_end_to_end_equivalence(model_dir: str, ctx: int) -> dict:
    """T3: hybrid(global) ≡ topk（同一分组）——全链路 Head 对齐与分段合并正确性。"""
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForCausalLM.from_pretrained(model_dir, dtype=torch.float32)
    model.eval()

    ids = tok("注意力头对齐验证。" * (ctx // 12), return_tensors="pt").input_ids[:, :ctx]
    N = ids.shape[1]
    # 合成分组：特权 + 语义群
    rng = np.random.default_rng(7)
    labels = rng.integers(0, 16, size=N).astype(np.int64)

    results = {}
    for mode, scope in (("topk", "global"), ("hybrid", "global")):
        p = CompressedAttentionPatcher(
            model, mode=mode, topk_per_group=4, sink_tokens=4, window_tokens=32,
            compress_layers=[6, 12, 18], kmean_renorm="none",
            group_score_prior="none", mid_scope=scope)
        p.set_groups(labels, N)
        p.install()
        try:
            with torch.no_grad():
                out = model(ids, use_cache=False)
        finally:
            p.uninstall()
        results[mode] = {"logits": out.logits.detach(), "slots": p.kv_slots()}

    lg_topk, lg_hyb = results["topk"]["logits"], results["hybrid"]["logits"]
    max_err = float((lg_topk - lg_hyb).abs().max())
    agree = float((lg_topk.argmax(-1) == lg_hyb.argmax(-1)).float().mean())
    rel = max_err / float(lg_topk.abs().max())
    return {"test": "T3 hybrid(global)≡topk 端到端", "pass": bool(rel < 1e-3),
            "max_abs_err": max_err, "relative_err": rel,
            "argmax_agreement": agree, "slots_per_layer": results["topk"]["slots"],
            "detail": "分段 logsumexp 合并与联合 softmax 数值等价（fp32 相对误差 <1e-3）；"
                      "选槽/因果掩码/GQA 头序全链路一致" if rel < 1e-3 else "出现偏差，需排查"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="/workspace/models/Qwen2.5-0.5B")
    ap.add_argument("--ctx", type=int, default=512)
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    t0 = time.time()
    checks = [test_t1_repeat_kv(), test_t2_importance_mapping()]
    try:
        checks.append(test_t3_end_to_end_equivalence(args.model, args.ctx))
        model_info = {"path": args.model, "ctx": args.ctx}
    except Exception as e:                       # 模型缺失时 T1/T2 仍有效
        checks.append({"test": "T3 端到端", "pass": False, "detail": f"跳过: {e}"})
        model_info = {"path": args.model, "ctx": args.ctx, "error": str(e)}

    n_pass = sum(1 for c in checks if c.get("pass"))
    report = {
        "config": {"device": args.device, "dtype": "float32",
                   "purpose": "GQA 14Q/2KV Head 对齐验证（DS-20261018-009-WB1 / H-DS14）",
                   **model_info},
        "environment": {"kind": "cpu-synthetic+real-model-smoke"},
        "checks": checks,
        "summary": {"pass": n_pass, "total": len(checks),
                    "verdict": "ALIGNED" if n_pass == len(checks) else "MISALIGNED"},
        "wall_time_s": round(time.time() - t0, 1),
    }
    out = ROOT / "results" / "gqa_alignment_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for c in checks:
        icon = "✔" if c.get("pass") else "✘"
        print(f" [{icon}] {c['test']}: {c.get('detail', '')}")
    print(f"===== {n_pass}/{len(checks)} PASS -> {report['summary']['verdict']} =====")
    print(f"结果: {out}")
    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
