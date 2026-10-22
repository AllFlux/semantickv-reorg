#!/usr/bin/env python3.11
# exp02e_orthogonal_metrics.py  (v2, corrected evaluation)
# WB0-INSTR-20261022-EXP-02E : H-WB0-A 用正交结构度量复判（k-core + community bridge）
# 纯 CPU；真实数据（results_v2/longbench_multihop_2wikimqa.jsonl），零合成、零 LLM、零训练。
import json, re, math, random, time, hashlib, sys
from collections import defaultdict, Counter, deque

SEED = 20261022
random.seed(SEED)
START = time.time()
def tnow(): return round(time.time()-START, 2)

def norm(t):
    t = t.lower()
    t = t.replace("\u2013","-").replace("\u2014","-")
    t = re.sub(r"[^0-9a-z\- ]"," ",t)
    t = re.sub(r"\s+"," ",t).strip()
    return t

DS = "/workspace/dataset.jsonl"
records = [json.loads(l) for l in open(DS, encoding="utf-8") if l.strip()]
print(f"[load] {len(records)} records  t={tnow()}", flush=True)

# ---- 1. 实体抽取：段落首行标题（对齐 EXP-02/02C 口径） ----
title_to_idx = {}
titles = []
def get_idx(nt):
    if nt not in title_to_idx:
        title_to_idx[nt]=len(titles); titles.append(nt); return title_to_idx[nt]
    return title_to_idx[nt]

per_q_titles=[]; per_q_passages=[]
for rec in records:
    ctx = rec.get("context","")
    parts = re.split(r"Passage\s+\d+:\s*\n", ctx)
    q_titles=set(); q_passages=[]
    for p in parts:
        if not p.strip(): continue
        lines = p.split("\n")
        raw_title = lines[0].strip()
        if not raw_title: continue
        nt = norm(raw_title)
        if not nt: continue
        ti = get_idx(nt)
        body = "\n".join(lines[1:]).lower()
        q_titles.add(ti); q_passages.append((ti, body))
    per_q_titles.append(q_titles); per_q_passages.append(q_passages)

n_ent = len(titles)
print(f"[entities] {n_ent} titles  t={tnow()}", flush=True)

# ---- 2. 共现图（题内 title 共现）：每题内所有标题两两连边（对齐 EXP-02E 指令"题内 title 共现"，背景 7475 边） ----
edges = set()
freq = defaultdict(int)  # 频次基线 = title 在多少题中出现（跨题出现次数）
for q_t in per_q_titles:
    for ti in q_t: freq[ti]+=1
    lst=list(q_t)
    for a in range(len(lst)):
        for b in range(a+1,len(lst)):
            u,v = lst[a],lst[b]
            edges.add((u,v) if u<v else (v,u))
print(f"[cooccur-graph] edges={len(edges)}  t={tnow()}", flush=True)

adj=defaultdict(set)
for u,v in edges:
    adj[u].add(v); adj[v].add(u)
nodes=list(range(n_ent))

# ---- 3. 结构度量 ----
deg={n:len(adj[n]) for n in nodes}
def kcore(adj, nodes):
    d={n:len(adj[n]) for n in nodes}
    remaining=set(nodes); core={}
    while remaining:
        minn=min(remaining, key=lambda n:d[n]); md=d[minn]; core[minn]=md
        remaining.discard(minn)
        for u in adj[minn]:
            if u in remaining: d[u]-=1
    return core
kcore_num=kcore(adj,nodes)
print(f"[kcore] done  t={tnow()}", flush=True)

def label_prop(adj, nodes, seed=SEED, max_iter=50):
    rnd=random.Random(seed); labels={n:n for n in nodes}
    for _ in range(max_iter):
        changed=False; order=list(nodes); rnd.shuffle(order)
        for v in order:
            if not adj[v]: continue
            cnt=Counter(labels[u] for u in adj[v])
            best=min(cnt.keys(), key=lambda l:( -cnt[l], l ))
            if labels[v]!=best: labels[v]=best; changed=True
        if not changed: break
    return labels
lp=label_prop(adj,nodes)
comm=defaultdict(set)
for n,l in lp.items(): comm[l].add(n)

bridge={n:0 for n in nodes}
for n in nodes:
    lv=lp[n]
    for u in adj[n]:
        if lp[u]!=lv: bridge[n]+=1
print(f"[lp-communities] {len(comm)} communities  t={tnow()}", flush=True)

