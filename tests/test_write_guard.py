"""Write-guard трекера: на чужих задачах — только теги; создание — только подзадача
с компонентой+unique; полный PATCH — только созданным в этом прогоне. Сеть не задействуется
(live=False + проверки-гварды выполняются до HTTP)."""

import pytest

from analyzer.tracker import DryRunResult, TrackerClient, WriteGuardError


def _client():
    return TrackerClient("https://api.tracker.test", "tok", "org", "X-Org-ID", live=False)


def test_guard_tags_only_rejects_other_fields():
    tc = _client()
    with pytest.raises(WriteGuardError):
        tc._guard_tags_only("ONE-1", {"tags": {"add": ["x"]}, "summary": "нельзя"})
    tc._guard_tags_only("ONE-1", {"tags": {"add": ["x"]}})  # только теги — ок


def test_created_this_run_key_bypasses_tags_guard():
    tc = _client()
    tc._created_this_run.add("ONE-9")
    # созданной в прогоне задаче можно менять любые поля
    tc._guard_tags_only("ONE-9", {"summary": "можно", "description": "можно"})


def test_guard_create_requires_parent_component_unique():
    tc = _client()
    with pytest.raises(WriteGuardError):
        tc._guard_create({"parent": ""}, 478, "u")          # нет parent
    with pytest.raises(WriteGuardError):
        tc._guard_create({"parent": "ONE-1"}, None, "u")     # нет компоненты
    with pytest.raises(WriteGuardError):
        tc._guard_create({"parent": "ONE-1"}, 478, "")       # нет unique
    tc._guard_create({"parent": "ONE-1"}, 478, "u")          # всё есть — ок


def test_patch_created_only_for_created_this_run():
    tc = _client()
    with pytest.raises(WriteGuardError):
        tc.patch_created_issue("ONE-404", {"description": "x"})
    tc._created_this_run.add("ONE-9")
    assert isinstance(tc.patch_created_issue("ONE-9", {"description": "x"}), DryRunResult)


def test_update_tags_dry_run_returns_marker_without_network():
    tc = _client()
    assert isinstance(tc.update_tags("ONE-1", add=["t"]), DryRunResult)


def test_finish_iteration_clears_created_registry():
    tc = _client()
    tc._created_this_run.add("ONE-9")
    tc.finish_iteration()
    with pytest.raises(WriteGuardError):
        tc.patch_created_issue("ONE-9", {"description": "x"})


def test_add_comment_dry_run_returns_marker_without_network():
    tc = _client()
    # комментарий к ЛЮБОЙ существующей задаче разрешён (не проходит _guard_tags_only, суб-ресурс)
    assert isinstance(tc.add_comment("ONE-1", "Анализ невозможен: система не установлена"), DryRunResult)


def test_add_comment_empty_text_is_noop():
    tc = _client()
    assert tc.add_comment("ONE-1", "   ") is None
