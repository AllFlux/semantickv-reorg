#!/usr/bin/env python3.11
# exp05_hop_moderation.py  —  WB0-INSTR-20261022-EXP-05（instruction 1792650493-8c7b9c）
# EXP-05：多跳链长调节（LongBench multi-hop，hop 2/3/4）——结构驱动 vs 频次驱动分配 PR@5
# 纯 CPU；真实数据 r2_longbench_multihop_2wikimqa.json（200 条 2wikimqa / 1404 实体 / 7475 无向共现边）；
# 图构造与数据口径与 EXP-03/R3 逐字一致（同 norm、同题内 title 共现图、同 Louvain seed），仅改任务构造与分配。
#
# 设计（对照 WB0 指令 §2/§3/§5）：
#  1) 任务构造：题目-gold 协议在本语料仅 n=2 可评（EXP-03/R3 已证实：66 题 gold 为全局 title，64 题 gold∈seed 退化），
#     无法满足"每层 ≥10 条"→ 按指令"构造多跳检索任务"授权，在真实共现图上以 BFS 最短距离=h 构造
#     (start, gold) 链（seed={start} 单点，gold=距离恰为 h 的端点），中性抽样（start 均匀、端点在该距离层均匀）。
#  2) 分配（EXP-04 预算语义）：保留 top-X% 节点——结构驱动=k-core / community bridge；频次驱动=title 频次。
#  3) 检索：子图上 PPR（d=0.85, 50 轮收敛），死点→保留节点内 uniform teleport（口径披露）；
#     排名仅限保留节点；top-5 排除 seed；PR@5=命中率。
#  4) 判定：指令预注册判据 + D1 三分。地板闸门：若无分配全图参考 PR@5 亦为 0（判据无动态范围），
#     Δ=0 不得解读为"频次不劣于结构"（禁止把测不出当已证伪）→ pending，并附 rank 补充诊断（非预注册口径，显式披露）。
import json, re, time, hashlib
from collections import defaultdict, Counter
import numpy as np
import networkx as nx
from scipy.stats import spearmanr, t as t_dist

SEED = 20261022
START = time.time()
def tnow(): return round(time.time() - START, 2)

DS = "/workspace/wb2/r2_longbench_multihop_2wikimqa.json"
OUT_JSON = "/workspace/wb2/results_v2/exp05_hop_moderation.json"
DAMP, ITERS = 0.85, 50
HOPS = [2, 3, 4]
X_LEVELS = [40, 50, 60]
N_PER_HOP = 40           # 构造任务数/跳（指令要求 ≥10；取 40 使 X=40 分配后每层 n_ev 仍 ≥10）
RANDOM_DRAWS = 200
POWER_MIN = 10           # 分配后每 (hop, arm, X)  paired 样本下限
SIG_PP = 2.0             # 指令预注册：PR@5 差 ≥2pp 记"显著"
FLOOR_GATE = 0.05        # 全图无分配参考 PR@5 < 0.05 → 判据无动态范围（地板），pending

rng = np.random.default_rng(SEED)

# ---------------- 1. 载入真实数据（EXP-03/R3 逐字一致） ----------------
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
print(f"[entities] {n_ent} titles  t={tnow()}", flush=True)

# 共现图（题内 title 共现，无向）+ 频次（title 跨题出现次数，EXP-03 口径）
co_edges = set(); co_count = defaultdict(int); freq = defaultdict(int)
for q_t in per_q_titles:
    for ti in q_t: freq[ti] += 1
    lst = list(q_t)
    for a in range(len(lst)):
        for b in range(a + 1, len(lst)):
            u, v = (lst[a], lst[b]) if lst[a] < lst[b] else (lst[b], lst[a])
            co_edges.add((u, v)); co_count[(u, v)] += 1
print(f"[cooccur] edges={len(co_edges)}  t={tnow()}", flush=True)

