"""Лимиты: дневной счётчик разборов на автора, гейты бюджета/автора в run_workflow, снятие defer.
Сеть/LLM не задействуются — select_issues/process_issue подменяются."""

from __future__ import annotations

from types import SimpleNamespace

from analyzer import daemon, pipeline
from analyzer.budget import DailyCounts, DailySpend
from analyzer.pipeline import LimitGate


# ---------- DailyCounts ----------

def test_daily_counts_rollover_persist_and_exceeded(tmp_path):
    c = DailyCounts(tmp_path / "counts.json")
    assert c.count("2026-07-21", "u1") == 0
    c.add("2026-07-21", "u1")
    c.add("2026-07-21", "u1")
    assert c.count("2026-07-21", "u1") == 2
    assert c.exceeded("2026-07-21", "u1", 2) is True
    assert c.exceeded("2026-07-21", "u1", 3) is False
    assert c.exceeded("2026-07-21", "u1", 0) is False     # 0/None = без лимита
    assert c.exceeded("2026-07-21", "u1", None) is False
    assert c.count("2026-07-22", "u1") == 0                # смена суток -> сброс
    c.add("2026-07-22", "u2")
    assert DailyCounts(tmp_path / "counts.json").count("2026-07-22", "u2") == 1   # персист


# ---------- run_workflow: гейты лимитов ----------

def _issue(key, author="u1"):
    return {"key": key, "_trigger_author": (author, f"User {author}")}


def _wf_ctx(gate, tagged):
    return SimpleNamespace(
        limit_gate=gate,
        usage={},
        usage_snapshot=lambda: {},
        live=True,
        acfg=SimpleNamespace(
            bugs=SimpleNamespace(deferred_tag="ИИ_отложено_лимит"),
            limits=SimpleNamespace(max_consecutive_errors=3, throttle_between_issues_s=0),
        ),
        journal=SimpleNamespace(run_event=lambda **k: None, run_summary=lambda **k: None),
        tracker=SimpleNamespace(
            finish_iteration=lambda: None,
            update_tags=lambda key, add=None, remove=None: tagged.append((key, tuple(add or []))),
        ),
    )


def _patch(monkeypatch, issues, proc_calls):
    monkeypatch.setattr(pipeline, "select_issues", lambda ctx, wf, sel, limit, issue_key: list(issues))

    def fake_process(ctx, issue, workflow, idx=1, total=1, force=False):
        proc_calls.append(issue["key"])
        return {"issue": issue["key"], "action": "dry-run", "cost": 0.6, "currency": "$"}

    monkeypatch.setattr(pipeline, "process_issue", fake_process)


def test_budget_gate_defers_rest_and_started_finish(tmp_path, monkeypatch):
    """Общий дневной бюджет: начатые доигрывают, при превышении остаток откладывается тегом."""
    tagged, proc = [], []
    _patch(monkeypatch, [_issue("ONE-1"), _issue("ONE-2"), _issue("ONE-3")], proc)
    gate = LimitGate(spend=DailySpend(tmp_path / "s.json"), counts=DailyCounts(tmp_path / "c.json"),
                     today="2026-07-21", ccy="$", daily_budget=1.0, per_author_limit=0,
                     deferred_tag="ИИ_отложено_лимит")
    ctx = _wf_ctx(gate, tagged)
    results = pipeline.run_workflow(ctx, "bugs", "trigger-tag", 5)
    # ONE-1 (0<1) и ONE-2 (0.6<1) разобраны; после них spend=1.2 -> ONE-3 отложена
    assert proc == ["ONE-1", "ONE-2"]
    acts = [r["action"] for r in results]
    assert acts == ["dry-run", "dry-run", "deferred-budget"]
    assert ("ONE-3", ("ИИ_отложено_лимит",)) in tagged


