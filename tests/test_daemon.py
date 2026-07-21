"""Демон: окно работы (чистая логика) + один тик цикла с подменёнными зависимостями."""

import threading
from types import SimpleNamespace

from analyzer import daemon
from analyzer.config import PathsCfg, WatchCfg
from analyzer.daemon import _within_window, run_watch


# ---------- окно работы ----------

def test_window_empty_always_true():
    assert _within_window(0, "") is True
    assert _within_window(1439, "   ") is True


def test_window_normal():
    w = "08:00-20:00"
    assert _within_window(8 * 60, w) is True
    assert _within_window(19 * 60 + 59, w) is True
    assert _within_window(20 * 60, w) is False        # правая граница не включается
    assert _within_window(7 * 60 + 59, w) is False


def test_window_overnight():
    w = "22:00-06:00"
    assert _within_window(23 * 60, w) is True
    assert _within_window(1 * 60, w) is True
    assert _within_window(12 * 60, w) is False


def test_window_bad_format_defaults_true():
    assert _within_window(600, "notatime") is True


# ---------- один тик run_watch ----------

def _fake_ctx(tmp_path):
    return SimpleNamespace(
        acfg=SimpleNamespace(
            queue="ONE",
            watch=WatchCfg(interval_s=1, status_port=0),   # без веб-сервера в юнит-тестах
            paths=PathsCfg(work_dir="work"),
            limits=SimpleNamespace(max_issues_per_run=5),
            bugs=SimpleNamespace(deferred_tag="ИИ_отложено_лимит", trigger_authors=[]),
        ),
        project_root=tmp_path,
        tracker=SimpleNamespace(finish_iteration=lambda: None),
        reset_for_run=lambda run_id: None,
        limit_gate=None,
    )


def test_tick_processes_when_candidates(tmp_path, monkeypatch):
    stop = threading.Event()
    calls = {"run": 0, "gate": None}
    monkeypatch.setattr(daemon, "analyst_currency", lambda ctx: "$")
    monkeypatch.setattr(daemon, "count_candidates", lambda ctx, wf, sel: 1)

    def fake_run_workflow(ctx, wf, sel, limit, should_stop=None, concurrency=1):
        calls["run"] += 1
        calls["gate"] = ctx.limit_gate              # демон должен собрать и передать gate лимитов
        stop.set()                                  # остановиться после первого тика
        return [{"action": "created", "cost": 0.4, "currency": "$"}]

    monkeypatch.setattr(daemon, "run_workflow", fake_run_workflow)
    run_watch(_fake_ctx(tmp_path), stop=stop)
    assert calls["run"] == 1
    # учёт трат теперь per-issue внутри run_workflow; демон лишь собирает gate и передаёт его
    g = calls["gate"]
    assert g is not None and g.ccy == "$" and g.deferred_tag == "ИИ_отложено_лимит"


def test_resolve_author_maps_email_and_overrides(tmp_path):
    """uid->email (для дашборда) + uid->индивид.лимит из per_author_limit_overrides.
    Учитывает несколько активных uid на один email."""
    users = [{"email": "garipov_ir@grandtrade.world", "uid": "842", "dismissed": False},
             {"email": "garipov_ir@grandtrade.world", "uid": "2727", "dismissed": False},
             {"email": "petrov@grandtrade.world", "uid": "500", "dismissed": False}]
    ctx = SimpleNamespace(
        acfg=SimpleNamespace(
            watch=WatchCfg(per_author_limit_overrides={"garipov_ir@grandtrade.world": 15}),
            bugs=SimpleNamespace(trigger_authors=["garipov_ir@grandtrade.world", "petrov@grandtrade.world"]),
            paths=PathsCfg(work_dir="work")),
        project_root=tmp_path,
        tracker=SimpleNamespace(get_users=lambda: users))
    u2e, ov = daemon._resolve_author_maps(ctx)
    assert u2e == {"842": "garipov_ir@grandtrade.world", "2727": "garipov_ir@grandtrade.world",
                   "500": "petrov@grandtrade.world"}
    assert ov == {"842": 15, "2727": 15}                    # оба uid Гарипова -> 15; Петров без оверрайда


def test_tick_skips_when_no_candidates(tmp_path, monkeypatch):
    stop = threading.Event()
    calls = {"run": 0}
    monkeypatch.setattr(daemon, "analyst_currency", lambda ctx: "$")

    def fake_count(ctx, wf, sel):
        stop.set()                                  # остановиться после одной проверки
        return 0

    monkeypatch.setattr(daemon, "count_candidates", fake_count)
    monkeypatch.setattr(daemon, "run_workflow",
                        lambda *a, **k: calls.__setitem__("run", calls["run"] + 1) or [])
    run_watch(_fake_ctx(tmp_path), stop=stop)
    assert calls["run"] == 0                         # нет кандидатов → тяжёлый прогон не запускался
