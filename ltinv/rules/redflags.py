"""Red-flag rule engine: pure functions over DB rows (Architecture §7.1).

Rules come from config/red-flags.yaml (which exist, severity, wording) merged with
ltinv/rules/patterns.yaml (how each is detected in a filing). A flag records the rule, the
filing (or the date, for data rules), what matched, and the thesis kill criterion it hits.
Flags are only candidates: `flags confirm` (LLM) or a person settles them, and only
confirmed fatal/serious flags or kill criteria become proposals.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from importlib import resources

import yaml

from ltinv.timeutil import iso, now_ist

SEVERITIES = ("fatal", "serious", "watch")
_CATCHALL = re.compile(r"directors/kmp/smp/auditor/rta", re.I)


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    detect: str
    text: str
    all: tuple[re.Pattern, ...] = ()
    any: tuple[re.Pattern, ...] = ()
    none: tuple[re.Pattern, ...] = ()
    categories: tuple[str, ...] = ()
    reclassify_to: tuple[str, ...] = ()

    @property
    def specific(self) -> bool:
        return bool(self.all or self.any)

    def matches(self, text: str) -> str | None:
        """The matched snippet, or None."""
        if not self.specific:
            return None
        if self.none and any(p.search(text) for p in self.none):
            return None
        hits = []
        for p in self.all:
            m = p.search(text)
            if not m:
                return None
            hits.append(m.group(0))
        if self.any:
            m = next((m for p in self.any if (m := p.search(text))), None)
            if not m:
                return None
            hits.append(m.group(0))
        return " + ".join(hits)


def _compile(items) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.I) for p in (items or []))


def load_rules(config: dict, patterns: dict | None = None) -> dict[str, Rule]:
    if patterns is None:
        patterns = yaml.safe_load((resources.files("ltinv.rules") / "patterns.yaml").read_text()) or {}
    defs: dict[str, dict] = {}
    for rid, p in patterns.items():
        if isinstance(p, dict) and "default" in p:
            defs[rid] = dict(p["default"], id=rid)
    for r in config.get("rules", []):
        if isinstance(r, dict) and r.get("id"):
            defs[r["id"]] = {**defs.get(r["id"], {}), **r}
    rules = {}
    for rid, d in defs.items():
        if d.get("enabled") is False:
            continue
        if d.get("severity") not in SEVERITIES:
            raise ValueError(f"red-flag rule {rid}: severity must be one of {SEVERITIES}")
        p = d.get("match") or patterns.get(rid) or {}
        rules[rid] = Rule(
            id=rid, severity=d["severity"], detect=d.get("detect", "subject"), text=d.get("text", ""),
            all=_compile(p.get("all")), any=_compile(p.get("any")), none=_compile(p.get("none")),
            categories=tuple(c.lower() for c in p.get("categories", [])),
            reclassify_to=tuple(p.get("reclassify_to", [])),
        )
    return rules


def kill_map(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """{symbol: {rule_id: kill_criterion_id}} from validated theses."""
    out: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT symbol, kill_criteria_json FROM theses"):
        for k in json.loads(r["kill_criteria_json"]):
            if k.get("rule"):
                out.setdefault(r["symbol"], {})[k["rule"]] = k["id"]
    return out


def filing_text(category: str | None, subject: str | None) -> str:
    return _CATCHALL.sub("directors or kmp", f"{category or ''} | {subject or ''}")


def evaluate(rules: dict[str, Rule], category: str | None, subject: str | None) -> list[tuple[Rule, str]]:
    """Rules a filing triggers: every specific rule that matches; else category candidates."""
    text = filing_text(category, subject)
    hits = [(r, m) for r in rules.values() if (m := r.matches(text))]
    if hits:
        return hits
    cat = (category or "").strip().lower()
    return [(r, f"category: {category}") for r in rules.values() if cat and cat in r.categories]


def _insert_flag(conn, symbol, rule: Rule, filing_id, trigger_date, detail, kills) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO flags(symbol, filing_id, rule_id, severity, fired_at, status,
                                       trigger_date, detail, kill_criterion)
           VALUES (?,?,?,?,?, 'open', ?,?,?)""",
        (symbol, filing_id, rule.id, rule.severity, iso(now_ist()), trigger_date, detail,
         kills.get(symbol, {}).get(rule.id)),
    )
    return cur.rowcount == 1


def scan_filings(conn: sqlite3.Connection, rules: dict[str, Rule]) -> list[int]:
    """Run rules over filings not yet seen; returns new flag ids."""
    kills = kill_map(conn)
    new: list[int] = []
    rows = conn.execute("SELECT id, symbol, category, subject FROM filings WHERE seen_by_rules=0").fetchall()
    with conn:
        for f in rows:
            for rule, detail in evaluate(rules, f["category"], f["subject"]):
                if _insert_flag(conn, f["symbol"], rule, f["id"], None, detail, kills):
                    new.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute("UPDATE filings SET seen_by_rules=1 WHERE id=?", (f["id"],))
    return new


def price_volume_drops(conn: sqlite3.Connection, symbols: list[str], drop_pct: float = 10,
                       volume_mult: float = 3, window: int = 20) -> list[dict]:
    """Latest close down >= drop_pct on volume >= volume_mult x the prior `window`-day average."""
    out = []
    for sym in symbols:
        rows = conn.execute("SELECT date, c, volume FROM prices WHERE symbol=? ORDER BY date DESC LIMIT ?",
                            (sym, window + 2)).fetchall()
        if len(rows) < window + 2:
            continue
        last, prev, base = rows[0], rows[1], rows[1:window + 1]
        vols = [r["volume"] for r in base if r["volume"]]
        if not vols or not last["volume"] or not prev["c"]:
            continue
        change = (last["c"] / prev["c"] - 1) * 100
        mult = last["volume"] / (sum(vols) / len(vols))
        if change <= -drop_pct and mult >= volume_mult:
            out.append({"symbol": sym, "date": last["date"], "change_pct": round(change, 1),
                        "volume_mult": round(mult, 1)})
    return out


def scan_prices(conn: sqlite3.Connection, rules: dict[str, Rule], symbols: list[str]) -> list[int]:
    rule = rules.get("price_volume_drop")
    if rule is None:
        return []
    kills, new = kill_map(conn), []
    with conn:
        for d in price_volume_drops(conn, symbols):
            detail = f"close {d['change_pct']}% on {d['volume_mult']}x 20-day volume (read only, never a sell)"
            if _insert_flag(conn, d["symbol"], rule, None, d["date"], detail, kills):
                new.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return new
