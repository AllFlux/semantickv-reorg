#!/usr/bin/env python3.11
# exp06_curriculum.py — WB0-INSTR-20261022-EXP-06：语义 Router 课程学习（递增 vs 随机 vs 递减）
# 纯 CPU；真实数据（Hub 实拉）；零合成、零 LLM 前向、零主干加载（仅 Router 前向 + 线性探针）。
#
# 协议（operationalization，回执已显式披露）：
#   样本流 = 60 实体 embedding（Hub r2_entity_embeddings / entity_embeddings.npz meta 定序）。
#   课程顺序（主）：难度 = 该 Router 自身逐样本预测熵 H(p_i)（自定进度）；并列按 1e-6 取整后稳定排序
#     （17/18 Router 熵梯度≈0 → 顺序退化为索引序，已检测并披露）。
#   课程顺序（敏感性）：难度 = 图 degree（高频=易，方向为披露性假设；5 个不入图实体按 deg=0）。
#   指标① 码本利用率 exp(H)：课程前缀 m 样本池化码分布有效码数，util=exp(H)/K（WB1 口径）。
#   指标② 下游 OOD：线性探针（多项逻辑回归）在课程流上训练；输入=码分布 p（主）/隐层 h 逐特征 z-score（敏感性）；
#     模式=online 单遍 SGD（顺序敏感，主）/ batch 前缀子集全批 GD（子集敏感，敏感性）；3 种子/臂。
#     任务=LP 社区预测（主，实体级 60/40 留出，同类两 split 可见）/ k-core 层级（次）。
#   判据（指令 §3 预注册）：confirmed ⟺ 递增在 OOD（≥2pp 宏acc，配对 bootstrap CI95 下界>0，同时胜随机与递减）
#     或 exp(H)（≥2pp util，主预算 m=24）占优；refuted ⟺ 两臂均无显著差异；否则 pending（D1：测不出≠已证伪）。
import json, re, time, hashlib, io, zipfile, pickle, os, zlib
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
import numpy as np
from collections import defaultdict, Counter

SEED = 20261022
START = time.time()
def tnow(): return round(time.time()-START, 2)
def log(*a): print(f"[{tnow():7.2f}s]", *a, flush=True)

BUDGETS = [12, 24, 36, 48, 60]
ARM_SEEDS = [11, 22, 33]
LR_ONLINE, LR_BATCH, E_BATCH = 0.5, 0.5, 200
INIT_SCALE = 0.01
BOOT = 10000
TIE_EPS = 1e-6   # 难度并列取整精度（披露）

# ---------------- 1. embeddings ----------------
z = np.load("entity_embeddings.npz", allow_pickle=True)
ents60 = [str(e) for e in z["entities"]]
X = np.ascontiguousarray(z["hidden_states"], dtype="<f4")
emb_sha = hashlib.sha256(X.tobytes()).hexdigest()
assert emb_sha.startswith("d1cf27753f274405"), "emb sha mismatch"
log("embeddings", X.shape, "sha16", emb_sha[:16])

# ---------------- 2. 共现图（EXP-02E 口径） ----------------
def norm(t):
    t = t.lower().replace("\u2013","-").replace("\u2014","-")
    t = re.sub(r"[^0-9a-z\- ]"," ",t)
    return re.sub(r"\s+"," ",t).strip()

records = [json.loads(l) for l in open("/workspace/dataset.jsonl", encoding="utf-8") if l.strip()]
title_to_idx = {}; titles = []
def get_idx(nt):
    if nt not in title_to_idx:
        title_to_idx[nt] = len(titles); titles.append(nt); return title_to_idx[nt]
    return title_to_idx[nt]
per_q_titles = []
for rec in records:
    qt = set()
    for p in re.split(r"Passage\s+\d+:\s*\n", rec.get("context","")):
        if not p.strip(): continue
        rt = p.split("\n")[0].strip()
        if rt:
            nt = norm(rt)
            if nt: qt.add(get_idx(nt))
    per_q_titles.append(qt)
n_ent = len(titles)
edges = set(); freq = defaultdict(int)
for qt in per_q_titles:
    for ti in qt: freq[ti] += 1
    lst = list(qt)
    for a in range(len(lst)):
        for b in range(a+1, len(lst)):
            u, v = lst[a], lst[b]
            edges.add((u,v) if u<v else (v,u))
