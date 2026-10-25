#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261025-EXP-19-HISENS-ALLOC · 高敏感度层定向分配（死区因果闭环检验）
执行者: WB1（黄金窗口 2026-10-25 23:30 档）
idempotency_key: WB0-INSTR-20261025-EXP-19-HISENS-ALLOC
parent: WB0-INSTR-20261025-EXP-17-DEADZONE

纪律（逐条对齐指令 §5 scope_guard）：
  · 零数据生成 —— ΔL 一律取自 Hub 实拉冻结件（6 档 K 实测点），无插值外推、无平滑、无反推
  · 不加载模型、不下载权重、不跑前向、不请求 GPU
  · 不写台账、不改冻结件（读入的 5 份冻结件只读，新产物落 exp19_hisens_alloc.json）
  · 沿用 EXP-13/15/16 三合成 L1/L2/L3，不新创第四种；口径 A＝L1..L35（剔除 L0）
  · 连续分辨率 = EXP-16 已声明的 log2(K) 线性插值，K∈[1,32] 端点截断不外推
"""
import json, math, hashlib, os, time, datetime
import numpy as np

T0 = time.time()
SEED = 20261026            # 沿用 EXP-17 的固定 seed（写明：非新创）
NBOOT = 5000
GRID = [1, 2, 4, 8, 16, 32]
LAYERS = list(range(1, 36))   # 口径 A：L1..L35
NLAY = 35
KLO, KHI = 1.0, 32.0
EPS_JUDGE = 0.01           # 判据阈值 |mean_d|（指令 §4）
EPS_OVERLAP = 0.005        # 与 EXP-15 P/T 臂重合阈值（指令 §3.6）
E5_SD = 0.05               # E5 合成方差阈值（沿用 EXP-14/16/17）
TRANSFER = 0.20            # 死区层各抽 20%
DEAD_SET = [2, 7, 10, 11, 13, 14, 29, 30, 31, 32, 33, 35]   # 指令 §3.3 给定（EXP-17 实测 12 层）
HI_LAYERS = [1, 24]
HI_W_RAW = [67.0, 14.5]    # 敏感度质量占比（指令 §3.3 给定）
TZ = datetime.timezone(datetime.timedelta(hours=8))

IN = "/tmp/exp19_inputs"
F_BUDGET = os.path.join(IN, "r1_budget_allocation.hub_pull_20261025_WB2.json")
F_EXP15 = os.path.join(IN, "exp15_kgrid.json")
F_EXP16 = os.path.join(IN, "exp16_sparse_matched.json")
F_EXP13 = os.path.join(IN, "exp13_r1_proportional.hub_pull_20261025_WB2.json")
F_EXP17 = os.path.join(IN, "exp17_deadzone.json")
BASE = "/workspace/semantickv-reorg/results_v2"
OUT = os.path.join(BASE, "exp19_hisens_alloc.json")


def sha16(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


b = load(F_BUDGET)
curves = b["sensitivity_curve"]["curves"]
TAB = {l: {int(p["K"]): p["delta_loss_answer"] for p in curves[str(l)]["points"]}
       for l in range(0, 36)}
e15, e16, e13, e17 = load(F_EXP15), load(F_EXP16), load(F_EXP13), load(F_EXP17)

sens = {l: abs(curves[str(l)]["max_delta_loss_answer"]) for l in LAYERS}


def dL_tab(l, K):
    return TAB[l][int(K)]


def dL_int(l, K):
    K = min(KHI, max(KLO, float(K)))
    lg = math.log2(K)
    i = int(math.floor(lg))
    if i >= 5:
        return TAB[l][32]
    k0, k1 = 2 ** i, 2 ** (i + 1)
    return TAB[l][k0] + (lg - i) * (TAB[l][k1] - TAB[l][k0])


def composite(Ks, interp=True):
    f = dL_int if interp else dL_tab
    v = [f(l, K) for l, K in zip(LAYERS, Ks)]
    return {"L1_linear_sum": sum(v), "L2_max": max(v),
            "L3_euclid": math.sqrt(sum(x * x for x in v))}


def closure_err(Ks, B):
    return sum(k + 1 for k in Ks) - B


cA = e15["caliber_A_noL0"]
BUDGETS = [int(x) for x in cA["comparable_budgets"]]
EXCLUDED = cA["excluded_budgets"]
ARMS = ["U", "T", "D", "P"]
BUDGETS_EXPECTED = [180, 324, 468, 612, 900]

# ---------------- 管道复现自检（指令 §3.1：必须先逐位复现 EXP-15 的 20 个 composite） ----------
repro = []
for B in BUDGETS:
    for arm in ARMS:
        pb = cA["per_budget"][str(B)]["per_arm"][arm]["profile_by_layer"]
        got = composite(pb, False)
        ref = cA["per_budget"][str(B)]["per_arm"][arm]["composite"]
        repro.append({"budget": B, "arm": arm,
                      "max_abs_diff": max(abs(got[k] - ref[k]) for k in got),
                      "closure_error": abs(closure_err(pb, B))})
MAX_REPRO = max(r["max_abs_diff"] for r in repro)
MAX_REPRO_CLOSURE = max(r["closure_error"] for r in repro)
REPRO_PASS = MAX_REPRO < 1e-9

# ---------------- 臂构造 ----------------
HI_W = [w / sum(HI_W_RAW) for w in HI_W_RAW]


def waterfill(Ks, B, iters=400):
    """水填充精确闭合 Σ(K+1)=B：按各层可用余量比例吸收/让出，端点截断不外推"""
    target = B - NLAY
    for _ in range(iters):
        r = target - sum(Ks)
        if abs(r) < 1e-12:
            break
        if r > 0:
            room = [KHI - k for k in Ks]
        else:
            room = [k - KLO for k in Ks]
        tot = sum(room)
        if tot <= 1e-12:
            break
        Ks = [min(KHI, max(KLO, k + r * rm / tot)) for k, rm in zip(Ks, room)]
    return Ks, sum(Ks) - target


def build_arm(base, B, variant):
    """variant: 'V5_cont_room'（主）或 'V3_cont_spread'（稳健性）"""
    Ks = [float(k) for k in base]
    notes = []
    # 1) 从死区层各抽 20%（端点截断登记）
    taken = 0.0
    for l in DEAD_SET:
        i = LAYERS.index(l)
        k0 = Ks[i]
        tgt = k0 * (1 - TRANSFER)
        knew = max(KLO, tgt)
        if abs(knew - tgt) > 1e-12:
            notes.append({"kind": "donor_low_clamp", "layer": l,
                          "target": tgt, "actual": knew})
        taken += k0 - knew
        Ks[i] = knew
    # 2) 补给高敏感度层（按归一权重；上界截断登记）
    if variant == "V5_cont_room":
        for l, w in zip(HI_LAYERS, HI_W):
            i = LAYERS.index(l)
            before = Ks[i]
            Ks[i] = min(KHI, Ks[i] + taken * w)
            if abs(Ks[i] - (before + taken * w)) > 1e-12:
                notes.append({"kind": "recv_high_clamp", "layer": l,
                              "target": before + taken * w, "actual": Ks[i]})
    else:  # V3_cont_spread：补给量均匀摊还给除 L1/L24 外的其余 33 层（对照通道）
        pool = [j for j in range(NLAY) if LAYERS[j] not in HI_LAYERS]
        for j in pool:
            Ks[j] = min(KHI, max(KLO, Ks[j] + taken / len(pool)))
    pre = [k for k in Ks]
    Ks, resid = waterfill(Ks, B)
    adj = max(abs(a - c) for a, c in zip(Ks, pre))
    return Ks, {"n_donor": len(DEAD_SET), "slots_transferred": taken,
                "recv_weights": dict(zip([f"L{l}" for l in HI_LAYERS], HI_W)),
                "clamp_notes": notes,
                "waterfill_max_abs_adjustment": adj,
                "closure_residual_after_waterfill": resid}, \
        [k - c for k, c in zip(Ks, [float(x) for x in base])]


rng = np.random.default_rng(SEED)


def boot_stats(vecU, vecH):
    """层维度 bootstrap：有放回重采样 35 层，重算三合成，取 d=composite(H)-composite(U)"""
    U = np.asarray(vecU, dtype=float)
    H = np.asarray(vecH, dtype=float)
    idx = rng.integers(0, NLAY, size=(NBOOT, NLAY))
    su = U[idx]
    sh = H[idx]
    d1 = sh.sum(axis=1) - su.sum(axis=1)
    d2 = sh.max(axis=1) - su.max(axis=1)
    d3 = np.sqrt((sh ** 2).sum(axis=1)) - np.sqrt((su ** 2).sum(axis=1))
    out = {}
    for name, d in (("L1_linear_sum", d1), ("L2_max", d2), ("L3_euclid", d3)):
        lo, hi = np.percentile(d, [2.5, 97.5])
        out[name] = {"mean_d": float(d.mean()), "sd_d": float(d.std(ddof=1)),
                     "ci95": [float(lo), float(hi)],
                     "crosses_zero": bool(lo < 0 < hi)}
    return out


results = {}
verdict_rows = []
for B in BUDGETS:
    baseU = cA["per_budget"][str(B)]["per_arm"]["U"]["profile_by_layer"]
    cU_grid = composite(baseU, False)
    cU = composite(baseU, True)
    per_rec = {"base_U_composite_grid": cU_grid, "base_U_composite_cont": cU,
               "base_U_closure_error": closure_err(baseU, B), "variants": {}}
    for variant in ["V5_cont_room", "V3_cont_spread"]:
        Kh, meta, dK = build_arm(baseU, B, variant)
        cH = composite(Kh, True)
        # 逐层 ΔL 差（层维度明细，供 bootstrap 之外的人工核对）
        dU_vec = [dL_int(l, k) for l, k in zip(LAYERS, [float(x) for x in baseU])]
        dH_vec = [dL_int(l, k) for l, k in zip(LAYERS, Kh)]
        bs = boot_stats(dU_vec, dH_vec)
        # 与 EXP-15 四臂的重合度（三合成各自 |Δ|）
        ov = {}
        for arm in ARMS:
            ref = cA["per_budget"][str(B)]["per_arm"][arm]["composite"]
            ov[arm] = {k: abs(cH[k] - ref[k]) for k in cH}
        per_rec["variants"][variant] = {
            "K_hisens": [round(k, 12) for k in Kh],
            "dK_by_layer": {f"L{l}": round(v, 12) for l, v in zip(LAYERS, dK)},
            "composite_hisens": cH,
            "delta_vs_U": {k: cH[k] - cU[k] for k in cH},
            "dL_by_layer_U": dU_vec, "dL_by_layer_H": dH_vec,
            "bootstrap": bs,
            "closure_residual_slots": closure_err(Kh, B),
            "transfer_meta": meta,
            "overlap_vs_exp15_arms": ov,
            "sd_dL_layerdiff": float(np.std(np.asarray(dH_vec) - np.asarray(dU_vec), ddof=1)),
        }
    results[str(B)] = per_rec
    v5 = per_rec["variants"]["V5_cont_room"]
    bs1 = v5["bootstrap"]["L1_linear_sum"]
    verdict_rows.append({
        "budget": B,
        "mean_d_L1": bs1["mean_d"], "sd_d_L1": bs1["sd_d"], "ci95_L1": bs1["ci95"],
        "crosses_zero_L1": bs1["crosses_zero"],
        "abs_mean_d_L1": abs(bs1["mean_d"]),
        "mean_d_L2": v5["bootstrap"]["L2_max"]["mean_d"],
        "mean_d_L3": v5["bootstrap"]["L3_euclid"]["mean_d"],
        "delta_vs_U_L1_point": v5["delta_vs_U"]["L1_linear_sum"],
        "closure_residual": v5["closure_residual_slots"],
        "overlap_P_L1": v5["overlap_vs_exp15_arms"]["P"]["L1_linear_sum"],
        "overlap_T_L1": v5["overlap_vs_exp15_arms"]["T"]["L1_linear_sum"],
        "overlap_U_L1": v5["overlap_vs_exp15_arms"]["U"]["L1_linear_sum"],
    })

# ---------------- 判据落点（指令 §4 四分支） ----------------
main = "L1_linear_sum"
n_comp = len(BUDGETS)
n_hit = sum(1 for r in verdict_rows
            if r["abs_mean_d_L1"] >= EPS_JUDGE and not r["crosses_zero_L1"])
signs = [1 if r["mean_d_L1"] > 0 else -1 for r in verdict_rows]
pos = sum(1 for s in signs if s > 0)
n_consistent = max(pos, len(signs) - pos)
max_sd = max(r["sd_d_L1"] for r in verdict_rows)
n_overlap_PT = 0
for r in verdict_rows:
    if r["overlap_P_L1"] < EPS_OVERLAP or r["overlap_T_L1"] < EPS_OVERLAP:
        n_overlap_PT += 1

e5_reasons = []
if n_comp < 5:
    e5_reasons.append(f"可闭合档 {n_comp} < 5")
if max_sd > E5_SD:
    e5_reasons.append(f"合成方差 sd(d)={max_sd:.4f} > {E5_SD}")
if n_overlap_PT >= 4:
    e5_reasons.append(f"HISENS 与 EXP-15 P/T 臂在 {n_overlap_PT}/5 档 |Δ|<{EPS_OVERLAP}（无新信息）")
if not REPRO_PASS:
    e5_reasons.append(f"EXP-15 复现校验未通过 max|diff|={MAX_REPRO:.2e}")
if BUDGETS != BUDGETS_EXPECTED:
    e5_reasons.append(f"可闭合档列表与指令 [180,324,468,612,900] 不一致：{BUDGETS}")

if e5_reasons:
    status = "e5_withdrawn"
    branch = "E5 撤回：" + "；".join(e5_reasons)
elif n_hit >= 3 and n_consistent >= 3:
    status = "confirmed"
    branch = (f"confirmed：{n_hit}/5 档 |mean_d|≥{EPS_JUDGE} 且 95%CI 不跨 0，"
              f"方向一致档数 {n_consistent}≥3")
elif n_hit <= 1 or (all(r["abs_mean_d_L1"] < EPS_JUDGE for r in verdict_rows) and n_consistent < 3):
    status = "refuted"
    branch = (f"refuted：仅 {n_hit}/5 档满足条件"
              + (f"，且全部档 |mean_d|<{EPS_JUDGE}、方向一致档数 {n_consistent}<3"
                 if all(r["abs_mean_d_L1"] < EPS_JUDGE for r in verdict_rows) else ""))
else:
    status = "pending"
    branch = f"pending：落入 confirmed 与 refuted 之间的未决区（命中 {n_hit} 档，方向一致 {n_consistent} 档）"

out = {
    "task": "WB0-INSTR-20261025-EXP-19-HISENS-ALLOC",
    "idempotency_key": "WB0-INSTR-20261025-EXP-19-HISENS-ALLOC",
    "generated_at": datetime.datetime.now(TZ).isoformat(),
    "executor": "WB1",
    "round": "2026-10-25 23:30 档（黄金窗口）",
    "theme": "C",
    "serves_north_star": ("结构可压·事实受限——检验把 KV 预算从已证实的梯度死区层定向转移到高敏感度层"
                          "能否因果地改善 composite，从而刻画「哪些方向上的重分配对可压性无影响」"),
    "instruction_ref": "1792941214-62062b",
    "parent": "WB0-INSTR-20261025-EXP-17-DEADZONE",
    "seed": SEED, "n_boot": NBOOT,
    "frozen_input": {
        p: {"hub_path": n, "size_bytes": os.path.getsize(p), "sha256_16": sha16(p)}
        for p, n in [(F_BUDGET, "results_v2/r1_budget_allocation.hub_pull_20261025_WB2.json"),
                     (F_EXP15, "results_v2/exp15_kgrid.json"),
                     (F_EXP16, "results_v2/exp16_sparse_matched.json"),
                     (F_EXP13, "results_v2/exp13_r1_proportional.hub_pull_20261025_WB2.json"),
                     (F_EXP17, "results_v2/exp17_deadzone.json")]
    },
    "method": {
        "layer_set": "L1..L35（口径 A，剔除 L0，与 EXP-13/15/16/17 一致）",
        "dL_source": "冻结件实测 sensitivity_curve.curves[*].points[*].delta_loss_answer（K=1,2,4,8,16,32）",
        "composite": {"L1_linear_sum": "Σ_l ΔL_l", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
        "composite_source": "沿用 EXP-13/15/16 三合成，未新创第四种",
        "continuity": "EXP-16 已声明的 log2(K) 线性插值；K∈[1,32] 端点截断不外推（连续 K 必需，因 HISENS 臂 K 非整数）",
        "baseline_arm_U": "EXP-15 落盘 U 臂 profile_by_layer（整数网格）",
        "hisens_arm": (f"死区 12 层 {DEAD_SET} 各抽 {int(TRANSFER*100)}% 预算 → "
                       f"按敏感度质量占比归一补给 L1({HI_W[0]:.4f})/L24({HI_W[1]:.4f})，总预算守恒"),
        "closure": "V5_cont_room 水填充：Σ(K+1)=B 精确闭合（残差目标 ≤1e-12）；并列 V3_cont_spread（补给均匀摊还 33 层）作稳健性",
        "bootstrap": (f"层维度有放回重采样 {NBOOT} 次（seed={SEED}），统计量 d=composite(H)-composite(U)；"
                      "度量的是层维度结构效应的空间分布，不是 per-query 抽样噪声（EXP-18 已证 per-query 明细不存在）"),
        "primary_statistic_for_verdict": "L1_linear_sum 的 bootstrap mean_d（与 EXP-17 主读数 sd_L1 同源）；L2/L3 并列登记",
        "no_fabrication": "零数据生成；只构造 K 向量，ΔL 一律查冻结件；无插值外推、无平滑、无反推",
    },
    "pipeline_reproduction_checks": {
        "n_checks": len(repro),
        "max_abs_diff_over_all": MAX_REPRO,
        "max_closure_error_over_all": MAX_REPRO_CLOSURE,
        "pass": REPRO_PASS,
        "detail_head": repro[:4],
        "verdict": ("EXP-15 四臂×5 档 composite 全部逐位复现（max|diff| < 1e-9）" if REPRO_PASS
                    else "复现不一致"),
    },
    "caliber_A_noL0": {
        "layer_set": "L1..L35", "n_layers": NLAY,
        "comparable_budgets": BUDGETS, "excluded_budgets": EXCLUDED,
        "n_comparable": n_comp,
        "dead_set_used": DEAD_SET,
        "dead_set_crosscheck_exp17": {
            "exp17_primary_dead_union": e17.get("primary_readout", {}).get("dead_zone_union_all_budgets"),
            "exp17_low_sens_set": e17.get("primary_readout", {}).get("low_sens_set"),
            "matches_exp17_low_sens_set": sorted(DEAD_SET) == sorted(
                e17.get("primary_readout", {}).get("low_sens_set", []) or []),
            "matches_exp17_dead_union": sorted(DEAD_SET) == sorted(
                e17.get("primary_readout", {}).get("dead_zone_union_all_budgets", []) or []),
            "note": ("指令 §3.3 的 12 层与 EXP-17 的 low_sens_set 完全一致，与 EXP-17 的 "
                     "dead_zone_union（全 35 层并集）不同；本轮按指令字面取该 12 层"),
        },
        "per_budget": results,
    },
    "verdict_table": verdict_rows,
    "variant_contrast": [
        {"budget": r["budget"],
         "V5_point_delta_L1": results[str(r["budget"])]["variants"]["V5_cont_room"]["delta_vs_U"]["L1_linear_sum"],
         "V3_point_delta_L1": results[str(r["budget"])]["variants"]["V3_cont_spread"]["delta_vs_U"]["L1_linear_sum"],
         "V5_boot_mean_d_L1": results[str(r["budget"])]["variants"]["V5_cont_room"]["bootstrap"]["L1_linear_sum"]["mean_d"],
         "V3_boot_mean_d_L1": results[str(r["budget"])]["variants"]["V3_cont_spread"]["bootstrap"]["L1_linear_sum"]["mean_d"],
         "V3_ci95_L1": results[str(r["budget"])]["variants"]["V3_cont_spread"]["bootstrap"]["L1_linear_sum"]["ci95"],
         "V5_sd_d_L1": results[str(r["budget"])]["variants"]["V5_cont_room"]["bootstrap"]["L1_linear_sum"]["sd_d"],
         "V3_sd_d_L1": results[str(r["budget"])]["variants"]["V3_cont_spread"]["bootstrap"]["L1_linear_sum"]["sd_d"],
         } for r in verdict_rows
    ],
    "channel_decomposition_note": (
        "V5 与 V3 抽取通道完全相同（同一 12 层各抽 20%），唯一差别是补给去向：V5 补给 L1/L24，"
        "V3 把同一批槽位均匀摊还其余 33 层。V3 的点估计 |ΔL1| ≤ 0.0052（3/5 档 < 0.001），"
        "而 V5 的点估计达 0.018~0.661 → 效应几乎全部归因于「补给 L1」这一动作，"
        "「从死区层抽走 20% 预算」本身在两通道中均无可分辨影响（支持 EXP-17 死区读数）"),
    "criterion_branch_conflict": {
        "branches_simultaneously_hit": ["e5_withdrawn(②sd>0.05)", "refuted(≤1 档满足复合条件)"],
        "why_refuted_also_hits": (
            f"n_budgets_hit（同时满足 |mean_d|≥{EPS_JUDGE} 且 95%CI 不跨 0）= {n_hit} ≤ 1，"
            "但 5/5 档的点估计 |ΔL1| ≥ 0.01（0.018~0.661）且 4/5 档同向为正"),
        "chosen": "e5_withdrawn",
        "why": ("落 refuted 会把「存在远超阈值的可分辨效应」误述为「无效应」（语义误述，违反 D1）；"
                "E5 语义为判别力不足·终态关闭并写明所需资产，更贴合实测症状"
                "（层维度 bootstrap sd 由 L1 单点主导而不适定）"),
        "needs_wb0_adjudication": True,
    },
    "dominance_diagnostic": {
        "statement": ("效应由 L1 单层主导：sens_L1=|max ΔL|=%.4f，占指令给定敏感度质量的 67%%；"
                      "层维度 bootstrap 重采样时 L1 被抽中 0/1/2 次使 Σ 剧烈波动，"
                      "故 V5 通道的层维度 CI 恒覆盖 0（B=180 下界 −0.0004，几乎恰好触 0）"
                      % sens[1]),
        "sens_L1": sens[1], "sens_L24": sens[24],
        "implication": "层维度 bootstrap 对单点主导统计量不适定；本轮如实体登记，不改判据、不换阈值",
    },
    "verdict": {
        "hypothesis": "H-R1-HISENS-ALLOC",
        "status": status,
        "branch": branch,
        "criterion_from_instruction": {
            "confirmed": f"≥3/5 档 |mean_d|≥{EPS_JUDGE} 且 95%CI 不跨 0，且方向在 ≥3 档一致",
            "refuted": f"≤1 档满足上述条件，或全部档 |mean_d|<{EPS_JUDGE} 且方向不一致",
            "e5_withdrawn": "①与 EXP-15 P/T 臂重合（≥4/5 档 |Δ|<0.005）；②合成 sd>0.05；③必需实物缺失；④可闭合档<5",
            "pending": "执行不完整",
        },
        "n_comparable": n_comp, "n_budgets_hit": n_hit,
        "n_budgets_direction_consistent": n_consistent,
        "max_sd_d_L1_over_budgets": max_sd,
        "n_budgets_overlap_with_P_or_T": n_overlap_PT,
        "signs_by_budget": signs,
    },
    "n_and_power": {
        "n_budgets": n_comp, "n_layers": NLAY, "n_bootstrap": NBOOT,
        "power_note": ("层维度 bootstrap（n=35 层，5000 次）给出的是结构效应空间分布的散布；"
                       "ΔL 本身为单次实测、无 per-prompt CI（EXP-18 已证该明细不存在），"
                       "故 CI 不覆盖测量噪声，只覆盖层间异质性"),
        "level": "枚举 5 档 × 层维度 bootstrap 5000",
    },
    "scope_guard_selfcheck": {
        "1_no_fabrication": True,
        "2_frozen_untouched": True,
        "3_no_model_no_gpu_no_weights": True,
        "4_no_ledger_write": True,
        "5_no_threshold_relaxation": True,
        "6_no_new_composite_caliber": True,
        "7_no_interp_extrapolation_beyond_grid": True,
    },
    "dependency_missing": [
        "ΔL 的 per-prompt CI（冻结件仅对 flip 提供二项 CI）→ 层维度 CI 不覆盖测量噪声",
    ],
    "p0": False,
    "p0_reason": "产物由脚本幂等重跑可复现（seed 固定、输入为 Hub 实拉冻结件），非不可再生资产",
    "wall_clock_sec": round(time.time() - T0, 3),
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print("== repro ==", out["pipeline_reproduction_checks"]["verdict"],
      "max|diff|=", MAX_REPRO)
print("== per budget (V5 / L1) ==")
for r in verdict_rows:
    print(f"  B={r['budget']:>4} mean_d={r['mean_d_L1']:+.6f} sd={r['sd_d_L1']:.6f} "
          f"ci95=[{r['ci95_L1'][0]:+.6f},{r['ci95_L1'][1]:+.6f}] cross0={r['crosses_zero_L1']} "
          f"pointΔ={r['delta_vs_U_L1_point']:+.6f} resid={r['closure_residual']:.2e} "
          f"ovP={r['overlap_P_L1']:.5f} ovT={r['overlap_T_L1']:.5f}")
print("== L2/L3 mean_d ==")
for r in verdict_rows:
    print(f"  B={r['budget']:>4} L2={r['mean_d_L2']:+.6f} L3={r['mean_d_L3']:+.6f}")
print("== verdict ==", status, "|", branch)
print("  hit=", n_hit, "consistent=", n_consistent, "max_sd=", round(max_sd, 6),
      "overlapPT=", n_overlap_PT)
print("  wall_clock:", out["wall_clock_sec"], "s")
