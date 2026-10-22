#!/usr/bin/env python3.11
# r3_graph_diffusion.py  —  WB0-INSTR-20261022-R3（instruction 1792650492-ff2d3a）
# R3：图扩散路由（多跳 + Top-K 门控 + 块对角）
# 纯 CPU；真实数据 r2_longbench_multihop_2wikimqa.json（200 条 2wikimqa / 1404 实体 / 7475 无向共现边）；
# 图构造与评测口径与 EXP-03 完全一致（同图、同 seed/gold、同 PPR 递推、同排除 seed 规则），仅改扩散配置。
import json, re, time, hashlib
from collections import defaultdict
import numpy as np
import networkx as nx

SEED = 20261022
START = time.time()
def tnow(): return round(time.time()-START, 2)

DS = "/workspace/wb2/r2_longbench_multihop_2wikimqa.json"
OUT = "/workspace/wb2/results_v2/r3_graph_diffusion.json"
DAMP = 0.85

# ---------------- 1. 载入真实数据（与 EXP-03 同口径） ----------------
records = [json.loads(l) for l in open(DS, encoding="utf-8").read().splitlines() if l.strip()]
assert len(records) == 200, f"expect 200 records, got {len(records)}"
print(f"[load] {len(records)} records  t={tnow()}", flush=True)

def norm(t):
    t = t.lower(); t = t.replace("\u2013","-").replace("\u2014","-")
    t = re.sub(r"[^0-9a-z\- ]"," ",t); t = re.sub(r"\s+"," ",t).strip(); return t

title_to_idx = {}; titles = []
def get_idx(nt):
    if nt not in title_to_idx:
        title_to_idx[nt]=len(titles); titles.append(nt)
    return title_to_idx[nt]

per_q_titles = []; per_q_text = []
for rec in records:
    ctx = rec.get("context","")
    parts = re.split(r"Passage\s+\d+:\s*\n", ctx)
    q_titles = set()
    for p in parts:
        if not p.strip(): continue
        lines = p.split("\n"); raw = lines[0].strip()
        if not raw: continue
        nt = norm(raw)
        if not nt: continue
        q_titles.add(get_idx(nt))
    per_q_titles.append(q_titles)
    per_q_text.append(norm(rec.get("input","")))
n_ent = len(titles)
print(f"[entities] {n_ent} titles  t={tnow()}", flush=True)

# ---------------- 2. 共现图（题内 title 共现，无向，与 EXP-03 同一张图） ----------------
co_edges = set(); co_count = defaultdict(int)
freq = defaultdict(int)
for q_t in per_q_titles:
    for ti in q_t: freq[ti]+=1
    lst = list(q_t)
    for a in range(len(lst)):
        for b in range(a+1,len(lst)):
            u,v = (lst[a],lst[b]) if lst[a]<lst[b] else (lst[b],lst[a])
            co_edges.add((u,v)); co_count[(u,v)] += 1
print(f"[cooccur] edges={len(co_edges)}  t={tnow()}", flush=True)

adj = defaultdict(list)
for (u,v) in co_edges:
    adj[u].append(v); adj[v].append(u)
n_dead_end_raw = sum(1 for u in range(n_ent) if len(adj[u])==0)

# ---------------- 3. 社区划分（块对角约束用；Louvain，确定性 seed） ----------------
G = nx.Graph(); G.add_nodes_from(range(n_ent)); G.add_edges_from(co_edges)
comms = nx.community.louvain_communities(G, seed=SEED, resolution=1.0)
node2comm = {}
for ci, cset in enumerate(comms):
    for u in cset: node2comm[u] = ci
comm_sizes = sorted([len(c) for c in comms], reverse=True)
print(f"[community] n={len(comms)} sizes(top10)={comm_sizes[:10]}  t={tnow()}", flush=True)

# ---------------- 4. seed/gold/评测集（与 EXP-03 逐字一致） ----------------
def find_seeds(q):
    return [ti for ti in range(n_ent) if titles[ti] and titles[ti] in per_q_text[q]]
