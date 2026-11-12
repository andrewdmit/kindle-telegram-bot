#!/usr/bin/env bash
set -euo pipefail

# Деплой бота на VPS с вашего компьютера.
#
#   ./deploy.sh root@203.0.113.10          # или user@host с правами sudo
#   SSH_PORT=2222 ./deploy.sh user@host
#
# Копирует код на сервер, запускает install.sh и перезапускает сервис.
# При первой установке предложит заполнить .env прямо на сервере:
# токен и SMTP-пароль не проходят через этот скрипт и не попадают в git.

TARGET="${1:-}"
SSH_PORT="${SSH_PORT:-22}"
APP_DIR="/opt/kindle-telegram-bot"
SERVICE="kindle-telegram-bot"
STAGING="/tmp/kindle-telegram-bot-deploy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${TARGET}" ]]; then
  echo "Использование: ./deploy.sh user@host"
  exit 1
fi

SSH=(ssh -p "${SSH_PORT}" -o ServerAliveInterval=15)
SUDO="sudo"
[[ "${TARGET%%@*}" == "root" ]] && SUDO=""

echo "==> Проверка доступа к ${TARGET}"
"${SSH[@]}" "${TARGET}" "echo ok >/dev/null"

echo "==> Копирование кода"
"${SSH[@]}" "${TARGET}" "rm -rf ${STAGING} && mkdir -p ${STAGING}"
tar -C "${SCRIPT_DIR}" \
  --exclude .git --exclude .venv --exclude data --exclude .env \
  --exclude __pycache__ --exclude .pytest_cache \
  -czf - . | "${SSH[@]}" "${TARGET}" "tar -C ${STAGING} -xzf -"

echo "==> Установка"
"${SSH[@]}" -t "${TARGET}" "cd ${STAGING} && ${SUDO} bash install.sh"

if "${SSH[@]}" "${TARGET}" "${SUDO} grep -q '^TELEGRAM_BOT_TOKEN=123456789:' ${APP_DIR}/.env"; then
  echo ""
  echo "В ${APP_DIR}/.env ещё стоят значения из примера."
  read -r -p "Открыть его в nano на сервере сейчас? [Y/n] " answer
  if [[ "${answer:-y}" =~ ^[Yy]$ ]]; then
    "${SSH[@]}" -t "${TARGET}" "${SUDO} nano ${APP_DIR}/.env"
  else
    echo "Заполните .env и запустите: ssh ${TARGET} '${SUDO} systemctl restart ${SERVICE}'"
    exit 0
  fi
fi

echo "==> Перезапуск сервиса"
"${SSH[@]}" "${TARGET}" "${SUDO} systemctl restart ${SERVICE} && sleep 3 && ${SUDO} systemctl --no-pager --lines=15 status ${SERVICE}"
"${SSH[@]}" "${TARGET}" "rm -rf ${STAGING}"

echo ""
echo "Готово. Логи: ssh ${TARGET} '${SUDO} journalctl -u ${SERVICE} -f'"
