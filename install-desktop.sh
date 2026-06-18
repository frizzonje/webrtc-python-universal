#!/usr/bin/env bash
# Добавляет ярлык в меню приложений Ubuntu, чтобы запускать клиент в один клик.
# Перед этим запустите ./run.sh хотя бы раз в терминале — он поставит зависимости.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"

cat > "$APPS/bb-native.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Белая Берёзка (нативка)
GenericName=Голос и демонстрация экрана
Comment=Голосовой чат + демонстрация экрана со звуком системы (Linux)
Exec=$DIR/run.sh
Path=$DIR
Icon=audio-input-microphone
Terminal=false
Categories=Network;AudioVideo;Chat;
StartupNotify=true
EOF

chmod +x "$APPS/bb-native.desktop"
update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo "✓ Ярлык «Белая Берёзка (нативка)» добавлен в меню приложений."
echo "  Найдите его в списке программ и при желании закрепите на панель."
echo "  Важно: один раз запустите ./run.sh в терминале (ставит зависимости),"
echo "  дальше ярлык открывает окно мгновенно."
