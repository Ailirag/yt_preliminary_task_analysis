#!/usr/bin/env bash
# Init + запуск демона в одном контейнере:
#   1) МУЛЬТИ-СИСТЕМА (config.systems заполнен): сгенерировать state-файл onec-lite, клонировать
#      зеркала (sync --once) и построить FTS по КАЖДОМУ воркспейсу;
#   2) ОДНО-ВОРКСПЕЙСНЫЙ режим (systems пуст): построить один FTS-индекс по $DUMP (как раньше);
#   3) exec'нуть демон как console-script (PID получает SIGTERM от `docker stop` -> graceful).
set -euo pipefail

DUMP="${ONEC_DUMP_PATH:-/data/dump}"
STATE="${ONEC_LITE_STATE:-/data/onec-lite/config.json}"
FTS_DIR="$(dirname "$STATE")/fts"
MIRRORS="$(dirname "$STATE")/mirrors"
ANALYZER=/app/.venv/bin/analyzer
# onec-vecgraph (typer CLI) для serve-lite --build-fts; onec-lite (launcher) для sync.
VEC=(uv run --no-sync --directory vendor/onec-vecgraph onec-vecgraph)
LITE=(uv run --no-sync --directory vendor/onec-vecgraph onec-lite)

# Мульти-система? gen-workspaces пишет state-файл из config.systems (exit 0 = есть ≥1 воркспейс).
if "$ANALYZER" gen-workspaces; then
  echo "[entrypoint] мульти-воркспейс: обновляю зеркала (onec-lite sync --once)…"
  "${LITE[@]}" sync --once --pull \
    || echo "[entrypoint] ВНИМАНИЕ: sync --once завершился с ошибкой (часть репозиториев недоступна?)."
  # FTS по каждому клонированному зеркалу. Маркер .indexed-<имя> пропускает уже построенные при
  # рестарте (сервер сам дообновляет индекс по mtime; sync подтягивает свежий код).
  shopt -s nullglob
  for mdir in "$MIRRORS"/*/; do
    name="$(basename "$mdir")"
    root="$MIRRORS/$name"                       # без хвостового слэша — тот же путь, что у сервера (digest FTS)
    [ -d "$root/.git" ] || continue
    if [ -f "$FTS_DIR/.indexed-$name" ]; then
      echo "[entrypoint] FTS воркспейса '$name' уже построен — пропускаю."
      continue
    fi
    echo "[entrypoint] строю FTS воркспейса '$name' (root=$root)…"
    if "${VEC[@]}" serve-lite --root "$root" --build-fts --check; then
      mkdir -p "$FTS_DIR" && touch "$FTS_DIR/.indexed-$name"
    else
      echo "[entrypoint] ВНИМАНИЕ: FTS воркспейса '$name' не построен — fts_search по нему недоступен."
    fi
  done
  shopt -u nullglob
else
  # Одно-воркспейсный режим (systems пуст): один индекс по $DUMP, как прежде.
  # Строим только если его ещё нет (том persistent) — иначе каждый рестарт заново сканирует
  # десятки тысяч файлов (особенно медленно при bind-mount с Windows/drvfs).
  if ls "$FTS_DIR"/*.db >/dev/null 2>&1; then
    echo "[entrypoint] FTS-индекс уже построен ($FTS_DIR) — пропускаю построение."
  elif [ -d "$DUMP" ] && [ -n "$(ls -A "$DUMP" 2>/dev/null)" ]; then
    echo "[entrypoint] onec-lite: строю FTS-индекс (root=$DUMP)…"
    # Тот же --root, что и в рантайме (analyzer.yaml: onec.dump_path=$DUMP) — иначе digest БД не совпадёт.
    "${VEC[@]}" serve-lite --root "$DUMP" --build-fts --check \
      || echo "[entrypoint] ВНИМАНИЕ: построить FTS-индекс не удалось — fts_search может быть недоступен."
  else
    echo "[entrypoint] ВНИМАНИЕ: выгрузка $DUMP пуста или не смонтирована — индекс не строю."
  fi
fi

echo "[entrypoint] запускаю демон: analyzer watch --live"
# Console-script = прямой python-процесс -> SIGTERM доходит до наших обработчиков (graceful stop, снятие лока).
exec /app/.venv/bin/analyzer watch --live
