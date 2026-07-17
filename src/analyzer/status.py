"""Чтение состояния приложения для команды `analyzer status` (только чтение).

Чистые хелперы над файлами состояния (лок, daily_spend, runs.jsonl); ввод-вывод,
форматирование и опциональный запрос к трекеру — в cli.cmd_status.
"""

from __future__ import annotations

import json
from pathlib import Path


def daemon_state(lock_path: Path, now: float, stale_after_s: float) -> dict:
    """Состояние демона по лок-файлу: {running, pid, age_s}.
    running=True, если heartbeat свежее stale_after_s."""
    try:
        info = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"running": False, "pid": None, "age_s": None}
    age = max(0.0, now - float(info.get("ts", 0)))
    return {"running": age <= stale_after_s, "pid": info.get("pid"), "age_s": age}


def read_issue_rows(runs_path: Path) -> list[dict]:
    """Строки runs.jsonl по задачам (агрегаты kind=run_summary и мусор пропускаются)."""
    rows: list[dict] = []
    try:
        text = Path(runs_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("kind") == "run_summary" or not d.get("issue"):
            continue
        rows.append(d)
    return rows


def todays_rows(rows: list[dict], today: str) -> list[dict]:
    """Строки за сегодня (сравнение по префиксу даты ISO-таймстампа ts=YYYY-MM-DD...)."""
    return [r for r in rows if str(r.get("ts", ""))[:10] == today]


def budget_state(spent: float, budget: float | None, currency: str) -> dict:
    """Дневной бюджет: {currency, spent, budget, remaining}. remaining=None при отсутствии лимита."""
    remaining = None if not budget else round(float(budget) - float(spent), 2)
    return {"currency": currency, "spent": round(float(spent), 2),
            "budget": budget, "remaining": remaining}
