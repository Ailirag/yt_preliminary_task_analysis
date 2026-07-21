"""Параллельный разбор (concurrency>1): реальная одновременность, изоляция форка ctx,
finish_iteration один раз, учёт current_work. process_issue подменяется фейком с задержкой,
чтобы принудить перекрытие; сеть/LLM не задействуются."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from analyzer import pipeline
from analyzer.pipeline import RunContext, _clamp_concurrency
from analyzer.progress import CurrentWork


def _ctx(tmp_path, tracker=None, current_work=None):
    return RunContext(
        acfg=SimpleNamespace(limits=SimpleNamespace(max_consecutive_errors=5, throttle_between_issues_s=0)),
        pcfgs=None,
        tracker=tracker or SimpleNamespace(finish_iteration=lambda: None),
        wiki=None, onec=None,
        journal=SimpleNamespace(run_event=lambda **k: None, run_summary=lambda **k: None),
        analyst=None, vision=None, live=False, max_steps=10, component_id=1,
        project_root=tmp_path, current_work=current_work,
    )


def test_clamp_concurrency():
    assert _clamp_concurrency(0) == 1 and _clamp_concurrency(1) == 1
    assert _clamp_concurrency(3) == 3 and _clamp_concurrency(5) == 5
    assert _clamp_concurrency(99) == 5                 # зажим сверху
    assert _clamp_concurrency(None) == 1 and _clamp_concurrency("x") == 1


def test_parallel_runs_overlap_and_fork_is_isolated(tmp_path, monkeypatch):
    """concurrency=3 на 6 задачах: реально >1 одновременно, но не больше 3; форк изолирует
    onec_workspaces и usage (иначе задачи затёрли бы маршрут кода / учёт токенов друг другу)."""
    N = 6
    issues = [{"key": f"ONE-{i}", "_trigger_author": None} for i in range(N)]
    monkeypatch.setattr(pipeline, "select_issues", lambda *a, **k: list(issues))

    lock = threading.Lock()
    flight = {"cur": 0, "max": 0}
    ws_seen: list = []

    def fake_process(fctx, issue, workflow, idx=1, total=1, force=False):
        with lock:
            flight["cur"] += 1
            flight["max"] = max(flight["max"], flight["cur"])
        fctx.onec_workspaces = [issue["key"]]          # своё у каждой задачи
        fctx.add_usage("analyst", {"input_tokens": 10, "output_tokens": 5})
        time.sleep(0.05)                               # держим слот -> принуждаем перекрытие
        with lock:
            ws_seen.append((issue["key"], list(fctx.onec_workspaces)))
            flight["cur"] -= 1
        return {"issue": issue["key"], "action": "dry-run", "cost": 0.1, "currency": "$"}

    monkeypatch.setattr(pipeline, "process_issue", fake_process)
    results = pipeline.run_workflow(_ctx(tmp_path), "bugs", "trigger-tag", 10, concurrency=3)

    assert len(results) == N
    assert 2 <= flight["max"] <= 3                     # шли параллельно, но не свыше лимита
    assert all(k == ws[0] for k, ws in ws_seen)        # форк не дал затереть чужой воркспейс
    assert all(r["usage"]["analyst"]["input_tokens"] == 10 for r in results)  # usage изолирован (не накопился)


def test_finish_iteration_called_once_and_current_work_drained(tmp_path, monkeypatch):
    issues = [{"key": f"ONE-{i}", "_trigger_author": None} for i in range(4)]
    monkeypatch.setattr(pipeline, "select_issues", lambda *a, **k: list(issues))
    monkeypatch.setattr(pipeline, "process_issue",
                        lambda fctx, issue, workflow, idx=1, total=1, force=False:
                        {"issue": issue["key"], "action": "dry-run", "cost": 0.1, "currency": "$"})

    fin = {"n": 0}
    tracker = SimpleNamespace(finish_iteration=lambda: fin.__setitem__("n", fin["n"] + 1))
    cw = CurrentWork(tmp_path / "current.json")
    pipeline.run_workflow(_ctx(tmp_path, tracker=tracker, current_work=cw),
                          "bugs", "trigger-tag", 10, concurrency=3)

    assert fin["n"] == 1                               # один раз на весь прогон, не по задаче
    from analyzer.status import in_progress
    assert in_progress(tmp_path / "current.json", now=1e12) == []   # все завершились -> пусто


def test_parallel_error_isolated_does_not_break_others(tmp_path, monkeypatch):
    """Исключение в одной задаче -> action=error для неё, остальные доигрывают."""
    issues = [{"key": f"ONE-{i}", "_trigger_author": None} for i in range(4)]
    monkeypatch.setattr(pipeline, "select_issues", lambda *a, **k: list(issues))

    def fake_process(fctx, issue, workflow, idx=1, total=1, force=False):
        if issue["key"] == "ONE-2":
            raise RuntimeError("bang")
        return {"issue": issue["key"], "action": "dry-run", "cost": 0.1, "currency": "$"}

    monkeypatch.setattr(pipeline, "process_issue", fake_process)
    results = pipeline.run_workflow(_ctx(tmp_path), "bugs", "trigger-tag", 10, concurrency=3)

    by_key = {r["issue"]: r["action"] for r in results}
    assert by_key["ONE-2"] == "error"
    assert sum(1 for a in by_key.values() if a == "dry-run") == 3
