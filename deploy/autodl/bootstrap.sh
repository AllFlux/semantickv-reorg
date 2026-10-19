#!/usr/bin/env bash
# ============================================================================
#  bootstrap.sh — 手机端一行跑完（PAI-DSW / AutoDL / Colab）
#
#  为什么存在：手机浏览器里长粘贴会卡死，9 行命令几乎必然出错。
#  本脚本从仓库直接拉取，用户只需粘**一行**。
#
#  一行用法：
#    curl -sL https://cdn.jsdelivr.net/gh/AllFlux/semantickv-reorg@main/deploy/autodl/bootstrap.sh | bash
#
#  后台跑（关掉手机也不中断，最推荐）：
#    curl -sL .../bootstrap.sh | bash -s -- --bg
#
#  先只跑特征（DS 认定的唯一破局点，最快拿到关键产物）：
#    curl -sL .../bootstrap.sh | bash -s -- --bg --features-first
#
#  跑完自动回传 GitHub（需 export GITHUB_TOKEN=... ；可选）：
#    curl -sL .../bootstrap.sh | bash -s -- --bg --features-first --upload
# ============================================================================
set -uo pipefail

REPO_RAW="https://raw.githubusercontent.com/AllFlux/semantickv-reorg/main"
EXPECT_BYTES=49195
EXPECT_SHA="32348271e211720054ad25d64333f3024d82a24ef3eacfc4c50f423533fbfaf3"
SKIP_VERIFY="${SKIP_VERIFY:-0}"

BG=0
UPLOAD=0
RUN_ARGS=()

for a in "$@"; do
  case "$a" in
    --bg)             BG=1 ;;
    --fg)             BG=0 ;;
    --features-first) RUN_ARGS+=("--skip-assault") ;;
    --assault-only)   RUN_ARGS+=("--skip-features") ;;
    --upload)         UPLOAD=1 ;;
    *)                RUN_ARGS+=("$a") ;;
  esac
done

# 工作目录：优先 /mnt/workspace（PAI-DSW 持久盘），否则当前目录
WORKDIR="${WORKDIR:-/mnt/workspace}"
if [ ! -d "$WORKDIR" ]; then WORKDIR="$(pwd)"; fi
cd "$WORKDIR" || { echo "[FATAL] 无法进入 $WORKDIR"; exit 1; }
echo "[bootstrap] workdir=$WORKDIR"

# ---------------------------------------------------------------- 1. 下载
# 国内环境 raw.githubusercontent 常被墙（实测 10s 无响应），故优先 jsDelivr CDN，
# 再回退 GitHub raw。任一镜像只要大小+sha 对得上就用；都不行才报错。
echo "[1/4] 下载部署包（约 48KB）"
MIRRORS=(
  "https://cdn.jsdelivr.net/gh/AllFlux/semantickv-reorg@main/dist/autodl_deploy.tar.gz"
  "https://raw.githubusercontent.com/AllFlux/semantickv-reorg/main/dist/autodl_deploy.tar.gz"
  "https://ghfast.top/https://raw.githubusercontent.com/AllFlux/semantickv-reorg/main/dist/autodl_deploy.tar.gz"
)

OK=0
for u in "${MIRRORS[@]}"; do
  host=$(echo "$u" | sed -E 's#https?://([^/]+).*#\1#')
  printf "      尝试 %s … " "$host"
  rm -f pkg.tgz
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 8 --max-time 120 -o pkg.tgz "$u" || true
  else
    wget -q --timeout=20 -O pkg.tgz "$u" || true
  fi
  [ -s pkg.tgz ] || { echo "失败"; continue; }

  SIZE=$(stat -c %s pkg.tgz 2>/dev/null || stat -f %z pkg.tgz)
  if [ "$SKIP_VERIFY" != "1" ] && [ "$SIZE" != "$EXPECT_BYTES" ]; then
    echo "大小不符（$SIZE ≠ $EXPECT_BYTES）"; continue
  fi
  if [ "$SKIP_VERIFY" != "1" ] && command -v sha256sum >/dev/null 2>&1; then
    GOT=$(sha256sum pkg.tgz | cut -d' ' -f1)
    if [ "$GOT" != "$EXPECT_SHA" ]; then
      echo "sha256 不符（可能 CDN 缓存旧版）"; continue
    fi
  fi
  echo "OK（$SIZE 字节，校验通过）"
  OK=1
  break
done

