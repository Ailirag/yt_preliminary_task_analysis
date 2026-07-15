"""Пер-задачный пайплайн анализа: выборка -> досье -> vision -> агентный цикл -> отчёт -> запись."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import AnalyzerCfg, ProvidersCfg
from .journal import Journal, now_iso
from .llm import ImagePart, Msg, Provider, ToolSpec, extract_json
from .llm.base import truncate
from .models import AnalysisResult, normalize_analysis
from .onec import OnecMCP
from .prompts import VISION_PROMPT, bug_system_prompt, ft_system_prompt
from .report import render_report
from .tracker import TrackerClient
from .wiki import WikiClient, extract_wiki_urls

log = logging.getLogger("analyzer.pipeline")

IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}


@dataclass
class RunContext:
    acfg: AnalyzerCfg
    pcfgs: ProvidersCfg
    tracker: TrackerClient
    wiki: WikiClient
    onec: OnecMCP | None
    journal: Journal
    analyst: Provider
    vision: Provider | None
    live: bool
    max_steps: int
    component_id: int
    project_root: Path
    # расход токенов раздельно по ролям моделей (analyst — GLM-5.2, vision — GLM-4.6V)
    usage: dict = field(default_factory=lambda: {
        "analyst": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        "vision": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
    })
    vision_calls: int = 0                                 # обращений к vision в рамках текущего анализа
    nav_log: list = field(default_factory=list)           # след навигации (для отчёта/журнала)
    related_cache: dict = field(default_factory=dict)     # ключ задачи -> готовый текст (кэш на прогон)

    def add_usage(self, role: str, usage: dict) -> None:
        bucket = self.usage.setdefault(role, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        bucket["input_tokens"] += int(usage.get("input_tokens") or 0)
        bucket["output_tokens"] += int(usage.get("output_tokens") or 0)
        bucket["calls"] += 1

    def usage_snapshot(self) -> dict:
        """Глубокая копия счётчиков (для вычисления пер-задачной дельты)."""
        return {role: dict(vals) for role, vals in self.usage.items()}


# ---------- выборка ----------

def build_query(acfg: AnalyzerCfg, workflow: str, selection: str) -> str:
    q = acfg.queue
    if workflow == "bugs":
        b = acfg.bugs
        if selection == "trigger-tag":
            return (f'Queue: {q} Type: Ошибка Resolution: empty() '
                    f'Tags: "{b.trigger_tag}" Tags: !"{b.done_tag}" "Sort by": Updated ASC')
        return (f'Queue: {q} Type: Ошибка Resolution: empty() '
                f'Tags: !"{b.done_tag}" "Sort by": Updated ASC')
    f = acfg.ft
    return f'Queue: {q} Resolution: empty() Tags: "{f.trigger_tag}" "Sort by": Updated ASC'


def select_issues(ctx: RunContext, workflow: str, selection: str, limit: int,
                  issue_key: str | None) -> list[dict]:
    if issue_key:
        return [ctx.tracker.get_issue(issue_key)]
    query = build_query(ctx.acfg, workflow, selection)
    log.info("Выборка: %s", query)
    raw = ctx.tracker.search(query, per_page=min(50, max(limit * 3, 10)), max_pages=1)
    out: list[dict] = []
    for issue in raw:
        tags = issue.get("tags") or []
        if workflow == "bugs":
            b = ctx.acfg.bugs
            type_key = ((issue.get("type") or {}).get("key") or "").lower()
            if type_key and type_key != "bug":
                continue
            if b.done_tag in tags:
                continue
            if selection == "trigger-tag" and b.trigger_tag not in tags:
                continue
        else:
            if ctx.acfg.ft.trigger_tag not in tags:
                continue
        out.append(issue)
        if len(out) >= limit:
            break
    return out


# ---------- досье ----------

def _issue_text_blob(issue: dict, comments: list[dict]) -> str:
    parts = [issue.get("description") or ""]
    for c in comments:
        parts.append(c.get("text") or "")
    return "\n".join(parts)


def _download_images(ctx: RunContext, key: str, attachments: list[dict]) -> tuple[list[ImagePart], list[str]]:
    """Скачивает картинки-вложения задачи в пределах лимитов. Возвращает (картинки, пропущенные)."""
    lim = ctx.acfg.limits
    images: list[ImagePart] = []
    skipped: list[str] = []
    max_bytes = lim.max_image_mb * 1024 * 1024
    for att in attachments:
        mime = (att.get("mimetype") or "").lower()
        name = att.get("name") or "?"
        if mime not in IMAGE_MIMES:
            skipped.append(f"{name} ({mime or 'без типа'} — не картинка)")
            continue
        if len(images) >= lim.max_images_per_issue:
            skipped.append(f"{name} (превышен лимит {lim.max_images_per_issue})")
            continue
        size = int(att.get("size") or 0)
        if size > max_bytes:
            skipped.append(f"{name} (размер {size // 1024**2}МБ > лимита)")
            continue
        try:
            data = ctx.tracker.download_attachment(key, att)
            images.append(ImagePart(data=data, mime="image/jpeg" if mime == "image/jpg" else mime))
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{name} (ошибка скачивания: {e})")
    return images, skipped


def build_dossier(ctx: RunContext, issue: dict, workflow: str) -> tuple[str, list[ImagePart], dict]:
    """Возвращает (текст досье, картинки, sources-метаданные)."""
    lim = ctx.acfg.limits
    key = issue["key"]
    comments = ctx.tracker.get_comments(key)
    links = ctx.tracker.get_links(key)
    attachments = ctx.tracker.get_attachments(key)

    # --- картинки ---
    images, skipped_images = _download_images(ctx, key, attachments)

    # --- вики ---
    wiki_urls: list[str] = []
    doc_link = issue.get(ctx.acfg.wiki.doc_field) or ""
    if doc_link:
        wiki_urls.extend(extract_wiki_urls(doc_link, ctx.acfg.wiki.allowed_hosts) or [])
    for url in extract_wiki_urls(_issue_text_blob(issue, comments), ctx.acfg.wiki.allowed_hosts):
        if url not in wiki_urls:
            wiki_urls.append(url)
    wiki_urls = wiki_urls[: lim.max_wiki_pages_per_issue]
    wiki_pages = [ctx.wiki.get_page(u, lim.max_wiki_chars) for u in wiki_urls]

    # --- текст досье ---
    lines: list[str] = []
    lines.append(f"# Задача {key}: {issue.get('summary', '')}")
    status = (issue.get("status") or {}).get("display", "?")
    priority = (issue.get("priority") or {}).get("display", "?")
    author = (issue.get("createdBy") or {}).get("display", "?")
    lines.append(f"Статус: {status} | Приоритет: {priority} | Автор: {author} | "
                 f"Создана: {issue.get('createdAt', '?')}")
    if issue.get("tags"):
        lines.append(f"Теги: {', '.join(issue['tags'])}")
    if doc_link:
        lines.append(f"Ссылка на документацию: {doc_link}")

    lines.append("\n## Описание\n")
    lines.append(truncate(issue.get("description") or "(пусто)", 20000, "описание усечено"))

    if comments:
        lines.append(f"\n## Комментарии ({len(comments)})\n")
        budget = lim.max_comment_chars
        for c in comments:
            author_c = (c.get("createdBy") or {}).get("display", "?")
            text = (c.get("text") or "").strip()
            entry = f"**{author_c}** ({c.get('createdAt', '')}):\n{text}\n"
            if budget - len(entry) < 0:
                lines.append(f"[... остальные комментарии усечены, всего {len(comments)}]")
                break
            lines.append(entry)
            budget -= len(entry)

    if links:
        lines.append("\n## Связи\n")
        for lk in links:
            obj = lk.get("object") or {}
            rel = (lk.get("type") or {}).get("id", "связана")
            lines.append(f"- {rel}: {obj.get('key', '?')} — {obj.get('display', '')}")

    if wiki_pages:
        lines.append("\n## Вики-документация\n")
        for p in wiki_pages:
            if p["error"]:
                lines.append(f"### {p['url']}\n[недоступно: {p['error']}]\n")
            else:
                lines.append(f"### {p['title'] or p['slug']} ({p['url']})\n{p['content']}\n")

    sources = {
        "wiki_pages": wiki_pages,
        "skipped_images": skipped_images,
        "images_total": len(images),
        "comments": len(comments),
    }
    return "\n".join(lines), images, sources


# ---------- vision ----------

def analyze_images(ctx: RunContext, images: list[ImagePart], source: str = "задача") -> list[str]:
    """Сайдкар-описания скриншотов vision-моделью. Ошибки не фатальны, есть общий потолок вызовов."""
    descriptions: list[str] = []
    assert ctx.vision is not None
    cap = ctx.acfg.limits.max_vision_calls_per_issue
    for i, img in enumerate(images, 1):
        if ctx.vision_calls >= cap:
            descriptions.append(f"[лимит vision-вызовов ({cap}) исчерпан — скриншот {i} не разобран]")
            continue
        ctx.vision_calls += 1
        try:
            resp = ctx.vision.chat([
                Msg.system(VISION_PROMPT),
                Msg.user(f"Скриншот {i} ({source}):", img),
            ])
            ctx.add_usage("vision", resp.usage)
            descriptions.append(resp.text.strip() or "[пустой ответ vision-модели]")
        except Exception as e:  # noqa: BLE001
            log.warning("Vision-анализ (%s) картинки %d не удался: %s", source, i, e)
            descriptions.append(f"[ошибка vision-анализа: {e}]")
    return descriptions


# ---------- навигационные инструменты (read-only) ----------

def fetch_issue_context(ctx: RunContext, key: str) -> str:
    """Текст связанной задачи для аналитика: описание, комментарии, связи и описания её
    скриншотов (vision прогоняется здесь, т.к. аналитик текстовый). Кэшируется на прогон."""
    key = key.strip().upper()
    cached = ctx.related_cache.get(key)
    if cached is not None:
        return cached
    nav = ctx.acfg.navigation
    prefixes = [p.upper() for p in (nav.known_prefixes or [ctx.acfg.queue])]
    prefix = key.split("-", 1)[0] if "-" in key else ""
    if prefix not in prefixes:
        return (f"[доступ к задаче {key} не разрешён: читать можно только задачи с префиксами "
                f"{prefixes}. Задачи других очередей недоступны.]")
    try:
        issue = ctx.tracker.get_issue(key)
    except Exception as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", None)
        msg = {403: "нет доступа", 404: "не найдена"}.get(code, f"{type(e).__name__}: {e}")
        out = f"[задача {key}: {msg}]"
        ctx.related_cache[key] = out
        return out

    comments = ctx.tracker.get_comments(key)
    links = ctx.tracker.get_links(key)
    attachments = ctx.tracker.get_attachments(key)

    lines = [f"# Связанная задача {key}: {issue.get('summary', '')}"]
    status = (issue.get("status") or {}).get("display", "?")
    itype = (issue.get("type") or {}).get("display", "?")
    lines.append(f"Статус: {status} | Тип: {itype}")
    doc_link = issue.get(ctx.acfg.wiki.doc_field) or ""
    if doc_link:
        lines.append(f"Ссылка на документацию: {doc_link}")
    lines.append("\n## Описание\n")
    lines.append(truncate(issue.get("description") or "(пусто)", nav.max_issue_chars, "описание усечено"))
    if comments:
        lines.append(f"\n## Комментарии ({len(comments)})\n")
        budget = nav.max_issue_chars
        for c in comments:
            author = (c.get("createdBy") or {}).get("display", "?")
            entry = f"**{author}**: {(c.get('text') or '').strip()}\n"
            if budget - len(entry) < 0:
                lines.append(f"[... комментарии усечены, всего {len(comments)}]")
                break
            lines.append(entry)
            budget -= len(entry)
    if links:
        lines.append("\n## Связи\n")
        for lk in links:
            obj = lk.get("object") or {}
            rel = (lk.get("type") or {}).get("id", "связана")
            lines.append(f"- {rel}: {obj.get('key', '?')} — {obj.get('display', '')}")

    # скриншоты -> vision -> текст (внутри инструмента, аналитику вернётся готовый текст)
    images, skipped = _download_images(ctx, key, attachments)
    if images:
        if ctx.vision is not None:
            descs = analyze_images(ctx, images, source=f"задача {key}")
            lines.append("\n## Скриншоты (описания vision-модели)\n")
            for i, d in enumerate(descs, 1):
                lines.append(f"### Скриншот {i}\n{d}\n")
        else:
            lines.append(f"\n[скриншотов: {len(images)}; vision отключён — не разобраны]\n")
    if skipped:
        lines.append("\n[пропущенные вложения: " + "; ".join(skipped) + "]\n")

    out = truncate("\n".join(lines), ctx.acfg.limits.max_tool_result_chars,
                   "контекст связанной задачи усечён")
    ctx.related_cache[key] = out
    return out


_NAV_TOOLS = {"tracker_get_issue", "tracker_search_issues", "wiki_get_page"}


def navigation_tool_specs(nav) -> list[ToolSpec]:
    """ToolSpec'и read-only навигации согласно config.navigation.tools."""
    if not nav.enabled:
        return []
    specs: list[ToolSpec] = []
    if "get_issue" in nav.tools:
        specs.append(ToolSpec(
            name="tracker_get_issue",
            description="Прочитать другую задачу трекера по ключу (например ONE-1915): описание, "
                        "комментарии, связи и текстовые описания её скриншотов (разбираются автоматически). "
                        "Только чтение. Используй, когда задача ссылается на другую и это важно.",
            schema={"type": "object",
                    "properties": {"key": {"type": "string", "description": "Ключ задачи, напр. ONE-1915"}},
                    "required": ["key"]},
        ))
    if "search_issues" in nav.tools:
        specs.append(ToolSpec(
            name="tracker_search_issues",
            description="Найти задачи в очереди по условию Yandex Tracker Query Language БЕЗ 'Queue:' "
                        "(напр. Summary: \"заявление о ввозе\"). Возвращает ключ, тему, статус. Только чтение.",
            schema={"type": "object",
                    "properties": {"query": {"type": "string", "description": "Условие без Queue:"}},
                    "required": ["query"]},
        ))
    if "get_wiki" in nav.tools:
        specs.append(ToolSpec(
            name="wiki_get_page",
            description="Прочитать страницу Yandex Wiki по URL (напр. из поля «Ссылка на документацию»). "
                        "Только чтение.",
            schema={"type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"]},
        ))
    return specs


