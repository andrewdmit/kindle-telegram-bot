#!/usr/bin/env bash
set -euo pipefail

# Скрипт установки Kindle Telegram Bot на Ubuntu VPS
# Запуск: sudo bash install.sh

APP_NAME="kindle-telegram-bot"
APP_DIR="/opt/${APP_NAME}"
APP_USER="kindlebot"
SERVICE_NAME="${APP_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите скрипт от root: sudo bash install.sh"
  exit 1
fi

echo "==> Установка системных пакетов"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

echo "==> Создание пользователя ${APP_USER}"
if ! id "${APP_USER}" &>/dev/null; then
  useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi

echo "==> Копирование файлов в ${APP_DIR}"
mkdir -p "${APP_DIR}"
rsync -a --exclude '.git' --exclude '.venv' --exclude 'data' --exclude '.env' \
  "${SCRIPT_DIR}/" "${APP_DIR}/"

mkdir -p "${APP_DIR}/data"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "==> Создание виртуального окружения Python"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "==> Создание файла конфигурации .env"
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
  chmod 600 "${APP_DIR}/.env"
  echo ""
  echo "ВАЖНО: отредактируйте ${APP_DIR}/.env"
  echo "  - TELEGRAM_BOT_TOKEN (от @BotFather)"
  echo "  - SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_EMAIL"
  echo ""
fi

echo "==> Установка systemd-сервиса"
install -m 644 "${APP_DIR}/kindle-bot.service" "/etc/systemd/system/${SERVICE_NAME}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo ""
echo "Установка завершена."
echo ""
echo "Дальнейшие шаги:"
echo "  1. nano ${APP_DIR}/.env"
echo "  2. systemctl start ${SERVICE_NAME}"
echo "  3. systemctl status ${SERVICE_NAME}"
echo "  4. journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "Обновление бота:"
echo "  cd ${SCRIPT_DIR} && sudo bash install.sh && systemctl restart ${SERVICE_NAME}"
