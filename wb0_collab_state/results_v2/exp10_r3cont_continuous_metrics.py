#!/usr/bin/env python3.11
# exp10_r3cont_continuous_metrics.py  —  WB0-INSTR-20261024-EXP-10-R3CONT（instruction 1792773164-645ad3）
# R3 图扩散路由·连续指标口径重析（MRR / gold-rank 替代已饱和的 PR@5，66 题 × 24 组）
#
# 与 R3 原件（r3_graph_diffusion.py）及 EXP-09-R3R（exp09_r3r_diffusion_rerun.py）完全一致的图与 PPR 口径：
#   - 数据 r2_longbench_multihop_2wikimqa.json（200 条）；norm() 归一化；题内 title 共现无向图，uniform w=1
#   - louvain_communities(resolution=1.0, seed=20261022)
#   - PPR p_t=(1-d)*T^T p_{t-1}+d*tele, d=0.85, 迭代数=hop；tele 在 seed 上均匀
#   - top-K 门控 tie-break：共现次数降序、节点 id 升序；死点 uniform teleport 1/N
#   - 排序 np.argsort(-p, kind="stable")
# 本令唯一实质改动 = 评估指标：
#   PR@5(M1) 已饱和（96.97% 样本 gold∈seed → teleport 结构性自命中恒 1.0）→ 改用连续型排序指标
#   主指标 MRR = mean(1/rank_gold)，rank_gold = gold 在 PPR 全 N 节点全序中的名次（1-based，不截断、无 top-K 天花板）
#   副指标（披露）：gold rank 中位数、rank≤10 比例、PR@5(M1)（与 R3R 并列做可复算一致性核验）
# 纯 CPU；seed=20261022；网格 hop{1,2,3} × K{3,5,10,inf} × block{F,T} = 24 组
# 红线：R3 原件与 R3R 产物一律只读（ORIG_JSON / R3R_JSON 仅用于一致性自检），本令产物另存新名。
import json, re, time, hashlib, math
from collections import defaultdict
import numpy as np
import networkx as nx

SEED = 20261022
START = time.time()
def tnow(): return round(time.time()-START, 2)

DS = "/workspace/wb2/r2_longbench_multihop_2wikimqa.json"
ORIG_JSON = "/workspace/wb2/results_v2/r3_graph_diffusion.json"
R3R_JSON = "/workspace/wb2/results_v2/exp09_r3r_diffusion_rerun.json"
OUT = "/workspace/wb2/results_v2/exp10_r3cont_continuous_metrics.json"
DAMP = 0.85
INSTR_ID = "1792773164-645ad3"
IDEMPOTENCY_KEY = "WB0-INSTR-20261024-EXP-10-R3CONT"

# ---------------- 1. 载入真实数据（与 R3 原件同口径） ----------------
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

# ---------------- 2. 共现图（与原件同一张图） ----------------
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

# ---------------- 3. 社区划分（块对角约束用） ----------------
G = nx.Graph(); G.add_nodes_from(range(n_ent)); G.add_edges_from(co_edges)
comms = nx.community.louvain_communities(G, seed=SEED, resolution=1.0)
node2comm = {}
for ci, cset in enumerate(comms):
    for u in cset: node2comm[u] = ci
comm_sizes = sorted([len(c) for c in comms], reverse=True)
print(f"[community] n={len(comms)} sizes(top10)={comm_sizes[:10]}  t={tnow()}", flush=True)

# ---------------- 4. seed/gold/评测集（M1 主口径，与 R3R 逐字一致） ----------------
def find_seeds(q):
    return [ti for ti in range(n_ent) if titles[ti] and titles[ti] in per_q_text[q]]
def find_gold(q):
    golds = [norm(a) for a in records[q].get("answers",[])]
    return [ti for ti in range(n_ent) if titles[ti] in golds]

primary_set = []
n_total = len(records); n_gold_title = 0; n_gold_in_seed = 0
for q in range(len(records)):
    seeds = find_seeds(q); gold = find_gold(q)
    if not seeds or not gold: continue
    n_gold_title += 1
    g = gold[0]
    if g in seeds:
        n_gold_in_seed += 1
        primary_set.append((q, seeds, g, True))     # stratum A: gold∈seed
    else:
        primary_set.append((q, seeds, g, False))    # stratum B: gold∉seed
