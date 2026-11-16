#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="schedule-bot"
SERVICE_USER="schedule-bot"
APP_DIR="/opt/schedule-bot-universal"
REPO_DIR="$APP_DIR/repo"
DATA_DIR="$APP_DIR/data"
ENV_PATH="$APP_DIR/.env"
SYSTEM_PYTHON="/usr/bin/python3"
REPO_URL="${REPO_URL:-https://github.com/xaku56/schedule-bot-universal.git}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if (( EUID != 0 )); then
    echo "Запустите скрипт через sudo: sudo bash install-ubuntu.sh" >&2
    exit 1
fi

if ! command -v apt-get >/dev/null || ! systemctl show --property=Version --value >/dev/null; then
    echo "Для установки нужен сервер Debian или Ubuntu с работающим systemd." >&2
    exit 1
fi

if [[ ! -f "$ENV_PATH" && ! -t 0 ]]; then
    echo "Для первой установки запустите скачанный скрипт в терминале, а не через pipe." >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 python3-venv ca-certificates

if ! "$SYSTEM_PYTHON" -I -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    echo "Для установки нужен Python 3.12 или новее. Debian 13: Python 3.13." >&2
    exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR" "$DATA_DIR"

if [[ -d "$REPO_DIR/.git" ]]; then
    runuser -u "$SERVICE_USER" -- git -C "$REPO_DIR" pull --ff-only
elif [[ -e "$REPO_DIR" ]]; then
    echo "$REPO_DIR существует, но не является Git-репозиторием." >&2
    exit 1
else
    runuser -u "$SERVICE_USER" -- git clone --depth 1 --branch main "$REPO_URL" "$REPO_DIR"
fi

if [[ ! -f "$ENV_PATH" ]]; then
    read -rsp "Telegram bot token: " token
    echo
    week_start="${NUMERATOR_WEEK_START:-$(sed -n 's/^NUMERATOR_WEEK_START=//p' "$REPO_DIR/.env.example")}"
    if [[ ! "$token" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]]; then
        echo "Токен Telegram имеет неверный формат." >&2
        exit 1
    fi
    if [[ ! "$week_start" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || [[ "$(date -d "$week_start" +%u 2>/dev/null || true)" != 1 ]]; then
        echo "NUMERATOR_WEEK_START должен быть корректной датой понедельника." >&2
        exit 1
    fi
    while true; do
        read -rp "HTTPS-адрес календаря (например, https://calendar.example.org:8443; Enter — без календаря): " calendar_url
        if calendar_url="$(
            cd "$REPO_DIR" || exit 1
            printf '%s' "$calendar_url" | runuser -u "$SERVICE_USER" -- "$SYSTEM_PYTHON" -B -c \
                'import sys; from app.config import parse_calendar_public_url; print(parse_calendar_public_url(sys.stdin.read()))' \
                2>/dev/null
        )"; then
            break
        fi
        echo "Введите только адрес вида https://calendar.example.org:8443 или нажмите Enter." >&2
    done
    (
        umask 077
        exec >"$ENV_PATH"
        sed \
            -e '/^TELEGRAM_BOT_TOKEN=/d' \
            -e '/^CALENDAR_PUBLIC_URL=/d' \
            -e "s|^NUMERATOR_WEEK_START=.*|NUMERATOR_WEEK_START=$week_start|" \
            -e "s|^BOT_WORKERS=.*|BOT_WORKERS=2|" \
            -e "s|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|" \
            "$REPO_DIR/.env.example"
        printf 'TELEGRAM_BOT_TOKEN=%s\nCALENDAR_PUBLIC_URL=%s\n' "$token" "$calendar_url"
    )
    chmod 600 "$ENV_PATH"
fi

if ! grep -Eq '^TELEGRAM_BOT_TOKEN=[0-9]{6,12}:[A-Za-z0-9_-]{30,}$' "$ENV_PATH"; then
    echo "В $ENV_PATH отсутствует корректный TELEGRAM_BOT_TOKEN." >&2
    exit 1
fi

if ! systemd-run --quiet --pipe --collect \
    --property="EnvironmentFile=$ENV_PATH" \
    --property="WorkingDirectory=$REPO_DIR" \
    --property="User=$SERVICE_USER" \
    --property="Group=$SERVICE_USER" \
    "$SYSTEM_PYTHON" -B -c 'from app.config import Config; Config.from_env()'; then
    echo "Не удалось проверить настройки в $ENV_PATH. Исправьте ошибку и запустите установщик снова." >&2
    exit 1
fi

runuser -u "$SERVICE_USER" -- "$SYSTEM_PYTHON" -m venv "$APP_DIR/.venv"
runuser -u "$SERVICE_USER" -- "$APP_DIR/.venv/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"

cat >"$UNIT_PATH" <<EOF
[Unit]
Description=Universal Telegram schedule bot
Wants=network-online.target
After=network-online.target

[Service]
Type=notify
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_PATH
ExecStart=$APP_DIR/.venv/bin/python $REPO_DIR/main.py
Restart=always
RestartSec=5
TimeoutStartSec=300
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
if ! systemctl restart "$SERVICE_NAME" || ! systemctl is-active --quiet "$SERVICE_NAME"; then
    journalctl -u "$SERVICE_NAME" -n 50 --no-pager
    exit 1
fi

memory_kb="$(awk '/MemTotal|SwapTotal/ { total += $2 } END { print total }' /proc/meminfo)"
if (( memory_kb < 1500000 )); then
    echo "Внимание: RAM + swap меньше 1.5 ГБ. Для VPS с 1 ГБ рекомендуется добавить swap." >&2
fi

echo "Бот установлен и запущен."
echo "Состояние: systemctl status $SERVICE_NAME"
echo "Журнал: journalctl -u $SERVICE_NAME -f"
