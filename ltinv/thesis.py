"""Written investment theses: data/thesis/<SYMBOL>.md with YAML front matter.

The prose sections are for you; the front matter is what the suite checks against:
target weight, tracked metrics with tolerances, and kill criteria that must each be
machine-checkable (a metric threshold or a red-flag rule id).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

KNOWN_METRICS = (
    "roce", "ebitda_margin", "ocf_pat", "nd_ebitda", "rev_growth_yoy", "pat_growth_yoy",
    "receivable_days", "inventory_days", "promoter_pct", "pledge_pct",
)
OPS = ("<", "<=", ">", ">=")
REQUIRED_SECTIONS = ("How it earns", "Moat", "What breaks it")
PLACEHOLDERS = {"", "todo", "tbd", "optional."}
_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)
_SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")


class ThesisError(Exception):
    pass


def normalise_symbol(symbol: str) -> str:
    sym = symbol.strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise ThesisError(f"not a valid NSE/BSE symbol: {symbol!r}")
    return sym


def path_for(thesis_dir: Path, symbol: str) -> Path:
    return thesis_dir / f"{normalise_symbol(symbol)}.md"


def render_template(symbol: str, created: str) -> str:
    text = (resources.files("ltinv") / "templates" / "thesis.md").read_text()
    return text.format(symbol=normalise_symbol(symbol), created=created,
                       metrics=", ".join(KNOWN_METRICS))


def create(thesis_dir: Path, symbol: str, created: str) -> Path:
    path = path_for(thesis_dir, symbol)
    if path.exists():
        raise ThesisError(f"{path} already exists; use `ltinv thesis edit {symbol}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_template(symbol, created))
    return path


@dataclass
class Validation:
    symbol: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    target_weight_pct: float | None = None
    metrics: list[dict] = field(default_factory=list)
    kill_criteria: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sections(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1)
            out[current] = ""
        elif current is not None:
            out[current] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def validate(text: str, symbol: str, rule_ids: set[str], single_stock_max_pct: float | None) -> Validation:
    sym = normalise_symbol(symbol)
    v = Validation(symbol=sym)
    m = _FRONT_RE.match(text)
    if not m:
        v.errors.append("missing YAML front matter between --- lines at the top")
        return v
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        v.errors.append(f"front matter is not valid YAML: {exc}")
        return v
    if not isinstance(meta, dict):
        v.errors.append("front matter must be a mapping")
        return v
    body = m.group(2)

    if str(meta.get("symbol", "")).upper() != sym:
        v.errors.append(f"symbol in front matter ({meta.get('symbol')!r}) does not match {sym}")

    tw = meta.get("target_weight_pct")
    if not _is_num(tw) or tw <= 0:
        v.errors.append("target_weight_pct must be a number above 0")
    elif single_stock_max_pct is not None and tw > single_stock_max_pct:
        v.errors.append(f"target_weight_pct {tw} exceeds the single-stock limit "
                        f"{single_stock_max_pct} in config/risk.yaml")
    else:
        v.target_weight_pct = float(tw)

    metrics = meta.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        v.errors.append("metrics: list at least one metric the thesis depends on")
        metrics = []
    for i, item in enumerate(metrics, 1):
        where = f"metrics[{i}]"
        if not isinstance(item, dict):
            v.errors.append(f"{where}: must be a mapping")
            continue
        name = item.get("metric")
        if name not in KNOWN_METRICS:
            v.errors.append(f"{where}: unknown metric {name!r} (known: {', '.join(KNOWN_METRICS)})")
            continue
        tol = item.get("tolerance")
        if not _is_num(tol) or tol < 0:
            v.errors.append(f"{where} ({name}): tolerance must be a number >= 0")
            continue
        bv = item.get("buy_value")
        if bv is None:
            v.warnings.append(f"{where} ({name}): buy_value empty; fill it once metrics exist (Phase 3)")
        elif not _is_num(bv):
            v.errors.append(f"{where} ({name}): buy_value must be a number")
            continue
        v.metrics.append({"metric": name, "buy_value": bv, "tolerance": tol})

    kills = meta.get("kill_criteria")
    if not isinstance(kills, list) or not kills:
        v.errors.append("kill_criteria: state at least one pre-committed sell rule")
        kills = []
    seen_ids: set[str] = set()
    for i, item in enumerate(kills, 1):
        where = f"kill_criteria[{i}]"
        if not isinstance(item, dict):
            v.errors.append(f"{where}: must be a mapping")
            continue
        kid = str(item.get("id") or "").strip()
        if not kid:
            v.errors.append(f"{where}: needs an id")
            continue
        if kid in seen_ids:
            v.errors.append(f"{where}: duplicate id {kid!r}")
            continue
        seen_ids.add(kid)
        has_rule, has_metric = item.get("rule") is not None, item.get("metric") is not None
        if has_rule and has_metric:
            v.errors.append(f"{where} ({kid}): give `metric` or `rule`, not both")
            continue
        if not has_rule and not has_metric:
            v.errors.append(f"{where} ({kid}): needs a `metric` threshold or a red-flag `rule` — "
                            "a criterion like 'if management disappoints' is not checkable")
            continue
        if has_rule:
            if item["rule"] not in rule_ids:
                v.errors.append(f"{where} ({kid}): rule {item['rule']!r} is not in config/red-flags.yaml")
                continue
            v.kill_criteria.append({"id": kid, "rule": item["rule"], "text": item.get("text") or ""})
            continue
        name, op, value = item.get("metric"), item.get("op"), item.get("value")
        periods = item.get("periods", 1)
        problems = []
        if name not in KNOWN_METRICS:
            problems.append(f"unknown metric {name!r}")
        if op not in OPS:
            problems.append(f"op must be one of {' '.join(OPS)}")
        if not _is_num(value):
            problems.append("value must be a number")
        if not isinstance(periods, int) or isinstance(periods, bool) or periods < 1:
            problems.append("periods must be a whole number >= 1")
        if problems:
            v.errors.append(f"{where} ({kid}): " + "; ".join(problems))
            continue
        v.kill_criteria.append({"id": kid, "metric": name, "op": op, "value": value,
                                "periods": periods, "text": item.get("text") or ""})

    sections = _sections(body)
    for name in REQUIRED_SECTIONS:
        content = sections.get(name)
        if content is None:
            v.errors.append(f"section '## {name}' is missing")
        elif content.strip().lower() in PLACEHOLDERS:
            v.errors.append(f"section '## {name}' is empty")
    return v


def rule_ids(red_flags_cfg: dict) -> set[str]:
    return {r["id"] for r in red_flags_cfg.get("rules", []) if isinstance(r, dict) and "id" in r}
