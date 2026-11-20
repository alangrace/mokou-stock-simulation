#!/usr/bin/env bash
# 生成 Mokou 模拟盘收盘快照，复制到 GitHub Pages 的 docs/index.html 并推送。
# 只提交 HTML；不提交 data/paper.db、日志、密钥。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
ACCOUNT="${SAYU_ACCOUNT:-默认模拟盘}"

cd "$ROOT/sayu-paper-trader"
"$PYTHON_BIN" -m papertrader.cli dashboard --account "$ACCOUNT"

mkdir -p "$ROOT/docs"
cp reports/dashboard.html "$ROOT/docs/index.html"
touch "$ROOT/docs/.nojekyll"

cd "$ROOT"
git add docs/index.html docs/.nojekyll sayu-paper-trader/reports/dashboard.html
if git diff --cached --quiet; then
  echo "dashboard: no tracked changes"
else
  git commit -m "dashboard: $(date +%F)" >/dev/null
  echo "dashboard: committed $(date +%F)"
fi

git push origin main
echo "dashboard: pushed"
