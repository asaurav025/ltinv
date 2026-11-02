"""Kite Connect over plain HTTPS (requests), read-only in v1.

pykiteconnect is not used: it pulls in Twisted/Autobahn for the live ticker, which ltinv
never needs. The REST calls used here are small and stable:
  login page   GET  {login_url}?v=3&api_key=...           (in the browser)
  session      POST /session/token  api_key, request_token, checksum=sha256(key+token+secret)
  holdings     GET  /portfolio/holdings
  trades       GET  /trades                                (current day only)
Every authenticated call sends `Authorization: token <api_key>:<access_token>` and
`X-Kite-Version: 3`. Access tokens expire at 06:00 IST the next morning.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from ltinv.config import Settings
from ltinv.db import repo
from ltinv.db.repo import Holding
from ltinv.timeutil import IST, iso, kite_session_expiry, now_ist


class KiteError(Exception):
    pass


class KiteAuthError(KiteError):
    """Token missing, expired or rejected: a fresh `ltinv kite login` is needed."""


_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")


def parse_request_token(text: str) -> str:
    """Accepts the full redirect URL Kite sent you to, or the bare request_token."""
    text = text.strip()
    if "request_token=" in text or text.startswith("http"):
        query = parse_qs(urlparse(text).query)
        status = (query.get("status") or [""])[0]
        if status and status != "success":
            raise KiteAuthError(f"Kite login did not succeed (status={status})")
        token = (query.get("request_token") or [""])[0]
    else:
        token = text
    if not _TOKEN_RE.match(token):
        raise KiteAuthError("no request_token found; paste the whole URL you landed on after login")
    return token


class KiteClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str | None = None,
        access_token: str | None = None,
        base_url: str = "https://api.kite.trade",
        login_base: str = "https://kite.zerodha.com/connect/login",
        timeout: float = 20,
        http: requests.Session | None = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.login_base = login_base
        self.timeout = timeout
        self.http = http or requests.Session()

    @classmethod
    def from_settings(cls, settings: Settings, access_token: str | None = None) -> "KiteClient":
        cfg = settings.load("kite")
        return cls(
            api_key=settings.require_env("KITE_API_KEY"),
            api_secret=settings.env("KITE_API_SECRET"),
            access_token=access_token,
            base_url=cfg.get("base_url", "https://api.kite.trade"),
            login_base=cfg.get("login_url", "https://kite.zerodha.com/connect/login"),
            timeout=float(cfg.get("timeout_s", 20)),
        )

    # -- login ----------------------------------------------------------------------

    def login_url(self) -> str:
        return f"{self.login_base}?v=3&api_key={self.api_key}"

    def checksum(self, request_token: str) -> str:
        if not self.api_secret:
            raise KiteAuthError("KITE_API_SECRET is not set")
        return hashlib.sha256(f"{self.api_key}{request_token}{self.api_secret}".encode()).hexdigest()

    def create_session(self, request_token: str) -> dict[str, Any]:
        data = self._call(
            "POST", "/session/token",
            data={"api_key": self.api_key, "request_token": request_token,
                  "checksum": self.checksum(request_token)},
            auth=False,
        )
        if not data.get("access_token"):
            raise KiteError("session response had no access_token")
        self.access_token = data["access_token"]
        return data

    # -- reads ----------------------------------------------------------------------

    def holdings(self) -> list[Holding]:
        out = []
        for h in self._call("GET", "/portfolio/holdings") or []:
            qty = int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
            out.append(Holding(
                symbol=str(h["tradingsymbol"]).upper(),
                exchange=h.get("exchange"),
                isin=h.get("isin"),
                qty=qty,
                avg_price=float(h["average_price"]) if h.get("average_price") is not None else None,
            ))
        return out

    def trades(self) -> list[dict[str, Any]]:
        return self._call("GET", "/trades") or []

    # -- transport --------------------------------------------------------------------

    def _call(self, method: str, path: str, data: dict | None = None, auth: bool = True) -> Any:
        headers = {"X-Kite-Version": "3"}
        if auth:
            if not self.access_token:
                raise KiteAuthError("no Kite session; run `ltinv kite login`")
            headers["Authorization"] = f"token {self.api_key}:{self.access_token}"
        try:
            resp = self.http.request(method, self.base_url + path, data=data,
                                     headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise KiteError(f"{method} {path}: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            raise KiteError(f"{method} {path}: HTTP {resp.status_code}, non-JSON body") from None
        if resp.status_code == 403 or body.get("error_type") == "TokenException":
            raise KiteAuthError(f"Kite rejected the session: {body.get('message', 'HTTP 403')}")
        if resp.status_code >= 400 or body.get("status") != "success":
            raise KiteError(f"{method} {path}: {body.get('error_type', resp.status_code)}: "
                            f"{body.get('message', '')}")
        return body.get("data")


# --- session store ----------------------------------------------------------------------


def login(settings: Settings, conn: sqlite3.Connection, redirect: str,
          client: KiteClient | None = None, at: datetime | None = None) -> dict[str, str]:
    """Exchange the pasted redirect URL for today's session and store it."""
    client = client or KiteClient.from_settings(settings)
    data = client.create_session(parse_request_token(redirect))
    logged_in = at or now_ist()
    expires = kite_session_expiry(logged_in)
    repo.save_kite_session(conn, logged_in.astimezone(IST).date().isoformat(), data.get("user_id"),
                           data["access_token"], iso(logged_in), iso(expires))
    repo.add_journal(conn, "system", f"Kite login as {data.get('user_id', '?')}")
    return {"user_id": data.get("user_id") or "", "expires_at": iso(expires)}


def active_token(conn: sqlite3.Connection, at: datetime | None = None) -> str | None:
    row = repo.latest_kite_session(conn)
    if row is None:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= (at or now_ist()):
        return None
    return row["access_token"]


def session_status(conn: sqlite3.Connection, at: datetime | None = None) -> str:
    row = repo.latest_kite_session(conn)
    if row is None:
        return "never logged in"
    if active_token(conn, at) is None:
        return f"expired (last login {row['logged_in_at']})"
    return f"active as {row['user_id'] or '?'} until {row['expires_at']}"
