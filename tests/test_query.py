"""Формирование YQL-выборки под режимы отбора."""

from analyzer.config import load_configs
from analyzer.pipeline import build_query


def test_bugs_trigger_tag():
    acfg, _ = load_configs()
    q = build_query(acfg, "bugs", "trigger-tag")
    assert f"Queue: {acfg.queue}" in q
    assert "Type: Ошибка" in q
    assert "Resolution: empty()" in q
    assert f'Tags: "{acfg.bugs.trigger_tag}"' in q
    assert f'Tags: !"{acfg.bugs.done_tag}"' in q


def test_bugs_no_done_tag_has_no_trigger_filter():
    acfg, _ = load_configs()
    q = build_query(acfg, "bugs", "no-done-tag")
    assert f'Tags: !"{acfg.bugs.done_tag}"' in q
    assert f'Tags: "{acfg.bugs.trigger_tag}"' not in q


def test_ft_uses_trigger_tag():
    acfg, _ = load_configs()
    q = build_query(acfg, "ft", "n/a")
    assert f'Tags: "{acfg.ft.trigger_tag}"' in q
    assert f"Queue: {acfg.queue}" in q


def test_query_excludes_skip_tag():
    """Гейт мульти-системы: помеченные skip_tag задачи исключаются из выборки (bugs и ft)."""
    acfg, _ = load_configs()
    assert acfg.skip_tag  # по умолчанию непустой
    for wf, sel in (("bugs", "trigger-tag"), ("bugs", "no-done-tag"), ("ft", "n/a")):
        assert f'Tags: !"{acfg.skip_tag}"' in build_query(acfg, wf, sel)
