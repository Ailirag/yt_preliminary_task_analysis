"""Чтение состояния для `analyzer status`: лок демона, чтение runs.jsonl, фильтр дня, бюджет."""

import json

from analyzer.status import budget_state, daemon_state, read_issue_rows, todays_rows


def test_daemon_state_running(tmp_path):
    p = tmp_path / "l.lock"
    p.write_text(json.dumps({"pid": 123, "ts": 1000.0}), encoding="utf-8")
    st = daemon_state(p, now=1010.0, stale_after_s=120)
    assert st["running"] is True and st["pid"] == 123 and st["age_s"] == 10.0


def test_daemon_state_stale(tmp_path):
    p = tmp_path / "l.lock"
    p.write_text(json.dumps({"pid": 123, "ts": 1000.0}), encoding="utf-8")
    st = daemon_state(p, now=2000.0, stale_after_s=120)
    assert st["running"] is False and st["pid"] == 123


def test_daemon_state_missing(tmp_path):
    assert daemon_state(tmp_path / "nope.lock", now=100.0, stale_after_s=120) == {
        "running": False, "pid": None, "age_s": None}


def test_read_issue_rows_skips_summary_and_junk(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text("\n".join([
        json.dumps({"ts": "2026-07-17T10:00:00+03:00", "issue": "ONE-1", "action": "created"}),
        json.dumps({"ts": "2026-07-17T10:05:00+03:00", "kind": "run_summary", "issues": 1}),
        "not json",
        "",
        json.dumps({"ts": "2026-07-17T11:00:00+03:00", "issue": "ONE-2", "action": "skipped"}),
    ]), encoding="utf-8")
    assert [r["issue"] for r in read_issue_rows(p)] == ["ONE-1", "ONE-2"]


def test_read_issue_rows_missing_file(tmp_path):
    assert read_issue_rows(tmp_path / "none.jsonl") == []


def test_todays_rows_filters_by_date():
    rows = [
        {"ts": "2026-07-17T10:00:00+03:00", "issue": "A"},
        {"ts": "2026-07-16T23:59:00+03:00", "issue": "B"},
        {"ts": "", "issue": "C"},
    ]
    assert [r["issue"] for r in todays_rows(rows, "2026-07-17")] == ["A"]


def test_budget_state():
    assert budget_state(0.45, 5.0, "$") == {
        "currency": "$", "spent": 0.45, "budget": 5.0, "remaining": 4.55}
    assert budget_state(1.2, None, "₽") == {
        "currency": "₽", "spent": 1.2, "budget": None, "remaining": None}
