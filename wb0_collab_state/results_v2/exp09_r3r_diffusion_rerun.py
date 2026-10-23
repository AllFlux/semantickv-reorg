#!/usr/bin/env python3.11
# exp09_r3r_diffusion_rerun.py  —  WB0-INSTR-20261023-EXP-09-R3R（instruction 1792770514-f24a42）
# R3 图扩散路由·扩样本/口径修正重跑（supersedes 1792650492-ff2d3a；原件与原件回执不覆盖、不删）
#
# 与 R3 原件（r3_graph_diffusion.py）完全一致的口径：
#   - 数据 r2_longbench_multihop_2wikimqa.json（200 条）；norm() 归一化；题内 title 共现无向图，uniform w=1
#   - louvain_communities(resolution=1.0, seed=20261022)
#   - PPR p_t=(1-d)*T^T p_{t-1}+d*tele, d=0.85, 迭代数=hop；tele 在 seed 上均匀
#   - top-K 门控 tie-break：共现次数降序、节点 id 升序；死点 uniform teleport 1/N
#   - 排序 np.argsort(-p, kind="stable")
# 本令唯一实质改动 = 可评估性口径：
#   M1(PRIMARY)：不排除 seed，gold 按答案别名归一化匹配全局 title 节点即计入 → n_evaluable 由 2 提至 66
#   M2(LEGACY) ：维持 exclude_seeds_from_ranking=True，仅作口径敏感性披露，不参与判据
# 纯 CPU；seed=20261022；网格 hop{1,2,3} × K{3,5,10,inf} × block{F,T} = 24 组
import json, re, time, hashlib
from collections import defaultdict
import numpy as np
import networkx as nx

SEED = 20261022
START = time.time()
def tnow(): return round(time.time()-START, 2)

DS = "/workspace/wb2/r2_longbench_multihop_2wikimqa.json"
ORIG_JSON = "/workspace/wb2/results_v2/r3_graph_diffusion.json"
OUT = "/workspace/wb2/results_v2/exp09_r3r_diffusion_rerun.json"
DAMP = 0.85
INSTR_ID = "1792770514-f24a42"

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

# ---------------- 4. seed/gold/评测集 ----------------
def find_seeds(q):
    return [ti for ti in range(n_ent) if titles[ti] and titles[ti] in per_q_text[q]]
def find_gold(q):
    golds = [norm(a) for a in records[q].get("answers",[])]
    return [ti for ti in range(n_ent) if titles[ti] in golds]

# M1 主口径集合：seed 非空 且 gold 可匹配（不排除 gold∈seed）
primary_set = []
# 原件口径集合（gold∉seed），用于 M2 与原件数值可比性自检
legacy_set = []
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
        legacy_set.append((q, seeds, g))
n_evaluable_primary = len(primary_set)
print(f"[eval] n_total={n_total} n_gold_title={n_gold_title} n_gold_in_seed={n_gold_in_seed} "
      f"n_evaluable_primary(M1)={n_evaluable_primary} n_legacy(M2原件口径)={len(legacy_set)}  t={tnow()}", flush=True)

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
    """返回 [p_1, p_2, p_3]；与原件 ppr_k 递推逐字一致，仅缓存中间步。"""
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

# ---------------- 6. 网格：hop{1,2,3} × K{3,5,10,inf} × block{F,T} ----------------
HOPS = [1,2,3]; KS = [3,5,10,None]; BLOCKS = [False, True]
T_cache = {}
for K in KS:
    for b in BLOCKS:
        T_cache[(K,b)] = build_T(K,b)
        print(f"[build T] K={K} block={b}  t={tnow()}", flush=True)

key = lambda K,b: (f"K={K if K is not None else 'inf'}", f"block={b}")
configs = []
per_query = {}   # qid -> {(K,block,hop): {"m1":hit,"m2":hit}}

