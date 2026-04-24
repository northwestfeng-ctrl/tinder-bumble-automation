# Tinder/Bumble Automation

Sanitized source export for the Tinder/Bumble automation workspace.

This repository intentionally excludes runtime logs, browser profiles, local databases, private corpora, generated review drafts, screenshots, and real `.env` files.

## Layout

- `shared_assets/` - shared orchestration, reply engine, queue, persistence, and sending utilities.
- `tinder-automation/` - Tinder-specific browser automation and platform adapters.
- `bumble-automation/` - Bumble-specific browser automation and platform adapters.

## Local Setup Notes

Use `.env.example` files or environment variables for API keys and local credentials. Do not commit runtime state files, logs, SQLite databases, browser profiles, or real strategy review artifacts.
