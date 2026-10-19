#!/usr/bin/env bash
# ============================================================================
#  run_all.sh — 7B 实机一键跑（AutoDL RTX 4090D / Colab）
#  产出两件交付物：
#    1) results/kaggle_7b_fixed_route.json   —— 固定路由 Argmax（DS 判 45% 线）
#    2) results/real_features_7b.npz        —— (N,3584) 真实隐状态 + GSC 标签（交 WB2）
#
#  用法：
#    bash run_all.sh                     # 两步都跑
#    bash run_all.sh --skip-assault      # 只导特征
#    bash run_all.sh --skip-features     # 只跑固定路由
# ============================================================================
set -u

MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
CTX="${CTX:-8192}"
ENV_TAG="${ENV_TAG:-autodl-7b}"      # 全尺寸显卡用 autodl-7b；Kaggle 用 kaggle-7b
OUTDIR="${OUTDIR:-results}"
THREADS="${SEMANTICKV_THREADS:-8}"

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export SEMANTICKV_THREADS="$THREADS"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-1}"

SKIP_ASSAULT=0
SKIP_FEATURES=0
for a in "$@"; do
  case "$a" in
    --skip-assault)  SKIP_ASSAULT=1 ;;
    --skip-features) SKIP_FEATURES=1 ;;
  esac
done

mkdir -p "$OUTDIR"

echo "=============================================================="
echo " SemanticKV · 7B 实机一键跑"
echo "  model      : $MODEL"
echo "  ctx        : $CTX"
echo "  env tag    : $ENV_TAG   (四条断言会校验它)"
echo "  HF_ENDPOINT: $HF_ENDPOINT"
echo "=============================================================="

python3 - <<'PY'
import torch
if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    print(f"[gpu] {p.name}  {p.total_memory/1024**3:.1f} GB  cuda={torch.version.cuda}")
else:
    print("[gpu] ⚠ 未检测到 CUDA。7B 4bit 需要 GPU；继续会在加载时报错。")
PY

# ---------------------------------------------------------------- Step 5
if [ "$SKIP_ASSAULT" -eq 0 ]; then
  echo
  echo "[Step 5] 固定路由突击（gsc / uniform / qk）"
  python3 src/semantickv/fixed_route_assault.py \
    --model "$MODEL" --dtype float16 --device cuda --load-in-4bit \
    --ctx "$CTX" --group-mode gsc,uniform,qk \
    --gsc-k 16 --gsc-tau 0.97 --qk-tau 0.90 \
    --compress-mode hybrid --mid-scope blockdiag \
    --topk-per-group 8 --sink-tokens 4 --window-tokens 64 \
    --compress-layers 0.25-0.75 --kmean-renorm none --group-score-prior logcount \
    --needle-positions 0.15,0.5,0.85 \
    --out "${OUTDIR}/kaggle_7b_fixed_route.json" \
    --environment "$ENV_TAG" \
    || { echo ">>> Step 5 失败（见上方错误）。若为 OOM，试 --ctx 4096。"; }
fi

# ---------------------------------------------------------------- Step 6
if [ "$SKIP_FEATURES" -eq 0 ]; then
  echo
  echo "[Step 6] 导出真实隐状态 (N,3584) + GSC 标签"
  python3 src/semantickv/export_real_features.py \
    --model "$MODEL" --dtype float16 --device cuda --load-in-4bit \
    --ctx "$CTX" --gsc-k 16 --gsc-tau 0.97 --metric centered \
    --max-tokens 8192 --expect-dim 3584 \
    --out "${OUTDIR}/real_features_7b" \
    --environment "$ENV_TAG" \
    || { echo ">>> Step 6 失败（见上方错误）。"; }
fi

# ---------------------------------------------------------------- 摘要
echo
echo "=============================================================="
echo " 产出摘要"
echo "=============================================================="
python3 - <<PY
import json, os
base = "$OUTDIR"
fr = os.path.join(base, "kaggle_7b_fixed_route.json")
if os.path.exists(fr):
    d = json.load(open(fr))
    e = d.get("environment", {})
    print(f"[固定路由] env={e.get('kind')}  claims={e.get('claims_checked')}")
    print(f"           gate={d['gate']['baseline_hit_rate']:.3f}  verdict={d['verdict']}")
    for a in d.get("fixed_route_assault", []):
        print(f"           {a['group_mode']:8s} G={a['n_groups']:3d} argmax={a['argmax']}")
else:
    print("[固定路由] 未产出")

fm = os.path.join(base, "real_features_7b.json")
if os.path.exists(fm):
    m = json.load(open(fm))
    print(f"[特征导出] dim={m['hidden_size']}  check={m.get('dim_check')}  "
          f"tokens={m['n_tokens']}  groups={m['n_groups']}")
else:
    print("[特征导出] 未产出")
PY
echo
echo "把这两个文件回传即可："
echo "  ${OUTDIR}/kaggle_7b_fixed_route.json"
echo "  ${OUTDIR}/real_features_7b.npz"
