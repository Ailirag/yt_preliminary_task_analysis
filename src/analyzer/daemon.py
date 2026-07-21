"""Резидентный демон (analyzer watch): периодический опрос трекера.

Один процесс держит tracker/wiki/onec-lite «тёплыми» и на каждом тике:
  1) обновляет heartbeat лока (single-instance);
  2) проверяет окно работы и дневной бюджет-кап;
  3) делает дешёвую оценку кандидатов (count) — при нуле тяжёлый стек не трогает;
  4) при наличии — сбрасывает пер-прогонное состояние, новый run_id, гоняет run_workflow;
  5) копит стоимость за сутки; спит interval_s (прерывается сигналом остановки).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .budget import DailyCounts, DailySpend
from .lock import LockHeld, SingleInstanceLock
from .pipeline import (LimitGate, RunContext, analyst_currency, build_query, count_candidates,
                       dump_revision, run_workflow)
from .progress import CurrentWork
from .status import in_progress, read_issue_rows, todays_rows
from .users import UserMap
from .webstatus import DaemonStats, StatusServer

log = logging.getLogger("analyzer.daemon")


def _within_window(cur_min: int, window: str) -> bool:
    """Попадает ли минута суток [0..1439] в окно 'HH:MM-HH:MM' (в т.ч. через полночь)."""
    if not window.strip():
        return True
    try:
        start_s, end_s = window.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except Exception:  # noqa: BLE001
        log.warning("watch: не удалось разобрать work_hours=%r — считаю круглосуточно", window)
        return True
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur_min < end
    return cur_min >= start or cur_min < end          # окно через полночь (напр. 22:00-06:00)


def _in_work_hours(now: float, window: str) -> bool:
    if not window.strip():
        return True
    lt = time.localtime(now)
    return _within_window(lt.tm_hour * 60 + lt.tm_min, window)


def _local_date(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _fmt_spend(spend: DailySpend, today: str) -> str:
    parts = [f"{spend.spent(today, c):.2f} {c}" for c in ("$", "₽") if spend.spent(today, c)]
    return "; ".join(parts) or "0"


def _undefer_all(ctx: RunContext) -> None:
    """Смена суток: снять тег «отложено по лимиту» со всех задач — лимиты сброшены, пусть
    возвращаются в выборку. В dry-run снятие лишь журналируется (реальных тегов там и не было)."""
    dtag = ctx.acfg.bugs.deferred_tag
    if not dtag:
        return
    try:
        issues = ctx.tracker.search(f'Queue: {ctx.acfg.queue} Tags: "{dtag}"', per_page=50, max_pages=10)
    except Exception as e:  # noqa: BLE001
        log.warning("watch: не удалось найти отложенные задачи для снятия тега (%s)", e)
        return
    n = 0
    for it in issues:
        try:
            ctx.tracker.update_tags(it["key"], remove=[dtag])
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("watch: не снял тег %s с %s (%s)", dtag, it.get("key"), e)
    if n:
        log.info("watch: смена суток — снял тег «%s» с %d отложенных задач (лимиты сброшены)", dtag, n)


def _resolve_author_maps(ctx: RunContext) -> tuple[dict[str, str], dict[str, int]]:
    """По trigger_authors + per_author_limit_overrides строит (uid->email, uid->индивид.лимит).
    email резолвится через UserMap (кеш work/user_map.json); ключи-оверрайды без '@' считаются
    уже uid. Вызывается один раз на старте демона (справочник кешируется)."""
    w = ctx.acfg.watch
    overrides = dict(w.per_author_limit_overrides or {})
    uid_to_email: dict[str, str] = {}
    overrides_by_uid: dict[str, int] = {}
    for k, v in overrides.items():                     # оверрайды, заданные напрямую uid (без '@')
        if "@" not in str(k):
            overrides_by_uid[str(k).strip()] = int(v)
    ov_by_email = {str(k).strip().lower(): int(v) for k, v in overrides.items() if "@" in str(k)}
    trigger = [a for a in (ctx.acfg.bugs.trigger_authors or []) if isinstance(a, str) and "@" in a]
    emails = sorted({a.strip().lower() for a in trigger} | set(ov_by_email))
    if emails:
        cache = ctx.project_root / ctx.acfg.paths.work_dir / "user_map.json"
        try:
            resolved = UserMap(cache).resolve(emails, ctx.tracker.get_users)
        except Exception as e:  # noqa: BLE001
            log.warning("лимиты/дашборд: резолв email авторов не удался (%s)", e)
            resolved = {}
        for email, uids in resolved.items():
            for uid in uids:
                uid_to_email[uid] = email
                if email in ov_by_email:
                    overrides_by_uid[uid] = ov_by_email[email]
    if overrides_by_uid:
        pretty = sorted({f"{uid_to_email.get(u, u)}={lim}" for u, lim in overrides_by_uid.items()})
        log.info("watch: индивидуальные лимиты: %s (uid: %d)", ", ".join(pretty), len(overrides_by_uid))
    return uid_to_email, overrides_by_uid


def _epoch(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _safe_count(ctx: RunContext, query: str) -> int | None:
    try:
        return ctx.tracker.count(query)
    except Exception as e:  # noqa: BLE001
        log.debug("web: count не удался (%s)", e)
        return None


def _safe_revisions(ctx: RunContext) -> list[dict]:
    """Ревизии зеркал по каждой целевой системе (для страницы статуса). Кэшируется вызывающим."""
    out: list[dict] = []
    try:
        systems = ctx.acfg.systems or []
        if not systems:
            return [{"name": "(выгрузка)", "workspace": "", "revision": dump_revision(ctx.acfg.onec.dump_path)}]
        from .systems import state_file_path
        mirrors = state_file_path(ctx.acfg.onec.env.get("ONEC_LITE_STATE")).parent / "mirrors"
        for s in systems:
            if (s.repo or "").strip():
                path = str(mirrors / s.workspace)
            elif (s.root or "").strip():
                path = s.root
            else:
                path = ""
            out.append({"name": s.name, "workspace": s.workspace,
                        "revision": dump_revision(path) if path else "путь неизвестен"})
    except Exception as e:  # noqa: BLE001
        log.debug("web: ревизии не собрались (%s)", e)
    return out


def _status_snapshot(ctx: RunContext, spend: DailySpend, counts: DailyCounts,
                     stats: DaemonStats, cache: dict, now: float, ccy: str,
                     uid_to_email: dict[str, str], overrides_by_uid: dict[str, int]) -> dict:
    """Полное состояние анализатора для веб-страницы/JSON. Дорогие запросы (трекер, git-ревизии)
    кэшируются на TTL, чтобы автообновление страницы не било по сети/диску каждую секунду.
    Авторы показываются по e-mail (uid->email); лимит на автора — индивидуальный или общий."""
    w = ctx.acfg.watch
    today = _local_date(now)
    work_dir = ctx.project_root / ctx.acfg.paths.work_dir
    journal_dir = ctx.project_root / ctx.acfg.paths.journal_dir

    rows = todays_rows(read_issue_rows(journal_dir / "runs.jsonl"), today)
    actions: dict[str, int] = {}
    trust: dict[str, int] = {}
    confs: list[float] = []
    costs: list[float] = []
    durs: list[float] = []
    cost_by_ccy: dict[str, float] = {}
    analyzed = 0
    earliest: float | None = None
    for r in rows:
        a = r.get("action") or "?"
        actions[a] = actions.get(a, 0) + 1
        if a in ("created", "dry-run", "error"):
            analyzed += 1
        if r.get("trust"):
            trust[r["trust"]] = trust.get(r["trust"], 0) + 1
        if isinstance(r.get("confidence"), (int, float)):
            confs.append(float(r["confidence"]))
        c, cc = r.get("cost"), r.get("currency")
        if isinstance(c, (int, float)) and cc:
            cost_by_ccy[cc] = round(cost_by_ccy.get(cc, 0.0) + float(c), 2)
            costs.append(float(c))
        if isinstance(r.get("duration_s"), (int, float)):
            durs.append(float(r["duration_s"]))
        e = _epoch(str(r.get("ts") or ""))
        if e is not None:
            earliest = e if earliest is None else min(earliest, e)
    throughput = None
    if earliest is not None and analyzed >= 2:
        throughput = round(analyzed / max(0.05, (now - earliest) / 3600), 1)
    recent = [{"time": str(r.get("ts") or "")[11:16], "issue": r.get("issue"),
               "action": r.get("action"), "trust": r.get("trust"), "cost": r.get("cost"),
               "currency": r.get("currency"), "subtask": r.get("subtask"),
               "author": uid_to_email.get(r.get("author_uid") or "", "") or r.get("author") or ""}
              for r in rows[-8:][::-1]]

    ttl = 60
    if now - cache.get("net_ts", 0) > ttl:
        cache["net_ts"] = now
        cache["pending"] = _safe_count(ctx, build_query(ctx.acfg, w.workflow, w.selection))
        dtag = ctx.acfg.bugs.deferred_tag
        cache["deferred"] = _safe_count(ctx, f'Queue: {ctx.acfg.queue} Tags: "{dtag}"') if dtag else None
        cache["revisions"] = _safe_revisions(ctx)

    onec = ctx.onec
    return {
        "now": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "mode": ctx.acfg.mode,
        "profile": w.profile or "(default)",
        "daemon": {
            "uptime_s": now - stats.start_ts,
            "tick": stats.tick,
            "last_tick_s_ago": (now - stats.last_tick_ts) if stats.last_tick_ts else None,
            "backoff_s": stats.backoff_s,
            "last_error": stats.last_error,
        },
        "watch": {"workflow": w.workflow, "selection": w.selection, "interval_s": w.interval_s,
                  "concurrency": w.concurrency, "work_hours": w.work_hours},
        "budget": {"currency": ccy, "spent": spend.spent(today, ccy), "budget": w.daily_budget,
                   "remaining": round(w.daily_budget - spend.spent(today, ccy), 2) if w.daily_budget else None},
        "limits": {"per_author_limit": w.per_author_daily_limit,
                   "authors": [{"uid": k, "email": uid_to_email.get(k, k), "count": v,
                                "limit": overrides_by_uid.get(k, w.per_author_daily_limit)}
                               for k, v in sorted(counts.all(today).items(), key=lambda kv: -kv[1])],
                   "deferred_count": cache.get("deferred"),
                   "rate_limited_today": actions.get("rate-limited", 0)},
        "in_progress": in_progress(work_dir / "current.json", now),
        "queue_pending": cache.get("pending"),
        "today": {"runs": len(rows), "actions": actions, "trust": trust,
                  "avg_confidence": round(sum(confs) / len(confs), 1) if confs else None,
                  "cost_by_ccy": cost_by_ccy,
                  "avg_cost": round(sum(costs) / len(costs), 2) if costs else None,
                  "avg_duration_s": round(sum(durs) / len(durs), 1) if durs else None,
                  "throughput_per_h": throughput},
        "recent": recent,
        "onec": {"available": bool(onec and onec.available),
                 "tools": len(onec.all_tool_names()) if onec else 0,
                 "workspaces": cache.get("revisions") or []},
    }


def run_watch(ctx: RunContext, *, stop: threading.Event, now_fn=time.time) -> None:
    """Главный цикл демона. Завершается по установке `stop` (сигнал SIGTERM/SIGINT)."""
    w = ctx.acfg.watch
    work_dir = ctx.project_root / ctx.acfg.paths.work_dir
    lock = SingleInstanceLock(work_dir / w.lock_file, stale_after_s=max(w.interval_s * 3, 120))
    if not lock.acquire():
        raise LockHeld(f"Демон уже запущен (лок {lock.path}) — второй экземпляр не стартует")

    spend = DailySpend(work_dir / "daily_spend.json")
    counts = DailyCounts(work_dir / "daily_counts.json")
    ctx.current_work = CurrentWork(work_dir / "current.json")   # учёт «в работе»; сброс устаревших на старте
    ccy = analyst_currency(ctx)
    backoff = 0
    tick = 0
    last_day = _local_date(now_fn())      # без снятия defer на старте — только при реальной смене суток
    stats = DaemonStats(start_ts=now_fn())
    uid_to_email, overrides_by_uid = _resolve_author_maps(ctx)   # email авторов + индивид. лимиты
    snap_cache: dict = {}
    server = None
    if w.status_port:
        server = StatusServer(
            w.status_host, w.status_port,
            lambda: _status_snapshot(ctx, spend, counts, stats, snap_cache, time.time(), ccy,
                                     uid_to_email, overrides_by_uid),
            w.status_refresh_s)
        server.start()
    log.info("watch: старт | workflow=%s selection=%s interval=%ss параллельно=%s бюджет=%s%s лимит/автор=%s окно=%s",
             w.workflow, w.selection, w.interval_s, w.concurrency,
             w.daily_budget if w.daily_budget else "—", f" {ccy}" if w.daily_budget else "",
             w.per_author_daily_limit or "—", w.work_hours or "24/7")
    try:
        while not stop.is_set():
            lock.heartbeat()
            wait = w.interval_s
            try:
                now = now_fn()
                today = _local_date(now)
                if today != last_day:               # смена суток -> лимиты обнулились, снять отложенные
                    _undefer_all(ctx)
                    last_day = today
                if not _in_work_hours(now, w.work_hours):
                    pass  # вне окна работы — просто ждём
                elif count_candidates(ctx, w.workflow, w.selection) > 0:
                    tick += 1
                    run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{tick}"
                    ctx.reset_for_run(run_id)
                    # лимиты тика: общий дневной бюджет + разборов на автора. Учёт трат и счётчиков
                    # ведётся per-issue ВНУТРИ run_workflow (через этот gate), поэтому здесь spend не копим.
                    ctx.limit_gate = LimitGate(
                        spend=spend, counts=counts, today=today, ccy=ccy,
                        daily_budget=w.daily_budget, per_author_limit=w.per_author_daily_limit,
                        deferred_tag=ctx.acfg.bugs.deferred_tag,
                        per_author_overrides=overrides_by_uid)
                    results = run_workflow(ctx, w.workflow, w.selection,
                                           ctx.acfg.limits.max_issues_per_run,
                                           should_stop=stop.is_set, concurrency=w.concurrency)
                    if results:
                        log.info("watch: тик #%d — задач %d, потрачено сегодня %s",
                                 tick, len(results), _fmt_spend(spend, today))
                    ctx.tracker.finish_iteration()
                backoff = 0
                stats.tick = tick
                stats.last_tick_ts = now
                stats.backoff_s = 0
                stats.last_error = ""
            except Exception as e:  # noqa: BLE001
                backoff = min(max(backoff * 2, w.error_backoff_s), w.max_backoff_s)
                wait = backoff
                stats.backoff_s = backoff
                stats.last_error = f"{type(e).__name__}: {e}"[:200]
                log.exception("watch: ошибка тика — backoff %ss", backoff)
            stop.wait(wait)
    finally:
        if server is not None:
            server.stop()
        lock.release()
        log.info("watch: остановлен")
