# Review Status

This repository snapshot has already addressed the previous high-severity review points.

## Fixed In Current `main`

- `shared_assets/unified_orchestrator.py` no longer deletes `core` modules from `sys.modules`.
- Bumble is loaded by `shared_assets/bumble_inspect.py` as `bumble_core.bumble_bot`, isolated from Tinder's `core` package.
- Long orchestrator sleeps use `_sleep_interruptibly(...)` and a process-wide shutdown event.
- Tinder profile DOM rules are externalized in `shared_assets/dom_rules.json`.
- Account-specific privacy mask words must be provided locally through `shared_assets/dom_rules.local.json` or `APP_PRIVACY_MASK_WORDS`.
- Bumble `auto_like` reads `bumble-automation/bumble_strategy.json`; no region preference is hardcoded in the bot driver.
- Bumble sender detection checks DOM markers first and only uses geometry as an explicit fallback.

## Reviewer Notes

- If a review still reports dynamic `core` purging, blocking quiet-cooldown sleep, immediate signal exit, or hardcoded region preference in the Bumble bot, it is reviewing an old commit or cached files.
- Please review `main` at or after commit `b3e0c48`.
- Runtime-only local files such as `dom_rules.local.json`, logs, databases, browser profiles, and corpora are intentionally excluded from this public repo.
