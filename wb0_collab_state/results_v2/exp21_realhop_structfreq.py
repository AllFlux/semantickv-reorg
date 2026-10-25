#!/usr/bin/env python3.11
# exp21_realhop_structfreq.py — WB0-INSTR-20261025-EXP-21-REALHOP-STRUCTFREQ
# 真实 LongBench 2WikiMultihopQA 多跳样本上的「结构臂(k-core / community_bridge) vs 频次臂(freq)」
# 第三次独立复验：连续口径 MRR + hop 分层 + 配对差。
#
# 口径来源（逐条对齐，不做新设）：
#   · 图构造 / 任务生成 / 臂分配器(keep_set: top-X%, tie-break=(-metric,id)) / PPR(d=0.85, 50 轮,
#     死点→保留集内 uniform teleport) —— 沿用 exp11_exp05cont_hop_continuous.py（其自称复用 EXP-05 冻结件
#     exp05_hop_moderation.py 的图构造 + 120 任务生成逻辑，参数零改动）；
#   · 主指标 = 连续口径 MRR = mean(1/rank_full)，rank 由全 N 节点 argsort 实算，不截断 Top-K（EXP-11 口径）。
#
# 【本轮硬约束登记】指令 §2.2 要求 gold/hop 取自样本件自身字段、不得推断填充。实测两件均无 hop 字段
# （见 asset_audit.hop_field_audit），故 hop 分层只能沿用 EXP-05/EXP-11 的 BFS 最短距离**构造** hop，
# 该口径属推断，违反 §2.2 → 主判据落 E5 分支；本脚本产出的分层对比仅作「判据外登记」，不作判据。
#
# 运行：python3.11 exp21_realhop_structfreq.py   （seed 固定，幂等可重跑；runtime 预算 300s）

import json, re, time, hashlib, math, os, sys
from collections import defaultdict, Counter
import numpy as np
import networkx as nx

START = time.time()
def tnow(): return round(time.time() - START, 2)

BASE = "/workspace/semantickv-reorg/results_v2"
DS_MAIN   = f"{BASE}/longbench_multihop_2wikimqa.jsonl"      # 200 条 2wikimqa（EXP-05/EXP-11 同一真实语料）
DS_N5     = f"{BASE}/longbench_multihop_samples_len4096_n5.jsonl"  # 10 条 len4096 子集
EXP05_JSON = f"{BASE}/exp05_hop_moderation.json"
EXP11_PY   = f"{BASE}/exp11_exp05cont_hop_continuous.py"
OUT_JSON   = f"{BASE}/exp21_realhop_structfreq.json"

SEED_TASK = 20261022          # 与 EXP-05/EXP-11 逐字一致（任务生成）
SEED_LOUVAIN = 20261022       # 同上
BOOT_SEED = 20261026          # 指令 §2.4 指定
BOOT_N = 5000
DAMP, ITERS = 0.85, 50
HOPS = [2, 3, 4]
X_LEVELS = [40, 50, 60]
N_PER_HOP = 40
X_MAIN = 50                   # 主口径 X（中位；40/60 并列披露，非事后挑选）
BUDGET = 300