adj = defaultdict(set)
for u,v in edges: adj[u].add(v); adj[v].add(u)
nodes = list(range(n_ent)); deg = {n: len(adj[n]) for n in nodes}

def kcore(adj, nodes):
    d = {n: len(adj[n]) for n in nodes}; remaining = set(nodes); core = {}
    while remaining:
        mn = min(remaining, key=lambda n: d[n]); core[mn] = d[mn]; remaining.discard(mn)
        for u in adj[mn]:
            if u in remaining: d[u] -= 1
    return core
kcore_num = kcore(adj, nodes)

def label_prop(adj, nodes, seed=SEED, max_iter=50):
    import random
    rnd = random.Random(seed); labels = {n: n for n in nodes}
    for _ in range(max_iter):
        changed = False; order = list(nodes); rnd.shuffle(order)
        for v in order:
            if not adj[v]: continue
            cnt = Counter(labels[u] for u in adj[v])
            best = min(cnt.keys(), key=lambda l: (-cnt[l], l))
            if labels[v] != best: labels[v] = best; changed = True
        if not changed: break
    return labels
lp = label_prop(adj, nodes)
log(f"graph: {n_ent} ent / {len(edges)} edges / {len(set(lp.values()))} LP comms")

g = {}
for i, e in enumerate(norm(x) for x in ents60):
    ti = title_to_idx.get(e)
    g[i] = dict(in_big=ti is not None, deg=deg[ti] if ti is not None else None,
                freq=freq[ti] if ti is not None else None,
                kcore=kcore_num[ti] if ti is not None else None,
                comm=lp[ti] if ti is not None else None)
in_big = [i for i in range(60) if g[i]["in_big"]]
deg_of = {i: (g[i]["deg"] if g[i]["deg"] is not None else 0) for i in range(60)}  # 不入图按 0（披露）
log(f"join: {len(in_big)}/60 in big graph")

# ---------------- 3. 任务 ----------------
def make_task(selector, n_classes, name):
    bycls = defaultdict(list)
    for i in in_big:
        c = selector(i)
        if c is not None: bycls[c].append(i)
    classes = sorted(c for c, v in bycls.items() if len(v) >= 2)[:n_classes]
    rng = np.random.default_rng(SEED)
    train, ood = [], []
    for c in classes:
        idx = np.array(bycls[c]); rng.shuffle(idx)
        k = max(1, int(round(len(idx)*0.6)))
        train += idx[:k].tolist(); ood += idx[k:].tolist()
    train.sort(); ood.sort()
    log(f"task {name}: {len(classes)} classes, train={len(train)} ood={len(ood)}")
    return dict(name=name, classes=classes, train=train, ood=ood,
                y={i: classes.index(selector(i)) for i in train+ood})

comm_sizes = Counter(g[i]["comm"] for i in in_big)
task_comm = make_task(lambda i: g[i]["comm"] if comm_sizes[g[i]["comm"]] >= 3 else None, 6, "lp_community")
task_kcore = make_task(lambda i: g[i]["kcore"] if g[i]["kcore"] >= 1 else None, 9, "kcore_tier")

