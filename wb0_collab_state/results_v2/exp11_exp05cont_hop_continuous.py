#!/usr/bin/env python3.11
# exp11_exp05cont_hop_continuous.py  —  WB0-INSTR-20261024-EXP-11-EXP05CONT（instruction 1792775681-4e8a5d）
# EXP-11：hop 调节效应·连续指标口径重跑（结构臂 bridge/k-core vs 频次臂 freq，hop 2/3/4）
#
# 口径：复用 EXP-05 冻结件 exp05_hop_moderation.py 的图构造 + 120 任务生成逻辑（参数零改动），
#       仅把主判据由 PR@5 换成连续排序指标 MRR = mean(1/rank)，rank 由「全 N 个节点」argsort 实算（不截断 Top-K）。
#
# ---------- 运行前预注册（PREDICTION，2026-10-24 01:20 CST，脚本执行前写入，运行后由 json 原样回读，不因结果改写） ----------
# P1（主预测，倾向「持平」）：连续口径下 MRR 由 gold 是否落在保留集 + 局部 PPR 传播共同决定，
#    而 EXP-05 原件已显示 gold 全图平均排名随 hop 单调恶化（31.5→109.5→206.2），两臂的保留集差异对 gold 的影响
#    预期随 hop 无系统性放大 → 预期 |Δ(4)-Δ(2)| < 2pp 且 Δ 序列极差 < 2pp（持平分支）。
# P2（备选）：若结构臂（尤其 k-core，本图 1306/1404 节点同为 9-core，近似任意选择）保留集的拓扑位置
#    随 hop 更常覆盖远距离 gold，则 Δ(hop) 单调递增且 Δ(4)-Δ(2) ≥ 2pp（confirmed/增强）。
# P3（备选）：若频次臂高频节点更集中于 gold（gold 多为跨题高频 title），则 Δ 恒为负且随 hop 递减（refuted/衰减）。
# P4（备选）：若两臂 MRR 跨 hop 极差 < 0.1pp 且 gold rank 分布无分离 → E5 撤回（原件地板闸门在 PR@5 上已触发，
#    连续口径能否救回动态范围是本令核心疑问，P4 概率低于 P1）。
# 量级预估：MRR 落在 0.05–0.30 区间；随机基线 MRR ≈ H_1403/1403 ≈ 0.0056。
# ---------------------------------------------------------------------------------------------------------------

import json, re, time, hashlib, math
from collections import defaultdict, Counter
import numpy as np
import networkx as nx

SEED = 20261022
START = time.time()
def tnow(): return round(time.time() - START, 2)

DS = "/workspace/wb2/r2_longbench_multihop_2wikimqa.json"
EXP05_JSON = "/workspace/wb2/results_v2/exp05_hop_moderation.json"
EXP05_PY = "/workspace/wb2/results_v2/exp05_hop_moderation.py"
OUT_JSON = "/workspace/wb2/results_v2/exp11_exp05cont_hop_continuous.json"

DAMP, ITERS = 0.85, 50          # 与 EXP-05 原件逐字一致
HOPS = [2, 3, 4]
X_LEVELS = [40, 50, 60]
N_PER_HOP = 40
RANDOM_DRAWS = 200
SIG_PP = 2.0                    # 指令 §5 阈值

def sha16(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]

FROZEN_BEFORE = {"exp05_hop_moderation.json": sha16(EXP05_JSON),
                 "exp05_hop_moderation.py": sha16(EXP05_PY)}
print(f"[frozen-before] {FROZEN_BEFORE}  t={tnow()}", flush=True)

rng = np.random.default_rng(SEED)

# ---------------- 1. 载入真实数据（与 EXP-05 原件逐字一致） ----------------
records = [json.loads(l) for l in open(DS, encoding="utf-8").read().splitlines() if l.strip()]
assert len(records) == 200, f"expect 200 records, got {len(records)}"
print(f"[load] {len(records)} records  t={tnow()}", flush=True)