n_evaluable_primary = len(primary_set)
qids = [q for (q,_,_,_) in primary_set]
print(f"[eval] n_total={n_total} n_gold_title={n_gold_title} n_gold_in_seed={n_gold_in_seed} "
      f"n_evaluable_primary(M1)={n_evaluable_primary}  t={tnow()}", flush=True)

# ---------------- 5. 转移矩阵（与原件逐字一致） ----------------
def build_T(K, block):
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
            w = 1.0/len(nbrs)
            for v in nbrs: T[u,v] = w
        else:
            T[u,:] = 1.0/N; n_dead += 1
    return T, n_dead

def ppr_series(T, seed_set, max_hop):
    N = n_ent
    tele = np.zeros(N)
    for s in seed_set: tele[s]+=1
    if tele.sum()==0: return [np.zeros(N)]*max_hop
    tele = tele/tele.sum()
    ps = []
    p = tele.copy()
    for _ in range(max_hop):
        p = (1-DAMP)*(T.T @ p) + DAMP*tele
        ps.append(p)
    return ps

# ---------------- 6. 网格：hop{1,2,3} × K{3,5,10,inf} × block{F,T}；连续指标 ----------------
HOPS = [1,2,3]; KS = [3,5,10,None]; BLOCKS = [False, True]
T_cache = {}
for K in KS:
    for b in BLOCKS:
        T_cache[(K,b)] = build_T(K,b)
        print(f"[build T] K={K} block={b}  t={tnow()}", flush=True)

configs = []
per_query = {}   # qid -> {cfg_key: {"rank":..,"rr":..,"pr5_m1":..}}
detail_ranks = {}  # cfg_key -> list of (qid, rank) 仅保留秩用于中位数检查

for K in KS:
    for b in BLOCKS:
        T,_ = T_cache[(K,b)]
        rr_all = {h: [] for h in HOPS}
        rr_A = {h: [] for h in HOPS}; rr_B = {h: [] for h in HOPS}
        rk_all = {h: [] for h in HOPS}; rk_A = {h: [] for h in HOPS}; rk_B = {h: [] for h in HOPS}
        pr5 = {h: [] for h in HOPS}
        for (q, seeds, g, gold_in_seed) in primary_set:
            ps = ppr_series(T, seeds, 3)
            for h in HOPS:
                p = ps[h-1]
                order = [int(i) for i in np.argsort(-p, kind="stable")]
                pos = order.index(g)
                rank = pos + 1                      # 1-based 全序名次，不截断
                rr = 1.0/rank
                rr_all[h].append(rr); rk_all[h].append(rank)
                (rr_A if gold_in_seed else rr_B)[h].append(rr)
                (rk_A if gold_in_seed else rk_B)[h].append(rank)
                pr5[h].append(1 if rank <= 5 else 0)   # M1 口径 PR@5（等价 top5 命中，不排除 seed）
                per_query.setdefault(q, {})[f"K={K if K is not None else 'inf'},block={b},hop={h}"] = {
                    "rank_gold": rank, "rr": round(rr, 6), "pr5_m1": (1 if rank<=5 else 0)}
        for h in HOPS:
            med = float(np.median(rk_all[h]))
            configs.append({
                "hop": h, "K": (K if K is not None else "inf"), "block_diagonal": b,
                "mrr_M1": round(float(np.mean(rr_all[h])), 6),
                "gold_rank_median_M1": med,
                "gold_rank_mean_M1": round(float(np.mean(rk_all[h])), 3),
                "frac_rank_le_10_M1": round(float(np.mean([1 if r<=10 else 0 for r in rk_all[h]])), 4),
                "frac_rank_eq_1_M1": round(float(np.mean([1 if r==1 else 0 for r in rk_all[h]])), 4),
                "pr5_M1_recomputed": round(float(np.mean(pr5[h])), 4),
                "mrr_stratumA": round(float(np.mean(rr_A[h])), 6) if rr_A[h] else None,
                "gold_rank_median_stratumA": float(np.median(rk_A[h])) if rk_A[h] else None,
                "mrr_stratumB": round(float(np.mean(rr_B[h])), 6) if rr_B[h] else None,
                "gold_rank_median_stratumB": float(np.median(rk_B[h])) if rk_B[h] else None,
                "n_M1": n_evaluable_primary, "n_stratumA": len(rr_A[h]), "n_stratumB": len(rr_B[h]),
            })
        print(f"[cfg] K={K} block={b} MRR={[round(float(np.mean(rr_all[h])),4) for h in HOPS]} "
              f"medrank={[float(np.median(rk_all[h])) for h in HOPS]}  t={tnow()}", flush=True)