def _dispatch_navigation(ctx: RunContext, name: str, args: dict) -> str:
    try:
        if name == "tracker_get_issue":
            key = str(args.get("key", "")).strip()
            ctx.nav_log.append(f"issue:{key}")
            return fetch_issue_context(ctx, key) if key else "[не указан ключ задачи]"
        if name == "tracker_search_issues":
            q = str(args.get("query", "")).strip()
            ctx.nav_log.append(f"search:{truncate(q, 60, '')}")
            if not q:
                return "[пустой запрос]"
            full = q if q.lower().startswith("queue:") else f"Queue: {ctx.acfg.queue} {q}"
            n = ctx.acfg.navigation.max_search_results
            hits = ctx.tracker.search(full, per_page=n, max_pages=1)
            if not hits:
                return "[ничего не найдено]"
            return "\n".join(
                f"- {h.get('key')}: {truncate(h.get('summary', ''), 120, '')} "
                f"[{(h.get('status') or {}).get('display', '')}]" for h in hits[:n])
        if name == "wiki_get_page":
            url = str(args.get("url", "")).strip()
            ctx.nav_log.append(f"wiki:{truncate(url, 60, '')}")
            urls = extract_wiki_urls(url, ctx.acfg.wiki.allowed_hosts)
            if not urls:
                return f"[URL не с разрешённого хоста {ctx.acfg.wiki.allowed_hosts}]"
            p = ctx.wiki.get_page(urls[0], ctx.acfg.limits.max_wiki_chars)
            return f"[вики недоступна: {p['error']}]" if p["error"] else f"# {p['title'] or p['slug']}\n{p['content']}"
    except Exception as e:  # noqa: BLE001
        return f"[ошибка инструмента {name}: {type(e).__name__}: {e}]"
    return f"[неизвестный инструмент {name}]"


