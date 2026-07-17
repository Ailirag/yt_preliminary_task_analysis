"""Агрегат прогона: действия, доверие, стоимость по валютам, токены."""

from analyzer.pipeline import summarize_run


def test_counts_trust_cost_and_tokens():
    results = [
        {"action": "created", "trust": "доверять", "confidence": 90, "cost": 0.45, "currency": "$",
         "usage": {"analyst": {"input_tokens": 1000, "output_tokens": 50,
                               "cached_tokens": 800, "calls": 10}}},
        {"action": "created", "trust": "частично", "confidence": 50, "cost": 0.30, "currency": "$"},
        {"action": "error"},
        {"action": "skipped", "trust": "доверять", "confidence": 80, "cost": 13.0, "currency": "₽"},
    ]
    s = summarize_run(results)
    assert s["issues"] == 4
    assert s["actions"] == {"created": 2, "error": 1, "skipped": 1}
    assert s["trust"] == {"доверять": 2, "частично": 1}
    assert s["avg_confidence"] == round((90 + 50 + 80) / 3, 1)
    assert s["cost_by_currency"] == {"$": 0.75, "₽": 13.0}
    assert s["tokens"]["analyst"]["cached_tokens"] == 800


def test_empty_run():
    s = summarize_run([])
    assert s["issues"] == 0
    assert s["avg_confidence"] is None
    assert s["cost_by_currency"] == {}
    assert s["actions"] == {}


def test_cost_only_when_currency_present():
    # стоимость без валюты не учитывается (не сможем корректно просуммировать)
    s = summarize_run([{"action": "created", "cost": 5.0}])
    assert s["cost_by_currency"] == {}
