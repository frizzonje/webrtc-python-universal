#!/usr/bin/env bash
# Запуск нативного клиента «Белой Берёзки».
#   ./run.sh            → GUI (по умолчанию)
#   ./run.sh --cli ...  → консольный режим с флагами
# Поддерживает macOS (avfoundation) и Linux/Ubuntu (x11grab + pulse).
set -e
cd "$(dirname "$0")"

OS="$(uname -s)"

if [ "$OS" = "Darwin" ]; then
  # ── macOS ───────────────────────────────────────────────────────────────
  if ! command -v ffmpeg >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      echo ">> Ставлю ffmpeg через Homebrew…"
      brew install ffmpeg
    else
      echo "!! Нужен ffmpeg. Поставь Homebrew (https://brew.sh), потом: brew install ffmpeg"
      exit 1
    fi
  fi
  # venv БЕЗ --system-site-packages: на macOS tkinter идёт в самом python.org/brew
  VENV_ARGS=""
  PY="$(command -v python3)"
else
  # ── Linux (Ubuntu) ──────────────────────────────────────────────────────
  need_apt=0
  command -v ffmpeg >/dev/null 2>&1 || need_apt=1
  python3 -c 'import tkinter' >/dev/null 2>&1 || need_apt=1
  if [ "$need_apt" = 1 ]; then
    echo ">> Ставлю системные пакеты (нужен sudo)…"
    sudo apt-get update
    sudo apt-get install -y python3-venv python3-dev python3-tk pulseaudio-utils ffmpeg \
         libavdevice-dev libopus0 libvpx-dev libportaudio2 pkg-config
  fi
  VENV_ARGS="--system-site-packages"
  PY=python3
fi

# venv + python-зависимости (один раз)
if [ ! -d .venv ]; then
  "$PY" -m venv $VENV_ARGS .venv
  ./.venv/bin/pip install -U pip wheel
  ./.venv/bin/pip install -r requirements.txt
fi

# запуск
if [ "$1" = "--cli" ]; then
  shift
  exec ./.venv/bin/python bb_native.py "$@"
fi
exec ./.venv/bin/python bb_gui.py "$@"
