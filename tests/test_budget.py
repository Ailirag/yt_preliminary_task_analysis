"""Дневной бюджет-кап: накопление, persist, rollover по дате, проверка превышения."""

from analyzer.budget import DailySpend


def test_add_accumulates_and_persists(tmp_path):
    p = tmp_path / "spend.json"
    b = DailySpend(p)
    b.add("2026-07-17", {"$": 0.45})
    b.add("2026-07-17", {"$": 0.30, "₽": 13.0})
    assert b.spent("2026-07-17", "$") == 0.75
    assert b.spent("2026-07-17", "₽") == 13.0
    # новый инстанс читает файл — расход переживает рестарт демона
    assert DailySpend(p).spent("2026-07-17", "$") == 0.75


def test_rollover_resets_on_new_day(tmp_path):
    b = DailySpend(tmp_path / "s.json")
    b.add("2026-07-17", {"$": 5.0})
    assert b.spent("2026-07-18", "$") == 0.0
    b.add("2026-07-18", {"$": 1.0})
    assert b.spent("2026-07-18", "$") == 1.0


def test_exceeded(tmp_path):
    b = DailySpend(tmp_path / "s.json")
    b.add("2026-07-17", {"$": 4.5})
    assert b.exceeded("2026-07-17", 5.0, "$") is False
    b.add("2026-07-17", {"$": 0.6})            # 5.1 >= 5.0
    assert b.exceeded("2026-07-17", 5.0, "$") is True
    assert b.exceeded("2026-07-17", None, "$") is False   # лимит не задан
    assert b.exceeded("2026-07-17", 5.0, "₽") is False    # в другой валюте не тратили
