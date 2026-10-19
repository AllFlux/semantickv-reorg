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

# 定位到脚本所在目录：无论用户从哪个 cwd 调用，都能找到 src/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

MODEL="${MODEL:-Qwen/Qwen2.5-7B-Instruct}"
CTX="${CTX:-8192}"
ENV_TAG="${ENV_TAG:-autodl-7b}"      # 全尺寸显卡用 autodl-7b；Kaggle 用 kaggle-7b
OUTDIR="${OUTDIR:-results}"
DTYPE="${DTYPE:-float16}"
THREADS="${SEMANTICKV_THREADS:-8}"

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export SEMANTICKV_THREADS="$THREADS"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_ENABLE_HF_TRANSFER="${HF_HUB_ENABLE_HF_TRANSFER:-1}"

SKIP_ASSAULT=0
SKIP_FEATURES=0
FAILED=0
for a in "$@"; do
  case "$a" in
    --skip-assault)  SKIP_ASSAULT=1 ;;
    --skip-features) SKIP_FEATURES=1 ;;
  esac
done

# 冒烟模式：无 GPU 的机器上先验证包能不能跑（0.5B / CPU / 不打 4bit / 代理标签）
if [ "${SMOKE:-0}" = "1" ]; then
  # 两个脚本的参数名不同：突击用 --max-new-tokens（生成长度），导出用 --max-tokens（导出条数）
  SMOKE_SCALE_ASSAULT="--paragraphs 3 --repeats 2 --target-layer 4"
  SMOKE_SCALE_FEATURES="--paragraphs 3 --repeats 2 --target-layer 4 --max-tokens 512"
  MODEL="${SMOKE_MODEL:-/workspace/models/Qwen2.5-0.5B}"
  CTX=512
  ENV_TAG=local-sandbox-cpu-proxy-0.5B
  DEV=cpu
  DTYPE=float32
  FOURBIT_ARG=""
  echo "[SMOKE] 冒烟模式：0.5B / CPU / 无 4bit / 代理标签（产物不可交付）"
else
  SMOKE_SCALE_ASSAULT=""
  SMOKE_SCALE_FEATURES=""
  DEV="${DEV:-cuda}"
  FOURBIT_ARG="--load-in-4bit"
fi

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

# ---------------------------------------------------------------- 依赖预检
echo
echo "[deps] 检查运行依赖"
MISSING=$(SMOKE="${SMOKE:-0}" python3 - <<'PYDEP'
import os
import importlib.util as u
need = {"torch": "torch", "transformers": "transformers", "numpy": "numpy"}
# 4bit 路径才需要 bnb/accelerate；冒烟模式（CPU/无 4bit）不要求
if os.environ.get("SMOKE") != "1":
    need.update({"bitsandbytes": "bitsandbytes", "accelerate": "accelerate"})
print(" ".join(pkg for mod, pkg in need.items() if u.find_spec(mod) is None))
PYDEP
)
if [ -n "$MISSING" ]; then
  echo "[deps] 缺少: $MISSING"
  echo "       请先执行：pip install -r requirements.txt"
  if [ "${AUTO_INSTALL:-0}" = "1" ]; then
    echo "[deps] AUTO_INSTALL=1，开始安装…"
    python3 -m pip install -q -r requirements.txt || { echo ">>> 依赖安装失败"; exit 3; }
  else
    echo "       （或设 AUTO_INSTALL=1 让本脚本自动装）"
    exit 3
  fi
else
  echo "[deps] 齐全"
fi

# ---------------------------------------------------------------- Step 5
if [ "$SKIP_ASSAULT" -eq 0 ]; then
  echo
  echo "[Step 5] 固定路由突击（gsc / uniform / qk）"
  python3 src/semantickv/fixed_route_assault.py \
    --model "$MODEL" --dtype "$DTYPE" --device "$DEV" $FOURBIT_ARG \
    --ctx "$CTX" $SMOKE_SCALE_ASSAULT --group-mode gsc,uniform,qk \
    --gsc-k 16 --gsc-tau 0.97 --qk-tau 0.90 \
    --compress-mode hybrid --mid-scope blockdiag \
    --topk-per-group 8 --sink-tokens 4 --window-tokens 64 \
    --compress-layers 0.25-0.75 --kmean-renorm none --group-score-prior logcount \
    --needle-positions 0.15,0.5,0.85 \
    --out "${OUTDIR}/kaggle_7b_fixed_route.json" \
    --environment "$ENV_TAG"
  RC=$?
  if [ "$RC" -ne 0 ]; then
    FAILED=1
    echo ">>> Step 5 失败 rc=$RC（见上方错误）。OOM 可试 --ctx 4096；参数错请核对上方 usage。"
  fi
fi

# ---------------------------------------------------------------- Step 6
if [ "$SKIP_FEATURES" -eq 0 ]; then
  echo
  echo "[Step 6] 导出真实隐状态 (N,3584) + GSC 标签"
  python3 src/semantickv/export_real_features.py \
    --model "$MODEL" --dtype "$DTYPE" --device "$DEV" $FOURBIT_ARG \
    --ctx "$CTX" $SMOKE_SCALE_FEATURES --gsc-k 16 --gsc-tau 0.97 --metric centered \
    --max-tokens 8192 --expect-dim 3584 \
    --out "${OUTDIR}/real_features_7b" \
    --environment "$ENV_TAG"
  RC=$?
  if [ "$RC" -ne 0 ]; then
    FAILED=1
    echo ">>> Step 6 失败 rc=$RC（见上方错误）。"
  fi
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
echo "把这两个文件回传即可（绝对路径）："
echo "  ${SCRIPT_DIR}/${OUTDIR}/kaggle_7b_fixed_route.json"
echo "  ${SCRIPT_DIR}/${OUTDIR}/real_features_7b.npz"
echo
echo "若 HF_HOME 未设置，模型缓存会落在默认 ~/.cache；"
echo "在 PAI-DSW / 临时实例上建议：export HF_HOME=/mnt/workspace/hf_cache"
echo
if [ "$FAILED" -ne 0 ]; then
  echo "⚠ 有步骤失败（见上方）。不要回传不完整的结果。"
else
  echo "✔ 已执行的步骤均成功。"
fi
exit "$FAILED"