adj = defaultdict(list)
for (u, v) in co_edges:
    adj[u].append(v); adj[v].append(u)

# ---------------- 2. 社区 + 结构指标（与 R3 同 Louvain；bridge 同 EXP-04 口径） ----------------
G = nx.Graph(); G.add_nodes_from(range(n_ent)); G.add_edges_from(co_edges)
comms = nx.community.louvain_communities(G, seed=SEED, resolution=1.0)
node2comm = {}
for ci, cset in enumerate(comms):
    for u in cset: node2comm[u] = ci
comm_sizes = sorted([len(c) for c in comms], reverse=True)
print(f"[community] n={len(comms)} sizes(top5)={comm_sizes[:5]}  t={tnow()}", flush=True)

kcore = nx.core_number(G)
bridge = {v: sum(1 for u in adj[v] if node2comm[u] != node2comm[v]) for v in range(n_ent)}
deg = dict(G.degree())
freq_arr = np.array([freq[i] for i in range(n_ent)])
kc_arr = np.array([kcore[i] for i in range(n_ent)])
br_arr = np.array([bridge[i] for i in range(n_ent)])
dg_arr = np.array([deg[i] for i in range(n_ent)])

def rho(a, b):
    if a.std() == 0 or b.std() == 0: return None
    return round(float(spearmanr(a, b).correlation), 4)

corr_freq = {"kcore": rho(freq_arr, kc_arr), "community_bridge": rho(freq_arr, br_arr),
             "degree": rho(freq_arr, dg_arr)}
corr_kcore_bridge = rho(kc_arr, br_arr)
kcore_hist = {str(k): v for k, v in sorted(Counter(kc_arr.tolist()).items())}
print(f"[metrics] kcore_hist={kcore_hist} freq~kcore={corr_freq['kcore']} "
      f"freq~bridge={corr_freq['community_bridge']}  t={tnow()}", flush=True)

# ---------------- 3. 任务构造：BFS 最短距离 = h 的 (start, gold) 链 ----------------
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
print(f"[chains] available pairs={avail_pairs} starts={avail_starts}  t={tnow()}", flush=True)

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

# 题目-gold 协议可评数（记录用：证明构造任务的必要性）
def find_seeds(q):
    return [ti for ti in range(n_ent) if titles[ti] and titles[ti] in per_q_text[q]]
def find_gold(q):
    golds = [norm(a) for a in records[q].get("answers", [])]
    return [ti for ti in range(n_ent) if titles[ti] in golds]
n_gold_title = 0; n_gold_in_seed = 0; n_eval_question = 0
for q in range(len(records)):
    seeds = find_seeds(q); gold = find_gold(q)
    if not seeds or not gold: continue
    n_gold_title += 1
    if gold[0] in seeds: n_gold_in_seed += 1; continue
    n_eval_question += 1
print(f"[question-protocol] n_gold_title={n_gold_title} n_gold_in_seed={n_gold_in_seed} "
      f"n_evaluable={n_eval_question}  t={tnow()}", flush=True)

# ---------------- 4. 分配：top-X% 保留（EXP-04 语义；tie-break=(-metric, id)） ----------------
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

# ---------------- 5. PPR（批量；死点→保留节点内 uniform teleport，已披露口径） ----------------
def build_T(keep):
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
        T[np.array(dead)[:, None], np.array(kept)[None, :]] = 1.0 / len(kept)
    return T, len(dead)

T_cache = {}
def get_T(keep):
    key = frozenset(keep)
    if key not in T_cache:
        T_cache[key] = build_T(keep)
    return T_cache[key]

def ppr_batch(T, keep, start_list):
    """返回 (n_ent, n_q) 质量矩阵；非保留节点置 -inf（不可排名）。"""
    N = n_ent
    tele = np.zeros((N, len(start_list)))
    for j, s in enumerate(start_list): tele[s, j] = 1.0
    p = tele.copy()
    for _ in range(ITERS):
        p = DAMP * (T.T @ p) + (1 - DAMP) * tele
    mask = np.full(N, -np.inf)
    mask[sorted(keep)] = 0.0
    return p + mask[:, None]

