# Agent guidance for ShikiUpdatesBot

## Required project context

- Before starting repository work, read this file and `ARCHITECTURE.md` completely. Do not reconstruct current architecture or domain contracts from an old chat.
- `AGENTS.md` defines how maintainers and agents work. `ARCHITECTURE.md` defines how the bot is built and why its internal contracts exist. Keep that boundary when either document grows.

## Working agreements

### Roles and technical authority

- The maintainer defines product goals, desired behaviour, and priorities, and retains final authority over repository changes and merges. Codex is the primary author and technical lead for the Python codebase: within the approved scope, it makes implementation and architectural decisions by default without offloading Python details to the maintainer.
- Do not assume the maintainer must resolve Python implementation details. Inspect the code and existing decisions directly. Ask only when a product choice, ambiguous intent, missing authority, or external coordination genuinely requires input.
- If a request or proposed solution would harm correctness, stability, maintainability, security, or the bot as a whole, say so explicitly before implementation, explain the concrete risk, and recommend a safer alternative. The maintainer may accept or override a technical choice after reviewing the tradeoffs, while Codex remains responsible for the quality of what it authors and must not silently ship a knowingly harmful solution.
- Reject complexity that has no comparable practical benefit. Prefer the smallest robust design that preserves existing behaviour.

### Language and code comments

- Write Python and build-script comments, module docstrings, and function/class docstrings in Russian. Keep identifiers, protocol names, library/API names, command-line flags, and standard license headers in their canonical form. GitHub issues, commit/PR metadata, and unavoidable third-party configuration fields remain in English where the workflow requires it.
- Write Telegram replies and user-guide README prose in reader-facing language. Describe outcomes in plain Russian; do not expose internal identifiers such as `OWNER_ID`, transport/storage terms such as `private chat ID`, or FSM/callback/middleware terminology when a normal user does not need them. Exact names remain appropriate in configuration instructions, the technical reference, code, tests, and logs.
- Format every `from ... import ...` with multiple names as a parenthesized vertical list with one name per line and trailing commas; keep single-name imports on one line.

### Canon and durable knowledge

- Written canon beats conversational memory. Record important discoveries and decisions when they are made:
  - agreed implementation work → a GitHub issue;
  - collaboration, verification, and delivery process → `AGENTS.md`;
  - architecture, module ownership, runtime/domain contracts, and stability decisions → `ARCHITECTURE.md`;
  - raw, deferred, or rejected ideas (with the reason) → `ideas.md`.
- Do not keep duplicate sources of truth. Code and tests establish current behaviour; `README.md` owns user-facing behaviour, configuration, and deployment; `AGENTS.md` owns working agreements; `ARCHITECTURE.md` owns internal design and contracts; GitHub issues own accepted active work; `ideas.md` is the pre-issue inbox and decision parking lot.
- Codex may update `AGENTS.md` and `ARCHITECTURE.md` whenever needed without asking for permission, but must report material documentation changes in its handoff.
- Both files are committed and synchronized through public GitHub. Never put secrets, tokens, private identifiers, machine-specific paths, or temporary session details in them.
- Keep `AGENTS.md` focused on how work is performed. New bot knowledge belongs in `ARCHITECTURE.md`; do not repeatedly compress architectural context to make it fit beside process instructions.

### Tests and verification

- A behaviour change or bug fix ships with its tests. A regression test must fail against the broken/unpatched behaviour and pass with the fix.
- Test placement mirrors production ownership: `test_<module>.py` for focused modules and `test_handlers_<flow>.py` for handler orchestration. Each symbol's input→output matrix has one authoritative test home.
- Tests of module X verify X's logic and its handling of dependency contracts, including `None`, empty, and exceptional outcomes. They do not duplicate the dependency's own input→output matrix.
- Run cheap pure helpers for real. Mock I/O boundaries such as Telegram, HTTP, storage/filesystem access, and clocks. Full aiogram surfaces may use `unittest.mock`; narrow contracts may use small hand-written stubs.
- Patch dependencies where callers look them up (`handlers.<name>`, `stats.<name>`, etc.); for example, `monkeypatch.setattr("handlers.fetch_favourites", …)`. String-form `monkeypatch.setattr("module.name", …)` avoids local-variable shadowing.
- Coverage is diagnostic, not a target. Do not pin or chase a percentage; prefer focused contracts and smoke tests over exhaustive presentation snapshots.
- Before a PR, run `pytest tests/`, `ruff check .`, and `git diff --check` when the environment provides the required tools. If verification cannot run, report that limitation explicitly.

### Live Shikimori contract verification

- Shikimori's public source repositories are archival evidence, not authority for the current site. When documentation is unclear or archived code may differ from production, verify the disputed point with the smallest bounded set of read-only requests to the live API.
- Always send an explicit non-browser `User-Agent` that identifies the application, as required by Shikimori. A bot version and contact or repository URL may be added for manual verification but are not part of Shikimori's required header contract. Keep manual verification bounded and paced; production code must continue to use the central throttle and 429 retry described in `ARCHITECTURE.md`.
- The maintainer's Shikimori profile `WNR` is public and may be used for production-shaped API verification, including inspection of its public history responses. Do not invent a privacy restriction for this data.
- A live sample is evidence, not an eternal guarantee. Record stable conclusions in focused regression tests and update `ARCHITECTURE.md` when the internal contract or its rationale changes.

