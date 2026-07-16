# ИИ-анализатор задач Yandex Tracker для 1С-разработки

Автономный CLI-инструмент: берёт задачи из очереди трекера, анализирует постановку,
скриншоты, вики-документацию и **код конфигурации 1С** (через
[onec-vecgraph](https://github.com/Ailirag/onec-vecgraph) lite / MCP), после чего создаёт
подзадачу-отчёт с компонентой **«ИИ анализ»** и помечает родителя тегами.

LLM-провайдеры взаимозаменяемы: **z.ai (GLM), Yandex, Claude, OpenAI** — выбор в
`config/providers.yaml` или флагами CLI. Роли `analyst` (текст+инструменты) и `vision`
(скриншоты) назначаются независимо — немультимодальные модели работают в паре с
vision-сайдкаром, либо картинки помечаются как непроанализированные.

**Агентский цикл.** Аналитик сам исследует контекст read-only инструментами: код 1С
(поиск, структура объектов, граф вызовов) и навигация по трекеру/вики
(`tracker_get_issue`, `tracker_search_issues`, `wiki_get_page`). Если задача ссылается на
другую («в рамках задачи 1915»), модель читает её сама; скриншоты вложенной задачи при этом
разбираются vision-моделью **внутри инструмента** и возвращаются готовым текстом. Чтение
ограничено белым списком очередей (`navigation.allowed_queues`). Расход токенов считается
раздельно по ролям analyst/vision — в журнале прогонов и в итоговой сводке CLI.

## Workflow

| Команда | Что делает |
|---|---|
| `analyzer bugs` | Незакрытые «Ошибки»: понимание проблемы → вероятная причина → затронутые объекты → сложность (простая/сложная) → драфт решения. Отбор: `--selection no-done-tag` (все без тега `ИИ_анализ_проведен`) или `--selection trigger-tag` (только с тегом `к_анализу_ии`) |
| `analyzer ft` | Задачи с тегом `К анализу ИИ` (ФТ готово, ссылка в поле «Ссылка на документацию»): полнота ФТ → маппинг на конфигурацию → драфт плана реализации → риски |

После обработки: подзадача с отчётом (видимость ограничивается компонентой — настройка в UI очереди),
родителю теги `ИИ_анализ_проведен`/`ИИ_анализ_ФТ_проведен` + `ИИ-простая`/`ИИ-сложная`,
тег-триггер снимается.

## Установка

```powershell
# 1. Зависимости Python
uv sync

# 2. Вендорим onec-vecgraph (анализ кода 1С)
git clone https://github.com/Ailirag/onec-vecgraph vendor/onec-vecgraph
uv sync --directory vendor/onec-vecgraph   # его собственные зависимости

# 3. Секреты — переменные окружения ОС ИЛИ файл .env в корне (cp .env.example .env).
#    .env грузится при старте; переменные ОС имеют приоритет. .env в .gitignore.
#    YATRACKER_TOKEN_GT, YATRACKER_ORGID_GT  — трекер/вики
#    ZAI_API_KEY                             — z.ai (GLM-5.2 / GLM-4.6V)
#    YANDEX_API_KEY, YANDEX_FOLDER_ID        — Yandex AI Studio (опционально)
#    ANTHROPIC_API_KEY / OPENAI_API_KEY      — опционально

# 4. Путь к выгрузке конфигурации УТ (XML Конфигуратора, git-репозиторий):
#    config/analyzer.yaml -> onec.dump_path
#    Затем укажите этот путь в onec-lite (веб-админка):
uv run --directory vendor/onec-vecgraph onec-lite admin   # http://localhost:8010

# 5. Самопроверка
uv run analyzer preflight
uv run analyzer llm-test --provider zai

# 6. Компонента «ИИ анализ» (одноразово; видимость настроить в UI очереди)
uv run analyzer init-component
```

## Использование

```powershell
# Dry-run (по умолчанию): отчёты падают в journal/dry-run/*.md, трекер не трогается
uv run analyzer bugs --limit 3
uv run analyzer bugs --issue ONE-4740          # одна конкретная задача

# Переключение моделей на лету
uv run analyzer bugs --analyst yandex/yandexgpt --vision none

# Боевой режим: сначала в config/analyzer.yaml поставить mode: live, затем
uv run analyzer bugs --limit 5 --live
uv run analyzer bugs --selection trigger-tag --live   # только помеченные к_анализу_ии
uv run analyzer ft --live

# Бюджет агентных шагов по коду
uv run analyzer bugs --max-steps 25 --limit 1
```

## Расписание

`scripts/run_scheduled.cmd` + Планировщик Windows (см. комментарий в файле).
Лог планировщика: `journal/scheduler.log`; журнал прогонов: `journal/runs.jsonl`;
аудит записей в трекер: `journal/writes.jsonl`.

## Безопасность

- **Write-guard**: существующие задачи изменяются только тегами; полный доступ — только к
  подзадачам, созданным в текущем прогоне; создание — только подзадач с компонентой и unique-ключом.
- LLM не имеет пишущих инструментов (только read-only: код 1С и навигация по трекеру/вики с
  guard по белому списку очередей) — инъекция из текста задачи не может ничего записать
  или увести анализ в чужую очередь.
- Идемпотентность: тег-фильтр → поиск существующей ИИ-подзадачи → `unique` (повторный POST → 409 = успех).
- Двойной предохранитель live-режима: `mode: live` в конфиге **и** флаг `--live`.
- Откат: все ИИ-подзадачи находятся запросом `Components: "ИИ анализ"`.

## Структура

```
config/            analyzer.yaml (параметры), providers.yaml (LLM)
src/analyzer/      cli, pipeline, tracker (+write-guard), wiki, onec (MCP), llm/, report
templates/         шаблоны отчётов (YFM)
journal/           runs.jsonl, writes.jsonl, dry-run/*.md, scheduler.log  [не в git]
work/              временные файлы прогонов                               [не в git]
vendor/            onec-vecgraph (клон)                                   [не в git]
```