def find_gold(q):
    golds = [norm(a) for a in records[q].get("answers",[])]
    return [ti for ti in range(n_ent) if titles[ti] in golds]

evaluable = []; gold_title_qs = []
n_total = len(records); n_gold_title = 0; n_gold_in_seed = 0
for q in range(len(records)):
    seeds = find_seeds(q); gold = find_gold(q)
    if not seeds or not gold: continue
    n_gold_title += 1; gold_title_qs.append((q, seeds, gold[0]))
    g = gold[0]
    if g in seeds:
        n_gold_in_seed += 1; continue
    evaluable.append((q, seeds, g))
print(f"[eval] n_total={n_total} n_gold_title={n_gold_title} n_gold_in_seed={n_gold_in_seed} "
      f"n_evaluable={len(evaluable)}  t={tnow()}", flush=True)

# ---------------- 5. 转移矩阵构造（hop-K 门控 × 块对角） ----------------
def build_T(K, block):
    """K=None 表示不限制（全邻居）；top-K 按共现次数降序、id 升序 tie-break；
    block=True 只保留同社区邻居；死点（无保留邻居）uniform teleport（EXP-03 口径，显式披露）。"""
    N = n_ent
    T = np.zeros((N,N), dtype=np.float64)
    n_dead = 0
    for u in range(N):
        nbrs = adj[u]
        if K is not None:
            nbrs = sorted(nbrs, key=lambda v: (-co_count[(min(u,v),max(u,v))], v))[:K]
        if block:
            nbrs = [v for v in nbrs if node2comm[v]==node2comm[u]]
        if nbrs:
            w = 1.0/len(nbrs)          # 边权均匀（对齐 EXP-03 口径）
            for v in nbrs: T[u,v] = w
        else:
            T[u,:] = 1.0/N; n_dead += 1   # 死点 teleport
    return T, n_dead

def ppr_k(T, seed_set, k):
    """k 跳截断 PPR：p_0=tele；p_t=(1-d)*T^T p_{t-1} + d*tele（递推与 EXP-03 一致，仅迭代数=hop）。"""
    N = n_ent
    tele = np.zeros(N)
    for s in seed_set: tele[s]+=1
    if tele.sum()==0: return np.zeros(N)
    tele = tele/tele.sum()
    p = tele.copy()
    for _ in range(k):
        p = (1-DAMP)*(T.T @ p) + DAMP*tele
    return p

def pr_at_5(T, seeds, gold, k):
    p = ppr_k(T, seeds, k)
    order = np.argsort(-p, kind="stable")
    top5 = [int(i) for i in order if int(i) not in seeds][:5]
    return 1 if gold in top5 else 0

# ---------------- 6. 网格：hop{1,2,3} × K{3,5,10,∞} × block{F,T} ----------------
HOPS = [1,2,3]; KS = [3,5,10,None]; BLOCKS = [False, True]
T_cache = {}
for K in KS:
    for b in BLOCKS:
        T_cache[(K,b)] = build_T(K,b)
        print(f"[build T] K={K} block={b}  t={tnow()}", flush=True)

configs = []
for h in HOPS:
    for K in KS:
        for b in BLOCKS:
            T,_ = T_cache[(K,b)]
            ev_hits = [pr_at_5(T, s, g, h) for (_,s,g) in evaluable]
            g6_hits = [pr_at_5(T, s, g, h) for (_,s,g) in gold_title_qs]
            pr5_ev = float(np.mean(ev_hits)) if ev_hits else None
            pr5_g6 = float(np.mean(g6_hits)) if g6_hits else None
            configs.append({"hop":h, "K":(K if K is not None else "inf"), "block_diagonal":b,
                            "pr5_evaluable": round(pr5_ev,4) if pr5_ev is not None else None,
                            "pr5_goldtitle66": round(pr5_g6,4),
                            "per_query_evaluable": [{"q":q,"hit":hh} for (q,_,_),hh in zip(evaluable, ev_hits)]})
            print(f"[cfg] hop={h} K={K} block={b} pr5_ev={pr5_ev} pr5_g66={pr5_g6:.4f}  t={tnow()}", flush=True)