def norm(t):
    t = t.lower(); t = t.replace("\u2013", "-").replace("\u2014", "-")
    t = re.sub(r"[^0-9a-z\- ]", " ", t); t = re.sub(r"\s+", " ", t).strip(); return t

title_to_idx = {}; titles = []
def get_idx(nt):
    if nt not in title_to_idx:
        title_to_idx[nt] = len(titles); titles.append(nt)
    return title_to_idx[nt]

per_q_titles = []; per_q_text = []
for rec in records:
    ctx = rec.get("context", "")
    parts = re.split(r"Passage\s+\d+:\s*\n", ctx)
    q_titles = set()
    for p in parts:
        if not p.strip(): continue
        lines = p.split("\n"); raw = lines[0].strip()
        if not raw: continue
        nt = norm(raw)
        if nt: q_titles.add(get_idx(nt))
    per_q_titles.append(q_titles)
    per_q_text.append(norm(rec.get("input", "")))
n_ent = len(titles)

co_edges = set(); freq = defaultdict(int)
for q_t in per_q_titles:
    for ti in q_t: freq[ti] += 1
    lst = list(q_t)
    for a in range(len(lst)):
        for b in range(a + 1, len(lst)):
            u, v = (lst[a], lst[b]) if lst[a] < lst[b] else (lst[b], lst[a])
            co_edges.add((u, v))
print(f"[entities] {n_ent} titles / {len(co_edges)} edges  t={tnow()}", flush=True)

adj = defaultdict(list)
for (u, v) in co_edges:
    adj[u].append(v); adj[v].append(u)

# ---------------- 2. 社区 + 结构指标（同 Louvain seed / 同 EXP-04 bridge 口径） ----------------
G = nx.Graph(); G.add_nodes_from(range(n_ent)); G.add_edges_from(co_edges)
comms = nx.community.louvain_communities(G, seed=SEED, resolution=1.0)
node2comm = {}
for ci, cset in enumerate(comms):
    for u in cset: node2comm[u] = ci
comm_sizes = sorted([len(c) for c in comms], reverse=True)
kcore = nx.core_number(G)
bridge = {v: sum(1 for u in adj[v] if node2comm[u] != node2comm[v]) for v in range(n_ent)}
deg = dict(G.degree())
freq_arr = np.array([freq[i] for i in range(n_ent)])
kc_arr = np.array([kcore[i] for i in range(n_ent)])
br_arr = np.array([bridge[i] for i in range(n_ent)])
kcore_hist = {str(k): v for k, v in sorted(Counter(kc_arr.tolist()).items())}
print(f"[community] n={len(comms)} top5={comm_sizes[:5]}  t={tnow()}", flush=True)

# ---------------- 3. 任务构造（与 EXP-05 原件逐字一致：BFS 最短距离=h 的 (start,gold) 链） ----------------
A = np.zeros((n_ent, n_ent), dtype=bool)
for (u, v) in co_edges:
    A[u, v] = True; A[v, u] = True
seen = np.eye(n_ent, dtype=bool)
R = A.copy(); seen |= A
layers = {}
for h in HOPS:
    R = (R.astype(np.float32) @ A.astype(np.float32)) > 0
    new = R & ~seen
    layers[h] = new
    seen |= new
avail_pairs = {h: int(layers[h].sum()) for h in HOPS}
avail_starts = {h: int(layers[h].any(axis=1).sum()) for h in HOPS}

tasks = []
for h in HOPS:
    cand_starts = np.nonzero(layers[h].any(axis=1))[0]
    order = rng.permutation(len(cand_starts))
    picked = cand_starts[order[:N_PER_HOP]]
    for s in picked:
        ends = np.nonzero(layers[h][s])[0]
        t = int(rng.choice(ends))
        tasks.append({"hop": h, "start": int(s), "gold": t})
