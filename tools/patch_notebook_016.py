#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_notebook_016.py — 按 DS-20261019-016-WB1 更新 run_on_kaggle.ipynb：
1. 模型选型：T4(≥14GB) → Qwen2.5-7B-Instruct + 4bit NF4/FP16 + ctx=8192（DS-016 规格）；
2. Step 1/2 命令加 --load-in-4bit；
3. 新增 Step 4 cell：WB2 Router .pt 合流（守卫式，.pt 未到不中断）；
4. 标题 md 的流程说明同步为四步。
"""
import json
from pathlib import Path

NB = Path(__file__).resolve().parent.parent / "run_on_kaggle.ipynb"
nb = json.loads(NB.read_text(encoding="utf-8"))


def find_code(sub):
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] == "code" and sub in "".join(c["source"]):
            return i
    raise AssertionError(f"未找到 cell: {sub}")


def set_src(cell, text):
    cell["source"] = [l + "\n" for l in text.split("\n")[:-1]]


# ---------- 1. cell-3 选型：7B 4bit + 8192 ----------
OLD_SEL = '''if vram_gb >= 40:
    MODEL, DTYPE, CTX = "Qwen/Qwen2.5-7B-Instruct", "bfloat16", 4096
elif vram_gb >= 14:
    MODEL, DTYPE, CTX = "Qwen/Qwen2.5-1.5B-Instruct", "bfloat16", 2048
else:
    MODEL, DTYPE, CTX = "Qwen/Qwen2.5-0.5B", "float32", 1024
print(f"自动选择: {MODEL} dtype={DTYPE} ctx={CTX}")'''
NEW_SEL = '''# DS-20261019-016 规格：Qwen2.5-7B-Instruct（NF4 4-bit + FP16 compute/KV）+ 8K 上下文
if vram_gb >= 14:   # T4(15GB)/P100(16G) 可装 7B 4bit(~4.5GB) + 8K FP16 KV(~900MB)
    MODEL, DTYPE, CTX, LOAD4BIT = "Qwen/Qwen2.5-7B-Instruct", "float16", 8192, True
else:               # 无 GPU 或显存不足：退回 0.5B/1K 慢速 CPU 冒烟
    MODEL, DTYPE, CTX, LOAD4BIT = "Qwen/Qwen2.5-0.5B", "float32", 1024, False
FOURBIT_ARG = "--load-in-4bit" if LOAD4BIT else ""
print(f"自动选择: {MODEL} dtype={DTYPE} ctx={CTX} 4bit={LOAD4BIT}")'''
i = find_code("按显存自动选模型")
set_src(nb["cells"][i], "".join(nb["cells"][i]["source"]).replace(OLD_SEL, NEW_SEL))

# ---------- 2. Step 1/2 命令加 --load-in-4bit ----------
for marker in ("--baseline-only", "--strategy v15"):
    i = find_code(marker)
    src = "".join(nb["cells"][i]["source"])
    assert "{FOURBIT_ARG}" not in src
    src = src.replace("--device {DEV} \\", "--device {DEV} {FOURBIT_ARG} \\")
    set_src(nb["cells"][i], src)

# ---------- 3. 新增 Step 4 cell（插在 Step 3 之后） ----------
STEP4 = '''# ============ 8. Step 4：WB2 接口合流（Router .pt 送达后自动生效） ============
# DS-20261019-016 步骤 3：WB2 交付 .pt 后 → 替换随机权重 → 验证路由 →
# 挂载混合保护（Sink+Win+Mid-TopK）→ Argmax 是否突破 50%（H-DS10 通过线）。
# 用法：把 WB2 的 .pt 放到 /kaggle/working/wb2_router.pt 后重跑本 cell。
WB2_PT = "/kaggle/working/wb2_router.pt"
if os.path.exists(WB2_PT):
    # ① 替换随机权重：load_external_router 真加载路径（DS 规格 state_dict）
    from src.semantickv.vllm_plugin import SemanticRouter
    router = SemanticRouter.load_external_router(WB2_PT)
    print("WB2 Router 已挂载:", WB2_PT)
    # ② 混合保护端到端：Sink4 + Win128 + 中段 TopK8（块对角掩码，H-DS7/H-DS8 口径）
    !cd {REPO} && SEMANTICKV_THREADS=8 python3 src/semantickv/v41_torch_native_runner.py \\
        --model {MODEL} --dtype {DTYPE} --device {DEV} {FOURBIT_ARG} \\
        --ctx {CTX} --strategy v15 \\
        --metric centered --compress-mode hybrid --mid-scope blockdiag \\
        --topk-per-group 8 --sink-tokens 4 --window-tokens 128 \\
        --compress-layers 0.25-0.75 --kmean-renorm none --group-score-prior logcount \\
        --k-grid 8 --tau-grid 0.97 \\
        --needle-positions 0.15,0.5,0.85 \\
        --out results/kaggle_7b_hybrid.json
    print(">>> 判定：argmax ≥50% 且 cos ≥0.85 → H-DS10 confirmed；")
    print(">>> 否则按 DS-20261018-007 预案：换 14B/32B 或开 YaRN。")
else:
    print("等待 WB2 Router .pt（DS-20261019-016：已催）。")
    print("放置于 /kaggle/working/wb2_router.pt 后重跑本 cell 即自动合流。")'''
i3 = find_code("Step 3：Router 空壳 + 插件冒烟")
nb["cells"].insert(i3 + 1, {"cell_type": "code", "execution_count": None,
                            "metadata": {}, "outputs": [],
                            "source": [l + "\n" for l in STEP4.split("\n")[:-1]]})

# 原「7. 结果摘要」改「9.」（Step4=8）
for c in nb["cells"]:
    if c["cell_type"] == "code":
        src = "".join(c["source"])
        if src.startswith("# ============ 7. 结果摘要"):
            set_src(c, src.replace("# ============ 7. 结果摘要",
                                   "# ============ 9. 结果摘要", 1))

# ---------- 4. 标题 md：流程说明同步 ----------
i = find_code("Step 1：原始 needle 基线")
for c in nb["cells"]:
    if c["cell_type"] == "markdown" and "baseline-first 两步流程" in "".join(c["source"]):
        src = "".join(c["source"])
        src = src.replace(
            "## baseline-first 两步流程",
            "## baseline-first 四步流程（DS-20261019-016）")
        src = src.replace(
            "2. **Step 2 · V1.5 压缩验证**：通过基线门后才跑压缩全流程"
            "（V0 不变性 + 分组扫描 + 保真度 + 端到端 needle）。",
            "2. **Step 2 · V1.5 压缩验证**：通过基线门后才跑压缩全流程"
            "（V0 不变性 + 分组扫描 + 保真度 + 端到端 needle）。\n"
            "3. **Step 3 · Router 空壳冒烟**：挂随机初始化 Router（DS 规格），"
            "测延迟/显存 + CUDA Graph capture 探测。\n"
            "4. **Step 4 · WB2 合流**：Router .pt 送达后自动替换随机权重，"
            "跑混合保护端到端（argmax ≥50% 验证）。")
        c["source"] = [l + "\n" for l in src.split("\n")[:-1]]
        break

NB.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")

# 校验
for j, c in enumerate(nb["cells"]):
    if c["cell_type"] == "code":
        src = "".join(c["source"])
        try:
            compile(src, f"cell{j}", "exec")
            tag = "OK"
        except SyntaxError as e:
            tag = f"MAGIC/SYNTAX({e.lineno})" if "!" in src or "%" in src else f"ERR {e}"
        print(f"cell{j}: {tag}  {src.splitlines()[0][:46]}")