baseline = next(c for c in configs if c["hop"]==1 and c["K"]=="inf" and c["block_diagonal"]==False)
base_pr5 = baseline["pr5_evaluable"]
for c in configs:
    c["delta_vs_1hop_baseline_pp"] = round((c["pr5_evaluable"]-base_pr5)*100, 2) if c["pr5_evaluable"] is not None else None

# ---------------- 7. 随机基线（任务难度/天花板核查，WB0 EXP-04 教训） ----------------
rng = np.random.default_rng(SEED)
rand_rates = []
for (q, seeds, g) in evaluable:
    cand = [i for i in range(n_ent) if i not in seeds]
    hits = []
    for _ in range(200):
        pick = rng.choice(len(cand), size=5, replace=False)
        hits.append(1 if g in [cand[j] for j in pick] else 0)
    rand_rates.append(float(np.mean(hits)))
rand_pr5 = float(np.mean(rand_rates)) if rand_rates else None
print(f"[random baseline] PR@5={rand_pr5:.4f} (n={len(rand_rates)})  t={tnow()}", flush=True)

# ---------------- 8. 无约束序列（hop=1,2,3; K=inf; block=F）与拐点 ----------------
unc = {c["hop"]: c["pr5_evaluable"] for c in configs if c["K"]=="inf" and not c["block_diagonal"]}
peak_hop = max(unc, key=lambda h: (unc[h] if unc[h] is not None else -1))
decline_at_3 = (unc[3] is not None and unc[2] is not None and unc[3] < unc[2])
print(f"[unconstrained series] {unc} peak_hop={peak_hop} decline_at_3={decline_at_3}  t={tnow()}", flush=True)

# 每 (K,block) 的 hop 序列拐点
inflections = {}
for K in KS:
    for b in BLOCKS:
        series = {c["hop"]: c["pr5_evaluable"] for c in configs
                  if c["K"]==(K if K is not None else "inf") and c["block_diagonal"]==b}
        vals = [series[h] for h in HOPS]
        pk = HOPS[int(np.argmax(vals))]
        inflections[f"K={K if K is not None else 'inf'},block={b}"] = {
            "series": [round(v,4) for v in vals], "peak_hop": pk,
            "decline_after_peak": any(vals[i+1] < vals[i] for i in range(len(vals)-1))}

# ---------------- 9. 判定（R3 判据 × D1 三分） ----------------
POWER_MIN = 20
best_cfg = max(configs, key=lambda c: (c["delta_vs_1hop_baseline_pp"] if c["delta_vs_1hop_baseline_pp"] is not None else -1e9))
best_delta = best_cfg["delta_vs_1hop_baseline_pp"]
n_ev = len(evaluable)
criteria = {
    "multi_hop_gain_ge2pp": bool(best_delta is not None and best_delta >= 2.0),
    "unconstrained_3hop_decline": bool(decline_at_3),
    "random_baseline_le0.7": bool(rand_pr5 is not None and rand_pr5 <= 0.7),
    "baseline_saturated_1.0": bool(base_pr5 == 1.0),
}
if n_ev < POWER_MIN:
    verdict = "pending"
    reason = (f"D1 功效闸门：有效多跳可评样本 n={n_ev}（200 题中 {n_gold_title} 题 gold 为全局 passage-title、"
              f"其中 {n_gold_in_seed} 题答案实体已在题面共现→seed 退化），远低于功效下限 {POWER_MIN}，"
              f"点估计不可作为 confirmed/refuted 依据。"
              f"R3 判据核查：最佳配置 {best_cfg['hop']}跳/K={best_cfg['K']}/block={best_cfg['block_diagonal']} "
              f"PR@5={best_cfg['pr5_evaluable']} vs 1跳基线 {base_pr5}（Δ={best_delta}pp，≥2pp 触发={criteria['multi_hop_gain_ge2pp']}）；"
              f"无约束 3 跳下降={decline_at_3}（序列 {unc}）。"
              f"天花板核查：随机基线 PR@5={round(rand_pr5,4) if rand_pr5 is not None else None}（≤0.7 易度闸门={criteria['random_baseline_le0.7']}）；"
              f"1 跳基线饱和={criteria['baseline_saturated_1.0']}。"
              f"按 D1 未把『测不出』写成『已证伪』，亦未把 n=2 点估计写成 confirmed。")
