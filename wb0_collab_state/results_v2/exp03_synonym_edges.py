#!/usr/bin/env python3.11
# exp03_synonym_edges.py  —  WB0-INSTR-20261022-EXP-03
# Synonym Edges 增强（HippoRAG 式）对多跳检索的影响
# 纯 CPU；真实数据 r2_longbench_multihop_2wikimqa.json（200 条 2wikimqa / 1404 实体 / 7475 边）；
# embedding = BGE-small-en-v1.5（Qdrant ONNX，CLS pooling，无 query prefix，全 title 一致处理）。
import json, re, time, hashlib, random
from collections import defaultdict
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

SEED = 20261022
random.seed(SEED)
START = time.time()
def tnow(): return round(time.time()-START, 2)

# ---------------- 1. 载入真实数据 ----------------
DS = "r2_longbench_multihop_2wikimqa.json"
records = [json.loads(l) for l in open(DS, encoding="utf-8").read().splitlines() if l.strip()]
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

# ---------------- 2. 共现图（题内 title 共现）baseline ----------------
co_edges = set()
freq = defaultdict(int)   # 跨题出现次数（频次基线）
for q_t in per_q_titles:
    for ti in q_t: freq[ti]+=1
    lst = list(q_t)
    for a in range(len(lst)):
        for b in range(a+1,len(lst)):
            u,v = (lst[a],lst[b]) if lst[a]<lst[b] else (lst[b],lst[a])
            co_edges.add((u,v))
print(f"[cooccur] edges={len(co_edges)}  t={tnow()}", flush=True)

# ---------------- 3. BGE 语义 embedding（指令优先项） ----------------
emb_t0 = time.time()
tok = Tokenizer.from_file("bge_onnx/tokenizer.json")
sess = ort.InferenceSession("bge_onnx/model_optimized.onnx", providers=["CPUExecutionProvider"])
def embed(text):
    enc = tok.encode(text)
    ids = enc.ids
    if ids[0] != 101: ids = [101]+ids   # [CLS]
    ids = ids+[102]                      # [SEP]
    n = len(ids)
    res = sess.run(None, {"input_ids":[ids],"attention_mask":[[1]*n],"token_type_ids":[[0]*n]})
    v = res[0][0,0,:]                     # CLS pooling
    return v/np.linalg.norm(v)
E = np.array([embed(t) for t in titles], dtype=np.float32)   # (n_ent,384) normalized
print(f"[embed] {E.shape} in {round(time.time()-emb_t0,1)}s  t={tnow()}", flush=True)

# ---------------- 4. synonym edges（cos>tau） ----------------
Taus = [0.6, 0.7, 0.8]
S = E @ E.T                          # cosine sim matrix
syn_counts = {}
syn_edges_by_tau = {}
for tau in Taus:
    mask = S > tau
    pairs = np.argwhere(np.triu(mask,1))
    edges = []
    for i,j in pairs:
        edges.append((int(i),int(j),float(S[i,j])))
    syn_edges_by_tau[tau] = edges
    syn_counts[tau] = len(edges)
    print(f"[synonym] tau={tau}: {len(edges)} edges  t={tnow()}", flush=True)

# ---------------- 5. 个性化 PageRank 检索 ----------------
def build_transition(extra_edges):
    # co-occurrence (w=1) + synonym edges (w=cos) -> row-normalized transition
    W = defaultdict(float)
    for u,v in co_edges:
        W[(u,v)] += 1.0; W[(v,u)] += 1.0
    for u,v,w in extra_edges:
        W[(u,v)] += w; W[(v,u)] += w
    deg = defaultdict(float)
    for (a,b),w in W.items(): deg[a]+=w
    N = n_ent
    T = np.zeros((N,N), dtype=np.float64)
    for (a,b),w in W.items():
        if deg[a] > 0: T[a,b] = w/deg[a]
    # dead-end -> teleport uniform
    for a in range(N):
        if deg[a] == 0: T[a,:] = 1.0/N
    return T

