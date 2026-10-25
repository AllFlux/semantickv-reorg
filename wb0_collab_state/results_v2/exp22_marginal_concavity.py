#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB0-INSTR-20261026-EXP-22-MARGINAL-CONCAVITY
Q1: 冻结件 Delta-L(K) 曲面的边际收益递减 / 次模性检验
Q2: L1 层 K=16 尖峰（系统性伪象 vs 孤立形态）判别

scope_guard:
  g1 禁插值/外推/平滑/插补，缺失点剔除并登记
  g2 两份输入件只读
  g3 不加载模型权重、不请求 GPU
  g5 纯 CPU 数值管线，无模型前向
  g7 不引入新 composite 口径（本脚本不计算新 composite，仅复算 EXP-15 既有三合成作交叉核验）

seed = 20261026, n_boot = 5000, 可幂等重跑。
"""
import json, os, math, random, hashlib, time, sys

T0 = time.time()
SEED = 20261026
N_BOOT = 5000
EPS = 1e-9
K_GRID = [1, 2, 4, 8, 16, 32]
SPIKE_INNER = [2, 4, 8, 16]          # 非端点 K
SPIKE_RATIO = 10.0

BASE = os.path.dirname(os.path.abspath(__file__))
IN_DIR = "/root/.codebuddy/artifact/2f8a7d30-9c21-4f0b-1d57-dbe3c6ec8000/wb2_inputs"
R1_PATH = os.path.join(IN_DIR, "r1_budget_allocation.hub_pull_20261025_WB2.json")
E15_PATH = os.path.join(IN_DIR, "exp15_kgrid.json")
OUT_PATH = os.path.join(BASE, "exp22_marginal_concavity.json")

EXPECT = {"r1": 86123, "exp15": 61759}


def sha256_16(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()[:16]


def load():
    meta = {}
    for key, path, exp in (("r1", R1_PATH, EXPECT["r1"]),
                           ("exp15", E15_PATH, EXPECT["exp15"])):
        if not os.path.exists(path):
            raise SystemExit("E5: 实物缺失 %s" % path)
        sz = os.path.getsize(path)
        meta[key] = {"path": path, "size_bytes": sz,
                     "size_expected": exp, "size_match": sz == exp,
                     "sha256_16": sha256_16(path)}
        if sz != exp:
            raise SystemExit("E5: 尺寸不符 %s (%d != %d)" % (path, sz, exp))
    r1 = json.load(open(R1_PATH, encoding="utf-8"))
    e15 = json.load(open(E15_PATH, encoding="utf-8"))
    return r1, e15, meta


# ---------- Clopper-Pearson 精确二项区间 ----------
def _beta_inv_lower(a, b, p):  # 二分求 Beta(a,b) 分位
    def cdf(x):
        # 正则化不完全 Beta，连分式（Lentz）
        if x <= 0: return 0.0
        if x >= 1: return 1.0
        lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
        front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
        f, c, d = 1.0, 1.0, 0.0
        for i in range(0, 300):
            m = i // 2
            if i == 0:
                num = 1.0
            elif i % 2 == 0:
                num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
            else:
                num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
            d = 1.0 + num * d
            if abs(d) < 1e-30: d = 1e-30
            d = 1.0 / d
            c = 1.0 + num / c
            if abs(c) < 1e-30: c = 1e-30
            f *= c * d
            if abs(1.0 - c * d) < 1e-14: break
        return front * f
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) < p: lo = mid
        else: hi = mid
    return (lo + hi) / 2


def clopper_pearson(k, n, alpha=0.05):
    """返回 (lo, hi) 精确 95% 区间"""
    if n == 0:
        return (float("nan"), float("nan"))
    if k == 0:
        lo = 0.0
    else:
        lo = _beta_inv_lower(k, n - k + 1, alpha / 2)
    if k == n:
        hi = 1.0
    else:
        hi = 1.0 - _beta_inv_lower(n - k, k + 1, alpha / 2)
    return (lo, hi)


def bootstrap_ci(flags, n_boot=N_BOOT, seed=SEED):
    """flags: list[bool]，层间 bootstrap"""
    n = len(flags)
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        s = 0
        for _i in range(n):
            if flags[rng.randrange(n)]: s += 1
        vals.append(s / n)
    vals.sort()
    lo = vals[int(math.floor(0.025 * (n_boot - 1)))]
    hi = vals[int(math.ceil(0.975 * (n_boot - 1)))]
    return (lo, hi)


def analyze(r1, e15, meta):
    curves = r1["sensitivity_curve"]["curves"]
    layers = [str(i) for i in range(1, 36)]          # caliber A: L1..L35
    excluded = []
    per_layer = {}
    viol_flags, spike_flags = [], []

    for lk in layers:
        if lk not in curves:
            excluded.append({"layer": "L" + lk, "reason": "curves 中无该层键"})
            continue
        pts = curves[lk].get("points", [])
        m = {p["K"]: p.get("delta_loss_answer") for p in pts}
        missing = [K for K in K_GRID if K not in m or m[K] is None]
        if missing:
            excluded.append({"layer": "L" + lk,
                             "reason": "缺 K 点 %s（不插补，整层剔除）" % missing})
            continue
        D = [float(m[K]) for K in K_GRID]
        marg = [D[i + 1] - D[i] for i in range(len(D) - 1)]
        # 上升间隔：m_{i+1} > m_i + eps（主口径：6 点 -> 5 个边际增益，i=1..4）
        rise_idx = [i + 1 for i in range(len(marg) - 1)
                    if marg[i + 1] > marg[i] + EPS]     # 1-based 位置 of m_{i+1}
        is_viol = len(rise_idx) > 0
        # 尖峰层
        spike_K, spike_r = None, None
        for i in range(1, len(K_GRID) - 1):
            dprev, dcur, dnext = D[i - 1], D[i], D[i + 1]
            base = max(abs(dprev), abs(dnext))
            if dcur > 0 and dcur >= SPIKE_RATIO * base:
                if spike_K is None or dcur > spike_r:
                    spike_K, spike_r = K_GRID[i], dcur / base if base > 0 else float("inf")
        is_spike = spike_K is not None
        per_layer["L" + lk] = {
            "K": K_GRID,
            "delta_loss_answer": D,
            "marginal_gains": marg,
            "rising_interval_positions": rise_idx,
            "is_violation": is_viol,
            "is_spike": is_spike,
            "spike_K": spike_K,
            "spike_ratio": spike_r,
        }
        viol_flags.append(is_viol)
        spike_flags.append(is_spike)

    n_valid = len(viol_flags)
    n_viol = sum(viol_flags)
    n_spike = sum(spike_flags)

    cp_v = clopper_pearson(n_viol, n_valid)
    cp_s = clopper_pearson(n_spike, n_valid)
    bs_v = bootstrap_ci(viol_flags)
    bs_s = bootstrap_ci(spike_flags)

    # ---- 判据外登记 ----
    hist = {}
    for v in per_layer.values():
        for p in v["rising_interval_positions"]:
            hist[str(p)] = hist.get(str(p), 0) + 1
    first_rise = {k: (v["rising_interval_positions"][0] if v["rising_interval_positions"] else None)
                  for k, v in per_layer.items()}
    endpoints = {k: {"D_K1": v["delta_loss_answer"][0], "D_K32": v["delta_loss_answer"][-1]}
                 for k, v in per_layer.items()}
    abs_rank = sorted(per_layer.items(),
                      key=lambda kv: -max(abs(x) for x in kv[1]["delta_loss_answer"]))
    low_sens = {2, 7, 10, 11, 13, 14, 29, 30, 31, 32, 33, 35}
    viol_layers = {int(k[1:]) for k, v in per_layer.items() if v["is_violation"]}
    spike_layers = {int(k[1:]) for k, v in per_layer.items() if v["is_spike"]}
    ov_viol = sorted(viol_layers & low_sens)
    ov_spike = sorted(spike_layers & low_sens)

    # ---- 口径歧义敏感性（判据外登记，不进入落点）----
    sens_variants = {}
    for name, idxs in (("first5_K1_16", [0, 1, 2, 3, 4]),
                       ("last5_K2_32", [1, 2, 3, 4, 5])):
        vflags = []
        for v in per_layer.values():
            D = [v["delta_loss_answer"][i] for i in idxs]
            mg = [D[i + 1] - D[i] for i in range(len(D) - 1)]
            vflags.append(any(mg[i + 1] > mg[i] + EPS for i in range(len(mg) - 1)))
        nv = sum(vflags)
        sens_variants[name] = {"k_used": [K_GRID[i] for i in idxs],
                               "n_violation": nv, "n_valid": len(vflags),
                               "proportion": nv / len(vflags) if vflags else None,
                               "cp95": list(clopper_pearson(nv, len(vflags)))}

    # ---- 交叉核验：由 r1 curves 复算 exp15 既有三合成 ----
    def lookup(li, K):
        for p in curves[str(li)]["points"]:
            if p["K"] == K:
                return float(p["delta_loss_answer"])
        return None

    maxdiff = 0.0
    n_cmp = 0
    detail_sample = {}
    pb = e15["caliber_A_noL0"]["per_budget"]
    for bud in sorted(pb.keys(), key=int):
        arm_d = pb[bud].get("per_arm", {})
        for arm in sorted(arm_d.keys()):
            prof = arm_d[arm].get("profile_by_layer")
            rep = arm_d[arm].get("composite")
            if not prof or not rep: continue
            dl = []
            ok = True
            for i, K in enumerate(prof):                 # profile_by_layer[i] -> L(i+1)
                v = lookup(i + 1, int(round(float(K))))
                if v is None: ok = False; break
                dl.append(v)
            if not ok: continue
            calc = {"L1_linear_sum": math.fsum(dl),
                    "L2_max": max(abs(x) for x in dl),
                    "L3_euclid": math.sqrt(math.fsum(x * x for x in dl))}
            for s in calc:
                d = abs(calc[s] - rep[s])
                n_cmp += 1
                if d > maxdiff: maxdiff = d
            if bud in ("180", "324") and arm == "U":
                detail_sample["%s/%s" % (bud, arm)] = {"calc": calc, "reported": rep}

    out = {
        "task": "WB0-INSTR-20261026-EXP-22-MARGINAL-CONCAVITY",
        "idempotency_key": "WB0-INSTR-20261026-EXP-22-MARGINAL-CONCAVITY",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "theme": "B",
        "serves_north_star": "结构可压·事实受限（Router 预算分配的注水最优性前提 / 死区机制）",
        "instruction_ref": "1792946231-9b6eab",
        "executor": "WB2",
        "seed": SEED, "n_boot": N_BOOT, "eps": EPS,
        "inputs": meta,
        "caliber": "A（L1..L35，剔除 L0）",
        "k_grid": K_GRID,
        "caliber_ambiguity_note": (
            "指令 §2.2 枚举 K 网格为 {1,2,4,8,16,32}（6 点 / 5 间隔），但 §2.3 文案写『每层 5 个点、4 个相邻间隔』"
            "且上升间隔检测限定 i∈{1,2,3}。二者不可同时满足。本轮主口径取 §2.2 显式枚举的 6 点网格"
            "（6 点 → 5 个边际增益 m_1..m_5，上升检测 i∈{1..4}）；5 点子网格的两种读法仅作判据外敏感性登记，"
            "不进入落点。此歧义已显式披露，未用于放宽阈值。"
        ),
        "n_valid_layers": n_valid,
        "valid_layer_list": ["L%d" % i for i in range(1, 36) if ("L%d" % i) in per_layer],
        "excluded_layers": excluded,
        "Q1_marginal_concavity": {
            "definition": "m_i = D_{i+1} - D_i；上升间隔 = 存在 i 使 m_{i+1} > m_i + eps(1e-9)；违例层 = 至少一个上升间隔",
            "n_violation": n_viol, "n_valid": n_valid,
            "proportion": n_viol / n_valid if n_valid else None,
            "cp95": list(cp_v), "bootstrap95": list(bs_v),
            "violation_layers": sorted(viol_layers),
        },
        "Q2_spike_form": {
            "definition": "尖峰层 = 存在非端点 K∈{2,4,8,16} 使 D_i ≥ 10 × max(D_{i-1}, D_{i+1}) 且 D_i > 0",
            "n_spike": n_spike, "n_valid": n_valid,
            "proportion": n_spike / n_valid if n_valid else None,
            "cp95": list(cp_s), "bootstrap95": list(bs_s),
            "spike_layers": sorted(spike_layers),
            "L1_detail": per_layer.get("L1"),
        },
        "crosscheck_vs_exp15": {
            "layer_mapping": "exp15 profile_by_layer[i] -> r1 curves[str(i+1)]（caliber_A = L1..L35）",
            "max_abs_diff_all_budgets_arms_synths": maxdiff,
            "n_comparisons": n_cmp,
            "all_match_within_1e-9": maxdiff < 1e-9,
            "detail_sample": detail_sample,
        },
        "registry_only_not_in_verdict": {
            "rising_interval_histogram": hist,
            "first_rise_position_per_layer": first_rise,
            "endpoint_values": endpoints,
            "per_layer_abs_magnitude_rank": [k for k, _v in abs_rank],
            "exp17_low_sens_set": sorted(low_sens),
            "overlap_violation_with_low_sens": ov_viol,
            "overlap_spike_with_low_sens": ov_spike,
            "caliber_sensitivity_variants": sens_variants,
        },
        "per_layer": per_layer,
        "wall_clock_sec": round(time.time() - T0, 3),
    }
    return out


def main():
    r1, e15, meta = load()
    out = analyze(r1, e15, meta)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    q1 = out["Q1_marginal_concavity"]; q2 = out["Q2_spike_form"]
    print("n_valid=%d n_viol=%d p=%.4f CP=%s BS=%s" % (
        out["n_valid_layers"], q1["n_violation"], q1["proportion"], q1["cp95"], q1["bootstrap95"]))
    print("n_spike=%d p=%.4f CP=%s BS=%s" % (
        q2["n_spike"], q2["proportion"], q2["cp95"], q2["bootstrap95"]))
    print("crosscheck max|diff| = %r (n_cmp=%d)" % (
        out["crosscheck_vs_exp15"]["max_abs_diff_all_budgets_arms_synths"],
        out["crosscheck_vs_exp15"]["n_comparisons"]))
    print("violation_layers:", q1["violation_layers"])
    print("spike_layers:", q2["spike_layers"])


if __name__ == "__main__":
    main()
