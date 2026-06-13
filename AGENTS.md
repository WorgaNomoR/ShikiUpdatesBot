# Agent guidance for ShikiUpdatesBot

## What this repository is
- A Telegram bot implemented primarily in `main.py`.
- Tracks a Shikimori user's history and favourites, then sends notifications to Telegram subscribers.
- Collects quarterly statistics (genres, studios, scores, etc.) and sends the owner an automatic report at the start of each quarter.
- Uses `aiogram` for Telegram and `aiohttp` for HTTP requests (both client, for Shikimori, and server, for healthcheck).
- Contains tests in `tests/` and configuration at the top of `main.py`.

## Important files
- `main.py` — main entrypoint and implementation: config, message bank, parsers, Shikimori API calls, statistics, command handlers, polling loop.
- `healthcheck.py` — isolated HTTP healthcheck server with a heartbeat watchdog. Imports nothing from `main.py`; the dependency is one-way (`main.py` calls `heartbeat()` and `start_health_server()`).
- `README.md` — setup, environment variables, commands, statistics, healthcheck, and test instructions.
- `requirements.txt` / `requirements-dev.txt` — runtime and development dependencies.
- `tests/` — pytest-based coverage for storage, notification logic, parser logic, statistics, and Telegram send behavior.

## Key patterns and conventions
- Environment variables: `BOT_TOKEN`, `OWNER_ID` are required at runtime. Optional `DATA_DIR` (default `/data`) controls persistent file location; optional `PORT` (default `8080`) controls the healthcheck server port.
- The bot stores state in JSON files under `DATA_DIR`: `seen_ids.json`, `subscribers.json`, `seen_favourites.json`, `stats_all.json`, `stats_current.json`, and snapshots in `quarters/`.
- All file writes go through `_atomic_write()` (temp file + `os.replace()`) for crash safety.
- All Telegram messages use `ParseMode.HTML`; user-facing strings from the API are escaped via `h()` (`html.escape`).
- **Stability is the top priority.** Every function must be exception-safe: unexpected or missing data must never crash the bot. Network fetches return `None` on any error (not empty collections) to distinguish API failures from genuinely empty results. Statistics degrade gracefully — a failed export or GraphQL call yields a report without enriched metadata rather than a crash.
- Statistics data sources: user lists come from the public `list_export` JSON endpoints (no auth); title metadata comes from the GraphQL `animes`/`mangas` batch queries with `censored: false`. Do not reintroduce per-title REST calls or OAuth — these were evaluated and rejected.
- The bot's lifecycle is managed by `asyncio`: `polling_loop` runs as a background task created in `main()`, alongside `dp.start_polling` and the healthcheck server.
- Use `pytest tests/` to validate changes; `tests/conftest.py` provides default env vars for test execution.
- Preserve existing behaviour for Shikimori event filtering, favourite notifications, and statistics aggregation when modifying logic.

## Typical developer tasks
- Update message templates or event classification in `main.py`.
- Fix parser edge cases for Shikimori descriptions and score extraction.
- Extend statistics aggregation or report formatting (functions named `build_*_messages`, `recompute_aggregates`, `_build_quarter_section`).
- Improve notification filtering, storage handling, and broadcast flow.
- Add tests under `tests/` for any new behavior.

## How to run
- Install runtime dependencies: `pip install -r requirements.txt`
- Install test dependencies before running tests: `pip install -r requirements-dev.txt`
- Run the bot: `python main.py`
- Run tests: `pytest tests/`

## Notes for AI agents
- The codebase is large (`main.py` is sizable). A future refactor into modules (`config`, `messages`, `shiki_api`, `stats`, `handlers`, `storage`) is planned but should happen only after the current feature work is merged and stable. `healthcheck.py` is the first extracted module and demonstrates the intended pattern: one-way dependencies, parameters instead of imports from `main.py`.
- When extracting modules, keep dependencies one-directional and pass values (like `CHECK_INTERVAL`) as parameters rather than importing from `main.py`, to avoid circular imports.
- Do not commit actual bot tokens or owner IDs.
- When changing configuration defaults, document them in both `main.py` and `README.md`.
- Prefer minimal, behavior-preserving fixes, and verify with existing tests.
- Git operations are handled manually by the maintainer — do not push via tooling.
