"""Ретрай LLM при 429 (Provider.chat) + честная фиксация в подзадаче при исчерпании попыток."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from analyzer import pipeline
from analyzer.llm.base import LLMResponse, Provider, RateLimitExhausted, is_rate_limit_error


class _Err429(Exception):
    status_code = 429


class _FakeProvider(Provider):
    RATE_LIMIT_SLEEP_S = 0            # без реальных пауз в тестах

    def __init__(self, fail_times: int, exc: Exception | None = None):
        self.fail_times = fail_times
        self.calls = 0
        self._exc = exc or _Err429("too many requests")

    def _chat_once(self, messages, tools=None, tool_choice=None) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._exc
        return LLMResponse(text="ok", tool_calls=[], usage={})


# ---------- is_rate_limit_error ----------

def test_is_rate_limit_error_variants():
    assert is_rate_limit_error(_Err429()) is True
    assert is_rate_limit_error(Exception("HTTP 429 Too Many Requests")) is True
    assert is_rate_limit_error(Exception("rate limit exceeded")) is True
    assert is_rate_limit_error(type("RateLimitError", (Exception,), {})()) is True
    assert is_rate_limit_error(ValueError("bad json")) is False


# ---------- ретрай chat ----------

def test_chat_retries_on_429_then_succeeds():
    p = _FakeProvider(fail_times=2)              # 2 отказа, 3-я успешна
    resp = p.chat([])
    assert resp.text == "ok" and p.calls == 3


def test_chat_raises_exhausted_after_5_attempts():
    p = _FakeProvider(fail_times=99)             # всегда 429
    with pytest.raises(RateLimitExhausted) as ei:
        p.chat([])
    assert p.calls == 5 and ei.value.attempts == 5


def test_chat_non_rate_limit_error_not_retried():
    p = _FakeProvider(fail_times=99, exc=ValueError("bad json"))
    with pytest.raises(ValueError):
        p.chat([])
    assert p.calls == 1                          # без ретрая


# ---------- честная фиксация в подзадаче ----------

def _rl_ctx(live: bool, created: list, tagged: list, dry: list):
    return SimpleNamespace(
        live=live, component_id=478,
        journal=SimpleNamespace(run_id="20260721-1", dry_run_report=lambda k, m: (dry.append((k, m)) or "path.md")),
        tracker=SimpleNamespace(
            count_ai_subtasks=lambda p, c, s: 0,
            create_subtask=lambda **kw: (created.append(kw) or "ONE-900"),
            update_tags=lambda key, add=None, remove=None: tagged.append((key, tuple(add or []))),
        ),
        acfg=SimpleNamespace(
            queue="ONE", component_name="ИИ анализ",
            bugs=SimpleNamespace(
                deferred_tag="ИИ_отложено_лимит",
                subtask=SimpleNamespace(type="task", summary_prefix="[ИИ анализ] ", unique_prefix="ai-bug"),
            ),
        ),
    )


def test_record_rate_limited_live_creates_subtask_and_defers():
    created, tagged, dry = [], [], []
    ctx = _rl_ctx(True, created, tagged, dry)
    issue = {"key": "ONE-1", "summary": "баг", "queue": {"key": "ONE"}}
    r = pipeline._record_rate_limited(ctx, "bugs", issue, RateLimitExhausted(5))
    assert r["action"] == "rate-limited" and r["subtask"] == "ONE-900"
    md = created[0]["description"]
    assert "429" in md and "НЕ ВЫПОЛНЕН" in md                    # честно сказано, что разбора нет
    assert created[0]["unique"] == "ai-bug-ONE-1-20260721-1"
    assert ("ONE-1", ("ИИ_отложено_лимит",)) in tagged            # помечена на переразбор
    assert dry == []


def test_record_rate_limited_dry_run_writes_report_no_subtask():
    created, tagged, dry = [], [], []
    ctx = _rl_ctx(False, created, tagged, dry)
    r = pipeline._record_rate_limited(ctx, "bugs", {"key": "ONE-1", "summary": "x"}, RateLimitExhausted(5))
    assert r["action"] == "rate-limited" and "subtask" not in r
    assert created == [] and tagged == [] and len(dry) == 1
    assert "429" in dry[0][1]


# ---------- интеграция: run_workflow ловит 429 из воркера ----------

def test_run_workflow_rate_limited_records_and_refunds_author(tmp_path, monkeypatch):
    """process_issue -> RateLimitExhausted: результат rate-limited + честная подзадача,
    счётчик автора (зарезервированный при сабмите) возвращён."""
    from analyzer.budget import DailyCounts, DailySpend
    from analyzer.pipeline import LimitGate, RunContext

    issues = [{"key": "ONE-1", "_trigger_author": ("uA", "A"), "summary": "баг", "queue": {"key": "ONE"}}]
    monkeypatch.setattr(pipeline, "select_issues", lambda *a, **k: list(issues))

    def boom(fctx, issue, workflow, idx=1, total=1, force=False):
        raise RateLimitExhausted(5)

    monkeypatch.setattr(pipeline, "process_issue", boom)

    created, tagged = [], []
    tracker = SimpleNamespace(
        finish_iteration=lambda: None,
        count_ai_subtasks=lambda p, c, s: 0,
        create_subtask=lambda **kw: (created.append(kw) or "ONE-900"),
        update_tags=lambda key, add=None, remove=None: tagged.append((key, tuple(add or []))),
    )
    ctx = RunContext(
        acfg=SimpleNamespace(
            queue="ONE", component_name="ИИ анализ",
            limits=SimpleNamespace(max_consecutive_errors=5, throttle_between_issues_s=0),
            bugs=SimpleNamespace(deferred_tag="ИИ_отложено_лимит",
                                 subtask=SimpleNamespace(type="task", summary_prefix="[ИИ анализ] ",
                                                         unique_prefix="ai-bug")),
        ),
        pcfgs=None, tracker=tracker, wiki=None, onec=None,
        journal=SimpleNamespace(run_id="R1", run_event=lambda **k: None, run_summary=lambda **k: None,
                                dry_run_report=lambda k, m: "p"),
        analyst=None, vision=None, live=True, max_steps=10, component_id=478, project_root=tmp_path,
    )
    ctx.limit_gate = LimitGate(spend=DailySpend(tmp_path / "s.json"), counts=DailyCounts(tmp_path / "c.json"),
                               today="2026-07-21", ccy="₽", daily_budget=None, per_author_limit=5,
                               deferred_tag="ИИ_отложено_лимит")
    results = pipeline.run_workflow(ctx, "bugs", "trigger-tag", 10, concurrency=2)

    assert [r["action"] for r in results] == ["rate-limited"]
    assert results[0]["subtask"] == "ONE-900"
    assert ("ONE-1", ("ИИ_отложено_лимит",)) in tagged
    assert ctx.limit_gate.counts.count("2026-07-21", "uA") == 0   # резерв возвращён
