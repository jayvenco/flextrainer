# FlexTrainer — CLAUDE.md

Context for Claude Code. Keep this file current whenever the project changes.

## What this is
Self-hosted exam trainer. The owner uploads PDF, Word and Markdown files (and can scrape web pages) containing exam
questions, practises them with instant right/wrong feedback, keeps progress per exam (resumable sessions) and lets
ChatGPT check every question with a short explanation and a suggested reference link. One overview of all exams is the
main menu. Single user.

## Stack and layout
Python 3.12, FastAPI, SQLAlchemy, Jinja2, vanilla JS, SQLite (WAL) in `/app/data`. One container.
- `app/main.py` app, middleware (CSRF origin check, security headers), health/ready, optional `BASE_URL` mount
- `app/config.py` env config + validation (clear startup errors); `app/logging_setup.py` key=value logs to stdout and
  `/app/data/logs/app.log`
- `app/routers/pages.py` HTML + login + PWA files; `app/routers/ui.py` cookie-only JSON endpoints (no public/token API)
- `app/services/`: `extract.py` (PDF/docx/md text), `parser.py` (heuristic question parser), `scraper.py` (SSRF-safe
  fetch + HTML to text), `gpt.py` (OpenAI-compatible checks and AI-assisted import), `exams.py` (questions, sessions,
  progress, background bulk check), `backup.py`, `store.py` (settings, encrypted API key), `updates.py`
- `app/static/` css (5 themes via `[data-theme]`, AA contrast checked), js (no inline scripts: CSP `script-src 'self'`),
  bundled fonts. `unraid/` template + icon, `scripts/install-unraid.sh`, `docker/entrypoint.sh` (PUID/PGID drop)
- `docs/architecture.md`, `docs/api.md`, `scripts/screenshots.py` (Playwright, demo data only)

## Rules
- UI and all documentation are in **English** (the owner writes requirements in Dutch).
- Lightweight first: fewest dependencies, smallest possible image, low idle RAM/CPU, no build chain.
- Auth: default `admin`/`admin` with **forced password change on first login**; never weaken that.
- No public API: every endpoint is cookie-authenticated and lives in `routers/ui.py`.
- The answer key is never sent to the browser before a question is answered.
- ChatGPT links are *suggestions*; the UI says so. Never log the API key.
- 5 themes (2 light, 3 dark), 5 bundled fonts, no external CDNs.
- Container port 8000, suggested host port 8822, data `/mnt/user/appdata/flextrainer/data`.
  Image `ghcr.io/jayvenco/flextrainer`. Single release flow: `main` -> `:latest`.
- Update README, CHANGELOG, `docs/api.md` and screenshots with every user-visible change. Conventional commits,
  semantic version tags.

## Commands
- Run: `docker compose up --build` (http://localhost:8822) or `./.venv/bin/uvicorn app.main:app --reload --port 8000`
- Tests: `./.venv/bin/python -m pytest -q`; lint: `./.venv/bin/ruff check app tests`
- Screenshots: `python scripts/screenshots.py` (needs Playwright and a running demo server, see the script header)