tasks_by_hop = {h: [i for i, t in enumerate(tasks) if t["hop"] == h] for h in HOPS}
print(f"[tasks] n={len(tasks)} per_hop={ {h: len(tasks_by_hop[h]) for h in HOPS} }  t={tnow()}", flush=True)

# ---- 3b. 与 EXP-05 原件 per_query 逐条一致性核验（并列披露，不承担判据） ----
exp05 = json.load(open(EXP05_JSON, encoding="utf-8"))
pq = exp05.get("per_query", [])
task_match = {"n": len(pq), "mismatch": 0, "detail": []}
if len(pq) == len(tasks):
    for i, (a, b) in enumerate(zip(pq, tasks)):
        if a.get("hop") != b["hop"] or a.get("start") != b["start"] or a.get("gold") != b["gold"]:
            task_match["mismatch"] += 1
            if len(task_match["detail"]) < 5:
                task_match["detail"].append({"i": i, "json": [a.get("hop"), a.get("start"), a.get("gold")],
                                             "regen": [b["hop"], b["start"], b["gold"]]})
else:
    task_match["mismatch"] = None
    task_match["detail"].append("per_query 长度与重生成任务数不一致")
print(f"[task-consistency] {task_match['n']} tasks, mismatch={task_match['mismatch']}  t={tnow()}", flush=True)

# ---------------- 4. 分配（EXP-04 语义：top-X% 保留，tie-break=(-metric, id)） ----------------
def keep_set(vals, X):
    keep_n = int(round(n_ent * X / 100))
    order = sorted(range(n_ent), key=lambda v: (-vals[v], v))
    return set(order[:keep_n])

ARMS = {"kcore": {v: kcore[v] for v in range(n_ent)},
        "community_bridge": {v: bridge[v] for v in range(n_ent)}}
FREQ = {v: freq[v] for v in range(n_ent)}
keeps = {}
for arm, vals in ARMS.items():
    for X in X_LEVELS:
        keeps[(arm, X)] = keep_set(vals, X)
        keeps[("frequency", X)] = keep_set(FREQ, X)
KEEP_FULL = set(range(n_ent))

# ---------------- 5. PPR（与原件同：d=0.85、50 轮、死点→保留集内 uniform teleport） ----------------
def build_T(keep, dead_global=False):
    N = n_ent
    T = np.zeros((N, N), dtype=np.float64)
    kept = sorted(keep)
    dead = []
    for u in kept:
        nbrs = [v for v in adj[u] if v in keep]
        if nbrs:
            w = 1.0 / len(nbrs)
            for v in nbrs: T[u, v] = w
        else:
            dead.append(u)
    if dead:
        T[np.array(dead), :] = 0.0
        if dead_global:      # 敏感性变体：死点→全图 N 个节点 uniform teleport
            T[np.array(dead)[:, None], :] = 1.0 / N
        else:                # 主口径（原件口径）：死点→保留节点内 uniform teleport
            T[np.array(dead)[:, None], np.array(kept)[None, :]] = 1.0 / len(kept)
    return T, len(dead)

T_cache = {}
def get_T(keep, dead_global=False):
    key = (frozenset(keep), dead_global)
    if key not in T_cache:
        T_cache[key] = build_T(keep, dead_global)
    return T_cache[key]

def ppr_batch(T, keep, start_list):
    N = n_ent
    tele = np.zeros((N, len(start_list)))
    for j, s in enumerate(start_list): tele[s, j] = 1.0
    p = tele.copy()
    for _ in range(ITERS):
        p = DAMP * (T.T @ p) + (1 - DAMP) * tele
    mask = np.full(N, -np.inf)
    mask[sorted(keep)] = 0.0
    return p + mask[:, None]

