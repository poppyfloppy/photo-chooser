import pytest

from photo_chooser.config import CloudCfg, Price
from photo_chooser.judge.cost import (
    BudgetExceeded,
    TokenUsage,
    UnknownModelPrice,
    check_budget,
    estimate_run_usd,
    image_tokens,
    price_for,
    usage_usd,
)


def test_image_tokens_scales_to_long_side():
    assert image_tokens(4032, 3024, 1024) == 1049  # 1024×768 / 750 = 1048.6
    assert image_tokens(3024, 4032, 1024) == 1049
    assert image_tokens(800, 600, 1024) == 640  # меньше long_side — не увеличивается


def test_estimate_run_counts_screen_once_and_judge_per_theme():
    cfg = CloudCfg()
    # превью 1024×768 → 1049 токенов; накладные 500 на группу из 20
    # первая ступень: 1000×1049 + 50×500 = 1 074 000 вход, 105 000 выход → 0.1074 + 0.0525
    # вторая на тему: 100×1049 + 5×500 = 107 400 вход, 8 000 выход
    haiku = estimate_run_usd(1000, [100, 100], cfg, "claude-haiku-5-5", "claude-haiku-5-5")
    assert haiku == pytest.approx(0.1599 + 2 * (0.01074 + 0.004))
    opus_judge = estimate_run_usd(1000, [100, 100], cfg, "claude-haiku-5-5", "claude-opus-5-5")
    assert opus_judge == pytest.approx(0.1599 + 2 * (0.4296 + 0.16))
    assert estimate_run_usd(0, [], cfg, "claude-haiku-5-5", "claude-haiku-5-5") == 0.0


def test_usage_usd_haiku():
    usage = TokenUsage(input=1_000_000, output=1_000_000)
    assert usage_usd(usage, Price(input=0.10, output=0.50)) == pytest.approx(0.60)


OPUS = Price(input=4.00, output=20.00, cache_read=0.20)
CACHED = TokenUsage(input=1000, output=100, cache_write=4000, cache_read=40000)


def test_usage_usd_with_cache_opus():
    expected = (1000 * 4 + 100 * 20 + 4000 * 4 * 1.25 + 40000 * 0.20) / 1e6
    assert usage_usd(CACHED, OPUS) == pytest.approx(expected)


def test_usage_usd_1h_write_and_batch():
    one_hour = (1000 * 4 + 100 * 20 + 4000 * 4 * 2.0 + 40000 * 0.20) / 1e6
    assert usage_usd(CACHED, OPUS, cache_ttl="1h") == pytest.approx(one_hour)
    assert usage_usd(CACHED, OPUS, cache_ttl="1h", batch=True) == pytest.approx(one_hour * 0.5)


def test_usage_of_missing_cache_fields():
    from types import SimpleNamespace

    assert TokenUsage.of(SimpleNamespace(input_tokens=5, output_tokens=1)) == TokenUsage(input=5, output=1)
    none = SimpleNamespace(input_tokens=5, output_tokens=1, cache_creation_input_tokens=None,
                           cache_read_input_tokens=None)
    assert TokenUsage.of(none) == TokenUsage(input=5, output=1)
    full = SimpleNamespace(input_tokens=5, output_tokens=1, cache_creation_input_tokens=7, cache_read_input_tokens=9)
    assert TokenUsage.of(full) == TokenUsage(input=5, output=1, cache_write=7, cache_read=9)


def test_token_usage_adds_up():
    assert TokenUsage(1, 2, 3, 4) + TokenUsage(10, 20, 30, 40) == TokenUsage(11, 22, 33, 44)


def test_cache_read_price_default():
    assert Price(input=3, output=15).cache_read_price == pytest.approx(0.3)
    assert OPUS.cache_read_price == 0.20


def test_default_pricing_cache_read():
    pricing = CloudCfg().pricing
    assert [pricing[m].cache_read for m in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")] == [
        0.01, 0.10, 0.20]


def test_price_for_unknown_model_raises():
    cfg = CloudCfg()
    assert price_for(cfg, "claude-opus-5-5").output == 20.00
    with pytest.raises(UnknownModelPrice) as e:
        price_for(cfg, "claude-mystery-1")
    assert "claude-mystery-1" in str(e.value)


def test_check_budget():
    with pytest.raises(BudgetExceeded) as e:
        check_budget(4.0, 1.5, 5.0)
    assert (e.value.spent, e.value.estimate, e.value.limit) == (4.0, 1.5, 5.0)
    assert "--max-cost" in str(e.value)
    check_budget(4.0, 1.5, 6.0)
    check_budget(3.5, 1.5, 5.0)  # ровно лимит — можно


def test_estimate_adds_judge_extra_tokens():
    cfg = CloudCfg()
    plain = estimate_run_usd(0, [100], cfg, "claude-haiku-5-5", "claude-opus-5-5")
    styled = estimate_run_usd(0, [100], cfg, "claude-haiku-5-5", "claude-opus-5-5", judge_extra_tokens=4000)
    assert styled - plain == pytest.approx(5 * 4000 * 4.00 / 1e6)


def test_estimate_includes_compose():
    cfg = CloudCfg()
    expected = (30 * (1049 + 60) + 500) * 4.00 / 1e6 + 1500 * 20.00 / 1e6
    assert estimate_run_usd(0, [], cfg, "claude-haiku-5-5", "claude-opus-5-5", n_compose=[30, 0]) == \
        pytest.approx(expected)


def test_estimate_batch_halves_judge_and_compose():
    cfg = CloudCfg()
    args = (1000, [100, 100], cfg, "claude-haiku-5-5", "claude-opus-5-5")
    full = estimate_run_usd(*args, n_compose=[30, 30])
    screen = estimate_run_usd(1000, [], cfg, "claude-haiku-5-5", "claude-opus-5-5")
    assert estimate_run_usd(*args, n_compose=[30, 30], batch=True) == pytest.approx(screen + 0.5 * (full - screen))
