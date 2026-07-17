"""CLI: analyzer bugs | ft | preflight | llm-test | init-component."""

from __future__ import annotations

import argparse
import logging
import os
import struct
import sys
import zlib
from datetime import datetime
from pathlib import Path

from .config import load_configs, project_root
from .journal import Journal, setup_logging
from .llm import ImagePart, Msg, ToolSpec, build_provider, extract_json
from .onec import OnecMCP
from .pipeline import RunContext, run_workflow, summarize_run
from .tracker import TrackerClient
from .wiki import WikiClient

log = logging.getLogger("analyzer.cli")


# ---------- общие помощники ----------

def _make_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _tracker_env(acfg) -> tuple[str, str]:
    token = os.environ.get(acfg.tracker.token_env, "")
    org = os.environ.get(acfg.tracker.org_id_env, "")
    return token, org


def _resolve_live(acfg, args) -> bool:
    want_live = bool(getattr(args, "live", False))
    if want_live and acfg.mode != "live":
        log.warning("Флаг --live проигнорирован: в config/analyzer.yaml mode: %s "
                    "(двойной предохранитель). Запись в трекер ОТКЛЮЧЕНА.", acfg.mode)
        return False
    if want_live:
        log.warning("РЕЖИМ LIVE: записи в трекер будут выполняться.")
    return want_live


def _build_run_context(args, workflow: str) -> tuple[RunContext, OnecMCP | None]:
    acfg, pcfgs = load_configs(getattr(args, "config", None))
    if getattr(args, "queue", None):
        acfg.queue = args.queue
    live = _resolve_live(acfg, args)
    root = project_root()
    run_id = _make_run_id()
    journal = Journal(root / acfg.paths.journal_dir, run_id)
    tracker = TrackerClient.from_env(acfg.tracker, live=live, journal=journal)

    token, org = _tracker_env(acfg)
    wiki = WikiClient(acfg.wiki.api, token, org, acfg.tracker.org_header)

    profile_name = getattr(args, "profile", None)
    a_def, v_def = pcfgs.effective_roles(profile_name)
    analyst_spec = getattr(args, "analyst", None) or a_def
    analyst = build_provider(pcfgs, analyst_spec)
    vision_spec = getattr(args, "vision", None)
    if vision_spec is None:
        vision_spec = v_def
    vision = None
    if vision_spec and vision_spec.lower() not in ("", "none", "off"):
        vision = build_provider(pcfgs, vision_spec)
    log.info("Профиль: %s | analyst=%s vision=%s", profile_name or pcfgs.default_profile or "(roles)",
             analyst.label(), vision.label() if vision else "—")

    # компонента «ИИ анализ»
    component_id = tracker.find_component_id(acfg.queue, acfg.component_name)
    if component_id is None:
        if live:
            raise RuntimeError(
                f"Компонента «{acfg.component_name}» не найдена в очереди {acfg.queue}. "
                f"Создайте её: analyzer init-component"
            )
        log.warning("Компонента «%s» не найдена — dry-run продолжается с заглушкой id=-1",
                    acfg.component_name)
        component_id = -1

    onec = None
    if acfg.onec.enabled:
        onec = OnecMCP(acfg.onec, root)
        if onec.start():
            names = onec.all_tool_names()
            log.info("Инструменты onec-lite (%d): %s", len(names), ", ".join(names[:15]) +
                     (" ..." if len(names) > 15 else ""))
        else:
            log.warning("Анализ пойдёт БЕЗ инструментов кода (onec: %s)", onec.error)

    max_steps = getattr(args, "max_steps", None) or acfg.limits.max_tool_steps
    ctx = RunContext(
        acfg=acfg, pcfgs=pcfgs, tracker=tracker, wiki=wiki, onec=onec,
        journal=journal, analyst=analyst, vision=vision, live=live,
        max_steps=max_steps, component_id=component_id, project_root=root,
    )
    return ctx, onec