def ppr(T, seed_set, damping=0.85, iters=50):
    N = n_ent
    tele = np.zeros(N); 
    for s in seed_set: tele[s]+=1
    if tele.sum()==0: return np.zeros(N)
    tele = tele/tele.sum()
    p = tele.copy()
    for _ in range(iters):
        p = (1-damping)*(T.T @ p) + damping*tele
    return p

# baseline transition
T0 = build_transition([])
T_aug = {tau: build_transition(syn_edges_by_tau[tau]) for tau in Taus}

# seed/gold mapping（全局 title 图，标准 HippoRAG 多跳设定）
def find_seeds(q):
    # 题面提及、且为全局 title 节点的实体 -> 作为 PPR seed
    return [ti for ti in range(n_ent) if titles[ti] and titles[ti] in per_q_text[q]]
def find_gold(q):
    # 答案实体须为全局 title 节点（2wikimqa 答案为 passage title）
    golds = [norm(a) for a in records[q].get("answers",[])]
    return [ti for ti in range(n_ent) if titles[ti] in golds]

# frequency layers
freqs = np.array([freq[i] for i in range(n_ent)])
q1, q2 = np.percentile(freqs[freqs>0], [33.3,66.7]) if (freqs>0).any() else (1,2)
def layer(i):
    f = freq[i]
    if f <= q1: return "low"
    if f <= q2: return "mid"
    return "high"

# ---------------- 6. 评估 ----------------
evaluable = []
n_total = len(records)
n_gold_title = 0
n_gold_in_seed = 0
for q in range(len(records)):
    seeds = find_seeds(q)
    gold = find_gold(q)
    if not seeds: continue
    if not gold: continue
    n_gold_title += 1
    g = gold[0]
    if g in seeds:
        n_gold_in_seed += 1
        continue   # 退化：gold 即 seed（答案实体已在题面共现），PPR 无法检索
    evaluable.append((q, seeds, g))

print(f"[eval] n_total={n_total} n_gold_title={n_gold_title} n_gold_in_seed={n_gold_in_seed} n_evaluable={len(evaluable)}  t={tnow()}", flush=True)

def pr_at_5(T, seeds, gold):
    p = ppr(T, seeds)
    order = sorted(range(n_ent), key=lambda i: -p[i])
    top5 = [i for i in order if i not in seeds][:5]
    hit = 1 if gold in top5 else 0
    rank = order.index(gold) if gold in order else (n_ent+1)
    return hit, (1 if rank<=5 else 0)

results = {"baseline":[], "tau":{}}
layer_gain = {tau:{"low":[],"mid":[],"high":[]} for tau in Taus}
agg = {}
# baseline
base_hits = []
for q,seeds,g in evaluable:
    h,_ = pr_at_5(T0, seeds, g)
    base_hits.append(h)
    results["baseline"].append({"q":q,"hit":h,"gold_layer":layer(g)})
base_pr5 = float(np.mean(base_hits))
print(f"[baseline] PR@5={base_pr5:.4f} (n={len(base_hits)})  t={tnow()}", flush=True)

for tau in Taus:
    aug_hits = []
    for q,seeds,g in evaluable:
        h,_ = pr_at_5(T_aug[tau], seeds, g)
        aug_hits.append(h)
        gain = h - results["baseline"][len(aug_hits)-1]["hit"]
        results["tau"].setdefault(tau, {"per_q":[]})
        results["tau"][tau]["per_q"].append({"q":q,"hit":h,"gold_layer":layer(g)})
        layer_gain[tau][layer(g)].append(gain)
    aug_pr5 = float(np.mean(aug_hits))
    delta = (aug_pr5 - base_pr5)*100
    # stratified gain
    str_lg = {}
    for L in ["low","mid","high"]:
        arr = layer_gain[tau][L]
        str_lg[L] = {"n":len(arr), "mean_gain_pp": round(float(np.mean(arr))*100,3) if arr else None}
    agg[tau] = {"aug_pr5":round(aug_pr5,4), "delta_pp":round(delta,3),
                "syn_edges":syn_counts[tau], "stratified_gain_pp":str_lg}
    print(f"[tau={tau}] aug PR@5={aug_pr5:.4f} delta={delta:+.3f}pp  stratified={str_lg}  t={tnow()}", flush=True)