# ---------------- 4. Router 加载与前向 ----------------
class _U(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "torch._utils" and name == "_rebuild_tensor_v2":
            def rb(storage, offset, size, stride, rg, hooks, meta=None):
                return dict(key=storage[2], size=tuple(size), stride=tuple(stride))
            return rb
        if module == "torch" and name == "FloatStorage":
            class _S:
                def __init__(self, *a): pass
                def __setstate__(self, s): pass
            return _S
        return super().find_class(module, name)
    def persistent_load(self, pid):
        return pid

def load_router(fn):
    zf = zipfile.ZipFile(fn); pre = fn[:-3]
    data = _U(io.BytesIO(zf.read(f"{pre}/data.pkl"))).load()
    sd = {}
    for k, info in data["state_dict"].items():
        raw = zf.read(f"{pre}/data/{info['key']}")
        sd[k] = np.frombuffer(raw, dtype="<f4").reshape(info["size"])
    return dict(W1=sd["net.0.weight"], b1=sd["net.0.bias"], W2=sd["net.2.weight"], b2=sd["net.2.bias"],
                K=sd["net.2.weight"].shape[0], arch=data.get("arch"), method=data.get("method"), seed=data.get("seed"))

CONFIGS = []
for k in [8, 16, 64]:
    for m in ["differentiable_pr", "reinforce_ema"]:
        for p in ["raw", "zscore"]:
            CONFIGS.append((f"b_router_K{k}_{m}_{p}.pt", k, m, p))
        CONFIGS.append((f"b_router_K{k}_{m}.pt", k, m, "none"))

mu = X.astype(np.float64).mean(axis=0, keepdims=True)
sd_ = X.astype(np.float64).std(axis=0, keepdims=True)
sd_safe = np.where(sd_ > 1e-8, sd_, 1.0)
X_feat_z = ((X.astype(np.float64) - mu) / sd_safe).astype(np.float32)

def preprocess(mode):
    return X if mode in ("raw", "none") else X_feat_z

def forward(rt, x):
    h = np.maximum(x @ rt["W1"].T + rt["b1"], 0)
    logits = h @ rt["W2"].T + rt["b2"]
    logits = logits - logits.max(axis=1, keepdims=True)
    e = np.exp(logits.astype(np.float64))
    p = e / e.sum(axis=1, keepdims=True)
    return h.astype(np.float64), p

wb1 = json.load(open("r2_router_collapse.json"))["per_router"]
xcheck = []
log("loading 18 routers + forward ...")
ROUTERS = {}
for fn, k, m, p in CONFIGS:
    rt = load_router(fn)
    h, pdist = forward(rt, preprocess(p))
    ROUTERS[f"{m}|{p}|K{k}"] = dict(fn=fn, K=k, method=m, preproc=p, h=h, p=pdist)
    H_i = -(pdist * np.log(np.clip(pdist, 1e-12, None))).sum(axis=1)
    rec = wb1.get(f"{m}|{p}|K{k}")
    if rec:
        xcheck.append(dict(router=f"{m}|{p}|K{k}", expH_mine=float(np.exp(H_i.mean())), expH_wb1=rec["effective_codes_expH"],
                           util_mine=float(np.exp(H_i.mean())/k), util_wb1=rec["utilization_vs_K"],
                           top1_mine=float(pdist.max(axis=1).mean()), top1_wb1=rec["mean_top1_prob"]))
max_dev = max(max(abs(c["expH_mine"]-c["expH_wb1"]), abs(c["top1_mine"]-c["top1_wb1"])) for c in xcheck)
log(f"forward xcheck vs WB1: n={len(xcheck)} max|dev|={max_dev:.2e}")

# ---------------- 5. 探针工具 ----------------
def softmax_fit(Xm, W, b):
    zz = Xm @ W.T + b
    zz = zz - zz.max(axis=1, keepdims=True)
    e = np.exp(zz); return e / e.sum(axis=1, keepdims=True)

def train_batch(Xs, ys, C, D, seed):
    rng = np.random.default_rng(seed)
    W = rng.normal(0, INIT_SCALE, (C, D)); b = np.zeros(C)
    Y = np.eye(C)[ys]; m = len(ys)
    for _ in range(E_BATCH):
        P = softmax_fit(Xs, W, b)
        Gr = (P - Y) / m
        W -= LR_BATCH * (Gr.T @ Xs); b -= LR_BATCH * Gr.sum(axis=0)
    return W, b

def macro_acc(W, b, Xe, ye, n_classes):
    if len(ye) == 0: return float("nan"), float("nan")
    pred = (Xe @ W.T + b).argmax(axis=1)
    accs = [float((pred[ye == ci] == ci).mean()) for ci in range(n_classes) if (ye == ci).sum() > 0]
    macro = float(np.mean(accs)) if accs else float("nan")
    return macro, float((pred == ye).mean())

def probe_features(router, variant):
    if variant == "p": return router["p"]
    h = router["h"]
    s = np.where(h.std(axis=0, keepdims=True) > 1e-8, h.std(axis=0, keepdims=True), 1.0)
    return (h - h.mean(axis=0, keepdims=True)) / s

# ---------------- 6. 课程臂构建 ----------------
def build_arms(diff_vec, increasing_first=True):
    """diff_vec: 60 维难度（大=易）。返回 inc/dec/rnd 五臂（inc/dec 各重复 3 次以变探针初始化）。"""
    d = np.round(np.asarray(diff_vec, dtype=np.float64), 6)   # 并列取整（披露）
    if increasing_first:   # 易在前：难度降序
        inc = list(np.argsort(-d, kind="stable")); dec = list(np.argsort(d, kind="stable"))
    else:                  # diff 已按"大=难"给（如熵）：易在前=升序
        inc = list(np.argsort(d, kind="stable")); dec = list(np.argsort(-d, kind="stable"))
    rnds = [list(np.random.default_rng(s).permutation(60)) for s in ARM_SEEDS]
    return {"inc": [inc]*3, "dec": [dec]*3, "rnd": rnds}, (inc, dec, rnds)

def expH_traj(p, K, uniq_orders):
    traj = {}
    for arm, si, order in uniq_orders:
        for m in BUDGETS:
            pb = p[order[:m]].mean(axis=0)
            Hb = -(pb * np.log(np.clip(pb, 1e-12, None))).sum()
            traj[f"{arm}|{si}|{m}"] = dict(expH_pooled=float(np.exp(Hb)), util=float(np.exp(Hb)/K))
    return traj

def run_probes(router, key, arms):
    K = router["K"]
    res = {}
    for task in (task_comm, task_kcore):
        tname = task["name"]; C = len(task["classes"])
        train, ood = task["train"], task["ood"]; train_set = set(train)
        Xte_y = np.array([task["y"][i] for i in ood])
        for variant in ("p", "h"):
            F = probe_features(router, variant); D = F.shape[1]
            Fe_ood = F[ood]
            for mode in ("online", "batch"):
                for arm, orders in arms.items():
                    for si, order in enumerate(orders):
                        seed = ARM_SEEDS[si] + zlib.crc32(f"{key}|{tname}|{variant}|{mode}".encode()) % 1000
                        rng = np.random.default_rng(seed)
                        W = rng.normal(0, INIT_SCALE, (C, D)); b = np.zeros(C)
                        seen = []
                        ckpt = {m: None for m in BUDGETS}
                        pos_in_order = {i: pos for pos, i in enumerate(order)}
                        for pos, i in enumerate(order, start=1):
                            if mode == "online" and i in train_set:
                                xi = F[i]; yi = task["y"][i]
                                zz = W @ xi + b; zz = zz - zz.max()
                                pr = np.exp(zz); pr /= pr.sum()
                                gr = pr.copy(); gr[yi] -= 1.0
                                W -= LR_ONLINE * np.outer(gr, xi); b -= LR_ONLINE * gr
                                seen.append(i)
                            if pos in ckpt:
                                if mode == "online":
                                    Wc, bc = W.copy(), b.copy()
                                else:
                                    sub = [j for j in train if pos_in_order[j] < pos]
                                    if not sub:
                                        ckpt[pos] = dict(macro=float("nan"), micro=float("nan"), n=0); continue
                                    Wc, bc = train_batch(F[sub], np.array([task["y"][j] for j in sub]), C, D, seed)
                                ma, mi = macro_acc(Wc, bc, Fe_ood, Xte_y, C)
                                ckpt[pos] = dict(macro=ma, micro=mi, n=len(seen) if mode == "online" else len(sub))
                        for m in BUDGETS:
                            res[f"{tname}|{variant}|{mode}|{arm}|{si}|{m}"] = ckpt[m]
    return res

def run_difficulty(dname, diff_getter, increasing_first):
    """对 18 Router 跑全网格；返回 per-router 结果。"""
    out = {}
    for key, router in ROUTERS.items():
        p = router["p"]; K = router["K"]
        H_i = -(p * np.log(np.clip(p, 1e-12, None))).sum(axis=1)
        diff = np.array([diff_getter(i, H_i) for i in range(60)], dtype=np.float64)
        arms, (inc, dec, rnds) = build_arms(diff, increasing_first)
        uniq = [("inc", 0, inc), ("dec", 0, dec)] + [("rnd", si, o) for si, o in enumerate(rnds)]
        traj = expH_traj(p, K, uniq)
        probes = run_probes(router, key, arms)
        out[key] = dict(traj=traj, probes=probes, diff_std=float(diff.std()),
                        degenerate=bool(diff.std() < TIE_EPS),
                        inc_eq_dec=bool(inc == dec))
    return out

log("difficulty A (primary): per-router predictive entropy ...")
RES_ENT = run_difficulty("entropy", lambda i, H: H[i], increasing_first=False)
log("difficulty B (sensitivity): graph degree (high=easy, disclosed assumption) ...")
RES_DEG = run_difficulty("degree", lambda i, H: deg_of[i], increasing_first=True)

# ---------------- 7. 聚合 + 判据 ----------------
def arm_mean(R, k, tname, variant, mode, arm, m, field="macro"):
    vals = [R[k]["probes"][f"{tname}|{variant}|{mode}|{arm}|{si}|{m}"][field] for si in range(3)]
    return float(np.nanmean(vals))

def paired(keys, R, fa, fb):
    d = []
    for k in keys:
        a, b = fa(k), fb(k)
        if not (np.isnan(a) or np.isnan(b)): d.append(a - b)
    return np.array(d)

def boot_ci(d, n=BOOT):
    if len(d) == 0: return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(d), size=(n, len(d)))
    means = d[idx].mean(axis=1)
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))