for K in KS:
    for b in BLOCKS:
        T,_ = T_cache[(K,b)]
        hits_m1 = {h: [] for h in HOPS}     # M1: 不排除 seed
        hits_m2 = {h: [] for h in HOPS}     # M2(legacy 口径, 在 M1 全集 66 题上): 排除 seed
        hits_m2l = {h: [] for h in HOPS}    # M2 在原件 2 题集上（与原件 pr5_evaluable 对齐）
        hits_A = {h: [] for h in HOPS}      # stratum A (gold∈seed, 64 题) M1
        hits_B = {h: [] for h in HOPS}      # stratum B (gold∉seed, 2 题)  M1
        for (q, seeds, g, gold_in_seed) in primary_set:
            ps = ppr_series(T, seeds, 3)
            for h in HOPS:
                p = ps[h-1]
                order = [int(i) for i in np.argsort(-p, kind="stable")]
                top5_m1 = order[:5]
                top5_m2 = [i for i in order if i not in seeds][:5]
                hm1 = 1 if g in top5_m1 else 0
                hm2 = 1 if g in top5_m2 else 0
                hits_m1[h].append(hm1); hits_m2[h].append(hm2)
                (hits_A if gold_in_seed else hits_B)[h].append(hm1)
                per_query.setdefault(q, {})[f"K={K if K is not None else 'inf'},block={b},hop={h}"] = {
                    "m1_hit": hm1, "m2_hit": hm2}
        for (q, seeds, g) in legacy_set:
            ps = ppr_series(T, seeds, 3)
            for h in HOPS:
                p = ps[h-1]
                order = [int(i) for i in np.argsort(-p, kind="stable")]
                top5 = [i for i in order if i not in seeds][:5]
                hits_m2l[h].append(1 if g in top5 else 0)
        for h in HOPS:
            configs.append({
                "hop": h, "K": (K if K is not None else "inf"), "block_diagonal": b,
                "pr5_primary_M1": round(float(np.mean(hits_m1[h])), 4),
                "pr5_legacy_M2_on66": round(float(np.mean(hits_m2[h])), 4),
                "pr5_legacy_M2_on2": round(float(np.mean(hits_m2l[h])), 4) if hits_m2l[h] else None,
                "pr5_stratumA_gold_in_seed_M1": round(float(np.mean(hits_A[h])), 4) if hits_A[h] else None,
                "pr5_stratumB_gold_not_in_seed_M1": round(float(np.mean(hits_B[h])), 4) if hits_B[h] else None,
                "n_M1": n_evaluable_primary,
                "n_stratumA": len(hits_A[h]), "n_stratumB": len(hits_B[h]),
            })
        print(f"[cfg] K={K} block={b} "
              f"M1={[round(float(np.mean(hits_m1[h])),4) for h in HOPS]} "
              f"M2_66={[round(float(np.mean(hits_m2[h])),4) for h in HOPS]} "
              f"t={tnow()}", flush=True)

# ---------------- 7. 基线与 delta ----------------
base_cfg = next(c for c in configs if c["hop"]==1 and c["K"]=="inf" and c["block_diagonal"]==False)
base_pr5 = base_cfg["pr5_primary_M1"]
for c in configs:
    c["delta_vs_1hop_baseline_pp"] = round((c["pr5_primary_M1"]-base_pr5)*100, 2)

# ---------------- 8. 随机基线（M1 口径：全 1404 节点候选，每查询 200 次抽 5） ----------------
rng = np.random.default_rng(SEED)
rand_rates = []
for (q, seeds, g, gis) in primary_set:
    hits = []
    for _ in range(200):
        pick = rng.choice(n_ent, size=5, replace=False)
        hits.append(1 if g in pick else 0)
    rand_rates.append(float(np.mean(hits)))
rand_pr5 = float(np.mean(rand_rates)) if rand_rates else None
print(f"[random baseline M1] PR@5={rand_pr5:.4f} (n={len(rand_rates)})  t={tnow()}", flush=True)

# ---------------- 9. 无约束序列与拐点 ----------------
unc = {c["hop"]: c["pr5_primary_M1"] for c in configs if c["K"]=="inf" and not c["block_diagonal"]}
peak_hop = max(unc, key=lambda h: unc[h])
decline_at_3 = unc[3] < max(unc.values())
decline_at_3_pp = round((max(unc.values())-unc[3])*100, 2)
inflections = {}
for K in KS:
    for b in BLOCKS:
        series = {c["hop"]: c["pr5_primary_M1"] for c in configs
                  if c["K"]==(K if K is not None else "inf") and c["block_diagonal"]==b}
        vals = [series[h] for h in HOPS]
        inflections[f"K={K if K is not None else 'inf'},block={b}"] = {
            "series": [round(v,4) for v in vals],
            "peak_hop": HOPS[int(np.argmax(vals))],
            "decline_after_peak": any(vals[i+1] < vals[i] for i in range(len(vals)-1)),
            "delta_3hop_vs_peak_pp": round((max(vals)-vals[2])*100, 2)}
print(f"[unconstrained series] {unc} peak_hop={peak_hop} decline_at_3={decline_at_3}  t={tnow()}", flush=True)

# 附加读数：块对角 vs 同 (hop,K) 无约束，按 hop 分层
block_effect = {}
for h in HOPS:
    for K in KS:
        kk = K if K is not None else "inf"
        u = next(c["pr5_primary_M1"] for c in configs if c["hop"]==h and c["K"]==kk and not c["block_diagonal"])
        bd = next(c["pr5_primary_M1"] for c in configs if c["hop"]==h and c["K"]==kk and c["block_diagonal"])
        block_effect[f"hop={h},K={kk}"] = {"unconstrained": u, "block_diagonal": bd,
                                          "delta_pp": round((bd-u)*100, 2)}

