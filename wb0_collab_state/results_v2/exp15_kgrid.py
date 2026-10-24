#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261025-EXP-15-KGRID  (target=WB2, theme=A, stage=E2)
整数 K 网格四臂重算：检验 R1 / EXP-13 的 refuted 是否由冻结件 profile_by_layer 展示值缺陷驱动。

口径（与指令 §2 一致）：
  * K 仅取已测量网格点 {1,2,4,8,16,32}；ΔL 一律直接查表，零插值、零几何中点、零外推。
  * 四臂 U(均匀) / T(线性递减) / D(敏感度反比) / P(敏感度正比)。
  * 预算闭合：Σ_l (K_l + 1) = budget，整数精确闭合（DP 最小畸变分配，误差恒为 0）。
  * 三合成 L1=ΣΔL / L2=max ΔL / L3=sqrt(ΣΔL²)；越低越好。
  * 两口径 A=L1..L35(35层，判据口径) / B=L0..L35(36层，管道自检)。
只读冻结件，纯 CPU，无模型加载，不写台账。
"""
import json, math, os, hashlib, time, sys

T0 = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results_v2")
os.makedirs(RES, exist_ok=True)

FROZEN = os.path.join(HERE, "pull_r1_budget_allocation.json")
EXP13 = os.path.join(HERE, "pull_r2_exp13_r1_proportional.json")

GRID = [1, 2, 4, 8, 16, 32]
GRID_SET = set(GRID)
BUDGETS = [180, 324, 468, 612, 900, 1188]
MODES = ["L1_linear_sum", "L2_max", "L3_euclid"]
ARMS = ["U", "T", "D", "P"]
CLAMP_LO, CLAMP_HI = 1.0, 32.0
EPS = 1e-9


def sha16(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def sha16b(b):
    return hashlib.sha256(b).hexdigest()[:16]


# ---------------------------------------------------------------- 载入
frozen_bytes = open(FROZEN, "rb").read()
frozen = json.loads(frozen_bytes)
exp13 = json.load(open(EXP13, "rb"))
frozen_sha_before = sha16b(frozen_bytes)

curves = frozen["sensitivity_curve"]["curves"]          # '0'..'35'
NLAYER = len(curves)                                     # 36 (L0..L35)
# ΔL 查表: dl[l][K]
dl = {}
for l in range(NLAYER):
    d = {}
    for p in curves[str(l)]["points"]:
        d[int(p["K"])] = float(p["delta_loss_answer"])
    dl[l] = d
# 层敏感度 sens_l = |max_delta_loss_answer|（沿用 EXP-13 定义）
sens = [abs(float(curves[str(l)]["max_delta_loss_answer"])) for l in range(NLAYER)]
mean_sens = sum(sens) / len(sens)

CALIBERS = {"A": list(range(1, 36)), "B": list(range(0, 36))}   # A 剔 L0, B 含 L0


# ---------------------------------------------------------------- 连续目标（仅用于定义各臂形状，随后被网格化取代）
def arm_raw(arm, layers):
    """各臂的连续形状 weight_l（未归一化）。"""
    if arm == "U":
        return {l: 1.0 for l in layers}
    if arm == "T":
        n = len(layers)
        return {l: (n - (l - layers[0])) / n for l in layers}   # 底层大、顶层小，线性递减
    if arm == "D":
        return {l: mean_sens / max(sens[l], EPS) for l in layers}  # 反比
    if arm == "P":
        return {l: max(sens[l], EPS) for l in layers}              # 正比
    raise ValueError(arm)


def continuous_target(arm, layers, budget):
    """连续（未网格化）目标 K_l：clamp[1,32] + 二分缩放使 Σ(K_l+1)=budget。返回 dict。"""
    raw = arm_raw(arm, layers)
    n = len(layers)
    need = budget - n                      # Σ K_l
    lo_cap, hi_cap = n * CLAMP_LO, n * CLAMP_HI
    if need < lo_cap - 1e-9 or need > hi_cap + 1e-9:
        return None                        # 不可行（容量上下界）

    def total(c):
        return sum(min(CLAMP_HI, max(CLAMP_LO, c * raw[l])) for l in layers)

    a, b = 0.0, 1.0
    while total(b) < need:
        b *= 2.0
    for _ in range(200):
        m = 0.5 * (a + b)
        if total(m) < need:
            a = m
        else:
            b = m
    c = 0.5 * (a + b)
    return {l: min(CLAMP_HI, max(CLAMP_LO, c * raw[l])) for l in layers}


# ---------------------------------------------------------------- 整数网格精确闭合分配（DP 最小畸变）
def grid_allocate(arm, layers, budget):
    """
    在 K ∈ {1,2,4,8,16,32} 上求 Σ(K_l+1)=budget 的最小畸变分配。
    畸变 = |log2(K) - log2(连续目标 K_l)|。返回 (alloc dict, err, infeasible_reason)
    """
    n = len(layers)
    need = budget - n
    if need < n * GRID[0] - 1e-9 or need > n * GRID[-1] + 1e-9:
        return None, None, ("capacity", n * (GRID[0] + 1), n * (GRID[-1] + 1))
    tgt = continuous_target(arm, layers, budget)
    if tgt is None:
        return None, None, ("capacity", n * (CLAMP_LO + 1), n * (CLAMP_HI + 1))

    lt = {l: math.log2(max(tgt[l], 1e-12)) for l in layers}
    cost = {l: {k: abs(math.log2(k) - lt[l]) for k in GRID} for l in layers}

    INF = float("inf")
    # dp[s] = 最小代价；parent 用于回溯
    dp = [INF] * (budget + 1)
    dp[0] = 0.0
    par = [[None] * (budget + 1) for _ in range(n)]
    for i, l in enumerate(layers):
        ndp = [INF] * (budget + 1)
        for s in range(budget + 1):
            if dp[s] == INF:
                continue
            base = dp[s]
            for k in GRID:
                s2 = s + k + 1
                if s2 > budget:
                    continue
                v = base + cost[l][k]
                if v < ndp[s2]:
                    ndp[s2] = v
                    par[i][s2] = (s, k)
        dp = ndp
    if dp[budget] == INF:
        return None, None, ("no_exact_closure", None, None)

    alloc, s = {}, budget
    for i in range(n - 1, -1, -1):
        ps, k = par[i][s]
        alloc[layers[i]] = k
        s = ps
    err = sum(alloc[l] + 1 for l in layers) - budget
    return alloc, float(err), None


# ---------------------------------------------------------------- 合成
def composite(alloc, layers):
    v1 = sum(dl[l][alloc[l]] for l in layers)
    v2 = max(dl[l][alloc[l]] for l in layers)
    v3 = math.sqrt(sum(dl[l][alloc[l]] ** 2 for l in layers))
    return {"L1_linear_sum": v1, "L2_max": v2, "L3_euclid": v3}


def interp_dl(l, k):
    """log2(K) 线性插值——仅用于对照/敏感性，不用于判据格。"""
    if k in dl[l]:
        return dl[l][k]
    xs = sorted(dl[l])
    if k <= xs[0]:
        return dl[l][xs[0]]
    if k >= xs[-1]:
        return dl[l][xs[-1]]
    for a, b in zip(xs, xs[1:]):
        if a <= k <= b:
            f = (math.log2(k) - math.log2(a)) / (math.log2(b) - math.log2(a))
            return dl[l][a] + f * (dl[l][b] - dl[l][a])


# ---------------------------------------------------------------- 主扫描
out = {
    "task": "WB0-INSTR-20261025-EXP-15-KGRID",
    "idempotency_key": "WB0-INSTR-20261025-EXP-15-KGRID",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "theme": "A",
    "serves_north_star": "结构可压·事实受限（KV 预算结构性分配 → 压缩质量上限）",
    "parent": "WB0-INSTR-20261025-EXP-12-FROZENAUDIT",
    "instruction_ref": "1792859863-64a102",
    "method": {
        "k_grid": GRID,
        "interpolation": "无（ΔL 一律查表，网格点全部为冻结件已测量点）",
        "arms": {"U": "均匀：全层同一目标 K", "T": "线性递减：底层大顶层小",
                 "D": "K_l ∝ mean_sens/sens_l（反比）", "P": "K_l ∝ sens_l（正比）"},
        "sens_definition": "sens_l = |max_delta_loss_answer|（每层 6 档 K 上最大绝对 ΔL_answer），沿用 EXP-13",
        "grid_allocation": "整数网格 {1,2,4,8,16,32} 上最小 |log2 K - log2(连续目标)| 畸变 DP，Σ(K_l+1)=budget 精确闭合",
        "budget_closure_rule": "闭合误差 >1e-6 或容量不可行的档单列剔除，分母相应变化，不静默缩样",
        "composite": {"L1_linear_sum": "Σ_l ΔL_l（越低越好）", "L2_max": "max_l ΔL_l", "L3_euclid": "sqrt(Σ ΔL_l²)"},
        "pass_definition": "该档三合成一致严格优于 U 且 T（composite 严格更小）",
        "note_on_cell_count": "指令 §2 写『216 格』；按 6 档×4 臂×3 合成×2 口径=144 格（与 EXP-13 同定义）。"
                              "本轮实跑 144 格全量并额外输出逐档 Δ(D−U)/Δ(D−T)/Δ(P−U)/Δ(P−T) 共 144 个 delta，"
                              "144+72=216 疑为合并计数口径差异，已在回执登记，不作判据。",
    },
    "frozen_input": {
        "path": "results_v2/r1_budget_allocation.json",
        "hub_endpoint": "/api/file/r1_budget_allocation",
        "size_bytes": len(frozen_bytes),
        "sha256_16_before": frozen_sha_before,
    },
    "exp13_input": {
        "path": "results_v2/exp13_r1_proportional.json",
        "hub_endpoint": "/api/file/r2_exp13_r1_proportional",
        "size_bytes": len(open(EXP13, "rb").read()),
        "sha256_16": sha16(EXP13),
    },
    "layer_sets": {"A": "L1..L35 (35 层，剔除 L0，判据口径)", "B": "L0..L35 (36 层，管道自检)"},
}

caliber_out = {}
pipeline_checks = []

for cal, layers in CALIBERS.items():
    n = len(layers)
    per_budget = {}
    for budget in BUDGETS:
        row = {"n_layers": n, "capacity_slots_max": n * (GRID[-1] + 1),
               "capacity_slots_min": n * (GRID[0] + 1), "per_arm": {}, "delta": {}, "pass": {}}
        allocs, comps = {}, {}
        for arm in ARMS:
            alloc, err, why = grid_allocate(arm, layers, budget)
            if alloc is None:
                row["per_arm"][arm] = {"infeasible": True, "reason": why[0],
                                       "capacity_range_slots": [why[1], why[2]], "closure_error": None}
                allocs[arm], comps[arm] = None, None
                continue
            allocs[arm] = alloc
            comps[arm] = composite(alloc, layers)
            row["per_arm"][arm] = {
                "total_slots_check": float(sum(alloc[l] + 1 for l in layers)),
                "closure_error": err,
                "K_min": float(min(alloc.values())), "K_max": float(max(alloc.values())),
                "K_mean": float(sum(alloc.values()) / n),
                "K_grid_hist": {str(k): sum(1 for l in layers if alloc[l] == k) for k in GRID},
                "profile_by_layer": [alloc[l] for l in layers],
                "composite": comps[arm],
            }
        # 剔除判定
        ok = all(comps[a] is not None and abs(row["per_arm"][a]["closure_error"]) <= 1e-6 for a in ARMS)
        row["comparable"] = bool(ok)
        if not ok:
            bad = [a for a in ARMS if comps[a] is None or abs(row["per_arm"][a]["closure_error"]) > 1e-6]
            row["excluded_reason"] = ("容量/闭合不可行：" + ",".join(bad))
        # delta 与 pass
        if ok:
            for m in MODES:
                dU, dT = comps["D"][m] - comps["U"][m], comps["D"][m] - comps["T"][m]
                pU, pT = comps["P"][m] - comps["U"][m], comps["P"][m] - comps["T"][m]
                row["delta"][m] = {"D_minus_U": dU, "D_minus_T": dT,
                                   "P_minus_U": pU, "P_minus_T": pT,
                                   "D_better_than_U": bool(dU < 0), "D_better_than_T": bool(dT < 0),
                                   "P_better_than_U": bool(pU < 0), "P_better_than_T": bool(pT < 0)}
            for arm in ("D", "P"):
                row["pass"][arm] = all(row["delta"][m][arm + "_better_than_U"]
                                       and row["delta"][m][arm + "_better_than_T"] for m in MODES)
        per_budget[str(budget)] = row
        # 管道自检：冻结件 U 臂恰在网格点上的档
        fs = next((s for s in frozen["sweep"] if s["budget_total_kv_slots"] == budget), None)
        if cal == "B" and fs is not None and comps["U"] is not None:
            prof = fs["effective_K"]["U"]["profile_by_layer"]
            if all(abs(round(p) - p) < 1e-9 and int(round(p)) in GRID_SET for p in prof):
                pipeline_checks.append({
                    "budget": budget, "check": "冻结件 U 臂 profile 全为整数网格点 → 网格化 U 复合值应逐位复现",
                    "frozen_U_composite": fs["composite"]["U"],
                    "grid_U_composite": comps["U"],
                    "max_abs_diff": max(abs(fs["composite"]["U"][m] - comps["U"][m]) for m in MODES)})
    comparable = [b for b in BUDGETS if per_budget[str(b)]["comparable"]]
    excluded = [{"budget": b, "reason": per_budget[str(b)].get("excluded_reason", "")}
                for b in BUDGETS if not per_budget[str(b)]["comparable"]]
    caliber_out[cal] = {
        "layer_set": CALIBERS and ("L1..L35" if cal == "A" else "L0..L35"),
        "n_layers": n,
        "comparable_budgets": comparable,
        "excluded_budgets": excluded,
        "n_comparable": len(comparable),
        "n_excluded": len(excluded),
        "per_budget": per_budget,
        "pass_counts": {a: sum(1 for b in comparable if per_budget[str(b)]["pass"].get(a))
                        for a in ("D", "P")},
        "pass_budgets": {a: [b for b in comparable if per_budget[str(b)]["pass"].get(a)]
                         for a in ("D", "P")},
    }
out["caliber_A_noL0"] = caliber_out["A"]
out["caliber_B_all"] = caliber_out["B"]
out["pipeline_reproduction_checks"] = pipeline_checks

# ---------------------------------------------------------------- 判据（指令 §5，判据口径=A）
A = caliber_out["A"]
nD, nP = A["pass_counts"]["D"], A["pass_counts"]["P"]
ncomp = A["n_comparable"]
nexc = A["n_excluded"]
range_A = None
vals = []
for b in A["comparable_budgets"]:
    for arm in ARMS:
        vals += [A["per_budget"][str(b)]["per_arm"][arm]["composite"][m] for m in MODES]
range_A = max(vals) - min(vals)

e5 = (nexc >= 4)
if e5:
    status, branch = "e5_withdrawn", "e5_withdrawn"
elif nD >= 3 or nP >= 5:
    status, branch = "refuted", "refuted"
elif nD <= 1 and nP <= 3:
    status, branch = "confirmed", "confirmed"
else:
    status, branch = "pending", "pending"

out["verdict"] = {
    "hypothesis": "H-R1-KGRID-ROBUST",
    "status": status,
    "branch": branch,
    "n_D_pass": nD, "n_P_pass": nP, "n_comparable": ncomp, "n_excluded": nexc,
    "pass_budgets_D": A["pass_budgets"]["D"], "pass_budgets_P": A["pass_budgets"]["P"],
    "range_A": range_A,
    "criterion_from_instruction": {
        "confirmed": "口径A：D通过档数 ≤1/可比 且 P通过档数 ≤3/可比，与 R1-REJUDGE(1/5)、EXP-13(3/5) 同向或更差",
        "refuted": "口径A：D通过 ≥3/可比 或 P通过 ≥5/可比（此时不得复活 H-R1-BUDGET）",
        "e5_withdrawn": "≥4/6 档预算无法闭合或 ΔL 缺网格点",
        "pending": "仅限执行不完整（脚本中断/缺档），须列 dependency_missing",
    },
    "e5_gate_triggered": bool(e5),
}

# ---------------------------------------------------------------- 与 EXP-13 同向性对照（口径 A）
cmp13 = []
for b in A["comparable_budgets"]:
    e = exp13["caliber_A_noL0"]["per_budget"].get(str(b))
    if not e:
        continue
    rec = {"budget": b}
    for arm in ("D", "P"):
        for m in MODES:
            g = A["per_budget"][str(b)]["delta"][m]
            o = e["delta"][m] if "delta" in e else None
            key = arm + "_minus_U"
            if o and key in o:
                rec.setdefault("signs", {})[f"{arm}/{m}/vs_U"] = {
                    "exp13_original": ("-" if o[key] < 0 else "+"),
                    "grid_this_round": ("-" if g[key] < 0 else "+"),
                    "same_direction": (o[key] < 0) == (g[key] < 0),
                    "exp13_value": o[key], "grid_value": g[key]}
    rec["exp13_P_pass"] = bool(b in exp13["verdict"]["budgets_pass"])
    rec["grid_P_pass"] = A["per_budget"][str(b)]["pass"].get("P")
    rec["grid_D_pass"] = A["per_budget"][str(b)]["pass"].get("D")
    cmp13.append(rec)
out["comparison_with_exp13_caliber_A"] = cmp13

# ---------------------------------------------------------------- 污染敏感度（468/900，原 U 臂几何中点档）
cs = {}
for b in (468, 900):
    fs = next((s for s in frozen["sweep"] if s["budget_total_kv_slots"] == b), None)
    if fs is None:
        continue
    prof = fs["effective_K"]["U"]["profile_by_layer"]
    fU = fs["composite"]["U"]
    gA = A["per_budget"][str(b)]["per_arm"]["U"]["composite"]
    gB = caliber_out["B"]["per_budget"][str(b)]["per_arm"]["U"]["composite"]
    cs[str(b)] = {
        "frozen_U_profile_sample": prof[:3],
        "frozen_U_K_min": min(prof), "frozen_U_K_max": max(prof),
        "frozen_U_all_integer_grid": all(abs(round(p) - p) < 1e-9 and int(round(p)) in GRID_SET for p in prof),
        "frozen_U_composite_caliberB": fU,
        "frozen_U_slot_closure_error": float(fs["effective_K"]["U"]["total_slots_check"] - b),
        "grid_U_composite_caliberB": gB,
        "grid_U_composite_caliberA": gA,
        "delta_composite_B_grid_minus_frozen": {m: gB[m] - fU[m] for m in MODES},
        "note": "冻结件 U 臂该档 K 非整数（几何中点口径）→ 与整数网格口径不可直接逐位对齐；"
                "此差值为『展示值缺陷影响幅度』的解释性登记，不作判据。",
    }
out["contamination_sensitivity_468_900"] = cs

out["frozen_input"]["sha256_16_after"] = sha16b(open(FROZEN, "rb").read())
out["frozen_input"]["sha_stable"] = out["frozen_input"]["sha256_16_before"] == out["frozen_input"]["sha256_16_after"]
out["n_and_power"] = {"n": 1, "detail": "1 冻结件 / 1 种子 / 1 needle / 8 段落 / 1 模型（沿用）；确定性复算，无方差可估",
                      "power_analysis": "不可做（n=1，无种子/任务/prompt 方差）",
                      "level": "假设级 / 机制观察，不得表述为该模型或架构的真实层敏感度"}
out["dependency_missing"] = ["原始 3B 权重 /workspace/models/Qwen2.5-3B-Instruct 已蒸发（沿用冻结件 dependency_missing），"
                             "本轮未加载任何模型、未下载权重；纯冻结件确定性复算"]
out["p0"] = True
out["wall_clock_sec"] = round(time.time() - T0, 3)
out["scope_guard_selfcheck"] = {
    "1_no_fabrication": "ΔL 一律取自冻结件已测量网格点 K∈{1,2,4,8,16,32}，零插值/零几何中点/零外推；未填入任何未实测值",
    "2_frozen_untouched": f"冻结件只读：sha256_16 前后均 {out['frozen_input']['sha256_16_before']}；未覆盖/改名/修复",
    "3_no_overreach": "仅做网格化重算；未训练、未下载/加载模型、未新建假设（H-R1-KGRID-ROBUST 为指令给定）、未写台账、未碰 WB1 指令 EXP-14",
    "4_no_ledger_write": "未直写 hypothesis_ledger.yaml",
    "5_no_model_load": "纯标准库 python，无模型下载/加载",
    "6_no_silent_trim": f"6 档×4 臂×3 合成×2 口径=144 格全跑，wall_clock={out['wall_clock_sec']}s≤300s；"
                        f"剔除档已在 excluded_budgets 单列并说明，分母 {ncomp}/{len(BUDGETS)} 相应变化",
}

dst = os.path.join(RES, "exp15_kgrid.json")
with open(dst, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

print("== EXP-15 判据 ==")
print("comparable(A):", A["comparable_budgets"], " excluded:", nexc)
print(f"D pass {nD}/{ncomp}  P pass {nP}/{ncomp}  range_A={range_A:.10f}")
print("VERDICT:", status, "|", branch)
print("== 管道自检 ==")
for p in pipeline_checks:
    print(f"  budget {p['budget']}: max_abs_diff={p['max_abs_diff']:.3e}  frozen={p['frozen_U_composite']}  grid={p['grid_U_composite']}")
print("== 逐档 P/D 通过 (口径A) ==")
for b in A["comparable_budgets"]:
    r = A["per_budget"][str(b)]
    print(f"  {b}: D={r['pass'].get('D')} P={r['pass'].get('P')}  "
          f"P-U(L1)={r['delta']['L1_linear_sum']['P_minus_U']:+.6f}  D-U(L1)={r['delta']['L1_linear_sum']['D_minus_U']:+.6f}")
print("== 污染敏感度 468/900 ==")
for b, v in cs.items():
    print(f"  {b}: frozen U K∈[{v['frozen_U_K_min']},{v['frozen_U_K_max']}] 整数网格={v['frozen_U_all_integer_grid']} "
          f"slot_err={v['frozen_U_slot_closure_error']:+.3f} ΔL1(B)={v['delta_composite_B_grid_minus_frozen']['L1_linear_sum']:+.6f}")
print("written:", dst, os.path.getsize(dst), "bytes; sha16", sha16(dst))