def dispatch_tool(ctx: RunContext, name: str, args: dict) -> str:
    """Роутер: навигация (трекер/вики) — локально, остальное — в MCP кода 1С."""
    if name in _NAV_TOOLS:
        return _dispatch_navigation(ctx, name, args)
    if ctx.onec and ctx.onec.available:
        return ctx.onec.call(name, args, ctx.acfg.limits.max_tool_result_chars)
    return f"[инструмент {name} недоступен]"


# ---------- агентный цикл ----------

def run_analysis(ctx: RunContext, system_prompt: str, dossier: str,
                 images_for_analyst: list[ImagePart],
                 tools: list[ToolSpec]) -> tuple[AnalysisResult | None, str, int]:
    """Возвращает (результат, сырой текст последнего ответа, число шагов инструментов)."""
    user_parts: list = [dossier]
    user_parts.extend(images_for_analyst)
    messages: list[Msg] = [Msg.system(system_prompt), Msg.user(*user_parts)]

    steps = 0
    resp = None
    use_tools = bool(tools) and ctx.analyst.supports_tools
    while True:
        resp = ctx.analyst.chat(messages, tools=tools if use_tools else None)
        ctx.add_usage("analyst", resp.usage)
        if not resp.tool_calls:
            break
        if steps >= ctx.max_steps:
            # бюджет исчерпан: отвечаем на вызовы заглушкой и просим финальный JSON
            messages.append(Msg.assistant(resp.text, resp.tool_calls))
            for tc in resp.tool_calls:
                messages.append(Msg.tool_result(tc.id, "[бюджет вызовов инструментов исчерпан]"))
            messages.append(Msg.user("Бюджет инструментов исчерпан. Верни финальный JSON по схеме."))
            resp = ctx.analyst.chat(messages)
            ctx.add_usage("analyst", resp.usage)
            break
        messages.append(Msg.assistant(resp.text, resp.tool_calls))
        for tc in resp.tool_calls:
            steps += 1
            log.info("  инструмент %d/%d: %s(%s)", steps, ctx.max_steps, tc.name,
                     truncate(str(tc.args), 200, ""))
            result = dispatch_tool(ctx, tc.name, tc.args)
            messages.append(Msg.tool_result(tc.id, result))

    data = extract_json(resp.text)
    if data is None:
        log.warning("Ответ не распознан как JSON, повторный запрос")
        messages.append(Msg.assistant(resp.text))
        messages.append(Msg.user("Ответ не распознан. Верни СТРОГО один JSON-объект по схеме, без текста вокруг."))
        resp = ctx.analyst.chat(messages)
        ctx.add_usage("analyst", resp.usage)
        data = extract_json(resp.text)
    if data is None:
        return None, resp.text, steps
    try:
        return normalize_analysis(data), resp.text, steps
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось нормализовать результат: %s", e)
        return None, resp.text, steps


