"""Test FastAPI dashboard endpoints."""
import pytest
from fastapi.testclient import TestClient
from optiondesk.app import create_app


@pytest.fixture
def client():
    app = create_app(autostart=False)
    with TestClient(app) as c:
        yield c


def test_health_endpoint(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "provider" in data
    assert "ist_now" in data


def test_chain_endpoint(client):
    res = client.get("/api/chain?underlying=NIFTY&expiry_index=0&around_atm=10")
    assert res.status_code == 200
    data = res.json()
    assert data["underlying"] == "NIFTY"
    assert data["spot"] > 0
    assert len(data["rows"]) > 0
    assert "atm_iv" in data
    assert "iv_context" in data


def test_universe_endpoint(client):
    res = client.get("/api/universe")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) > 0
    assert "underlying" in data[0]
    assert "verdict" in data[0]


def test_rules_endpoint(client):
    res = client.get("/api/rules")
    assert res.status_code == 200
    data = res.json()
    assert "nifty_hedged_spreads" in data
    assert "stock_option_buying" in data
    assert "risk" in data


def test_scan_and_signals_endpoint(client):
    res_scan = client.post("/api/scan")
    assert res_scan.status_code == 200
    scan_data = res_scan.json()
    assert "signals" in scan_data
    assert scan_data["scanned"] > 0

    res_sig = client.get("/api/signals")
    assert res_sig.status_code == 200
    sigs = res_sig.json()
    assert isinstance(sigs, list)


def test_payoff_endpoint(client):
    payload = {
        "spot": 25000,
        "dte": 10,
        "lot_size": 65,
        "lots": 1,
        "iv": 0.15,
        "legs": [
            {"side": "SELL", "option_type": "PE", "strike": 24800, "premium": 80},
            {"side": "BUY", "option_type": "PE", "strike": 24600, "premium": 30},
        ]
    }
    res = client.post("/api/payoff", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["max_profit"] == (80 - 30) * 65  # 3250
    assert data["max_loss"] == (200 - 50) * 65   # 9750
    assert len(data["breakevens"]) == 1
    assert abs(data["breakevens"][0] - 24750.0) < 1.0


def test_config_update(client):
    res = client.post("/api/config", json={"account": {"max_open_trades": 7}})
    assert res.status_code == 200
    data = res.json()
    assert data["account"]["max_open_trades"] == 7
