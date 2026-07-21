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

from .budget import DailyCounts, DailySpend
from .lock import LockHeld, SingleInstanceLock
from .pipeline import (LimitGate, RunContext, analyst_currency, count_candidates,
                       run_workflow)

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


def run_watch(ctx: RunContext, *, stop: threading.Event, now_fn=time.time) -> None:
    """Главный цикл демона. Завершается по установке `stop` (сигнал SIGTERM/SIGINT)."""
    w = ctx.acfg.watch
    work_dir = ctx.project_root / ctx.acfg.paths.work_dir
    lock = SingleInstanceLock(work_dir / w.lock_file, stale_after_s=max(w.interval_s * 3, 120))
    if not lock.acquire():
        raise LockHeld(f"Демон уже запущен (лок {lock.path}) — второй экземпляр не стартует")

    spend = DailySpend(work_dir / "daily_spend.json")
    counts = DailyCounts(work_dir / "daily_counts.json")
    ccy = analyst_currency(ctx)
    backoff = 0
    tick = 0
    last_day = _local_date(now_fn())      # без снятия defer на старте — только при реальной смене суток
    log.info("watch: старт | workflow=%s selection=%s interval=%ss бюджет=%s%s лимит/автор=%s окно=%s",
             w.workflow, w.selection, w.interval_s,
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
                        deferred_tag=ctx.acfg.bugs.deferred_tag)
                    results = run_workflow(ctx, w.workflow, w.selection,
                                           ctx.acfg.limits.max_issues_per_run,
                                           should_stop=stop.is_set)
                    if results:
                        log.info("watch: тик #%d — задач %d, потрачено сегодня %s",
                                 tick, len(results), _fmt_spend(spend, today))
                    ctx.tracker.finish_iteration()
                backoff = 0
            except Exception:  # noqa: BLE001
                backoff = min(max(backoff * 2, w.error_backoff_s), w.max_backoff_s)
                wait = backoff
                log.exception("watch: ошибка тика — backoff %ss", backoff)
            stop.wait(wait)
    finally:
        lock.release()
        log.info("watch: остановлен")