# ---------- запись ----------

def dump_revision(dump_path: str) -> str:
    if not dump_path:
        return "выгрузка не настроена"
    try:
        out = subprocess.run(
            ["git", "-C", dump_path, "log", "-1", "--format=%h %cd", "--date=short"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "не git-репозиторий / ревизия неизвестна"


def write_results(ctx: RunContext, workflow: str, issue: dict, markdown: str,
                  result: AnalysisResult) -> tuple[str, str | None]:
    """Возвращает (action, subtask_key)."""
    key = issue["key"]
    wf = ctx.acfg.bugs if workflow == "bugs" else ctx.acfg.ft
    if not ctx.live:
        path = ctx.journal.dry_run_report(key, markdown)
        log.info("[DRY-RUN] Отчёт: %s", path)
        return "dry-run", None

    queue = (issue.get("queue") or {}).get("key") or ctx.acfg.queue
    parent_summary = truncate(issue.get("summary", ""), 90, "")
    subtask_key = ctx.tracker.create_subtask(
        queue=queue,
        parent=key,
        summary=f"{wf.subtask.summary_prefix}{key}: {parent_summary}",
        description=markdown,
        issue_type=wf.subtask.type,
        component_id=ctx.component_id,
        unique=f"{wf.subtask.unique_prefix}-{key}-v1",
    )
    add = [wf.done_tag, wf.complexity_tags.simple if result.complexity == "simple"
           else wf.complexity_tags.complex]
    remove: list[str] = []
    if workflow == "ft":
        remove.append(ctx.acfg.ft.trigger_tag)
    elif ctx.acfg.bugs.selection == "trigger-tag" and ctx.acfg.bugs.trigger_tag in (issue.get("tags") or []):
        remove.append(ctx.acfg.bugs.trigger_tag)
    ctx.tracker.update_tags(key, add=add, remove=remove or None)
    return "created", subtask_key


# ---------- обработка одной задачи ----------

def process_issue(ctx: RunContext, issue: dict, workflow: str) -> dict:
    key = issue["key"]
    started = time.monotonic()
    wf = ctx.acfg.bugs if workflow == "bugs" else ctx.acfg.ft
    log.info("=== %s: %s", key, truncate(issue.get("summary", ""), 100, ""))
    ctx.vision_calls = 0      # потолок vision — на каждый анализ отдельно
    ctx.nav_log = []          # след навигации — по текущей задаче

    # идемпотентность: подзадача уже есть -> только долечить теги
    existing = ctx.tracker.find_existing_ai_subtask(
        key, ctx.acfg.component_name, wf.subtask.summary_prefix)
    if existing:
        log.info("У %s уже есть ИИ-подзадача %s — долечиваю теги", key, existing)
        if ctx.live:
            remove = [ctx.acfg.ft.trigger_tag] if workflow == "ft" else None
            ctx.tracker.update_tags(key, add=[wf.done_tag], remove=remove)
        return {"issue": key, "action": "skipped-existing", "subtask": existing}

    # ft: обязательная ссылка на документацию
    if workflow == "ft" and ctx.acfg.ft.require_doc_link:
        if not (issue.get(ctx.acfg.wiki.doc_field) or "").strip():
            log.warning("%s: поле «Ссылка на документацию» пусто — пропуск", key)
            return {"issue": key, "action": "skipped-no-doclink"}

    dossier, images, sources = build_dossier(ctx, issue, workflow)

    # vision: сайдкар только если аналитик сам не мультимодален
    images_for_analyst: list[ImagePart] = []
    images_note = "нет вложенных картинок"
    if images:
        if ctx.analyst.supports_vision:
            images_for_analyst = images
            images_note = f"проанализировано напрямую моделью-аналитиком: {len(images)}"
        elif ctx.vision is not None:
            descriptions = analyze_images(ctx, images)
            dossier += "\n\n## Скриншоты (описания vision-модели)\n"
            for i, d in enumerate(descriptions, 1):
                dossier += f"\n### Скриншот {i}\n{d}\n"
            images_note = f"проанализировано vision-моделью {ctx.vision.label()}: {len(descriptions)}"
        else:
            images_note = f"НЕ проанализированы (vision-модель не настроена): {len(images)}"
    if sources["skipped_images"]:
        images_note += "; пропущено: " + "; ".join(sources["skipped_images"])

    onec_specs = ctx.onec.tool_specs() if (ctx.onec and ctx.onec.available) else []
    nav_specs = navigation_tool_specs(ctx.acfg.navigation)
    tools = onec_specs + nav_specs
    supports = ctx.analyst.supports_tools
    prompt_fn = bug_system_prompt if workflow == "bugs" else ft_system_prompt
    system_prompt = prompt_fn(ctx.max_steps,
                              code_tools=bool(onec_specs) and supports,
                              nav_tools=bool(nav_specs) and supports)

    result, raw_text, steps = run_analysis(ctx, system_prompt, dossier, images_for_analyst, tools)
    if result is None:
        (ctx.journal.dir / "dry-run" / f"{key}.raw.txt").write_text(raw_text or "", encoding="utf-8")
        return {"issue": key, "action": "error",
                "error": "LLM не вернула валидный JSON (сырой ответ сохранён в journal/dry-run)"}

    wiki_note_items = []
    for p in sources["wiki_pages"]:
        wiki_note_items.append(f"{p['url']}" + (f" [{p['error']}]" if p["error"] else ""))
    code_note = ("инструменты кода недоступны" if not onec_specs
                 else f"вызовов инструментов: {steps}")
    nav_note = "; ".join(ctx.nav_log) if ctx.nav_log else "переходов по связанным задачам не было"
    markdown = render_report(
        ctx.project_root / "templates",
        workflow,
        result,
        parent_key=key,
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        models=(ctx.analyst.label() + (f" + vision {ctx.vision.label()}"
                                       if (ctx.vision and ctx.vision_calls > 0) else "")),
        dump_rev=dump_revision(ctx.acfg.onec.dump_path),
        disclaimer=ctx.acfg.report.disclaimer,
        sources={
            "images_note": images_note,
            "wiki_note": "; ".join(wiki_note_items) if wiki_note_items else "ссылок на вики не найдено",
            "code_note": code_note,
            "nav_note": nav_note,
        },
    )

    action, subtask = write_results(ctx, workflow, issue, markdown, result)
    return {
        "issue": key,
        "action": action,
        "subtask": subtask,
        "complexity": result.complexity,
        "tool_steps": steps,
        "duration_s": round(time.monotonic() - started, 1),
    }


# ---------- прогон ----------

def _usage_delta(before: dict, after: dict) -> dict:
    """Расход за одну задачу = снимок после минус снимок до, раздельно по ролям."""
    out: dict = {}
    for role, vals in after.items():
        b = before.get(role, {})
        out[role] = {k: int(v) - int(b.get(k, 0)) for k, v in vals.items()}
    return out


def run_workflow(ctx: RunContext, workflow: str, selection: str, limit: int,
                 issue_key: str | None = None) -> list[dict]:
    issues = select_issues(ctx, workflow, selection, limit, issue_key)
    log.info("К обработке: %d задач(и)", len(issues))
    results: list[dict] = []
    consecutive_errors = 0
    for i, issue in enumerate(issues):
        before = ctx.usage_snapshot()
        try:
            r = process_issue(ctx, issue, workflow)
            consecutive_errors = consecutive_errors + 1 if r.get("action") == "error" else 0
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка обработки %s", issue.get("key"))
            r = {"issue": issue.get("key"), "action": "error", "error": f"{type(e).__name__}: {e}"}
            consecutive_errors += 1
        r["workflow"] = workflow
        r["mode"] = "live" if ctx.live else "dry-run"
        r["usage"] = _usage_delta(before, ctx.usage)  # пер-задачный расход по ролям
        ctx.journal.run_event(**r)
        results.append(r)
        ctx.tracker.finish_iteration()  # полный доступ к созданным — только на время итерации
        if consecutive_errors >= ctx.acfg.limits.max_consecutive_errors:
            log.error("Аварийная остановка: %d ошибок подряд", consecutive_errors)
            break
        if i < len(issues) - 1:
            time.sleep(ctx.acfg.limits.throttle_between_issues_s)
    return results