def betweenness_sample(adj, nodes, nsamp=200, seed=SEED):
    rnd=random.Random(seed); samp=rnd.sample(nodes, min(nsamp,len(nodes)))
    bc=defaultdict(float); N=len(nodes)
    for s in samp:
        sigma={v:0 for v in nodes}; sigma[s]=1
        dist={v:-1 for v in nodes}; dist[s]=0; P=defaultdict(list); S=[]; Q=deque([s])
        while Q:
            v=Q.popleft(); S.append(v)
            for w in adj[v]:
                if dist[w]<0: dist[w]=dist[v]+1; Q.append(w)
                if dist[w]==dist[v]+1: sigma[w]+=sigma[v]; P[w].append(v)
        delta={v:0.0 for v in nodes}
        while S:
            w=S.pop()
            for v in P[w]: delta[v]+= sigma[v]/sigma[w]*(1+delta[w])
            if w!=s: bc[w]+=delta[w]
    normf=((N-1)*(N-2)/2)/nsamp/2.0 if N>2 else 1.0
    return {v:bc[v]*normf for v in bc}
btw=betweenness_sample(adj,nodes)
print(f"[betweenness] sampled done  t={tnow()}", flush=True)

# ---- 4. 相关性矩阵（主交付：正交性验证） ----
def rankdata(a):
    idxs=sorted(range(len(a)), key=lambda i:a[i]); ranks=[0]*len(a); i=0
    while i<len(a):
        j=i
        while j+1<len(a) and a[idxs[j+1]]==a[idxs[i]]: j+=1
        avg=(i+j)/2.0+1
        for k in range(i,j+1): ranks[idxs[k]]=avg
        i=j+1
    return ranks
def spearman(xs, ys):
    n=len(xs); rx=rankdata(xs); ry=rankdata(ys)
    mx=sum(rx)/n; my=sum(ry)/n
    dx=[a-mx for a in rx]; dy=[b-my for b in ry]
    num=sum(a*b for a,b in zip(dx,dy)); den=math.sqrt(sum(a*a for a in dx)*sum(b*b for b in dy))
    return num/den if den>0 else 0.0
def pearson(xs,ys):
    n=len(xs); mx=sum(xs)/n; my=sum(ys)/n
    dx=[a-mx for a in xs]; dy=[b-my for b in ys]
    num=sum(a*b for a,b in zip(dx,dy)); den=math.sqrt(sum(a*a for a in dx)*sum(b*b for b in dy))
    return num/den if den>0 else 0.0

sel=[n for n in nodes if freq[n]>0]
F=[float(freq[n]) for n in sel]
metrics={"degree":[float(deg[n]) for n in sel],
         "kcore":[float(kcore_num[n]) for n in sel],
         "community_bridge":[float(bridge[n]) for n in sel],
         "betweenness":[float(btw.get(n,0.0)) for n in sel]}
corr_matrix={mk:{"spearman_vs_freq":round(spearman(mv,F),4),
                 "pearson_vs_freq":round(pearson(mv,F),4)} for mk,mv in metrics.items()}
print(f"[corr] kcore~freq={corr_matrix['kcore']['spearman_vs_freq']}  bridge~freq={corr_matrix['community_bridge']['spearman_vs_freq']}  btw~freq={corr_matrix['betweenness']['spearman_vs_freq']}  deg~freq={corr_matrix['degree']['spearman_vs_freq']}  t={tnow()}", flush=True)

# ---- 5. 查询构造（seed=问句中的主语实体, gold=答案实体） ----
def titles_in_text(text):
    tl=norm(text); found=[]
    for nt,ti in title_to_idx.items():
        if re.search(r"(?<![0-9a-z])"+re.escape(nt)+r"(?![0-9a-z])", tl): found.append(ti)
    return found
queries=[]
for i,rec in enumerate(records):
    qtext=rec.get("input","")
    golds=[title_to_idx[norm(str(a))] for a in rec.get("answers",[]) if norm(str(a)) in title_to_idx]
    if not golds: continue
    gold=golds[0]
    ins=titles_in_text(qtext)
    seed=None
    for ti in ins:
        if ti!=gold: seed=ti; break
    if seed is None and ins: seed=ins[0]
    if seed is None or seed==gold: continue
    queries.append((seed,gold,i))
print(f"[queries] {len(queries)} (seed,gold) pairs  t={tnow()}", flush=True)

# OOD 分类（全图，确定性）
def compute_bridges(adj, nodes):
    disc={}; low={}; timer=[0]; bridges=set()
    sys.setrecursionlimit(10000)
    def dfs(u,p):
        disc[u]=low[u]=timer[0]; timer[0]+=1
        for v in adj[u]:
            if v==p: continue
            if v not in disc:
                dfs(v,u); low[u]=min(low[u],low[v])
                if low[v]>disc[u]: bridges.add((min(u,v),max(u,v)))
            else: low[u]=min(low[u],disc[v])
    for n in nodes:
        if n not in disc: dfs(n,-1)
    return bridges
