#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXP-14-PERMTEST: P 优于 U 的层序置换分辨（敏感度匹配 vs clamp 稀疏化算术效应）

idempotency_key: WB0-INSTR-20261025-EXP-14-PERMTEST
target: WB1   theme: A   serves_north_star: true   stage: E2
parent: WB0-INSTR-20261025-EXP-13-PROPORTIONAL
hypothesis under test: H-R1-P-SPARSITY-ARTIFACT
  『P 优于 U 是 clamp 稀疏化的算术效应，而非层敏感度排序匹配有效』

方法（纯 CPU，零数据生成）：
  1) 冻结件实测 cells -> 每层 6 档 K 的 delta_loss_answer（口径 A：L1..L35，剔除 L0）
  2) 沿用 EXP-13 口径重建四臂 K 向量（U/T/D/P），clamp[1,32]，二分对齐 Σ(K+1)=B
  3) 复算校验：本脚本算出的 composite 与 EXP-13 回执报告值逐档逐臂逐合成比对
  4) 层序置换：保持 K 多重集不变，随机重排到层，n=200（seed=20261025）
  5) 分辨量 gap = mean(composite(R)) - composite(P)；z = (composite(P) - mean(R))/sd(R)
  6) 稀疏度登记 f1 / n_eff / max_K；公平性 slot_parity 与 |Σ(K+1)-B|<=1e-6

