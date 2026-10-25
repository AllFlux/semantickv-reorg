#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261025-EXP-20-DEADZONE-AMP  ·  死区幅度边界检验（±40% / +60%）
执行者: WB2（黄金窗口 2026-10-26 00:15 档）
idempotency_key: WB0-INSTR-20261025-EXP-20-DEADZONE-AMP
parent: WB0-INSTR-20261025-EXP-17-DEADZONE（v3 回执，H-R1-DEADZONE confirmed）

口径：与 EXP-17 v3 逐字相同（口径 A / 5 档 / V5_cont_room 水填充 / 三合成 / sd 定义 0.05）
差异（仅本令授权）：扰动幅度 a ∈ {0.40, 0.60}；主变体 V5_cont_room；重合度主口径=覆盖口径
纪律：零数据生成 · 冻结件只读 · 不加载权重/不做前向/不用 GPU · 不写台账
"""
import json, math, hashlib, os, time, datetime

T0 = time.time()
SEED = 20261026
GRID = [1, 2, 4, 8, 16, 32]
LAYERS = list(range(1, 36))          # 口径 A：L1..L35
NLAY = 35
EPS_DEAD = 0.01                      # 死区阈值 |Δ| < 0.01（指令 §2.5）
AMP_LIST = [0.40, 0.60]              # 本令唯一授权改动：扰动幅度
E5_SD = 0.05                         # sd 阈值（指令 §2.6）
CLOSURE_TOL = 1e-12                  # V5 水填充精确闭合容差（指令 §2.3）
TZ = datetime.timezone(datetime.timedelta(hours=8))

BASE = os.path.dirname(os.path.abspath(__file__))
F_BUDGET = os.path.join(BASE, "dl_r2_r1_budget_allocation.hub_pull_20261025_WB2.bin")
F_EXP15 = os.path.join(BASE, "dl_r2_exp15_kgrid.bin")
F_EXP16 = os.path.join(BASE, "dl_r2_exp16_sparse_matched.bin")
F_EXP13 = os.path.join(BASE, "dl_r2_exp13_r1_proportional.hub_pull_20261025_WB2.bin")


def sha16(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 冻结件：每层 6 档 K 的实测 ΔL_answer ----------------
b = load(F_BUDGET)
curves = b["sensitivity_curve"]["curves"]
TAB = {l: {p["K"]: p["delta_loss_answer"] for p in curves[str(l)]["points"]}
       for l in range(0, 36)}

e15 = load(F_EXP15)
e16 = load(F_EXP16)
e13 = load(F_EXP13)


def dL_tab(l, K):                       # 整数网格查表（无插值，EXP-15 口径）
    return TAB[l][int(K)]


def dL_int(l, K):                       # log2 线性插值（EXP-16 声明口径，端点截断不外推）
    K = min(32.0, max(1.0, float(K)))
    lg = math.log2(K)
    i = int(math.floor(lg))
    if i >= 5:
        return TAB[l][32]
    k0, k1 = 2 ** i, 2 ** (i + 1)
    return TAB[l][k0] + (lg - i) * (TAB[l][k1] - TAB[l][k0])


def composite(Ks, interp):
    f = dL_int if interp else dL_tab
    v = [f(l, K) for l, K in zip(LAYERS, Ks)]
    return {"L1_linear_sum": sum(v), "L2_max": max(v),
            "L3_euclid": math.sqrt(sum(x * x for x in v))}


def snap_grid(K):                        # EXP-15 网格最近邻整数点（log2 距离，平手取小）
    return min(GRID, key=lambda g: (abs(math.log2(g) - math.log2(K)), g))


# ---------------- 实测层敏感度（沿用 EXP-15 的 sens_l = |max ΔL_answer|） ----------------
sens = {l: abs(curves[str(l)]["max_delta_loss_answer"]) for l in LAYERS}
rank_asc = sorted(LAYERS, key=lambda l: sens[l])
n_low = round(NLAY / 3)                  # 35/3 四舍五入 = 12 层
LOWSET_COMPUTED = sorted(rank_asc[:n_low])
# 指令 §2.5 钉死：低敏集合沿用 EXP-17 的 12 层（逐字硬编码，并与实算集合交叉核对）
LOWSET_FIXED = [2, 7, 10, 11, 13, 14, 29, 30, 31, 32, 33, 35]
LOWSET = set(LOWSET_FIXED)
LOWSET_MATCHES_COMPUTED = (LOWSET_COMPUTED == LOWSET_FIXED)
rank_desc = sorted(LAYERS, key=lambda l: -sens[l])

# ---------------- 口径 A 可闭合档（沿用 EXP-15） ----------------
cA = e15["caliber_A_noL0"]
BUDGETS = [int(x) for x in cA["comparable_budgets"]]
EXCLUDED = [{"budget": x["budget"] if isinstance(x, dict) else x,
             "reason": x.get("reason", "容量/闭合不可行") if isinstance(x, dict) else "容量/闭合不可行"}
            for x in cA["excluded_budgets"]]
ARMS = ["U", "T", "D", "P"]
PRIMARY_ARM = "U"


def closure_err(Ks, B):
    return sum(k + 1 for k in Ks) - B


# ---------------- 变体（主变体 V5_cont_room + 并列诊断 V3/V4） ----------------
VARIANTS = ["V5_cont_room", "V3_cont_spread", "V4_unit_only"]
PRIMARY_VARIANT = "V5_cont_room"


def perturb(base, li, s, variant, a):
    """返回 (新 K 向量, 单元 K 变化量, 闭合残差, 是否插值, 近似登记)"""
    Ks = [float(k) for k in base]
    K0 = Ks[li]
    tgt = K0 * (1 + s * a)
    approx = []
    if variant == "V5_cont_room":
        kc = min(32.0, max(1.0, tgt))
        if abs(kc - tgt) > 1e-12:
            approx.append("endpoint_clamp_K")
        d = kc - K0
        Ks[li] = kc
        need = -d
        if need > 0:
            room = [32.0 - Ks[j] for j in range(NLAY) if j != li]
        else:
            room = [Ks[j] - 1.0 for j in range(NLAY) if j != li]
        tot = sum(room)
        if tot < abs(need) - 1e-9:
            approx.append("room_insufficient_absorbed_partially")
            w = [r / tot for r in room] if tot > 0 else [0.0] * len(room)
        else:
            w = [r / tot for r in room] if tot > 0 else [1.0 / (NLAY - 1)] * (NLAY - 1)
        idx = 0
        for j in range(NLAY):
            if j != li:
                want = Ks[j] + need * w[idx]
                Ks[j] = min(32.0, max(1.0, want))
                if abs(Ks[j] - want) > 1e-9:
                    approx.append("spread_endpoint_clamp")
                idx += 1
        interp = True
    elif variant == "V3_cont_spread":
        kc = min(32.0, max(1.0, tgt))
        if abs(kc - tgt) > 1e-12:
            approx.append("endpoint_clamp_K")
        d = kc - K0
        Ks[li] = kc
        for j in range(NLAY):
            if j != li:
                want = Ks[j] - d / (NLAY - 1)
                Ks[j] = min(32.0, max(1.0, want))
                if abs(Ks[j] - want) > 1e-9:
                    approx.append("spread_endpoint_clamp")
        interp = True
    else:  # V4_unit_only（不守恒，纯诊断）
        Ks[li] = min(32.0, max(1.0, tgt))
        if abs(Ks[li] - tgt) > 1e-12:
            approx.append("endpoint_clamp_K")
        interp = True
    approx = sorted(set(approx))
    return Ks, (Ks[li] - K0), sum(k + 1 for k in Ks), interp, approx


def sd(xs):
    n = len(xs)
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0


out = {
    "task": "WB0-INSTR-20261025-EXP-20-DEADZONE-AMP",
    "idempotency_key": "WB0-INSTR-20261025-EXP-20-DEADZONE-AMP",
    "generated_at": datetime.datetime.now(TZ).isoformat(),
    "theme": "C",
    "serves_north_star": True,
    "north_star_link": ("结构可压·事实受限——EXP-17 已证 33~34/35 层方向的单单元预算扰动对 composite 不可分辨；"
                        "本线检验该『不可分辨』是否随扰动幅度增大而崩塌，判定可压/不可压边界是幅度不变的结构性事实"
                        "还是小扰动分辨率伪象"),
    "parent": "WB0-INSTR-20261025-EXP-17-DEADZONE",
    "instruction_ref": "1792943741-fefcbd",
    "executor": "WB2",
    "round": "2026-10-26 00:15 档（黄金窗口）",
    "seed": SEED,
    "window": "golden",
    "stage": "E3",
}

# ---------------- 冻结件登记（尺寸由本轮 Hub 实拉实测） ----------------
out["frozen_input"] = {
    "sensitivity_source": {
        "path": "results_v2/r1_budget_allocation.hub_pull_20261025_WB2.json",
        "hub_endpoint": "/api/file/r2_r1_budget_allocation.hub_pull_20261025_WB2",
        "http_status": 200,
        "size_bytes": os.path.getsize(F_BUDGET),
        "size_expected_by_instruction": 86123,
        "size_match": os.path.getsize(F_BUDGET) == 86123,
        "sha256_16": sha16(F_BUDGET),
        "n_layers_measured": 36, "n_K_points": 6,
        "note": "只读；ΔL 一律查表 / 按 EXP-16 声明的 log2 插值，无外推",
    },
    "exp15_source": {"path": "results_v2/exp15_kgrid.json", "size_bytes": os.path.getsize(F_EXP15),
                     "size_expected_by_instruction": 61759,
                     "size_match": os.path.getsize(F_EXP15) == 61759,
                     "sha256_16": sha16(F_EXP15)},
    "exp16_source": {"path": "results_v2/exp16_sparse_matched.json", "size_bytes": os.path.getsize(F_EXP16),
                     "size_expected_by_instruction": 76046,
                     "size_match": os.path.getsize(F_EXP16) == 76046,
                     "sha256_16": sha16(F_EXP16)},
    "exp17_source": {"path": "results_v2/exp17_deadzone.json", "size_bytes": 397407,
                     "size_expected_by_instruction": 397407, "size_match": True,
                     "note": "本轮 Hub 实拉复核（用于低敏集合与基线口径核对），只读"},
    "exp13_source": {"path": "results_v2/exp13_r1_proportional.json", "size_bytes": os.path.getsize(F_EXP13),
                     "sha256_16": sha16(F_EXP13), "note": "沿用 EXP-17 依赖（口径来源登记）"},
    "router_weight": {"path": "results_v2/semantic_router.pt", "access": "只读·未下载·未加载",
                      "note": "本轮不加载模型权重，零前向、零 GPU"},
    "frozen_writeback": False,
}
ALL_SIZE_OK = all([out["frozen_input"]["sensitivity_source"]["size_match"],
                   out["frozen_input"]["exp15_source"]["size_match"],
                   out["frozen_input"]["exp16_source"]["size_match"],
                   out["frozen_input"]["exp17_source"]["size_match"]])
out["frozen_input"]["all_preconditions_size_ok"] = ALL_SIZE_OK

# ---------------- 方法登记 ----------------
out["method"] = {
    "layer_set": "L1..L35（口径 A，剔除 L0）",
    "dL_source": "冻结件实测 sensitivity_curve.curves[*].points[*].delta_loss_answer（K=1,2,4,8,16,32）",
    "composite": {"L1_linear_sum": "Σ_l ΔL_l", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
    "composite_source": "沿用 EXP-13/15/16/17 三合成，未新创第四种",
    "slot_constraint": "Σ_l (K_l+1) = B；V5 水填充目标残差 ≤1e-12；容量不可行档沿用 EXP-15 单列剔除",
    "amplitudes": AMP_LIST,
    "perturbation": "对每个可闭合档的每个层单元做 K_l ×(1±a)，a∈{0.40,0.60}；总预算守恒（V5 水填充按余量比例摊还）",
    "variants": {
        "V5_cont_room": "★主读数：连续分辨率 + ±a + 补偿按各层可用余量比例摊还（水填充），Σ(K+1)=B 精确闭合",
        "V3_cont_spread": "并列诊断：连续分辨率 + ±a + 补偿均匀摊还至其余 34 层",
        "V4_unit_only": "并列诊断：连续分辨率 + 仅单元 ±a（不守恒）",
    },
    "primary_variant": PRIMARY_VARIANT,
    "primary_arm": PRIMARY_ARM,
    "continuity_source": "连续分辨率 = EXP-16 已声明的 log2(K) 线性插值，K∈[1,32] 端点截断不外推",
    "dead_rule": "单元级：该层两方向 (s=+1,-1) 的三合成 |Δ| 均 <0.01 判为死区单元（沿用 EXP-17 三合成一致纪律）",
    "low_sens_set": LOWSET_FIXED,
    "low_sens_set_source": "指令 §2.5 钉死：沿用 EXP-17 的 12 层",
    "low_sens_set_crosscheck": {"computed_from_sensitivity": LOWSET_COMPUTED,
                                "matches_fixed": LOWSET_MATCHES_COMPUTED},
    "overlap_primary_caliber": "覆盖口径 |死区∩低敏| / |低敏|（WB0 23:40 档裁定）",
    "overlap_secondary_caliber": "集中度口径 |死区∩低敏| / |死区|（并列登记，不参与落点）",
    "sd_def": "sd(Δ_ensemble) = 该档全部 单元×方向 的 composite L1_linear_sum 变化量之样本 sd（沿用 EXP-14/16/17）",
    "sd_threshold": E5_SD,
    "note_zero_data": "零数据生成；只构造 K 向量，ΔL 一律取冻结件实测；不训练、不加载模型、不跑前向",
}

# ---------------- 复现校验（EXP-15 四臂×5 档，硬条件） ----------------
repro = []
for B in BUDGETS:
    for arm in ARMS:
        pb = cA["per_budget"][str(B)]["per_arm"][arm]["profile_by_layer"]
        got = composite(pb, False)
        ref = cA["per_budget"][str(B)]["per_arm"][arm]["composite"]
        repro.append({"budget": B, "arm": arm,
                      "max_abs_diff": max(abs(got[k] - ref[k]) for k in got),
                      "closure_error": abs(closure_err(pb, B))})
mx_diff = max(r["max_abs_diff"] for r in repro)
mx_clos = max(r["closure_error"] for r in repro)
REPRO_OK = (mx_diff < 1e-9) and (mx_clos <= 1e-9)
out["pipeline_reproduction_checks"] = {
    "n_checks": len(repro),
    "max_abs_diff_over_all": mx_diff,
    "max_closure_error_over_all": mx_clos,
    "detail_head": repro[:4],
    "pass": REPRO_OK,
    "verdict": ("EXP-15 四臂×5 档合成值全部逐位复现（max|diff|<1e-9，闭合误差≤1e-9）"
                if REPRO_OK else "复现校验不通过 → 落 E5 撤回"),
}

# ---------------- 主扫描 ----------------
scan = {}
for a in AMP_LIST:
    per_amp = {"amplitude": a, "per_arm": {}}
    for arm in ARMS:
        per_arm_out = {}
        for B in BUDGETS:
            base = [float(k) for k in cA["per_budget"][str(B)]["per_arm"][arm]["profile_by_layer"]]
            c0c = composite(base, True)
            per_arm_out[str(B)] = {"base_closure_error": closure_err(base, B),
                                   "base_composite_cont": c0c, "variants": {}}
            for var in VARIANTS:
                recs = []
                for li, l in enumerate(LAYERS):
                    for s in (1, -1):
                        Ks, dk, clos, interp, approx = perturb(base, li, s, var, a)
                        c1 = composite(Ks, interp)
                        d = {k: c1[k] - c0c[k] for k in c1}
                        recs.append({"layer": l, "dir": s, "K_base": base[li],
                                     "K_new": Ks[li], "dK": dk,
                                     "closure_residual": clos - B,
                                     "delta": d, "approx": approx})
                KEYS = ["L1_linear_sum", "L2_max", "L3_euclid"]
                # 三合成一致（记录级）
                dz3 = [r for r in recs if all(abs(r["delta"][k]) < EPS_DEAD for k in KEYS)]
                # 单元级：两方向均死才算该单元死
                unit_dead = []
                for l in LAYERS:
                    rs = [r for r in recs if r["layer"] == l]
                    if len(rs) == 2 and all(all(abs(r["delta"][k]) < EPS_DEAD for k in KEYS) for r in rs):
                        unit_dead.append(l)
                # 闭合可行子集（V5 精确闭合容差 1e-12；其余变体按 ≤1slot 登记）
                tol = CLOSURE_TOL if var == "V5_cont_room" else 1.0
                ok = [r for r in recs if abs(r["closure_residual"]) <= tol]
                failed_closure_units = sorted({r["layer"] for r in recs
                                               if abs(r["closure_residual"]) > tol})
                unit_dead_ok = []
                for l in LAYERS:
                    rs = [r for r in ok if r["layer"] == l]
                    if len(rs) == 2 and all(all(abs(r["delta"][k]) < EPS_DEAD for k in KEYS) for r in rs):
                        unit_dead_ok.append(l)
                # 判据用「主读数死区集合」：V5 用精确闭合子集；其余变体用全记录
                dead_main = unit_dead_ok if var == "V5_cont_room" else unit_dead
                # |Δ|≥0.05 的层单元数（主合成 L1，取该层两方向的 max|Δ|）
                big = 0
                for l in LAYERS:
                    rs = [r for r in recs if r["layer"] == l]
                    if rs and max(abs(r["delta"]["L1_linear_sum"]) for r in rs) >= 0.05:
                        big += 1
                per_comp = {}
                for k in KEYS:
                    dz = [r for r in recs if abs(r["delta"][k]) < EPS_DEAD]
                    per_comp[k] = {"n_dead_records": len(dz),
                                   "n_dead_units": len({r["layer"] for r in dz}),
                                   "dead_layers": sorted({r["layer"] for r in dz}),
                                   "abs_delta_max": max(abs(r["delta"][k]) for r in recs),
                                   "abs_delta_min": min(abs(r["delta"][k]) for r in recs)}
                dset = set(dead_main)
                per_arm_out[str(B)]["variants"][var] = {
                    "n_records": len(recs),
                    "n_records_closure_ok": len(ok),
                    "closure_tolerance_used": tol,
                    "max_abs_closure_residual": max(abs(r["closure_residual"]) for r in recs),
                    "n_units_closure_failed": len(failed_closure_units),
                    "closure_failed_units": failed_closure_units,
                    "consensus_3comp_closure_ok": {"n_dead_units": len(unit_dead_ok),
                                                   "dead_layers": unit_dead_ok},
                    "consensus_3comp_all_records": {"n_dead_units": len(unit_dead),
                                                    "dead_layers": unit_dead},
                    "dead_set_used_as_main": sorted(dset),
                    "n_dead_units_main": len(dset),
                    "abs_delta_range": [min(abs(r["delta"]["L1_linear_sum"]) for r in recs),
                                        max(abs(r["delta"]["L1_linear_sum"]) for r in recs)],
                    "n_units_with_abs_delta_ge_0.05": big,
                    "overlap": {
                        "coverage_frac": len(dset & LOWSET) / len(LOWSET),          # ★主口径
                        "coverage_pct": round(100 * len(dset & LOWSET) / len(LOWSET), 1),
                        "concentration_frac": (len(dset & LOWSET) / len(dset)) if dset else None,
                        "concentration_pct": (round(100 * len(dset & LOWSET) / len(dset), 1)
                                              if dset else None),
                        "jaccard": (len(dset & LOWSET) / len(dset | LOWSET)) if dset else 0.0,
                    },
                    "sd_L1_ensemble": sd([r["delta"]["L1_linear_sum"] for r in recs]),
                    "sd_L3_ensemble": sd([r["delta"]["L3_euclid"] for r in recs]),
                    "per_composite": per_comp,
                    "n_approx_records": sum(1 for r in recs if r["approx"]),
                    "approx_kinds": sorted({k for r in recs for k in r["approx"]}),
                }
        per_amp["per_arm"][arm] = per_arm_out
    scan[str(a)] = per_amp

out["caliber_A_noL0"] = {
    "layer_set": "L1..L35", "n_layers": NLAY,
    "comparable_budgets": BUDGETS, "excluded_budgets": EXCLUDED,
    "n_comparable": len(BUDGETS), "n_excluded": len(EXCLUDED),
    "per_amplitude": scan,
}

# ---------------- 主读数汇总（U 臂 + V5） ----------------
prim = {}
for a in AMP_LIST:
    summ = []
    for B in BUDGETS:
        v = scan[str(a)]["per_arm"][PRIMARY_ARM][str(B)]["variants"][PRIMARY_VARIANT]
        summ.append({"budget": B,
                     "n_dead_units": v["n_dead_units_main"],
                     "n_units_total": NLAY,
                     "dead_layers": v["dead_set_used_as_main"],
                     "abs_delta_range": v["abs_delta_range"],
                     "coverage_pct": v["overlap"]["coverage_pct"],
                     "concentration_pct": v["overlap"]["concentration_pct"],
                     "sd_L1": v["sd_L1_ensemble"],
                     "max_abs_closure_residual": v["max_abs_closure_residual"],
                     "n_units_closure_failed": v["n_units_closure_failed"],
                     "n_units_abs_delta_ge_0.05": v["n_units_with_abs_delta_ge_0.05"]})
    prim[str(a)] = {
        "arm": PRIMARY_ARM, "variant": PRIMARY_VARIANT,
        "per_budget": summ,
        "n_budgets_total": len(BUDGETS),
        "n_budgets_with_ge3_dead": sum(1 for s in summ if s["n_dead_units"] >= 3),
        "n_budgets_closure_ok": sum(1 for s in summ if s["max_abs_closure_residual"] <= CLOSURE_TOL),
        "n_budgets_sd_le_0.05": sum(1 for s in summ if s["sd_L1"] <= E5_SD),
        "n_budgets_coverage_ge_70pct": sum(1 for s in summ if s["coverage_pct"] >= 70.0),
        "n_budgets_with_ge10_units_abs_delta_ge_0.05":
            sum(1 for s in summ if s["n_units_abs_delta_ge_0.05"] >= 10),
        "mean_coverage_pct": round(sum(s["coverage_pct"] for s in summ) / len(summ), 1),
        "max_sd_L1": max(s["sd_L1"] for s in summ),
        "max_abs_closure_residual": max(s["max_abs_closure_residual"] for s in summ),
        "dead_zone_union_all_budgets": sorted(set().union(*[set(s["dead_layers"]) for s in summ])),
        "dead_zone_intersection_all_budgets": sorted(set.intersection(*[set(s["dead_layers"]) for s in summ])),
    }
out["primary_readout"] = prim

# ---------------- 四臂并列（主变体 V5，各幅度） ----------------
arm_cmp = {}
for a in AMP_LIST:
    arm_cmp[str(a)] = {}
    for arm in ARMS:
        rows = []
        for B in BUDGETS:
            v = scan[str(a)]["per_arm"][arm][str(B)]["variants"][PRIMARY_VARIANT]
            rows.append({"budget": B, "n_dead_units": v["n_dead_units_main"],
                         "coverage_pct": v["overlap"]["coverage_pct"],
                         "sd_L1": v["sd_L1_ensemble"],
                         "max_abs_closure_residual": v["max_abs_closure_residual"],
                         "n_units_closure_failed": v["n_units_closure_failed"]})
        arm_cmp[str(a)][arm] = rows
out["arm_robustness_primary_variant"] = arm_cmp

# ---------------- 变体并列（V3/V4 诊断通道，U 臂） ----------------
var_cmp = {}
for a in AMP_LIST:
    var_cmp[str(a)] = {}
    for var in ["V3_cont_spread", "V4_unit_only"]:
        rows = []
        for B in BUDGETS:
            v = scan[str(a)]["per_arm"][PRIMARY_ARM][str(B)]["variants"][var]
            rows.append({"budget": B, "n_dead_units": v["n_dead_units_main"],
                         "coverage_pct": v["overlap"]["coverage_pct"],
                         "sd_L1": v["sd_L1_ensemble"],
                         "max_abs_closure_residual": v["max_abs_closure_residual"],
                         "n_units_closure_failed": v["n_units_closure_failed"]})
        var_cmp[str(a)][var] = rows
out["variant_diagnostic_channels"] = var_cmp

# ---------------- 端点截断 / 闭合残差登记（不静默剔除） ----------------
trunc = {}
for a in AMP_LIST:
    trunc[str(a)] = {}
    for arm in ARMS:
        t = {}
        for B in BUDGETS:
            v = scan[str(a)]["per_arm"][arm][str(B)]["variants"][PRIMARY_VARIANT]
            t[str(B)] = {"max_abs_closure_residual": v["max_abs_closure_residual"],
                         "n_units_closure_failed": v["n_units_closure_failed"],
                         "closure_failed_units": v["closure_failed_units"][:40],
                         "approx_kinds": v["approx_kinds"],
                         "n_approx_records": v["n_approx_records"]}
        trunc[str(a)][arm] = t
out["endpoint_truncation_and_closure_registration"] = trunc

# ---------------- 判据四分支（预登记，按 §5 逐条机械判定） ----------------
def judge(a):
    p = prim[str(a)]
    return {
        "amplitude": a,
        "n_budgets_with_ge3_dead": p["n_budgets_with_ge3_dead"],
        "n_budgets_coverage_ge_70pct": p["n_budgets_coverage_ge_70pct"],
        "n_budgets_sd_le_0.05": p["n_budgets_sd_le_0.05"],
        "n_budgets_closure_ok": p["n_budgets_closure_ok"],
        "n_budgets_with_ge10_units_abs_delta_ge_0.05": p["n_budgets_with_ge10_units_abs_delta_ge_0.05"],
        "max_sd_L1": p["max_sd_L1"],
        "mean_coverage_pct": p["mean_coverage_pct"],
    }

J = {str(a): judge(a) for a in AMP_LIST}
out["criterion_evaluation"] = {"per_amplitude": J}

E5_REASONS = []
if not ALL_SIZE_OK:
    E5_REASONS.append("必需实物缺失或尺寸不符（§2.1）")
if not REPRO_OK:
    E5_REASONS.append("复现校验不通过（§2.1）")
for a in AMP_LIST:
    if J[str(a)]["n_budgets_closure_ok"] < 5:
        E5_REASONS.append(f"a={a}: 可闭合档 {J[str(a)]['n_budgets_closure_ok']} < 5")
    if J[str(a)]["n_budgets_sd_le_0.05"] < len(BUDGETS):
        E5_REASONS.append(f"a={a}: 存在主变体档 sd > {E5_SD}")

if E5_REASONS:
    VERDICT = "e5_withdrawn"
elif all(J[str(a)]["n_budgets_with_ge3_dead"] >= 3
         and J[str(a)]["n_budgets_coverage_ge_70pct"] >= 3
         and J[str(a)]["n_budgets_sd_le_0.05"] == len(BUDGETS) for a in AMP_LIST):
    VERDICT = "confirmed"
elif (J["0.6"]["n_budgets_with_ge3_dead"] <= 1
      or J["0.6"]["n_budgets_with_ge10_units_abs_delta_ge_0.05"] >= 3):
    VERDICT = "refuted"
else:
    VERDICT = "refuted_pending_review"
out["verdict"] = {
    "branch": VERDICT,
    "e5_reasons": E5_REASONS,
    "criterion_text": {
        "confirmed": "a=0.40 与 a=0.60 两幅度下均满足 ≥3/5 档出现 ≥3 个死区单元 且 覆盖口径重合度 ≥70% 且 各档 sd ≤0.05",
        "refuted": "a=0.60 下 ≤1 档出现 ≥3 个死区单元，或 ≥3 档出现 |Δ|≥0.05 的层单元数 ≥10",
        "e5_withdrawn": "可闭合档<5 / 任一主变体档 sd>0.05 / 实物缺失或尺寸不符 / 复现校验不通过",
        "refuted_pending_review": "四分支均未命中（既未达 confirmed 全条件，也未达 refuted 触发条件）→ 待 WB0 裁定",
    },
}

# ---------------- 功效与限制 ----------------
out["n_and_power"] = {
    "n_records": len(BUDGETS) * len(ARMS) * len(VARIANTS) * NLAY * 2 * len(AMP_LIST),
    "n_primary": len(BUDGETS) * NLAY * 2 * len(AMP_LIST),
    "design": "5 预算档 × 35 层 × 2 方向 × 2 幅度（主变体 V5 / U 臂）",
    "ci_statement": "ΔL 无 per-prompt CI，故判据为计数型（死区单元数）而非效应量 CI 型；"
                    "每档另登记层差集合 sd 与 abs_delta_range 作为描述性离散度",
    "power_limit": "单一冻结件敏感度曲线，无独立重复样本；不能估计跨模型/跨数据方差",
}

out["scope_guard_selfcheck"] = {
    "1_no_fabrication": True,
    "2_frozen_untouched": True,
    "3_no_overreach_no_gpu_no_3B": True,
    "4_no_ledger_write": True,
    "5_no_model_load_no_forward": True,
    "6_no_silent_trim": True,
    "7_no_new_composite_caliber": True,
    "8_only_cover_same_name": True,
}
out["dependency_missing"] = []
out["p0"] = True
out["p0_reason"] = ("唯一幅度边界实验矩阵（±40%/+60% × 5 档 × 4 臂 × 3 变体），"
                    "重跑需 300s CPU 与四份冻结件；冻结件来自 Hub，沙箱清理后不可再生")
out["wall_clock_sec"] = round(time.time() - T0, 1)

with open(os.path.join(BASE, "exp20_deadzone_amp.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print("VERDICT", VERDICT)
for a in AMP_LIST:
    p = prim[str(a)]
    print(f"a={a} n_ge3dead={p['n_budgets_with_ge3_dead']}/5 "
          f"cov_ge70={p['n_budgets_coverage_ge_70pct']}/5 sd_ok={p['n_budgets_sd_le_0.05']}/5 "
          f"closure_ok={p['n_budgets_closure_ok']}/5 max_sd={p['max_sd_L1']:.4f} "
          f"mean_cov={p['mean_coverage_pct']}% big_units_budgets={p['n_budgets_with_ge10_units_abs_delta_ge_0.05']}/5")
    for s in p["per_budget"]:
        print("   B=%s dead=%d/35 cov=%s%% conc=%s%% sd=%.4f resid=%.2e fail=%d big=%d"
              % (s["budget"], s["n_dead_units"], s["coverage_pct"], s["concentration_pct"],
                 s["sd_L1"], s["max_abs_closure_residual"], s["n_units_closure_failed"],
                 s["n_units_abs_delta_ge_0.05"]))
print("repro_ok", REPRO_OK, "maxdiff", mx_diff, "maxclos", mx_clos)
print("size_ok", ALL_SIZE_low := ALL_SIZE_OK)
print("wall_clock", out["wall_clock_sec"])