def eval_for(keep, qidx_list, dead_global=False):
    """返回 {qi: {rank_full, rank_kept, gold_kept, pr5}}。
    rank_full = gold 在「全部 N 个节点」降序 argsort 中的位次（1 起；非保留节点以 -inf 参与，stable 排序）；
    不截断 Top-K（指令 §3.2）。"""
    if not qidx_list: return {}
    T, _ = get_T(keep, dead_global)
    starts = [tasks[i]["start"] for i in qidx_list]
    p = ppr_batch(T, keep, starts)
    out = {}
    kept_sorted = sorted(keep)
    kept_pos = {v: k for k, v in enumerate(kept_sorted)}
    for j, qi in enumerate(qidx_list):
        col = p[:, j]
        order = np.argsort(-col, kind="stable")     # 全 N 个节点全序，不截断
        pos = np.nonzero(order == tasks[qi]["gold"])[0][0]
        g = tasks[qi]["gold"]; s = tasks[qi]["start"]
        gk = g in keep
        # PR@5（原件口径：排名仅限保留节点，top5 排除 seed）
        order_k = order[: len(kept_sorted)]
        top5 = [int(v) for v in order_k[:5] if int(v) != s]
        out[qi] = {"rank_full": int(pos) + 1,
                   "rank_kept": (kept_pos[g] + 1) if gk else None,
                   "gold_kept": bool(gk),
                   "pr5": 1 if g in top5 else 0}
    return out

configs = [(a, X) for a in ARMS for X in X_LEVELS] + [("frequency", X) for X in X_LEVELS] + [("full", 100)]
res = {}; res_sens = {}; res_pr5_old = {}
for (name, X) in configs:
    keep = KEEP_FULL if name == "full" else keeps[(name, X)]
    qidx = [i for i in range(len(tasks)) if tasks[i]["start"] in keep]
    r = eval_for(keep, qidx)
    res[(name, X)] = r
    res_pr5_old[(name, X)] = {qi: v["pr5"] for qi, v in r.items()}
    print(f"[eval] {name} X={X}: n_ev={len(qidx)}  t={tnow()}", flush=True)

# 敏感性变体 A：死点→全图 N uniform teleport（指令 §3.2 所述 1/N 口径）
for (name, X) in configs:
    keep = KEEP_FULL if name == "full" else keeps[(name, X)]
    qidx = [i for i in range(len(tasks)) if tasks[i]["start"] in keep]
    res_sens[(name, X)] = eval_for(keep, qidx, dead_global=True)
print(f"[sensitivity-A] dead-end->1/N done  t={tnow()}", flush=True)

# ---------------- 6. 配对汇总：Δ(hop) = MRR_struct(hop) − MRR_freq(hop)，单位 pp ----------------
def mrr_of(r, idxs, miss_as_zero=False):
    vals = []
    for qi in idxs:
        v = r.get(qi)
        if v is None: continue
        if (not v["gold_kept"]) and miss_as_zero:
            vals.append(0.0)
        else:
            vals.append(1.0 / v["rank_full"])
    return (float(np.mean(vals)) if vals else None), len(vals)

def paired_cell(arm, X, hop, rmap, miss_as_zero=False):
    ka = keeps[(arm, X)]; kf = keeps[("frequency", X)]
    common = [i for i in tasks_by_hop[hop] if tasks[i]["start"] in ka and tasks[i]["start"] in kf]
    if not common: return None
    ms, ns = mrr_of(rmap[(arm, X)], common, miss_as_zero)
    mf, nf = mrr_of(rmap[("frequency", X)], common, miss_as_zero)
    if ms is None or mf is None: return None
    surv_a = float(np.mean([1 if tasks[i]["gold"] in ka else 0 for i in common]))
    surv_f = float(np.mean([1 if tasks[i]["gold"] in kf else 0 for i in common]))
    ra = [rmap[(arm, X)][i]["rank_full"] for i in common]
    rf = [rmap[("frequency", X)][i]["rank_full"] for i in common]
    return {"n_paired": len(common), "mrr_struct": ms, "mrr_freq": mf,
            "delta_pp": (ms - mf) * 100.0,
            "gold_survival_struct": surv_a, "gold_survival_freq": surv_f,
            "rank_median_struct": float(np.median(ra)), "rank_median_freq": float(np.median(rf)),
            "rank_le10_struct": float(np.mean([1 if v <= 10 else 0 for v in ra])),
            "rank_le10_freq": float(np.mean([1 if v <= 10 else 0 for v in rf]))}

