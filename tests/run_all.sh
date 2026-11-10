#!/usr/bin/env bash
# 运行本目录测试并生成 REPORT.md。不创建任何轮询/定时任务。
set -euo pipefail
TESTS="$(cd "$(dirname "$0")" && pwd)"
cd "$TESTS"

MODE="${1:-real}"
if [[ "${1:-}" == "mock" || "${1:-}" == "real" || "${1:-}" == "auto" ]]; then
  shift || true
fi

case "$MODE" in
  mock)
    export BOT_TEST_USE_MOCK=1
    ;;
  real|auto)
    export BOT_TEST_USE_MOCK=0
    ;;
  *)
    echo "用法: $0 [mock|real] [pytest 额外参数]"
    echo "  mock  使用 tests/mock 跑通脚手架（非产品验收）"
    echo "  real  对接开发模块，缺失则 skip（默认）"
    exit 2
    ;;
esac

# 优先使用本地虚拟环境，避免系统 Python 禁止 pip（PEP 668）
VENV="$TESTS/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c "import pytest" 2>/dev/null; then
  "$VENV/bin/pip" install -q pytest
fi
PYTHON="$VENV/bin/python"

# 其余参数传给 pytest，例如: ./run_all.sh real -k security
set +e
"$PYTHON" -m pytest -c pytest.ini "$@"
PY_EXIT=$?
set -e

"$PYTHON" report.py || true
exit "$PY_EXIT"