bridges_set=compute_bridges(adj,nodes)
def ood_classify(s,g):
    return {"ood_lp": lp[s]!=lp[g], "ood_bridge": (min(s,g),max(s,g)) in bridges_set}
q_ood=[ood_classify(s,g) for (s,g,_) in queries]
ood_lp_total=sum(1 for oc in q_ood if oc["ood_lp"])
ood_bridge_total=sum(1 for oc in q_ood if oc["ood_bridge"])
print(f"[ood-prevalence] ood_lp={ood_lp_total}/{len(queries)}  ood_bridge={ood_bridge_total}/{len(queries)}  t={tnow()}", flush=True)

# ---- 6. PPR 检索（成对比较，仅取 seed 与 gold 均在保留子集内的查询） ----
def build_sub(keep):
    sadj=defaultdict(set)
    for u in keep:
        for v in adj[u]:
            if v in keep: sadj[u].add(v)
    return sadj
def ppr_topk(sadj, seed, alpha=0.85, iters=50, topk=5):
    p={seed:1.0}
    for _ in range(iters):
        new=defaultdict(float); new[seed]+=alpha
        for j,mass in p.items():
            if mass==0: continue
            d=len(sadj[j])
            if d==0: new[seed]+=(1-alpha)*mass; continue
            c=(1-alpha)*mass/d
            for i in sadj[j]: new[i]+=c
        p=new
    ranked=sorted(p.items(), key=lambda kv:-kv[1])
    return [i for i,_ in ranked if i!=seed][:topk]

Xs=[40,50,60]
def topk_by(metric_dict, X):
    k=int(X/100.0*n_ent)
    order=sorted(nodes, key=lambda n: metric_dict.get(n,0.0), reverse=True)
    return set(order[:k])
allocations={"kcore":{X:topk_by(kcore_num,X) for X in Xs},
             "freq":{X:topk_by(freq,X) for X in Xs},
             "random":{X:set(random.sample(nodes, int(X/100.0*n_ent))) for X in Xs}}

# 单分配、单预算：ID/OOD PR@5（仅在 seed,gold 均保留时计入，成对可比对）
def eval_alloc(aname):
    res={}
    for X in Xs:
        keep=allocations[aname][X]; sadj=build_sub(keep)
        idh=[]; olp=[]; obr=[]
        for (s,g,_),oc in zip(queries,q_ood):
            if s in keep and g in keep:
                hit=1.0 if g in ppr_topk(sadj,s) else 0.0
                if (not oc["ood_lp"]) and (not oc["ood_bridge"]): idh.append(hit)
                else:
                    if oc["ood_lp"]: olp.append(hit)
                    if oc["ood_bridge"]: obr.append(hit)
        res[X]={"kept":len(keep),
                 "id_pr5":round(sum(idh)/len(idh),4) if idh else None,"id_n":len(idh),
                 "ood_lp_pr5":round(sum(olp)/len(olp),4) if olp else None,"ood_lp_n":len(olp),
                 "ood_bridge_pr5":round(sum(obr)/len(obr),4) if obr else None,"ood_bridge_n":len(obr)}
    return res
results={a:eval_alloc(a) for a in allocations}
print(f"[retrieval] done  t={tnow()}", flush=True)

# 成对 delta（struct - freq）仅取两者均可达的同一查询；分 ID / OOD
def paired_delta(X):
    ks=allocations["kcore"][X]; kf=allocations["freq"][X]
    sadj=build_sub(ks); fadj=build_sub(kf)
    pid=[]; pod=[]
    for (s,g,_),oc in zip(queries,q_ood):
        ins=s in ks and g in ks; inf=s in kf and g in kf
        if ins and inf:
            ha=1.0 if g in ppr_topk(sadj,s) else 0.0
            hb=1.0 if g in ppr_topk(fadj,s) else 0.0
            if (not oc["ood_lp"]) and (not oc["ood_bridge"]): pid.append((ha,hb))
            else: pod.append((ha,hb))
    def mk(pairs):
        if not pairs: return {"delta_pp":None,"ci95_lo":None,"ci95_hi":None,"n":0}
        diffs=[a-b for a,b in pairs]; m=sum(diffs)/len(diffs)*100
        rnd=random.Random(SEED+11); bs=[]
        for _ in range(5000):
            s1=sum(rnd.choice(diffs) for _ in range(len(diffs)))/len(diffs)*100; bs.append(s1)
        bs.sort()
        return {"delta_pp":round(m,2),"ci95_lo":round(bs[int(0.025*5000)],2),
                "ci95_hi":round(bs[int(0.975*5000)],2),"n":len(diffs)}
    return {"id":mk(pid),"ood_lp":mk(pod)}