def eval_ood(R, keys, tname, variant, mode, m):
    d1 = paired(keys, R, lambda k: arm_mean(R, k, tname, variant, mode, "inc", m),
                       lambda k: arm_mean(R, k, tname, variant, mode, "rnd", m))
    d2 = paired(keys, R, lambda k: arm_mean(R, k, tname, variant, mode, "inc", m),
                       lambda k: arm_mean(R, k, tname, variant, mode, "dec", m))
    m1, lo1, hi1 = boot_ci(d1); m2, lo2, hi2 = boot_ci(d2)
    return dict(delta_inc_minus_rnd=dict(mean=m1, ci95=[lo1, hi1], n=len(d1)),
                delta_inc_minus_dec=dict(mean=m2, ci95=[lo2, hi2], n=len(d2)))

def eval_expH(R, keys, m, metric="util"):
    d1 = paired(keys, R, lambda k: R[k]["traj"][f"inc|0|{m}"][metric],
                       lambda k: float(np.mean([R[k]["traj"][f"rnd|{si}|{m}"][metric] for si in range(3)])))
    d2 = paired(keys, R, lambda k: R[k]["traj"][f"inc|0|{m}"][metric],
                       lambda k: R[k]["traj"][f"dec|0|{m}"][metric])
    m1, lo1, hi1 = boot_ci(d1); m2, lo2, hi2 = boot_ci(d2)
    return dict(delta_inc_minus_rnd=dict(mean=m1, ci95=[lo1, hi1], n=len(d1)),
                delta_inc_minus_dec=dict(mean=m2, ci95=[lo2, hi2], n=len(d2)))

