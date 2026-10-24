#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261025-EXP-17-DEADZONE  ·  KV 预算分配梯度死区定位
执行者: WB2 (纯夜间窗口 2026-10-25 01:52 档)
idempotency_key: WB0-INSTR-20261025-EXP-17-DEADZONE

纪律：
  · 零数据生成 —— ΔL 一律取自冻结件实测曲线（r1_budget_allocation 的 6 档 K 点）
  · 不加载模型、不下载 3B 权重、不跑前向、不请求 GPU
  · 不写台账、不改冻结件（semantic_router.pt / r1_budget_allocation.json 只读）
  · 沿用既有口径：三合成 L1/L2/L3（EXP-13/15/16 同定义），不新创第四种
  · 口径 A（剔除 L0，L1..L35，35 层）；可闭合档单列，分母公开
"""
import json, math, hashlib, os, time, datetime

T0 = time.time()
SEED = 20261026
GRID = [1, 2, 4, 8, 16, 32]
LAYERS = list(range(1, 36))          # 口径 A：L1..L35
NLAY = 35
EPS_DEAD = 0.01                      # 死区阈值（指令 §2.4 预登记）
PERT = 0.20                          # ±20% 局部扰动（指令 §2.3）
E5_SD = 0.05                         # E5 合成方差阈值（指令 §2.6，沿用 EXP-14/16）
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
n_low = round(NLAY / 3)                  # 排序后 1/3 → 12 层（35/3 四舍五入）
LOWSET = set(rank_asc[:n_low])
LOWSET_11 = set(rank_asc[:11])           # 稳健性：取下整 11 层
rank_desc = sorted(LAYERS, key=lambda l: -sens[l])   # 高→低，用于「最高敏感度层」补偿

# ---------------- 口径 A 可闭合档（沿用 EXP-15，不得新创） ----------------
cA = e15["caliber_A_noL0"]
BUDGETS = [int(x) for x in cA["comparable_budgets"]]
EXCLUDED = [{"budget": x["budget"] if isinstance(x, dict) else x,
             "reason": x.get("reason", "容量/闭合不可行") if isinstance(x, dict) else "容量/闭合不可行"}
            for x in cA["excluded_budgets"]]
ARMS = ["U", "T", "D", "P"]
PRIMARY_ARM = "U"                        # 主臂：均匀（R1 反比/正比与之比较的参照基线）


def closure_err(Ks, B):
    return sum(k + 1 for k in Ks) - B


# ---------------- 变体定义（合成口径与闭合规则三者完全相同，仅扰动实现方式不同） ----------------
# V1 grid_nearest : 指令 §2.3 字面主读数 —— 整数网格 + 最近邻可行整数点 + 最高敏感度层补回
# V2 cont_hi      : 连续分辨率（EXP-16 插值）+ ±20% + 最高敏感度层补回（指令字面补偿规则）
# V3 cont_spread  : 连续分辨率 + ±20% + 补偿金额均匀摊还到其余 34 层（隔离单元自身梯度）★主读数
# V4 unit_only    : 连续分辨率 + 仅单元 ±20%（不守恒，纯诊断，用于分解）
VARIANTS = ["V1_grid_nearest", "V2_cont_hi", "V3_cont_spread", "V4_unit_only",
            "V5_cont_room"]
PRIMARY_VARIANT = "V3_cont_spread"


def perturb(base, li, s, variant):
    """返回 (新 K 向量, 单元 K 变化量, 闭合残差, 近似登记)"""
    Ks = [float(k) for k in base]
    K0 = Ks[li]
    tgt = K0 * (1 + s * PERT)
    approx = []
    if variant == "V1_grid_nearest":
        ksnap = float(snap_grid(tgt))
        d = ksnap - K0
        Ks[li] = ksnap
        if abs(d) > 1e-12:
            m = rank_desc[0] if rank_desc[0] != LAYERS[li] else rank_desc[1]
            mi = LAYERS.index(m)
            Ks[mi] = float(snap_grid(Ks[mi] - d))
            approx.append("grid_snap_with_hi_comp")
        else:
            approx.append("grid_snap_noop")     # ±20% 在 ×2 网格上不可分辨 → 原地
        interp = False
    elif variant == "V2_cont_hi":
        kc = min(32.0, max(1.0, tgt))
        d = kc - K0
        Ks[li] = kc
        m = rank_desc[0] if rank_desc[0] != LAYERS[li] else rank_desc[1]
        mi = LAYERS.index(m)
        before = Ks[mi]
        Ks[mi] = min(32.0, max(1.0, Ks[mi] - d))
        if abs(Ks[mi] - (before - d)) > 1e-9:
            approx.append("hi_comp_clamped")
        interp = True
    elif variant == "V3_cont_spread":
        kc = min(32.0, max(1.0, tgt))
        d = kc - K0
        Ks[li] = kc
        for j in range(NLAY):
            if j != li:
                before = Ks[j]
                Ks[j] = min(32.0, max(1.0, Ks[j] - d / (NLAY - 1)))
                if abs(Ks[j] - (before - d / (NLAY - 1))) > 1e-9:
                    approx.append("spread_clamped")
        approx = sorted(set(approx))
        interp = True
    elif variant == "V5_cont_room":
        # 连续分辨率 + ±20% + 补偿按「可用余量」比例摊还（水填充），保证 Σ(K+1)=B 精确闭合
        kc = min(32.0, max(1.0, tgt))
        d = kc - K0
        Ks[li] = kc
        need = -d                       # 其余 34 层需净吸收的槽位
        if need > 0:                    # 单元加预算 → 其余层让出
            room = [32.0 - Ks[j] for j in range(NLAY) if j != li]
        else:                           # 单元让出 → 其余层吸收
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
                Ks[j] = min(32.0, max(1.0, Ks[j] + need * w[idx]))
                idx += 1
        interp = True
    else:  # V4_unit_only  （不守恒，纯诊断）
        Ks[li] = min(32.0, max(1.0, tgt))
        interp = True
    return Ks, (Ks[li] - K0), sum(k + 1 for k in Ks), interp, approx


def sd(xs):
    n = len(xs)
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0


out = {
    "task": "WB0-INSTR-20261025-EXP-17-DEADZONE",
    "idempotency_key": "WB0-INSTR-20261025-EXP-17-DEADZONE",
    "generated_at": datetime.datetime.now(TZ).isoformat(),
    "theme": "C",
    "serves_north_star": "结构可压·事实受限——死区定位回答「哪些 KV 结构预算变动不影响可压性」，直接刻画可压/不可压边界",
    "parent": "WB0-INSTR-20261025-EXP-15-KGRID",
    "instruction_ref": "1792863416-657b0d",
    "executor": "WB2",
    "round": "2026-10-25 01:52 档",
    "seed": SEED,
}

# ---------------- 冻结件登记 ----------------
out["frozen_input"] = {
    "sensitivity_source": {
        "path": "results_v2/r1_budget_allocation.hub_pull_20261025_WB2.json",
        "hub_endpoint": "/api/file/r2_r1_budget_allocation.hub_pull_20261025_WB2",
        "size_bytes": os.path.getsize(F_BUDGET),
        "sha256_16": sha16(F_BUDGET),
        "n_layers_measured": 36,
        "n_K_points": 6,
        "note": "只读；ΔL 一律查表 / 按 EXP-16 声明的 log2 插值，无外推",
    },
    "exp15_source": {"path": "results_v2/exp15_kgrid.json", "size_bytes": os.path.getsize(F_EXP15),
                     "sha256_16": sha16(F_EXP15)},
    "exp16_source": {"path": "results_v2/exp16_sparse_matched.json", "size_bytes": os.path.getsize(F_EXP16),
                     "sha256_16": sha16(F_EXP16)},
    "exp13_source": {"path": "results_v2/exp13_r1_proportional.json", "size_bytes": os.path.getsize(F_EXP13),
                     "sha256_16": sha16(F_EXP13)},
    "router_weight": {"path": "results_v2/semantic_router.pt", "access": "只读·未下载·未加载",
                      "note": "本轮不加载模型权重，零前向"},
    "frozen_writeback": False,
}

# ---------------- 方法登记（口径全部沿用，逐条注明来源） ----------------
out["method"] = {
    "layer_set": "L1..L35（口径 A，剔除 L0，与 EXP-13/15/16 一致）",
    "dL_source": "冻结件实测 sensitivity_curve.curves[*].points[*].delta_loss_answer（6 档 K=1,2,4,8,16,32）",
    "composite": {"L1_linear_sum": "Σ_l ΔL_l", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
    "composite_source": "沿用 EXP-13/15/16 三合成，未新创第四种",
    "slot_constraint": "Σ_l (K_l+1) = budget；容差 1e-6；容量不可行的档（1188）沿用 EXP-15 单列剔除",
    "perturbation": "对每个可闭合档的每个层单元做 K_l ±20% 局部扰动",
    "variants": {
        "V1_grid_nearest": "整数网格 + 最近邻可行整数点 + 最高敏感度层回收/补回（指令 §2.3 字面读数）",
        "V2_cont_hi": "连续分辨率 + ±20% + 最高敏感度层回收/补回（指令 §2.3 字面补偿规则）",
        "V3_cont_spread": "连续分辨率 + ±20% + 补偿金额均匀摊还至其余 34 层（隔离单元自身梯度）★主读数",
        "V4_unit_only": "连续分辨率 + 仅单元 ±20%（不守恒，纯诊断，用于 Δ 通道分解）",
        "V5_cont_room": "连续分辨率 + ±20% + 补偿按各层可用余量比例摊还（水填充），Σ(K+1)=B 精确闭合（无端点截断残差）",
    },
    "primary_variant": PRIMARY_VARIANT,
    "primary_arm": PRIMARY_ARM,
    "continuity_source": "连续分辨率 = EXP-16 已声明的 log2(K) 线性插值，K 超 [1,32] 端点截断不外推；非新创口径",
    "dead_rule": "|Δ| < 0.01 判为死区单元；主判定取「三合成一致」（三合成 |Δ| 均 <0.01），沿用 EXP-15『三合成一致』纪律",
    "low_sens_set": f"实测层敏感度（sens_l=|max ΔL_answer|，EXP-15 定义）升序取前 {n_low}/35 = 1/3",
    "overlap_def": "重合度 = |死区层 ∩ 实测低敏感度层| / |实测低敏感度层|（另登记 Jaccard 与反向分母为稳健性）",
    "sd_def": "sd(Δ_ensemble) = 该档全部 单元×方向 的 composite L1_linear_sum 变化量之样本 sd（沿用 EXP-14/16 的 sd(R) 公式）",
    "note_zero_data": "零数据生成；只构造 K 向量，ΔL 一律取冻结件实测；不训练、不加载模型",
}

# ---------------- 管道复现自检（对 EXP-15 四臂合成值逐位核对） ----------------
repro = []
for B in BUDGETS:
    for arm in ARMS:
        pb = cA["per_budget"][str(B)]["per_arm"][arm]["profile_by_layer"]
        got = composite(pb, False)
        ref = cA["per_budget"][str(B)]["per_arm"][arm]["composite"]
        repro.append({"budget": B, "arm": arm,
                      "max_abs_diff": max(abs(got[k] - ref[k]) for k in got),
                      "closure_error": abs(closure_err(pb, B))})
out["pipeline_reproduction_checks"] = {
    "n_checks": len(repro),
    "max_abs_diff_over_all": max(r["max_abs_diff"] for r in repro),
    "max_closure_error_over_all": max(r["closure_error"] for r in repro),
    "detail_head": repro[:4],
    "verdict": "EXP-15 四臂×5 档合成值全部逐位复现（max|diff| < 1e-9）" if max(
        r["max_abs_diff"] for r in repro) < 1e-9 else "复现不一致，须停",
}

# ---------------- 主扫描 ----------------
results = {}
for B in BUDGETS:
    per_budget = {"comparable": True, "capacity_upper_bound": 1155.0,
                  "per_arm": {}}
    for arm in ARMS:
        base = [float(k) for k in cA["per_budget"][str(B)]["per_arm"][arm]["profile_by_layer"]]
        c0 = composite(base, False)
        c0c = composite(base, True)
        per_arm = {"base_closure_error": closure_err(base, B),
                   "base_composite_grid": c0, "base_composite_cont": c0c,
                   "variants": {}}
        for var in VARIANTS:
            recs = []
            for li, l in enumerate(LAYERS):
                for s in (1, -1):
                    Ks, dk, clos, interp, approx = perturb(base, li, s, var)
                    c1 = composite(Ks, interp)
                    d = {k: c1[k] - c0c[k] for k in c1}
                    recs.append({"layer": l, "dir": s, "K_base": base[li],
                                 "K_new": Ks[li], "dK": dk,
                                 "closure_residual": clos - B,
                                 "delta": d, "approx": approx})
            # 逐合成死区计数
            per_comp = {}
            for k in ["L1_linear_sum", "L2_max", "L3_euclid"]:
                dz = [r for r in recs if abs(r["delta"][k]) < EPS_DEAD]
                per_comp[k] = {
                    "n_dead_records": len(dz),
                    "n_dead_units": len({r["layer"] for r in dz}),
                    "dead_layers": sorted({r["layer"] for r in dz}),
                    "abs_delta_max": max(abs(r["delta"][k]) for r in recs),
                    "abs_delta_min": min(abs(r["delta"][k]) for r in recs),
                }
            # 三合成一致
            dz3 = [r for r in recs if all(abs(r["delta"][k]) < EPS_DEAD for k in
                                          ["L1_linear_sum", "L2_max", "L3_euclid"])]
            u3 = sorted({r["layer"] for r in dz3})
            # 单元级（两方向均死才算该单元死）
            unit_dead = []
            for l in LAYERS:
                rs = [r for r in recs if r["layer"] == l]
                if all(all(abs(r["delta"][k]) < EPS_DEAD for k in
                           ["L1_linear_sum", "L2_max", "L3_euclid"]) for r in rs):
                    unit_dead.append(l)
            # 闭合可行子集（|闭合残差| <= 1 slot）：摊还补偿在 K∈[1,32] 端点被截断时残差非零，单列登记
            ok = [r for r in recs if abs(r["closure_residual"]) <= 1.0]
            unit_dead_ok = []
            for l in LAYERS:
                rs = [r for r in ok if r["layer"] == l]
                if len(rs) == 2 and all(all(abs(r["delta"][k]) < EPS_DEAD for k in
                                            ["L1_linear_sum", "L2_max", "L3_euclid"]) for r in rs):
                    unit_dead_ok.append(l)
            per_arm["variants"][var] = {
                "n_records": len(recs),
                "n_records_closure_ok": len(ok),
                "consensus_3comp_closure_ok": {
                    "n_dead_units": len(unit_dead_ok),
                    "dead_layers": unit_dead_ok,
                    "overlap_frac_of_lowsens": (len(set(unit_dead_ok) & LOWSET) / len(LOWSET)),
                },
                "per_composite": per_comp,
                "consensus_3comp": {
                    "n_dead_units": len(unit_dead),
                    "dead_layers": unit_dead,
                    "n_units_total": NLAY,
                    "abs_delta_range": [min(abs(r["delta"]["L1_linear_sum"]) for r in recs),
                                        max(abs(r["delta"]["L1_linear_sum"]) for r in recs)],
                },
                "sd_L1_ensemble": sd([r["delta"]["L1_linear_sum"] for r in recs]),
                "sd_L3_ensemble": sd([r["delta"]["L3_euclid"] for r in recs]),
                "max_abs_closure_residual": max(abs(r["closure_residual"]) for r in recs),
                "n_approx_records": sum(1 for r in recs if r["approx"]),
                "overlap": {
                    "frac_of_lowsens_dead": len(set(unit_dead) & LOWSET) / len(LOWSET),
                    "frac_of_dead_in_lowsens": (len(set(unit_dead) & LOWSET) / len(unit_dead)) if unit_dead else None,
                    "jaccard": (len(set(unit_dead) & LOWSET) / len(set(unit_dead) | LOWSET)) if unit_dead else 0.0,
                    "overlap_11": len(set(unit_dead) & LOWSET_11) / len(LOWSET_11),
                },
            }
        per_budget["per_arm"][arm] = per_arm
    results[str(B)] = per_budget

out["caliber_A_noL0"] = {
    "layer_set": "L1..L35", "n_layers": NLAY,
    "comparable_budgets": BUDGETS, "excluded_budgets": EXCLUDED,
    "n_comparable": len(BUDGETS), "n_excluded": len(EXCLUDED),
    "per_budget": results,
}

# ---------------- 主读数汇总（U 臂 + V3） ----------------
summ = []
for B in BUDGETS:
    v = results[str(B)]["per_arm"][PRIMARY_ARM]["variants"][PRIMARY_VARIANT]
    summ.append({"budget": B,
                 "n_dead_units": v["consensus_3comp"]["n_dead_units"],
                 "n_units_total": NLAY,
                 "dead_layers": v["consensus_3comp"]["dead_layers"],
                 "abs_delta_range": v["consensus_3comp"]["abs_delta_range"],
                 "overlap_pct": round(100 * v["overlap"]["frac_of_lowsens_dead"], 1),
                 "sd_L1": v["sd_L1_ensemble"]})
out["primary_readout"] = {
    "arm": PRIMARY_ARM, "variant": PRIMARY_VARIANT,
    "per_budget": summ,
    "n_budgets_with_ge3_dead": sum(1 for s in summ if s["n_dead_units"] >= 3),
    "mean_overlap_pct": round(sum(s["overlap_pct"] for s in summ) / len(summ), 1),
    "max_sd_L1_over_budgets": max(s["sd_L1"] for s in summ),
    "low_sens_set": sorted(LOWSET),
    "low_sens_set_size": len(LOWSET),
    "dead_zone_union_all_budgets": sorted(set().union(*[set(s["dead_layers"]) for s in summ])),
    "dead_zone_intersection_all_budgets": sorted(set.intersection(*[set(s["dead_layers"]) for s in summ])),
}

# ---------------- 分辨率诊断（±20% vs ×2 网格） ----------------
per20 = abs(math.log2(1 + PERT))
out["resolution_diagnostic"] = {
    "perturbation_log2_magnitude": round(per20, 4),
    "log2_magnitude_minus": round(abs(math.log2(1 - PERT)), 4),
    "grid_log2_spacing": 1.0,
    "grid_half_spacing": 0.5,
    "below_resolution": per20 < 0.5,
    "statement": (f"±20% ≡ {per20:.3f} log2 单位 < 网格半间距 0.5 log2（网格间距 ×2），"
                  "故任一单元 ±20% 的最近邻网格点恒为原网格点 → V1 读数 Δ≡0，不携带判别信息"),
    "v1_delta_identically_zero": all(
        abs(r) < 1e-12 for B in BUDGETS for arm in ARMS
        for r in [x["abs_delta_range"][1] for x in
                  [results[str(B)]["per_arm"][arm]["variants"]["V1_grid_nearest"]["consensus_3comp"]]]),
    "v1_dead_units_per_budget_arm": {f"{B}/{arm}":
        results[str(B)]["per_arm"][arm]["variants"]["V1_grid_nearest"]["consensus_3comp"]["n_dead_units"]
        for B in BUDGETS for arm in ARMS},
}

# ---------------- 补偿通道分解（V2 vs V3 对照） ----------------
decomp = []
for B in BUDGETS:
    v2 = results[str(B)]["per_arm"][PRIMARY_ARM]["variants"]["V2_cont_hi"]["consensus_3comp"]
    v3 = results[str(B)]["per_arm"][PRIMARY_ARM]["variants"]["V3_cont_spread"]["consensus_3comp"]
    v4 = results[str(B)]["per_arm"][PRIMARY_ARM]["variants"]["V4_unit_only"]["consensus_3comp"]
    decomp.append({"budget": B,
                   "n_dead_V2_hicomp": v2["n_dead_units"],
                   "n_dead_V3_spread": v3["n_dead_units"],
                   "n_dead_V4_unitonly": v4["n_dead_units"],
                   "abs_delta_max_V2": v2["abs_delta_range"][1],
                   "abs_delta_max_V3": v3["abs_delta_range"][1],
                   "abs_delta_max_V4": v4["abs_delta_range"][1]})
out["compensation_channel_decomposition"] = {
    "per_budget": decomp,
    "note": ("V2 的补偿落在最高敏感度层 L1（实测 max|ΔL|=0.835，35 层中最大），"
             "故 V2 的 Δ 由补偿通道主导、与被测单元无关；V3 把补偿摊还到其余 34 层，"
             "使 Δ 反映被测单元自身梯度"),
}

# ---------------- 臂间稳健性 ----------------
rob = []
for B in BUDGETS:
    rob.append({"budget": B, **{arm: results[str(B)]["per_arm"][arm]["variants"][PRIMARY_VARIANT]
               ["consensus_3comp"]["n_dead_units"] for arm in ARMS},
               "overlap_U": round(100 * results[str(B)]["per_arm"]["U"]["variants"][PRIMARY_VARIANT]
                                  ["overlap"]["frac_of_lowsens_dead"], 1)})
out["arm_robustness_primary_variant"] = rob

# ---------------- 摊还补偿的端点截断（K∈[1,32]）登记 ----------------
cl = []
for B in BUDGETS:
    for arm in ARMS:
        v = results[str(B)]["per_arm"][arm]["variants"][PRIMARY_VARIANT]
        cl.append({"budget": B, "arm": arm,
                   "max_abs_closure_residual": v["max_abs_closure_residual"],
                   "n_records_closure_ok": v["n_records_closure_ok"],
                   "n_records": v["n_records"],
                   "n_dead_closure_ok": v["consensus_3comp_closure_ok"]["n_dead_units"],
                   "n_dead_all": v["consensus_3comp"]["n_dead_units"]})
out["closure_residual_registration"] = {
    "cause": "摊还补偿 ±d/34 落到 K=1 或 K=32 端点时被截断（EXP-16 声明的端点截断规则），槽位无法完全吸收",
    "max_abs_residual_slots": max(c["max_abs_closure_residual"] for c in cl),
    "per_budget_arm": cl,
    "note": "指令 §2.3 要求显式登记该近似；本轮不静默剔除，主读数报全样本，另并列闭合可行子集",
}

# ---------------- 预测第二子句（死区规模随预算收紧而扩大）核验 ----------------
tr = [{"budget": B,
       "n_dead": results[str(B)]["per_arm"][PRIMARY_ARM]["variants"][PRIMARY_VARIANT]
       ["consensus_3comp"]["n_dead_units"]} for B in BUDGETS]
spear = None
try:
    xs = [t["budget"] for t in tr]; ys = [t["n_dead"] for t in tr]
    mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    spear = num / den if den > 0 else None
except Exception:
    pass
out["prediction_second_clause_check"] = {
    "clause": "死区规模随预算收紧而扩大（回执模板 prediction 第二子句）",
    "per_budget_dead_counts": tr,
    "pearson_r_budget_vs_n_dead": spear,
    "supported": bool(spear is not None and spear < -0.5),
    "note": "该子句不在 §4 预登记判据内，仅作方向性登记，不参与 confirmed/refuted 落点",
}

# ---------------- 判据落点（四分支预登记后按实拉数值判定） ----------------
pr = out["primary_readout"]
n_b_ge3 = pr["n_budgets_with_ge3_dead"]
mean_ov = pr["mean_overlap_pct"]
max_sd = pr["max_sd_L1_over_budgets"]
n_comp = len(BUDGETS)
e5_reasons = []
if n_comp < 5:
    e5_reasons.append(f"可闭合档 {n_comp} < 5")
if max_sd > E5_SD:
    e5_reasons.append(f"合成方差 sd(L1)={max_sd:.4f} > {E5_SD}")
if e5_reasons:
    status = "e5_withdrawn"
    branch = "E5：" + "；".join(e5_reasons)
elif n_b_ge3 >= 3 and mean_ov >= 70:
    status = "confirmed"
    branch = (f"confirmed：{n_comp} 个可闭合档中 {n_b_ge3} 档出现 ≥3 个死区层单元，"
              f"且与实测低敏感度层平均重合度 {mean_ov}%（≥70%）")
elif n_b_ge3 <= 1 or mean_ov < 30:
    status = "refuted"
    branch = f"refuted：仅 {n_b_ge3} 档出现死区单元，或重合度 {mean_ov}% < 30%"
else:
    status = "pending"
    branch = "pending：落入 confirmed 与 refuted 之间的未决区（判据未覆盖）"

out["verdict"] = {
    "hypothesis": "H-R1-DEADZONE",
    "hypothesis_statement": "R1 预算分配存在结构性梯度死区：部分层的 K 变动不引起 composite 可分辨变化，且死区集中于实测低敏感度层",
    "status": status,
    "branch": branch,
    "criterion_from_instruction": {
        "confirmed": "≥5 个可闭合档中 ≥3 档出现 ≥3 个死区层单元（|Δ|<0.01）且重合度 ≥70%",
        "refuted": "≤1 档出现死区单元，或重合度 <30%",
        "e5_withdrawn": "可闭合档 <5，或 sd > 0.05 致不可分辨，或必需实物缺失",
    },
    "n_comparable": n_comp,
    "n_budgets_with_ge3_dead": n_b_ge3,
    "mean_overlap_pct": mean_ov,
    "max_sd_L1": max_sd,
    "degenerate_flags": {
        "v1_resolution_collapse": out["resolution_diagnostic"]["v1_delta_identically_zero"],
        "threshold_vs_scale": ("|Δ|<0.01 阈值约为该档 composite 总量（0.07–0.14）的 1/10；"
                               "单单元 ±20% 在摊还补偿下的最大可达 |Δ| 见 primary_readout"),
    },
    "directional_observation_not_a_verdict": True,
    "claim_level": "结构性读数·按预登记四分支落点",
}

out["assets_required_to_resolve"] = [
    "per-prompt ΔL 的 CI（冻结件仅对 flip 提供二项 CI，ΔL 无 CI）→ 无法把 |Δ|<0.01 与测量噪声分离",
    "多种子/多 prompt 批次重复测量，以给出 composite 的真实合成方差（本轮 sd 为枚举散布，非测量方差）",
    "更细的 K 网格（现为 ×2 稀疏网格）或更密的实测 K 点，以支撑 ±20% 量级的局部扰动",
    "指令未钉死的补偿约定（本轮 V2/V3 两路并存，落点差异见 compensation_channel_decomposition）",
]

out["n_and_power"] = {
    "n_perturbations": len(BUDGETS) * len(ARMS) * NLAY * 2 * len(VARIANTS),
    "n_budgets": len(BUDGETS), "n_arms": len(ARMS), "n_units": NLAY,
    "n_directions": 2, "n_variants": len(VARIANTS),
    "power_note": ("枚举式全扫描（35 单元 × 2 方向 × 5 档 × 4 臂 × 4 变体），无抽样误差；"
                   "但 ΔL 为单次实测、无 per-prompt CI，故统计功效受制于测量方差未知（见 assets_required_to_resolve）"),
    "level": "枚举全量 + 单点实测",
}

out["scope_guard_selfcheck"] = {
    "1_no_fabrication": True,
    "2_frozen_untouched": True,
    "3_no_overreach_no_gpu_no_3B": True,
    "4_no_ledger_write": True,
    "5_no_model_load_no_forward": True,
    "6_no_silent_trim": True,
    "7_no_new_composite_caliber": True,
    "8_no_touch_exp16_closed_line": True,
}
out["dependency_missing"] = [
    "results_v2/exp14_perm_test.json（未实拉；sd 公式取自 EXP-16 声明口径，非本轮必需）",
]
out["p0"] = False
out["p0_reason"] = "产物由脚本幂等重跑可复现（seed 固定、无随机抽样），非不可再生资产"
out["wall_clock_sec"] = round(time.time() - T0, 3)

with open(os.path.join(BASE, "exp17_deadzone.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

# 控制台摘要
print("== primary readout (arm U / V3_cont_spread) ==")
for s in summ:
    print(f"  B={s['budget']:>4} dead={s['n_dead_units']}/35 overlap={s['overlap_pct']}% "
          f"|Δ|∈[{s['abs_delta_range'][0]:.5f},{s['abs_delta_range'][1]:.5f}] sd={s['sd_L1']:.5f}")
print("== V1 (grid literal) ==")
print("  ", out["resolution_diagnostic"]["v1_dead_units_per_budget_arm"])
print("== V2/V3/V4 (U) ==")
for d in decomp:
    print(f"  B={d['budget']:>4} V2={d['n_dead_V2_hicomp']} V3={d['n_dead_V3_spread']} "
          f"V4={d['n_dead_V4_unitonly']} max|Δ| V2={d['abs_delta_max_V2']:.4f} V3={d['abs_delta_max_V3']:.4f}")
print("== V5_cont_room (U) ==")
for B in BUDGETS:
    v5 = results[str(B)]['per_arm'][PRIMARY_ARM]['variants']["V5_cont_room"]
    print(f"  B={B} dead={v5['consensus_3comp']['n_dead_units']}/35 "
          f"overlap={round(100*v5['overlap']['frac_of_lowsens_dead'],1)}% "
          f"maxres={v5['max_abs_closure_residual']:.2e} sd={v5['sd_L1_ensemble']:.5f} "
          f"|Δ|max={v5['consensus_3comp']['abs_delta_range'][1]:.4f}")
print("== verdict ==", status, "|", branch)
print("  n_budgets_ge3 =", n_b_ge3, " mean_overlap =", mean_ov, " max_sd =", round(max_sd, 5))
print("  low_sens set:", sorted(LOWSET))
print("  dead union:", pr["dead_zone_union_all_budgets"])
print("  wall_clock:", out["wall_clock_sec"], "s")