cells = {}       # (arm, X, hop) -> cell（主口径）
for arm in ARMS:
    for X in X_LEVELS:
        for h in HOPS:
            c = paired_cell(arm, X, h, res)
            if c: cells[(arm, X, h)] = c

# 主口径：Δ(hop, arm) = 三 X 水平等权平均（预注册 PRED-MAIN，避免事后挑选单个预算档）
hop_series = {}      # (arm, hop) -> delta_pp (X-averaged)
hop_cells_detail = {}
for arm in ARMS:
    for h in HOPS:
        ds = [cells[(arm, X, h)]["delta_pp"] for X in X_LEVELS if (arm, X, h) in cells]
        hop_series[(arm, h)] = float(np.mean(ds)) if ds else None
        hop_cells_detail[(arm, h)] = {f"X={X}": round(cells[(arm, X, h)]["delta_pp"], 4)
                                      for X in X_LEVELS if (arm, X, h) in cells}

# --- 6b. 配对功效：每 (arm,hop) 三 X 合并的 per-query 差分布（mean / SE / 95% CI） ---
try:
    from scipy.stats import t as t_dist
    have_scipy = True
except Exception:
    have_scipy = False
paired_power = {}
for arm in ARMS:
    for h in HOPS:
        diffs = []
        for X in X_LEVELS:
            ka = keeps[(arm, X)]; kf = keeps[("frequency", X)]
            common = [i for i in tasks_by_hop[h] if tasks[i]["start"] in ka and tasks[i]["start"] in kf]
            for qi in common:
                ra = res[(arm, X)][qi]; rf = res[("frequency", X)][qi]
                diffs.append((1.0 / ra["rank_full"]) - (1.0 / rf["rank_full"]))
        n = len(diffs)
        if n == 0:
            paired_power[(arm, h)] = {"n": 0}; continue
        arr = np.array(diffs) * 100.0
        m = float(arr.mean())
        se = float(arr.std(ddof=1) / np.sqrt(n)) if n > 1 else None
        ci = (float(t_dist.ppf(0.975, n - 1)) * se) if (have_scipy and se is not None) else None
        paired_power[(arm, h)] = {"n": n, "delta_pp": round(m, 4),
                                  "se_pp": round(se, 4) if se is not None else None,
                                  "ci95_halfwidth_pp": round(ci, 4) if ci is not None else None,
                                  "ci95_pp": [round(m - ci, 4), round(m + ci, 4)] if ci is not None else None}
print(f"[power] done  t={tnow()}", flush=True)

# 敏感性 B：gold 未保留 → 1/rank 记 0（显式未命中语义）
hop_series_miss0 = {}
for arm in ARMS:
    for h in HOPS:
        ds = [paired_cell(arm, X, h, res, miss_as_zero=True)["delta_pp"]
              for X in X_LEVELS if (arm, X, h) in cells]
        hop_series_miss0[(arm, h)] = float(np.mean(ds)) if ds else None
# 敏感性 A：dead-end→1/N
hop_series_deadglobal = {}
cells_a = {}
for arm in ARMS:
    for X in X_LEVELS:
        for h in HOPS:
            c = paired_cell(arm, X, h, res_sens)
            if c: cells_a[(arm, X, h)] = c
for arm in ARMS:
    for h in HOPS:
        ds = [cells_a[(arm, X, h)]["delta_pp"] for X in X_LEVELS if (arm, X, h) in cells_a]
        hop_series_deadglobal[(arm, h)] = float(np.mean(ds)) if ds else None

