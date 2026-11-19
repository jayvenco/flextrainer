# Architecture

One container, one process (uvicorn), one SQLite file. No external services are required; ChatGPT, GitHub update
checks and web scraping are optional and only make outbound requests when used.

```
browser ── HTML pages (Jinja2) ── vanilla JS ──> /ui/* JSON (cookie session, same-origin only)
                                         │
FastAPI ── routers/pages.py  (login, pages, manifest, service worker)
        ── routers/ui.py     (exams, import, practice, settings, backups)
        ── services/*        (extract, parser, scraper, gpt, exams, backup, store, updates)
        ── SQLAlchemy ──> /app/data/app.db (WAL)   /app/data/backup/*.zip   /app/data/logs/app.log
```

## Data model
`User`, `Setting` (key/value; the ChatGPT key is Fernet-encrypted with the secret in `/app/data/.secret_key`),
`Exam` → `Source`, `Question`; `PracticeSession` → `Attempt`.

- `Question.correct_json` is the key from the source; `ai_correct_json`/`ai_answer` come from ChatGPT. The effective key
  is the source key, falling back to ChatGPT. `verify_status`: `unchecked`, `confirmed`, `disputed`, `answered`, `error`.
- A session stores a snapshot of question ids (`order_json`). Progress is derived from its `Attempt` rows, so a session
  can be resumed at any time and deleting a question never breaks it.
- Progress per exam uses the latest graded attempt per question (mastered / to review) and overall accuracy.

## Import pipeline
file or URL → `extract`/`scraper` (plain text) → `parser.parse_questions` (numbered questions, lettered options,
answer lines, trailing answer key, checkboxes, Dutch and English labels) → optional `gpt.extract_questions` when the
parser finds nothing or AI-assisted import is ticked → de-duplicate by fingerprint → `Question` rows.

## ChatGPT check
`gpt.verify_question` asks for JSON (letters, explanation, optional reference URL). Unsafe URLs are dropped. Bulk checks
run in a background thread pool (3 workers) with an in-memory progress dict polled by the exam page. During practice a
question without any key is checked synchronously; otherwise the check runs after the instant right/wrong feedback.

## Security
Bcrypt passwords, signed session cookie (HttpOnly, SameSite=Lax, Secure behind HTTPS), forced password change,
rate-limited login and heavy endpoints, same-origin (CSRF) check on every state-changing request, CSP without inline
scripts, scraper refuses non-public addresses (also on redirects) unless `ALLOW_PRIVATE_SCRAPE=true`.

## Reverse proxy
Uvicorn runs with `--proxy-headers`. `BASE_URL=/sub` mounts the app under a sub-path; `/health` stays at the root.
