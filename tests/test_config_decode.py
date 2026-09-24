import base64
import json

import pytest

from mapex import config
from mapex.ctrader import decode as d


def test_defaults_start_without_secrets():
    s = config.load({})
    assert s.trading_mode == "paper" and not s.confirm_live
    assert s.lots == {} and not s.tradable("XAUUSD")  # M6: no lot -> never trades
    assert any("LOT_XAUUSD missing" in w for w in s.warnings)
    assert any("volume" in w for w in s.warnings)  # P6 warning without a volume


def test_lot_above_max_refused():
    s = config.load({"LOT_XAUUSD": "2.0", "LOT_BTCUSD": "0.01", "MAX_LOT": "1.0"})
    assert "XAUUSD" not in s.lots and s.lots["BTCUSD"] == 0.01
    assert any("LOT_XAUUSD" in e for e in s.errors)


def test_unknown_mode_falls_back_to_paper():
    s = config.load({"TRADING_MODE": "yolo"})
    assert s.trading_mode == "paper"
    assert config.load({"TRADING_MODE": "live", "CONFIRM_LIVE_ACCOUNT": "yes"}).confirm_live


def test_volume_cents_mapping():
    assert config.volume_cents(0.10, 100) == 1000  # 0.10 lot gold = 10 oz = 1000 cents
    assert config.volume_cents(0.01, 1) == 1
    assert config.lot_mapping_line("XAUUSD", 0.10, 100) == "XAUUSD 0.10 lot = 1000 cents = 10 units"
    assert d.check_volume(0.10, 100, 1.0) == 1000
    with pytest.raises(d.DecodeError):
        d.check_volume(None, 100, 1.0)
    with pytest.raises(d.DecodeError):
        d.check_volume(1.5, 100, 1.0)
    with pytest.raises(d.DecodeError):
        d.check_volume(0.001, 1, 1.0)  # 0.1 cent


def test_units():
    assert d.to_display(2650450, 3) == 2650.45
    assert d.to_points(3.57, 3) == 3570
    assert d.price_field(2650.456, 2, [1500, 14000]) == 2650.46
    with pytest.raises(d.DecodeError):
        d.price_field(2650450, 2, [1500, 14000])  # M5: pipettes written into a price field
    assert d.calibrate_digits(2650450, [None, None, 3], [1500, 14000]) == 3
    with pytest.raises(d.DecodeError):
        d.calibrate_digits(2650450, [2], [1500, 14000])  # never guesses
    assert d.decode_position_price(2650450, 3, [1500, 14000]) == 2650.45
    assert d.decode_position_price(2650.45, 3, [1500, 14000]) == 2650.45
    assert d.decode_position_price(0, 3, [1500, 14000]) is None


def _tok(env):
    head = base64.urlsafe_b64encode(json.dumps({"plant": "ctrader", "environment": env}).encode()).decode().rstrip("=")
    return f"{head}.payload.sig"


def test_parse_config_forms_and_environment():
    t = _tok("demo")
    frag = f'"url": "https://mcp.ctrader.com/trading/mcp", "headers": {{ "Authorization": "Bearer {t}" }}'
    assert d.parse_mcp_config(frag) == ("https://mcp.ctrader.com/trading/mcp", t)
    full = json.dumps({"mcpServers": {"ctrader": {"url": "https://x/trading/mcp",
                                                  "headers": {"Authorization": f"Bearer {t}"}}}})
    assert d.parse_mcp_config(full) == ("https://x/trading/mcp", t)
    assert d.parse_mcp_config(t) == (d.DEFAULT_URL, t)
    assert d.parse_mcp_config(f"Bearer {t}") == (d.DEFAULT_URL, t)
    assert d.token_environment(t) == "demo"
    assert d.token_environment(_tok("live")) == "live"
    assert d.token_environment("garbage.x.y") == "unknown"
    masked = d.mask(t)
    assert t not in masked and masked.startswith(t[:4]) and masked.endswith(t[-4:])
    with pytest.raises(d.DecodeError):
        d.parse_mcp_config("   ")


def test_exact_ctrader_web_copy_configuration():
    """What the 'Copy configuration' button gives (multi-line, no outer braces), pasted as one Railway variable."""
    head = base64.urlsafe_b64encode(json.dumps({"plant": "icmarkets", "environment": "live"},
                                               separators=(",", ":")).encode()).decode().rstrip("=")
    for t in (f"{head}.payload.sig", f"{head}XYZabc123"):  # with or without dots
        block = f'"url": "https://mcp.ctrader.com/trading/mcp",\n"headers": {{\n  "Authorization": "Bearer {t}"\n}}'
        for raw in (block, block.replace("\n", " "), f"  {block}  "):
            assert d.parse_mcp_config(raw) == ("https://mcp.ctrader.com/trading/mcp", t)
        assert d.parse_mcp_config(t) == (d.DEFAULT_URL, t)  # "Copy token" into CTRADER_MCP_CONFIG
    assert d.token_environment(f"{head}.payload.sig") == "live"
    s = config.load({"CTRADER_MCP_CONFIG": block, "CTRADER_MCP_URL": "", "CTRADER_MCP_TOKEN": ""})
    assert not any("CTRADER" in w for w in s.warnings)
