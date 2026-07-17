#!/usr/bin/env bash
# Init + запуск демона в одном контейнере:
#   1) строим FTS-индекс onec-lite, если его нет (иначе fts_search вернёт ошибку);
#   2) exec'аем демон как console-script (PID получает SIGTERM от `docker stop` -> graceful).
set -euo pipefail

DUMP="${ONEC_DUMP_PATH:-/data/dump}"
STATE="${ONEC_LITE_STATE:-/data/onec-lite/config.json}"
FTS_DIR="$(dirname "$STATE")/fts"

# Строим индекс только если его ещё нет (том persistent). Иначе пропускаем — иначе каждый рестарт
# заново сканирует десятки тысяч файлов (особенно медленно при bind-mount с Windows/drvfs).
# Обновления выгрузки подхватываются авто-догоном onec-lite (mtime, ~30 с во время поиска).
if ls "$FTS_DIR"/*.db >/dev/null 2>&1; then
  echo "[entrypoint] FTS-индекс уже построен ($FTS_DIR) — пропускаю построение."
elif [ -d "$DUMP" ] && [ -n "$(ls -A "$DUMP" 2>/dev/null)" ]; then
  echo "[entrypoint] onec-lite: строю FTS-индекс (root=$DUMP)…"
  # Тот же --root, что и в рантайме (analyzer.yaml: onec.dump_path=$DUMP) — иначе digest БД не совпадёт.
  uv run --no-sync --directory vendor/onec-vecgraph \
      onec-vecgraph serve-lite --root "$DUMP" --build-fts --check \
    || echo "[entrypoint] ВНИМАНИЕ: построить FTS-индекс не удалось — fts_search может быть недоступен."
else
  echo "[entrypoint] ВНИМАНИЕ: выгрузка $DUMP пуста или не смонтирована — индекс не строю."
fi

echo "[entrypoint] запускаю демон: analyzer watch --live"
# Console-script = прямой python-процесс -> SIGTERM доходит до наших обработчиков (graceful stop, снятие лока).
exec /app/.venv/bin/analyzer watch --live
