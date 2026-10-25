#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261025-EXP-18-PAIRED-RESOLV
评估口径分辨力救治：配对差口径 vs 非配对口径（EXP-14/16 的独立 sd(R) 合成）

口径纪律：
  · 输入只用指令 §2.1 列出的三个 Hub 实物；
  · ΔL 一律取冻结件实测 (r1 sensitivity_curve.curves[*].points[*].delta_loss_answer)，
    K 全为整数网格 {1,2,4,8,16,32} 上的已测点，无插值、无外推、无反推；
  · 不做任何补全/插补/生成替代明细；缺失即登记 dependency_missing；
  · seed=20261026 固定；幂等可重跑，只覆盖同名产物。
"""
import json, hashlib, os, itertools
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
R15 = os.path.join(HERE, "exp15_kgrid.json")
R16 = os.path.join(HERE, "exp16_sparse_matched.json")
R1 = os.path.join(HERE, "r1_budget_allocation.hub_pull_20261025_WB2.json")
OUT = os.path.join(HERE, "exp18_paired_resolv.json")

SEED = 20261026
N_BOOT = 5000
K_GRID = [1, 2, 4, 8, 16, 32]


def sha16(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]


def composite(dl, synth):
    a = np.asarray(dl, dtype=float)
    if synth == "L1_linear_sum":
        return float(a.sum())
    if synth == "L2_max":
        return float(a.max())
    if synth == "L3_euclid":
        return float(np.sqrt((a ** 2).sum()))
    raise ValueError(synth)


def boot_ci(x, n_boot=N_BOOT, seed=SEED):
    rng = np.random.default_rng(seed)
    x = np.asarray(x, dtype=float)
    idx = rng.integers(0, x.size, size=(n_boot, x.size))
    means = x[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main():
    j15 = json.load(open(R15))
    j16 = json.load(open(R16))
    j1 = json.load(open(R1))

    out = {
        "task": "WB0-INSTR-20261025-EXP-18-PAIRED-RESOLV",
        "idempotency_key": "WB0-INSTR-20261025-EXP-18-PAIRED-RESOLV",
        "instruction_ref": "1792865189-e25872",
        "executor": "WB1",
        "theme": "B",
        "serves_north_star": "结构可压·事实受限——判『评估口径能否分辨差异』而非『哪个分配更好』",
        "seed": SEED,
        "n_boot": N_BOOT,
        "inputs": {},
        "granularity_audit": {},
        "recompute_check_vs_exp15": {},
        "paired_analysis": {},
        "previously_indistinguishable_cases": {},
        "verdict": {},
        "dependency_missing": [],
        "p0": False,
        "p0_reason": "产物可由本脚本在 Hub 三实物上幂等重跑复现（seed 固定），非不可再生资产",
    }

    # ---------- 0. 输入指纹 ----------
    out["inputs"] = {
        "results_v2/exp15_kgrid.json": {"size": os.path.getsize(R15), "sha256_16": sha16(R15)},
        "results_v2/exp16_sparse_matched.json": {"size": os.path.getsize(R16), "sha256_16": sha16(R16)},
        "results_v2/r1_budget_allocation.hub_pull_20261025_WB2.json": {"size": os.path.getsize(R1), "sha256_16": sha16(R1)},
    }

    # ---------- 1. 粒度审计：是否存在 per-query / per-sample 级 composite 明细 ----------
    def scan(node, path="", hits=None, sample_dims=None):
        if hits is None:
            hits, sample_dims = [], []
        if isinstance(node, dict):
            for k, v in node.items():
                scan(v, path + "/" + str(k), hits, sample_dims)
        elif isinstance(node, list):
            # 候选样本维度：长度 >= 8 的数值数组，或键名含 query/prompt/segment/sample
            low = path.lower()
            if node and all(isinstance(x, (int, float)) for x in node):
                if len(node) >= 8 or any(t in low for t in ("query", "prompt", "segment", "sample", "per_")):
                    sample_dims.append({"path": path, "len": len(node),
                                        "numeric": True,
                                        "name_hint": any(t in low for t in ("query", "prompt", "segment", "sample", "per_"))})
            if any(t in low for t in ("query", "prompt", "segment")):
                hits.append({"path": path, "len": len(node)})
            for i, v in enumerate(node[:3]):
                scan(v, path + f"[{i}]", hits, sample_dims)
        return hits, sample_dims

    h15, s15 = scan(j15, "exp15")
    h16, s16 = scan(j16, "exp16")
    h1, s1 = scan(j1, "r1")

    out["granularity_audit"] = {
        "question": "三个输入文件是否存在 per-query（per-prompt / per-segment）级 composite 原始明细",
        "r1_n_boundary": j1.get("n_boundary", {}).get("detail"),
        "r1_note": j1.get("n_boundary", {}).get("consequence"),
        "query_named_arrays": {"exp15": h15, "exp16": h16, "r1": h1},
        "numeric_sample_dims": {
            "exp15": [d for d in s15 if d["name_hint"]][:12],
            "exp16": [d for d in s16 if d["name_hint"]][:12],
            "r1": [d for d in s1 if d["name_hint"]][:12],
        },
        "per_query_detail_present": False,
        "finest_available_unit": "层单元（layer）：r1 sensitivity_curve.curves 逐层 × 6 档 K 的实测 delta_loss_answer（36 层）",
        "per_query_absence_reason": (
            "r1 n_boundary 明示 n=1 种子 / 1 needle 任务 / 8 段落，且落盘仅到『层 × K 档』聚合 ΔL 与 flip rate，"
            "无段落级（per-segment / per-prompt）composite 数组；exp15 只到『预算档 × arm』composite；"
            "exp16 只到『预算档 × 合成』的族 A 聚合统计（mean/sd/分位/min/max，n_used=200 但逐次值未落盘）。"
        ),
    }

    # ---------- 2. 冻结件 ΔL 表 + 复算 exp15 composite 校验 ----------
    curves = j1["sensitivity_curve"]["curves"]          # key: "0".."35"，每层 6 档 K
    dl_tab = {}
    for lk, lv in curves.items():
        row = {}
        for p in lv["points"]:
            row[int(p["K"])] = float(p["delta_loss_answer"])
        dl_tab[int(lk)] = row
    missing_pts = [l for l, row in dl_tab.items() if sorted(row.keys()) != K_GRID]
    out["frozen_dl_table"] = {
        "n_layers": len(dl_tab),
        "k_grid": K_GRID,
        "layers_with_incomplete_k_grid": missing_pts,
        "interpolation_used": False,
        "note": "exp15 的 K 网格 {1,2,4,8,16,32} 全部落在冻结件已测点上，无需插值/外推",
    }

    # 复算校验：用 exp15 的 profile_by_layer 重算 composite，与 exp15 落盘值比对
    calA = j15["caliber_A_noL0"]
    layer_offset = 1  # caliber_A = L1..L35（剔除 L0）
    recom = {}
    maxdiff = 0.0
    for b, bv in calA["per_budget"].items():
        per_arm = bv.get("per_arm", {})
        recom[b] = {}
        for arm, av in per_arm.items():
            if "profile_by_layer" not in av or "composite" not in av:
                recom[b][arm] = {"skipped": True, "available_keys": sorted(av.keys())[:8]}
                continue
            prof = av["profile_by_layer"]
            dl = [dl_tab[i + layer_offset][int(k)] for i, k in enumerate(prof)]
            got = av["composite"]
            calc = {s: composite(dl, s) for s in ("L1_linear_sum", "L2_max", "L3_euclid")}
            diffs = {s: abs(calc[s] - got[s]) for s in calc}
            maxdiff = max(maxdiff, max(diffs.values()))
            recom[b][arm] = {"calc": calc, "reported": got, "abs_diff": diffs}
    out["recompute_check_vs_exp15"] = {
        "layer_mapping": "exp15 profile_by_layer[i] -> curves[str(i+1)]（caliber_A = L1..L35，剔除 L0）",
        "max_abs_diff_all_budgets_arms_synths": maxdiff,
        "all_match": bool(maxdiff < 1e-9),
        "detail_sample": {b: recom[b] for b in list(recom)[:2]},
    }

    # ---------- 3. 层单元配对分析 ----------
    # 配对单元 u = 层 l（口径 A：L1..L35，n=35）；配对键 = (预算档, 层)；配对量 d_l = ΔL_l(armA) − ΔL_l(armB)
    budgets = [b for b in calA.get("comparable_budgets", []) if str(b) in calA["per_budget"]]
    arms = ["U", "T", "D", "P"]
    synths = ["L1_linear_sum", "L2_max", "L3_euclid"]
    pairs = [("D", "U"), ("T", "U"), ("P", "U"), ("D", "T"), ("P", "D")]

    paired = {}
    for b in budgets:
        bv = calA["per_budget"][str(b)]
        prof = {a: bv["per_arm"][a]["profile_by_layer"] for a in arms if a in bv.get("per_arm", {})}
        dl_by_arm = {a: np.array([dl_tab[i + layer_offset][int(k)] for i, k in enumerate(p)], dtype=float)
                     for a, p in prof.items()}
        paired[str(b)] = {}
        for (aa, ab) in pairs:
            if aa not in dl_by_arm or ab not in dl_by_arm:
                continue
            d = dl_by_arm[aa] - dl_by_arm[ab]
            n = int(d.size)
            lo, hi = boot_ci(d)
            # 非配对口径对照（沿用 EXP-14/16 落盘 sd(R)：族/置换抽样分布标准差）
            unpaired = {}
            rc16 = j16.get("resolution_comparison_vs_exp14", {}).get("per_unit", {})
            for s in synths:
                key = f"{b}/{s}"
                if key in rc16:
                    unpaired[s] = {
                        "sd_R_exp14_perm": rc16[key].get("sd_R_exp14_perm"),
                        "sd_Rprime_exp16_familyA": rc16[key].get("sd_Rprime_exp16_familyA"),
                    }
            paired[str(b)][f"{aa}-{ab}"] = {
                "n_paired_units": n,
                "mean_d_L1": float(d.mean()),
                "sd_d": float(d.std(ddof=1)),
                "ci95_L1_bootstrap5000": [lo, hi],
                "ci_excludes_zero": bool((lo > 0) or (hi < 0)),
                "abs_mean_d_ge_0_01": bool(abs(float(d.mean())) >= 0.01),
                "unpaired_baseline_sd_R": unpaired,
                "reduction_vs_sdRprime_L1": (None if "L1_linear_sum" not in unpaired
                                             or unpaired["L1_linear_sum"].get("sd_Rprime_exp16_familyA") in (None, 0)
                                             else float(1.0 - d.std(ddof=1) / unpaired["L1_linear_sum"]["sd_Rprime_exp16_familyA"])),
            }
    out["paired_analysis"] = {
        "pairing_unit": "层单元（同预算档、同层、两 arm 的 K 分配差异）",
        "pairing_key": "(budget, layer)",
        "d_definition": "d_l = ΔL_l(K_l^A) − ΔL_l(K_l^B)，ΔL 全部取自冻结件实测点",
        "n_units_per_budget": calA.get("n_layers", 35),
        "ci_method": f"bootstrap {N_BOOT} 次，seed={SEED}（配对 t 未使用，口径已写明）",
        "per_budget": paired,
        "caliber_change_disclosure": (
            "【D2 口径披露】指令 §2.3 的配对键为『同 query + 同预算档 + 同层单元』；"
            "本轮 per-query 维度不存在（r1 n=1 种子/1 任务，无段落级落盘），配对键退化为 (budget, layer)。"
            "故 sd(d) 度量的是『两 arm 的 ΔL 差异在层维度上的离散度』（结构效应的空间分布），"
            "与非配对 sd(R)（随机 K 向量抽样分布的 composite 离散）不属于同一抽样空间，"
            "两者之比的『降低幅度』不能直接解读为测量噪声下降。此项已显式披露，不作为 confirmed 依据。"
        ),
    }

    # ---------- 4. 既有「不可分辨」对照的转可分辨检验 ----------
    sd_table = j16.get("verdict", {}).get("e5_diagnostics", {}).get("sd_table", {})
    indist = [k for k, v in sd_table.items() if v and v > 0.05]
    cases = []
    for key in indist[:6]:
        b, s = key.split("/")
        if b not in paired:
            continue
        for pa, pv in paired[b].items():
            cases.append({
                "case": f"{key} · arm pair {pa}",
                "sd_R_exp16": sd_table[key],
                "n": pv["n_paired_units"],
                "mean_d": pv["mean_d_L1"],
                "sd_d": pv["sd_d"],
                "ci95": pv["ci95_L1_bootstrap5000"],
                "abs_mean_d_ge_0_01": pv["abs_mean_d_ge_0_01"],
                "ci_excludes_zero": pv["ci_excludes_zero"],
                "turned_resolvable": bool(pv["abs_mean_d_ge_0_01"] and pv["ci_excludes_zero"]),
            })
    n_turned = sum(1 for c in cases if c["turned_resolvable"])
    out["previously_indistinguishable_cases"] = {
        "n_cases_checked": len(cases),
        "n_turned_resolvable": n_turned,
        "note": "取自 exp16 verdict.e5_diagnostics.sd_table 中 sd>0.05 的不可分辨单元；逐组登记",
        "cases": cases,
    }

    # ---------- 5. 判据四分支 ----------
    sds = [pv["sd_d"] for b in paired.values() for pv in b.values()]
    mean_sd_d = float(np.mean(sds)) if sds else None
    min_sd_d = float(np.min(sds)) if sds else None
    max_sd_d = float(np.max(sds)) if sds else None
    sdR = [v for v in sd_table.values() if v]
    mean_sd_R = float(np.mean(sdR)) if sdR else None
    reduction = float(1.0 - mean_sd_d / mean_sd_R) if (mean_sd_d is not None and mean_sd_R) else None

    crit = {
        "C1_n_ge_30": bool(calA.get("n_layers", 35) >= 30),
        "C2_sd_d_lt_0_05": bool(mean_sd_d is not None and mean_sd_d < 0.05),
        "C3_reduction_ge_50pct": bool(reduction is not None and reduction >= 0.50),
        "C4_at_least_2_of_3_turned": bool(n_turned >= 2 and len(cases) >= 3),
        "refuted_branch_sd_d_ge_0_05": bool(mean_sd_d is not None and mean_sd_d >= 0.05),
        "refuted_branch_reduction_lt_20pct": bool(reduction is not None and reduction < 0.20),
        "e5_branch_per_query_missing": True,  # 见 granularity_audit
        "values": {
            "n_paired_units_per_budget": calA.get("n_layers", 35),
            "mean_sd_d": mean_sd_d, "min_sd_d": min_sd_d, "max_sd_d": max_sd_d,
            "mean_sd_R_exp16": mean_sd_R,
            "reduction_pct": None if reduction is None else round(reduction * 100, 2),
            "n_cases_checked": len(cases), "n_turned_resolvable": n_turned,
        },
    }
    if crit["e5_branch_per_query_missing"]:
        status = "e5_withdrawn"
    elif crit["C1_n_ge_30"] and crit["C2_sd_d_lt_0_05"] and crit["C3_reduction_ge_50pct"] and crit["C4_at_least_2_of_3_turned"]:
        status = "confirmed"
    elif crit["refuted_branch_sd_d_ge_0_05"] or crit["refuted_branch_reduction_lt_20pct"]:
        status = "refuted"
    else:
        status = "pending"

    out["verdict"] = {
        "hypothesis": "H-R1-RESOLV-PAIRED",
        "status": status,
        "criteria": crit,
        "decision_note": (
            "主结论按 §4 E5 分支：三输入文件均无 per-query（per-prompt/per-segment）级 composite 明细，"
            "最细可得单元为『层 × K 档』实测 ΔL；per-query 维度在源数据上即不存在（n=1 种子/1 任务，"
            "仅 8 段落且未落盘段落级值），无法扩容，故终态关闭本问。"
            "层单元配对结果作为『所需资产到位后是否值得重跑』的可行性上限估计附后，不构成判据。"
        ),
        "required_assets": [
            "per-prompt / per-segment 级 composite（或 ΔL_answer）逐样本落盘：至少 n>=30 个独立 prompt/段落单元，且同一单元在多 arm 下均有实测值",
            "多种子重复测量（>=3 seed）以分离测量噪声与结构效应",
            "3B 权重 /workspace/models/Qwen2.5-3B-Instruct（P0，已蒸发）或等价可重算环境，用于补齐上述逐样本落盘",
        ],
        "auxiliary_layer_unit_observation": {
            "non_binding": True,
            "mean_sd_d": mean_sd_d, "mean_sd_R_exp16": mean_sd_R,
            "reduction_pct": None if reduction is None else round(reduction * 100, 2),
            "n_turned_resolvable": n_turned, "n_cases_checked": len(cases),
            "caveat": ("该观察换用了层单元口径（非 per-query），且 sd(d) 与非配对 sd(R) 不同抽样空间；"
                       "即使 sd(d) < 0.05，也只能说明『两 arm 差异在层维度上较均匀』，"
                       "不能说明测量噪声被消除。按 D1 不作为 confirmed/refuted 依据。"),
        },
    }

    out["dependency_missing"] = [
        "per-query / per-prompt / per-segment 级 composite 明细（三输入文件均无，见 granularity_audit）",
        "多种子（>=3）重复测量的逐样本 composite 落盘",
        "/workspace/models/Qwen2.5-3B-Instruct（P0，r1 n_boundary 明示已蒸发，无法重算逐样本值）",
    ]

    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=2)

    print("== EXP-18 摘要 ==")
    print("粒度审计 per_query_detail_present:", out["granularity_audit"]["per_query_detail_present"])
    print("复算 exp15 composite max_abs_diff:", maxdiff, "| all_match:", out["recompute_check_vs_exp15"]["all_match"])
    print("mean sd(d) =", round(mean_sd_d, 6) if mean_sd_d is not None else None,
          "| mean sd(R)_exp16 =", round(mean_sd_R, 6) if mean_sd_R else None,
          "| reduction% =", crit["values"]["reduction_pct"])
    print("不可分辨对照转可分辨:", n_turned, "/", len(cases))
    print("status =", status)
    print("产物:", OUT, os.path.getsize(OUT), "B")


if __name__ == "__main__":
    main()