def _print_summary(results: list[dict], ctx) -> None:
    print("\n===== ИТОГИ ПРОГОНА =====")
    for r in results:
        line = f"  {r.get('issue')}: {r.get('action')}"
        if r.get("complexity"):
            line += f" | сложность: {r['complexity']}"
        if r.get("trust"):
            line += f" | доверие: {r['trust']} ({r.get('confidence')}/100)"
        if r.get("subtask"):
            line += f" | подзадача: {r['subtask']}"
        if r.get("tool_steps") is not None:
            line += f" | шагов: {r['tool_steps']}"
        if r.get("error"):
            line += f" | ОШИБКА: {r['error']}"
        print(line)

    # агрегат прогона: действия, доверие, стоимость, токены
    s = summarize_run(results)
    print("  Действия: " + ", ".join(f"{k}={v}" for k, v in sorted(s["actions"].items())))
    if s["trust"]:
        tr = ", ".join(f"{k}={v}" for k, v in s["trust"].items())
        conf = f"; средняя уверенность {s['avg_confidence']}/100" if s["avg_confidence"] is not None else ""
        print(f"  Доверие: {tr}{conf}")
    if s["cost_by_currency"]:
        print("  Стоимость (с учётом кеша): "
              + "; ".join(f"{v} {ccy}" for ccy, v in s["cost_by_currency"].items()))
    tokens = s["tokens"]
    if tokens:
        labels = {"analyst": ctx.analyst.label(),
                  "vision": ctx.vision.label() if ctx.vision else "не задана"}
        print("  Токены за прогон (раздельно по моделям):")
        grand_in = grand_out = 0
        for role in ("analyst", "vision"):
            v = tokens.get(role)
            if not v or (v["input_tokens"] == 0 and v["output_tokens"] == 0 and v["calls"] == 0):
                continue
            print(f"    {role} [{labels.get(role, role)}]: "
                  f"in={v['input_tokens']} out={v['output_tokens']} "
                  f"(кеш {v.get('cached_tokens', 0)}) вызовов={v['calls']}")
            grand_in += v["input_tokens"]
            grand_out += v["output_tokens"]
        print(f"    ИТОГО: in={grand_in} out={grand_out}")


# ---------- команды ----------

def cmd_run(args, workflow: str) -> int:
    ctx, onec = _build_run_context(args, workflow)
    selection = getattr(args, "selection", None) or ctx.acfg.bugs.selection
    try:
        results = run_workflow(ctx, workflow, selection, args.limit, args.issue,
                               force=getattr(args, "force", False))
        _print_summary(results, ctx)
        return 0 if all(r.get("action") != "error" for r in results) else 2
    finally:
        if onec:
            onec.stop()
        ctx.tracker.close()
        ctx.wiki.close()


def cmd_preflight(args) -> int:
    acfg, pcfgs = load_configs(getattr(args, "config", None))
    root = project_root()
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # env трекера
    token, org = _tracker_env(acfg)
    check(f"env {acfg.tracker.token_env}", bool(token))
    check(f"env {acfg.tracker.org_id_env}", bool(org))

    # трекер
    if token and org:
        try:
            journal = Journal(root / acfg.paths.journal_dir, _make_run_id())
            tr = TrackerClient.from_env(acfg.tracker, live=False, journal=journal)
            me = tr.myself()
            check("Tracker API /myself", True, me.get("login", ""))
            q = tr.get_queue(acfg.queue)
            check(f"Очередь {acfg.queue}", True, q.get("name", ""))
            comp_id = tr.find_component_id(acfg.queue, acfg.component_name)
            check(f"Компонента «{acfg.component_name}»", comp_id is not None,
                  f"id={comp_id}" if comp_id else "создайте: analyzer init-component")
            tr.close()
        except Exception as e:  # noqa: BLE001
            check("Tracker API", False, str(e))
        # вики
        try:
            wiki = WikiClient(acfg.wiki.api, token, org, acfg.tracker.org_header)
            import httpx
            resp = httpx.get(
                f"{acfg.wiki.api}/v1/pages", params={"slug": "homepage"},
                headers={"Authorization": f"OAuth {token}", acfg.tracker.org_header: org},
                timeout=20,
            )
            check("Wiki API", resp.status_code in (200, 404), f"HTTP {resp.status_code}")
            wiki.close()
        except Exception as e:  # noqa: BLE001
            check("Wiki API", False, str(e))

    # LLM провайдеры (роли активного профиля)
    a_def, v_def = pcfgs.effective_roles()
    for role_name, spec in (("analyst", a_def), ("vision", v_def)):
        if not spec:
            check(f"Роль {role_name}", True, "не задана (пропуск картинок)")
            continue
        try:
            pname, pcfg, model, caps = pcfgs.resolve(spec)
            has_key = bool(pcfg.api_key())
            detail = f"{spec}; env {pcfg.api_key_env}" + ("" if has_key else " НЕ ЗАДАНА")
            if pcfg.folder_id_env:
                has_folder = bool(os.environ.get(pcfg.folder_id_env))
                detail += f"; env {pcfg.folder_id_env}" + ("" if has_folder else " НЕ ЗАДАНА")
                has_key = has_key and has_folder
            check(f"Роль {role_name}", has_key, detail)
        except ValueError as e:
            check(f"Роль {role_name}", False, str(e))

    # onec
    vec = root / acfg.onec.vecgraph_dir
    check("vendor/onec-vecgraph", vec.exists(), str(vec))
    if acfg.onec.dump_path:
        dp = Path(acfg.onec.dump_path)
        ok = dp.exists()
        detail = str(dp)
        if ok and not (dp / "Configuration.xml").exists():
            detail += " (Configuration.xml не найден — проверьте формат выгрузки)"
        check("Выгрузка 1С (dump_path)", ok, detail)
    else:
        check("Выгрузка 1С (dump_path)", False, "не задан в config/analyzer.yaml -> onec.dump_path")

    # вывод
    failed = 0
    print("\n===== PREFLIGHT =====")
    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    print(f"Итого: {len(checks) - failed}/{len(checks)} OK")
    return 0 if failed == 0 else 1


