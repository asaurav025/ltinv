"""Daily digest: what needs your attention, as markdown in data/digests/YYYY/."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from ltinv.fetch import kite as kite_mod

ICON = {"fatal": "FATAL", "serious": "SERIOUS", "watch": "watch"}


def _flag_state(f: sqlite3.Row) -> str:
    if f["status"] == "dismissed":
        return "dismissed"
    if f["llm_confirmed"] == 1:
        return "confirmed"
    if f["llm_confirmed"] == 0:
        return "dismissed after reading"
    return "not judged yet: run red-flag-scan in Claude Code"


def daily(conn: sqlite3.Connection, day: date) -> tuple[str, bool]:
    """(markdown, needs_attention). Attention = a new flag today or an open proposal."""
    d = day.isoformat()
    flags = conn.execute(
        """SELECT f.*, fl.url, fl.subject AS fsubject, fl.category FROM flags f
           LEFT JOIN filings fl ON fl.id=f.filing_id
           WHERE substr(f.fired_at,1,10)=?
              OR (f.status='open' AND f.llm_confirmed IS NULL AND f.filing_id IS NOT NULL)
           ORDER BY CASE f.severity WHEN 'fatal' THEN 0 WHEN 'serious' THEN 1 ELSE 2 END, f.id""",
        (d,)).fetchall()
    props = conn.execute("SELECT * FROM proposals WHERE status='open' ORDER BY id").fetchall()
    filings = conn.execute(
        "SELECT * FROM filings WHERE substr(filed_at,1,10)=? ORDER BY symbol, filed_at", (d,)).fetchall()
    errors = conn.execute(
        """SELECT entry, step, detail, finished_at FROM runs
           WHERE status='error' AND substr(started_at,1,10)=? ORDER BY id""", (d,)).fetchall()
    last_filings = conn.execute(
        "SELECT MAX(finished_at) FROM runs WHERE step='filings' AND status='ok'").fetchone()[0]

    out = [f"# ltinv daily — {d}", ""]
    out.append(f"Kite: {kite_mod.session_status(conn)}. Last filings check: {last_filings or 'never'}.")
    out.append("")
    out.append(f"## Red flags ({len(flags)})")
    if not flags:
        out.append("None.")
    for f in flags:
        kill = f" — **thesis kill criterion `{f['kill_criterion']}`**" if f["kill_criterion"] else ""
        what = f["fsubject"] or f["detail"] or ""
        link = f" — [filing]({f['url']})" if f["url"] else ""
        out.append(f"- **{ICON[f['severity']]}** {f['symbol']} `{f['rule_id']}` (#{f['id']}){kill}: "
                   f"{_flag_state(f)}. {what[:200]}{link}")
        if f["llm_rationale"]:
            out.append(f"  - Verdict: {f['llm_rationale']}")
    out.append("")
    out.append(f"## Open proposals ({len(props)})")
    if not props:
        out.append("None.")
    for p in props:
        band = f" limit {p['limit_low']}–{p['limit_high']}" if p["limit_low"] else ""
        out.append(f"- #{p['id']} {p['side']} {p['qty']} {p['symbol']}{band} — {p['reason']} "
                   f"(expires {p['expires_at']})")
    out.append("")
    out.append(f"## Filings for your stocks today ({len(filings)})")
    if not filings:
        out.append("None.")
    for f in filings:
        cat = f"{f['category']}: " if f["category"] else ""
        link = f" — [doc]({f['url']})" if f["url"] else ""
        out.append(f"- {f['symbol']} {f['filed_at'][11:16]} {cat}{(f['subject'] or '')[:160]}{link}")
    if errors:
        out += ["", f"## Problems ({len(errors)})"]
        out += [f"- {e['entry']}/{e['step']}: {e['detail']}" for e in errors]
    attention = any(substr == d for substr in (f["fired_at"][:10] for f in flags)) or bool(props)
    return "\n".join(out) + "\n", attention


def write(data_dir: Path, day: date, markdown: str) -> Path:
    path = data_dir / "digests" / f"{day:%Y}" / f"daily-{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)
    return path
