#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="schedule-bot"
SERVICE_USER="schedule-bot"
APP_DIR="/opt/schedule-bot-universal"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if (( EUID != 0 )); then
    echo "Запустите скрипт через sudo: sudo ./install-ubuntu.sh" >&2
    exit 1
fi

if [[ ! -f "$SOURCE_DIR/.env" ]]; then
    echo "Создайте $SOURCE_DIR/.env из .env.example и заполните TELEGRAM_BOT_TOKEN." >&2
    exit 1
fi
if ! grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' "$SOURCE_DIR/.env" || grep -q 'replace-with-your-bot-token' "$SOURCE_DIR/.env"; then
    echo "В $SOURCE_DIR/.env не заполнен TELEGRAM_BOT_TOKEN." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv ca-certificates

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR" "$APP_DIR/data"
cp -a "$SOURCE_DIR/app" "$SOURCE_DIR/main.py" "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SOURCE_DIR/.env" "$APP_DIR/.env"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

cat >"$UNIT_PATH" <<EOF
[Unit]
Description=Universal Telegram schedule bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py
Restart=always
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    journalctl -u "$SERVICE_NAME" -n 50 --no-pager
    exit 1
fi

memory_kb="$(awk '/MemTotal|SwapTotal/ { total += $2 } END { print total }' /proc/meminfo)"
if (( memory_kb < 1500000 )); then
    echo "Внимание: RAM + swap меньше 1.5 ГБ. Для VPS с 1 ГБ рекомендуется добавить swap." >&2
fi

echo "Бот запущен: systemctl status $SERVICE_NAME"
echo "Журнал: journalctl -u $SERVICE_NAME -f"