if [ "$OK" -ne 1 ]; then
  echo "[FATAL] 所有镜像均不可用或校验不通过。"
  echo "  可手动下载后放到 $WORKDIR/pkg.tgz 再运行本脚本。"
  echo "  镜像列表："
  printf '    %s\n' "${MIRRORS[@]}"
  exit 1
fi

# ---------------------------------------------------------------- 2. 解压
echo "[2/4] 解压"
rm -rf deploy_autodl
tar -xzf pkg.tgz || { echo "[FATAL] 解压失败"; exit 1; }
[ -d deploy_autodl ] || { echo "[FATAL] 未生成 deploy_autodl/"; exit 1; }
cd deploy_autodl || exit 1

# ---------------------------------------------------------------- 3. 环境 + 依赖
echo "[3/4] 环境与依赖"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_HOME="${HF_HOME:-$WORKDIR/hf_cache}"
export SEMANTICKV_THREADS="${SEMANTICKV_THREADS:-8}"
export TOKENIZERS_PARALLELISM=false
mkdir -p "$HF_HOME"

SMOKE="${SMOKE:-0}" python3 - <<'PYDEPS'
import os, importlib.util as u
need = {"torch": "torch", "transformers": "transformers"}
if os.environ.get("SMOKE") != "1":
    need.update({"bitsandbytes": "bitsandbytes", "accelerate": "accelerate"})
miss = [p for m, p in need.items() if u.find_spec(m) is None]
print("      缺少:", " ".join(miss) if miss else "无")
raise SystemExit(1 if miss else 0)
PYDEPS
if [ $? -ne 0 ]; then
  echo "      安装依赖（首次约 1–3 分钟）"
  pip install -q -r requirements.txt || { echo "[FATAL] pip 安装失败"; exit 1; }
fi

# ---------------------------------------------------------------- 4. 运行
echo "[4/4] 运行"
if [ "$BG" -eq 1 ]; then
  nohup bash run_all.sh "${RUN_ARGS[@]}" > run.log 2>&1 &
  PID=$!
  echo "$PID" > run.pid
  echo
  echo "=============================================================="
  echo " 已后台启动，PID=$PID"
  echo " 可以关掉手机，任务不会中断"
  echo
  echo " 看进度： tail -f $WORKDIR/deploy_autodl/run.log"
  echo " 看是否跑完： grep -E '^✔|^⚠' $WORKDIR/deploy_autodl/run.log"
  echo " 进程还在吗： ps -p $PID"
  echo "=============================================================="
else
  bash run_all.sh "${RUN_ARGS[@]}" 2>&1 | tee run.log
  RC=${PIPESTATUS[0]}
  if [ "$RC" -ne 0 ]; then
    echo "[FATAL] run_all.sh 失败 rc=$RC，见上方日志"
    exit "$RC"
  fi
fi

# ---------------------------------------------------------------- 5. 可选：回传
if [ "$UPLOAD" -eq 1 ] && [ "$BG" -eq 0 ]; then
  echo
  echo "[upload] 回传结果到 GitHub"
  if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "      未设置 GITHUB_TOKEN，跳过。"
    echo "      手动下载：JupyterLab 左侧进入 $WORKDIR/deploy_autodl/results/"
  else
    BR="results/$(hostname)-$(date +%Y%m%d_%H%M%S)"
    TMP="$WORKDIR/_upload_tmp"
    rm -rf "$TMP"
    echo "      克隆仓库（浅）…"
    git clone -q --depth 1 "https://x-access-token:${GITHUB_TOKEN}@github.com/AllFlux/semantickv-reorg.git" "$TMP" \
      || { echo "      克隆失败（token 无效或无权限）"; exit 0; }
    mkdir -p "$TMP/results/uploads/$(date +%Y%m%d_%H%M%S)"
    DEST="$TMP/results/uploads/$(date +%Y%m%d_%H%M%S)"
    cp -v results/real_features_7b.npz results/real_features_7b.json \
          results/kaggle_7b_fixed_route.json "$DEST"/ 2>/dev/null || true
    (
      cd "$TMP" || exit 0
      git config user.email "wb1@semantickv.local"
      git config user.name "WB1-bootstrap"
      git checkout -q -b "$BR"
      git add -A
      git commit -q -m "results: 7B 实机产物回传（$BR）" || { echo "      无变更可提交"; exit 0; }
      git push -q origin "$BR" && echo "      已推送到分支 $BR"
    )
    rm -rf "$TMP"
  fi
fi