# ---------------- 10. 与 R3 原件数值的可比性自检（B-08） ----------------
orig = None
try:
    orig = json.load(open(ORIG_JSON, encoding="utf-8"))
except Exception as e:
    print(f"[warn] 原件 json 不可读: {e}", flush=True)
compat = {}
if orig:
    ocfg = {(c["hop"], str(c["K"]), c["block_diagonal"]): c.get("pr5_evaluable") for c in orig.get("configs",[])}
    ocfg66 = {(c["hop"], str(c["K"]), c["block_diagonal"]): c.get("pr5_goldtitle66") for c in orig.get("configs",[])}
    mism2 = []; mism66 = []
    for c in configs:
        k = (c["hop"], str(c["K"]), c["block_diagonal"])
        if k in ocfg and ocfg[k] is not None and c["pr5_legacy_M2_on2"] is not None:
            if abs(ocfg[k]-c["pr5_legacy_M2_on2"]) > 1e-9: mism2.append((k, ocfg[k], c["pr5_legacy_M2_on2"]))
        if k in ocfg66 and ocfg66[k] is not None:
            if abs(ocfg66[k]-c["pr5_legacy_M2_on66"]) > 1e-9: mism66.append((k, ocfg66[k], c["pr5_legacy_M2_on66"]))
    compat = {"orig_n_evaluable": orig.get("eval_protocol",{}).get("n_evaluable"),
              "orig_n_gold_title": orig.get("eval_protocol",{}).get("n_gold_title"),
              "orig_n_gold_in_seed": orig.get("eval_protocol",{}).get("n_gold_in_seed"),
              "orig_random_baseline_pr5": orig.get("random_baseline_pr5"),
              "orig_baseline_1hop_unconstrained_pr5": orig.get("baseline_1hop_unconstrained_pr5"),
              "recomputed_M2_on2_mismatch": mism2,
              "recomputed_M2_on66_mismatch": mism66,
              "graph_identical": (orig.get("graph_construction",{}).get("n_entities")==n_ent and
                                  orig.get("graph_construction",{}).get("cooccur_edges")==len(co_edges))}
    print(f"[compat] mismatch_on2={len(mism2)} mismatch_on66={len(mism66)} graph_identical={compat['graph_identical']}  t={tnow()}", flush=True)

# ---------------- 11. 判定（§5 主口径 M1；n_evaluable_primary>=30 才允许定级） ----------------
POWER_MIN = 30
best_cfg = max(configs, key=lambda c: c["delta_vs_1hop_baseline_pp"])
best_delta = best_cfg["delta_vs_1hop_baseline_pp"]
criteria = {
    "n_evaluable_primary_ge30": bool(n_evaluable_primary >= POWER_MIN),
    "multi_hop_gain_ge2pp": bool(best_delta >= 2.0),
    "unconstrained_3hop_decline_ge1pp": bool(decline_at_3_pp >= 1.0),
}
all_below_2pp = all(c["delta_vs_1hop_baseline_pp"] < 2.0 for c in configs)

if not criteria["n_evaluable_primary_ge30"]:
    status = "pending"; reason = f"M1 n_evaluable={n_evaluable_primary} < {POWER_MIN}，功效不足"
elif criteria["multi_hop_gain_ge2pp"] and criteria["unconstrained_3hop_decline_ge1pp"]:
    status = "confirmed"; reason = (f"最佳配置 hop={best_cfg['hop']}/K={best_cfg['K']}/block={best_cfg['block_diagonal']} "
        f"Δ={best_delta}pp≥2pp，且无约束 K=inf 序列 3 跳较峰值下降 {decline_at_3_pp}pp≥1pp")
elif all_below_2pp:
    status = "refuted"; reason = f"24 组配置 Δ 全部 <2pp（最大 {best_delta}pp）"
else:
    status = "pending"; reason = f"增益达标但 3 跳下降未达标（Δmax={best_delta}pp，下降={decline_at_3_pp}pp）"

# 口径诚实的附加限定（不参与机器判据，仅供 WB0 裁定）
stratumA_n = sum(1 for x in primary_set if x[3])
stratumB_n = n_evaluable_primary - stratumA_n
base_A = next(c["pr5_stratumA_gold_in_seed_M1"] for c in configs
              if c["hop"]==1 and c["K"]=="inf" and not c["block_diagonal"])
print(f"[verdict] {status}: {reason}  t={tnow()}", flush=True)