def _make_test_png(width: int = 64, height: int = 64, rgb: tuple = (255, 0, 0)) -> bytes:
    """Красный квадрат PNG без внешних зависимостей — для проверки vision."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def cmd_llm_test(args) -> int:
    _, pcfgs = load_configs(getattr(args, "config", None))
    if args.model:
        specs = [args.model]
    elif args.provider:
        pcfg = pcfgs.providers.get(args.provider)
        if not pcfg:
            print(f"Провайдер {args.provider!r} не найден в providers.yaml")
            return 1
        specs = [f"{args.provider}/{m}" for m in pcfg.models]
    else:
        a_def, v_def = pcfgs.effective_roles()
        specs = [a_def] + ([v_def] if v_def else [])

    failed = 0
    for spec in specs:
        print(f"\n===== {spec} =====")
        try:
            provider = build_provider(pcfgs, spec)
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] инициализация: {e}")
            failed += 1
            continue

        # 1) простой чат
        try:
            resp = provider.chat([Msg.user("Ответь одним словом: сколько будет 2+2?")])
            ok = "4" in resp.text or "четыре" in resp.text.lower()
            print(f"  [{'OK  ' if ok else 'WARN'}] чат: {resp.text.strip()[:80]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] чат: {e}")
            failed += 1
            continue

        # 2) JSON
        try:
            resp = provider.chat([Msg.user('Верни строго JSON-объект {"ok": true} без пояснений.')])
            data = extract_json(resp.text)
            print(f"  [{'OK  ' if data and data.get('ok') else 'WARN'}] JSON: {resp.text.strip()[:80]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] JSON: {e}")
            failed += 1

        # 3) инструменты
        if provider.supports_tools:
            tool = ToolSpec(
                name="get_server_time",
                description="Возвращает текущее время сервера. Вызови для ответа на вопрос о времени.",
                schema={"type": "object", "properties": {}, "required": []},
            )
            try:
                messages = [Msg.user("Который час на сервере? Используй инструмент.")]
                resp = provider.chat(messages, tools=[tool])
                if resp.tool_calls:
                    messages.append(Msg.assistant(resp.text, resp.tool_calls))
                    for tc in resp.tool_calls:
                        messages.append(Msg.tool_result(tc.id, "2026-07-15 12:00:00"))
                    final = provider.chat(messages, tools=[tool])
                    print(f"  [OK  ] инструменты: вызван {resp.tool_calls[0].name}, "
                          f"финальный ответ: {final.text.strip()[:60]!r}")
                else:
                    print(f"  [WARN] инструменты: модель не вызвала инструмент ({resp.text.strip()[:60]!r})")
            except Exception as e:  # noqa: BLE001
                print(f"  [FAIL] инструменты: {e}")
                failed += 1

        # 4) vision
        if provider.supports_vision:
            try:
                img_bytes = (Path(args.image).read_bytes() if args.image
                             else _make_test_png())
                mime = "image/png"
                resp = provider.chat([Msg.user(
                    "Какого цвета квадрат на картинке? Ответь одним словом.",
                    ImagePart(data=img_bytes, mime=mime),
                )])
                ok = any(w in resp.text.lower() for w in ("красн", "red"))
                print(f"  [{'OK  ' if ok or args.image else 'WARN'}] vision: {resp.text.strip()[:80]!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  [FAIL] vision: {e}")
                failed += 1
    return 0 if failed == 0 else 1


def cmd_init_component(args) -> int:
    acfg, _ = load_configs(getattr(args, "config", None))
    root = project_root()
    journal = Journal(root / acfg.paths.journal_dir, _make_run_id())
    tracker = TrackerClient.from_env(acfg.tracker, live=True, journal=journal)
    try:
        existing = tracker.find_component_id(acfg.queue, acfg.component_name)
        if existing:
            print(f"Компонента «{acfg.component_name}» уже существует (id={existing})")
            return 0
        if not args.yes:
            answer = input(f"Создать компоненту «{acfg.component_name}» в очереди {acfg.queue}? [y/N]: ")
            if answer.strip().lower() not in ("y", "yes", "д", "да"):
                print("Отменено.")
                return 1
        result = tracker.create_component(acfg.component_name, acfg.queue)
        print(f"Создана компонента: id={result.get('id')} name={result.get('name')!r}")
        print("ВАЖНО: настройте ограничение видимости по компоненте в UI очереди (админ).")
        return 0
    finally:
        tracker.close()


# ---------- парсер ----------

def _add_run_args(p: argparse.ArgumentParser, with_selection: bool) -> None:
    p.add_argument("--limit", type=int, default=None, help="Максимум задач за прогон")
    p.add_argument("--live", action="store_true", help="Запись в трекер (требует mode: live в конфиге)")
    p.add_argument("--issue", help="Обработать только указанную задачу, например ONE-123")
    p.add_argument("--queue", help="Переопределить очередь из конфига")
    if with_selection:
        p.add_argument("--selection", choices=["no-done-tag", "trigger-tag"],
                       help="Режим отбора багов (по умолчанию из конфига)")
    p.add_argument("--max-steps", type=int, dest="max_steps",
                   help="Бюджет агентных шагов (по умолчанию из конфига)")
    p.add_argument("--profile", help="Сценарий из providers.yaml: z.ai | yandex | z.ai-yandex")
    p.add_argument("--force", action="store_true",
                   help="Переанализировать, даже если ИИ-подзадача уже есть (создаёт новую версию)")
    p.add_argument("--analyst", help="Роль analyst: провайдер/модель (переопределяет профиль)")
    p.add_argument("--vision", help="Роль vision: провайдер/модель; 'none' — отключить (переопределяет профиль)")


def _load_env_file() -> None:
    """Подхватывает .env из корня проекта. Реальные переменные окружения ОС имеют приоритет
    (override=False), так что боевой env не затирается локальным файлом."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = project_root() / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
        log.info(".env загружен: %s (переменные окружения ОС имеют приоритет)", env_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyzer",
                                     description="ИИ-анализатор задач Yandex Tracker для 1С")
    parser.add_argument("--config", help="Каталог конфигов (по умолчанию ./config)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bugs = sub.add_parser("bugs", help="Workflow 1: предварительный анализ ошибок")
    _add_run_args(p_bugs, with_selection=True)

    p_ft = sub.add_parser("ft", help="Workflow 2: анализ готовых ФТ (тег-триггер)")
    _add_run_args(p_ft, with_selection=False)

    sub.add_parser("preflight", help="Самопроверка окружения и доступов")

    p_llm = sub.add_parser("llm-test", help="Проверка LLM-провайдеров (чат/JSON/tools/vision)")
    p_llm.add_argument("--provider", help="Проверить все модели провайдера (zai, yandex, ...)")
    p_llm.add_argument("--model", help="Проверить конкретную роль: провайдер/модель")
    p_llm.add_argument("--image", help="Файл картинки для vision-теста (вместо синтетической)")

    p_init = sub.add_parser("init-component", help="Создать компоненту «ИИ анализ» в очереди")
    p_init.add_argument("--yes", action="store_true", help="Без интерактивного подтверждения")

    args = parser.parse_args(argv)
    # Windows: консоль/редирект могут быть в cp866/cp1251 — пишем UTF-8 устойчиво
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    setup_logging(args.verbose)
    _load_env_file()

    try:
        if args.command == "bugs":
            args.limit = args.limit or load_configs(args.config)[0].limits.max_issues_per_run
            return cmd_run(args, "bugs")
        if args.command == "ft":
            args.limit = args.limit or load_configs(args.config)[0].limits.max_issues_per_run
            return cmd_run(args, "ft")
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "llm-test":
            return cmd_llm_test(args)
        if args.command == "init-component":
            return cmd_init_component(args)
        parser.error("неизвестная команда")
        return 2
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130
    except Exception as e:  # noqa: BLE001
        log.error("Фатальная ошибка: %s", e, exc_info=args.verbose)
        return 1


if __name__ == "__main__":
    sys.exit(main())