keys_all = list(ROUTERS.keys())
P = dict(task="lp_community", variant="p", mode="online", m=60)   # 预注册主设置

ood_primary = eval_ood(RES_ENT, keys_all, P["task"], P["variant"], P["mode"], P["m"])
expH_primary = eval_expH(RES_ENT, keys_all, 24)
expH_curve = {m: eval_expH(RES_ENT, keys_all, m) for m in BUDGETS}
ood_curve = {m: eval_ood(RES_ENT, keys_all, P["task"], P["variant"], P["mode"], m) for m in BUDGETS}
# 敏感性设置（全部记录，供回执照会）
sens = {}
for tname in ("lp_community", "kcore_tier"):
    for variant in ("p", "h"):
        for mode in ("online", "batch"):
            sens[f"{tname}|{variant}|{mode}"] = {m: eval_ood(RES_ENT, keys_all, tname, variant, mode, m) for m in BUDGETS}
deg_arm = dict(
    expH_curve={m: eval_expH(RES_DEG, keys_all, m) for m in BUDGETS},
    ood_primary=eval_ood(RES_DEG, keys_all, P["task"], P["variant"], P["mode"], P["m"]),
    ood_curve={m: eval_ood(RES_DEG, keys_all, P["task"], P["variant"], P["mode"], m) for m in BUDGETS},
    degenerate_routers=[k for k in keys_all if RES_DEG[k]["degenerate"]])

def conf(r):
    a, b = r["delta_inc_minus_rnd"], r["delta_inc_minus_dec"]
    return a["mean"] >= 0.02 and a["ci95"][0] > 0 and b["mean"] >= 0.02 and b["ci95"][0] > 0
def refu(r):
    a, b = r["delta_inc_minus_rnd"], r["delta_inc_minus_dec"]
    return (abs(a["mean"]) < 0.02 and a["ci95"][0] <= 0 <= a["ci95"][1]
            and abs(b["mean"]) < 0.02 and b["ci95"][0] <= 0 <= b["ci95"][1])