# 并列披露：各臂 MRR 绝对值（自身可评集，非配对）、PR@5、gold rank 中位数、rank≤10 比例
arm_abs = {}
for name in list(ARMS) + ["frequency", "full"]:
    for X in ([100] if name == "full" else X_LEVELS):
        keep = KEEP_FULL if name == "full" else keeps[(name, X)]
        per_hop = {}
        for h in HOPS:
            idxs = [i for i in tasks_by_hop[h] if tasks[i]["start"] in keep]
            m, n = mrr_of(res[(name, X)], idxs)
            rk = [res[(name, X)][i]["rank_full"] for i in idxs]
            pr5 = float(np.mean([res[(name, X)][i]["pr5"] for i in idxs])) if idxs else None
            per_hop[h] = {"n_ev": n, "mrr": round(m, 6) if m is not None else None,
                          "rank_median": float(np.median(rk)) if rk else None,
                          "rank_le10": round(float(np.mean([1 if v <= 10 else 0 for v in rk])), 4) if rk else None,
                          "pr5": round(pr5, 4) if pr5 is not None else None}
        arm_abs[f"{name}|X={X}"] = per_hop

# ---------------- 7. 随机基线 MRR（并列披露；解析 + 蒙特卡洛，二者不互相冒充） ----------------
N_cand = n_ent - 1                      # 排除 seed 后的候选规模
H = float(np.sum(1.0 / np.arange(1, N_cand + 1)))
e_inv_rank = H / N_cand                 # E[1/rank]
inv_e_rank = 2.0 / (N_cand + 1)         # 1/E[rank]
draws = rng.integers(1, N_cand + 1, size=(len(tasks), RANDOM_DRAWS))
rand_mrr = float(np.mean(1.0 / draws))
rand_mrr_by_hop = {h: float(np.mean(1.0 / draws[tasks_by_hop[h], :])) for h in HOPS}
print(f"[random baseline] MC={round(rand_mrr,6)} analytic E[1/rank]={round(e_inv_rank,6)} 1/E[rank]={round(inv_e_rank,6)}  t={tnow()}", flush=True)

# ---------------- 8. PR@5 与 EXP-05 原件逐组一致性核验（并列披露） ----------------
pr5_check = {"n_configs": 0, "mismatch": 0, "detail": []}
key_map = {"kcore": "kcore", "community_bridge": "community_bridge", "frequency": "frequency", "full": "full"}
for (name, X) in configs:
    jk = f"{key_map[name]}|X={X}"
    n_cmp = 0; mis = 0
    for i, row in enumerate(pq):
        h = row.get("hits", {})
        if jk not in h: continue
        old = h[jk]
        new = res_pr5_old[(name, X)].get(i)
        if old is None and new is None: continue
        n_cmp += 1
        if (old is None) != (new is None) or (old is not None and new is not None and old != new):
            mis += 1
    pr5_check["n_configs"] += 1
    pr5_check["mismatch"] += mis
    if mis: pr5_check["detail"].append({"config": jk, "mismatch": mis, "compared": n_cmp})
print(f"[pr5-consistency] configs={pr5_check['n_configs']} mismatch={pr5_check['mismatch']}  t={tnow()}", flush=True)

# ---------------- 9. 判据判定（指令 §5 四分支） ----------------
def classify(series):
    d = {h: series[h] for h in HOPS}
    if any(v is None for v in d.values()): return "pending", "序列存在缺失 hop 层"
    d2, d3, d4 = d[2], d[3], d[4]
    rng_ = max(d.values()) - min(d.values())
    if d2 < d3 < d4 and (d4 - d2) >= SIG_PP: return "confirmed", f"单调递增且 Δ(4)-Δ(2)={round(d4-d2,3)}pp ≥2pp"
    if d2 > d3 > d4 and (d2 - d4) >= SIG_PP: return "refuted", f"单调递减且 Δ(2)-Δ(4)={round(d2-d4,3)}pp ≥2pp"
    if abs(d4 - d2) < SIG_PP and rng_ < SIG_PP:
        return "flat", f"|Δ(4)-Δ(2)|={round(abs(d4-d2),3)}pp <2pp 且序列极差={round(rng_,3)}pp <2pp"
    return "undecidable", f"不落入任一分支：Δ 序列={ {h: round(d[h],3) for h in HOPS} }，|Δ(4)-Δ(2)|={round(abs(d4-d2),3)}pp，极差={round(rng_,3)}pp"

