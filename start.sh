#!/usr/bin/env bash
# moments2tg launcher for macOS and Linux: creates .venv on first run, installs deps, starts the panel.
#   ./start.sh               -> control panel in the browser
#   ./start.sh --once ...    -> any moments2tg.py arguments
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  PYTHON=""
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
      PYTHON="$c"; break
    fi
  done
  if [ -z "$PYTHON" ]; then
    echo "Не найден Python 3.10+."
    echo "  macOS:          brew install python   (или https://www.python.org/downloads/)"
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-venv"
    echo "  Fedora:         sudo dnf install python3"
    exit 1
  fi
  echo "[moments2tg] Первый запуск: создаю окружение Python в .venv ..."
  if ! "$PYTHON" -m venv "$VENV"; then
    rm -rf "$VENV"
    echo "Не удалось создать venv. В Debian/Ubuntu: sudo apt install python3-venv"
    exit 1
  fi
fi

if ! cmp -s requirements.txt "$VENV/requirements.installed"; then
  echo "[moments2tg] Устанавливаю зависимости ..."
  "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt "$VENV/requirements.installed"
fi

if [ "${1:-}" = "--setup" ]; then
  shift
  "$PY" install_alpha.py "$@"
  echo
  echo "[moments2tg] Готово. Дальше запускайте ./start.sh без аргументов — откроется панель."
  exit 0
fi

if [ $# -eq 0 ]; then
  exec "$PY" moments2tg.py --panel
else
  exec "$PY" moments2tg.py "$@"
fi
