# API

FlexTrainer has **no public API** and no API tokens by design. The endpoints below are internal, same-origin JSON
endpoints used by the web UI. They require the signed-in browser session cookie and are not a stable interface.

Operational endpoints (no authentication):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness: `{"status":"ok","version":"…"}` |
| GET | `/ready` | Readiness: database reachable (503 otherwise) |

Internal UI endpoints (prefix `/ui`, cookie session):

| Area | Endpoints |
| --- | --- |
| Appearance | `POST /ui/appearance` |
| Exams | `POST /ui/exams`, `PATCH|DELETE /ui/exams/{id}`, `POST /ui/exams/{id}/reset` |
| Import | `POST /ui/exams/{id}/upload`, `POST /ui/exams/{id}/scrape`, `DELETE /ui/sources/{id}` |
| Questions | `GET|PATCH|DELETE /ui/questions/{id}`, `POST …/verify`, `POST …/accept-ai`, `POST …/reveal` |
| ChatGPT bulk check | `POST /ui/exams/{id}/verify[?redo=true]`, `GET /ui/exams/{id}/verify/status` |
| Practice | `POST /ui/exams/{id}/sessions`, `GET /ui/sessions/{id}[/question|/summary]`, `POST /ui/sessions/{id}/answer`, `DELETE /ui/sessions/{id}` |
| Settings | `POST /ui/settings/openai[/test]`, `POST /ui/settings/backup` |
| Backups | `GET|POST /ui/backups`, `GET /ui/backups/export`, `GET|DELETE /ui/backups/{name}`, `POST /ui/backups/import` |

Errors are JSON `{"detail": "…"}`. Heavy endpoints (upload, scrape, ChatGPT calls) are rate limited to 40 requests per
minute per client.
