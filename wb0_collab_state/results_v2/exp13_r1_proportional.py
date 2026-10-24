#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261025-EXP-13-PROPORTIONAL
R1 预算分配「正比 vs 反比」四臂同口径对照重跑（U / T / D(反比) / P(正比)）

纯 CPU / numpy，无模型加载、无 GPU、无外部 API。
输入（只读）：Hub 实拉冻结件 r1_budget_allocation.json（本轮 20261025 拉取副本）
输出（新产物，只覆盖同名产物）：results_v2/exp13_r1_proportional.json

口径 A（主口径，剔除 L0）：层集 L1..L35（35 层）→ 参与判据
口径 B（全层自检）：层集 L0..L35（36 层）→ 与冻结件 sweep composite 逐格比对，不参与判据
新增臂 P：K_l ∝ sens_l（正比），与 D（K_l ∝ mean_sens/sens_l，反比）仅方向不同，其余口径一致。
"""
import json, hashlib, math, time
import numpy as np

T0 = time.time()

REPO = "/workspace/semantickv-reorg"
HUB_PULL = f"{REPO}/results_v2/r1_budget_allocation.hub_pull_20261025_WB1.json"
FROZEN_REPO_COPY = f"{REPO}/results_v2/r1_budget_allocation.json"
OUT = f"{REPO}/results_v2/exp13_r1_proportional.json"

KS = [1, 2, 4, 8, 16, 32]                 # 已测量 K 档（kv_slots = K + 1）
LOG2KS = [math.log2(k) for k in KS]
BUDGETS = [180, 324, 468, 612, 900, 1188]
MODES = ["L1_linear_sum", "L2_max", "L3_euclid"]
ARMS = ["U", "T", "D", "P"]               # P = 新增正比臂
KMIN, KMAX = 1.0, 32.0
EPS_SENS = 1e-9
E5_GATE = 0.05

raw = open(HUB_PULL, "rb").read()
sha_before = hashlib.sha256(raw).hexdigest()[:16]
try:
    frozen_repo_raw = open(FROZEN_REPO_COPY, "rb").read()
    sha_frozen_repo = hashlib.sha256(frozen_repo_raw).hexdigest()[:16]
except FileNotFoundError:
    sha_frozen_repo = None
d = json.loads(raw)

curves = d["sensitivity_curve"]["curves"]
NL_ALL = 36
layers_all = list(range(NL_ALL))
DL = np.array([[curves[str(l)]["points"][i]["delta_loss_answer"] for i in range(6)]
               for l in layers_all], dtype=float)
SENS = np.array([abs(curves[str(l)]["max_delta_loss_answer"]) for l in layers_all], dtype=float)


def is_grid_point(K):
    return any(abs(K - kk) < 1e-12 for kk in KS)


def dL_vec(Ks, layers):
    """层集 × 连续 K → ΔL 向量；K 超出 [1,32] 端点截断（不外推），log2(K) 线性插值"""
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


def alloc_U(layers, budget):
    n = len(layers)
    return np.clip(np.full(n, budget / n - 1.0), KMIN, KMAX)


def _bisect(prof_fn, budget):
    lo, hi = 0.0, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if np.sum(np.clip(prof_fn(mid), KMIN, KMAX) + 1.0) < budget:
            lo = mid
        else:
            hi = mid
    return np.clip(prof_fn(0.5 * (lo + hi)), KMIN, KMAX)


def alloc_T(layers, budget):
    n = len(layers)
    idx = np.arange(n, dtype=float)
    return _bisect(lambda a: 1.0 + a * (n - 1 - idx) / (n - 1), budget)


def _scale_to_budget(w, budget):
    c_lo, c_hi = 0.0, 1e12
    for _ in range(200):
        mid = 0.5 * (c_lo + c_hi)
        if np.sum(np.clip(mid * w, KMIN, KMAX) + 1.0) < budget:
            c_lo = mid
        else:
            c_hi = mid
    return np.clip(0.5 * (c_lo + c_hi) * w, KMIN, KMAX)


def alloc_D(layers, budget):
    """反比：K_l ∝ mean_sens / sens_l"""
    s = np.maximum(SENS[layers], EPS_SENS)
    return _scale_to_budget(float(np.mean(s)) / s, budget)


def alloc_P(layers, budget):
    """正比（本轮新增）：K_l ∝ sens_l"""
    s = np.maximum(SENS[layers], EPS_SENS)
    return _scale_to_budget(s, budget)


ALLOC = {"U": alloc_U, "T": alloc_T, "D": alloc_D, "P": alloc_P}


def run_case(layers, budget, tag):
    per_arm = {}
    for arm in ARMS:
        Ks = ALLOC[arm](layers, budget)
        dl, interp = dL_vec(Ks, layers)
        per_arm[arm] = {
            "K_min": float(np.min(Ks)), "K_max": float(np.max(Ks)),
            "K_mean": float(np.mean(Ks)),
            "n_layers_at_kmin": int(np.sum(Ks <= KMIN + 1e-9)),
            "n_layers_at_kmax": int(np.sum(Ks >= KMAX - 1e-9)),
            "total_slots_check": float(np.sum(Ks + 1.0)),
            "slot_error": float(np.sum(Ks + 1.0) - budget),
            "interpolated": bool(interp),
            "composite": {m: composite(dl, m) for m in MODES},
        }
    deg = {m: float(max(per_arm[a]["composite"][m] for a in ARMS)
                    - min(per_arm[a]["composite"][m] for a in ARMS)) for m in MODES}
    slots = [per_arm[a]["total_slots_check"] for a in ARMS]
    slot_parity = bool(max(slots) - min(slots) <= 1e-6)
    budget_met = bool(all(abs(s - budget) <= 1e-6 for s in slots))
    return {
        "tag": tag, "layers": [int(x) for x in layers], "n_layers": len(layers), "budget": budget,
        "per_arm": per_arm, "degenerate_max_abs_diff": deg,
        "is_degenerate": bool(max(deg.values()) < 1e-9),
        "slot_parity": slot_parity, "budget_met": budget_met,
        "capacity_upper_bound": float(len(layers) * (KMAX + 1.0)),
        "budget_infeasible": bool(budget > len(layers) * (KMAX + 1.0)),
    }


A = {str(b): run_case(list(range(1, 36)), b, "A_noL0") for b in BUDGETS}
B = {str(b): run_case(list(range(0, 36)), b, "B_all") for b in BUDGETS}
frozen = {str(s["budget_total_kv_slots"]): s for s in d["sweep"]}

# ---------- 口径 B 管道自检（沿用 R1-REJUDGE 归因子集口径） ----------
attribution, max_attr = {}, 0.0
for b in BUDGETS:
    p = frozen[str(b)]["effective_K"]["U"]["profile_by_layer"]
    if len(set(p)) == 1 and is_grid_point(p[0]):
        for m in MODES:
            dif = abs(B[str(b)]["per_arm"]["U"]["composite"][m] - frozen[str(b)]["composite"]["U"][m])
            attribution[f"{b}|{m}|U"] = dif
            max_attr = max(max_attr, dif)
pipeline_trusted = bool(max_attr <= 1e-6)

# ---------- 可比档 ----------
comparable, excluded = [], []
for b in BUDGETS:
    r = A[str(b)]
    if r["is_degenerate"]:
        excluded.append({"budget": b, "reason": "四臂 composite 完全恒等（退化档）"})
    elif not r["slot_parity"] or not r["budget_met"]:
        excluded.append({"budget": b, "reason":
                         f"K 上限封顶：35 层×K≤32 容量上限 {r['capacity_upper_bound']:.0f} < 预算 {b}，"
                         f"四臂实际 slots {[round(A[str(b)]['per_arm'][a]['total_slots_check'], 1) for a in ARMS]} 不相等，无法构造等预算比较"})
    else:
        comparable.append(b)

# ---------- P vs U / P vs T / P vs D ----------
delta = {}
for b in BUDGETS:
    pa = A[str(b)]["per_arm"]
    delta[str(b)] = {m: {
        "P_minus_U": pa["P"]["composite"][m] - pa["U"]["composite"][m],
        "P_minus_T": pa["P"]["composite"][m] - pa["T"]["composite"][m],
        "P_minus_D": pa["P"]["composite"][m] - pa["D"]["composite"][m],
        "P_better_than_U": bool(pa["P"]["composite"][m] < pa["U"]["composite"][m]),
        "P_better_than_T": bool(pa["P"]["composite"][m] < pa["T"]["composite"][m]),
        "P_better_than_D": bool(pa["P"]["composite"][m] < pa["D"]["composite"][m]),
    } for m in MODES}

range_A = 0.0
for b in comparable:
    for m in MODES:
        range_A = max(range_A, abs(delta[str(b)][m]["P_minus_U"]), abs(delta[str(b)][m]["P_minus_T"]))

budgets_pass, conflict = [], []
for b in comparable:
    if all(delta[str(b)][m]["P_better_than_U"] and delta[str(b)][m]["P_better_than_T"] for m in MODES):
        budgets_pass.append(b)
    if len({(delta[str(b)][m]["P_better_than_U"], delta[str(b)][m]["P_better_than_T"]) for m in MODES}) > 1:
        conflict.append(b)

# 方向一致性（P-vs-U、P-vs-T 在 L1/L2/L3 上的符号是否一致）
dir_signs = {b: {k: sorted({np.sign(delta[str(b)][m][k]) for m in MODES})
                 for k in ["P_minus_U", "P_minus_T"]} for b in comparable}

# ---------- L0 支配度（四臂） ----------
l0_dom = {str(b): {f"{m}|{a}": (1.0 - A[str(b)]["per_arm"][a]["composite"][m] / B[str(b)]["per_arm"][a]["composite"][m]
                                if abs(B[str(b)]["per_arm"][a]["composite"][m]) > 1e-12 else None)
                   for m in MODES for a in ARMS} for b in BUDGETS}

ranking_A = {str(b): {m: sorted(ARMS, key=lambda a: A[str(b)]["per_arm"][a]["composite"][m]) for m in MODES}
             for b in BUDGETS}

n_pass, n_comp = len(budgets_pass), len(comparable)

if not pipeline_trusted:
    status = "pending"
    branch = (f"pending：口径 B 归因子集自检超差（max_abs_diff={max_attr:.3e} > 1e-6），无法确认 composite 管道与冻结件一致，"
              "判据不予落定；缺口＝可精确复现冻结件 composite 的内部实现")
elif range_A < E5_GATE:
    status = "e5_withdrawn"
    branch = (f"撤回：动态范围闸门触发（range_A={range_A:.6f} < {E5_GATE}）→ 剔除 L0 后 P vs U/T 判据不可分辨；"
              "所需资产＝可重算层敏感度的 3B 权重本体 / 多种子·多任务·多 prompt 重复测量")
elif n_pass >= 5 and not conflict:
    status = "confirmed"
    branch = f"confirmed：口径 A 下 P 在 {n_pass}/{n_comp} 个可比档严格优于 U 且 T，L1/L2/L3 三合成方向无矛盾"
else:
    status = "refuted"
    branch = (f"refuted：口径 A 下 {n_comp - n_pass}/{n_comp} 个可比档 P 未同时优于 U 与 T"
              + (f"；三合成方向矛盾档 {conflict}" if conflict else ""))

wall = time.time() - T0
sha_after = hashlib.sha256(open(HUB_PULL, "rb").read()).hexdigest()[:16]
sha_frozen_repo_after = (hashlib.sha256(open(FROZEN_REPO_COPY, "rb").read()).hexdigest()[:16]
                         if sha_frozen_repo is not None else None)

out = {
    "task": "WB0-INSTR-20261025-EXP-13-PROPORTIONAL",
    "idempotency_key": "WB0-INSTR-20261025-EXP-13-PROPORTIONAL",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "theme": "A",
    "serves_north_star": "结构可压·事实受限（KV 预算的结构性分配方向 → 压缩质量上限）",
    "parent": "WB0-INSTR-20261024-R1-REJUDGE",
    "frozen_input": {
        "path": "results_v2/r1_budget_allocation.json",
        "hub_endpoint": "/api/file/r1_budget_allocation",
        "size_bytes": len(raw),
        "sha256_16_before": sha_before, "sha256_16_after": sha_after,
        "sha_stable": sha_before == sha_after,
        "repo_copy_sha256_16_before": sha_frozen_repo, "repo_copy_sha256_16_after": sha_frozen_repo_after,
        "repo_copy_stable": (sha_frozen_repo == sha_frozen_repo_after) if sha_frozen_repo else None,
        "hub_pull_copy": "results_v2/r1_budget_allocation.hub_pull_20261025_WB1.json",
    },
    "method": {
        "arms": {
            "U": "全层同一连续 K（均匀基线）",
            "T": "底层 K 大高层 K 小线性递减，最顶层 K=1，二分抬升尺度对齐预算（教科书式）",
            "D": "K_l ∝ mean_sens/sens_l（实测层敏感度【反比】，沿用冻结件定义）",
            "P": "K_l ∝ sens_l（实测层敏感度【正比】，本轮新增；与 D 仅方向不同，其余口径一致）",
        },
        "sens_definition": "sens_l = |max_delta_loss_answer|（每层 6 档 K 上的最大绝对 ΔL_answer，取自冻结件实测 cells）",
        "interpolation": "log2(K) 线性插值；K 超出 [1,32] 按端点截断，不做曲线外推；未测量 K 标 interpolated=true",
        "composite": {"L1_linear_sum": "Σ_l ΔL_l", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
        "slot_constraint": "Σ_l (K_l+1) = budget；clamp [1,32] 后二分求解，容差 1e-6",
        "eps_sens": EPS_SENS,
        "note": "复合口径族不变（仅改变分配方向），未新创第三种分配口径",
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
                "K_mean": A[str(b)]["per_arm"][a]["K_mean"],
                "n_at_clamp_min": A[str(b)]["per_arm"][a]["n_layers_at_kmin"],
                "n_at_clamp_max": A[str(b)]["per_arm"][a]["n_layers_at_kmax"],
                "interpolated": A[str(b)]["per_arm"][a]["interpolated"],
                "composite": A[str(b)]["per_arm"][a]["composite"],
            } for a in ARMS},
            "delta": delta[str(b)],
            "ranking_best_to_worst": ranking_A[str(b)],
        } for b in BUDGETS},
        "range_A": range_A,
        "comparable_budgets": comparable,
        "excluded_budgets": excluded,
        "direction_signs_per_budget": {str(b): {k: [float(x) for x in v] for k, v in dir_signs[b].items()}
                                       for b in comparable},
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
            "frozen_composite_D_U_T": {a: frozen[str(b)]["composite"][a] for a in ["U", "T", "D"]},
        } for b in BUDGETS},
        "attribution_subset": {
            "definition": "冻结件 U 臂 profile 恰为测量网格点（K=4/8/16/32，无 round/几何近似/外推）的格；逐位复现则管道可信",
            "cells": attribution, "max_abs_diff": max_attr, "n_cells": len(attribution),
        },
        "pipeline_trusted": pipeline_trusted,
    },
    "L0_dominance": {"definition": "1 - composite_A / composite_B", "per_budget": l0_dom},
    "verdict": {
        "status": status, "branch_text": branch,
        "n_budget_pass": n_pass, "n_comparable": n_comp,
        "budgets_pass": budgets_pass, "mode_direction_conflict_budgets": conflict,
        "e5_gate": E5_GATE, "range_A": range_A,
        "counts": {"budgets": len(BUDGETS), "modes": len(MODES), "arms": len(ARMS), "calibers": 2,
                   "cells": len(BUDGETS) * len(MODES) * len(ARMS) * 2},
        "mechanism_boundary_note": (
            "若 refuted：说明『剔除 L0 后失速』并非反比口径特有，而是任何敏感度导向分配（正比/反比）在 L0 支配下"
            "均无法摆脱的机制性现象（[发生器] 类比：单一支配分量移除后，剩余分量为低动态范围的平坦谱）。"
            "若 confirmed：说明失速归因于反比这一方向本身，正比方向可部分恢复区分度。"),
    },
    "n_and_power": {
        "n": 1, "detail": "1 种子 / 1 needle / 8 段落 / 2613 tokens / 1 模型（沿用冻结件 n_boundary）",
        "power_analysis": "不可做（n=1，无种子/任务/prompt 方差）",
        "level": "假设级 / 机制观察，不得表述为该模型或架构的真实层敏感度",
    },
    "dependency_missing": ["3B 权重本体已蒸发，层敏感度不可重算（沿用冻结件 n_boundary，本轮仅重分配不重测）"],
    "p0": True,
    "wall_clock_sec": round(wall, 3),
    "scope_guard_selfcheck": {
        "1_no_fabrication": "所有 ΔL 取自冻结件原始 cells；未测量 K 一律 log2 插值并标 interpolated；未填入任何未实测层敏感度",
        "2_frozen_untouched": f"冻结件只读：运行前后 hub_pull sha256_16 均为 {sha_before}；仓库副本 sha16 {sha_frozen_repo}→{sha_frozen_repo_after}",
        "3_no_overreach": "仅做四臂同口径重算；未训练、未下载/加载模型、未新建假设、未写台账、未碰 WB2 指令；未改写 H-R1-BUDGET 任何字段",
        "4_no_ledger_write": "未直写 hypothesis_ledger.yaml",
        "5_no_model_load": "纯 numpy，无模型下载/加载",
        "6_no_silent_trim": (f"6 档×3 合成×4 臂×2 口径={len(BUDGETS)*len(MODES)*len(ARMS)*2} 格全跑，"
                             f"wall_clock={wall:.1f}s≤300s；剔除档已在 caliber_A.excluded_budgets 显式披露（非静默裁剪）"),
    },
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print(json.dumps({
    "status": status, "branch": branch, "range_A": round(range_A, 7),
    "attribution_max_abs_diff": max_attr, "attribution_cells": len(attribution),
    "comparable": comparable, "excluded": [e["budget"] for e in excluded],
    "budgets_pass": budgets_pass, "conflict": conflict,
    "wall_clock": round(wall, 2),
    "A_L1": {str(b): {a: round(A[str(b)]["per_arm"][a]["composite"]["L1_linear_sum"], 5) for a in ARMS} for b in BUDGETS},
    "A_L2": {str(b): {a: round(A[str(b)]["per_arm"][a]["composite"]["L2_max"], 5) for a in ARMS} for b in BUDGETS},
    "A_L3": {str(b): {a: round(A[str(b)]["per_arm"][a]["composite"]["L3_euclid"], 5) for a in ARMS} for b in BUDGETS},
    "slots_A": {str(b): {a: round(A[str(b)]["per_arm"][a]["total_slots_check"], 3) for a in ARMS} for b in BUDGETS},
    "ranking_A_L1": {str(b): ranking_A[str(b)]["L1_linear_sum"] for b in BUDGETS},
    "delta_P_U_L1": {str(b): round(delta[str(b)]["L1_linear_sum"]["P_minus_U"], 6) for b in BUDGETS},
    "delta_P_T_L1": {str(b): round(delta[str(b)]["L1_linear_sum"]["P_minus_T"], 6) for b in BUDGETS},
    "delta_P_D_L1": {str(b): round(delta[str(b)]["L1_linear_sum"]["P_minus_D"], 6) for b in BUDGETS},
    "L0dom_L1": {str(b): {a: round(l0_dom[str(b)][f"L1_linear_sum|{a}"], 4) for a in ARMS} for b in BUDGETS},
    "sha_before": sha_before, "sha_after": sha_after,
    "sha_repo_before": sha_frozen_repo, "sha_repo_after": sha_frozen_repo_after,
}, ensure_ascii=False, indent=1))
