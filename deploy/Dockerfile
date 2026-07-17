# syntax=docker/dockerfile:1
# Образ ИИ-анализатора: демон `analyzer watch` + вложенный onec-lite (FTS-индекс).
# Один контейнер (init-шаг строит индекс, затем запускается демон). Прод — Linux/x86-64.

########################## build ##########################
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /app

# Полный исходник (venv'ы исключены через .dockerignore и создаются здесь заново).
COPY . .
# Основное приложение (без dev-зависимостей — pytest не нужен в образе).
RUN uv sync --frozen --no-dev
# Вложенный onec-vecgraph (свой проект/venv; его запускает анализатор через `uv run --directory`).
RUN uv sync --no-dev --directory vendor/onec-vecgraph

########################## runtime ##########################
FROM python:3.12-slim AS runtime
# git — для onec-lite changed_objects/review_set; ripgrep — ускоряет search_code (опционально).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv нужен в рантайме: анализатор спавнит onec-lite как `uv run --directory ... onec-lite stdio`.
COPY --from=build /bin/uv /bin/uv

RUN useradd -r -m -u 10001 -d /home/app app
WORKDIR /app
COPY --from=build /app /app

# Каталоги состояния/данных (монтируются томами в compose) + нормализация entrypoint
# (файл мог прийти с CRLF/без exec-бита с Windows).
RUN mkdir -p /data/dump /data/onec-lite /app/work /app/journal \
    && sed -i 's/\r$//' /app/deploy/entrypoint.sh \
    && chmod +x /app/deploy/entrypoint.sh \
    && chown -R app:app /app /data \
    # выгрузка примонтирована RO и принадлежит другому uid — разрешаем git читать её
    && git config --system --add safe.directory /data/dump

ENV ONEC_LITE_STATE=/data/onec-lite/config.json \
    ONEC_DUMP_PATH=/data/dump \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

USER app
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
