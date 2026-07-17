#!/usr/bin/env bash
# Init + запуск демона в одном контейнере:
#   1) строим FTS-индекс onec-lite, если его нет (иначе fts_search вернёт ошибку);
#   2) exec'аем демон как console-script (PID получает SIGTERM от `docker stop` -> graceful).
set -euo pipefail

DUMP="${ONEC_DUMP_PATH:-/data/dump}"

if [ -d "$DUMP" ] && [ -n "$(ls -A "$DUMP" 2>/dev/null)" ]; then
  echo "[entrypoint] onec-lite: проверяю/строю FTS-индекс (root=$DUMP)…"
  # Тот же --root, что и в рантайме (analyzer.yaml: onec.dump_path=$DUMP) — иначе digest БД не совпадёт.
  if uv run --no-sync --directory vendor/onec-vecgraph \
        onec-vecgraph serve-lite --root "$DUMP" --build-fts --check; then
    echo "[entrypoint] FTS-индекс готов."
  else
    echo "[entrypoint] ВНИМАНИЕ: построить FTS-индекс не удалось — fts_search может быть недоступен, остальной анализ работает."
  fi
else
  echo "[entrypoint] ВНИМАНИЕ: выгрузка $DUMP пуста или не смонтирована — индекс не строю."
fi

echo "[entrypoint] запускаю демон: analyzer watch --live"
# Console-script = прямой python-процесс -> SIGTERM доходит до наших обработчиков (graceful stop, снятие лока).
exec /app/.venv/bin/analyzer watch --live
