# Agent guidance for ShikiUpdatesBot

## What this repository is
- A Telegram bot implemented primarily in `main.py`.
- Tracks a Shikimori user's history and favourites, then sends notifications to Telegram subscribers.
- Collects statistics (genres, studios, scores, demographics, etc.) and sends the owner an automatic report at the start of each quarter.
- Exposes `/stats` (button menu: current quarter / all-time) and `/favs` (favourite anime & manga) to all subscribers.
- Uses `aiogram` for Telegram and `aiohttp` for HTTP requests (both client, for Shikimori, and server, for healthcheck).
- Contains tests in `tests/` and configuration at the top of `main.py`.

## Important files
- `main.py` — main entrypoint and implementation: config, message bank, parsers, Shikimori API calls, statistics, command handlers, polling loop.
- `healthcheck.py` — isolated HTTP healthcheck server with a heartbeat watchdog. Imports nothing from `main.py`; the dependency is one-way (`main.py` calls `heartbeat()` and `start_health_server()`).
- `README.md` — setup (three deploy tiers), commands, statistics, healthcheck, and test instructions.
- `requirements.txt` / `requirements-dev.txt` — runtime and development dependencies.
- `tests/` — pytest-based coverage for storage, notification logic, parser logic, statistics, and Telegram send behavior.

## Key patterns and conventions
- Environment variables: `BOT_TOKEN`, `OWNER_ID` are required at runtime. Optional `DATA_DIR` (default `/data`) controls persistent file location; optional `PORT` (default `8080`) controls the healthcheck server port.
- The bot stores state in JSON files under `DATA_DIR`: `seen_ids.json`, `subscribers.json`, `seen_favourites.json`, `stats_all.json`, `stats_current.json`, and snapshots in `quarters/`.
- All file writes go through `_atomic_write()` (temp file + `os.replace()`) for crash safety.
- All Telegram messages use `ParseMode.HTML`; user-facing strings from the API are escaped via `h()` (`html.escape`).
- **Stability is the top priority.** Every function must be exception-safe: unexpected or missing data must never crash the bot. Network fetches return `None` on any error (not empty collections) to distinguish API failures from genuinely empty results. Statistics degrade gracefully — a failed export or GraphQL call yields a report without enriched metadata rather than a crash.
- Statistics data sources: user lists come from the public `list_export` JSON endpoints (no auth); title metadata comes from the GraphQL `animes`/`mangas` batch queries with `censored: false`. Do NOT reintroduce per-title REST calls or OAuth — these were evaluated and rejected.
- A single relevance filter `is_relevant(media, kind)` governs BOTH notifications and statistics. OVA/ONA are kept; specials/clips/PV are dropped. Do not duplicate or diverge this logic.
- The bot's lifecycle is managed by `asyncio`: `polling_loop` runs as a background task created in `main()`, alongside `dp.start_polling` and the healthcheck server.
- Use `pytest tests/` to validate changes; `tests/conftest.py` provides default env vars (incl. a temp `DATA_DIR`) for test execution.
- Preserve existing behaviour for Shikimori event filtering, favourite notifications, and statistics aggregation when modifying logic.

## Gotchas discovered the hard way (read before touching stats/links)
- **GraphQL vs REST URL formats differ.** GraphQL Shikimori returns FULL urls (`https://shikimori.io/animes/123`), while REST history returns RELATIVE (`/animes/123`). All link-building code prepends `SHIKI_BASE_URL`, so a full url would produce a double-domain broken link. `_rel_url()` normalizes any url to relative form — it is applied at the source (`fetch_meta_batch`) and defensively at every render point. When adding new link rendering, run urls through `_rel_url()`.
- **Translations are baked into stored records.** `origin` and `rating` are translated via `_ORIGIN_RU`/`_RATING_RU` at fetch time and saved into `titles`. Existing records keep their old value when a dict is edited — fixes apply only to new records (or after wiping the test bot's data). This was deliberately not refactored to "store-raw-translate-on-display" (deemed over-engineering for a rarely-changing dict).
- **`/stats` and the quarterly report share `_build_quarter_section`.** Editing it changes both at once — convenient, but verify both.
- **`/stats all` and the quarterly report are built by DIFFERENT code** (`build_stats_all_messages` works from pre-computed aggregates; the quarterly section aggregates a title list on the fly). Shared look comes from common formatters (`_top_block`, `_fmt_mono_rows`, `_section_header`, `_score_dist_block`), not shared builders.
- **Link previews are disabled selectively.** `/favs` passes `disable_preview=True` (its first link is always the same favourite); `/stats`, `/status`, and notifications keep previews (a card for the relevant title is desirable).

## Testing notes (important — two real prod bugs slipped through)
- A `test_stats.py` is planned. It MUST include **smoke tests**: every report builder (`build_stats_all_messages`, `build_current_stats_messages`, `build_favourites_messages`, and the `_stats_report_*` async builders) should be called and asserted to return `list[str]`; rendered links should contain the domain exactly once (no double-domain).
- Rationale: two production bugs would have been caught instantly by such tests — (1) `build_stats_all_messages` going undefined after a manual merge clobbered its header, and (2) double-domain broken links from GraphQL full urls. Cheap tests, whole class of bugs eliminated.

## Typical developer tasks
- Update message templates or event classification in `main.py` (the message bank near the top).
- Fix parser edge cases for Shikimori descriptions and score extraction.
- Extend statistics aggregation or report formatting (functions named `build_*_messages`, `recompute_aggregates`, `_build_quarter_section`, and the `_top_block`/`_fmt_*` formatters).
- Add a new report type to the `/stats` menu: append one entry to `_STATS_MENU` (callback key, label, async builder, row) — keyboard and dispatch update automatically.
- Improve notification filtering, storage handling, and broadcast flow.
- Add tests under `tests/` for any new behavior.

## How to run
- Install runtime dependencies: `pip install -r requirements.txt`
- Install test dependencies before running tests: `pip install -r requirements-dev.txt`
- Run the bot: `python main.py`
- Run tests: `pytest tests/`

## Notes for AI agents
- The codebase is large (`main.py` is several thousand lines). A future refactor into modules (`config`, `messages`, `shiki_api`, `stats`, `handlers`, `storage`, `utils`) is planned but should happen ONLY after the current feature work is merged and stable — and only once `test_stats.py` exists to catch regressions during the split. `healthcheck.py` is the first extracted module and demonstrates the intended pattern: one-way dependencies, parameters instead of imports from `main.py`.
- When extracting modules, keep dependencies one-directional and pass values (like `CHECK_INTERVAL`) as parameters rather than importing from `main.py`, to avoid circular imports. A planned `utils.py` will hold shared helpers (`_utcnow`, `_safe_int`, `_safe_float`, `quarter_*`) with a matching `test_utils.py`.
- Do not commit actual bot tokens or owner IDs.
- When changing configuration defaults, document them in both `main.py` and `README.md`.
- Prefer minimal, behavior-preserving fixes, and verify with existing tests.
- Git operations are handled manually by the maintainer — do not push via tooling.
