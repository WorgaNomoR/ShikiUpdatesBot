# Agent guidance for ShikiUpdatesBot

## What this repository is
- A small Telegram bot implemented in `main.py`.
- Tracks a Shikimori user history and favourites, then sends notifications to Telegram subscribers.
- Uses `aiogram` for Telegram and `aiohttp` for HTTP requests.
- Contains tests in `tests/` and configuration at the top of `main.py`.

## Important files
- `main.py` — single entrypoint and main implementation.
- `README.md` — setup, environment variables, commands, and test instructions.
- `requirements.txt` / `requirements-dev.txt` — runtime and development dependencies.
- `tests/` — pytest-based coverage for storage, notification logic, parser logic, and Telegram send behavior.

## Key patterns and conventions
- Environment variables are required at runtime: `BOT_TOKEN`, `OWNER_ID`. Optional `DATA_DIR` controls persistent file location.
- The bot stores state in JSON files: `seen_ids.json`, `subscribers.json`, `seen_favourites.json`.
- The bot’s lifecycle is managed by `asyncio` with a polling loop created in `main()`.
- Use `pytest tests/` to validate changes; `tests/conftest.py` provides default env vars for test execution.
- Preserve existing behaviour for Shikimori event filtering and favourite notifications when modifying logic.

## Typical developer tasks
- Update message templates or event classification in `main.py`.
- Fix parser edge cases for Shikimori descriptions and score extraction.
- Improve notification filtering, storage handling, and broadcast flow.
- Add tests under `tests/` for any new behavior.

## How to run
- Install runtime dependencies: `pip install -r requirements.txt`
- Install test dependencies before running tests: `pip install -r requirements-dev.txt`
- Run the bot: `python main.py`
- Run tests: `pytest tests/`

## Notes for AI agents
- Avoid adding new top-level scripts; the repository is intended to stay small and centered on `main.py`.
- Do not commit actual bot tokens or owner IDs.
- When changing configuration defaults, document them in both `main.py` and `README.md`.
- Prefer minimal, behavior-preserving fixes, and verify with existing tests.