def eval_for(keep, qidx_list):
    """返回 (hits, ranks)：hits[qi]∈{0,1}；ranks[qi]=gold 在保留节点排名中的位次（1 起；gold 未保留→None）。"""
    if not qidx_list: return {}, {}
    T, _ = get_T(keep)
    starts = [tasks[i]["start"] for i in qidx_list]
    p = ppr_batch(T, keep, starts)
    hits, ranks = {}, {}
    for j, qi in enumerate(qidx_list):
        col = p[:, j]
        order = np.argsort(-col, kind="stable")
        top5 = [int(v) for v in order[:5] if int(v) != tasks[qi]["start"]]
        hits[qi] = 1 if tasks[qi]["gold"] in top5 else 0
        pos = np.nonzero(order == tasks[qi]["gold"])[0]
        ranks[qi] = (int(pos[0]) + 1) if len(pos) else None
    return hits, ranks

configs = []
for arm in ARMS:
    for X in X_LEVELS:
        configs.append((arm, X))
for X in X_LEVELS:
    configs.append(("frequency", X))
configs.append(("full", 100))

hits, ranks = {}, {}
for (name, X) in configs:
    keep = KEEP_FULL if name == "full" else keeps[(name, X)]
    qidx = [i for i in range(len(tasks)) if tasks[i]["start"] in keep]
    h, r = eval_for(keep, qidx)
    hits[(name, X)] = h; ranks[(name, X)] = r
    pr5 = float(np.mean([h[i] for i in qidx])) if qidx else None
    print(f"[eval] {name} X={X}: n_ev={len(qidx)} PR@5={pr5}  t={tnow()}", flush=True)

# ---------------- 6. 随机基线（每 hop；全图非 seed 候集中抽 5，200 次/查询） ----------------
rand_by_hop = {}
for h in HOPS:
    rates = []
    for qi in tasks_by_hop[h]:
        s = tasks[qi]["start"]; g = tasks[qi]["gold"]
        cand = np.array([i for i in range(n_ent) if i != s])
        hits_r = []
        for _ in range(RANDOM_DRAWS):
            pick = rng.choice(len(cand), size=5, replace=False)
            hits_r.append(1 if g in cand[pick] else 0)
        rates.append(float(np.mean(hits_r)))
    rand_by_hop[h] = float(np.mean(rates))
print(f"[random baseline] per_hop={ {h: round(v,4) for h,v in rand_by_hop.items()} }  t={tnow()}", flush=True)

# ---------------- 7. 汇总：配对 Δ（PR@5，共同可评子集）+ rank 补充诊断 ----------------
def paired_pr5(arm, X, hop=None):
    ka = keeps[(arm, X)]; kf = keeps[("frequency", X)]
    common = [i for i in range(len(tasks))
              if tasks[i]["start"] in ka and tasks[i]["start"] in kf
              and (hop is None or tasks[i]["hop"] == hop)]
    if not common: return 0, None, None, []
    diffs = np.array([hits[(arm, X)][i] - hits[("frequency", X)][i] for i in common], dtype=float)
    n = len(diffs); m = float(diffs.mean()) * 100
    ci = (float(t_dist.ppf(0.975, n - 1)) * float(diffs.std(ddof=1) / np.sqrt(n)) * 100) if n > 1 else None
    return n, round(m, 3), (round(ci, 3) if ci is not None else None), diffs.tolist()

