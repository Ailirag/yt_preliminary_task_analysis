"""Веб-страница статуса: рендер HTML/JSON (чистые функции) + smoke живого сервера (stdlib)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from analyzer.webstatus import StatusServer, render_html, render_json


def _snap():
    return {
        "now": "2026-07-21 18:40:00", "mode": "live", "profile": "yandex",
        "daemon": {"uptime_s": 3720, "tick": 12, "last_tick_s_ago": 8, "backoff_s": 0, "last_error": ""},
        "watch": {"workflow": "bugs", "selection": "trigger-tag", "interval_s": 30,
                  "concurrency": 3, "work_hours": ""},
        "budget": {"currency": "₽", "spent": 291.8, "budget": 8000.0, "remaining": 7708.2},
        "limits": {"per_author_limit": 5,
                   "authors": [{"uid": "uA", "email": "garipov_ir@grandtrade.world", "count": 2, "limit": 15},
                               {"uid": "uB", "email": "petrov@grandtrade.world", "count": 1, "limit": 5}],
                   "deferred_count": 1, "rate_limited_today": 2},
        "in_progress": [{"key": "ONE-1", "workflow": "bugs", "age_s": 130}],
        "queue_pending": 4,
        "today": {"runs": 6, "actions": {"created": 3, "rate-limited": 2}, "trust": {"доверять": 3},
                  "avg_confidence": 92.0, "cost_by_ccy": {"₽": 443.5}, "avg_cost": 88.7,
                  "avg_duration_s": 720.0, "throughput_per_h": 1.5},
        "recent": [{"time": "16:32", "issue": "ONE-4768", "action": "created", "trust": "доверять",
                    "cost": 107.57, "currency": "₽", "subtask": "ONE-4771",
                    "author": "kirilkin_da@grandtrade.world"}],
        "onec": {"available": True, "tools": 30,
                 "workspaces": [{"name": "УТ", "workspace": "ut", "revision": "c8dca92867 2026-07-21"}]},
    }


def test_render_json_roundtrips():
    d = json.loads(render_json(_snap()).decode("utf-8"))
    assert d["mode"] == "live" and d["onec"]["tools"] == 30


def test_render_html_contains_key_facts():
    html = render_html(_snap()).decode("utf-8")
    assert html.startswith("<!doctype html>")
    for needle in ("ONE-1", "Параллельно", "8000", "429", "ONE-4771", "ut", "Отложено по лимиту",
                   "garipov_ir@grandtrade.world", "2 / 15",     # автор по e-mail + индивид. лимит
                   "kirilkin_da@grandtrade.world"):             # автор запуска в «Последних прогонах»
        assert needle in html, needle


def test_render_html_tolerates_empty_snapshot():
    html = render_html({}).decode("utf-8")             # ничего не должно падать
    assert "<!doctype html>" in html and "статус" in html


def test_status_server_serves_html_and_json():
    srv = StatusServer("127.0.0.1", 0, _snap, refresh_s=5)   # порт 0 -> ОС назначит свободный
    assert srv.start() is True
    try:
        port = srv._httpd.server_address[1]                  # noqa: SLF001
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200 and "ИИ-анализатор" in r.read().decode("utf-8")
        with urllib.request.urlopen(base + "/status.json", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read().decode("utf-8"))["queue_pending"] == 4
        code = None
        try:
            urllib.request.urlopen(base + "/nope", timeout=5)
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 404                                   # неизвестный путь -> 404
    finally:
        srv.stop()
