#!/usr/bin/env bash
# round_close.sh — 每轮固定的收尾动作（纪律 v2.0 §三.5）
#
# 做三件事：
#   1) 跑纪律检验并写入台账
#   2) 生成「给 DS 的投喂包」并跑 7 项自检
#   3) 打印文件位置与体积
#
# 用法：
#   bash tools/round_close.sh R19 "R19 交付：xxx"
#   bash tools/round_close.sh R19 "R19 交付：xxx" --auto
#
# 产出：results/_ds_brief/R19_TO_DS.md（整段复制给 DS）
set -u
ROUND="${1:?用法: round_close.sh <轮次如R19> <标题> [--auto]}"
TITLE="${2:?缺少标题}"
shift 2 || true
EXTRA="$*"
# 没给额外参数时默认 --auto（按本轮 git 变更自动抓取）
[ -z "$EXTRA" ] && EXTRA="--auto"

cd "$(dirname "$0")/.." || exit 1

echo "=============================================="
echo " 轮次收尾：$ROUND — $TITLE"
echo "=============================================="

# 预检：WB2 rest-upload 会覆盖 .gitignore（已 4 次），先修再交付
bash tools/fix_gitignore.sh || true

echo
echo "[1/2] 纪律检验"
python3 tools/check_discipline.py --round "$ROUND" | tail -6
CHECK_RC=${PIPESTATUS[0]}

echo
echo "[2/2] 生成投喂包"
python3 tools/make_ds_brief.py \
  --title "$TITLE" \
  --discipline-round "$ROUND" \
  --out "results/_ds_brief/${ROUND}_TO_DS.md" \
  $EXTRA
BRIEF_RC=$?

echo
if [ "$CHECK_RC" -ne 0 ]; then
  echo "⚠ 纪律检验非全绿（rc=$CHECK_RC）——按 §四.4 必须在交付说明中显式声明。"
fi
if [ "$BRIEF_RC" -ne 0 ]; then
  echo "⚠ 投喂包自检有 WARN——先修再发。"
  exit 2
fi
echo "✔ 交付包就绪：results/_ds_brief/${ROUND}_TO_DS.md"
echo "  下一步：打开该文件，全选复制，整段粘给 DS（不要改发链接）。"
