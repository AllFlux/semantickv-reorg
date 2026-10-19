#!/usr/bin/env bash
# 打包 7B 实机最小部署包 → dist/autodl_deploy.tar.gz
# 只含运行必需文件，不含 results/、weights、.git 等。
set -eu
cd "$(dirname "$0")/.." || exit 1

OUT="dist/autodl_deploy.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/src/semantickv"
cp deploy/autodl/run_all.sh          "$STAGE/"
cp deploy/autodl/requirements.txt     "$STAGE/"
cp deploy/autodl/README.md            "$STAGE/"
cp src/semantickv/fixed_route_assault.py       "$STAGE/src/semantickv/"
cp src/semantickv/export_real_features.py      "$STAGE/src/semantickv/"
cp src/semantickv/v41_torch_native_runner.py   "$STAGE/src/semantickv/"
cp src/semantickv/mock_grouper.py              "$STAGE/src/semantickv/"
cp eval_runner.py                              "$STAGE/"   # mock_grouper 的 GroupAssignment 依赖

# 语法自检：包内文件必须可解析，否则不打包
for f in "$STAGE"/src/semantickv/*.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$f"
done
bash -n "$STAGE/run_all.sh"

mkdir -p dist
tar -czf "$OUT" -C "$STAGE" .

echo "[OK] $OUT"
echo "     大小: $(du -h "$OUT" | cut -f1)"
echo "     内容:"
tar -tzf "$OUT" | sed 's/^/       /'
