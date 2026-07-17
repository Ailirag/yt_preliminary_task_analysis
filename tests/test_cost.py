"""Точный расчёт стоимости с учётом единицы тарифа (z.ai за 1М, Yandex за 1000)."""

from analyzer.pipeline import _rub_cost

# prices = (price_in, price_out, price_cached, price_tools, currency, unit)


def test_per_million_zai_glm5():
    prices = (1.0, 3.2, 0.2, None, "$", 1_000_000)
    # in=1_000_000 (кеш 800k), out=20k:
    # fresh 200k*1.0/1M=0.2 + cached 800k*0.2/1M=0.16 + out 20k*3.2/1M=0.064 => 0.42
    assert _rub_cost(1_000_000, 20_000, 800_000, 0, prices) == 0.42


def test_per_thousand_yandex_deepseek():
    prices = (0.3, 0.5, 0.075, 0.075, "₽", 1000)
    # in=100k (кеш 80k), out=2k:
    # fresh 20k*0.3/1000=6 + cached 80k*0.075/1000=6 + out 2k*0.5/1000=1 => 13.0
    assert _rub_cost(100_000, 2_000, 80_000, 0, prices) == 13.0


def test_tool_tokens_priced_separately():
    prices = (0.3, 0.5, 0.075, 0.1, "₽", 1000)
    # in=10000 всего (кеш 6000, инстр. 2000 -> fresh 2000), out=1000:
    # fresh 2000*0.3/1000=0.6 + cached 6000*0.075/1000=0.45 + tool 2000*0.1/1000=0.2 + out 1000*0.5/1000=0.5 => 1.75
    assert _rub_cost(10_000, 1_000, 6_000, 2_000, prices) == 1.75


def test_cached_falls_back_to_input_price_when_none():
    prices = (2.0, 4.0, None, None, "$", 1_000_000)
    # pcached=None -> кеш по входному тарифу: in=1M (кеш 500k), out=0
    # fresh 500k*2/1M=1.0 + cached 500k*2/1M=1.0 => 2.0
    assert _rub_cost(1_000_000, 0, 500_000, 0, prices) == 2.0


def test_no_price_returns_none():
    assert _rub_cost(100, 10, 0, 0, (None, None, None, None, "₽", 1000)) is None


def test_missing_unit_defaults_to_1000():
    # обратная совместимость: старый 5-элементный кортеж без unit
    prices = (0.3, 0.5, 0.075, 0.075, "₽")
    assert _rub_cost(100_000, 2_000, 80_000, 0, prices) == 13.0
