# Security

## Reporting

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/asaurav025/ltinv/security/advisories/new),
not in a public issue.

## What this repo never contains

This is a public code repo used for a real portfolio. Personal state lives outside it,
under `LTINV_HOME`:

| Where | What | Protection |
| --- | --- | --- |
| `.env` | Kite API key/secret, Anthropic key | gitignored; on the VPS only |
| `config/` | your limits, peer sets, sectors (reveals holdings) | gitignored; created by `ltinv init` from `ltinv/config_examples/` |
| `data/` | DB (incl. the day's Kite access token), theses, reviews, journal | its own private git repo; `ltinv.db` is mode 600 and never committed |

`tests/test_public_repo.py` fails CI if any of these is tracked, and GitHub secret
scanning with push protection is enabled on the repo.

## Design limits worth knowing

- v1 never places orders. Kite access is read-only in practice (holdings, trades);
  `config/kite.yaml: execution_enabled: false` is the Phase 6 kill switch.
- Kite sessions need a manual login each trading day; automated TOTP login is
  deliberately unsupported.
- The day's Kite access token sits in the SQLite DB. Anyone with read access to
  `data/ltinv.db` can act as you on Kite until 06:00 IST the next day.
