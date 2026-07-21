"""Мульти-система: минизапуск определения системы, маршрутизация workspace=, генератор state-файла.
Сеть/LLM не задействуются — аналитик и трекер подменяются заглушками."""

from __future__ import annotations

import json
from types import SimpleNamespace

from analyzer import pipeline
from analyzer.config import SystemCfg
from analyzer.systems import build_workspaces, write_state_file


# ---------- минизапуск determine_target_systems ----------

class _Resp:
    def __init__(self, text: str):
        self.text = text
        self.usage: dict = {}


class _Analyst:
    def __init__(self, text: str):
        self._text = text
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return _Resp(self._text)


_SYSTEMS = [
    SystemCfg(name="Управление Торговлей", workspace="ut", repo="https://x/ut.git",
              components=["УТ"], aliases=["торговля"]),
    SystemCfg(name="ERP", workspace="erp", repo="https://x/erp.git", components=["ERP"]),
]


def _ctx(systems, analyst_text, refetch_components=None):
    refetched = {"called": False}

    def get_issue(key):
        refetched["called"] = True
        return {"key": key, "components": refetch_components or []}

    ctx = SimpleNamespace(
        acfg=SimpleNamespace(systems=systems),
        tracker=SimpleNamespace(get_issue=get_issue),
        analyst=_Analyst(analyst_text),
        add_usage=lambda role, usage: None,
    )
    return ctx, refetched


def _issue(**kw):
    base = {"key": "ONE-1", "summary": "тест", "description": "текст"}
    base.update(kw)
    return base


def test_detect_returns_valid_workspace():
    ctx, _ = _ctx(_SYSTEMS, '{"systems": ["ut"]}')
    assert pipeline.determine_target_systems(ctx, _issue(components=[{"display": "УТ"}])) == ["ut"]


def test_detect_maps_name_and_alias_to_workspace():
    # модель вернула человекочитаемое имя и синоним вместо workspace — всё равно резолвим
    ctx, _ = _ctx(_SYSTEMS, '{"systems": ["Управление Торговлей", "ERP"]}')
    assert pipeline.determine_target_systems(ctx, _issue(components=[])) == ["ut", "erp"]
    ctx2, _ = _ctx(_SYSTEMS, '{"systems": ["торговля"]}')
    assert pipeline.determine_target_systems(ctx2, _issue(components=[])) == ["ut"]


def test_detect_drops_unknown_and_dedups():
    ctx, _ = _ctx(_SYSTEMS, '{"systems": ["zzz", "ut", "ut"]}')
    assert pipeline.determine_target_systems(ctx, _issue(components=[])) == ["ut"]


def test_detect_empty_when_model_returns_none():
    ctx, _ = _ctx(_SYSTEMS, '{"systems": []}')
    assert pipeline.determine_target_systems(ctx, _issue(components=[])) == []


def test_detect_no_systems_configured_skips_llm():
    ctx, _ = _ctx([], '{"systems": ["ut"]}')
    assert pipeline.determine_target_systems(ctx, _issue(components=[])) == []
    assert ctx.analyst.calls == 0  # LLM не вызывается, если систем нет


def test_detect_refetches_components_when_absent():
    # в issue нет ключа "components" -> дочитываем задачу через tracker.get_issue
    ctx, refetched = _ctx(_SYSTEMS, '{"systems": ["erp"]}', refetch_components=[{"display": "ERP"}])
    issue = {"key": "ONE-2", "summary": "s", "description": "d"}  # без components
    assert pipeline.determine_target_systems(ctx, issue) == ["erp"]
    assert refetched["called"] is True


def test_detect_survives_bad_json():
    ctx, _ = _ctx(_SYSTEMS, "не json вовсе")
    assert pipeline.determine_target_systems(ctx, _issue(components=[])) == []


# ---------- маршрутизация _route_workspace ----------

def _route_ctx(targets, accepts=True):
    return SimpleNamespace(
        onec_workspaces=targets,
        onec=SimpleNamespace(accepts_workspace=lambda name: accepts),
    )


def test_route_injects_first_target_when_unspecified():
    ctx = _route_ctx(["ut", "erp"])
    assert pipeline._route_workspace(ctx, "search_code", {"query": "x"})["workspace"] == "ut"


def test_route_keeps_valid_requested_workspace():
    ctx = _route_ctx(["ut", "erp"])
    out = pipeline._route_workspace(ctx, "search_code", {"query": "x", "workspace": "erp"})
    assert out["workspace"] == "erp"


def test_route_clamps_out_of_scope_to_first_target():
    ctx = _route_ctx(["ut", "erp"])
    assert pipeline._route_workspace(ctx, "search_code", {"workspace": "zzz"})["workspace"] == "ut"


def test_route_skips_tool_without_workspace_param():
    ctx = _route_ctx(["ut"], accepts=False)   # напр. list_workspaces
    assert "workspace" not in pipeline._route_workspace(ctx, "list_workspaces", {})


def test_route_noop_in_single_workspace_mode():
    ctx = _route_ctx([])                       # systems пуст -> целей нет
    args = {"query": "x"}
    assert pipeline._route_workspace(ctx, "search_code", args) is args


# ---------- генератор state-файла ----------

def test_build_workspaces_mirror_path_and_skip():
    acfg = SimpleNamespace(systems=[
        SystemCfg(name="УТ", workspace="ut", repo="https://x/ut.git", branch="main"),
        SystemCfg(name="ERP", workspace="erp", root="D:/dumps/erp"),
        SystemCfg(name="Плохая", workspace="bad"),          # без repo/root -> пропуск + предупреждение
    ])
    wss, warns = build_workspaces(acfg)
    assert set(wss) == {"ut", "erp"}
    assert wss["ut"]["repo"] == "https://x/ut.git" and wss["ut"]["root"] == ""      # зеркало
    assert wss["erp"]["root"] == "D:/dumps/erp" and wss["erp"]["repo"] == ""        # путь
    assert all(w["update_on_start"] == "off" for w in wss.values())
    assert any("bad" in w for w in warns)


def test_write_state_file_v2_schema(tmp_path):
    acfg = SimpleNamespace(systems=[SystemCfg(name="УТ", workspace="ut", repo="https://x/ut.git")])
    path, n, warns = write_state_file(acfg, str(tmp_path / "config.json"), preserve=False)
    assert n == 1 and not warns
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["active"] == "ut"
    assert data["workspaces"]["ut"]["repo"] == "https://x/ut.git"
    assert data["workspaces"]["ut"]["update_on_start"] == "off"


def test_onec_passthrough_env_keeps_onec_and_tz_only():
    """ONEC_LITE_* и TZ пробрасываются в подпроцесс onec-lite (get_default_environment их вырезает),
    а секреты/PATH — нет (их даёт безопасный набор MCP-SDK)."""
    from analyzer.onec import _passthrough_env
    out = _passthrough_env({
        "ONEC_LITE_STATE": "/data/onec-lite/config.json", "ONEC_LITE_WORKSPACE": "ut",
        "TZ": "Europe/Moscow", "YATRACKER_TOKEN_GT": "secret", "PATH": "/bin", "HOME": "/home/app",
    })
    assert out == {"ONEC_LITE_STATE": "/data/onec-lite/config.json",
                   "ONEC_LITE_WORKSPACE": "ut", "TZ": "Europe/Moscow"}
