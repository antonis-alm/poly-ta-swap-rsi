from decimal import Decimal
from typing import Any

import streamlit as st

from almanak.framework.dashboard.templates import get_rsi_config, render_ta_dashboard


def _to_decimal(value: Any, default: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _build_ta_config(strategy_config: dict[str, Any]):
    period = _to_int(strategy_config.get("rsi_period", 14), 14)
    overbought = float(_to_decimal(strategy_config.get("rsi_upper", "55"), "55"))
    oversold = float(_to_decimal(strategy_config.get("rsi_lower", "45"), "45"))
    return get_rsi_config(period=period, overbought=overbought, oversold=oversold)


def _render_regime_state(session_state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    st.subheader("Regime State Machine")

    regime_state = str(session_state.get("regime_state", "NEUTRAL"))
    pending_target = session_state.get("pending_target_state") or "None"
    last_processed = _to_int(session_state.get("last_processed_candle", -1), -1)
    cooldown_until = _to_int(session_state.get("cooldown_until_candle", -1), -1)
    cooldown_remaining = max(cooldown_until - last_processed, 0)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Current State", regime_state)
    with col2:
        st.metric("Pending Target", str(pending_target))
    with col3:
        st.metric("Last Candle", str(last_processed))
    with col4:
        st.metric("Cooldown Remaining", str(cooldown_remaining))

    failed_swaps = _to_int(session_state.get("consecutive_failed_swaps", 0), 0)
    max_failures = _to_int(strategy_config.get("max_consecutive_failures", 3), 3)
    halted = bool(session_state.get("halted", False))

    if halted:
        st.error(
            f"Trading halted after {failed_swaps}/{max_failures} consecutive failed swaps."
        )
    elif failed_swaps > 0:
        st.warning(
            f"Swap failures: {failed_swaps}/{max_failures}. Next failure may trigger halt."
        )
    else:
        st.success("Failure counter healthy (0 consecutive failed swaps).")


def _render_execution_controls(strategy_config: dict[str, Any]) -> None:
    st.subheader("Execution Controls")

    allocation_pct = _to_decimal(strategy_config.get("allocation_pct", "0.95"), "0.95")
    max_slippage_bps = _to_decimal(strategy_config.get("max_slippage_bps", "30"), "30")
    max_price_impact_bps = _to_decimal(
        strategy_config.get("max_price_impact_bps", "80"), "80"
    )
    max_gas_ratio = _to_decimal(strategy_config.get("max_gas_ratio", "0.05"), "0.05")
    min_swap_value = _to_decimal(strategy_config.get("min_swap_value_usd", "10"), "10")
    min_expected_out = _to_decimal(
        strategy_config.get("min_expected_out_usd", "9"), "9"
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Allocation", f"{float(allocation_pct * Decimal('100')):.1f}%")
    with col2:
        st.metric("Max Slippage", f"{float(max_slippage_bps / Decimal('100')):.2f}%")
    with col3:
        st.metric("Max Price Impact", f"{float(max_price_impact_bps / Decimal('100')):.2f}%")

    col4, col5, col6 = st.columns(3)
    with col4:
        st.metric("Max Gas Ratio", f"{float(max_gas_ratio * Decimal('100')):.1f}%")
    with col5:
        st.metric("Min Swap Value", f"${float(min_swap_value):.2f}")
    with col6:
        st.metric("Min Expected Out", f"${float(min_expected_out):.2f}")


def _render_last_decision(session_state: dict[str, Any]) -> None:
    st.subheader("Latest Decision")
    last_decision = session_state.get("last_decision") or {}

    action = last_decision.get("action", "HOLD")
    reason = str(last_decision.get("reason", "No decision recorded yet"))
    signal = str(last_decision.get("signal", "n/a"))
    rsi = str(last_decision.get("rsi", "n/a"))

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Action", str(action))
    with col2:
        st.metric("Signal", signal)
    with col3:
        st.metric("RSI", rsi)
    with col4:
        st.metric("Tx Success", str(last_decision.get("tx_success", "n/a")))

    st.caption(f"Reason: {reason}")

    tx_hash = last_decision.get("tx_hash")
    if tx_hash:
        st.code(str(tx_hash))


def render_custom_dashboard(
    strategy_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    ta_config = _build_ta_config(strategy_config)
    render_ta_dashboard(strategy_id, strategy_config, session_state, ta_config)

    st.divider()
    _render_regime_state(session_state, strategy_config)

    st.divider()
    _render_execution_controls(strategy_config)

    st.divider()
    _render_last_decision(session_state)
