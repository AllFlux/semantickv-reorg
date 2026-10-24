#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXP-16-SPARSE-MATCHED: 稀疏度匹配对照族 —— 检验 EXP-14 的 E5 可否转为可判定

idempotency_key: WB0-INSTR-20261025-EXP-16-SPARSE-MATCHED
target: WB1   theme: A   serves_north_star: true   stage: E2
parent: WB0-INSTR-20261025-EXP-14-PERMTEST
hypothesis under test: H-R1-P-SPARSITY-ARTIFACT（沿用，不新建、不复活 H-R1-BUDGET*）

方法（纯 CPU，零数据生成，零模型加载）：
  1) 沿用 EXP-13/14 口径：冻结件实测 ΔL 表（L1..L35，6 档 K），log2 插值，clamp[1,32]，
     二分对齐 Σ(K+1)=B；三合成 L1/L2/L3。
  2) 族 A（必做，n=200，seed=20261026）：稀疏度匹配随机对照族。
     保持 P 臂的 clamp(=1) 层数 n1，free 层用「P 的 free 多重集」与「log-uniform(1,32)
     随机多重集」的凸组合（λ 为均衡度），再二分缩放 + clamp 使 Σ(K+1)=B 精确闭合；
     λ 经标定使整臂 n_eff 与 P 匹配（相对误差 ≤5%），f1 同目标（相对误差 ≤5%）。
     每次样本独立采样并随机重排到层；不满足「预算闭合 + f1/n_eff 匹配」的样本重采
     （最多 20 次），仍不满足者不计入判据并显式披露（禁止静默缩样）。
  3) 族 B（选做）：受限置换子空间 —— P 自身多重集的重排中 n_eff 偏差 ≤5% 者占比。
     （重排不改变多重集，故 n_eff 恒等，占比预期 =100%，该退化本身即诊断结论）
  4) 族 C（选做）：对族 A 的 composite(R') 做 n=1000 自助重采样，报 z' 的 95% CI 上界。
  5) 分辨量：gap' = mean(composite(R')) - composite(P)；z' = (composite(P) - mean(R'))/sd(R')

只覆盖同名产物 results_v2/exp16_sparse_matched.json（幂等）。冻结件只读，不写回。
"""
import json, math, os, time, hashlib
import numpy as np

T0 = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = os.path.join(HERE, "r1_budget_allocation.hub_pull_20261025_WB1.json")
EXP13 = os.path.join(HERE, "exp13_r1_proportional.hub_pull_20261025.json")
EXP14 = os.path.join(HERE, "exp14_perm_test.json")
OUT = os.path.join(HERE, "exp16_sparse_matched.json")

SEED = 20261026
N_FAM = 200
N_BOOT = 1000
BUDGETS = [180, 324, 468, 612, 900, 1188]
K_GRID = np.array([1, 2, 4, 8, 16, 32], dtype=float)
LOGX = np.log2(K_GRID)
CLAMP = (1.0, 32.0)
ARMS = ["U", "T", "D", "P"]
SYNTH = ["L1_linear_sum", "L2_max", "L3_euclid"]
EPS = 1e-12

with open(FROZEN) as f:
    frozen = json.load(f)
with open(EXP13) as f:
    exp13 = json.load(f)
with open(EXP14) as f:
    exp14 = json.load(f)

curves = frozen["sensitivity_curve"]["curves"]
layer_keys = [str(i) for i in range(1, 36)]
assert all(k in curves for k in layer_keys), "冻结件缺层曲线"
dL_tab = np.array([[curves[k]["points"][j]["delta_loss_answer"] for j in range(6)]
                   for k in layer_keys], dtype=float)
sens = np.abs(dL_tab).max(axis=1)
NL = 35


# ---- 口径函数（与 EXP-13/14 逐字一致）--------------------------------------
def interp_dL(Kvec):
    Kc = np.clip(np.asarray(Kvec, dtype=float), CLAMP[0], CLAMP[1])
    t = np.clip(np.log2(Kc), LOGX[0], LOGX[-1])
    i = np.clip(np.floor(t).astype(int), 0, len(LOGX) - 2)
    frac = t - i
    if Kc.ndim == 1:
        rows = np.arange(NL)
        return dL_tab[rows, i] * (1 - frac) + dL_tab[rows, i + 1] * frac
    rows = np.arange(NL)[None, :]
    return dL_tab[rows, i] * (1 - frac) + dL_tab[rows, i + 1] * frac


def slots(Kvec):
    return float(np.sum(np.asarray(Kvec, dtype=float) + 1.0))


def solve_scale(weights, budget):
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
    out = {}
    Ku = np.full(NL, np.clip(budget / NL - 1.0, *CLAMP))
    out["U"] = (Ku, {"slot_ok": abs(slots(Ku) - budget) <= 1e-6,
                     "budget_infeasible": abs(slots(Ku) - budget) > 1e-6})
    ramp = np.linspace(1.0, 0.0, NL)

    def t_scale(c):
        return 1.0 + c * ramp
    lo, hi = 0.0, 1e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if slots(np.clip(t_scale(mid), *CLAMP)) < budget:
            lo = mid
        else:
            hi = mid
    Kt = np.clip(t_scale(0.5 * (lo + hi)), *CLAMP)
    out["T"] = (Kt, {"slot_ok": abs(slots(Kt) - budget) <= 1e-6,
                     "budget_infeasible": abs(slots(Kt) - budget) > 1e-6})
    w = np.mean(sens) / np.maximum(sens, 1e-9)
    Kd, cd, okd = solve_scale(w, budget)
    out["D"] = (Kd, {"slot_ok": okd, "budget_infeasible": not okd})
    wp = np.maximum(sens, 0.0)
    Kp, cp, okp = solve_scale(wp, budget)
    out["P"] = (Kp, {"slot_ok": okp, "budget_infeasible": not okp})
    return out


def composites(Kvec):
    d = interp_dL(Kvec)
    return {"L1_linear_sum": float(d.sum()), "L2_max": float(d.max()),
            "L3_euclid": float(math.sqrt((d ** 2).sum()))}


def sparsity(Kvec):
    K = np.asarray(Kvec, dtype=float)
    p = K / K.sum()
    H = float(-(p * np.log(np.maximum(p, 1e-300))).sum())
    return {"f1": float((K <= CLAMP[0] + 1e-9).mean()), "n_eff": float(math.exp(H)),
            "max_K": float(K.max()), "shannon_H_nats": H,
            "n_at_clamp_min": int((K <= CLAMP[0] + 1e-9).sum()),
            "n_at_clamp_max": int((K >= CLAMP[1] - 1e-9).sum())}


def align_sum(mix, target):
    """二分缩放 c 使 Σ clamp(c*mix) = target（clamp 后连续单调，可精确闭合）"""
    lo, hi = 1e-9, 1e9
    for _ in range(90):
        mid = 0.5 * (lo + hi)
        if np.clip(mid * mix, *CLAMP).sum() < target:
            lo = mid
        else:
            hi = mid
    return np.clip(0.5 * (lo + hi) * mix, *CLAMP)


# ---- 族 A 构造 ---------------------------------------------------------------
def sample_family_A(rng, Kp_free, n1, R, lam):
    """返回一个稀疏度匹配、预算闭合、随机重排到层的 K 向量 (35,)"""
    u = 2.0 ** rng.uniform(0.0, 5.0, size=Kp_free.size)      # log-uniform(1,32)
    mix = (1.0 - lam) * Kp_free + lam * u
    Kfree = align_sum(mix, R)
    K = np.concatenate([np.ones(n1), Kfree])
    rng.shuffle(K)
    return K


report = {
    "task": "WB0-INSTR-20261025-EXP-16-SPARSE-MATCHED",
    "idempotency_key": "WB0-INSTR-20261025-EXP-16-SPARSE-MATCHED",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "theme": "A",
    "serves_north_star": "结构可压·事实受限（KV 预算结构性分配方向 -> 压缩质量上限的机制边界）",
    "parent": "WB0-INSTR-20261025-EXP-14-PERMTEST",
    "hypothesis_under_test": "H-R1-P-SPARSITY-ARTIFACT",
    "hypothesis_statement": "P 优于 U 是 clamp 稀疏化的算术效应（K 多重集的集中程度），而非层敏感度排序匹配有效",
    "frozen_input": {
        "path": "results_v2/r1_budget_allocation.hub_pull_20261025_WB1.json",
        "hub_endpoint": "/api/file/r1_budget_allocation",
        "size_bytes": os.path.getsize(FROZEN),
        "sha256": hashlib.sha256(open(FROZEN, "rb").read()).hexdigest(),
        "exp14_source": "results_v2/exp14_perm_test.json（本轮实拉 /api/file/r2_exp14_perm_test，83062B）",
        "exp14_size_bytes": os.path.getsize(EXP14),
        "exp13_size_bytes": os.path.getsize(EXP13),
        "frozen_writeback": False,
    },
    "method": {
        "layer_set": "L1..L35（口径 A，剔除 L0，与 EXP-13/14 一致）",
        "dL_source": "冻结件实测 sensitivity_curve.curves[*].points[*].delta_loss_answer（6 档 K=1,2,4,8,16,32）",
        "interpolation": "log2(K) 线性插值，K 超出 [1,32] 端点截断，不外推；未测量 K 沿用 interpolated=true",
        "family_A": "稀疏度匹配随机对照族：保留 P 臂 clamp(=1) 层数 n1；free 层 = (1-λ)·P_free 多重集 + λ·log-uniform(1,32)，"
                    "二分缩放+clamp 使 Σ(K+1)=B 精确闭合，随机重排到层；λ 标定至 n_eff 与 P 的相对误差 ≤5%",
        "family_A_sparsity_match_rule": "f1 相对误差 = |f1'-f1_P| / max(f1_P, 1/35)（f1_P=0 时以 1 层为分母单位）；"
                                        "n_eff 相对误差 = |n_eff'-n_eff_P| / n_eff_P；两者均须 ≤5%",
        "family_B": "受限置换：P 自身多重集的全部重排中 n_eff 偏差 ≤5% 者占比（重排不改变多重集 → n_eff 恒等）",
        "family_C": "对族 A 的 composite(R') 做 n=1000 自助重采样，报 z' 的 95% 分位 CI（2.5%/97.5%）",
        "composite": {"L1_linear_sum": "Σ_l ΔL_l", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
        "slot_constraint": "Σ_l (K_l+1) = budget；容差 1e-6；不可闭合档（1188）单列剔除，分母相应变化",
        "discriminator": "gap' = mean(composite(R')) - composite(P)；z' = (composite(P) - mean(R'))/sd(R')",
        "n_and_seed": {"n_family_A": N_FAM, "seed": SEED, "rng": "numpy default_rng",
                       "n_bootstrap": N_BOOT, "resample_cap_per_sample": 20},
        "note": "零数据生成；只构造 K 向量，ΔL 一律取冻结件实测；不训练、不加载模型；沿用三合成，未新创第四种口径",
    },
    "caliber_A_noL0": {},
    "family_A": {},
    "family_B": {},
    "family_C": {},
    "fairness_checks": {},
    "recompute_check_vs_exp14": {},
    "verdict": {},
}

comparable, excluded = [], []
per_budget, famA, famB, famC, fair, recheck = {}, {}, {}, {}, {}, {}

for B in BUDGETS:
    arms = build_arms(B)
    slot_ok_all = all(v[1]["slot_ok"] for v in arms.values())
    infeasible = any(v[1]["budget_infeasible"] for v in arms.values())
    is_comparable = slot_ok_all and not infeasible
    (comparable if is_comparable else excluded).append(B)

    entry = {}
    for arm in ARMS:
        K, meta = arms[arm]
        entry[arm] = {"composite": composites(K), "slot_parity": bool(meta["slot_ok"]),
                      "slot_error": float(slots(K) - B), "K_min": float(K.min()),
                      "K_max": float(K.max()), "K_mean": float(K.mean()), "interpolated": True,
                      "n_at_clamp_min": int((K <= CLAMP[0] + 1e-9).sum()),
                      "n_at_clamp_max": int((K >= CLAMP[1] - 1e-9).sum()),
                      "budget_infeasible": bool(meta["budget_infeasible"])}
    per_budget[str(B)] = {"comparable": is_comparable, "slot_parity": slot_ok_all,
                          "budget_infeasible": infeasible, "per_arm": entry,
                          "capacity_upper_bound": NL * (CLAMP[1] + 1.0)}

    # 复算校验（对照 EXP-14 报告值）
    e14 = exp14["caliber_A_noL0"]["per_budget"].get(str(B), {}).get("per_arm", {})
    rc = {}
    for arm in ARMS:
        if arm not in e14:
            continue
        rc[arm] = {s: {"exp16_recomputed": entry[arm]["composite"][s],
                       "exp14_reported": e14[arm]["composite"][s],
                       "abs_diff": abs(entry[arm]["composite"][s] - e14[arm]["composite"][s]),
                       "match": abs(entry[arm]["composite"][s] - e14[arm]["composite"][s]) <= 1e-9}
                   for s in SYNTH}
    recheck[str(B)] = rc

    if not is_comparable:
        famA[str(B)] = {"status": "excluded_budget_infeasible", "note": "不可闭合档，不做对照族判据"}
        famB[str(B)] = {"status": "excluded_budget_infeasible"}
        famC[str(B)] = {"status": "excluded_budget_infeasible"}
        fair[str(B)] = {"slot_parity": slot_ok_all, "budget_infeasible": infeasible,
                        "sum_slot_error_max": max(abs(entry[a]["slot_error"]) for a in ARMS)}
        continue

    Kp = arms["P"][0]
    sp_P = sparsity(Kp)
    n1 = int(sp_P["n_at_clamp_min"])
    n_free = NL - n1
    R = (B - NL) - n1 * 1.0                      # free 层 K 之和目标
    Kp_free = np.sort(Kp[Kp > CLAMP[0] + 1e-9])
    tgt_f1, tgt_neff = sp_P["f1"], sp_P["n_eff"]

    def matched(K):
        sp = sparsity(K)
        f1_err = abs(sp["f1"] - tgt_f1) / max(tgt_f1, 1.0 / NL)
        ne_err = abs(sp["n_eff"] - tgt_neff) / tgt_neff
        return (f1_err <= 0.05 and ne_err <= 0.05), f1_err, ne_err, sp

    # --- λ 标定：网格 argmin，要求 |n_eff 相对误差| ≤5%，取最大可行 λ（更均衡）---
    cal_rng = np.random.default_rng(SEED + B)
    lam_grid = np.round(np.arange(0.0, 1.0001, 0.05), 4)
    lam_stats = []
    for lam in lam_grid:
        ne_vals, f1_ok = [], 0
        for _ in range(20):
            K = sample_family_A(cal_rng, Kp_free, n1, R, float(lam))
            ok, _, ne, sp = matched(K)
            ne_vals.append(sp["n_eff"])
            f1_ok += int(ok)
        m = float(np.mean(ne_vals))
        ne_err = abs(m - tgt_neff) / tgt_neff
        lam_stats.append({"lam": float(lam), "mean_n_eff": m, "n_eff_rel_err": ne_err,
                          "f1_and_neff_pass_rate_20": f1_ok / 20.0})
    feasible = [s for s in lam_stats if s["n_eff_rel_err"] <= 0.05]
    if feasible:
        lam_star = max(feasible, key=lambda s: s["lam"])["lam"]
        lam_note = "n_eff 匹配（≤5%）成立，取最大可行 λ（最均衡）"
    else:
        lam_star = min(lam_stats, key=lambda s: s["n_eff_rel_err"])["lam"]
        lam_note = "无 λ 使 n_eff 相对误差 ≤5%，取 argmin 并显式披露（该档稀疏度匹配未完全达成）"

    # --- 族 A 采样 n=200（拒绝重采上限 20 次/样本）---
    rng = np.random.default_rng(SEED)
    Ks, tries, rejected = [], [], 0
    for i in range(N_FAM):
        got = None
        for t in range(20):
            K = sample_family_A(rng, Kp_free, n1, R, lam_star)
            ok, f1e, nee, sp = matched(K)
            if ok and abs(slots(K) - B) <= 1e-6:
                got = (K, sp, t + 1)
                break
        if got is None:
            rejected += 1
            continue
        Ks.append(got[0]); tries.append(got[2])
        if len(Ks) == 1:
            famA.setdefault(str(B), {})
    Kmat = np.asarray(Ks, dtype=float)                    # (n_used,35)
    n_used = int(Kmat.shape[0])
    dmat = interp_dL(Kmat)                               # (n_used,35)
    arrs = {"L1_linear_sum": dmat.sum(axis=1), "L2_max": dmat.max(axis=1),
            "L3_euclid": np.sqrt((dmat ** 2).sum(axis=1))}
    slot_err_max = float(np.abs(Kmat.sum(axis=1) + NL - B).max())

    # 族 C：自助重采样（对已采到的 R' 样本）
    boot_rng = np.random.default_rng(SEED + 1)
    famC[str(B)] = {}

    pinfo, cinfo = {}, {}
    for s in SYNTH:
        a = arrs[s]
        obs = entry["P"]["composite"][s]
        mean, sd = float(a.mean()), float(a.std(ddof=1))
        gap = mean - obs
        z = (obs - mean) / sd if sd > 0 else None
        # bootstrap z' 的 95% CI
        idx = boot_rng.integers(0, a.size, size=(N_BOOT, a.size))
        bs = a[idx]
        bmean, bsd = bs.mean(axis=1), bs.std(axis=1, ddof=1)
        bz = (obs - bmean) / np.where(bsd > 0, bsd, np.nan)
        bz = bz[~np.isnan(bz)]
        cinfo[s] = {"n_boot": int(bz.size), "z_ci95_low": float(np.percentile(bz, 2.5)),
                    "z_ci95_high": float(np.percentile(bz, 97.5)),
                    "z_point": z, "frac_boot_z_le_minus2": float((bz <= -2).mean()) if bz.size else None}
        pinfo[s] = {"composite_P_observed": obs, "familyA_mean": mean, "familyA_sd": sd,
                    "familyA_p2_5": float(np.percentile(a, 2.5)),
                    "familyA_p97_5": float(np.percentile(a, 97.5)),
                    "familyA_min": float(a.min()), "familyA_max": float(a.max()),
                    "gap_prime": gap, "z_prime": z, "n_used": n_used,
                    "frac_familyA_worse_than_P": float((a >= obs).mean())}

    famA[str(B)] = {
        "lam_star": float(lam_star), "lam_note": lam_note, "lam_calibration": lam_stats,
        "target_sparsity": {"f1_P": tgt_f1, "n_eff_P": tgt_neff, "n_at_clamp_min_P": n1,
                            "n_free": int(n_free), "R_free_sum": float(R)},
        "n_requested": N_FAM, "n_used": n_used, "n_rejected_after_20_tries": rejected,
        "mean_resample_tries": float(np.mean(tries)) if tries else None,
        "slot_parity_max_err": slot_err_max, "slot_parity": bool(slot_err_max <= 1e-6),
        "per_synth": pinfo,
        "familyA_sparsity_sample": sparsity(Kmat[0]) if n_used else None,
        "multiset_conserved": False,
        "multiset_note": "族 A 为新构造对照族（非 P 多重集置换），多重集守恒不适用；"
                         "入判据条件改为『预算闭合 ≤1e-6 且 f1/n_eff 相对误差 ≤5%』（指令 §2.4 的等值公平性要求）",
    }

    famC[str(B)] = cinfo

    # 族 B：受限置换子空间占比（重排不改变多重集 → n_eff 恒等）
    b_rng = np.random.default_rng(SEED + 2)
    perm_idx = np.argsort(b_rng.random((200, NL)), axis=1)
    Kp_perm = Kp[perm_idx]
    ne_perm = np.exp(-(np.where(Kp_perm > 0, Kp_perm / Kp_perm.sum(axis=1, keepdims=True), 1e-300)
                       * np.log(np.maximum(Kp_perm / Kp_perm.sum(axis=1, keepdims=True), 1e-300))).sum(axis=1))
    dev = np.abs(ne_perm - tgt_neff) / tgt_neff
    famB[str(B)] = {"n_perm_checked": 200, "frac_within_5pct": float((dev <= 0.05).mean()),
                    "n_eff_P": tgt_neff, "n_eff_perm_min": float(ne_perm.min()),
                    "n_eff_perm_max": float(ne_perm.max()),
                    "note": "重排不改变多重集 → n_eff 恒等于 P（占比应 =1.0）；"
                            "故『稀疏度不匹配』不是 EXP-14 置换分布过宽的成因（诊断结论）"}

    fair[str(B)] = {"slot_parity": slot_ok_all, "budget_infeasible": infeasible,
                    "sum_slot_error_max": max(abs(entry[a]["slot_error"]) for a in ARMS),
                    "familyA_slot_parity_max_err": slot_err_max,
                    "familyA_n_used": n_used, "familyA_n_requested": N_FAM,
                    "sparsity_match_rule": "f1 与 n_eff 相对误差均 ≤5%",
                    "rule": "每档 |Σ(K+1)-B| ≤ 1e-6 且稀疏度匹配才计入判据；不可闭合档（1188）单列剔除，分母=5 档"}

report["caliber_A_noL0"] = {"layer_set": "L1..L35（口径 A，剔除 L0）",
                            "comparable_budgets": comparable, "excluded_budgets": excluded,
                            "per_budget": per_budget}
report["family_A"] = famA
report["family_B"] = famB
report["family_C"] = famC
report["fairness_checks"] = fair
report["recompute_check_vs_exp14"] = recheck

# ---- 分辨力对比：EXP-14 置换 sd(R) vs 本轮族 A sd(R') -------------------------
res_cmp = {"per_unit": {}, "rule": "improved := sd(R') < sd(R)；ratio := sd(R')/sd(R)"}
_ratios = []
for B in comparable:
    e14p = exp14["permutation_test"]["per_budget"].get(str(B), {}).get("P", {})
    for s in SYNTH:
        if s not in e14p:
            continue
        sd14 = float(e14p[s]["perm_sd"])
        sd16 = float(famA[str(B)]["per_synth"][s]["familyA_sd"])
        ratio = sd16 / sd14 if sd14 > 0 else None
        if ratio is not None:
            _ratios.append(ratio)
        res_cmp["per_unit"][f"{B}/{s}"] = {
            "sd_R_exp14_perm": sd14, "sd_Rprime_exp16_familyA": sd16, "ratio": ratio,
            "improved": bool(sd16 < sd14), "n_perm_exp14": int(e14p[s]["n_perm"]),
            "n_familyA_exp16": int(famA[str(B)]["per_synth"][s]["n_used"])}
res_cmp["n_units_improved"] = sum(1 for v in res_cmp["per_unit"].values() if v["improved"])
res_cmp["n_units_total"] = len(res_cmp["per_unit"])
res_cmp["mean_ratio"] = float(np.mean(_ratios)) if _ratios else None
res_cmp["median_ratio"] = float(np.median(_ratios)) if _ratios else None
# 判定口径（预登记，避免以『微弱多数单元 sd 略降』冒充『分辨力提升』）：
#   实质性提升 := 中位比值 ≤ 0.80（sd 至少降 20%）且 ≥2/3 单元下降；否则判『未见实质改善』
_med = res_cmp["median_ratio"]
res_cmp["substantive_improvement_rule"] = "median(sd(R')/sd(R)) ≤ 0.80 且 ≥2/3 单元下降，方判『实质性提升』"
res_cmp["substantive_improvement"] = bool(
    _med is not None and _med <= 0.80 and
    res_cmp["n_units_improved"] >= (2 * res_cmp["n_units_total"] + 2) // 3)
res_cmp["conclusion"] = (
    "换用稀疏度匹配对照族后分辨力实质性提升（sd(R') 系统低于 sd(R)，中位比值 %.3f）" % _med
    if res_cmp["substantive_improvement"] else
    "换用稀疏度匹配对照族后分辨力未见实质改善：sd(R') 与 EXP-14 的 sd(R) 持平"
    "（中位比值 %.3f，均值 %.3f；%d/%d 单元略有下降但幅度 <20%%）" % (
        _med, res_cmp["mean_ratio"], res_cmp["n_units_improved"], res_cmp["n_units_total"]))
report["resolution_comparison_vs_exp14"] = res_cmp


# ---- 判据四分支（以 P 臂、口径 A 可闭合档为分母）-----------------------------
def branch(gap, sd, z):
    if sd <= 0.05 and abs(gap) <= 0.01:
        return "confirmed"
    if sd <= 0.05 and gap >= 0.02 and (z is not None and z <= -2):
        return "refuted"
    if sd > 0.05:
        return "sd_too_wide"
    return "neither"


units = []
per_budget_branch = {}
for B in comparable:
    pb = {}
    for s in SYNTH:
        p = famA[str(B)]["per_synth"][s]
        b = branch(p["gap_prime"], p["familyA_sd"], p["z_prime"])
        units.append({"budget": B, "synth": s, "sd": p["familyA_sd"], "gap": p["gap_prime"],
                      "z": p["z_prime"], "branch": b})
        pb[s] = {"sd_prime": p["familyA_sd"], "gap_prime": p["gap_prime"],
                 "z_prime": p["z_prime"], "branch": b}
    per_budget_branch[str(B)] = pb

n_units = len(units)
n_sd_wide = sum(1 for u in units if u["branch"] == "sd_too_wide")
n_conf = sum(1 for u in units if u["branch"] == "confirmed")
n_ref = sum(1 for u in units if u["branch"] == "refuted")
n_neither = n_units - n_sd_wide - n_conf - n_ref

if n_conf == n_units and n_units > 0:
    status = "confirmed"
elif n_ref == n_units and n_units > 0:
    status = "refuted"
elif n_sd_wide >= (n_units + 1) // 2:
    status = "e5_withdrawn"
else:
    status = "pending"

report["verdict"] = {
    "hypothesis": "H-R1-P-SPARSITY-ARTIFACT",
    "status": status,
    "branch_counts": {"confirmed": n_conf, "refuted": n_ref, "sd_too_wide": n_sd_wide,
                      "neither": n_neither},
    "comparable_budgets_used": comparable,
    "excluded_budgets": excluded,
    "exclusion_reason": {str(B): "Σ(K+1)=B 不可闭合（capacity/clamp 上限所致），与 EXP-14 同源剔除"
                         for B in excluded},
    "denominator_note": f"分母 = {len(comparable)} 个可闭合档 × {len(SYNTH)} 合成 = {n_units} 个判据单元（1188 档单列剔除）",
    "e5_diagnostics": {
        "n_family_A": N_FAM,
        "max_sd_Rprime": max((u["sd"] for u in units), default=None),
        "min_sd_Rprime": min((u["sd"] for u in units), default=None),
        "n_units_sd_gt_0_05": n_sd_wide,
        "n_units_total": n_units,
        "sd_table": {f"{u['budget']}/{u['synth']}": u["sd"] for u in units},
        "gap_table": {f"{u['budget']}/{u['synth']}": u["gap"] for u in units},
        "z_table": {f"{u['budget']}/{u['synth']}": u["z"] for u in units},
    },
    "directional_observation_not_a_verdict": {
        "gap_gt_0_count": sum(1 for u in units if u["gap"] > 0),
        "gap_ge_0_02_count": sum(1 for u in units if u["gap"] >= 0.02),
        "z_lt_0_count": sum(1 for u in units if (u["z"] is not None and u["z"] < 0)),
        "z_le_minus2_count": sum(1 for u in units if (u["z"] is not None and u["z"] <= -2)),
        "units_total": n_units,
        "note": "方向性观察只登记，不作 confirmed/refuted 判定（D1）",
    },
    "per_budget_branch": per_budget_branch,
    "family_B_finding": "受限置换子空间占比 = 100%（重排不改变多重集，n_eff 恒等）→ "
                        "『稀疏度不匹配』不是 EXP-14 sd(R) 过宽的成因；"
                        "改用稀疏度匹配的新对照族后 sd(R') 是否下降，见 resolution_comparison_vs_exp14",
    "resolution_comparison_vs_exp14": res_cmp,
    "assets_required_to_resolve": [
        "per-(层,K) ΔL 的 per-prompt CI（冻结件 n_boundary=1，无测量噪声估计）",
        "多种子·多任务·多 prompt 的重复测量，用于把『置换方差』与『测量噪声』分离",
        "Qwen2.5-3B-Instruct 权重本体（已蒸发，无 GPU 不可重测）",
    ],
    "claim_level": "假设级 / 机制观察（非该模型或架构的真实层敏感度结论）",
    "stop_condition": "本条为该子问题最后一次追打；无论落点均关闭，WB0 不再派发同族实验",
    "does_not_revive": ["H-R1-BUDGET", "H-R1-BUDGET-PROPORTIONAL"],
}
report["n_and_power"] = {"n_family_A": N_FAM, "seed": SEED, "n_bootstrap": N_BOOT,
                         "ci": "族 A 报 mean/sd 与 2.5%/97.5% 分位；族 C 报 z' 的 bootstrap 95% CI",
                         "power_note": "零数据生成、纯 CPU；结论定级为假设级，不作架构级断言"}
report["dependency_missing"] = [
    "/workspace/models/Qwen2.5-3B-Instruct 已蒸发（无 GPU、不可重测）：层敏感度不可重算，本轮沿用冻结件实测 ΔL",
    "冻结件 per-(层,K) ΔL 未附 per-prompt CI（n_boundary=1）：置换/对照族分布宽度无法被测量噪声解释或收窄",
]
report["p0"] = False
report["p0_reason"] = "产物可由脚本幂等重跑复现（seed 固定），非不可再生资产"
report["scope_guard_selfcheck"] = {
    "zero_data_generation": True,
    "dL_from_frozen_cells_only": True,
    "frozen_files_unmodified": True,
    "no_training_no_model_load": True,
    "no_new_hypothesis_id": True,
    "no_ledger_write": True,
    "n_family_A_actual": {str(B): famA[str(B)].get("n_used") for B in comparable},
    "downgrade_disclosed": True,
}
report["wall_clock_sec"] = round(time.time() - T0, 2)

with open(OUT, "w") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
print(f"[EXP-16] 写出 {OUT} ({os.path.getsize(OUT)}B) wall={report['wall_clock_sec']}s")
print(f"[EXP-16] status={status}  units={n_units} sd_wide={n_sd_wide} confirmed={n_conf} refuted={n_ref} neither={n_neither}")
for B in comparable:
    for s in SYNTH:
        p = famA[str(B)]["per_synth"][s]
        print(f"  B={B:5d} {s:14s} sd'={p['familyA_sd']:.4f} gap'={p['gap_prime']:+.4f} "
              f"z'={p['z_prime'] if p['z_prime'] is None else round(p['z_prime'],3)} n={p['n_used']} "
              f"lam*={famA[str(B)]['lam_star']}")
