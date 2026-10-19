#!/usr/bin/env bash
# fix_gitignore.sh — 修复被 WB2 rest-upload 覆盖的 .gitignore（第 4 次复发后固化）
#
# 现象：WB2 每次 rest-upload 都会把 .gitignore 写成 src/shared/ 白名单：
#     /*
#     !/src/
#     !/.gitignore
#     /src/*
#     !/src/shared/
# 后果：`/*` 会吞掉仓库根下一切新文件（results/*.json、src/semantickv/*.py 等），
#       提交时静默丢失。已在 DS-024 / DS-026 / DS-035 / DS-038 四次 rebase 中复现。
#
# 本脚本：检测到该白名单特征 → 恢复标准 Python 忽略规则；否则不动。
# 幂等：可反复运行。
set -eu
cd "$(dirname "$0")/.." || exit 1

NEED_FIX=0
if grep -qxF '/*' .gitignore 2>/dev/null && grep -q '^!/src/shared/' .gitignore 2>/dev/null; then
  NEED_FIX=1
fi

if [ "$NEED_FIX" -eq 0 ]; then
  echo "[gitignore] 正常，无需修复"
  exit 0
fi

cat > .gitignore <<'EOF'
# Python
__pycache__/
*.pyc
*.pyo
.ipynb_checkpoints/

# 本地环境 / 临时
.DS_Store
*.log
.venv/

# 注意（WB1）：DS-20261019-023-WB2 的 src/shared/ 范围裁定约束的是 WB2 侧
# 推送通道（其 REST 工具 + 本地白名单），不应写成共享仓库的全局白名单。
# 该白名单已四次随 rest-upload 回流（DS-024/026/035/038），每次都会让 `/*`
# 吞掉双方新增文件。故共享仓库固定使用标准 Python 规则；
# WB2 推送范围由其本地工具自行约束，不要写进本文件。
# 自动修复：bash tools/fix_gitignore.sh（round_close.sh 会先调用）

# 给 DS 的投喂包（可由 tools/make_ds_brief.py 重新生成，不入库）
results/_ds_brief/
EOF

echo "[gitignore] 检测到 WB2 白名单，已恢复标准规则"
git check-ignore -q results/_gitignore_probe.json 2>/dev/null || true
