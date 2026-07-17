# Разворот на продовой машине (Linux + systemd)

ИИ-анализатор на проде работает как **резидентный демон** `analyzer watch`: один процесс
держит tracker/wiki/onec-lite «тёплыми» и раз в `interval_s` секунд опрашивает трекер.
Тик дешёвый (сначала `issues_count`); тяжёлый разбор запускается только при наличии кандидатов.

## Почему демон, а не «крон каждые 30 секунд»

Один разбор бага идёт ~7 минут. Наивный запуск по расписанию каждые N секунд приводит к
**наложению прогонов** (двойная оплата LLM, гонки при записи). Демон решает это:
- **single-instance lock** (`work/analyzer.lock`, PID + heartbeat) — параллельные прогоны исключены;
- **последовательная** обработка, `max_issues_per_run` ограничивает объём и стоимость тика;
- **дешёвая предпроверка** — при отсутствии кандидатов onec-lite и LLM не трогаются.

## Предпосылки

- Linux x86-64, `git`, [`uv`](https://docs.astral.sh/uv/) (Python 3.12 uv поставит сам).
- Сетевой доступ к `api.tracker.yandex.net`, `api.z.ai` (и/или `*.api.cloud.yandex.net`).
- **Выгрузка конфигурации 1С (XML)** на диске прода — см. раздел ниже.

## Шаги

### 1. Код и зависимости
```bash
sudo useradd -r -s /usr/sbin/nologin analyzer
sudo git clone https://github.com/Ailirag/yt_preliminary_task_analysis /opt/analyzer
cd /opt/analyzer
sudo -u analyzer uv sync
sudo chown -R analyzer:analyzer /opt/analyzer
```

### 2. Выгрузка конфигурации 1С (внешняя зависимость!)
На Linux **нет Конфигуратора 1С**, поэтому XML-выгрузку УТ формируют на Windows-машине с 1С и
доставляют на прод (git-репозиторий выгрузки / `rsync` / сетевая шара). Это должно быть
**регулярным процессом** — качество анализа завязано на актуальность конфигурации.
Путь к выгрузке указывается в `config/analyzer.yaml` → `onec.dump_path` (напр. `/srv/1c/UT_config`).
`onec-lite` читает «живую» копию с диска, поэтому обновление выгрузки подхватывается без рестарта демона.

### 3. Секреты (не в git)
```bash
sudo mkdir -p /etc/analyzer
sudo tee /etc/analyzer/analyzer.env >/dev/null <<'ENV'
YATRACKER_TOKEN_GT=...
YATRACKER_ORGID_GT=...
ZAI_API_KEY=...
# YANDEX_API_KEY=...        # если используется профиль yandex / z.ai-yandex
# YANDEX_FOLDER_ID=...
ENV
sudo chown analyzer:analyzer /etc/analyzer/analyzer.env
sudo chmod 600 /etc/analyzer/analyzer.env
```

### 4. Конфиг `config/analyzer.yaml`
- `onec.dump_path` — путь к выгрузке (п.2). **Машинно-локальный, в git не коммитится.**
- `watch:` — `interval_s`, `workflow: bugs`, `selection: trigger-tag`, `daily_budget` (потолок $/₽ за сутки),
  `work_hours` (напр. `"08:00-20:00"`), `lock_file`.
- `bugs.trigger_authors` — кто может запускать анализ тегом (e-mail; см. основной README).
- `mode` пока оставить `dry-run` для проверки.

### 5. Проверка до боевого режима
```bash
sudo -u analyzer bash -lc 'cd /opt/analyzer && set -a && . /etc/analyzer/analyzer.env && set +a && uv run analyzer preflight'
# dry-run одного-двух прогонов, ревью отчётов:
sudo -u analyzer bash -lc 'cd /opt/analyzer && set -a && . /etc/analyzer/analyzer.env && set +a && uv run analyzer bugs --limit 3'
ls journal/dry-run/    # просмотреть сгенерированные отчёты
```

### 6. Боевой режим
- В `config/analyzer.yaml` выставить `mode: live` (демон запускается с `--live` — двойной предохранитель).
- Установить сервис:
```bash
sudo cp deploy/analyzer.service /etc/systemd/system/analyzer.service
# при необходимости поправить пути (WorkingDirectory, ExecStart -> which uv) и User
sudo systemctl daemon-reload
sudo systemctl enable --now analyzer
journalctl -u analyzer -f
```

## Эксплуатация

| Действие | Команда |
|---|---|
| Логи | `journalctl -u analyzer -f` |
| Остановка (мягкая, SIGTERM) | `sudo systemctl stop analyzer` |
| Рестарт | `sudo systemctl restart analyzer` |
| Обновление кода | `git pull && uv sync && sudo systemctl restart analyzer` |
| Расход за сутки | `cat work/daily_spend.json` |
| Журнал прогонов | `work/../journal/runs.jsonl` (по задаче + `kind=run_summary`) |
| Аудит записей в трекер | `journal/writes.jsonl` |
| Снять зависший лок | остановить сервис; при необходимости `rm work/analyzer.lock` |

## Предохранители (действуют на проде)

- **Двойной live-предохранитель:** `mode: live` в конфиге И флаг `--live` в `ExecStart`.
- **Single-instance lock** — нет наложения прогонов.
- **`daily_budget`** — аварийная пауза при превышении дневного лимита стоимости.
- **`work_hours`** — не жечь бюджет вне рабочего окна.
- **`selection: trigger-tag` + `trigger_authors`** — реагируем только на осознанно помеченные задачи.
- **write-guard** в трекере: на чужих задачах — только теги; полный доступ — только к своим подзадачам.
- **`max_consecutive_errors`** — прогон аварийно останавливается при серии ошибок; демон делает backoff.

---

# Вариант Б: Docker (compose) — один контейнер

Альтернатива systemd. Демон и вложенный onec-lite (со своим FTS-индексом) — в одном контейнере;
entrypoint строит индекс, если его нет, затем запускает `analyzer watch`. Файлы: `deploy/Dockerfile`,
`deploy/entrypoint.sh`, `deploy/docker-compose.yml`, `.dockerignore` (в корне).

## Предпосылки

- Docker Engine + `docker compose` v2 на хосте, **пользователь в группе `docker`** (иначе socket
  permission denied): однократно админом `sudo usermod -aG docker <user>` → перелогиниться.
- Исходящий доступ к `github.com`, `registry-1.docker.io`, `pypi.org` (сборка) и
  `api.tracker.yandex.net`, `api.z.ai` / `*.api.cloud.yandex.net` (рантайм).
- **Диск и общий демон.** Если Docker на хосте общий (рядом чужие контейнеры, напр. infinity/qdrant) —
  **не меняйте data-root и не перезапускайте демон** (уроните чужие сервисы). Образ (~1 ГБ) ляжет на
  раздел Docker (`/var/lib/docker`), а все данные (индекс ~1 ГБ, выгрузка УТ — возможно несколько ГБ,
  журналы) держите на отдельном большом диске через bind-маунты (в этом compose — `/mnt/storage/analyzer/*`).
  Периодически `docker builder prune`.

## Раскладка на хосте (пример под эту машину)

```
/home/adm_garipov_ir/analyzer                  # клон ЭТОГО репозитория (контекст сборки) + прод-config
/home/adm_garipov_ir/analyzer.env              # секреты, chmod 600
/mnt/storage/analyzer/dump       -> /data/dump:ro     # git-выгрузка УТ (clone/pull)   [владелец: пользователь]
/mnt/storage/analyzer/onec-lite  -> /data/onec-lite   # FTS-индекс (~1 ГБ)             [владелец: 10001]
/mnt/storage/analyzer/work       -> /app/work         # бюджет/лок/user_map            [владелец: 10001]
/mnt/storage/analyzer/journal    -> /app/journal      # журналы прогонов               [владелец: 10001]
```

### Подготовка каталогов данных (однократно, админом — БЕЗ перезапуска демона)
Контейнер работает под uid **10001**; каталоги под запись должны принадлежать ему, а выгрузку
(куда пользователь делает git clone/pull) оставляем пользователю:
```bash
sudo mkdir -p /mnt/storage/analyzer/{dump,onec-lite,work,journal}
sudo chown adm_garipov_ir:"$(id -gn adm_garipov_ir)" /mnt/storage/analyzer/dump
sudo chown -R 10001:10001 /mnt/storage/analyzer/onec-lite /mnt/storage/analyzer/work /mnt/storage/analyzer/journal
```

### Выгрузка 1С (git-репо, обновляется pull)
```bash
git clone --depth 1 <URL-репозитория-выгрузки> /mnt/storage/analyzer/dump
# обновление (в вашем pipeline/cron), без разрастания истории:
git -C /mnt/storage/analyzer/dump fetch --depth 1 origin && \
git -C /mnt/storage/analyzer/dump reset --hard origin/HEAD
```
`onec-lite` читает «живую» копию с диска; после `reset` изменения подхватываются авто-догоном индекса
(инкрементально, ~30 с во время поиска). git-инструменты (`changed_objects`/`review_set`) работают,
т.к. в образе есть `git` и настроен `safe.directory`.

### Прод-конфиг `/home/adm_garipov_ir/analyzer/config/analyzer.yaml` — обязательные правки
```yaml
mode: live
onec:
  dump_path: "/data/dump"        # РОВНО так (digest FTS-индекса зависит от строки пути)
bugs:
  trigger_authors: ["garipov_ir@grandtrade.world"]   # кто вправе запускать анализ тегом
watch:
  workflow: bugs
  selection: trigger-tag
  interval_s: 30
  daily_budget: 5.0              # потолок в валюте аналитика ($ для профиля z.ai); null — без лимита
  work_hours: "08:00-20:00"
```

## Сборка и запуск
```bash
cd /home/adm_garipov_ir/analyzer  # клон этого репозитория (если приватный — токен/deploy-key или rsync с dev-машины)
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f          # первый старт: ~3.4 мин на построение FTS
```
Проверка состояния и корректности:
```bash
docker compose -f deploy/docker-compose.yml exec analyzer /app/.venv/bin/analyzer status
```

## Эксплуатация

| Действие | Команда |
|---|---|
| Логи | `docker compose -f deploy/docker-compose.yml logs -f` |
| Статус | `... exec analyzer /app/.venv/bin/analyzer status` |
| Остановка (graceful, SIGTERM≤600с) | `docker compose -f deploy/docker-compose.yml down` |
| Обновить код | `git -C /home/adm_garipov_ir/analyzer pull && docker compose -f deploy/docker-compose.yml up -d --build` |
| Обновить выгрузку | `git -C /mnt/storage/analyzer/dump fetch --depth 1 origin && git -C /mnt/storage/analyzer/dump reset --hard origin/HEAD` |
| Расход за сутки | `/mnt/storage/analyzer/work/daily_spend.json` |

## Заметки

- **Двойной предохранитель live** сохраняется: `mode: live` в конфиге **и** `--live` в entrypoint.
- **Прокси:** при доступе только через прокси раскомментируйте `HTTP_PROXY/HTTPS_PROXY/NO_PROXY` в
  compose — наши http-клиенты (httpx + SDK) их уважают, код менять не нужно.
- **Индекс в `/mnt/storage/analyzer/onec-lite`** переживает пересоздание контейнера (не пересобираем каждый раз).
  При смене пути монтирования выгрузки индекс придётся построить заново (digest завязан на `/data/dump`).
- **Секреты** только через `env_file`, в образ не попадают; `work/`/`journal/` исключены `.dockerignore`.

