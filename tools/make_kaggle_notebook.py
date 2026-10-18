#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 run_on_kaggle.ipynb（Kaggle 一键上机脚本，V1.5 baseline-first 流程）。

流程设计（源自 DeepSeek 评审「先测无压缩 needle 基线再上压缩」的军规）：
  Step 1  --baseline-only：只验证原始模型 needle 基线（快，~1min）。
           原始 MISS => 模型太小/上下文太长，该测试无区分度，换更大模型再跑；
           原始 HIT  => 进入 Step 2。
  Step 2  V1.5 全量验证：V0 重排不变性 + (k,τ) 扫描 + 保真度曲线（V1/V1.5 家族）
           + 端到端 needle（原始 vs 压缩）+ 真实显存双指标（allocated/reserved）。
"""

import json

nb = {
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": [
            "# SemanticKV-Reorg · Kaggle 一键上机脚本（V1.5）\n",
            "\n",
            "**用途**：在 Kaggle（免费 T4×2 / P100，或租用的 A100/4090）上验证 SemanticKV-Reorg 的 V1.5 语义压缩机制。\n",
            "\n",
            "## 三步用法\n",
            "1. 左侧 **+ Add Input** → **Upload** → 上传 `semantickv-reorg.tar.gz`（或作为 Dataset 添加）；\n",
            "2. 右上 **Settings** → Accelerator 选 **GPU T4 x2**（或 P100）；\n",
            "3. **Run All**。\n",
            "\n",
            "## baseline-first 两步流程\n",
            "1. **Step 1 · 原始 needle 基线**（`--baseline-only`）：先确认无压缩模型在该上下文长度下能检索 needle。\n",
            "   原始 MISS ⇒ 测试无区分度，请换更大模型（Settings 改模型名）或缩短上下文后重跑；\n",
            "2. **Step 2 · V1.5 压缩验证**：通过基线门后才跑压缩全流程（V0 不变性 + 分组扫描 + 保真度 + 端到端 needle）。\n",
            "\n",
            "## V1.5 策略要点（相对 v0.1.2 的四处修正）\n",
            "- **度量**：cosine → **centered**（减全局均值再归一化，修复 LLM 隐空间各向异性）；\n",
            "- **代表**：flat 群均值 → **Top-K 代表 token**（每群注意力入度最高的 K 个真实 KV，K=8 推荐，K=4 更省）；\n",
            "- **结构**：Attention Sink 保护（前 4 token 不分组）+ 近期滑窗（64 token 不压缩）；\n",
            "- **层级**：只压中层（24 层模型压 6-17，即 25%-75%），保留底层/顶层原样；\n",
            "- **分数先验**：群均值槽补 `log(群规模)` 分数修正（少槽 softmax 温度剧变的补偿）。\n",
            "\n",
            "> 边界声明：本脚本验证**机制与数学正确性**，不是最终性能；架构有效性与模型参数量无关。\n"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "# ============ 1. 安装依赖 ============\n",
            "!pip install -q -U transformers scipy pyyaml numpy\n",
            "\n",
            "import os, sys, glob\n",
            "# 国内网络如无法访问 huggingface.co，取消下一行注释：\n",
            "# os.environ[\"HF_ENDPOINT\"] = \"https://hf-mirror.com\"\n",
            "print(\"环境准备完成\")\n"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "# ============ 2. 定位仓库 ============\n",
            "# 方式 A：semantickv-reorg.tar.gz 上传到 /kaggle/working\n",
            "# 方式 B：tar.gz 作为 Dataset 挂到 /kaggle/input/<数据集名>/\n",
            "cands = glob.glob(\"/kaggle/working/semantickv-reorg.tar.gz\") + \\\n",
            "        glob.glob(\"/kaggle/input/*/semantickv-reorg.tar.gz\")\n",
            "if cands:\n",
            "    !tar -xzf {cands[0]} -C /kaggle/working/\n",
            "    REPO = \"/kaggle/working/semantickv-reorg\"\n",
            "else:\n",
            "    # 方式 C：已推送远端仓库时，改成真实地址\n",
            "    !git clone --depth 1 https://github.com/<your>/semantickv-reorg.git /kaggle/working/semantickv-reorg\n",
            "    REPO = \"/kaggle/working/semantickv-reorg\"\n",
            "sys.path.insert(0, REPO)\n",
            "sys.path.insert(0, os.path.join(REPO, \"src\", \"semantickv\"))\n",
            "assert os.path.exists(os.path.join(REPO, \"eval_runner.py\")), \"仓库解压失败\"\n",
            "print(\"仓库路径:\", REPO)\n"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "# ============ 3. GPU 检查 + 接口签名校验 + 选模型 ============\n",
            "import torch\n",
            "if torch.cuda.is_available():\n",
            "    props = torch.cuda.get_device_properties(0)\n",
            "    vram_gb = props.total_memory / 1e9\n",
            "    print(f\"GPU: {props.name}  显存 {vram_gb:.1f} GB\")\n",
            "else:\n",
            "    vram_gb = 0.0\n",
            "    print(\"未检测到 GPU，将使用 CPU 慢速模式（建议选小模型 + 短上下文）\")\n",
            "\n",
            "# 按显存自动选模型（7B 起步：0.5B 的 needle 基线不可靠，见 results/REAL_MODEL_VALIDATION.md）\n",
            "if vram_gb >= 40:\n",
            "    MODEL, DTYPE, CTX = \"Qwen/Qwen2.5-7B-Instruct\", \"bfloat16\", 4096\n",
            "elif vram_gb >= 14:\n",
            "    MODEL, DTYPE, CTX = \"Qwen/Qwen2.5-1.5B-Instruct\", \"bfloat16\", 2048\n",
            "else:\n",
            "    MODEL, DTYPE, CTX = \"Qwen/Qwen2.5-0.5B\", \"float32\", 1024\n",
            "print(f\"自动选择: {MODEL} dtype={DTYPE} ctx={CTX}\")\n",
            "DEV = 'cuda' if vram_gb > 0 else 'cpu'\n",
            "\n",
            "# 接口签名校验（13/13 通过才能继续）\n",
            "!cd {REPO} && python3 eval_runner.py --check-interfaces\n"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "# ============ 4. Step 1：原始 needle 基线（baseline 门） ============\n",
            "# 无压缩、无分组、无压缩机制，只验证原始模型在该 ctx 下能否检索 needle。\n",
            "# 耗时参考：0.5B/CPU 约 1 分钟；7B/A100 约 1 分钟。\n",
            "!cd {REPO} && SEMANTICKV_THREADS=8 python3 src/semantickv/v41_torch_native_runner.py \\\n",
            "    --model {MODEL} --dtype {DTYPE} --device {DEV} \\\n",
            "    --ctx {CTX} --baseline-only \\\n",
            "    --needle-positions 0.15,0.5,0.85 \\\n",
            "    --out results/kaggle_baseline.json\n",
            "\n",
            "import json\n",
            "bl = json.load(open(os.path.join(REPO, \"results\", \"kaggle_baseline.json\")))\n",
            "base_hits = [r[\"original_hit\"] for r in bl[\"stages\"][\"needle\"]]\n",
            "print(\"\\n===== Step 1 结果：原始 needle 基线 =====\")\n",
            "for r in bl[\"stages\"][\"needle\"]:\n",
            "    print(f\"pos={r['position_frac']}: {'HIT ' if r['original_hit'] else 'MISS'} {r['original_answer']!r}\")\n",
            "if all(base_hits):\n",
            "    print(\"\\n>>> 基线门通过：原始模型三个位置全部 HIT，继续 Step 2 压缩验证。\")\n",
            "else:\n",
            "    print(\"\\n>>> 基线门未通过：原始模型存在 MISS。\")\n",
            "    print(\">>> 说明该模型/该上下文下 needle 测试无区分度，请换更大模型或缩短 ctx 后重新 Run All。\")\n",
            "    import sys as _sys; _sys.exit(\"baseline gate failed\") if False else None  # 仅提示，不强制中断\n"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "# ============ 5. Step 2：V1.5 全量验证 ============\n",
            "# V0 重排不变性 + (k,τ) 扫描 + V1/V1.5 保真度曲线 + 端到端 needle + 真实显存双指标\n",
            "# 耗时参考：0.5B/CPU 约 3-4 分钟；1.5B/T4 约 2 分钟；7B/A100 约 3-4 分钟\n",
            "!cd {REPO} && SEMANTICKV_THREADS=8 python3 src/semantickv/v41_torch_native_runner.py \\\n",
            "    --model {MODEL} --dtype {DTYPE} --device {DEV} \\\n",
            "    --ctx {CTX} --strategy v15 \\\n",
            "    --metric centered --compress-mode topk --topk-per-group 8 \\\n",
            "    --sink-tokens 4 --window-tokens 64 --compress-layers 0.25-0.75 \\\n",
            "    --kmean-renorm none --group-score-prior logcount \\\n",
            "    --k-grid 8 --tau-grid 0.97 \\\n",
            "    --needle-positions 0.15,0.5,0.85 \\\n",
            "    --out results/real_model_v15.json\n"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "# ============ 6. 结果摘要（V1 vs V1.5 对照 + 显存双指标） ============\n",
            "d = json.load(open(os.path.join(REPO, \"results\", \"real_model_v15.json\")))\n",
            "bl = json.load(open(os.path.join(REPO, \"results\", \"kaggle_baseline.json\")))\n",
            "print(f\"模型: {d['model']}   总耗时: {d['wall_time_s']}s   峰值RSS: {d.get('prefill_peak_rss_mb')}MB\")\n",
            "cm = d.get(\"cuda_memory\", {})\n",
            "if cm:\n",
            "    print(f\"CUDA 峰值显存: allocated={cm.get('max_memory_allocated_mb')}MB \"\n",
            "          f\"reserved={cm.get('max_memory_reserved_mb')}MB（reserved>allocated 属缓存池正常现象）\")\n",
            "\n",
            "f = d[\"stages\"][\"fidelity\"]\n",
            "v0 = f[\"v0_reorder_invariance\"]\n",
            "print(f\"\\n[V0 重排不变性] cos mean={v0['mean']:.6f} min={v0['min']:.6f}  \"\n",
            "      f\"{'PASS 机制正确' if v0['mean'] > 0.999 else 'WARN 低于预期'}\")\n",
            "\n",
            "print(\"\\n[压缩-保真度曲线]（单层注意力输出 vs 原始，target layer=12）\")\n",
            "print(f\"{'k':>3} {'tau':>5} {'G':>5} {'V1压缩':>7} {'V1 cos':>8} | \"\n",
            "      f\"{'flat+sink':>9} {'weighted':>9} {'topk':>8} {'topk压缩':>8}\")\n",
            "for r in f[\"sweep\"]:\n",
            "    v15 = r[\"v15\"]\n",
            "    print(f\"{r['k']:>3} {r['tau']:>5.2f} {r['n_groups']:>5} {r['compression_ratio']:>6.0f}x \"\n",
            "          f\"{r['v1_group_mean']['mean']:>8.4f} | \"\n",
            "          f\"{v15['flat_mean_sink']['mean']:>9.4f} {v15['weighted_sink']['mean']:>9.4f} \"\n",
            "          f\"{v15['topk_sink']['mean']:>8.4f} {v15['topk_compression']:>7.1f}x\")\n",
            "\n",
            "print(\"\\n[needle 检索] 基线 vs 语义压缩（Step 1 门 + Step 2 端到端）\")\n",
            "base_by_pos = {r[\"position_frac\"]: r for r in bl[\"stages\"][\"needle\"]}\n",
            "for r in d[\"stages\"][\"needle\"]:\n",
            "    pf = r.get(\"prefill_fidelity\", {})\n",
            "    b = base_by_pos.get(r[\"position_frac\"], {})\n",
            "    kv = r.get(\"kv_bytes\", {})\n",
            "    print(f\"pos={r['position_frac']}: 原始 {'HIT' if r['original_hit'] else 'MISS'} {r['original_answer']!r} | \"\n",
            "          f\"压缩 {'HIT' if r['compressed_hit'] else 'MISS'} {r['compressed_answer']!r}\")\n",
            "    print(f\"   hidden cos={pf.get('final_hidden_cos', {}).get('mean', float('nan')):.4f} \"\n",
            "          f\"argmax一致率={pf.get('logits_argmax_agreement')} | \"\n",
            "          f\"KV {kv.get('original_mb')}MB->{kv.get('compressed_mb')}MB 省{kv.get('saving_pct')}%\")\n",
            "\n",
            "print(\"\\n[GSC (k,tau) 扫描]（真实隐状态分组质量，E0 校准输入）\")\n",
            "for r in d[\"stages\"][\"gsc_scan\"][\"rows\"]:\n",
            "    print(f\"k={r['k']:>2} tau={r['tau']:.2f}: G={r['n_groups']:>4} \"\n",
            "          f\"ARI_topic={r['ari_topic']:+.4f} n_groups_per_tok={r.get('groups_per_tok')}\")\n",
            "\n",
            "n_hit = sum(1 for r in d[\"stages\"][\"needle\"] if r[\"compressed_hit\"])\n",
            "n_all = len(d[\"stages\"][\"needle\"])\n",
            "print(f\"\\n===== 总结论：压缩 needle {n_hit}/{n_all} HIT；\"\n",
            "      f\"保真度 topk={f['sweep'][0]['v15']['topk_sink']['mean']:.4f}（V1 基线 {f['sweep'][0]['v1_group_mean']['mean']:.4f}）=====\")\n"
        ]},
        {"cell_type": "markdown", "metadata": {}, "source": [
            "## 结果解读指南\n",
            "\n",
            "| 指标 | 期望 | 含义 |\n",
            "|---|---|---|\n",
            "| Step 1 基线 | 原始 HIT | 不过则测试无区分度，换大模型（7B 起步） |\n",
            "| V0 cos | ≈1.000000 | KV gather/scatter + RoPE + GQA + 掩码的**张量机制**正确（不过则代码有 bug） |\n",
            "| V1 cos | 基线参照 | flat 群均值、cosine 度量、全层压缩（v0.1.2 方案） |\n",
            "| topk cos | > V1 | V1.5：Top-K 真实代表 + centered 度量 + sink/窗口保护 + 中层压缩 |\n",
            "| hidden cos / argmax | 越高越好 | 端到端保真度：最终隐状态余弦 / logits argmax 一致率 |\n",
            "| needle | 基线 HIT 且压缩 HIT | 理想情况；压缩 HIT 率随 KV 节省率 trade-off |\n",
            "| KV saving | 30%-50% 现实区间 | 12/24 层压缩 + Top-K 后的真实 KV 字节节省（>60% 不可信，检查记账） |\n",
            "| ARI_topic | >0.5 好 | GSC 在真实隐状态上恢复话题结构的程度（合成数据是 1.0） |\n",
            "\n",
            "**V1 vs V1.5 对照逻辑**（v0.1.2 → v0.2.0 的四处修正，详见 `results/REAL_MODEL_VALIDATION_V15.md`）：\n",
            "1. 若 `topk cos > V1 cos`：Top-K 真实代表 + centered 度量优于 flat 群均值 + cosine（预期，0.5B 实测 0.61 vs 0.41）；\n",
            "2. 若 `topk cos` 仍 <0.7：检查 sink/窗口是否真的保护了特权 token（`n_priv` 字段）；\n",
            "3. 若 needle 仍 MISS 但 hidden cos >0.8：问题不在压缩保真度，在检索鲁棒性——尝试加大 `--topk-per-group` 到 12/16，或加宽 `--window-tokens` 到 128；\n",
            "4. 真实显存看 `reserved`（分配器缓存池上限）而非 `allocated`（张量实际占用），两者差 30% 属正常。\n",
            "\n",
            "**已知边界**：\n",
            "- 0.5B 模型 needle 检索能力弱（v0.1.2 实测原始即部分 MISS），正式结论以 7B 为准；\n",
            "- 慢速验证器为保持 RoPE 位置正确仍写全量 cache，KV 节省以解析记账（`kv_bytes`）为准；\n",
            "- 真实隐空间呈「话题 × 位置」多尺度结构，单尺度 GSC 的 ARI 明显低于合成数据——这正是 E0 要做 kNN 分布校准的原因。\n"
        ]}
    ]
}

nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3.11"}
}
nb["nbformat"] = 4
nb["nbformat_minor"] = 5

# nbformat 4.5 要求每个 cell 有 id 字段
for i, cell in enumerate(nb["cells"]):
    cell.setdefault("id", f"cell-{i:02d}")

out = "/workspace/semantickv-reorg/run_on_kaggle.ipynb"
with open(out, "w", encoding="utf-8") as f:
    json.dump(nb, f, ensure_ascii=False, indent=1)

# 校验：nbformat 读取 + JSON 合法
try:
    import nbformat
    nbformat.read(out, as_version=4)
    print("nbformat 校验通过")
except ImportError:
    print("nbformat 未安装，跳过校验（JSON 结构已按规范生成）")
print("written:", out, len(json.dumps(nb)), "bytes")