# ---------------- 7. 判定（D1 三分） ----------------
def judge():
    # 总增益（取最佳 tau）
    best = max(agg, key=lambda t: agg[t]["delta_pp"])
    best_delta = agg[best]["delta_pp"]
    lg = agg[best]["stratified_gain_pp"]
    low_g = lg["low"]["mean_gain_pp"] if lg["low"]["mean_gain_pp"] is not None else 0.0
    high_g = lg["high"]["mean_gain_pp"] if lg["high"]["mean_gain_pp"] is not None else 0.0
    ratio = (low_g/high_g) if high_g != 0 else (1.0 if low_g>0 else 0.0)
    # D1：n 过小 -> 不可判，强行 pending，禁止把点估计当 confirmed
    POWER_MIN = 20
    if len(evaluable) < POWER_MIN:
        verdict = "pending"
        reason = (f"有效多跳可评样本 n={len(evaluable)}（200 题中 {n_gold_title} 题 gold 为全局 title、"
                  f"其中 {n_gold_in_seed} 题答案实体已在题面共现→PPR seed 退化，仅 {len(evaluable)} 题构成有效多跳评测），"
                  f"功效不足（<{POWER_MIN}）无法达判据；点估计 +{best_delta:.0f}pp 不可作为 confirmed，按 D1 判 pending。"
                  f"低频层增益 {low_g:.1f}pp vs 高频层 n=0（无法检验低频≥50%·高频）。")
    elif best_delta >= 2.0 and low_g >= 0.5*high_g:
        verdict = "confirmed"
        reason = f"最佳 tau={best} 时 PR@5 +{best_delta:.2f}pp(≥2pp) 且低频增益 {low_g:.2f}pp ≥ 高频 {high_g:.2f}pp 的 50%（比={ratio:.2f}）；n={len(evaluable)}"
    elif best_delta < 2.0:
        verdict = "refuted"
        reason = f"最大增益仅 +{best_delta:.2f}pp(<2pp 判据)，n={len(evaluable)}"
    else:
        verdict = "pending"
        reason = f"总增益 +{best_delta:.2f}pp 但增益集中于高频层（低频/高频比={ratio:.2f}<0.5），不能排除纯频次效应；n={len(evaluable)}"
    return verdict, reason, best, best_delta, low_g, high_g, ratio

verdict, reason, best, best_delta, low_g, high_g, ratio = judge()
print(f"[verdict] {verdict}: {reason}  t={tnow()}", flush=True)

elapsed = round(time.time()-START, 2)
# ---------------- 8. 输出 JSON ----------------
out = {
    "schema": "exp03_synonym_edges/v1",
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+0800", time.localtime()),
    "dataset": "r2_longbench_multihop_2wikimqa (200 条 2wikimqa)",
    "params": {
        "n_entities": n_ent, "cooccur_edges": len(co_edges),
        "embedding": "BGE-small-en-v1.5 (Qdrant ONNX, CLS pooling, no query prefix)",
        "embedding_source": "HuggingFace Qdrant/bge-small-en-v1.5-onnx-Q (direct download; SSL via system CA)",
        "taus": Taus, "ppr_damping": 0.85, "ppr_iters": 50,
        "freq_quantile_split": [round(float(q1),3), round(float(q2),3)],
        "seed": SEED,
    },
    "n_total": n_total,
    "n_gold_title": n_gold_title,
    "n_gold_in_seed": n_gold_in_seed,
    "n_evaluable": len(evaluable),
    "baseline_pr5": round(base_pr5,4),
    "synonym_edge_counts": syn_counts,
    "per_tau": agg,
    "verdict": verdict,
    "verdict_reason": reason,
    "elapsed_s": elapsed,
    "self_sha256": hashlib.sha256(
        json.dumps({"b":round(base_pr5,4),"agg":agg,"v":verdict}, sort_keys=True).encode()
    ).hexdigest()[:16],
}
with open("results_v2/exp03_synonym_edges.json","w",encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"[write] results_v2/exp03_synonym_edges.json  elapsed={elapsed}s  t={tnow()}", flush=True)