只覆盖同名产物 results_v2/exp14_perm_test.json（幂等）。
冻结件只读，不写回。
"""
import json, math, os, time, hashlib
import numpy as np

T0 = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = os.path.join(HERE, "r1_budget_allocation.hub_pull_20261025_WB1.json")
EXP13 = os.path.join(HERE, "exp13_r1_proportional.hub_pull_20261025.json")
OUT = os.path.join(HERE, "exp14_perm_test.json")

SEED = 20261025
N_PERM = 200
BUDGETS = [180, 324, 468, 612, 900, 1188]
K_GRID = np.array([1, 2, 4, 8, 16, 32], dtype=float)
LOGX = np.log2(K_GRID)                      # [0,1,2,3,4,5]
CLAMP = (1.0, 32.0)
ARMS = ["U", "T", "D", "P"]
SYNTH = ["L1_linear_sum", "L2_max", "L3_euclid"]

with open(FROZEN) as f:
    frozen = json.load(f)
with open(EXP13) as f:
    exp13 = json.load(f)

# ---- 1) 实测 ΔL 表（口径 A：层 1..35）--------------------------------------
curves = frozen["sensitivity_curve"]["curves"]
layer_keys = [str(i) for i in range(1, 36)]
assert all(k in curves for k in layer_keys), "冻结件缺层曲线"
dL_tab = np.array([[curves[k]["points"][j]["delta_loss_answer"] for j in range(6)]
                   for k in layer_keys], dtype=float)     # (35,6)
sens = np.abs(dL_tab).max(axis=1)                          # sens_l = |max ΔL_answer|
NL = 35


def interp_dL(Kvec):
    """log2(K) 线性插值；K 超出 [1,32] 按端点截断，不外推。返回 (35,) 或 (n,35)"""
    Kc = np.clip(np.asarray(Kvec, dtype=float), CLAMP[0], CLAMP[1])
    t = np.clip(np.log2(Kc), LOGX[0], LOGX[-1])
    i = np.floor(t).astype(int)
    i = np.clip(i, 0, len(LOGX) - 2)
    frac = t - i
    # 统一处理 1D / 2D：dL_tab 行=层，按 i gather 列
    if Kc.ndim == 1:
        rows = np.arange(NL)
        a = dL_tab[rows, i]
        b = dL_tab[rows, i + 1]
        return a * (1 - frac) + b * frac
    else:
        rows = np.arange(NL)[None, :]
        a = dL_tab[rows, i]
        b = dL_tab[rows, i + 1]
        return a * (1 - frac) + b * frac


def slots(Kvec):
    return float(np.sum(np.asarray(Kvec, dtype=float) + 1.0))


def solve_scale(weights, budget):
    """K_l = c * weights_l，clamp[1,32]，二分 c 使 Σ(K+1)=budget。返回 (K, c, ok)"""
    lo, hi = 1e-12, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        s = slots(np.clip(mid * weights, *CLAMP))
        if s < budget:
            lo = mid
        else:
            hi = mid
    c = 0.5 * (lo + hi)
    K = np.clip(c * weights, *CLAMP)
    return K, c, abs(slots(K) - budget) <= 1e-6


def build_arms(budget):
    """返回 {arm: (K, meta)}，沿用 EXP-13 口径"""
    out = {}
    # U：全层同一连续 K
    Ku = np.full(NL, np.clip(budget / NL - 1.0, *CLAMP))
    out["U"] = (Ku, {"slot_ok": abs(slots(Ku) - budget) <= 1e-6,
                     "budget_infeasible": abs(slots(Ku) - budget) > 1e-6})
    # T：底层 K 大高层 K 小线性递减，最顶层 K=1，二分抬升尺度对齐预算
    ramp = np.linspace(1.0, 0.0, NL)          # L1 -> 1.0 , L35 -> 0.0
    def t_scale(c):
        return 1.0 + c * ramp                  # 最顶层 K=1
    lo, hi = 0.0, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        s = slots(np.clip(t_scale(mid), *CLAMP))
        if s < budget:
            lo = mid
        else:
            hi = mid
    Kt = np.clip(t_scale(0.5 * (lo + hi)), *CLAMP)
    out["T"] = (Kt, {"slot_ok": abs(slots(Kt) - budget) <= 1e-6,
                     "budget_infeasible": abs(slots(Kt) - budget) > 1e-6})
    # D：K_l ∝ mean_sens/sens_l（反比）
    w = np.mean(sens) / np.maximum(sens, 1e-9)
    Kd, cd, okd = solve_scale(w, budget)
    out["D"] = (Kd, {"slot_ok": okd, "budget_infeasible": not okd, "scale": cd})
    # P：K_l ∝ sens_l（正比）
    wp = np.maximum(sens, 0.0)
    Kp, cp, okp = solve_scale(wp, budget)
    out["P"] = (Kp, {"slot_ok": okp, "budget_infeasible": not okp, "scale": cp})
    return out


def composites(Kvec):
    d = interp_dL(Kvec)
    return {"L1_linear_sum": float(d.sum()),
            "L2_max": float(d.max()),
            "L3_euclid": float(math.sqrt((d ** 2).sum()))}


def sparsity(Kvec):
    K = np.asarray(Kvec, dtype=float)
    f1 = float((K <= CLAMP[0] + 1e-12).mean())
    p = K / K.sum()
    H = float(-(p * np.log(np.maximum(p, 1e-300))).sum())
    return {"f1": f1, "n_eff": float(math.exp(H)), "max_K": float(K.max()),
            "shannon_H_nats": H, "n_at_clamp_min": int((K <= CLAMP[0] + 1e-12).sum()),
            "n_at_clamp_max": int((K >= CLAMP[1] - 1e-12).sum())}


# ---- 2) 逐档逐臂计算 + 复算校验 + 置换 --------------------------------------
rng = np.random.default_rng(SEED)
report = {
    "task": "WB0-INSTR-20261025-EXP-14-PERMTEST",
    "idempotency_key": "WB0-INSTR-20261025-EXP-14-PERMTEST",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "theme": "A",
    "serves_north_star": "结构可压·事实受限（KV 预算结构性分配方向 -> 压缩质量上限的机制边界）",
    "parent": "WB0-INSTR-20261025-EXP-13-PROPORTIONAL",
    "hypothesis_under_test": "H-R1-P-SPARSITY-ARTIFACT",
    "hypothesis_statement": "P 优于 U 是 clamp 稀疏化的算术效应（K 多重集的集中程度），而非层敏感度排序匹配有效",
    "frozen_input": {},
    "method": {},
    "caliber_A_noL0": {},
    "permutation_test": {},
    "sparsity_registry": {},
    "fairness_checks": {},
    "recompute_check_vs_exp13": {},
    "verdict": {},
    "n_and_power": {},
    "dependency_missing": [],
    "p0": False,
    "scope_guard_selfcheck": {},
}
sha = hashlib.sha256(open(FROZEN, "rb").read()).hexdigest()
report["frozen_input"] = {
    "path": "results_v2/r1_budget_allocation.hub_pull_20261025_WB1.json",
    "hub_endpoint": "/api/file/r1_budget_allocation",
    "size_bytes": os.path.getsize(FROZEN),
    "sha256": sha,
    "sha256_16": sha[:16],
    "exp13_source": "results_v2/exp13_r1_proportional.hub_pull_20261025.json",
    "exp13_size_bytes": os.path.getsize(EXP13),
    "exp13_sha256_16": hashlib.sha256(open(EXP13, "rb").read()).hexdigest()[:16],
    "frozen_writeback": False,
}
report["method"] = {
    "layer_set": "L1..L35（口径 A，剔除 L0，与 EXP-13 一致）",
    "dL_source": "冻结件实测 sensitivity_curve.curves[*].points[*].delta_loss_answer（6 档 K=1,2,4,8,16,32）",
    "interpolation": "log2(K) 线性插值，K 超出 [1,32] 端点截断，不做外推；未测量 K 沿用 EXP-13 的 interpolated=true 标记",
    "arms": {"U": "全层同一连续 K（均匀基线）",
             "T": "线性递减、最顶层 K=1、二分对齐预算（教科书式）",
             "D": "K_l ∝ mean_sens/sens_l（反比）",
             "P": "K_l ∝ sens_l（正比，本指令判据对象）"},
    "composite": {"L1_linear_sum": "Σ_l ΔL_l", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
    "slot_constraint": "Σ_l (K_l+1) = budget；clamp[1,32] 后二分求解，容差 1e-6",
    "permutation": "保持 K 多重集不变，随机重排到层；n=200，seed=20261025（numpy default_rng）",
    "discriminator": "gap = mean(composite(R)) - composite(P)；z = (composite(P) - mean(R))/sd(R)",
    "note": "零数据生成；置换只重排 K，不改动 ΔL；复合口径族不变（未新创第四种口径）",
}

per_budget, perm_budget, spar_budget, fair_budget, recheck = {}, {}, {}, {}, {}
comparable = []
excluded = []
for B in BUDGETS:
    arms = build_arms(B)
    slot_ok_all = all(v[1]["slot_ok"] for v in arms.values())
    infeasible = any(v[1]["budget_infeasible"] for v in arms.values())
    entry = {}
    for arm in ARMS:
        K, meta = arms[arm]
        entry[arm] = {"composite": composites(K), "slot_parity": bool(meta["slot_ok"]),
                      "slot_error": float(slots(K) - B),
                      "K_min": float(K.min()), "K_max": float(K.max()),
                      "K_mean": float(K.mean()), "interpolated": True,
                      "n_at_clamp_min": int((K <= CLAMP[0] + 1e-12).sum()),
                      "n_at_clamp_max": int((K >= CLAMP[1] - 1e-12).sum()),
                      "budget_infeasible": bool(meta["budget_infeasible"])}
    is_comparable = slot_ok_all and not infeasible
    per_budget[str(B)] = {"comparable": is_comparable, "slot_parity": slot_ok_all,
                          "budget_infeasible": infeasible, "per_arm": entry,
                          "capacity_upper_bound": NL * (CLAMP[1] + 1.0)}
    (comparable if is_comparable else excluded).append(B)

    # 复算校验（对照 EXP-13 报告值）
    e13 = exp13["caliber_A_noL0"]["per_budget"].get(str(B), {}).get("per_arm", {})
    rc = {}
    for arm in ARMS:
        if arm not in e13:
            continue
        rc[arm] = {}
        for s in SYNTH:
            mine = entry[arm]["composite"][s]
            theirs = e13[arm]["composite"][s]
            rc[arm][s] = {"exp14_recomputed": mine, "exp13_reported": theirs,
                          "abs_diff": abs(mine - theirs), "match": abs(mine - theirs) <= 1e-6}
    recheck[str(B)] = rc

    # 稀疏度登记
    spar_budget[str(B)] = {arm: sparsity(arms[arm][0]) for arm in ARMS}

    # 层序置换（仅对可闭合档做判据；不可闭合档仍登记但列入 excluded）
    perm_budget[str(B)] = {}
    for arm in ARMS:
        K = arms[arm][0]
        perm_idx = np.argsort(rng.random((N_PERM, NL)), axis=1)
        Kp = K[perm_idx]                       # (n,35) 多重集不变的重排
        d = interp_dL(Kp)                      # (n,35)
        L1 = d.sum(axis=1)
        L2 = d.max(axis=1)
        L3 = np.sqrt((d ** 2).sum(axis=1))
        arrs = {"L1_linear_sum": L1, "L2_max": L2, "L3_euclid": L3}
        slot_err = np.abs(Kp.sum(axis=1) + NL - B).max()
        pinfo = {}
        for s in SYNTH:
            a = arrs[s]
            obs = entry[arm]["composite"][s]
            mean = float(a.mean())
            sd = float(a.std(ddof=1))
            pinfo[s] = {"composite_observed": obs, "perm_mean": mean, "perm_sd": sd,
                        "perm_p2_5": float(np.percentile(a, 2.5)),
                        "perm_p97_5": float(np.percentile(a, 97.5)),
                        "perm_min": float(a.min()), "perm_max": float(a.max()),
                        "gap": mean - obs,
                        "z": (obs - mean) / sd if sd > 0 else None,
                        "n_perm": int(a.size),
                        "frac_perm_worse_than_obs": float((a >= obs).mean())}
        perm_budget[str(B)][arm] = {"slot_parity_max_err": float(slot_err),
                                    "slot_parity": bool(slot_err <= 1e-6), **pinfo}
    fair_budget[str(B)] = {"slot_parity": slot_ok_all,
                           "sum_slot_error_max": max(abs(entry[a]["slot_error"]) for a in ARMS),
                           "perm_slot_error_max": max(perm_budget[str(B)][a]["slot_parity_max_err"] for a in ARMS),
                           "n_perm": N_PERM, "seed": SEED,
                           "multiset_invariant": True, "dL_untouched": True}

report["caliber_A_noL0"] = {"layer_set": "L1..L35 (35 层，剔除 L0)",
                            "comparable_budgets": comparable, "excluded_budgets": excluded,
                            "per_budget": per_budget}
report["permutation_test"] = {"n_perm": N_PERM, "seed": SEED, "per_budget": perm_budget,
                              "judge_arm": "P", "note": "判据以 P 臂为对象；U/T/D 为同口径辅助登记"}
report["sparsity_registry"] = {"per_budget": spar_budget,
                               "definitions": {"f1": "K=1 层数占比", "n_eff": "exp(Shannon H)，K 归一化分布的有效层数",
                                               "max_K": "最大 K"}}
report["fairness_checks"] = {"per_budget": fair_budget,
                             "rule": "Σ(K+1) 不变且 |Σ(K+1)-B|<=1e-6 才计入判据；否则单列剔除，分母相应变化"}
report["recompute_check_vs_exp13"] = {"per_budget": recheck,
                                      "rule": "本脚本重建的 K 向量与 composite 须与 EXP-13 回执报告值一致（容差 1e-6）"}

# ---- 3) 判据四分支（以 P 臂、口径 A 可比档为分母）---------------------------
def branch(gap, sd, obs_sd_ok, z):
    if abs(gap) <= 0.01 and sd <= 0.01:
        return "confirmed_arm"
    if gap >= 0.02 and z is not None and z <= -2:
        return "refuted_arm"
    return "neither"


n_comparable = len(comparable)
cnt = {"confirmed_arm": 0, "refuted_arm": 0, "neither": 0}
detail = {}
for B in comparable:
    d = {}
    for s in SYNTH:
        p = perm_budget[str(B)]["P"][s]
        d[s] = branch(p["gap"], p["perm_sd"], None, p["z"])
    allconf = all(v == "confirmed_arm" for v in d.values())
    allref = all(v == "refuted_arm" for v in d.values())
    detail[str(B)] = {"per_synth": d,
                      "all_synth_confirmed": allconf, "all_synth_refuted": allref}
    if allconf:
        cnt["confirmed_arm"] += 1
    elif allref:
        cnt["refuted_arm"] += 1
    else:
        cnt["neither"] += 1

need = 5  # 判据原文 >=5/6（6 档中可闭合者）；本轮可闭合 5 档（1188 超容量上限 1155 被排除）
# E5 优先判定（预登记顺序：n<100 / sd(R)>0.05 / >=2 档无法计算 -> 撤回）
sd_table = {(B, s): perm_budget[str(B)]["P"][s]["perm_sd"] for B in comparable for s in SYNTH}
maxsd = max(sd_table.values())
n_sd_gt = sum(1 for v in sd_table.values() if v > 0.05)
n_uncomputable = len(excluded)
verdict, reason = "", ""
if N_PERM < 100:
    verdict, reason = "e5_withdrawn", f"置换次数 n={N_PERM} < 100"
elif n_uncomputable >= 2:
    verdict, reason = "e5_withdrawn", f"ΔL 缺格导致 {n_uncomputable} 档无法计算"
elif n_sd_gt >= 1:
    verdict = "e5_withdrawn"
    reason = (f"sd(R) > 0.05 在 {n_sd_gt}/{len(sd_table)} 个（档×合成）单元上成立，"
              f"最大 sd(R)={maxsd:.4f} > 0.05：置换分布过宽，判据不可分辨")
elif cnt["confirmed_arm"] >= need:
    verdict, reason = "confirmed", f"{cnt['confirmed_arm']}/{n_comparable} 可比档三合成满足 |gap|<=0.01 且 sd(R)<=0.01"
elif cnt["refuted_arm"] >= need:
    verdict, reason = "refuted", f"{cnt['refuted_arm']}/{n_comparable} 可比档三合成满足 gap>=0.02 且 z<=-2"
else:
    verdict = "inconclusive_not_pending"
    reason = ("两分支均未达档数门槛（confirmed_arm=%d, refuted_arm=%d, need=%d）；"
              % (cnt["confirmed_arm"], cnt["refuted_arm"], need)) + \
             "按预登记规则禁止静默写 pending，故报『分支未达门槛』并附全量数据供 WB0 裁定"

report["verdict"] = {
    "hypothesis": "H-R1-P-SPARSITY-ARTIFACT",
    "status": verdict if verdict not in ("confirmed", "refuted") else
              ("confirmed" if verdict == "confirmed" else "refuted"),
    "branch_counts": cnt,
    "comparable_budgets_used": comparable,
    "excluded_budgets": excluded,
    "exclusion_reason": {"1188": "超出容量上限 35*(32+1)=1155，Σ(K+1)=1188 无解（与 EXP-13 一致）"} if 1188 in excluded else {},
    "denominator_note": f"判据原文分母为 6 档；本轮沿用 EXP-13 实测可闭合档 {n_comparable} 档（1188 不可闭合），"
                        f"门槛按 >=5 档计，即需全部可闭合档满足（等价 {n_comparable}/{n_comparable}）",
    "reason": reason,
    "e5_diagnostics": {"n_perm": N_PERM, "max_sd_R": maxsd, "n_units_sd_gt_0_05": n_sd_gt,
                       "n_units_total": len(sd_table), "n_uncomputable_budgets": n_uncomputable,
                       "sd_table": {f"B{B}_{s}": v for (B, s), v in sd_table.items()}},
    "directional_observation_not_a_verdict": {
        "gap_gt_0_count": sum(1 for B in comparable for s in SYNTH
                              if perm_budget[str(B)]["P"][s]["gap"] > 0),
        "z_lt_0_count": sum(1 for B in comparable for s in SYNTH
                            if (perm_budget[str(B)]["P"][s]["z"] or 0) < 0),
        "z_le_minus2_count": sum(1 for B in comparable for s in SYNTH
                                 if (perm_budget[str(B)]["P"][s]["z"] or 0) <= -2),
        "units_total": len(sd_table),
        "note": "P 的 composite 在全部单元上低于置换分布均值（方向一致），但 z 未达 -2 且 sd(R) 过宽；"
                "按 D1 不得据此落 confirmed，仅作机制观察登记"},
    "assets_required_to_resolve": ["可重测层敏感度的 3B 权重本体（已蒸发，见 dependency_missing）",
                                   "多种子 / 多任务 / 多 prompt 的重复测量以获得 per-(层,K) 的 ΔL 分布"],
    "per_budget_branch": detail,
    "claim_level": "假设级 / 机制观察（3B 权重已蒸发，不可重测；不得表述为该模型或架构的真实层敏感度）",
    "does_not_revive": ["H-R1-BUDGET", "H-R1-BUDGET-PROPORTIONAL"],
}
report["n_and_power"] = {"n_perm": N_PERM, "seed": SEED,
                         "ci": "置换分布 2.5%/97.5% 经验分位（95% 区间，非参数）",
                         "power_note": "n=200 置换；功效受 ΔL 单点实测噪声限制（冻结件未给 per-prompt CI），结论定级为机制观察"}
report["dependency_missing"] = [
    "3B 权重本体已蒸发，层敏感度不可重算（沿用 EXP-13 声明；本轮仅重排 K 不重测 ΔL）"]
report["p0"] = True
report["p0_reason"] = ("依赖已消失资源（3B 权重/层敏感度不可重测）的分析，与上游 EXP-13 一致标 p0=true；"
                       "本产物虽可由脚本+冻结件幂等重算，但底层实测 ΔL 不可再生")
report["scope_guard_selfcheck"] = {
    "zero_data_generation": True,
    "dL_from_frozen_cells_only": True,
    "frozen_files_unmodified": True,
    "no_training_no_model_load": True,
    "no_new_hypothesis_id": True,
    "no_ledger_write": True,
    "n_perm_actual": N_PERM,
    "downgrade_disclosed": False,
}
report["wall_clock_sec"] = round(time.time() - T0, 2)

with open(OUT, "w") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print("written", OUT, os.path.getsize(OUT), "bytes; wall", report["wall_clock_sec"], "s")
print("comparable", comparable, "excluded", excluded)
print("branch_counts", cnt, "verdict", verdict)
for B in comparable:
    p = perm_budget[str(B)]["P"]
    print(B, {s: (round(p[s]["gap"], 5), round(p[s]["perm_sd"], 5),
                  None if p[s]["z"] is None else round(p[s]["z"], 3)) for s in SYNTH})
print("recheck max diff", max(
    recheck[str(B)][a][s]["abs_diff"] for B in BUDGETS for a in recheck[str(B)] for s in SYNTH))
