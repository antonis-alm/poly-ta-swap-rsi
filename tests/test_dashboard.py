from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from dashboard import ui


class _DummyColumn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@contextmanager
def _stub_streamlit_layout():
    mocks = {
        "columns": MagicMock(side_effect=lambda n: [_DummyColumn() for _ in range(n)]),
        "metric": MagicMock(),
        "subheader": MagicMock(),
        "divider": MagicMock(),
        "success": MagicMock(),
        "warning": MagicMock(),
        "error": MagicMock(),
        "caption": MagicMock(),
        "code": MagicMock(),
    }
    with patch.multiple(ui.st, **mocks):
        yield mocks


def test_build_ta_config_uses_rsi_parameters() -> None:
    cfg = {
        "rsi_period": 14,
        "rsi_lower": 45,
        "rsi_upper": 55,
    }

    ta_cfg = ui._build_ta_config(cfg)

    assert ta_cfg.indicator_name == "RSI"
    assert ta_cfg.indicator_period == 14
    assert ta_cfg.lower_threshold == 45
    assert ta_cfg.upper_threshold == 55


def test_render_custom_dashboard_calls_ta_template() -> None:
    strategy_config = {
        "rsi_period": 14,
        "rsi_lower": 45,
        "rsi_upper": 55,
        "max_consecutive_failures": 3,
    }
    session_state = {
        "regime_state": "LONG_WMATIC",
        "consecutive_failed_swaps": 0,
        "halted": False,
        "last_decision": {"action": "HOLD", "reason": "test"},
    }

    with _stub_streamlit_layout(), patch.object(ui, "render_ta_dashboard") as render_mock:
        ui.render_custom_dashboard(
            strategy_id="poly_t_a_swap_r_s_i",
            strategy_config=strategy_config,
            api_client=None,
            session_state=session_state,
        )

    render_mock.assert_called_once()
    _, _, _, ta_cfg = render_mock.call_args.args
    assert ta_cfg.indicator_name == "RSI"
    assert ta_cfg.lower_threshold == 45
    assert ta_cfg.upper_threshold == 55


def test_halted_state_shows_error_banner() -> None:
    session_state = {
        "regime_state": "LONG_USDC",
        "pending_target_state": None,
        "last_processed_candle": 10,
        "cooldown_until_candle": 12,
        "consecutive_failed_swaps": 3,
        "halted": True,
    }

    with _stub_streamlit_layout() as st_mocks, patch.object(ui, "render_ta_dashboard"):
        ui.render_custom_dashboard(
            strategy_id="poly_t_a_swap_r_s_i",
            strategy_config={"max_consecutive_failures": 3},
            api_client=None,
            session_state=session_state,
        )

    st_mocks["error"].assert_called_once()
    assert "Trading halted" in st_mocks["error"].call_args.args[0]
