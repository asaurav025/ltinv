"""Settle a red flag: confirm or dismiss it, after a person or Claude Code has read the filing.

No LLM runs here (the VPS has none by design). Judgement happens in Claude Code on your own
machine (the red-flag-scan skill), which records the verdict with `ltinv flags resolve`.
The guardrails are enforced here, whoever resolves:
- severity may be kept or lowered, never raised above the (final) rule's ceiling
- a reclassification must be one of the candidate rule's `reclassify_to` targets
- confirming requires a verbatim quote from the filing
"""

from __future__ import annotations

import sqlite3

from ltinv.db import repo
from ltinv.rules.redflags import SEVERITIES, Rule, kill_map

RANK = {s: i for i, s in enumerate(SEVERITIES)}  # fatal=0 (highest) .. watch=2


class ResolveError(Exception):
    pass


def options(flag: sqlite3.Row, rules: dict[str, Rule]) -> list[Rule]:
    """The rules a flag may be resolved as: its own, then its reclassification targets."""
    rule = rules.get(flag["rule_id"])
    if rule is None:
        raise ResolveError(f"rule {flag['rule_id']} is no longer configured")
    return [rule] + [rules[r] for r in rule.reclassify_to if r in rules]


def describe(conn: sqlite3.Connection, flag_id: int, rules: dict[str, Rule]) -> dict:
    """Everything needed to judge a flag, as plain data (for `flags show --json`)."""
    flag = conn.execute("SELECT * FROM flags WHERE id=?", (flag_id,)).fetchone()
    if flag is None:
        raise ResolveError(f"no flag #{flag_id}")
    filing = conn.execute("SELECT * FROM filings WHERE id=?", (flag["filing_id"],)).fetchone() \
        if flag["filing_id"] else None
    return {
        "flag": {k: flag[k] for k in flag.keys()},
        "filing": {k: filing[k] for k in filing.keys() if k != "raw_json"} if filing else None,
        "options": [{"rule_id": r.id, "severity_ceiling": r.severity, "text": r.text}
                    for r in options(flag, rules)],
        "thesis_kill_criteria": kill_map(conn).get(flag["symbol"], {}),
    }


def resolve(conn: sqlite3.Connection, flag_id: int, rules: dict[str, Rule], *, confirmed: bool,
            rationale: str, quote: str = "", rule_id: str | None = None,
            severity: str | None = None, by: str = "manual") -> dict:
    flag = conn.execute("SELECT * FROM flags WHERE id=?", (flag_id,)).fetchone()
    if flag is None:
        raise ResolveError(f"no flag #{flag_id}")
    if flag["llm_confirmed"] is not None or flag["status"] != "open":
        raise ResolveError(f"flag #{flag_id} is already resolved ({flag['status']})")
    allowed = {r.id: r for r in options(flag, rules)}
    rid = rule_id or flag["rule_id"]
    if rid not in allowed:
        raise ResolveError(f"flag #{flag_id} can be resolved only as: {', '.join(allowed)}")
    ceiling = allowed[rid].severity
    sev = severity or ceiling
    if sev not in RANK:
        raise ResolveError(f"severity must be one of {', '.join(SEVERITIES)}")
    if RANK[sev] < RANK[ceiling]:
        raise ResolveError(f"severity {sev} is above the {rid} ceiling ({ceiling}); it can only be lowered")
    quote, rationale = quote.strip(), rationale.strip()
    if not rationale:
        raise ResolveError("give a rationale: what happened, and why it is or is not the red flag")
    if confirmed and not quote:
        raise ResolveError("confirming needs --quote: a sentence copied exactly from the filing")
    note = f"[{by}] {rationale}" + (f' Quote: "{quote}"' if quote else "")
    kill = kill_map(conn).get(flag["symbol"], {}).get(rid) if confirmed else None
    with conn:
        try:
            conn.execute(
                """UPDATE flags SET llm_confirmed=?, llm_rationale=?, rule_id=?, severity=?,
                                    kill_criterion=?, status=? WHERE id=?""",
                (1 if confirmed else 0, note, rid, sev, kill, "open" if confirmed else "dismissed", flag_id))
        except sqlite3.IntegrityError:
            raise ResolveError(f"{flag['symbol']} already has a {rid} flag for this filing; "
                               f"dismiss #{flag_id} instead") from None
        repo.add_journal(conn, "system", f"flag #{flag_id} {rid} {'confirmed' if confirmed else 'dismissed'} "
                         f"by {by}: {rationale[:200]}", symbol=flag["symbol"], ref_id=f"flag:{flag_id}",
                         commit=False)
    return {"flag_id": flag_id, "confirmed": confirmed, "rule_id": rid, "severity": sev,
            "kill_criterion": kill}


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Flags from filings that nobody has judged yet."""
    return conn.execute(
        """SELECT f.*, fl.company, fl.subject AS filing_subject, fl.url FROM flags f
           LEFT JOIN filings fl ON fl.id=f.filing_id
           WHERE f.status='open' AND f.llm_confirmed IS NULL AND f.filing_id IS NOT NULL
           ORDER BY CASE f.severity WHEN 'fatal' THEN 0 WHEN 'serious' THEN 1 ELSE 2 END, f.id""").fetchall()