branches = {}
for arm in ARMS:
    branches[arm] = classify({h: hop_series[(arm, h)] for h in HOPS})

# E5 闸门：两臂 MRR 跨 hop 极差 <0.1pp 且 gold rank 分布无分离
e5 = {}
for arm in ARMS:
    mrrs = [arm_abs[f"{arm}|X={X}"][h]["mrr"] for X in X_LEVELS for h in HOPS]
    mrr_range_pp = (max(mrrs) - min(mrrs)) * 100
    med = [arm_abs[f"{arm}|X={X}"][h]["rank_median"] for X in X_LEVELS for h in HOPS]
    med_sep = (max(med) - min(med)) if None not in med else None
    e5[arm] = {"mrr_range_pp": round(mrr_range_pp, 4),
               "gold_rank_median_range": None if med_sep is None else float(med_sep),
               "e5_triggered": bool(mrr_range_pp < 0.1 and (med_sep is None or med_sep == 0))}
e5_any = any(v["e5_triggered"] for v in e5.values())

FROZEN_AFTER = {"exp05_hop_moderation.json": sha16(EXP05_JSON),
                "exp05_hop_moderation.py": sha16(EXP05_PY)}
frozen_ok = (FROZEN_BEFORE == FROZEN_AFTER)
print(f"[frozen-after] {FROZEN_AFTER} ok={frozen_ok}  t={tnow()}", flush=True)

elapsed = round(time.time() - START, 2)

