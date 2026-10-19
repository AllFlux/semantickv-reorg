#!/usr/bin/env bash
# 打包 7B 实机最小部署包 → dist/autodl_deploy.tar.gz
# 只含运行必需文件，不含 results/、weights、.git 等。
set -eu
cd "$(dirname "$0")/.." || exit 1

OUT="dist/autodl_deploy.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# 顶层目录必须是 deploy_autodl/——用户手册与 DS 指令都写 `cd deploy_autodl`，
# 扁平解压会让该命令直接失败。
PKG="$STAGE/deploy_autodl"
mkdir -p "$PKG/src/semantickv"
cp deploy/autodl/run_all.sh          "$PKG/"
cp deploy/autodl/requirements.txt     "$PKG/"
cp deploy/autodl/README.md            "$PKG/"
cp src/semantickv/fixed_route_assault.py       "$PKG/src/semantickv/"
cp src/semantickv/export_real_features.py      "$PKG/src/semantickv/"
cp src/semantickv/v41_torch_native_runner.py   "$PKG/src/semantickv/"
cp src/semantickv/mock_grouper.py              "$PKG/src/semantickv/"
cp eval_runner.py                              "$PKG/"   # mock_grouper 的 GroupAssignment 依赖

# 语法自检：包内文件必须可解析，否则不打包
for f in "$PKG"/src/semantickv/*.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$f"
done
bash -n "$PKG/run_all.sh"

mkdir -p dist
tar -czf "$OUT" -C "$STAGE" deploy_autodl

echo "[OK] $OUT"
echo "     大小: $(du -h "$OUT" | cut -f1)"
echo "     内容:"
tar -tzf "$OUT" | sed 's/^/       /'