def paired_rank(arm, X, hop=None):
    """gold 同时被两臂保留的共同子集上，freq_rank - arm_rank（正=结构臂把 gold 排得更前）。"""
    ka = keeps[(arm, X)]; kf = keeps[("frequency", X)]
    common = [i for i in range(len(tasks))
              if tasks[i]["start"] in ka and tasks[i]["start"] in kf
              and tasks[i]["gold"] in ka and tasks[i]["gold"] in kf
              and (hop is None or tasks[i]["hop"] == hop)]
    if not common: return 0, None, None
    d = np.array([ranks[("frequency", X)][i] - ranks[(arm, X)][i] for i in common], dtype=float)
    n = len(d); m = float(d.mean())
    ci = (float(t_dist.ppf(0.975, n - 1)) * float(d.std(ddof=1) / np.sqrt(n))) if n > 1 else None
    return n, round(m, 2), (round(ci, 2) if ci is not None else None)

cells = {}
for arm in ARMS:
    for X in X_LEVELS:
        for h in HOPS:
            n, m, ci, _ = paired_pr5(arm, X, h)
            ka = keeps[(arm, X)]
            qa = [i for i in tasks_by_hop[h] if tasks[i]["start"] in ka]
            qf = [i for i in tasks_by_hop[h] if tasks[i]["start"] in keeps[("frequency", X)]]
            pr5_arm = float(np.mean([hits[(arm, X)][i] for i in qa])) if qa else None
            pr5_freq = float(np.mean([hits[("frequency", X)][i] for i in qf])) if qf else None
            nr, mr, cir = paired_rank(arm, X, h)
            cells[(arm, X, h)] = {"n_common": n, "delta_pp": m, "ci95_pp": ci,
                                  "n_ev_arm": len(qa), "pr5_arm": round(pr5_arm, 4) if pr5_arm is not None else None,
                                  "n_ev_freq": len(qf), "pr5_freq": round(pr5_freq, 4) if pr5_freq is not None else None,
                                  "rank_diag": {"n_gold_survived_both": nr, "mean_rank_gain": mr, "ci95": cir}}

hop_summary = {}
for arm in ARMS:
    for h in HOPS:
        all_diffs = []
        for X in X_LEVELS:
            _, _, _, diffs = paired_pr5(arm, X, h)
            all_diffs.extend(diffs)
        n = len(all_diffs)
        m = float(np.mean(all_diffs)) * 100 if n else None
        ci = (float(t_dist.ppf(0.975, n - 1)) * float(np.std(all_diffs, ddof=1) / np.sqrt(n)) * 100) if n > 1 else None
        surv = []
        rank_diffs = []
        for X in X_LEVELS:
            ka = keeps[(arm, X)]
            qs = tasks_by_hop[h]
            surv.append(float(np.mean([1 if tasks[i]["gold"] in ka else 0 for i in qs])))
            _, mr, _ = paired_rank(arm, X, h)
            if mr is not None: rank_diffs.append(mr)
        hop_summary[(arm, h)] = {
            "n_paired": n, "delta_pp": round(m, 3) if m is not None else None,
            "ci95_pp": round(ci, 3) if ci is not None else None,
            "gold_survival_mean": round(float(np.mean(surv)), 4),
            "rank_diag_mean_gain": round(float(np.mean(rank_diffs)), 2) if rank_diffs else None}

full_pr5 = {}
full_ranks = {}
for h in HOPS:
    qidx = tasks_by_hop[h]
    full_pr5[h] = round(float(np.mean([hits[("full", 100)][i] for i in qidx])), 4)
    rr = [ranks[("full", 100)][i] for i in qidx]
    full_ranks[h] = {"mean": round(float(np.mean(rr)), 1), "median": float(np.median(rr)),
                     "p90": round(float(np.percentile(rr, 90)), 1), "n": len(rr)}
print(f"[floor diag] full PR@5={full_pr5} full gold ranks={full_ranks}  t={tnow()}", flush=True)

