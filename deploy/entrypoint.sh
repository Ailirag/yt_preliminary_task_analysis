#!/usr/bin/env bash
# Единый entrypoint для двух ролей (различаются первым аргументом = compose command):
#   (без аргументов) — демон: init FTS-индексов + `analyzer watch --live`;
#   sync [args...]    — сайдкар обновления зеркал: `onec-lite sync <args>`.
# Общий шаг для обеих ролей — настройка git-кредов для приватных репозиториев 1С.
set -euo pipefail

DUMP="${ONEC_DUMP_PATH:-/data/dump}"
STATE="${ONEC_LITE_STATE:-/data/onec-lite/config.json}"
FTS_DIR="$(dirname "$STATE")/fts"
MIRRORS="$(dirname "$STATE")/mirrors"
ANALYZER=/app/.venv/bin/analyzer
# onec-vecgraph (typer CLI) для serve-lite --build-fts; onec-lite (launcher) для sync.
VEC=(uv run --no-sync --directory vendor/onec-vecgraph onec-vecgraph)
LITE=(uv run --no-sync --directory vendor/onec-vecgraph onec-lite)

# --- git-креды для приватных зеркал 1С (значение только из env CORP_GIT_CREDENTIALS, в лог НЕ пишем) ---
# Формат значения: строка(и) git credential-store, напр. https://user:token@git.corp.example
# Пусто (публичные репозитории / одно-воркспейсный режим) — шаг пропускается.
if [ -n "${CORP_GIT_CREDENTIALS:-}" ]; then
  umask 077
  printf '%s\n' "$CORP_GIT_CREDENTIALS" > "$HOME/.git-credentials"
  git config --global credential.helper store
  echo "[entrypoint] git-креды для corp-репозиториев настроены (helper store)."
fi

# --- роль sync-сайдкара: entrypoint.sh sync [--interval N | --at HH:MM ...] [--pull] ---
if [ "${1:-}" = "sync" ]; then
  shift
  echo "[entrypoint] роль onec-sync: onec-lite sync $*"
  exec "${LITE[@]}" sync "$@"
fi

# --- роль демона: построить индексы, затем запустить watch ---
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