c_ood, r_ood = conf(ood_primary), refu(ood_primary)
c_exp, r_exp = conf(expH_primary), refu(expH_primary)

# 全域显著性扫描：任一设置（含敏感性网格/两难度臂/全部预算）出现 CI95 排除 0 的 delta
def any_sig():
    hits = []
    pools = [("primary_ood_m60", ood_primary), ("primary_expH_m24", expH_primary)]
    for m in BUDGETS:
        pools.append((f"ood_curve_m{m}", ood_curve[m])); pools.append((f"expH_curve_m{m}", expH_curve[m]))
        pools.append((f"deg_expH_m{m}", deg_arm["expH_curve"][m])); pools.append((f"deg_ood_m{m}", deg_arm["ood_curve"][m]))
    for gname, g in sens.items():
        for m, r in g.items(): pools.append((f"{gname}_m{m}", r))
    for name, r in pools:
        for arm in ("delta_inc_minus_rnd", "delta_inc_minus_dec"):
            lo, hi = r[arm]["ci95"]
            if not (np.isnan(lo) or np.isnan(hi)) and (lo > 0 or hi < 0):
                hits.append(dict(setting=name, arm=arm, mean=r[arm]["mean"], ci95=[lo, hi]))
    return hits
sig_hits = any_sig()
log(f"significant deltas anywhere: {len(sig_hits)}")
for h in sig_hits[:14]: log("   SIG", h["setting"], h["arm"], f"{h['mean']:+.4f}", h["ci95"])

# D1 裁定的 refuted 需"无显著影响"为事实；敏感性存在显著效应（方向相反/幅值不足）时不得写 refuted
verdict = "confirmed" if (c_ood or c_exp) else ("refuted" if (r_ood and r_exp and not sig_hits) else "pending")
log(f"VERDICT={verdict}  ood_conf={c_ood} ood_ref={r_ood} expH_conf={c_exp} expH_ref={r_exp} sig_hits={len(sig_hits)}")

# 对照
inv60 = all(abs(RES_ENT[k]["traj"]["inc|0|60"]["expH_pooled"] - RES_ENT[k]["traj"]["dec|0|60"]["expH_pooled"]) < 1e-12 for k in keys_all)
chance = {t["name"]: 1.0/len(t["classes"]) for t in (task_comm, task_kcore)}
maj = {}
for t in (task_comm, task_kcore):
    top = Counter(t["y"][i] for i in t["train"]).most_common(1)[0][0]
    maj[t["name"]] = float(np.mean([1.0 if t["y"][i] == top else 0.0 for i in t["ood"]]))
n_deg_ent = sum(1 for k in keys_all if RES_ENT[k]["degenerate"])

per_router = {}
for k in keys_all:
    r = RES_ENT[k]
    per_router[k] = dict(K=ROUTERS[k]["K"], method=ROUTERS[k]["method"], preproc=ROUTERS[k]["preproc"],
        file=ROUTERS[k]["fn"], H_std=r["diff_std"], degenerate=r["degenerate"], inc_eq_dec=r["inc_eq_dec"],
        expH_full=r["traj"]["inc|0|60"]["expH_pooled"], util_full=r["traj"]["inc|0|60"]["util"],
        util_m12=dict(inc=r["traj"]["inc|0|12"]["util"], dec=r["traj"]["dec|0|12"]["util"],
                      rnd=float(np.mean([r["traj"][f"rnd|{si}|12"]["util"] for si in range(3)]))),
        ood_m60=dict(inc=arm_mean(RES_ENT, k, "lp_community", "p", "online", "inc", 60),
                     dec=arm_mean(RES_ENT, k, "lp_community", "p", "online", "dec", 60),
                     rnd=arm_mean(RES_ENT, k, "lp_community", "p", "online", "rnd", 60)),
        ood_m60_h=dict(inc=arm_mean(RES_ENT, k, "lp_community", "h", "online", "inc", 60),
                       dec=arm_mean(RES_ENT, k, "lp_community", "h", "online", "dec", 60),
                       rnd=arm_mean(RES_ENT, k, "lp_community", "h", "online", "rnd", 60)))

