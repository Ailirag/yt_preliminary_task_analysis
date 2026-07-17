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

from .budget import DailySpend
from .lock import LockHeld, SingleInstanceLock
from .pipeline import (RunContext, analyst_currency, count_candidates,
                       run_workflow, summarize_run)

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


def run_watch(ctx: RunContext, *, stop: threading.Event, now_fn=time.time) -> None:
    """Главный цикл демона. Завершается по установке `stop` (сигнал SIGTERM/SIGINT)."""
    w = ctx.acfg.watch
    work_dir = ctx.project_root / ctx.acfg.paths.work_dir
    lock = SingleInstanceLock(work_dir / w.lock_file, stale_after_s=max(w.interval_s * 3, 120))
    if not lock.acquire():
        raise LockHeld(f"Демон уже запущен (лок {lock.path}) — второй экземпляр не стартует")

    spend = DailySpend(work_dir / "daily_spend.json")
    ccy = analyst_currency(ctx)
    backoff = 0
    tick = 0
    log.info("watch: старт | workflow=%s selection=%s interval=%ss бюджет=%s%s окно=%s",
             w.workflow, w.selection, w.interval_s,
             w.daily_budget if w.daily_budget else "—", f" {ccy}" if w.daily_budget else "",
             w.work_hours or "24/7")
    try:
        while not stop.is_set():
            lock.heartbeat()
            wait = w.interval_s
            try:
                now = now_fn()
                today = _local_date(now)
                if not _in_work_hours(now, w.work_hours):
                    pass  # вне окна работы — просто ждём
                elif spend.exceeded(today, w.daily_budget, ccy):
                    log.info("watch: дневной бюджет %s %s исчерпан (потрачено %s) — пауза до след. суток",
                             w.daily_budget, ccy, _fmt_spend(spend, today))
                elif count_candidates(ctx, w.workflow, w.selection) > 0:
                    tick += 1
                    run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{tick}"
                    ctx.reset_for_run(run_id)
                    results = run_workflow(ctx, w.workflow, w.selection,
                                           ctx.acfg.limits.max_issues_per_run,
                                           should_stop=stop.is_set)
                    spend.add(today, summarize_run(results).get("cost_by_currency") or {})
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