def sha256_of(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()

def size_of(p):
    return os.path.getsize(p) if os.path.exists(p) else None

out = {
    "schema": "exp21_realhop_structfreq/v1",
    "instruction_id": "1792943742-7b5009",
    "idempotency_key": "WB0-INSTR-20261025-EXP-21-REALHOP-STRUCTFREQ",
    "created_at": "2026-10-26T00:15:00+0800",
    "executor": "WB1",
    "seeds": {"task": SEED_TASK, "louvain": SEED_LOUVAIN, "bootstrap": BOOT_SEED, "bootstrap_n": BOOT_N},
    "caliber": {"metric": "MRR=mean(1/rank_full) 连续口径（EXP-11）",
                "arms": ["kcore", "community_bridge", "frequency"],
                "keep_rule": "top-X% by metric, tie-break=(-metric, id)（EXP-05 原件）",
                "ppr": "d=0.85, 50 iters, dead-end->uniform teleport within keep（EXP-05 原件）"},
}

# ---------------- 0. 实物审计（指令 §2.1 逐字节记录 + §2.2 hop/gold 字段可得性） ----------------
def scan_fields(path):
    lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    keyset = Counter(); n_hop = 0; n_gold = 0; n = 0
    for l in lines:
        try: d = json.loads(l)
        except Exception: continue
        n += 1
        keyset.update(d.keys())
        low = [k.lower() for k in d.keys()]
        if any(k in low for k in ["hop", "hops", "num_hops", "n_hops", "hop_count"]): n_hop += 1
        if any(k in low for k in ["gold", "gold_idx", "gold_node", "answer_idx"]): n_gold += 1
    return {"n_records": n, "keys": sorted(keyset.keys()),
            "records_with_hop_field": n_hop,
            "records_with_gold_node_field": n_gold,
            "sample_keys": sorted(keyset.keys())[:20]}

out["asset_audit"] = {
    "required": {
        "results_v2/longbench_multihop_samples_len4096_n5.jsonl": {"expected_B": 372727, "actual_B": size_of(DS_N5)},
        "results_v2/longbench_multihop_2wikimqa.jsonl": {"expected_B": 6052108, "actual_B": size_of(DS_MAIN)},
        "results_v2/exp11_exp05cont_hop_continuous.py": {"expected_B": 25639, "actual_B": size_of(EXP11_PY)},
        "results_v2/exp05_hop_moderation.py": {"expected_B": 25830, "actual_B": size_of(f"{BASE}/exp05_hop_moderation.py"),
            "note": "Hub /api/file 按 id 取件，id 'exp05_hop_moderation' 同时绑 .json(90473B) 与 .py(25830B)，"
                    "GET 恒返回 .json；尝试 ?relpath/?path/?file/?ext/?as 与 r2_*_script 等变体均为 404 或返回 .json。"
                    "故 .py 不可经 Hub 单独取得 → 臂定义改以 exp11 脚本内自包含的同一分配器实现 + exp05 json per_query 一致性核验替代（显式披露，不静默）"},
    },
    "aux_actually_used": {
        "results_v2/exp05_hop_moderation.json": {"B": size_of(EXP05_JSON), "sha256": sha256_of(EXP05_JSON) if os.path.exists(EXP05_JSON) else None},
        "results_v2/exp11_exp05cont_hop_continuous.py": {"B": size_of(EXP11_PY), "sha256": sha256_of(EXP11_PY) if os.path.exists(EXP11_PY) else None},
        "results_v2/longbench_multihop_2wikimqa.jsonl": {"B": size_of(DS_MAIN), "sha256": sha256_of(DS_MAIN) if os.path.exists(DS_MAIN) else None},
        "results_v2/longbench_multihop_samples_len4096_n5.jsonl": {"B": size_of(DS_N5), "sha256": sha256_of(DS_N5) if os.path.exists(DS_N5) else None},
    },
    "hop_field_audit": {
        "longbench_multihop_2wikimqa.jsonl": scan_fields(DS_MAIN),
        "longbench_multihop_samples_len4096_n5.jsonl": scan_fields(DS_N5),
        "verdict": "两件均无 hop/hops/num_hops/hop_count 字段；gold 仅为自然语言答案字符串（answers / answer），"
                   "非图节点 id。故样本件自身不提供 hop 分层标注，也不提供节点级 gold。",
    },
}
print(f"[audit] done t={tnow()}", flush=True)

# ---------------- 1. 真实语料 → 实体共现图（与 EXP-11 逐字一致） ----------------
records = [json.loads(l) for l in open(DS_MAIN, encoding="utf-8").read().splitlines() if l.strip()]
assert len(records) == 200, f"expect 200 records, got {len(records)}"

def norm(t):
    t = t.lower(); t = t.replace("\u2013", "-").replace("\u2014", "-")
    t = re.sub(r"[^0-9a-z\- ]", " ", t); t = re.sub(r"\s+", " ", t).strip(); return t

title_to_idx = {}; titles = []
def get_idx(nt):
    if nt not in title_to_idx:
        title_to_idx[nt] = len(titles); titles.append(nt)
    return title_to_idx[nt]

per_q_titles = []
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
n_ent = len(titles)

co_edges = set(); freq = defaultdict(int)
for q_t in per_q_titles:
    for ti in q_t: freq[ti] += 1
    lst = list(q_t)
    for a in range(len(lst)):
        for b in range(a + 1, len(lst)):
            u, v = (lst[a], lst[b]) if lst[a] < lst[b] else (lst[b], lst[a])
            co_edges.add((u, v))

adj = defaultdict(list)
for (u, v) in co_edges:
    adj[u].append(v); adj[v].append(u)

G = nx.Graph(); G.add_nodes_from(range(n_ent)); G.add_edges_from(co_edges)
comms = nx.community.louvain_communities(G, seed=SEED_LOUVAIN, resolution=1.0)
node2comm = {}
for ci, cset in enumerate(comms):
    for u in cset: node2comm[u] = ci
kcore = nx.core_number(G)
bridge = {v: sum(1 for u in adj[v] if node2comm[u] != node2comm[v]) for v in range(n_ent)}
print(f"[graph] n_ent={n_ent} edges={len(co_edges)} comms={len(comms)} t={tnow()}", flush=True)

# ---------------- 2. 任务构造（BFS 最短距离 = hop；与 EXP-05/EXP-11 逐字一致） ----------------
# 【口径违约登记】hop 为 BFS 构造值，非样本件字段 → 仅作判据外登记（指令 §2.2 禁止推断填充）
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

rng = np.random.default_rng(SEED_TASK)
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

# 2b. 与 EXP-05 原件 per_query 逐条一致性核验
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
print(f"[task-consistency] n={task_match['n']} mismatch={task_match['mismatch']} t={tnow()}", flush=True)

# ---------------- 3. 臂分配（EXP-05 原件分配器） ----------------
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
for X in X_LEVELS:
    keeps[("frequency", X)] = keep_set(FREQ, X)

# ---------------- 4. PPR + 连续口径 MRR ----------------
def build_T(keep):
    N = n_ent
    T = np.zeros((N, N), dtype=np.float64)
    kept = sorted(keep); dead = []
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
    T, ndead = get_T(keep)
    starts = [tasks[i]["start"] for i in qidx_list]
    p = ppr_batch(T, keep, starts)
    out_ = {}
    for j, qi in enumerate(qidx_list):
        col = p[:, j]
        order = np.argsort(-col, kind="stable")
        pos = int(np.nonzero(order == tasks[qi]["gold"])[0][0])
        out_[qi] = {"rank_full": pos + 1, "gold_kept": tasks[qi]["gold"] in keep}
    return out_, ndead

configs = [(a, X) for a in ARMS for X in X_LEVELS] + [("frequency", X) for X in X_LEVELS]
res = {}; ndead_info = {}
for (name, X) in configs:
    keep = keeps[(name, X)]
    qidx = [i for i in range(len(tasks)) if tasks[i]["start"] in keep]
    r, nd = eval_for(keep, qidx)
    res[(name, X)] = r
    ndead_info[f"{name}|X{X}"] = nd
    print(f"[eval] {name} X={X}: n_ev={len(qidx)} dead={nd} t={tnow()}", flush=True)
    if tnow() > BUDGET - 60:
        out["runtime_truncated"] = True
        break

# ---------------- 5. 配对差 + bootstrap ----------------
def mrr_of(r, idxs):
    vals = [1.0 / r[qi]["rank_full"] for qi in idxs if qi in r]
    return (float(np.mean(vals)) if vals else None), len(vals)

def paired_cell(arm, X, hop):
    ka = keeps[(arm, X)]; kf = keeps[("frequency", X)]
    common = [i for i in tasks_by_hop[hop] if tasks[i]["start"] in ka and tasks[i]["start"] in kf]
    if not common: return None
    ms, ns = mrr_of(res[(arm, X)], common)
    mf, nf = mrr_of(res[("frequency", X)], common)
    if ms is None or mf is None: return None
    d = [1.0 / res[(arm, X)][i]["rank_full"] - 1.0 / res[("frequency", X)][i]["rank_full"] for i in common]
    boot = np.random.default_rng(BOOT_SEED)
    idx = boot.integers(0, len(d), size=(BOOT_N, len(d)))
    arr = np.asarray(d)[idx].mean(axis=1)
    lo, hi = np.percentile(arr, [2.5, 97.5])
    return {"n_pairs": len(common), "mrr_struct": ms, "mrr_freq": mf,
            "mean_d": float(np.mean(d)), "sd_d": float(np.std(d, ddof=1)) if len(d) > 1 else None,
            "ci95_lo": float(lo), "ci95_hi": float(hi),
            "delta_pp": float(ms - mf) * 100.0}

cells = {}
for X in X_LEVELS:
    for arm in ARMS:
        for h in HOPS:
            c = paired_cell(arm, X, h)
            if c: cells[f"{arm}|X{X}|hop{h}"] = c
    # pooled
    for arm in ARMS:
        ds = []
        for h in HOPS:
            ka = keeps[(arm, X)]; kf = keeps[("frequency", X)]
            common = [i for i in tasks_by_hop[h] if tasks[i]["start"] in ka and tasks[i]["start"] in kf]
            ds += [1.0 / res[(arm, X)][i]["rank_full"] - 1.0 / res[("frequency", X)][i]["rank_full"] for i in common]
        if ds:
            boot = np.random.default_rng(BOOT_SEED)
            idx = boot.integers(0, len(ds), size=(BOOT_N, len(ds)))
            arr = np.asarray(ds)[idx].mean(axis=1)
            lo, hi = np.percentile(arr, [2.5, 97.5])
            cells[f"{arm}|X{X}|pooled"] = {"n_pairs": len(ds), "mean_d": float(np.mean(ds)),
                "sd_d": float(np.std(ds, ddof=1)), "ci95_lo": float(lo), "ci95_hi": float(hi),
                "delta_pp": float(np.mean(ds)) * 100.0}

out["graph"] = {"n_entities": n_ent, "n_edges": len(co_edges), "n_communities": len(comms),
                "n_tasks": len(tasks), "per_hop": {h: len(tasks_by_hop[h]) for h in HOPS}}
out["consistency_vs_exp05_json"] = task_match
out["dead_nodes"] = ndead_info
out["paired_cells"] = cells
out["hop_provenance"] = {"source": "BFS 最短距离（EXP-05/EXP-11 任务构造），非样本件字段",
                         "compliant_with_instruction_2_2": False,
                         "note": "指令 §2.2 禁止推断填充 hop → 本分层仅作判据外登记，不承担主判据"}
out["runtime_sec"] = tnow()
out["runtime_truncated"] = out.get("runtime_truncated", False)

with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"[done] wrote {OUT_JSON} ({os.path.getsize(OUT_JSON)}B) t={tnow()}", flush=True)
print("[cells-main]", json.dumps({k: v for k, v in cells.items() if "X50" in k}, ensure_ascii=False)[:2000], flush=True)