def test_per_author_gate_defers_excess_others_proceed(tmp_path, monkeypatch):
    """Лимит на автора: сверх лимита откладывается, задачи другого автора идут."""
    tagged, proc = [], []
    issues = [_issue("ONE-1", "A"), _issue("ONE-2", "A"), _issue("ONE-3", "B")]
    _patch(monkeypatch, issues, proc)
    gate = LimitGate(spend=DailySpend(tmp_path / "s.json"), counts=DailyCounts(tmp_path / "c.json"),
                     today="2026-07-21", ccy="$", daily_budget=None, per_author_limit=1,
                     deferred_tag="ИИ_отложено_лимит")
    ctx = _wf_ctx(gate, tagged)
    results = pipeline.run_workflow(ctx, "bugs", "trigger-tag", 5)
    assert proc == ["ONE-1", "ONE-3"]                       # ONE-2 (второй у A) отложена
    acts = [r["action"] for r in results]
    assert acts == ["dry-run", "deferred-author", "dry-run"]
    assert ("ONE-2", ("ИИ_отложено_лимит",)) in tagged
    assert gate.counts.count("2026-07-21", "A") == 1        # засчитан один разбор A
    assert gate.counts.count("2026-07-21", "B") == 1


def test_no_gate_means_no_limits(tmp_path, monkeypatch):
    """Ручной прогон (limit_gate=None) — лимиты не применяются."""
    tagged, proc = [], []
    _patch(monkeypatch, [_issue("ONE-1"), _issue("ONE-2")], proc)
    ctx = _wf_ctx(None, tagged)
    pipeline.run_workflow(ctx, "bugs", "trigger-tag", 5)
    assert proc == ["ONE-1", "ONE-2"] and tagged == []


# ---------- снятие defer при смене суток ----------

def test_write_results_versions_subtask_and_run_id_unique():
    """Разбор при отсутствии done-тега создаёт НОВУЮ версию подзадачи: суффикс (vN) в теме,
    unique по run_id (переразбор не теряется)."""
    captured = {}
    ctx = SimpleNamespace(
        live=True, component_id=478,
        journal=SimpleNamespace(run_id="20260721-160000-1", dry_run_report=lambda k, m: ""),
        tracker=SimpleNamespace(
            count_ai_subtasks=lambda p, c, s: 1,               # уже есть 1 -> новая версия = 2
            create_subtask=lambda **kw: (captured.update(kw) or "ONE-999"),
            update_tags=lambda *a, **k: None,
        ),
        acfg=SimpleNamespace(
            queue="ONE", component_name="ИИ анализ",
            bugs=SimpleNamespace(
                done_tag="DONE", trigger_tag="TT", selection="trigger-tag",
                complexity_tags=SimpleNamespace(simple="ИИ-простая", complex="ИИ-сложная"),
                subtask=SimpleNamespace(type="task", summary_prefix="[ИИ анализ] ", unique_prefix="ai-bug"),
            ),
            ft=SimpleNamespace(trigger_tag="ftTT"),
        ),
    )
    issue = {"key": "ONE-1", "summary": "баг", "queue": {"key": "ONE"}, "tags": ["TT"]}
    action, sub = pipeline.write_results(ctx, "bugs", issue, "отчёт", SimpleNamespace(complexity="complex"))
    assert action == "created" and sub == "ONE-999"
    assert "(v2)" in captured["summary"]
    assert captured["unique"] == "ai-bug-ONE-1-20260721-160000-1"


def test_undefer_all_removes_deferred_tag():
    removed = []
    ctx = SimpleNamespace(
        acfg=SimpleNamespace(queue="ONE", bugs=SimpleNamespace(deferred_tag="DEF")),
        tracker=SimpleNamespace(
            search=lambda q, per_page=50, max_pages=10: [{"key": "ONE-1"}, {"key": "ONE-2"}],
            update_tags=lambda key, add=None, remove=None: removed.append((key, tuple(remove or []))),
        ),
    )
    daemon._undefer_all(ctx)
    assert removed == [("ONE-1", ("DEF",)), ("ONE-2", ("DEF",))]