# ---------------- 7. 基线与 delta（基线 = hop=1, K=inf, block=False） ----------------
base_cfg = next(c for c in configs if c["hop"]==1 and c["K"]=="inf" and c["block_diagonal"]==False)
base_mrr = base_cfg["mrr_M1"]; base_mrr_A = base_cfg["mrr_stratumA"]
for c in configs:
    c["delta_mrr_vs_1hop_baseline_pp"] = round((c["mrr_M1"]-base_mrr)*100, 4)
    c["delta_mrr_stratumA_vs_baseline_pp"] = (
        round((c["mrr_stratumA"]-base_mrr_A)*100, 4) if c["mrr_stratumA"] is not None else None)

# ---------------- 8. 随机基线（每查询 200 次抽样，均匀随机全序位置） ----------------
rng = np.random.default_rng(SEED)
rand_rrs = []
for (q, seeds, g, gis) in primary_set:
    ranks = rng.integers(0, n_ent, size=200)       # 0-based 均匀位置
    rand_rrs.append(float(np.mean(1.0/(ranks+1))))
rand_mrr = float(np.mean(rand_rrs)) if rand_rrs else None
H_N = float(sum(1.0/(r+1) for r in range(n_ent)))   # 调和数 H_N
theory_e_inv_rank = H_N/n_ent                        # E[1/rank] = H_N/N
naive_inv_expected_rank = 1.0/((n_ent+1)/2.0)        # 1/E[rank] = 2/(N+1)
print(f"[random baseline M1] MRR={rand_mrr:.6f} (n_query={len(rand_rrs)}, 200 draws/query) "
      f"theory H_N/N={theory_e_inv_rank:.6f}  1/E[rank]={naive_inv_expected_rank:.6f}  t={tnow()}", flush=True)

# ---------------- 9. 无约束序列与拐点 ----------------
unc = {c["hop"]: c["mrr_M1"] for c in configs if c["K"]=="inf" and not c["block_diagonal"]}
peak_hop = max(unc, key=lambda h: unc[h])
decline_at_3_pp = round((max(unc.values())-unc[3])*100, 4)
inflections = {}
for K in KS:
    for b in BLOCKS:
        series = {c["hop"]: c["mrr_M1"] for c in configs
                  if c["K"]==(K if K is not None else "inf") and c["block_diagonal"]==b}
        vals = [series[h] for h in HOPS]
        inflections[f"K={K if K is not None else 'inf'},block={b}"] = {
            "series_mrr": [round(v,6) for v in vals],
            "peak_hop": HOPS[int(np.argmax(vals))],
            "decline_after_peak": any(vals[i+1] < vals[i] for i in range(len(vals)-1)),
            "delta_3hop_vs_peak_pp": round((max(vals)-vals[2])*100, 4)}
print(f"[unconstrained series MRR] {unc} peak_hop={peak_hop} decline@3={decline_at_3_pp}pp  t={tnow()}", flush=True)

block_effect = {}
for h in HOPS:
    for K in KS:
        kk = K if K is not None else "inf"
        u = next(c["mrr_M1"] for c in configs if c["hop"]==h and c["K"]==kk and not c["block_diagonal"])
        bd = next(c["mrr_M1"] for c in configs if c["hop"]==h and c["K"]==kk and c["block_diagonal"])
        block_effect[f"hop={h},K={kk}"] = {"unconstrained": u, "block_diagonal": bd,
                                          "delta_pp": round((bd-u)*100, 4)}

# ---------------- 10. 可复算自检：PR@5(M1) 与 R3R 逐组一致 ----------------
r3r = None
try:
    r3r = json.load(open(R3R_JSON, encoding="utf-8"))