out = dict(
    id="WB2-20261022-EXP-06", instruction="WB0-INSTR-20261022-EXP-06", instruction_id="1792650493-7cd4cc",
    generated_at=time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime()),
    wall_clock_s=round(time.time()-START, 2), seed=SEED,
    inputs=dict(
        embeddings=dict(file="Hub r2_entity_embeddings / entity_embeddings.npz", shape=[60,2048],
                        sha256_prefix16=emb_sha[:16], declared="d1cf27753f274405", match=True),
        routers=dict(n=18, source="Hub b_router_K{8,16,64}_{differentiable_pr,reinforce_ema}[_{raw,zscore}].pt",
                     path_note="指令标注 r2_b_routers/b_router_* 实测 404；实体在 Hub 顶层，与 WB1 r2_router_collapse.json inputs.routers.source 同源；非缺失依赖，未发 open_item"),
        graph=dict(source="/workspace/dataset.jsonl（200 cond 2wikimqa）", n_ent=n_ent, n_edges=len(edges),
                   n_lp_comms=len(set(lp.values())), join_60=f"{len(in_big)}/60",
                   口径="题内 title 共现，复用 EXP-02E 构造")),
    method=dict(
        difficulty_primary="per-router per-sample predictive entropy H(p_i), ties rounded at 1e-6, stable sort",
        difficulty_sensitivity="graph degree (high=easy, disclosed direction assumption; 5 non-graph entities deg=0)",
        curricula=dict(increasing="easy-first", decreasing="hard-first", random=f"3 seeded shuffles {ARM_SEEDS}"),
        budgets=BUDGETS, tie_eps=TIE_EPS,
        expH_metric="pooled effective codes exp(H_bar) over curriculum prefix; util=exp(H)/K (WB1 口径)",
        probe="multinomial logistic regression; input p (K-dim, primary) / h z-scored 256-dim (sensitivity); online single-pass SGD lr=0.5 (order-sensitive, primary) / batch prefix full-batch GD E=200 lr=0.5 (subset-sensitive); 3 seeds/arm; init N(0,0.01)",
        tasks=dict(lp_community=dict(classes=len(task_comm["classes"]), train=len(task_comm["train"]), ood=len(task_comm["ood"]),
                                     split="entity-level 60/40 stratified, same classes in both splits"),
                   kcore_tier=dict(classes=len(task_kcore["classes"]), train=len(task_kcore["train"]), ood=len(task_kcore["ood"]))),
        preproc="WB1 口径: raw/none→raw input; zscore→per-feature dataset z-score (60-entity stats, unsupervised)",
        determinism="OMP/MKL=1; fp32 forward, f64 entropy; all rng seeded; crc32-derived probe seeds"),
    controls=dict(
        wb1_forward_xcheck=dict(n=len(xcheck), max_abs_dev=float(max_dev), detail=xcheck),
        expH_m60_order_invariance=bool(inv60),
        chance_level=chance, majority_baseline_ood=maj,
        entropy_degenerate_routers=f"{n_deg_ent}/18",
        degree_degenerate_routers=f"{len(deg_arm['degenerate_routers'])}/18"),
    criteria=dict(
        ood_primary=dict(setting=P, result=ood_primary,
                         rule="confirmed ⟺ inc beats rnd AND dec by >=2pp macro-acc with bootstrap CI95 lo>0; refuted ⟺ both |d|<2pp with CI covering 0"),
        expH_primary=dict(setting=dict(metric="util", m=24), result=expH_primary,
                          rule="confirmed ⟺ inc util beats rnd AND dec by >=2pp with CI95 lo>0"),
        expH_curve=expH_curve, ood_curve=ood_curve,
        sensitivity_grid=sens, difficulty_degree_arm=deg_arm),
    verdict=verdict,
    significant_deltas=sig_hits,
    per_router=per_router,
    synthesis=dict(
        power_note="n=60 embeddings：指令边界即机制观察；OOD 为实体级留出（同类可见），社区级未见类 OOD 对闭集分类器不可计算（已披露）。",
        pseudo_replication_note=f"主难度（Router 熵）下 {n_deg_ent}/18 Router 无逐样本梯度（H_std<1e-6）→ inc/dec 顺序退化为索引序/逆序，跨 Router 的课程子集构成共享 → 配对 bootstrap 为伪重复（有效 n≈1-2），CI 反保守；degree 难度臂子集构成对全部 18 Router 完全相同，伪重复更甚。exp(H) 早期预算的显著负向 delta 因此不可解释为课程顺序效应。",
        verdict_reason=("pending（D1）：① confirmed 判据（递增同时胜随机与递减且 >=2pp、CI95 下界>0）在任何设置均未满足——主设置 m=60 OOD d(inc-rnd)=+1.0pp（CI 跨 0）/ d(inc-dec)=+0.6pp，expH m=24 |d|<0.3pp；"
                        "② 主难度下 17/18 Router 无难度梯度使 inc≈dec 结构性拉平 inc-dec 对比，confirmed 判据在本批 Router 上近乎不可执行（前提缺失）；"
                        "③ 敏感性存在显著但方向相反/幅值远不足的效应（社区×h×online m=60：d(inc-rnd)=-6.0pp CI[-11.2,-1.0]；kcore×p：-0.6pp；degree 臂 expH +0.5~0.8pp），故『课程顺序无显著影响』的全局陈述不成立，按 D1『测不出≠已证伪』不写 refuted；亦不把未达判据的结果写 confirmed；"
                        "④ 落入指令 §3 pending 分支（样本不足或矛盾）+ n=60 机制观察边界。")),
)
json.dump(out, open("exp06_curriculum.json", "w"), ensure_ascii=False, indent=1)
log("wrote exp06_curriculum.json", os.path.getsize("exp06_curriculum.json"), "B")

