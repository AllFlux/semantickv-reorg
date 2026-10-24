#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261024-R1-REJUDGE
R1 预算分配「避开 L0 饱和」口径重判（D vs U vs T，6 预算档 × 3 合成）

纯 CPU / numpy，无模型加载、无 GPU、无外部 API。
输入（只读）：Hub 实拉冻结件 r1_budget_allocation.json
输出（新产物，不覆盖任何既有件）：results_v2/r1_rejudge_noL0.json

口径 A（主口径，避开 L0）：层集 L1..L35（35 层）
口径 B（管道自检，不参与判据）：层集 L0..L35（36 层），与原文件 sweep composite 逐格比对
"""
import json, hashlib, math, time
import numpy as np

T0 = time.time()

HUB_PULL = "/tmp/r1_budget_allocation.json"          # Hub 实拉冻结件（本次运行输入）
FROZEN_COPY = "/workspace/semantickv-reorg/results_v2/r1_budget_allocation.hub_pull_20261024.json"
OUT = "/workspace/semantickv-reorg/results_v2/r1_rejudge_noL0.json"

KS = [1, 2, 4, 8, 16, 32]                 # 已测量 K 档（kv_slots = K + 1）
LOG2KS = [math.log2(k) for k in KS]
BUDGETS = [180, 324, 468, 612, 900, 1188]
MODES = ["L1_linear_sum", "L2_max", "L3_euclid"]
ARMS = ["U", "T", "D"]
KMIN, KMAX = 1.0, 32.0                    # K clamp 边界（= 已测量域，超出即外推，禁止）
EPS_SENS = 1e-9                           # 防除零保护（实测最小 |maxdl| = 6e-6 @L30）

raw = open(HUB_PULL, "rb").read()
sha_before = hashlib.sha256(raw).hexdigest()[:16]
d = json.loads(raw)
with open(FROZEN_COPY, "wb") as f:        # 新件留档，不覆盖冻结件
    f.write(raw)

curves = d["sensitivity_curve"]["curves"]
NL_ALL = 36
layers_all = list(range(NL_ALL))
DL = np.array([[curves[str(l)]["points"][i]["delta_loss_answer"] for i in range(6)]
               for l in layers_all], dtype=float)
SENS = np.array([abs(curves[str(l)]["max_delta_loss_answer"]) for l in layers_all], dtype=float)


def is_grid_point(K):
    return any(abs(K - kk) < 1e-12 for kk in KS)


def dL_vec(Ks, layers):
    """层集 × 连续 K → ΔL 向量；K 超出 [1,32] 按端点截断（不外推），log2(K) 线性插值"""
    out, interp = [], False
    for l, K in zip(layers, Ks):
        kc = float(min(max(K, KS[0]), KS[-1]))
        if not is_grid_point(kc):
            interp = True
        x = math.log2(kc)
        out.append(float(np.interp(x, LOG2KS, DL[l])))
    return np.array(out), interp


def composite(dl, mode):
    if mode == "L1_linear_sum":
        return float(np.sum(dl))
    if mode == "L2_max":
        return float(np.max(dl))
    if mode == "L3_euclid":
        return float(np.sqrt(np.sum(dl ** 2)))
    raise ValueError(mode)


# ---------------- 分配算法（clamp [1,32] 后二分，尽力使 Σ(K_l+1) = budget） ----------------
def alloc_U(layers, budget):
    n = len(layers)
    return np.clip(np.full(n, budget / n - 1.0), KMIN, KMAX)


def _bisect(prof_fn, budget):
    """prof_fn(a) → K 向量（单调非降的 a）；二分使 Σ(K+1) = budget"""
    lo, hi = 0.0, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if np.sum(np.clip(prof_fn(mid), KMIN, KMAX) + 1.0) < budget:
            lo = mid
        else:
            hi = mid
    return np.clip(prof_fn(0.5 * (lo + hi)), KMIN, KMAX)


def alloc_T(layers, budget):
    """教科书式：底层 K 大、高层 K 小，线性递减；最顶层 K=1，向上线性抬升；clamp [1,32]"""
    n = len(layers)
    idx = np.arange(n, dtype=float)
    return _bisect(lambda a: 1.0 + a * (n - 1 - idx) / (n - 1), budget)


def alloc_D(layers, budget):
    """数据驱动：K_l ∝ mean_sens / sens_l（实测层敏感度反比，沿用冻结件定义）；clamp [1,32]"""
    s = np.maximum(SENS[layers], EPS_SENS)
    w = float(np.mean(s)) / s
    c_lo, c_hi = 0.0, 1e12
    for _ in range(200):
        mid = 0.5 * (c_lo + c_hi)
        if np.sum(np.clip(mid * w, KMIN, KMAX) + 1.0) < budget:
            c_lo = mid
        else:
            c_hi = mid
    return np.clip(0.5 * (c_lo + c_hi) * w, KMIN, KMAX)


def run_case(layers, budget, tag):
    per_arm = {}
    for arm in ARMS:
        Ks = {"U": alloc_U, "T": alloc_T, "D": alloc_D}[arm](layers, budget)
        dl, interp = dL_vec(Ks, layers)
        per_arm[arm] = {
            "K_profile": [float(x) for x in Ks],
            "K_min": float(np.min(Ks)), "K_max": float(np.max(Ks)),
            "total_slots_check": float(np.sum(Ks + 1.0)),
            "slot_error": float(np.sum(Ks + 1.0) - budget),
            "interpolated": bool(interp),
            "composite": {m: composite(dl, m) for m in MODES},
        }
    deg = {m: float(max(per_arm[a]["composite"][m] for a in ARMS)
                    - min(per_arm[a]["composite"][m] for a in ARMS)) for m in MODES}
    slots = [per_arm[a]["total_slots_check"] for a in ARMS]
    slot_parity = bool(max(slots) - min(slots) <= 1e-6)
    budget_met = bool(abs(slots[0] - budget) <= 1e-6) and slot_parity
    return {
        "tag": tag, "layers": [int(x) for x in layers], "n_layers": len(layers), "budget": budget,
        "per_arm": per_arm, "degenerate_max_abs_diff": deg,
        "is_degenerate": bool(max(deg.values()) < 1e-9),
        "slot_parity": slot_parity, "budget_met": budget_met,
        "capacity_upper_bound": float(len(layers) * (KMAX + 1.0)),
        "budget_infeasible": bool(budget > len(layers) * (KMAX + 1.0)),
    }


# ---------------- 口径 A / B ----------------
A = {str(b): run_case(list(range(1, 36)), b, "A_noL0") for b in BUDGETS}
B = {str(b): run_case(list(range(0, 36)), b, "B_all") for b in BUDGETS}
frozen = {str(s["budget_total_kv_slots"]): s for s in d["sweep"]}

# ---------------- 口径 B 自检：两路 ----------------
sc_recomp, sc_frozen, max_recomp, max_frozen = {}, {}, 0.0, 0.0
for b in BUDGETS:
    fb = frozen[str(b)]["composite"]
    for m in MODES:
        for a in ARMS:
            d1 = abs(B[str(b)]["per_arm"][a]["composite"][m] - fb[a][m])
            sc_recomp[f"{b}|{m}|{a}"] = d1
            max_recomp = max(max_recomp, d1)
            Ks = np.array(frozen[str(b)]["effective_K"][a]["profile_by_layer"], dtype=float)
            dl, _ = dL_vec(Ks, list(range(36)))
            d2 = abs(composite(dl, m) - fb[a][m])
            sc_frozen[f"{b}|{m}|{a}"] = d2
            max_frozen = max(max_frozen, d2)

# 归因子集：冻结件 profile 为精确测量网格点、无 round / 无外推 / 无几何近似的格
# （U 档在 K=4/8/16/32 四档 profile 恰为网格点；K=11.314/22.627 两档为 sqrt(8*16)/sqrt(16*32) 几何近似）
attribution, max_attr = {}, 0.0
for b in BUDGETS:
    p = frozen[str(b)]["effective_K"]["U"]["profile_by_layer"]
    if len(set(p)) == 1 and is_grid_point(p[0]):
        for m in MODES:
            dif = abs(B[str(b)]["per_arm"]["U"]["composite"][m] - frozen[str(b)]["composite"]["U"][m])
            attribution[f"{b}|{m}|U"] = dif
            max_attr = max(max_attr, dif)
frozen_slot_err = {str(b): {a: float(np.sum(np.array(frozen[str(b)]["effective_K"][a]["profile_by_layer"]) + 1.0) - b)
                            for a in ARMS} for b in BUDGETS}
frozen_geom_approx = {str(b): sorted(set(frozen[str(b)]["effective_K"]["U"]["profile_by_layer"]))[0]
                      for b in BUDGETS}
frozen_over_domain = {str(b): {a: float(max(frozen[str(b)]["effective_K"][a]["profile_by_layer"])) > KMAX
                               for a in ARMS} for b in BUDGETS}

# ---------------- 判据 ----------------
delta = {}
for b in BUDGETS:
    pa = A[str(b)]["per_arm"]
    delta[str(b)] = {m: {
        "D_minus_U": pa["D"]["composite"][m] - pa["U"]["composite"][m],
        "D_minus_T": pa["D"]["composite"][m] - pa["T"]["composite"][m],
        "D_better_than_U": bool(pa["D"]["composite"][m] < pa["U"]["composite"][m]),
        "D_better_than_T": bool(pa["D"]["composite"][m] < pa["T"]["composite"][m]),
    } for m in MODES}

# 可比档 = 非退化 且 三臂等预算（slot_parity）；预算不可达/不等 slot 的档剔除并披露
comparable, excluded = [], []
for b in BUDGETS:
    r = A[str(b)]
    if r["is_degenerate"]:
        excluded.append({"budget": b, "reason": "三臂 composite 完全恒等（退化档）"})
    elif not r["slot_parity"]:
        excluded.append({"budget": b, "reason":
                         f"K 上限封顶：35 层×K≤32 容量上限 {r['capacity_upper_bound']:.0f} < 预算 {b}，"
                         f"三臂实际 slots {[round(A[str(b)]['per_arm'][a]['total_slots_check'], 1) for a in ARMS]} 互不等，无法构造等预算比较"})
    else:
        comparable.append(b)

range_A = 0.0
for b in comparable:
    for m in MODES:
        range_A = max(range_A, abs(delta[str(b)][m]["D_minus_U"]), abs(delta[str(b)][m]["D_minus_T"]))

budgets_pass, conflict = [], []
for b in comparable:
    if all(delta[str(b)][m]["D_better_than_U"] and delta[str(b)][m]["D_better_than_T"] for m in MODES):
        budgets_pass.append(b)
    if len({(delta[str(b)][m]["D_better_than_U"], delta[str(b)][m]["D_better_than_T"]) for m in MODES}) > 1:
        conflict.append(b)

l0_dom = {str(b): {f"{m}|{a}": (1.0 - A[str(b)]["per_arm"][a]["composite"][m] / B[str(b)]["per_arm"][a]["composite"][m]
                                if abs(B[str(b)]["per_arm"][a]["composite"][m]) > 1e-12 else None)
                   for m in MODES for a in ARMS} for b in BUDGETS}

ranking_B = {str(b): {m: {
    "order_best_to_worst": sorted(ARMS, key=lambda a: B[str(b)]["per_arm"][a]["composite"][m]),
    "D_better_than_U": bool(B[str(b)]["per_arm"]["D"]["composite"][m] < B[str(b)]["per_arm"]["U"]["composite"][m]),
    "D_better_than_T": bool(B[str(b)]["per_arm"]["D"]["composite"][m] < B[str(b)]["per_arm"]["T"]["composite"][m]),
} for m in MODES} for b in BUDGETS}

E5_GATE = 0.05
pipeline_trusted = bool(max_attr <= 1e-6)      # 归因子集逐位复现 ⇒ 管道可信
n_pass, n_comp = len(budgets_pass), len(comparable)

if not pipeline_trusted:
    status = "pending"
    branch = ("pending：口径 B 归因子集自检超差（>1e-6），无法确认 composite 管道与冻结件一致，"
              "判据不予落定；缺口＝可精确复现冻结件 composite 的内部实现")
elif range_A < E5_GATE:
    status = "e5_withdrawn"
    branch = (f"撤回：动态范围闸门触发（range_A={range_A:.4f} < {E5_GATE}）→ 剔除 L0 后判据不可分辨；"
              "所需资产＝可重算层敏感度的 3B 权重本体 / 多种子·多任务·多 prompt 重复测量")
elif n_pass >= 5 and not conflict:
    status = "confirmed"
    branch = f"confirmed：口径 A 下 D 在 {n_pass}/{n_comp} 个可比档严格优于 U 且 T，三合成方向无矛盾"
else:
    status = "refuted"
    branch = (f"refuted：口径 A 下 {n_comp - n_pass}/{n_comp} 个可比档 D 未同时优于 U 与 T"
              + (f"；三合成方向矛盾档 {conflict}" if conflict else ""))

wall = time.time() - T0
sha_after = hashlib.sha256(open(HUB_PULL, "rb").read()).hexdigest()[:16]

out = {
    "task": "WB0-INSTR-20261024-R1-REJUDGE",
    "idempotency_key": "WB0-INSTR-20261024-R1-REJUDGE",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "theme": "D-规模资源",
    "serves_north_star": "结构可压·事实受限（预算约束下的 KV 结构性分配 → 压缩质量）",
    "frozen_input": {
        "path": "results_v2/r1_budget_allocation.json",
        "hub_endpoint": "/api/file/r1_budget_allocation",
        "size_bytes": len(raw),
        "sha256_16_before": sha_before, "sha256_16_after": sha_after,
        "sha_stable": sha_before == sha_after,
        "hub_pull_copy": "results_v2/r1_budget_allocation.hub_pull_20261024.json",
    },
    "method": {
        "interpolation": "log2(K) 线性插值（沿用冻结件口径）；K 超出 [1,32] 按端点截断，不做曲线外推",
        "composite": {"L1_linear_sum": "Σ_l ΔL_l", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
        "slot_constraint": "Σ_l (K_l+1) = budget；clamp [1,32] 后二分求解，容差 1e-6",
        "arm_U": "全层同一连续 K",
        "arm_T": "底层 K 大高层 K 小线性递减，最顶层 K=1，二分抬升尺度对齐预算",
        "arm_D": "K_l ∝ mean_sens/sens_l（沿用冻结件『反比』定义），sens_l=|max_delta_loss_answer|，clamp+二分对齐预算",
        "sens_definition": "sens_l = |max_delta_loss_answer|（每层 6 档 K 上的最大绝对 ΔL_answer）",
        "eps_sens": EPS_SENS,
        "eps_note": "仅防除零；实测最小 |maxdl|=6e-6(L30)，且 clamp 主导，eps 不改变任何档的分配结论",
    },
    "caliber_A_noL0": {
        "layer_set": "L1..L35 (35 层，剔除 L0)",
        "per_budget": {str(b): {
            "degenerate": A[str(b)]["is_degenerate"],
            "degenerate_max_abs_diff": A[str(b)]["degenerate_max_abs_diff"],
            "slot_parity": A[str(b)]["slot_parity"], "budget_met": A[str(b)]["budget_met"],
            "budget_infeasible": A[str(b)]["budget_infeasible"],
            "capacity_upper_bound": A[str(b)]["capacity_upper_bound"],
            "comparable": b in comparable,
            "per_arm": {a: {
                "total_slots_check": A[str(b)]["per_arm"][a]["total_slots_check"],
                "slot_error": A[str(b)]["per_arm"][a]["slot_error"],
                "K_min": A[str(b)]["per_arm"][a]["K_min"], "K_max": A[str(b)]["per_arm"][a]["K_max"],
                "interpolated": A[str(b)]["per_arm"][a]["interpolated"],
                "composite": A[str(b)]["per_arm"][a]["composite"],
            } for a in ARMS},
            "delta": delta[str(b)],
        } for b in BUDGETS},
        "range_A": range_A,
        "comparable_budgets": comparable,
        "excluded_budgets": excluded,
    },
    "caliber_B_all": {
        "layer_set": "L0..L35 (36 层，管道自检，不参与判据)",
        "per_budget": {str(b): {
            "degenerate": B[str(b)]["is_degenerate"],
            "per_arm": {a: {
                "total_slots_check": B[str(b)]["per_arm"][a]["total_slots_check"],
                "slot_error": B[str(b)]["per_arm"][a]["slot_error"],
                "composite": B[str(b)]["per_arm"][a]["composite"],
            } for a in ARMS},
            "frozen_composite": frozen[str(b)]["composite"],
        } for b in BUDGETS},
        "selfcheck_max_abs_diff_recomputed_profile": max_recomp,
        "selfcheck_max_abs_diff_frozen_profile": max_frozen,
        "selfcheck_detail_recomputed": sc_recomp,
        "selfcheck_detail_frozen_profile": sc_frozen,
        "attribution_subset": {
            "definition": "冻结件 U 臂 profile 恰为测量网格点（K=4/8/16/32，无 round、无几何近似、无外推）的格；"
                          "此子集内若逐位复现，则插值+合成管道与冻结件一致，剩余差异可归因于 profile 展示值缺陷",
            "cells": attribution, "max_abs_diff": max_attr,
            "n_cells": len(attribution),
        },
        "pipeline_trusted": pipeline_trusted,
        "frozen_artifact_defects": {
            "profile_slot_error": frozen_slot_err,
            "U_profile_values": frozen_geom_approx,
            "geometric_midpoint_approx_budgets": [b for b in BUDGETS if not is_grid_point(frozen_geom_approx[str(b)])],
            "over_measured_domain_K_gt_32": frozen_over_domain,
            "note": "冻结件 profile_by_layer 与其自述 total_slots_check 不闭合：多档少用 5~49 slots（最高 5.5%），"
                    "1188 档 T/D 出现 K=33（超出已测量域 K≤32，属无依据外推）；468/900 档 U 取几何中点 "
                    "sqrt(8×16)=11.314 / sqrt(16×32)=22.627 而非 12 / 24，导致预算未用满。以上均为冻结件既有缺陷，本轮仅披露不修改。",
        },
        "ranking_consistency_B": ranking_B,
    },
    "L0_dominance": {"definition": "1 - composite_A / composite_B", "per_budget": l0_dom},
    "verdict": {
        "status": status, "branch_text": branch,
        "n_budget_pass": n_pass, "n_comparable": n_comp,
        "budgets_pass": budgets_pass, "mode_direction_conflict_budgets": conflict,
        "e5_gate": E5_GATE, "range_A": range_A,
        "counts": {"budgets": len(BUDGETS), "modes": len(MODES), "arms": len(ARMS), "calibers": 2,
                   "cells": len(BUDGETS) * len(MODES) * len(ARMS) * 2},
    },
    "n_and_power": {
        "n": 1, "detail": "1 种子 / 1 needle / 8 段落 / 2613 tokens / 1 模型（沿用冻结件 n_boundary）",
        "power_analysis": "不可做（n=1，无种子/任务/prompt 方差）",
        "level": "假设级 / 机制观察，不得表述为该模型或架构的真实层敏感度",
    },
    "dependency_missing": ["/workspace/models/Qwen2.5-3B-Instruct 已蒸发，层敏感度不可重算（沿用冻结件 n_boundary）"],
    "p0": True,
    "wall_clock_sec": round(wall, 3),
    "scope_guard_selfcheck": {
        "1_no_fabrication": "所有 ΔL 取自冻结件原始 cells；未测量 K 一律 log2 插值并标 interpolated；未填入任何未实测层敏感度",
        "2_frozen_untouched": f"冻结件只读，运行前后 sha256_16 均为 {sha_before}（stable={sha_before == sha_after}）；新产物仅 r1_rejudge_noL0.json/.py 与 hub_pull 副本",
        "3_no_overreach": "仅做 R1 三臂重算与定级建议；未改写 H-R1-BUDGET 最终定级（待 WB0 裁定）；未触碰任何权重文件",
        "4_no_ledger_write": "未直写 hypothesis_ledger.yaml",
        "5_no_model_load": "纯 numpy，无模型下载/加载，无 long_run 需求",
        "6_no_silent_trim": f"6 档×3 合成×3 臂×2 口径={len(BUDGETS)*len(MODES)*len(ARMS)*2} 格全跑，wall_clock={wall:.1f}s≤300s，无分批无缩样；"
                            f"1188 档因 K 上限封顶不可比而剔除，已在 caliber_A.excluded_budgets 显式披露（非静默裁剪）",
    },
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print(json.dumps({
    "status": status, "branch": branch, "range_A": round(range_A, 6),
    "attribution_max_abs_diff": max_attr, "attribution_cells": len(attribution),
    "maxdiff_B_recomputed": max_recomp, "maxdiff_B_frozenprof": max_frozen,
    "comparable": comparable, "excluded": [e["budget"] for e in excluded],
    "budgets_pass": budgets_pass, "conflict": conflict,
    "wall_clock": round(wall, 2),
    "A_L1": {str(b): {a: round(A[str(b)]["per_arm"][a]["composite"]["L1_linear_sum"], 4) for a in ARMS} for b in BUDGETS},
    "A_L2": {str(b): {a: round(A[str(b)]["per_arm"][a]["composite"]["L2_max"], 4) for a in ARMS} for b in BUDGETS},
    "A_L3": {str(b): {a: round(A[str(b)]["per_arm"][a]["composite"]["L3_euclid"], 4) for a in ARMS} for b in BUDGETS},
    "slots_A": {str(b): {a: round(A[str(b)]["per_arm"][a]["total_slots_check"], 2) for a in ARMS} for b in BUDGETS},
    "L0dom_L1": {str(b): {a: round(l0_dom[str(b)][f"L1_linear_sum|{a}"], 4) for a in ARMS} for b in BUDGETS},
}, ensure_ascii=False, indent=1))
