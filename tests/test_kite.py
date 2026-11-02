import hashlib
from datetime import datetime

import pytest

from ltinv.fetch import kite
from ltinv.timeutil import IST, kite_session_expiry


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.requests.append((method, url, data, headers))
        return self.routes[(method, url.split("api.kite.trade")[1])]


def client(routes, token=None):
    return kite.KiteClient("key123", "secret456", access_token=token, http=FakeHTTP(routes))


@pytest.mark.parametrize("text", [
    "https://example.com/cb?action=login&type=login&status=success&request_token=AbC123xyz789",
    "AbC123xyz789",
    "  https://example.com/cb?request_token=AbC123xyz789  ",
])
def test_parse_request_token(text):
    assert kite.parse_request_token(text) == "AbC123xyz789"


@pytest.mark.parametrize("text", [
    "https://example.com/cb?status=cancelled&request_token=AbC123xyz789",
    "https://example.com/cb?status=success",
    "not a token!",
])
def test_parse_request_token_rejects(text):
    with pytest.raises(kite.KiteAuthError):
        kite.parse_request_token(text)


def test_create_session_sends_checksum():
    c = client({("POST", "/session/token"): Resp(200, {"status": "success",
                "data": {"access_token": "tok", "user_id": "AB1234"}})})
    data = c.create_session("req789")
    assert data["user_id"] == "AB1234" and c.access_token == "tok"
    method, _, form, headers = c.http.requests[0]
    assert form["checksum"] == hashlib.sha256(b"key123req789secret456").hexdigest()
    assert "Authorization" not in headers


def test_holdings_maps_fields_and_auth_header():
    c = client({("GET", "/portfolio/holdings"): Resp(200, {"status": "success", "data": [
        {"tradingsymbol": "INFY", "exchange": "NSE", "isin": "INE009A01021",
         "quantity": 8, "t1_quantity": 2, "average_price": 1500.5},
    ]})}, token="tok")
    [h] = c.holdings()
    assert (h.symbol, h.qty, h.avg_price, h.isin) == ("INFY", 10, 1500.5, "INE009A01021")
    headers = c.http.requests[0][3]
    assert headers["Authorization"] == "token key123:tok"
    assert headers["X-Kite-Version"] == "3"


def test_expired_token_raises_auth_error():
    c = client({("GET", "/portfolio/holdings"): Resp(403, {"status": "error",
                "error_type": "TokenException", "message": "Incorrect `api_key` or `access_token`."})},
               token="old")
    with pytest.raises(kite.KiteAuthError):
        c.holdings()


def test_no_token_raises_before_any_request():
    c = client({})
    with pytest.raises(kite.KiteAuthError):
        c.holdings()
    assert c.http.requests == []


@pytest.mark.parametrize("login,expiry", [
    ("2026-11-02T08:45:00+05:30", "2026-11-03T06:00:00+05:30"),
    ("2026-11-02T05:30:00+05:30", "2026-11-02T06:00:00+05:30"),
    ("2026-11-02T03:00:00+00:00", "2026-11-03T06:00:00+05:30"),  # 08:30 IST
])
def test_session_expiry_is_next_6am_ist(login, expiry):
    assert kite_session_expiry(datetime.fromisoformat(login)) == datetime.fromisoformat(expiry)


def test_login_stores_session_and_active_token(settings, conn):
    c = client({("POST", "/session/token"): Resp(200, {"status": "success",
                "data": {"access_token": "tok", "user_id": "AB1234"}})})
    at = datetime(2026, 11, 2, 9, 0, tzinfo=IST)
    out = kite.login(settings, conn, "https://x/cb?status=success&request_token=Req12345", client=c, at=at)
    assert out == {"user_id": "AB1234", "expires_at": "2026-11-03T06:00:00+05:30"}
    assert kite.active_token(conn, at=datetime(2026, 11, 2, 18, 30, tzinfo=IST)) == "tok"
    assert kite.active_token(conn, at=datetime(2026, 11, 3, 6, 0, tzinfo=IST)) is None
    assert kite.session_status(conn, at=datetime(2026, 11, 3, 7, 0, tzinfo=IST)).startswith("expired")
