import pytest

from photo_chooser.config import CloudCfg, Price
from photo_chooser.judge.cost import (
    BudgetExceeded,
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
    assert usage_usd(1_000_000, 1_000_000, Price(input=0.10, output=0.50)) == pytest.approx(0.60)


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