# ---------------- 8. hop 调节分析（增强/衰减/持平 + 拐点） ----------------
moderation = {}
for arm in ARMS:
    series = {h: hop_summary[(arm, h)]["delta_pp"] for h in HOPS}
    rseries = {h: hop_summary[(arm, h)]["rank_diag_mean_gain"] for h in HOPS}
    pr5_series = {f"X={X}": {h: cells[(arm, X, h)]["pr5_arm"] for h in HOPS} for X in X_LEVELS}
    d42 = (series[4] - series[2]) if (series[4] is not None and series[2] is not None) else None
    if d42 is None: trend = "unavailable"
    elif d42 >= SIG_PP: trend = "增强（结构优势随跳数上升）"
    elif d42 <= -SIG_PP: trend = "衰减（结构优势随跳数下降）"
    else: trend = "持平（Δ(hop4)-Δ(hop2) 绝对值 <2pp）"
    r42 = (rseries[4] - rseries[2]) if (rseries[4] is not None and rseries[2] is not None) else None
    rtrend = ("unavailable" if r42 is None else
              "增强" if r42 >= 5 else "衰减" if r42 <= -5 else "持平")
    peak = max(HOPS, key=lambda h: (series[h] if series[h] is not None else -1e9))
    moderation[arm] = {"delta_pp_by_hop": series, "pr5_by_hop_per_X": pr5_series,
                       "delta_hop4_minus_hop2_pp": round(d42, 3) if d42 is not None else None,
                       "trend_pr5": trend, "peak_delta_hop": peak,
                       "rank_diag_gain_by_hop": rseries, "rank_diag_trend": rtrend}

# ---------------- 9. 判定（指令预注册 + D1 三分 + 地板/天花板闸门） ----------------
rand_max = max(rand_by_hop.values())
ceiling_gate = bool(rand_max >= 0.7)
full_max = max(full_pr5.values())
floor_gate = bool(full_max < FLOOR_GATE)
print(f"[gates] ceiling={ceiling_gate} (rand_max={round(rand_max,4)}) floor={floor_gate} (full_max={full_max})", flush=True)

def arm_verdict(arm):
    series = {h: hop_summary[(arm, h)]["delta_pp"] for h in HOPS}
    ns = {h: hop_summary[(arm, h)]["n_paired"] for h in HOPS}
    if ceiling_gate:
        return "pending", (f"天花板闸门：随机基线 PR@5={round(rand_max,4)}≥0.7 → 任务过易，效应量不可推广（指令边界）。"
                           f"Δ 序列 {series}")
    if floor_gate:
        return "pending", (f"地板闸门：无分配全图参考 PR@5={full_pr5}（<{FLOOR_GATE}）→ PR@5 判据在本任务构造下无动态范围，"
                           f"Δ=0 不构成『频次不劣于结构』的证据（D1：禁止把测不出当已证伪）。Δ 序列 {series}；"
                           f"gold 全图平均排名 { {h: full_ranks[h]['mean'] for h in HOPS} }。")
    low = [h for h in HOPS if ns[h] < POWER_MIN]
    sig_better = [h for h in HOPS if series[h] is not None and series[h] >= SIG_PP]
    sig_worse = [h for h in HOPS if series[h] is not None and series[h] <= -SIG_PP]
    if low:
        return "pending", (f"功效闸门：hop {low} 配对样本 n<{POWER_MIN}（{ns}），点估计不可作判据；Δ 序列 {series}")
    if sig_better and not sig_worse:
        return "confirmed", (f"hop {sig_better} 上结构分配 PR@5 显著高于频次（Δ≥{SIG_PP}pp）且无任一跳反向显著（方向一致）")
    if not sig_better:
        return "refuted", (f"所有 hop 档结构分配均未显著优于频次（max Δ={max(series.values())}pp <{SIG_PP}pp）→ 频次分配不劣于结构分配")
    return "pending", f"跨 hop 矛盾：hop {sig_better} 显著优、hop {sig_worse} 显著劣 → 不可判；Δ 序列 {series}"

