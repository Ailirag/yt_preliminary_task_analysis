"""Дневной бюджет-кап: учёт стоимости прогонов за календарные сутки в work/daily_spend.json.

Переживает рестарт демона (persist на диск). Порог задаётся в валюте аналитика ($/₽);
при превышении демон ставит обработку на паузу до следующих суток. Дата (`today`)
передаётся снаружи — так логика чистая и тестируемая, без обращения к часам внутри.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("analyzer.budget")


class DailySpend:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._date: str = ""
        self._spent: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._date = str(d.get("date", ""))
            self._spent = {k: float(v) for k, v in (d.get("spent") or {}).items()}
        except Exception:  # noqa: BLE001
            self._date, self._spent = "", {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"date": self._date, "spent": self._spent}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось сохранить дневной расход %s: %s", self.path, e)

    def _rollover(self, today: str) -> None:
        if today != self._date:
            self._date = today
            self._spent = {}

    def spent(self, today: str, currency: str) -> float:
        self._rollover(today)
        return self._spent.get(currency, 0.0)

    def add(self, today: str, cost_by_currency: dict[str, float]) -> None:
        self._rollover(today)
        for ccy, amt in (cost_by_currency or {}).items():
            if amt:
                self._spent[ccy] = round(self._spent.get(ccy, 0.0) + float(amt), 4)
        self._save()

    def exceeded(self, today: str, budget: float | None, currency: str) -> bool:
        if not budget:
            return False
        return self.spent(today, currency) >= float(budget)