### Work tracking and delivery

- Once a design is agreed, create or update a GitHub issue immediately. Issues are written in English and should include appropriate labels, dependency notes such as `Blocked by: #N`, and an acceptance/test outline.
- An issue tied to a branch starts with the branch name (`branch-name: summary`). PR titles do not carry the branch prefix: use an imperative squash-commit subject and put `Fixes #N` in the PR body.
- Review feedback that arrives after merge approval and belongs to already planned work is added to the existing issue instead of being fixed opportunistically or duplicated in a new issue.
- Before a PR, audit the final diff and issue against the durable documentation: `README.md` for user-facing behaviour, configuration, and deployment; `AGENTS.md` for process; `ARCHITECTURE.md` for internal design and contracts; and `ideas.md` for deferred items that were implemented, rejected, superseded, or promoted to an issue. Update only affected sources and report material documentation changes in the handoff.
- At the end of every change, classify its SemVer impact (`none`, `patch`, `minor`, or `major`) against `project_meta.PROJECT_VERSION` on the current `main` and report the decision. A PR with `patch`, `minor`, or `major` impact updates `PROJECT_VERSION` in the same branch; documentation, tests, and behaviour-preserving internal work with `none` impact do not. If `main` advanced after the branch started, recalculate the version before merge so two PRs never claim the same code version.
- `PROJECT_VERSION` identifies the current source state, not the latest published portable build. It may advance through several merged PRs while GitHub Releases remain on an older version; those intermediate code versions do not require tags or portable releases. Every PR with user-visible behaviour that ships in the Windows EXE still includes a short, copy-ready release-note fragment written for portable users.
- Portable releases are accumulated deliberately instead of being published for every successful PR. Their manually curated notes consolidate only portable-relevant fragments since the previous published tag: include cross-platform behaviour present in the EXE plus Windows setup, update, security, compatibility, migration, and known-limitation information; omit Docker/source/CI-only changes and behaviour-preserving internals. Use the sections `Что изменилось`, `Как обновиться`, and `Известные ограничения`, followed by one `Все изменения` compare link. GitHub-generated notes are not required.
- Push a release tag only from the final merged `main` commit, and require it to exactly match the current `PROJECT_VERSION`. The tag triggers the verified Windows build, informational VirusTotal scan, and a draft GitHub Release titled `ShikiUpdatesBot vX.Y.Z`. Before manual publication, inspect the ZIP and checksum and place the complete marked VirusTotal block after the human notes and compare link, immediately before GitHub's Assets section. VirusTotal detections and integration failures warn but never block the draft; publishing remains a maintainer decision.
- Codex should offer a consistent commit subject/body and, when useful, an English PR title and body. Git-mutating operations remain with the maintainer unless explicitly requested; read-only inspection (`status`, `diff`, `log`, `show`) is allowed.
- Routine Dependabot version updates run monthly for the root Python manifests, Dockerfile, and GitHub Actions. Group compatible minor/patch updates within one ecosystem, keep major updates isolated, and cap open version-update PRs per ecosystem. Every dependency PR must pass the normal CI, maintainer review, and its own SemVer classification; never auto-merge it. Python base-image minor upgrades remain explicit project work rather than automated Docker updates.
- On Windows inside Codex, sandboxed `gh` commands may be unable to read the GitHub CLI credential stored in Windows Credential Manager. An `invalid token`, missing-token, or anonymous-rate-limit result from the sandbox is not proof that authorization is broken. Before suggesting `gh auth logout` / `gh auth login`, repeat the smallest necessary read-only `gh auth status` or `gh api` check in the approved user context; never print the token or use that context as a blanket sandbox bypass.
- Start every distinct development stage in a fresh Codex task; a new branch always means a new task. Durable context must come from repository canon rather than an old chat.

### Repository hygiene

- When changing configuration defaults, document them in both `config.py` and `README.md`.
- Line endings are CRLF. The scoped `.gitattributes` rule forces `packaging/windows/*.cmd` to CRLF in every checkout; preserve CRLF elsewhere to avoid whole-file diffs.
- Never commit actual bot tokens or owner IDs.

### Standard commands

- Install runtime dependencies: `pip install -r requirements.txt`.
- Install test dependencies: `pip install -r requirements-dev.txt`.
- Run the bot after configuring `.env`: `python main.py`.
- Build the Windows exe: `pip install -r requirements-build.txt`, then `pyinstaller --clean --noconfirm ShikiUpdatesBot.spec`.
- Smoke the exe with `dist\ShikiUpdatesBot.exe --version` and `dist\ShikiUpdatesBot.exe --check-config`.
- Run tests with `pytest tests/`; lint with `ruff check .` (`ruff.toml` enforces E4/E7/E9/F/I, including E402 and import sorting).