elapsed = round(time.time()-START, 2)
out = {
    "schema": "exp09_r3r_diffusion_rerun/v1",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+0800", time.localtime()),
    "instruction_id": INSTR_ID,
    "supersedes_instruction_id": "1792650492-ff2d3a",
    "idempotency_key": "WB0-INSTR-20261023-EXP-09-R3R",
    "dataset": "r2_longbench_multihop_2wikimqa (200 条 2wikimqa)",
    "graph_construction": {
        "n_entities": n_ent, "cooccur_edges": len(co_edges),
        "edge_weight": "uniform w=1（与 R3 原件同一张图、同一口径；题内 title 共现，无向）",
        "topk_ranking": "top-K 门控按共现次数降序、节点 id 升序 tie-break（次数仅用于门控选择，不入边权）",
        "dead_end": "无保留邻居→uniform teleport 1/N（R3 原件口径）",
        "community_method": "networkx louvain_communities(resolution=1.0, seed=20261022)，确定性",
        "n_communities": len(comms), "community_sizes_top10": comm_sizes[:10],
        "n_raw_dead_end_nodes": n_dead_end_raw,
        "n_dead_end_after_blockdiag": {f"K={K},block=True": T_cache[(K,True)][1] for K in KS},
    },
    "eval_protocol": {
        "M1_primary": {
            "n_total": n_total, "n_gold_title": n_gold_title, "n_gold_in_seed": n_gold_in_seed,
            "n_evaluable": n_evaluable_primary,
            "rule": "不排除 seed；gold 按答案别名归一化匹配全局 title 节点即计入；命中=gold 进入 PPR top-5",
            "participates_in_criteria": True,
            "stratum_A_gold_in_seed_n": stratumA_n, "stratum_B_gold_not_in_seed_n": stratumB_n,
            "stratum_A_note": ("stratum A 的 gold 本身是 seed，tele 直接赋分 0.85/|seeds|，"
                               "在 top-5 中具结构性自命中优势，属天花板效应，判别力受限（如实披露）"),
            "pr5_step_pp": round(100.0/n_evaluable_primary, 3)},
        "M2_legacy": {
            "rule": "维持 exclude_seeds_from_ranking=True",
            "participates_in_criteria": False,
            "on_M1_set_n": n_evaluable_primary, "on_original_set_n": len(legacy_set),
            "note": "仅作口径敏感性披露；on_original_set_n 口径与 R3 原件 pr5_evaluable 逐字对齐，用于可比性自检"},
        "seeds": "题面提及且为全局 title 节点（R3 原件同口径）",
        "gold": "answers 归一化后匹配全局 title 节点（R3 原件同口径）",
        "ppr": "p_t=(1-d)*T^T p_{t-1}+d*tele, d=0.85, 迭代数=hop（k 跳截断）",
        "ranking": "np.argsort(-p, kind='stable')",
        "seed": SEED,
    },
    "random_baseline_pr5_M1": round(rand_pr5,4) if rand_pr5 is not None else None,
    "random_baseline_note": "M1 口径：全 1404 节点候选，每查询 200 次随机抽 5，均值",
    "baseline_1hop_unconstrained_pr5_M1": base_pr5,
    "configs": configs,
    "per_query": per_query,
    "unconstrained_series_pr5_M1": unc,
    "unconstrained_3hop_decline_pp": decline_at_3_pp,
    "inflections": inflections,
    "block_diagonal_effect_by_hop": block_effect,
    "stratum_readout": {
        "stratum_A_n": stratumA_n, "stratum_B_n": stratumB_n,
        "stratum_A_baseline_1hop_unconstrained_pr5": base_A,
        "note": "stratum B（真正需要检索的题）仅 n=%d，其点估计步长 50pp，任何 ≤50pp 的组内差异在该层不可分辨" % stratumB_n},
    "criteria_check": criteria,
    "status": status,
    "verdict_reason": reason,
    "best_config": {"hop": best_cfg["hop"], "K": best_cfg["K"], "block_diagonal": best_cfg["block_diagonal"],
                    "pr5_primary_M1": best_cfg["pr5_primary_M1"], "delta_pp": best_delta},
    "compatibility_with_original": compat,
    "dependency_present": [DS, ORIG_JSON],
    "elapsed_s": elapsed,
    "self_sha256": hashlib.sha256(json.dumps(
        {"cfg":[(c["hop"],str(c["K"]),c["block_diagonal"],c["pr5_primary_M1"]) for c in configs],
         "unc":unc,"rand":round(rand_pr5,4) if rand_pr5 is not None else None,"s":status},
        sort_keys=True).encode()).hexdigest()[:16],
}
with open(OUT,"w",encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"[write] {OUT}  status={status}  elapsed={elapsed}s  t={tnow()}", flush=True)
