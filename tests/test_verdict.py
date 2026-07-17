"""Калибровка детерминированного скорера доверия."""

from analyzer.models import normalize_analysis
from analyzer.verdict import score_result


def _res(**kw):
    return normalize_analysis(kw)


def test_grounded_gives_high_trust():
    # много обращений к коду + конкретные ссылки + конкретные объекты (кейс ONE-4749)
    r = _res(
        complexity="complex",
        affected_objects=[{"object": "Document.РТУ", "module": "Object",
                           "procedure": "Провести", "role": "теряет КМ"}],
        code_refs=["Mod/Module.bsl:10", "Mod2/Module.bsl:20", "Mod3/Module.bsl:30"],
    )
    v = score_result(r, tool_steps=47, code_available=True, hit_budget=False, json_retried=False)
    assert v.level == "доверять"
    assert v.score >= 70


def test_symptom_only_gives_no_trust():
    # 0 обращений к коду при доступных инструментах + плейсхолдеры + нет ссылок (кейс YandexGPT)
    r = _res(
        complexity="complex",
        affected_objects=[{"object": "неизвестно", "role": "предположительно"}],
        code_refs=[],
    )
    v = score_result(r, tool_steps=0, code_available=True, hit_budget=False, json_retried=False)
    assert v.level == "не доверять"
    assert v.score < 40


def test_no_code_capped_at_partial():
    # даже с ссылками — без доступа к коду не выше «частично»
    r = _res(
        complexity="complex",
        affected_objects=[{"object": "Document.X", "module": "M", "procedure": "P", "role": "r"}],
        code_refs=["a:1", "b:2", "c:3"],
    )
    v = score_result(r, tool_steps=0, code_available=False, hit_budget=False, json_retried=False)
    assert v.score <= 55
    assert v.level != "доверять"


def test_simple_without_code_penalised():
    r = _res(complexity="simple", code_refs=[], affected_objects=[])
    v = score_result(r, tool_steps=0, code_available=True, hit_budget=False, json_retried=False)
    assert v.level == "не доверять"


def test_mapping_counts_as_objects_for_ft():
    # у ФТ конкретика идёт в mapping, а не affected_objects
    r = _res(
        complexity="complex",
        mapping=[{"requirement": "печать", "objects": "Document.РТУ", "notes": "форма"}],
        code_refs=["a:1", "b:2", "c:3"],
    )
    v = score_result(r, tool_steps=20, code_available=True, hit_budget=False, json_retried=False)
    assert v.level == "доверять"
