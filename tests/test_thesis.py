import pytest

from ltinv import thesis

RULES = {"auditor_resignation", "pledge_invocation"}

GOOD = """---
symbol: INFY
created: 2026-11-02
target_weight_pct: 8
metrics:
  - {metric: roce, buy_value: 30, tolerance: 4}
  - {metric: ocf_pat, buy_value: , tolerance: 0.1}
kill_criteria:
  - {id: k1, metric: ocf_pat, op: "<", value: 0.7, periods: 2, text: "cash conversion breaks"}
  - {id: k2, rule: auditor_resignation}
---

# INFY

## How it earns
IT services for large enterprises.

## Moat
Switching costs.

## What breaks it
Pricing collapse from AI tooling.
"""


def check(text, symbol="INFY", limit=12):
    return thesis.validate(text, symbol, RULES, limit)


def test_good_thesis_validates():
    v = check(GOOD)
    assert v.ok, v.errors
    assert v.target_weight_pct == 8.0
    assert v.kill_criteria == [
        {"id": "k1", "metric": "ocf_pat", "op": "<", "value": 0.7, "periods": 2,
         "text": "cash conversion breaks"},
        {"id": "k2", "rule": "auditor_resignation", "text": ""},
    ]
    assert v.warnings == ["metrics[2] (ocf_pat): buy_value empty; fill it once metrics exist (Phase 3)"]


def test_fresh_template_is_rejected_with_reasons():
    v = check(thesis.render_template("infy", "2026-11-02"))
    assert not v.ok
    joined = "\n".join(v.errors)
    for needle in ("target_weight_pct", "tolerance", "needs a `metric` threshold",
                   "'## How it earns' is empty", "'## Moat' is empty", "'## What breaks it' is empty"):
        assert needle in joined


@pytest.mark.parametrize("old,new,needle", [
    ("target_weight_pct: 8", "target_weight_pct: 20", "exceeds the single-stock limit"),
    ("symbol: INFY", "symbol: TCS", "does not match"),
    ("{id: k2, rule: auditor_resignation}", "{id: k2, rule: ceo_vibes}", "not in config/red-flags.yaml"),
    ("{id: k2, rule: auditor_resignation}", "{id: k1, rule: auditor_resignation}", "duplicate id"),
    ("{id: k2, rule: auditor_resignation}", "{id: k2, text: 'if management disappoints'}",
     "is not checkable"),
    ('op: "<"', 'op: "=="', "op must be one of"),
    ("periods: 2", "periods: 0", "periods must be"),
    ("metric: ocf_pat, op", "metric: vibes, op", "unknown metric 'vibes'"),
    ("{id: k2, rule: auditor_resignation}", "{id: k2, rule: auditor_resignation, metric: roce}",
     "not both"),
    ("## Moat\nSwitching costs.", "## Moat\nTODO", "'## Moat' is empty"),
    ("## What breaks it", "## Risks", "'## What breaks it' is missing"),
])
def test_rejections(old, new, needle):
    assert old in GOOD
    v = check(GOOD.replace(old, new))
    assert not v.ok
    assert any(needle in e for e in v.errors), v.errors


def test_missing_front_matter():
    v = check("# INFY\n\n## Moat\nx\n")
    assert v.errors == ["missing YAML front matter between --- lines at the top"]


def test_create_refuses_overwrite(tmp_path):
    p = thesis.create(tmp_path, "infy", "2026-11-02")
    assert p.name == "INFY.md"
    with pytest.raises(thesis.ThesisError):
        thesis.create(tmp_path, "INFY", "2026-11-02")


@pytest.mark.parametrize("bad", ["", "RELIANCE INDUSTRIES", "../etc", "x" * 30])
def test_symbol_validation(bad):
    with pytest.raises(thesis.ThesisError):
        thesis.normalise_symbol(bad)