except Exception as e:
    print(f"[warn] R3R json 不可读: {e}", flush=True)
recompute_selfcheck = {"compared_with": "results_v2/exp09_r3r_diffusion_rerun.json::configs[].pr5_primary_M1",
                       "n_configs_compared": 0, "mismatch": None, "mismatch_detail": []}
if r3r:
    r3r_map = {(c["hop"], str(c["K"]), bool(c["block_diagonal"])): c.get("pr5_primary_M1")
               for c in r3r.get("configs", [])}
    mm = []
    for c in configs:
        key = (c["hop"], str(c["K"]), bool(c["block_diagonal"]))
        if key in r3r_map and r3r_map[key] is not None:
            if abs(float(r3r_map[key]) - float(c["pr5_M1_recomputed"])) > 1e-9:
                mm.append({"cfg": key, "r3r": r3r_map[key], "this_run": c["pr5_M1_recomputed"]})
    recompute_selfcheck["n_configs_compared"] = len([k for k in r3r_map if r3r_map[k] is not None])
    recompute_selfcheck["mismatch"] = len(mm)
    recompute_selfcheck["mismatch_detail"] = mm
print(f"[selfcheck] PR@5(M1) vs R3R: compared={recompute_selfcheck['n_configs_compared']} "
      f"mismatch={recompute_selfcheck['mismatch']}  t={tnow()}", flush=True)

# ---------------- 11. 判据判定（§5 逐字实现，机器可读） ----------------
cands = [c for c in configs if c["hop"] >= 2]
confirmed_cfgs = [c for c in cands
                  if c["delta_mrr_vs_1hop_baseline_pp"] >= 2.0
                  and c["delta_mrr_stratumA_vs_baseline_pp"] is not None
                  and c["delta_mrr_stratumA_vs_baseline_pp"] >= 1.0]
max_delta = max(c["delta_mrr_vs_1hop_baseline_pp"] for c in configs)
max_delta_A = max((c["delta_mrr_stratumA_vs_baseline_pp"] or -999) for c in configs)
mrr_range = max(c["mrr_M1"] for c in configs) - min(c["mrr_M1"] for c in configs)
stratumA_set = {round(c["mrr_stratumA"], 12) for c in configs if c["mrr_stratumA"] is not None}
e5_no_range = (mrr_range*100 < 0.1) and (len(stratumA_set) == 1)
all_lt_2pp = all(c["delta_mrr_vs_1hop_baseline_pp"] < 2.0 for c in configs)

if e5_no_range:
    verdict = "e5_withdrawn"
elif confirmed_cfgs:
    verdict = "confirmed"
elif all_lt_2pp:
    verdict = "refuted"
else:
    verdict = "pending"   # 仅当存在 ≥2pp 但 stratum A 未同向达标等中间态，交由回执显式披露
criteria_check = {
    "criterion_source": "WB0-INSTR-20261024-EXP-10-R3CONT §5（逐字，未修改）",
    "baseline": {"hop":1, "K":"inf", "block_diagonal":False, "mrr_M1": base_mrr, "mrr_stratumA": base_mrr_A},
    "n_configs": len(configs),
    "n_candidates_hop_ge_2": len(cands),
    "max_delta_mrr_pp": round(max_delta, 4),
    "max_delta_mrr_stratumA_pp": round(max_delta_A, 4),
    "configs_ge_2pp": [{"hop":c["hop"],"K":c["K"],"block":c["block_diagonal"],
                        "delta_pp":c["delta_mrr_vs_1hop_baseline_pp"],
                        "delta_A_pp":c["delta_mrr_stratumA_vs_baseline_pp"]} for c in cands
                       if c["delta_mrr_vs_1hop_baseline_pp"] >= 2.0],
    "configs_ge_2pp_and_A_ge_1pp": [{"hop":c["hop"],"K":c["K"],"block":c["block_diagonal"],
                                     "delta_pp":c["delta_mrr_vs_1hop_baseline_pp"],
                                     "delta_A_pp":c["delta_mrr_stratumA_vs_baseline_pp"]} for c in confirmed_cfgs],
    "all_configs_delta_lt_2pp": bool(all_lt_2pp),
    "mrr_dynamic_range_pp": round(mrr_range*100, 4),
    "stratumA_mrr_distinct_values": len(stratumA_set),
    "e5_no_dynamic_range": bool(e5_no_range),
    "auto_verdict": verdict,
}