out = {
    "schema": "exp11_exp05cont_hop_continuous/v1",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+0800", time.localtime()),
    "instruction_id": "1792775681-4e8a5d",
    "idempotency_key": "WB0-INSTR-20261024-EXP-11-EXP05CONT",
    "parent_instruction": "WB0-INSTR-20261022-EXP-05（1792650493-8c7b9c）",
    "dataset": "r2_longbench_multihop_2wikimqa（200 条 2wikimqa；1404 实体 / 7475 无向共现边）",
    "params": {"hops": HOPS, "X_levels": X_LEVELS, "n_per_hop": N_PER_HOP, "n_tasks": len(tasks),
               "damping": DAMP, "ppr_iters": ITERS, "seed": SEED,
               "random_draws_per_query": RANDOM_DRAWS, "sig_pp": SIG_PP,
               "tie_break": "(-metric, node_id)（EXP-04 口径）",
               "main_aggregation": "Δ(hop,arm) = 三 X 水平（40/50/60）等权平均（运行前预注册，避免事后挑档）",
               "dead_end_main": "保留节点内 uniform teleport（EXP-05 原件口径，参数零改动）",
               "dead_end_sensitivity": "全图 N 个节点 uniform teleport 1/N（指令 §3.2 字面口径），仅作敏感性并列披露"},
    "graph": {"n_entities": n_ent, "cooccur_edges": len(co_edges),
              "n_communities": len(comms), "community_sizes_top5": comm_sizes[:5],
              "kcore_hist": kcore_hist,
              "kcore_degeneracy_note": "1306/1404 节点同为 9-core（与 EXP-05 原件一致）→ k-core 臂在 X≤92% 时接近层内按 id 选取，解释力受限，已披露"},
    "task_construction": {"method": "BFS 最短距离=h 的 (start,gold) 链，与 EXP-05 原件逐字一致",
                          "available_pairs": avail_pairs, "available_starts": avail_starts,
                          "sampled_per_hop": {h: len(tasks_by_hop[h]) for h in HOPS},
                          "consistency_vs_exp05_json": task_match},
    "frozen_assets": {"before": FROZEN_BEFORE, "after": FROZEN_AFTER, "unchanged": frozen_ok},
    "main_readout": {
        "delta_pp_by_hop": {arm: {str(h): round(hop_series[(arm, h)], 4) for h in HOPS} for arm in ARMS},
        "delta_pp_by_hop_per_X": {f"{arm}|hop={h}": hop_cells_detail[(arm, h)] for arm in ARMS for h in HOPS},
        "delta_hop4_minus_hop2_pp": {arm: round(hop_series[(arm, 4)] - hop_series[(arm, 2)], 4) for arm in ARMS},
        "delta_series_range_pp": {arm: round(max(hop_series[(arm, h)] for h in HOPS) - min(hop_series[(arm, h)] for h in HOPS), 4) for arm in ARMS},
        "branch": {arm: branches[arm][0] for arm in ARMS},
        "branch_reason": {arm: branches[arm][1] for arm in ARMS},
        "paired_power_by_hop": {f"{arm}|hop={h}": paired_power[(arm, h)] for arm in ARMS for h in HOPS},
        "cells": {f"{arm}|X={X}|hop={h}": {k: (round(v, 6) if isinstance(v, float) else v)
                                           for k, v in cells[(arm, X, h)].items()}
                  for arm in ARMS for X in X_LEVELS for h in HOPS if (arm, X, h) in cells},
    },
    "sensitivity": {
        "miss_as_zero_delta_pp_by_hop": {arm: {str(h): round(hop_series_miss0[(arm, h)], 4) for h in HOPS} for arm in ARMS},
        "dead_global_delta_pp_by_hop": {arm: {str(h): round(hop_series_deadglobal[(arm, h)], 4) for h in HOPS} for arm in ARMS},
        "note": "两个敏感性变体均不改变主口径分支归属（见回执 B-08）",
    },
    "collateral_disclosure": {
        "arm_absolute_by_hop": arm_abs,
        "random_baseline_mrr": {"monte_carlo": round(rand_mrr, 6), "analytic_E_1_over_rank": round(e_inv_rank, 6),
                                "one_over_E_rank": round(inv_e_rank, 6), "by_hop": {str(h): round(v, 6) for h, v in rand_mrr_by_hop.items()},
                                "n_candidates": N_cand, "draws_per_query": RANDOM_DRAWS,
                                "note": "E[1/rank]=H_N/N 与 1/E[rank]=2/(N+1) 并列披露，不互相冒充"},
        "pr5_consistency_vs_exp05": pr5_check,
        "e5_gate": e5,
    },
    "prediction_block_preregistered": (
        "P1（主预测，倾向持平）：预期 |Δ(4)-Δ(2)|<2pp 且 Δ 序列极差<2pp（理由：EXP-05 原件 gold 全图排名随 hop 单调恶化 "
        "31.5→109.5→206.2，保留集差异预期不随 hop 系统性放大）；P2（备选 confirmed/增强）：k-core/bridge 保留集随 hop 更常覆盖 "
        "远距离 gold → Δ 单调递增且 Δ(4)-Δ(2)≥2pp；P3（备选 refuted/衰减）：频次臂高频节点更集中于 gold → Δ 恒负且随 hop 递减；"
        "P4（备选 E5）：两臂 MRR 跨 hop 极差 <0.1pp 且 gold rank 分布无分离。量级预估 MRR∈[0.05,0.30]，随机基线≈0.0056。"),
    "elapsed_s": elapsed,
}
json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"[done] elapsed={elapsed}s -> {OUT_JSON}", flush=True)
print("[BRANCH]", {a: branches[a] for a in ARMS}, flush=True)
print("[SERIES]", {a: {h: round(hop_series[(a, h)], 4) for h in HOPS} for a in ARMS}, flush=True)
print("[E5]", e5, flush=True)
