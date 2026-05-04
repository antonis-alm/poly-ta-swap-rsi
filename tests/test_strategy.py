from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from almanak.framework.strategies import MarketSnapshot, RSIData, TokenBalance
from almanak.framework.teardown import TeardownMode
from strategy import PolyTASwapRSIStrategy, RegimeState


@pytest.fixture
def base_config() -> dict:
    return {
        "chain": "polygon",
        "protocol": "uniswap_v3",
        "base_token": "WMATIC",
        "quote_token": "USDC",
        "pool_address": "0xb6e57ed85c4c9dbfef2a68711e9d6f36c56e0fcb",
        "pool_fee_bps": 500,
        "rsi_period": 14,
        "rsi_timeframe": "5m",
        "rsi_lower": 45,
        "rsi_upper": 55,
        "allocation_pct": "0.95",
        "dust_buffer_base": "0.01",
        "dust_buffer_quote": "1",
        "min_swap_value_usd": "10",
        "min_expected_out_usd": "9",
        "max_slippage_bps": 30,
        "max_price_impact_bps": 80,
        "max_price_impact_pct": "0.08",
        "max_gas_ratio": "0.05",
        "min_pool_tvl_usd": "0",
        "min_pool_liquidity_raw": "1",
        "cooldown_candles": 1,
        "max_consecutive_failures": 3,
        "force_action": "",
    }


def create_strategy(config: dict) -> PolyTASwapRSIStrategy:
    return PolyTASwapRSIStrategy(
        config=config,
        chain=config["chain"],
        wallet_address="0x" + "1" * 40,
    )


class FakeMarket:
    def __init__(self, snapshot: MarketSnapshot):
        self._snapshot = snapshot
        self.chain = snapshot.chain
        self.wallet_address = snapshot.wallet_address
        self.timestamp = snapshot.timestamp
        self.pool_reserves = lambda *args, **kwargs: SimpleNamespace(
            fee_tier=500,
            liquidity=Decimal("1000000"),
            tvl_usd=Decimal("1000000"),
        )
        self.best_dex_price = lambda *args, **kwargs: SimpleNamespace(
            best_dex="uniswap_v3",
            best_quote=SimpleNamespace(
                amount_out=Decimal("100"),
                price_impact_bps=Decimal("10"),
            ),
        )
        self.is_trade_worthwhile = lambda *args, **kwargs: True

    def price(self, *args, **kwargs):
        return self._snapshot.price(*args, **kwargs)

    def balance(self, *args, **kwargs):
        return self._snapshot.balance(*args, **kwargs)

    def rsi(self, *args, **kwargs):
        return self._snapshot.rsi(*args, **kwargs)