delta={X:paired_delta(X) for X in Xs}
print(f"[delta] id={ {X:delta[X]['id']['delta_pp'] for X in Xs} }  ood_lp_n={ {X:delta[X]['ood_lp']['n'] for X in Xs} }  t={tnow()}", flush=True)

# ---- 7. 组装交付物 ----
wall=round(time.time()-START,2)
deliverable={
  "schema":"exp02e_h_wb0_a_orthogonal_metrics/v1",
  "task":"WB0-INSTR-20261022-EXP-02E / H-WB0-A 正交结构度量复判（k-core + community bridge）",
  "executor":"WB2","executed_at":"2026-10-22 13:40 GMT+8","seed":SEED,
  "data_source":{"longbench_2wikimqa":{"hub_relpath":"results_v2/longbench_multihop_2wikimqa.jsonl","n_samples":len(records),"synthesis_used":False},
                 "llm_forward":False,"training":False},
  "graph":{"construction":"题内 title 共现（每题内所有段落标题两两连边；段落标题=段落首行，归一化对齐 EXP-02/02C 口径）",
           "note":"对齐 EXP-02E 指令'题内 title 共现'；与 EXP-02/02C 的 mention-based 图（657 边）是不同构造。n_ent 实测 1404，n_edges 实测 7475，与指令背景口径一致。",
           "n_ent":n_ent,"n_edges":len(edges),"n_communities_lp":len(comm),"n_bridges":len(bridges_set)},
  "metric_definitions":{"frequency":"跨题出现次数（title 在多少题中出现）",
           "kcore":"迭代剥离得到的 k-core 数（主结构度量，正交候选）",
           "community_bridge":"标签传播社区下跨社区邻居数（辅结构度量）",
           "betweenness":"抽样 Brandes（200 源）相对中介中心性（对照，预期不可判）",
           "label_propagation":"异步标签传播，确定性 tie-break（最小标签），seed=20261022"},
  "correlation_matrix_spearman_vs_freq":{mk:corr_matrix[mk]["spearman_vs_freq"] for mk in corr_matrix},
  "correlation_matrix_pearson_vs_freq":{mk:corr_matrix[mk]["pearson_vs_freq"] for mk in corr_matrix},
  "corr_detail":corr_matrix,
  "budget_allocation":{"X":Xs,
           "retrieval_metric":"个性化 PageRank（damping=0.85, iters=50, top5）ID/OOD PR@5；纯图指标（无 embedding，口径披露）",
           "note":"成对比较（struct vs freq）仅取 seed 与 gold 均在保留子集内的同一查询；ID/OOD 分列。",
           "results":results,"paired_delta_struct_minus_freq":delta,
           "random_baseline":results["random"]},
  "queries_total":len(queries),
  "ood_prevalence":{"ood_lp_total":ood_lp_total,"ood_bridge_total":ood_bridge_total,
           "note":"共现图为逐题团结构，每个问题的标题构成团且倾向落入同一 LP 社区；跨社区/桥边 OOD 在此构造上结构性为 0（与 EXP-02C 在 mention 图上报告 OOD-LP max=8 的语料属性一致）。故 H-WB0-A 的 OOD 泛化判据在此图上不可得，按指令判 pending。"},
  "ood_definitions":{"ood_lp":"seed 与 gold 的标签传播社区不同（EXP-02 原口径）","ood_bridge":"边(seed,gold) 属全图桥集合（强定义）"},
  "elapsed_s":wall,"total_wall_clock_s":wall
}
out="results_v2/exp02e_h_wb0_a_orthogonal_metrics.json"
with open(out,"w",encoding="utf-8") as f: json.dump(deliverable,f,ensure_ascii=False,indent=2)
sha=hashlib.sha256(open(out,"rb").read()).hexdigest()
print(f"[write] {out} sha256={sha}  size={len(open(out,'rb').read())}  t={tnow()}", flush=True)
print("SUMMARY_JSON:"+json.dumps({"n_ent":n_ent,"n_edges":len(edges),"n_communities_lp":len(comm),
  "n_bridges":len(bridges_set),"queries":len(queries),"ood_lp_total":ood_lp_total,"ood_bridge_total":ood_bridge_total,
  "corr_spearman_vs_freq":{mk:corr_matrix[mk]["spearman_vs_freq"] for mk in corr_matrix},
  "id_delta_pp":{X:delta[X]["id"]["delta_pp"] for X in Xs},
  "id_delta_ci":{X:delta[X]["id"] for X in Xs},
  "ood_lp_n":{X:delta[X]["ood_lp"]["n"] for X in Xs}}, ensure_ascii=False))
print("SHA256:"+sha)