elif criteria["multi_hop_gain_ge2pp"] and decline_at_3:
    verdict = "confirmed"; reason = f"存在 (hop,K) Δ={best_delta}pp≥2pp 且无约束 3 跳下降；n={n_ev}"
elif best_delta is not None and best_delta < 2.0:
    verdict = "refuted"; reason = f"所有组合均不优于 1 跳基线（最大 Δ={best_delta}pp<2pp）；n={n_ev}"
else:
    verdict = "pending"; reason = f"结果矛盾或功效不足；n={n_ev}，best Δ={best_delta}pp"
print(f"[verdict] {verdict}: {reason}  t={tnow()}", flush=True)

elapsed = round(time.time()-START, 2)
out = {
    "schema": "r3_graph_diffusion/v1",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+0800", time.localtime()),
    "instruction_id": "1792650492-ff2d3a",
    "dataset": "r2_longbench_multihop_2wikimqa (200 条 2wikimqa)",
    "graph_construction": {
        "n_entities": n_ent, "cooccur_edges": len(co_edges),
        "edge_weight": "uniform w=1（与 EXP-03 同一张图、同一口径；题内 title 共现，无向）",
        "topk_ranking": "top-K 门控按共现次数降序、节点 id 升序 tie-break（次数仅用于门控选择，不入边权）",
        "dead_end": "无保留邻居→uniform teleport 1/N（EXP-03 口径，跨社区泄漏已披露）",
        "community_method": "networkx louvain_communities(resolution=1.0, seed=20261022)，确定性",
        "n_communities": len(comms), "community_sizes_top10": comm_sizes[:10],
        "n_raw_dead_end_nodes": n_dead_end_raw,
        "n_dead_end_after_blockdiag": {f"K={K},block=True": T_cache[(K,True)][1] for K in KS},
    },
    "eval_protocol": {
        "n_total": n_total, "n_gold_title": n_gold_title, "n_gold_in_seed": n_gold_in_seed,
        "n_evaluable": n_ev, "seeds": "题面提及且为全局 title 节点（EXP-03 同口径）",
        "gold": "answers 归一化后匹配全局 title 节点（EXP-03 同口径）",
        "exclude_seeds_from_ranking": True, "ppr": "p_t=(1-d)*T^T p_{t-1}+d*tele, d=0.85, 迭代数=hop（k 跳截断）",
        "secondary_set": "gold_title66（含 64 题 gold∈seed 结构性 PR@5=0，仅作水平披露，不参与判据）",
        "seed": SEED,
    },
    "random_baseline_pr5": round(rand_pr5,4) if rand_pr5 is not None else None,
    "random_baseline_note": "每查询 200 次随机抽 5（非 seed 候选），均值；用于任务难度/天花板核查",
    "baseline_1hop_unconstrained_pr5": base_pr5,
    "configs": configs,
    "unconstrained_series_pr5": unc,
    "inflections": inflections,
    "criteria_check": criteria,
    "verdict": verdict,
    "verdict_reason": reason,
    "elapsed_s": elapsed,
    "self_sha256": hashlib.sha256(json.dumps(
        {"cfg":[(c["hop"],str(c["K"]),c["block_diagonal"],c["pr5_evaluable"]) for c in configs],
         "unc":unc,"rand":round(rand_pr5,4) if rand_pr5 is not None else None,"v":verdict},
        sort_keys=True).encode()).hexdigest()[:16],
}
with open(OUT,"w",encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"[write] {OUT}  elapsed={elapsed}s  t={tnow()}", flush=True)
