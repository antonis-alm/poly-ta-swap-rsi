import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from almanak.framework.data.market_snapshot import (
    BalanceUnavailableError,
    DexQuoteUnavailableError,
    PoolReservesUnavailableError,
    PriceUnavailableError,
)
from almanak.framework.intents import Intent
from almanak.framework.strategies import (
    IntentStrategy,
    MarketSnapshot,
    TokenBalance,
    almanak_strategy,
)

logger = logging.getLogger(__name__)


class RegimeState(str, Enum):
    LONG_WMATIC = "LONG_WMATIC"
    LONG_USDC = "LONG_USDC"
    NEUTRAL = "NEUTRAL"


@almanak_strategy(
    name="poly_t_a_swap_r_s_i",
    description="RSI 5m regime flipper for WMATIC/USDC on Polygon Uniswap V3",
    version="1.0.0",
    author="Generated",
    tags=["ta_swap", "rsi", "uniswap_v3", "polygon"],
    supported_chains=["polygon"],
    supported_protocols=["uniswap_v3"],
    intent_types=["SWAP", "HOLD"],
    default_chain="polygon",
)
class PolyTASwapRSIStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.protocol = str(self.get_config("protocol", "uniswap_v3"))
        self.base_token = str(self.get_config("base_token", "WMATIC"))
        self.quote_token = str(self.get_config("quote_token", "USDC"))
        self.pool_address = str(self.get_config("pool_address", "")).lower()
        self.pool_fee_bps = int(self.get_config("pool_fee_bps", 500))

        self.rsi_period = int(self.get_config("rsi_period", 14))
        self.rsi_timeframe = str(self.get_config("rsi_timeframe", "5m"))
        self.rsi_lower = Decimal(str(self.get_config("rsi_lower", "45")))
        self.rsi_upper = Decimal(str(self.get_config("rsi_upper", "55")))
        self.rsi_source = str(self.get_config("rsi_source", "snapshot")).lower()
        self.rsi_quote_amount = Decimal(str(self.get_config("rsi_quote_amount", "1")))

        self.allocation_pct = Decimal(str(self.get_config("allocation_pct", "0.95")))
        self.dust_buffer_base = Decimal(str(self.get_config("dust_buffer_base", "0.01")))
        self.dust_buffer_quote = Decimal(str(self.get_config("dust_buffer_quote", "1")))
        self.min_swap_value_usd = Decimal(str(self.get_config("min_swap_value_usd", "10")))
        self.min_expected_out_usd = Decimal(str(self.get_config("min_expected_out_usd", "9")))

        self.max_slippage_bps = int(self.get_config("max_slippage_bps", 30))
        self.max_price_impact_bps = Decimal(str(self.get_config("max_price_impact_bps", "80")))
        self.max_price_impact_pct = Decimal(str(self.get_config("max_price_impact_pct", "0.08")))
        self.max_gas_ratio = Decimal(str(self.get_config("max_gas_ratio", "0.05")))

        self.min_pool_tvl_usd = Decimal(str(self.get_config("min_pool_tvl_usd", "0")))
        self.min_pool_liquidity_raw = Decimal(
            str(self.get_config("min_pool_liquidity_raw", "1"))
        )

        self.cooldown_candles = int(self.get_config("cooldown_candles", 1))
        self.max_consecutive_failures = int(self.get_config("max_consecutive_failures", 3))
        self.force_action = str(self.get_config("force_action", "") or "").lower()

        self.regime_state = RegimeState.NEUTRAL.value
        self.position_state = RegimeState.NEUTRAL.value
        self.prev_rsi: Optional[Decimal] = None
        self.rsi_close_prices: list[Decimal] = []
        self.last_processed_candle = -1
        self.cooldown_until_candle = -1
        self.pending_target_state: Optional[str] = None
        self.consecutive_failed_swaps = 0
        self.halted = False

        self._last_decision: dict[str, Any] = {}

    def _timeframe_seconds(self) -> int:
        raw = self.rsi_timeframe.strip().lower()
        if raw.endswith("m"):
            return int(raw[:-1]) * 60
        if raw.endswith("h"):
            return int(raw[:-1]) * 3600
        return 300

    def _candle_index(self, timestamp: datetime) -> int:
        return int(timestamp.replace(tzinfo=UTC).timestamp() // self._timeframe_seconds())

    def _candle_bounds(self, candle_index: int) -> tuple[datetime, datetime]:
        timeframe_seconds = self._timeframe_seconds()
        start = datetime.fromtimestamp(candle_index * timeframe_seconds, tz=UTC)
        end = datetime.fromtimestamp((candle_index + 1) * timeframe_seconds, tz=UTC)
        return start, end

    def _record_decision(self, **fields: Any) -> None:
        self._last_decision = {
            "timestamp": datetime.now(UTC).isoformat(),
            **fields,
        }
        logger.info("decision=%s", self._last_decision)

    def _hold(self, reason: str, **fields: Any) -> Intent:
        self._record_decision(action="HOLD", reason=reason, **fields)
        return Intent.hold(reason=reason)

    def _append_close_price(self, close_price: Decimal) -> None:
        self.rsi_close_prices.append(close_price)
        max_points = max(self.rsi_period * 5, 100)
        if len(self.rsi_close_prices) > max_points:
            self.rsi_close_prices = self.rsi_close_prices[-max_points:]

    def _compute_window_rsi(self) -> Optional[Decimal]:
        if len(self.rsi_close_prices) < self.rsi_period + 1:
            return None

        window = self.rsi_close_prices[-(self.rsi_period + 1) :]
        gains = Decimal("0")
        losses = Decimal("0")
        for idx in range(1, len(window)):
            delta = window[idx] - window[idx - 1]
            if delta > 0:
                gains += delta
            elif delta < 0:
                losses += abs(delta)

        avg_gain = gains / Decimal(str(self.rsi_period))
        avg_loss = losses / Decimal(str(self.rsi_period))

        if avg_loss == 0 and avg_gain == 0:
            return Decimal("50")
        if avg_loss == 0:
            return Decimal("100")

        rs = avg_gain / avg_loss
        return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))

    def _fetch_pool_close_price(self, market: MarketSnapshot) -> Decimal:
        probe_amount = self.rsi_quote_amount
        if probe_amount <= 0:
            raise ValueError("rsi_quote_amount must be positive")

        best = market.best_dex_price(
            token_in=self.base_token,
            token_out=self.quote_token,
            amount=probe_amount,
            dexs=[self.protocol],
        )
        best_quote = getattr(best, "best_quote", None)
        if best_quote is None:
            raise ValueError("No quote returned for RSI source")

        amount_out = Decimal(str(getattr(best_quote, "amount_out", "0") or "0"))
        if amount_out <= 0:
            raise ValueError("Invalid quote amount_out for RSI source")

        return amount_out / probe_amount

    def _resolve_rsi(self, market: MarketSnapshot) -> tuple[Optional[Decimal], dict[str, Any]]:
        if self.rsi_source == "pool_quote_close":
            close_price = self._fetch_pool_close_price(market)
            self._append_close_price(close_price)
            current_rsi = self._compute_window_rsi()
            return current_rsi, {
                "rsi_source": self.rsi_source,
                "close_price": str(close_price),
                "close_count": len(self.rsi_close_prices),
            }

        rsi_data = market.rsi(
            self.base_token,
            period=self.rsi_period,
            timeframe=self.rsi_timeframe,
        )
        return Decimal(str(rsi_data.value)), {
            "rsi_source": self.rsi_source,
            "close_price": None,
            "close_count": len(self.rsi_close_prices),
        }

    def _build_swap_for_target(
        self,
        market: MarketSnapshot,
        target_state: RegimeState,
        signal: str,
        rsi_value: Optional[Decimal],
    ) -> Intent:
        if target_state == RegimeState.LONG_WMATIC:
            source_token = self.quote_token
            target_token = self.base_token
            dust_buffer = self.dust_buffer_quote
        else:
            source_token = self.base_token
            target_token = self.quote_token
            dust_buffer = self.dust_buffer_base

        try:
            source_balance: TokenBalance = market.balance(source_token)
            target_balance: TokenBalance = market.balance(target_token)
        except (BalanceUnavailableError, ValueError) as exc:
            return self._hold(
                "Balance data unavailable",
                signal=signal,
                rsi=str(rsi_value) if rsi_value is not None else None,
                error=str(exc),
            )

        available_amount = source_balance.balance - dust_buffer
        if available_amount <= 0:
            return self._hold(
                f"Insufficient {source_token} after dust buffer",
                signal=signal,
                rsi=str(rsi_value) if rsi_value is not None else None,
                source_balance=str(source_balance.balance),
                dust_buffer=str(dust_buffer),
            )

        swap_amount = available_amount * self.allocation_pct
        if swap_amount <= 0:
            return self._hold(
                "Swap amount is zero",
                signal=signal,
                source_balance=str(source_balance.balance),
                allocation_pct=str(self.allocation_pct),
            )

        trade_value_usd = source_balance.balance_usd * self.allocation_pct
        if trade_value_usd < self.min_swap_value_usd:
            return self._hold(
                "Trade value below minimum",
                signal=signal,
                trade_value_usd=str(trade_value_usd),
                min_swap_value_usd=str(self.min_swap_value_usd),
            )

        if not self.pool_address:
            return self._hold("pool_address is not configured", signal=signal)

        try:
            pool = market.pool_reserves(self.pool_address, chain=self.chain)
        except (PoolReservesUnavailableError, ValueError) as exc:
            return self._hold(
                "Pool reserves unavailable",
                signal=signal,
                error=str(exc),
                pool_address=self.pool_address,
            )

        pool_fee = int(getattr(pool, "fee_tier", 0) or 0)
        if pool_fee and pool_fee != self.pool_fee_bps:
            return self._hold(
                "Pool fee tier mismatch",
                signal=signal,
                expected_fee_bps=self.pool_fee_bps,
                actual_fee_bps=pool_fee,
            )

        liquidity = Decimal(str(getattr(pool, "liquidity", "0") or "0"))
        if liquidity < self.min_pool_liquidity_raw:
            return self._hold(
                "Pool liquidity too low",
                signal=signal,
                liquidity=str(liquidity),
                min_liquidity=str(self.min_pool_liquidity_raw),
            )

        pool_tvl = Decimal(str(getattr(pool, "tvl_usd", "0") or "0"))
        if self.min_pool_tvl_usd > 0 and pool_tvl < self.min_pool_tvl_usd:
            return self._hold(
                "Pool TVL below minimum",
                signal=signal,
                pool_tvl_usd=str(pool_tvl),
                min_pool_tvl_usd=str(self.min_pool_tvl_usd),
            )

        try:
            best = market.best_dex_price(
                token_in=source_token,
                token_out=target_token,
                amount=swap_amount,
                dexs=[self.protocol],
            )
        except (DexQuoteUnavailableError, ValueError) as exc:
            return self._hold("DEX quote unavailable", signal=signal, error=str(exc))

        best_quote = getattr(best, "best_quote", None)
        best_dex = str(getattr(best, "best_dex", "") or "")
        if best_quote is None:
            return self._hold("No best quote returned", signal=signal)
        if best_dex and best_dex != self.protocol:
            return self._hold(
                "Best quote not on required protocol",
                signal=signal,
                best_dex=best_dex,
                required_protocol=self.protocol,
            )

        quote_impact = Decimal(
            str(getattr(best_quote, "price_impact_bps", "0") or "0")
        )
        if quote_impact > self.max_price_impact_bps:
            return self._hold(
                "Price impact above threshold",
                signal=signal,
                price_impact_bps=str(quote_impact),
                max_price_impact_bps=str(self.max_price_impact_bps),
            )

        expected_out = Decimal(str(getattr(best_quote, "amount_out", "0") or "0"))
        if expected_out <= 0:
            return self._hold(
                "Expected output unavailable",
                signal=signal,
                expected_out=str(expected_out),
            )

        try:
            target_price = market.price(target_token)
        except (PriceUnavailableError, ValueError) as exc:
            return self._hold(
                "Target price unavailable",
                signal=signal,
                target_token=target_token,
                error=str(exc),
            )

        expected_out_usd = expected_out * target_price
        if expected_out_usd < self.min_expected_out_usd:
            return self._hold(
                "Expected output below minimum",
                signal=signal,
                expected_out_usd=str(expected_out_usd),
                min_expected_out_usd=str(self.min_expected_out_usd),
            )

        if not market.is_trade_worthwhile(
            amount_usd=trade_value_usd,
            chain=self.chain,
            max_gas_ratio=self.max_gas_ratio,
        ):
            return self._hold(
                "Gas cost too high for trade size",
                signal=signal,
                trade_value_usd=str(trade_value_usd),
                max_gas_ratio=str(self.max_gas_ratio),
            )

        self.pending_target_state = target_state.value
        self._record_decision(
            action="SWAP",
            signal=signal,
            rsi=str(rsi_value) if rsi_value is not None else None,
            signal_state=self.regime_state,
            position_state=self.position_state,
            target_state=target_state.value,
            from_token=source_token,
            to_token=target_token,
            source_balance=str(source_balance.balance),
            source_balance_usd=str(source_balance.balance_usd),
            target_balance=str(target_balance.balance),
            swap_amount=str(swap_amount),
            trade_value_usd=str(trade_value_usd),
            expected_out=str(expected_out),
            expected_out_usd=str(expected_out_usd),
            quote_price_impact_bps=str(quote_impact),
            max_slippage_bps=self.max_slippage_bps,
        )

        return Intent.swap(
            from_token=source_token,
            to_token=target_token,
            amount=swap_amount,
            max_slippage=Decimal(str(self.max_slippage_bps)) / Decimal("10000"),
            max_price_impact=self.max_price_impact_pct,
            protocol=self.protocol,
            chain=self.chain,
        )

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        if self.force_action == "flip_to_wmatic":
            return self._build_swap_for_target(
                market=market,
                target_state=RegimeState.LONG_WMATIC,
                signal="force_flip_to_wmatic",
                rsi_value=None,
            )
        if self.force_action == "flip_to_usdc":
            return self._build_swap_for_target(
                market=market,
                target_state=RegimeState.LONG_USDC,
                signal="force_flip_to_usdc",
                rsi_value=None,
            )
        return self._hold("Unknown force_action", force_action=self.force_action)

    def decide(self, market: MarketSnapshot) -> Intent | None:
        if self.halted:
            return self._hold(
                "Strategy halted after repeated swap failures",
                consecutive_failed_swaps=self.consecutive_failed_swaps,
                previous_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
                current_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
            )

        if self.force_action:
            return self._forced_intent(market)

        candle_index = self._candle_index(market.timestamp)

        if candle_index == self.last_processed_candle:
            return self._hold(
                "Awaiting confirmed candle close",
                candle_index=candle_index,
                last_processed_candle=self.last_processed_candle,
                previous_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
                current_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
            )

        if candle_index < self.cooldown_until_candle:
            self.last_processed_candle = candle_index
            return self._hold(
                "Cooldown active",
                candle_index=candle_index,
                cooldown_until_candle=self.cooldown_until_candle,
                previous_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
                current_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
            )

        try:
            current_rsi, rsi_meta = self._resolve_rsi(market)
        except (DexQuoteUnavailableError, ValueError, Exception) as exc:
            self.last_processed_candle = candle_index
            return self._hold(
                "RSI data unavailable",
                candle_index=candle_index,
                rsi_source=self.rsi_source,
                error=str(exc),
                previous_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
                current_rsi=None,
            )

        if current_rsi is None:
            self.last_processed_candle = candle_index
            return self._hold(
                "Warm-up candle, waiting for enough RSI source closes",
                candle_index=candle_index,
                rsi_source=self.rsi_source,
                close_count=rsi_meta.get("close_count"),
                required_closes=self.rsi_period + 1,
                previous_rsi=str(self.prev_rsi) if self.prev_rsi is not None else None,
                current_rsi=None,
            )

        if self.prev_rsi is None:
            self.prev_rsi = current_rsi
            self.last_processed_candle = candle_index
            return self._hold(
                "Warm-up candle, waiting for RSI cross",
                candle_index=candle_index,
                rsi=str(current_rsi),
                **rsi_meta,
            )

        previous_rsi = self.prev_rsi
        crossed_up = previous_rsi <= self.rsi_upper and current_rsi > self.rsi_upper
        crossed_down = previous_rsi >= self.rsi_lower and current_rsi < self.rsi_lower

        candle_start, candle_end = self._candle_bounds(candle_index)
        logger.info(
            "rsi_close_diagnostic candle_index=%s candle_start=%s candle_end=%s observed_at=%s timeframe=%s period=%s source=%s close_price=%s close_count=%s prev_rsi=%s current_rsi=%s lower=%s upper=%s crossed_up=%s crossed_down=%s signal_state=%s position_state=%s",
            candle_index,
            candle_start.isoformat(),
            candle_end.isoformat(),
            market.timestamp.astimezone(UTC).isoformat(),
            self.rsi_timeframe,
            self.rsi_period,
            rsi_meta.get("rsi_source"),
            rsi_meta.get("close_price"),
            rsi_meta.get("close_count"),
            str(previous_rsi),
            str(current_rsi),
            str(self.rsi_lower),
            str(self.rsi_upper),
            crossed_up,
            crossed_down,
            self.regime_state,
            self.position_state,
        )

        if not crossed_up and not crossed_down:
            if self.rsi_lower <= current_rsi <= self.rsi_upper:
                self.regime_state = RegimeState.NEUTRAL.value
            elif current_rsi > self.rsi_upper:
                self.regime_state = RegimeState.LONG_WMATIC.value
            else:
                self.regime_state = RegimeState.LONG_USDC.value
            self.prev_rsi = current_rsi
            self.last_processed_candle = candle_index
            return self._hold(
                "RSI in hold zone or no crossing event",
                candle_index=candle_index,
                previous_rsi=str(previous_rsi),
                current_rsi=str(current_rsi),
                signal_state=self.regime_state,
                position_state=self.position_state,
                **rsi_meta,
            )

        target_state = RegimeState.LONG_WMATIC if crossed_up else RegimeState.LONG_USDC
        signal = "cross_above_upper" if crossed_up else "cross_below_lower"

        if self.position_state == target_state.value:
            self.regime_state = target_state.value
            self.prev_rsi = current_rsi
            self.last_processed_candle = candle_index
            return self._hold(
                "Already in target state",
                signal=signal,
                signal_state=self.regime_state,
                position_state=self.position_state,
                target_state=target_state.value,
                current_rsi=str(current_rsi),
                **rsi_meta,
            )

        decision = self._build_swap_for_target(
            market=market,
            target_state=target_state,
            signal=signal,
            rsi_value=current_rsi,
        )

        self.prev_rsi = current_rsi
        self.last_processed_candle = candle_index
        return decision

    def on_intent_executed(self, intent, success: bool, result):
        intent_type = getattr(getattr(intent, "intent_type", None), "value", "")
        if intent_type != "SWAP":
            return

        tx_hash = getattr(result, "tx_hash", None)
        self._last_decision["tx_success"] = success
        self._last_decision["tx_hash"] = tx_hash
        self._last_decision["tx_result"] = getattr(result, "extracted_data", {})

        if success:
            self.consecutive_failed_swaps = 0
            if self.pending_target_state:
                self.regime_state = self.pending_target_state
                self.position_state = self.pending_target_state
            self.pending_target_state = None
            self.cooldown_until_candle = self.last_processed_candle + self.cooldown_candles
            logger.info(
                "swap_executed success=%s tx_hash=%s next_state=%s",
                success,
                tx_hash,
                self.regime_state,
            )
            return

        self.pending_target_state = None
        self.consecutive_failed_swaps += 1
        if self.consecutive_failed_swaps >= self.max_consecutive_failures:
            self.halted = True
        logger.warning(
            "swap_failed failures=%s halted=%s tx_hash=%s",
            self.consecutive_failed_swaps,
            self.halted,
            tx_hash,
        )

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "regime_state": self.regime_state,
            "position_state": self.position_state,
            "prev_rsi": str(self.prev_rsi) if self.prev_rsi is not None else None,
            "rsi_close_prices": [str(v) for v in self.rsi_close_prices],
            "last_processed_candle": self.last_processed_candle,
            "cooldown_until_candle": self.cooldown_until_candle,
            "pending_target_state": self.pending_target_state,
            "consecutive_failed_swaps": self.consecutive_failed_swaps,
            "halted": self.halted,
            "last_decision": self._last_decision,
        }

    def load_persistent_state(self, state: dict[str, Any]):
        if not state:
            return
        self.regime_state = str(state.get("regime_state", RegimeState.NEUTRAL.value))
        self.position_state = str(state.get("position_state", self.regime_state))
        prev_rsi = state.get("prev_rsi")
        self.prev_rsi = Decimal(str(prev_rsi)) if prev_rsi is not None else None
        close_prices = state.get("rsi_close_prices", [])
        self.rsi_close_prices = [Decimal(str(v)) for v in close_prices]
        self.last_processed_candle = int(state.get("last_processed_candle", -1))
        self.cooldown_until_candle = int(state.get("cooldown_until_candle", -1))
        self.pending_target_state = state.get("pending_target_state")
        self.consecutive_failed_swaps = int(state.get("consecutive_failed_swaps", 0))
        self.halted = bool(state.get("halted", False))
        self._last_decision = dict(state.get("last_decision", {}))

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "poly_t_a_swap_r_s_i",
            "chain": self.chain,
            "wallet": self.wallet_address,
            "signal_state": self.regime_state,
            "position_state": self.position_state,
            "pending_target_state": self.pending_target_state,
            "prev_rsi": str(self.prev_rsi) if self.prev_rsi is not None else None,
            "rsi_source": self.rsi_source,
            "rsi_close_count": len(self.rsi_close_prices),
            "cooldown_until_candle": self.cooldown_until_candle,
            "consecutive_failed_swaps": self.consecutive_failed_swaps,
            "halted": self.halted,
            "last_decision": self._last_decision,
        }

    def get_open_positions(self):
        from almanak.framework.teardown import (
            PositionInfo,
            PositionType,
            TeardownPositionSummary,
        )

        positions: list[PositionInfo] = []
        try:
            market = self.create_market_snapshot()
            base_balance: TokenBalance = market.balance(self.base_token)
            if base_balance.balance > self.dust_buffer_base:
                positions.append(
                    PositionInfo(
                        position_type=PositionType.TOKEN,
                        position_id="poly_t_a_swap_r_s_i_wmatic",
                        chain=self.chain,
                        protocol=self.protocol,
                        value_usd=base_balance.balance_usd,
                        details={
                            "asset": self.base_token,
                            "balance": str(base_balance.balance),
                            "quote_token": self.quote_token,
                        },
                    )
                )
        except (BalanceUnavailableError, ValueError, RuntimeError):
            logger.warning("Failed to query open positions for teardown")

        return TeardownPositionSummary(
            strategy_id=getattr(self, "strategy_id", "poly_t_a_swap_r_s_i"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        from almanak.framework.teardown import TeardownMode

        snapshot = market or self.create_market_snapshot()
        try:
            base_balance: TokenBalance = snapshot.balance(self.base_token)
        except (BalanceUnavailableError, ValueError, RuntimeError):
            return []

        if base_balance.balance <= self.dust_buffer_base:
            return []

        max_slippage = (
            Decimal("0.03")
            if mode == TeardownMode.HARD
            else Decimal(str(self.max_slippage_bps)) / Decimal("10000")
        )

        return [
            Intent.swap(
                from_token=self.base_token,
                to_token=self.quote_token,
                amount="all",
                max_slippage=max_slippage,
                protocol=self.protocol,
                chain=self.chain,
            )
        ]


def _safe(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Enum):
        return getattr(v, "value", str(v))
    return v


if __name__ == "__main__":
    print("PolyTASwapRSIStrategy")