# ---------------- 12. 输出 ----------------
sha = lambda p: hashlib.sha256(open(p,'rb').read()).hexdigest()[:16]
out = {
  "schema": "exp10_r3cont_continuous_metrics/v1",
  "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
  "instruction_id": INSTR_ID,
  "idempotency_key": IDEMPOTENCY_KEY,
  "author": "WB2",
  "graph_construction": {
      "dataset": "longbench_multihop_2wikimqa", "n_records": len(records),
      "edge_rule": "题内 title 共现，无向，uniform w=1（与 R3 原件/R3R 同图）",
      "n_entities": n_ent, "n_edges": len(co_edges), "n_dead_end_raw": int(n_dead_end_raw),
      "community": "louvain_communities(resolution=1.0, seed=20261022)",
      "n_communities": len(comms), "community_sizes_top10": comm_sizes[:10],
      "ppr": {"damping": DAMP, "iterations_eq_hop": True,
              "dead_end": "uniform teleport 1/N", "tie_break": "共现次数降序、节点 id 升序",
              "order": "np.argsort(-p, kind='stable')"},
      "seed": SEED},
  "eval_protocol": {
      "metric_primary": "MRR = mean(1/rank_gold)，rank_gold 为 gold 在 PPR 全 N 节点全序中的 1-based 名次（不截断、无 top-K 天花板）",
      "metric_secondary": ["gold_rank_median", "frac_rank_le_10", "PR@5(M1，仅用于与 R3R 可复算一致性核验)"],
      "gating": "M1（不排除 seed；gold 按答案别名归一化匹配全局 title 节点即计入）",
      "n_evaluable": n_evaluable_primary,
      "n_gold_title_matched": n_gold_title, "n_gold_in_seed": n_gold_in_seed,
      "strata": {"A_gold_in_seed": sum(1 for x in primary_set if x[3]),
                 "B_gold_not_in_seed": sum(1 for x in primary_set if not x[3])},
      "note": "PR@5(M1) 已饱和（gold∈seed 占比 %.2f%%，teleport 结构性自命中），本令以连续指标替代；"
              "本令不改判 H-R3-DIFFUSION 的 PR@5 判据" % (100.0*n_gold_in_seed/max(1,n_evaluable_primary))},
  "configs": configs,
  "qids_evaluated": qids,
  "per_query": per_query,
  "random_baseline": {
      "method": "每查询 200 次均匀随机全序位置抽样（与 R3R 同法 200 draws/query）",
      "mrr_sampled": round(rand_mrr, 6) if rand_mrr is not None else None,
      "theory_E_inv_rank_HN_over_N": round(theory_e_inv_rank, 6),
      "naive_inv_expected_rank_2_over_N_plus_1": round(naive_inv_expected_rank, 6),
      "note": "E[1/rank]=H_N/N 与 1/E[rank]=2/(N+1) 因 Jensen 不等不相等，二者均披露，不互相冒充"},
  "unconstrained_series": {"mrr_by_hop_K_inf": {str(k): v for k, v in unc.items()},
                            "peak_hop": peak_hop, "delta_3hop_vs_peak_pp": decline_at_3_pp},
  "inflections_by_config": inflections,
  "block_diagonal_effect_by_hop_K": block_effect,
  "criteria_check": criteria_check,
  "recompute_selfcheck": recompute_selfcheck,
  "frozen_assets_readonly": {
      "r3_graph_diffusion.json_sha256_16": sha(ORIG_JSON) if __import__("os").path.exists(ORIG_JSON) else None,
      "exp09_r3r_diffusion_rerun.json_sha256_16": sha(R3R_JSON) if __import__("os").path.exists(R3R_JSON) else None,
      "note": "原件与 R3R 产物在本令中只读（仅用于 PR@5 一致性自检），未被修改或覆盖"},
  "runtime_sec": round(time.time()-START, 2),
}
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"[out] {OUT}  verdict={verdict}  max_delta={max_delta}pp  "
      f"mrr_range={round(mrr_range*100,4)}pp  runtime={out['runtime_sec']}s", flush=True)