print("\n=== per-router (primary setting m=60; ood = lp_community x p x online; ood_h = x h) ===")
for k in keys_all:
    r = per_router[k]
    print(f"{k:42s} K={r['K']:2d} deg={int(r['degenerate'])} Hstd={r['H_std']:.2e} expH60={r['expH_full']:6.2f} "
          f"u12={r['util_m12']['inc']:.3f}/{r['util_m12']['dec']:.3f}/{r['util_m12']['rnd']:.3f} "
          f"ood={r['ood_m60']['inc']:.3f}/{r['ood_m60']['dec']:.3f}/{r['ood_m60']['rnd']:.3f} "
          f"ood_h={r['ood_m60_h']['inc']:.3f}/{r['ood_m60_h']['dec']:.3f}/{r['ood_m60_h']['rnd']:.3f}")
print("\n=== primary OOD (community x p x online) ===")
for m in BUDGETS:
    r = ood_curve[m]
    print(f"  m={m}: d(inc-rnd)={r['delta_inc_minus_rnd']['mean']:+.4f} CI[{r['delta_inc_minus_rnd']['ci95'][0]:+.4f},{r['delta_inc_minus_rnd']['ci95'][1]:+.4f}]  d(inc-dec)={r['delta_inc_minus_dec']['mean']:+.4f} CI[{r['delta_inc_minus_dec']['ci95'][0]:+.4f},{r['delta_inc_minus_dec']['ci95'][1]:+.4f}]")
print("=== expH util curve (entropy difficulty) ===")
for m in BUDGETS:
    r = expH_curve[m]
    print(f"  m={m}: d(inc-rnd)={r['delta_inc_minus_rnd']['mean']:+.4f} CI[{r['delta_inc_minus_rnd']['ci95'][0]:+.4f},{r['delta_inc_minus_rnd']['ci95'][1]:+.4f}]  d(inc-dec)={r['delta_inc_minus_dec']['mean']:+.4f} CI[{r['delta_inc_minus_dec']['ci95'][0]:+.4f},{r['delta_inc_minus_dec']['ci95'][1]:+.4f}]")
print("=== degree-difficulty arm: expH util curve ===")
for m in BUDGETS:
    r = deg_arm["expH_curve"][m]
    print(f"  m={m}: d(inc-rnd)={r['delta_inc_minus_rnd']['mean']:+.4f} CI[{r['delta_inc_minus_rnd']['ci95'][0]:+.4f},{r['delta_inc_minus_rnd']['ci95'][1]:+.4f}]  d(inc-dec)={r['delta_inc_minus_dec']['mean']:+.4f} CI[{r['delta_inc_minus_dec']['ci95'][0]:+.4f},{r['delta_inc_minus_dec']['ci95'][1]:+.4f}]")
print("=== sensitivity grid (community x h x online, m=60) ===", json.dumps(sens["lp_community|h|online"][60], ensure_ascii=False))
print("=== sensitivity grid (kcore x p x online, m=60) ===", json.dumps(sens["kcore_tier|p|online"][60], ensure_ascii=False))
print(f"\nVERDICT={verdict}  wall={time.time()-START:.1f}s")
