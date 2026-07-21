"""CurrentWork (учёт «в работе») + status.in_progress. Файловый ввод-вывод, без сети."""

from __future__ import annotations

from analyzer.progress import CurrentWork
from analyzer.status import in_progress


def test_current_work_start_finish_and_read(tmp_path):
    path = tmp_path / "current.json"
    cw = CurrentWork(path)
    assert in_progress(path, now=1000.0) == []              # старт демона -> пусто

    cw.start("ONE-1", "bugs", ts=100.0)
    cw.start("ONE-2", "bugs", ts=140.0)
    rows = in_progress(path, now=200.0)
    assert [r["key"] for r in rows] == ["ONE-1", "ONE-2"]    # свежесть -> старейшая первой
    assert rows[0]["age_s"] == 100.0 and rows[1]["age_s"] == 60.0
    assert rows[0]["workflow"] == "bugs"

    cw.finish("ONE-1")
    rows = in_progress(path, now=200.0)
    assert [r["key"] for r in rows] == ["ONE-2"]

    cw.finish("ONE-2")
    assert in_progress(path, now=200.0) == []


def test_new_instance_clears_stale(tmp_path):
    path = tmp_path / "current.json"
    CurrentWork(path).start("ONE-9", "bugs", ts=1.0)
    assert in_progress(path, now=5.0)                        # запись есть
    CurrentWork(path)                                        # новый демон -> сброс на старте
    assert in_progress(path, now=5.0) == []


def test_in_progress_missing_file_is_empty(tmp_path):
    assert in_progress(tmp_path / "nope.json", now=1.0) == []