def build_market(
    *,
    ts: datetime,
    rsi: Decimal | None,
    wmatic_balance: Decimal = Decimal("100"),
    wmatic_usd: Decimal = Decimal("100"),
    usdc_balance: Decimal = Decimal("1000"),
    usdc_usd: Decimal = Decimal("1000"),
    price_wmatic: Decimal = Decimal("1"),
    quote_out: Decimal = Decimal("100"),
    price_impact_bps: Decimal = Decimal("10"),
    worthwhile: bool = True,
    fee_tier: int = 500,
    liquidity: Decimal = Decimal("1000000"),
    tvl_usd: Decimal = Decimal("1000000"),
) -> FakeMarket:
    snapshot = MarketSnapshot(
        chain="polygon",
        wallet_address="0x" + "1" * 40,
        timestamp=ts,
    )
    snapshot.set_price("WMATIC", price_wmatic)
    snapshot.set_price("USDC", Decimal("1"))
    snapshot.set_balance(
        "WMATIC",
        TokenBalance(
            symbol="WMATIC",
            balance=wmatic_balance,
            balance_usd=wmatic_usd,
            address="0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
        ),
    )
    snapshot.set_balance(
        "USDC",
        TokenBalance(
            symbol="USDC",
            balance=usdc_balance,
            balance_usd=usdc_usd,
            address="0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        ),
    )
    if rsi is not None:
        snapshot.set_rsi("WMATIC", RSIData(value=rsi, period=14), timeframe="5m")

    market = FakeMarket(snapshot)
    market.pool_reserves = lambda *args, **kwargs: SimpleNamespace(
        fee_tier=fee_tier,
        liquidity=liquidity,
        tvl_usd=tvl_usd,
    )
    market.best_dex_price = lambda *args, **kwargs: SimpleNamespace(
        best_dex="uniswap_v3",
        best_quote=SimpleNamespace(
            amount_out=quote_out,
            price_impact_bps=price_impact_bps,
        ),
    )
    market.is_trade_worthwhile = lambda *args, **kwargs: worthwhile
    return market


def test_warmup_holds_and_initializes_prev_rsi(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    market = build_market(ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC), rsi=Decimal("50"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert strategy.prev_rsi == Decimal("50")


def test_cross_above_upper_swaps_to_long_wmatic(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    strategy.decide(build_market(ts=t0, rsi=Decimal("50")))
    intent = strategy.decide(build_market(ts=t1, rsi=Decimal("56")))

    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "WMATIC"


def test_cross_below_lower_swaps_to_long_usdc(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    strategy.decide(build_market(ts=t0, rsi=Decimal("50")))
    intent = strategy.decide(build_market(ts=t1, rsi=Decimal("44")))

    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "WMATIC"
    assert intent.to_token == "USDC"


def test_neutral_zone_holds_and_sets_neutral_state(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    strategy.decide(build_market(ts=t0, rsi=Decimal("50")))
    intent = strategy.decide(build_market(ts=t1, rsi=Decimal("52")))

    assert intent.intent_type.value == "HOLD"
    assert strategy.regime_state == RegimeState.NEUTRAL.value


def test_same_candle_is_ignored(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    strategy.decide(build_market(ts=ts, rsi=Decimal("50")))
    intent = strategy.decide(build_market(ts=ts, rsi=Decimal("60")))

    assert intent.intent_type.value == "HOLD"
    assert "confirmed candle close" in intent.reason.lower()


def test_already_in_target_state_skips_repeat_swap(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    strategy.prev_rsi = Decimal("55")
    strategy.regime_state = RegimeState.LONG_WMATIC.value
    ts = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)

    intent = strategy.decide(build_market(ts=ts, rsi=Decimal("56")))

    assert intent.intent_type.value == "HOLD"
    assert "already in target state" in intent.reason.lower()


def test_cooldown_blocks_flips(base_config: dict) -> None:
    cfg = {**base_config, "cooldown_candles": 2}
    strategy = create_strategy(cfg)
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t1 + timedelta(minutes=5)

    strategy.decide(build_market(ts=t0, rsi=Decimal("50")))
    swap = strategy.decide(build_market(ts=t1, rsi=Decimal("56")))
    strategy.on_intent_executed(
        swap,
        success=True,
        result=SimpleNamespace(tx_hash="0xabc", extracted_data={}),
    )
    intent = strategy.decide(build_market(ts=t2, rsi=Decimal("44")))

    assert intent.intent_type.value == "HOLD"
    assert "cooldown" in intent.reason.lower()


def test_price_impact_guard_holds(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    strategy.decide(build_market(ts=t0, rsi=Decimal("50")))
    intent = strategy.decide(
        build_market(ts=t1, rsi=Decimal("56"), price_impact_bps=Decimal("150"))
    )

    assert intent.intent_type.value == "HOLD"
    assert "price impact" in intent.reason.lower()


def test_gas_worthiness_guard_holds(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)

    strategy.decide(build_market(ts=t0, rsi=Decimal("50")))
    intent = strategy.decide(build_market(ts=t1, rsi=Decimal("56"), worthwhile=False))

    assert intent.intent_type.value == "HOLD"
    assert "gas cost" in intent.reason.lower()


def test_force_action_flip_to_wmatic(base_config: dict) -> None:
    cfg = {**base_config, "force_action": "flip_to_wmatic"}
    strategy = create_strategy(cfg)

    intent = strategy.decide(
        build_market(ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC), rsi=Decimal("50"))
    )

    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "WMATIC"


def test_failed_swap_stop_halts_strategy(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    mock_intent = SimpleNamespace(intent_type=SimpleNamespace(value="SWAP"))

    for _ in range(3):
        strategy.on_intent_executed(
            mock_intent,
            success=False,
            result=SimpleNamespace(tx_hash="0xfail", extracted_data={}),
        )

    intent = strategy.decide(
        build_market(ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC), rsi=Decimal("50"))
    )

    assert strategy.halted is True
    assert intent.intent_type.value == "HOLD"
    assert "halted" in intent.reason.lower()


def test_persistent_state_round_trip(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    strategy.regime_state = RegimeState.LONG_WMATIC.value
    strategy.prev_rsi = Decimal("58")
    strategy.cooldown_until_candle = 123
    strategy.consecutive_failed_swaps = 1

    saved = strategy.get_persistent_state()

    fresh = create_strategy(base_config)
    fresh.load_persistent_state(saved)

    assert fresh.regime_state == RegimeState.LONG_WMATIC.value
    assert fresh.prev_rsi == Decimal("58")
    assert fresh.cooldown_until_candle == 123
    assert fresh.consecutive_failed_swaps == 1


def test_generate_teardown_intents_for_wmatic_position(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    market = build_market(
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        rsi=Decimal("50"),
        wmatic_balance=Decimal("2"),
        wmatic_usd=Decimal("2"),
    )

    intents = strategy.generate_teardown_intents(mode=TeardownMode.SOFT, market=market)

    assert len(intents) == 1
    assert intents[0].intent_type.value == "SWAP"
    assert intents[0].from_token == "WMATIC"
    assert intents[0].to_token == "USDC"


def test_get_open_positions_queries_live_balance(base_config: dict) -> None:
    strategy = create_strategy(base_config)
    market = build_market(
        ts=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        rsi=Decimal("50"),
        wmatic_balance=Decimal("3"),
        wmatic_usd=Decimal("3"),
    )
    strategy.create_market_snapshot = lambda: market

    summary = strategy.get_open_positions()

    assert len(summary.positions) == 1
    assert summary.positions[0].position_type.value == "TOKEN"
