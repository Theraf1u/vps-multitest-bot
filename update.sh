#!/usr/bin/env bash
# Checks for and installs bot updates from git: `bash update.sh` (or `bash update.sh --check`
# to only report whether an update is available, without touching anything).
set -euo pipefail

if [ ! -d ".git" ]; then
  echo "❌ Запускать из корня клонированного репозитория (нет .git здесь)." >&2
  exit 1
fi

echo "==> Проверяю обновления..."
git fetch origin

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse origin/main)"

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "✅ Уже последняя версия ($(git rev-parse --short HEAD))."
  exit 0
fi

echo "🔄 Доступно обновление:"
git log --oneline "$LOCAL..$REMOTE"

if [ "${1:-}" = "--check" ]; then
  echo ""
  echo "Для установки: bash update.sh"
  exit 0
fi

echo "==> Обновляю..."
git pull --ff-only origin main
docker compose build
docker compose up -d

echo "✅ Обновлено до $(git rev-parse --short HEAD). Логи: docker compose logs -f"
