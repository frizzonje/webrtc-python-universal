#!/usr/bin/env bash
# Запуск нативного клиента «Белой Берёзки» на Ubuntu.
#   ./run.sh            → GUI (по умолчанию)
#   ./run.sh --cli ...  → консольный режим с флагами
set -e
cd "$(dirname "$0")"

# 1) системные зависимости (один раз)
need_apt=0
command -v pacat  >/dev/null 2>&1 || need_apt=1
command -v ffmpeg >/dev/null 2>&1 || need_apt=1
python3 -c 'import tkinter' >/dev/null 2>&1 || need_apt=1
if [ "$need_apt" = 1 ]; then
  echo ">> Ставлю системные пакеты (нужен sudo)…"
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-dev python3-tk pulseaudio-utils ffmpeg \
       libavdevice-dev libopus0 libvpx-dev pkg-config
fi

# 2) venv (с системными пакетами, чтобы был виден tkinter) + python-зависимости
if [ ! -d .venv ]; then
  python3 -m venv --system-site-packages .venv
  ./.venv/bin/pip install -U pip wheel
  ./.venv/bin/pip install -r requirements.txt
fi

# 3) запуск
if [ "$1" = "--cli" ]; then
  shift
  exec ./.venv/bin/python bb_native.py "$@"
fi
exec ./.venv/bin/python bb_gui.py "$@"
