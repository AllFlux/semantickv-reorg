#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_notebook.py — 按 DS-20261018-011-WB1 更新 run_on_kaggle.ipynb：
1. Step 1 baseline gate 判定改为 ≥2/3 needle 命中（DS-011 前置门禁口径）；
2. 新增 Step 3 cell：Router 空壳冒烟（延迟/参数量/KV 占比）+ CUDA Graph
   capture 探测，存证 results/kaggle_7b_vllm_plugin_bench.json；
3. 解读指南补 H-DS15/H-DS17 行。
"""
import json
from pathlib import Path

NB = Path(__file__).resolve().parent.parent / "run_on_kaggle.ipynb"
nb = json.loads(NB.read_text(encoding="utf-8"))

# ---------- 1. Step 1 gate: ≥2/3 ----------
OLD_GATE = '''if all(base_hits):
    print("\\n>>> 基线门通过：原始模型三个位置全部 HIT，继续 Step 2 压缩验证。")
else:
    print("\\n>>> 基线门未通过：原始模型存在 MISS。")
    print(">>> 说明该模型/该上下文下 needle 测试无区分度，请换更大模型或缩短 ctx 后重新 Run All。")
    import sys as _sys; _sys.exit("baseline gate failed") if False else None  # 仅提示，不强制中断'''

NEW_GATE = '''# DS-20261018-011 前置门禁：7B 无压缩基线 needle 命中 ≥ 2/3 方有验证资格
n_base_hit = sum(1 for h in base_hits if h)
if n_base_hit >= 2:
    print(f"\\n>>> 前置门禁通过（≥2/3 口径）：{n_base_hit}/3 HIT，继续 Step 2/3。")
else:
    print(f"\\n>>> 前置门禁未通过（需 ≥2/3，实得 {n_base_hit}/3）。")
    print(">>> 原始模型 needle 检索能力不足，请换更大模型（Settings 改模型名）")
    print(">>> 或缩短 ctx 后重新 Run All。")'''

hit = False
for c in nb["cells"]:
    if c["cell_type"] != "code":
        continue
    src = "".join(c["source"])
    if OLD_GATE in src:
        c["source"] = [l + "\n" for l in src.replace(OLD_GATE, NEW_GATE).split("\n")[:-1]]
        hit = True
        break
assert hit, "未找到 Step1 gate 代码块"

# ---------- 2. 新增 Step 3 cell（插在 Step 2 cell 之后） ----------
STEP3 = '''# ============ 6. Step 3：Router 空壳 + 插件冒烟（H-DS15 / H-DS17 · DS-20261018-011） ============
# 挂随机初始化的 DS 规格 Router 空壳（learned_router.SemanticRouter，纯 Linear+ReLU），
# 验证四件事（DS-011 验收标准）：
#   ① Router 单次前向延迟（通过线 <0.5ms；证伪线 >2ms）
#   ② 参数量 <50M
#   ③ 权重占 7B KV cache 比例 <10%
#   ④ CUDA Graph capture 不被破坏（算子级 smoke；插件级见 vllm 分支）
# 存证：results/kaggle_7b_vllm_plugin_bench.json
import json, time
import torch
import torch.nn as nn
from src.semantickv.vllm_plugin.learned_router import LearnedSemanticRouter

Q_DIM = {"Qwen/Qwen2.5-7B-Instruct": 3584, "Qwen/Qwen2.5-1.5B-Instruct": 1536,
         "Qwen/Qwen2.5-0.5B": 896}.get(MODEL, 1024)
router = LearnedSemanticRouter(q_dim=Q_DIM, hidden_dim=256, num_groups=64).to(DEV).eval()
n_params = router.num_parameters()

@torch.no_grad()
def _lat(n_tok, reps=200):
    x = torch.randn(1, n_tok, Q_DIM, device=DEV)
    for _ in range(20):
        router(x)
    if DEV == "cuda":
        torch.cuda.synchronize()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        router(x)
        if DEV == "cuda":
            torch.cuda.synchronize()
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return {"p50_ms": round(ts[len(ts)//2], 4),
            "p99_ms": round(ts[min(len(ts)-1, int(len(ts)*0.99))], 4)}

lat_n1, lat_n2048 = _lat(1), _lat(2048)
weight_mb = n_params * router.net[0].weight.element_size() / 1024**2
kv_mb = 28 * 2 * 8 * 128 * 2 * 4096 / 1024**2   # 7B/28层/8KV头/hd128/fp16 KV/ctx4096

# ---- ④ CUDA Graph capture 探测（算子级 smoke） ----
cuda_graph = {"status": "skipped", "reason": "cpu 环境"}
if DEV == "cuda":
    try:
        g = torch.cuda.CUDAGraph()
        x = torch.randn(1, 1, Q_DIM, device=DEV)
        s = torch.cuda.Stream()
        s.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(s):
            for _ in range(3):
                router(x)
        torch.cuda.current_stream().wait_stream(s)
        with torch.cuda.graph(g):
            y_cap = router(x)
        g.replay()
        torch.cuda.synchronize()
        same = bool(torch.allclose(y_cap, router(x), atol=1e-5))
        cuda_graph = {"status": "captured" if same else "mismatch",
                      "replay_matches_eager": same}
    except Exception as e:               # noqa: BLE001
        cuda_graph = {"status": "failed", "error": str(e)}

# ---- 插件级（vLLM）探测：可用则测 enforce_eager=False，不可用记 pending ----
vllm_status = "pending（Kaggle 上 pip install vllm 后按 vllm_plugin/README 挂载点实测）"
try:
    import vllm
    vllm_status = f"vllm {vllm.__version__} 可用——按 README 挂载 SemanticRouter 后重跑 decode bench"
except Exception:                        # noqa: BLE001
    pass

bench = {
    "config": {"device": DEV, "dtype": str(next(router.parameters()).dtype),
               "model": MODEL, "q_dim": Q_DIM, "ctx": CTX,
               "purpose": "H-DS15/H-DS17 Router 空壳 + 插件冒烟（DS-20261018-011-WB1）"},
    "environment": {"kind": "real-model-arch-smoke",
                    "note": "Router 为随机初始化空壳；主模型未加载（算子级冒烟）"},
    "router": {"arch": "ds011_linear_relu_linear", "params": n_params,
               "params_under_50m": n_params < 50_000_000,
               "latency_single_query_N1": lat_n1,
               "latency_prefill_N2048": lat_n2048,
               "pass_line_0p5ms": lat_n1["p99_ms"] < 0.5,
               "fail_line_2ms": lat_n1["p99_ms"] > 2.0,
               "weight_mb": round(weight_mb, 4),
               "kv_cache_mb_7b_ctx4096_fp16": round(kv_mb, 2),
               "kv_mem_pct": round(100.0 * weight_mb / kv_mb, 6),
               "kv_mem_fail_10pct": 100.0 * weight_mb / kv_mb > 10.0},
    "cuda_graph": cuda_graph,
    "vllm_plugin": vllm_status,
    "summary": {
        "params_under_50m": n_params < 50_000_000,
        "kv_mem_under_10pct": 100.0 * weight_mb / kv_mb <= 10.0,
        "latency_pass_0p5ms": lat_n1["p99_ms"] < 0.5,
        "cuda_graph_ok": cuda_graph.get("status") == "captured",
    },
}
dest = os.path.join(REPO, "results", "kaggle_7b_vllm_plugin_bench.json")
os.makedirs(os.path.dirname(dest), exist_ok=True)
with open(dest, "w", encoding="utf-8") as f:
    json.dump(bench, f, ensure_ascii=False, indent=2)

print("===== Step 3：Router 空壳 + 插件冒烟 =====")
print(f"Router: q_dim={Q_DIM} params={n_params:,} (<50M: {bench['summary']['params_under_50m']})")
print(f"单 query 前向: p50={lat_n1['p50_ms']}ms p99={lat_n1['p99_ms']}ms "
      f"(通过线 0.5ms: {bench['summary']['latency_pass_0p5ms']})")
print(f"整段 N=2048 前向: p50={lat_n2048['p50_ms']}ms")
print(f"权重 {weight_mb:.3f}MB vs 7B KV cache {kv_mb:.0f}MB -> "
      f"{bench['router']['kv_mem_pct']}% (证伪线 10%)")
print(f"CUDA Graph: {cuda_graph}")
print(f"vLLM 插件: {vllm_status}")
print(f"存证: {dest}")'''

step3_cell = {"cell_type": "code", "execution_count": None, "metadata": {},
              "outputs": [], "source": [l + "\n" for l in STEP3.split("\n")[:-1]]}

# 插到 Step 2 cell（含 "Step 2：V1.5 全量验证"）之后
idx = None
for i, c in enumerate(nb["cells"]):
    if c["cell_type"] == "code" and "Step 2：V1.5 全量验证" in "".join(c["source"]):
        idx = i + 1
        break
assert idx is not None, "未找到 Step 2 cell"
nb["cells"].insert(idx, step3_cell)

# ---------- 3. 解读指南补行 ----------
GUIDE_OLD = "| ARI_topic | >0.5 好 | GSC 在真实隐状态上恢复话题结构的程度（合成数据是 1.0） |"
GUIDE_NEW = (GUIDE_OLD + "\n"
             "| Step 3 Router 延迟 | p99 <0.5ms（>2ms 证伪） | H-DS17：DS 规格空壳单 query 前向；CPU 参考值以 GPU 为准 |\n"
             "| Step 3 参数量/KV占比 | <50M / <10% | H-DS17 硬件无关项，CPU 即可终判 |\n"
             "| Step 3 CUDA Graph | captured | H-DS15：Router 空壳不破坏 graph capture 方可继续插件路线 |")
for c in nb["cells"]:
    if c["cell_type"] != "code":
        continue
    src = "".join(c["source"])
    if GUIDE_OLD in src:
        c["source"] = [l + "\n" for l in src.replace(GUIDE_OLD, GUIDE_NEW).split("\n")[:-1]]
        break

NB.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
print("notebook 已更新: Step1 gate(≥2/3) + Step3 Router cell + 指南补行")
import subprocess
r = subprocess.run(["python3", "-c",
                    "import json;nb=json.load(open('run_on_kaggle.ipynb'));"
                    "print('cells:',len(nb['cells']));"
                    "[print(' code' if c['cell_type']=='code' else ' md  ',"
                    "''.join(c['source'])[:60].replace(chr(10),' ')) for c in nb['cells']]"],
                   capture_output=True, text=True)
print(r.stdout, r.stderr)