verdicts, reasons = {}, {}
for arm in ARMS:
    verdicts[arm], reasons[arm] = arm_verdict(arm)
v_kc, v_br = verdicts["kcore"], verdicts["community_bridge"]
if v_kc == v_br:
    verdict = v_kc
    verdict_reason = reasons["community_bridge"] if v_kc != "confirmed" else reasons["kcore"]
else:
    verdict = "pending"
    verdict_reason = (f"两结构臂判定不一致（metric-dependent）：kcore={v_kc}；community_bridge={v_br} → 按 D1 不可判，pending。")

elapsed = round(time.time() - START, 2)
out = {
    "schema": "exp05_hop_moderation/v1",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+0800", time.localtime()),
    "instruction_id": "1792650493-8c7b9c",
    "dataset": "r2_longbench_multihop_2wikimqa (200 条 2wikimqa)",
    "params": {"hops": HOPS, "X_levels": X_LEVELS, "n_per_hop": N_PER_HOP,
               "damping": DAMP, "ppr_iters": ITERS, "seed": SEED,
               "random_draws_per_query": RANDOM_DRAWS, "power_min": POWER_MIN,
               "sig_pp": SIG_PP, "floor_gate": FLOOR_GATE,
               "tie_break": "(-metric, node_id)（EXP-04 口径）"},
    "graph": {"n_entities": n_ent, "cooccur_edges": len(co_edges),
              "n_communities": len(comms), "community_sizes_top5": comm_sizes[:5],
              "kcore_hist": kcore_hist,
              "kcore_degeneracy_note": ("本图 k-core 高度退化：1306/1404 节点同为 9-core → top-X% by k-core "
                                         "在 X≤92% 时等价于 9-core 层内按 node_id 升序选取（近任意选择），"
                                         "k-core 臂的解释力受限，已如实披露；community_bridge 臂为连续型区分度结构度量"),
              "freq_stats": {"nonzero": int((freq_arr > 0).sum()), "max": int(freq_arr.max()),
                             "mean": round(float(freq_arr.mean()), 3)},
              "spearman": {"freq_vs_kcore": corr_freq["kcore"], "freq_vs_bridge": corr_freq["community_bridge"],
                           "freq_vs_degree": corr_freq["degree"], "kcore_vs_bridge": corr_kcore_bridge},
              "freq_operationalization": ("频次驱动口径 = title 节点跨题出现次数（EXP-03/R3 既有口径）；"
                                            "指令所称 token 频次操作化为图节点频次，未使用语料 raw token 计数（与图节点无一一对应），已披露")},
    "task_construction": {
        "method": ("题目-gold 协议本语料仅 n=2 可评（EXP-03/R3 已证实），不满足每层≥10条 → 按指令'构造多跳检索任务'"
                    "授权，在真实共现图上以 BFS 最短距离=h 构造 (start,gold) 链；seed={start} 单点；"
                    "start 在含该距离层的节点中均匀抽样、端点在距离层内均匀抽样（对 k-core/频次指标中性）"),
        "question_protocol": {"n_total": len(records), "n_gold_title": n_gold_title,
                              "n_gold_in_seed": n_gold_in_seed, "n_evaluable": n_eval_question},
        "available_pairs": avail_pairs, "available_starts": avail_starts,
        "sampled_per_hop": {h: len(tasks_by_hop[h]) for h in HOPS}},
    "allocation": {"semantics": "EXP-04 预算语义：保留 top-X% 节点，在诱导子图上检索；排名仅限保留节点",
                   "dead_end": "无保留邻居 → 保留节点子图内 uniform teleport（EXP-03/R3 全图 teleport 口径不适用于节点删除式分配，已披露口径明确化）",
                   "ranking": "top-5 排除 seed 实体；非保留节点置 -inf 不可入榜"},
    "eval_protocol": {"ppr": "p=(1-d)*T^T p + d*tele, d=0.85, 50 轮收敛（非跳数截断；EXP-05 的 hop 指任务链长而非 PPR 截断，已披露）",
                      "paired_basis": "Δ 在两分配共同可评查询（start 同时被两臂保留）上配对计算；gold 是否被保留计入分配效应",
                      "ci": "配对差 t 分布 95%CI；(query,X) 对按独立处理（同查询跨 X 相关，CI 或略偏窄，已披露）",
                      "rank_diag": "补充诊断（非预注册判据）：gold 在两臂均保留的子集上比较排名位次（freq_rank - arm_rank，正=结构臂更前）；仅用于解释地板，不参与判定"},
    "random_baseline_pr5_by_hop": {h: round(rand_by_hop[h], 4) for h in HOPS},
    "ceiling_gate": {"random_baseline_max": round(rand_max, 4), "threshold": 0.7, "triggered": ceiling_gate,
                     "note": "指令边界：随机基线≥0.7 则任务过易、效应量不可推广"},
    "floor_gate": {"full_graph_pr5_by_hop": full_pr5, "threshold": FLOOR_GATE, "triggered": floor_gate,
                   "note": ("无分配全图参考 PR@5=0 → 单种子 PPR 在题内 clique 共现图上 top-5 被 seed 直接邻居垄断，"
                            "距离 2+ gold 无可达动态范围（与 EXP-03/R3 的 0.0 基线一致）；"
                            "此条件下 Δ=0 不可解读为频次不劣于结构")},
    "full_graph_gold_rank_by_hop": full_ranks,
    "saturation_note": (f"全图（X=100，无分配）PR@5≥0.95 的 hop：{ [h for h in HOPS if full_pr5[h] >= 0.95] or '无' }；"
                        f"若饱和则对分配无分辨力（EXP-04 教训）"),
    "cells": {f"{arm}|X={X}|hop={h}": cells[(arm, X, h)] for arm in ARMS for X in X_LEVELS for h in HOPS},
    "hop_summary": {f"{arm}|hop={h}": hop_summary[(arm, h)] for arm in ARMS for h in HOPS},
    "moderation": moderation,
    "arm_verdicts": {"kcore": {"verdict": verdicts["kcore"], "reason": reasons["kcore"]},
                     "community_bridge": {"verdict": verdicts["community_bridge"], "reason": reasons["community_bridge"]}},
    "verdict": verdict,
    "verdict_reason": verdict_reason,
    "elapsed_s": elapsed,
}
per_query = []
for i, t in enumerate(tasks):
    row = {"q": i, "hop": t["hop"], "start": t["start"], "gold": t["gold"],
           "hits": {f"{n}|X={X}": hits[(n, X)].get(i) for (n, X) in configs},
           "ranks": {f"{n}|X={X}": ranks[(n, X)].get(i) for (n, X) in configs}}
    per_query.append(row)
out["per_query"] = per_query
payload = json.dumps({"cells": out["cells"], "hop": out["hop_summary"], "v": verdict}, sort_keys=True)
out["self_sha256"] = hashlib.sha256(payload.encode()).hexdigest()[:16]

with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"[write] {OUT_JSON}  elapsed={elapsed}s  t={tnow()}", flush=True)
print("[SUMMARY]", json.dumps({
    "verdict": verdict, "arm_verdicts": verdicts,
    "hop_summary": {f"{a}|{h}": hop_summary[(a, h)] for a in ARMS for h in HOPS},
    "random_baseline": {h: round(rand_by_hop[h], 4) for h in HOPS},
    "full_ref": full_pr5, "full_ranks": full_ranks,
    "moderation": {a: {"pr5": moderation[a]["trend_pr5"], "rank": moderation[a]["rank_diag_trend"],
                       "rank_gain": moderation[a]["rank_diag_gain_by_hop"]} for a in ARMS},
    "elapsed": elapsed, "sha": out["self_sha256"]}, ensure_ascii=False, indent=1), flush=True)
