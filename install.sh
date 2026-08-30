#!/usr/bin/env bash
# One-command install for VPS Multitest Bot.
#   curl -fsSL https://raw.githubusercontent.com/Theraf1u/vps-multitest-bot/main/install.sh | bash
#
# Clones the repo (if not already inside it), asks for the required secrets once, writes
# .env, and brings the stack up with docker compose. Safe to re-run: it never overwrites an
# existing .env, and `docker compose up -d --build` just rebuilds in place.
set -euo pipefail

REPO_URL="https://github.com/Theraf1u/vps-multitest-bot.git"
DIR_NAME="vps-multitest-bot"

if [ -f "docker-compose.yml" ] && [ -f "bot/main.py" ]; then
  echo "==> Уже внутри репозитория, использую текущую директорию."
elif [ -d "$DIR_NAME" ]; then
  echo "==> Каталог $DIR_NAME уже существует, захожу в него."
  cd "$DIR_NAME"
else
  echo "==> Клонирую $REPO_URL..."
  git clone "$REPO_URL" "$DIR_NAME"
  cd "$DIR_NAME"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "❌ Docker не найден. Установите Docker + docker compose и запустите скрипт снова." >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "==> Настраиваю .env"
  cp .env.example .env

  read -rp "BOT_TOKEN (из @BotFather): " bot_token
  read -rp "ADMIN_ID (ваш числовой Telegram user id): " admin_id
  master_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null || openssl rand -base64 32)"

  sed -i.bak "s|^BOT_TOKEN=.*|BOT_TOKEN=${bot_token}|" .env
  sed -i.bak "s|^ADMIN_ID=.*|ADMIN_ID=${admin_id}|" .env
  sed -i.bak "s|^MASTER_ENCRYPTION_KEY=.*|MASTER_ENCRYPTION_KEY=${master_key}|" .env
  rm -f .env.bak

  echo "==> .env создан (MASTER_ENCRYPTION_KEY сгенерирован автоматически)."
  echo "    OPENROUTER_API_KEY можно добавить позже прямо из админ-панели бота."
else
  echo "==> .env уже существует, не трогаю."
fi

echo "==> Собираю и запускаю контейнер..."
docker compose up -d --build

echo ""
echo "✅ Готово. Логи: docker compose logs -f"
echo "   Обновить бота позже: bash update.sh"
