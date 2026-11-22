# Campus Navigation App: backend

A FastAPI server for the VIT Bhopal campus navigation app. It provides:

- **Indoor routing** between rooms, teachers' cabins and toilets, inside one building or across two, drawn as SVG floor maps.
- **A chatbot** (LangGraph + Gemini) that answers questions about teachers and gives directions with map links. It falls back to a local answerer when Gemini is unavailable.
- **Events**: public listing and registration, plus a logged-in cpanel for organisers.
- **Teacher directory** search, including fuzzy name matching and voice input.

The Next.js frontend in `../frontend` calls this API at `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`).

## Layout

```
backend/
├── pyproject.toml, uv.lock    dependencies and ruff/pyright/pytest settings
├── Dockerfile
├── docs/                      notes on each dependency (read before changing how it's used)
├── .claude/notes/             task notes, conventions, backlog and handoff
└── server/                    the app (run everything from here)
    ├── app.py                 FastAPI app: middleware, routing, teacher and chat endpoints
    ├── .env                   configuration (see below)
    ├── Utils/                 routing engine, SVG drawing, caches, logging, CLI tools
    ├── Chatbot/               LangGraph chatbot, its tools, transcription, /upload processor
    ├── Events_Management/     users, JWT auth, events and registrations
    ├── DB/                    SQLite: map DBs (ab01-01.db, lab.db), teachers, users, events
    ├── Floor_Maps/            source floor SVGs per building
    ├── Assets/                images served at /Assets (e.g. the map pin)
    ├── Teacher_Data/          faculty.xlsx (source for the teacher DB)
    └── Test/                  pytest suite
```

## Setup

You need Python 3.10 and [uv](https://docs.astral.sh/uv/). From `backend/`:

```
uv sync
```

This installs the runtime dependencies, the dev tools (pytest, ruff, pyright), and the spaCy `en_core_web_sm` model, which is pinned in `pyproject.toml`. The NLTK data (`punkt_tab`, `stopwords`) downloads itself on first import.

### Configuration (`server/.env`)

Relative paths are resolved from `server/` (`Utils/loader.py`).

| Variable | Used for |
|---|---|
| `DB_PATH` | Folder with the building map DBs (`ab01-01.db`, `lab.db`) |
| `DB_TEACHER_DATA` | Teacher directory DB (used by the chatbot and teacher search) |
| `DB_AUTH_PATH` | Users DB |
| `DB_EVENT` | Events, registrations and login history DB |
| `FLOOR_MAPS_DIR` | Source floor SVGs |
| `ASSETS_DIR` | Static images served at `/Assets` |
| `AUDIO_DIR` | Where `/upload` saves audio files |
| `EXCEL_TEACHER_PATH` | `faculty.xlsx`, for rebuilding the teacher DB |
| `SECRET_KEY` | **Required in production.** Signs login tokens (HS256). If it's missing, a random key is generated at startup with a warning, so every restart logs everyone out. Generate one with `uv run --no-sync python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `GEMINI_API_KEY` | Gemini for the chatbot and voice transcription. Without it the chatbot uses its local fallback. |
| `GEMINI_MODEL` | Optional; default `gemini-3.5-flash-lite` |
| `CHAT_CACHE_TTL`, `CHAT_CACHE_MAX_SIZE` | Optional chatbot answer cache: TTL in seconds (default 3600) and number of entries (default 256) |
| `LOG_LEVEL` | Optional: `DEBUG`, `INFO` (default), `WARNING` or `ERROR`. `DEBUG` logs every timed stage and every cache hit and miss. |
| `LOG_FILE` | Optional, e.g. `logs/backend.log`: a rotating log file (5 × 1 MB) in addition to the console |

## Running

From `backend/server`:

```
uv run --no-sync uvicorn app:app --reload --port 8000
```

or `uv run --no-sync python app.py`, which does the same. The interactive API docs are at http://localhost:8000/docs.

Each request logs one line with its total time, stage timings and cache hits. The same breakdown is sent in a `Server-Timing` header, so it shows in the browser's devtools (Network → Timing).

### Docker

From `backend/`:

```
docker build -t campus-nav-backend .
docker run -p 8000:8000 --env-file server/.env campus-nav-backend
```

The image runs one uvicorn process on port 8000 as a non-root user.

## API overview

| Area | Endpoints | Login |
|---|---|---|
| Maps | `GET /load_svg?floor=&building=` (a plain floor map), `GET /load_shortest_path_svg?floor=&building=&route=` (a drawn route) | no |
| Routing | `POST /process_path`, `POST /multi_building_process_path`, `POST /custom_process` (teacher cabin → cabin) | no |
| Teachers | `GET /teachers`, `GET /search_teacher` | no |
| Teachers (admin) | `POST /teachers` | yes |
| Chatbot | `POST /chat`, `POST /chat/audio`, `POST /upload` (older text/voice route finder) | no |
| Chatbot (admin) | `POST /reload_knowledge` | yes |
| Users | `POST /user/register`, `POST /user/login`, `GET/PUT/DELETE /user/profile`, `PUT /user/password`, `GET /user/login-history`, `GET /user/me` | yes, except register and login |
| Events (public) | `GET /events/`, `GET /events/{id}`, `POST /events/{id}/register`, `POST /events/{id}/cancel-registration`, `GET /events/user-registrations?email=` | no |
| Events (cpanel) | `/user/events` (list, create, get, update, delete, attendees, remove attendee) | yes |

- **Login:** `POST /user/login` returns `access_token`, valid for 30 minutes. Send it as `Authorization: Bearer <token>`.
- **Events are shared** between all cpanel admins. Only the event's creator can remove an attendee.
- **Route maps are drawn in memory**, not written to disk:
  - A route response either *is* the SVG (one floor) or contains map URLs with a `route=<32-hex id>`.
  - Those maps stay available for 10 minutes, within a 64 MB store (`Utils/route_maps.py`).
  - The chat endpoint also returns `files.legs`, one map per floor in walking order, including the exit and entrance floors of a multi-building route.
- **Errors** are `{"detail": "..."}`. A bad input (unknown room, unknown building, same start and end) returns 400 with a message that explains it.

A recorded call trace for every endpoint, showing what each request calls and passes, is in the request flow report (`.claude/notes/tasks/2026-10-10-request-flow-report.md` has the link and how it was made).

## Performance and caching

The routing engine caches, keyed by each file's version (modified time, size and the SQLite change counter). A cache refreshes on its own when its DB or SVG file changes, so it never needs clearing by hand.

- Frozen floor graphs per building and floor, with a coordinate index for node lookups.
- A read-only snapshot of each map DB's labels.
- The parsed source floor SVGs. Each request deep-copies one before drawing.
- The teacher list; the chatbot reloads its knowledge when the teacher DB changes.

Routing and drawing run in the threadpool, so a slow route doesn't block other requests. The details and benchmark numbers are in `.claude/notes/tasks/2026-10-10-bottlenecks-and-caching.md`.

## Tests and linting

From `backend/server`:

```
uv run --no-sync pytest -v
uv run --no-sync ruff check <changed files>
uv run --no-sync ruff format <changed files>
uv run --no-sync pyright <changed files>
```

- **Tests never touch the real data.** `Test/conftest.py` points every writable setting (DBs, audio folder) at temporary copies before the app is imported.
- **The chatbot tests need no network,** because Gemini is replaced by a scripted fake (`Test/chat_fakes.py`).
- **Lint rule:** no `noqa`, `type: ignore` or per-file ignores; fix the code instead.
- **`Test/test_api.py`** is a manual script for a running server and is out of date (see the backlog).

## Maintenance tools

Run from `backend/server`. Read-only unless noted.

| Command | What it does |
|---|---|
| `uv run --no-sync python -m Utils.route_audit [--preference none\|Stairs\|Lift\|all] [--render]` | Routes every pair of places and teachers and reports any failure (should report 0 problems) |
| `uv run --no-sync python -m Utils.profile_routes --repeat 20 --micro --profile [--concurrent 10] [--out file.json]` | Benchmarks and profiles the main scenarios; `--compare BEFORE.json AFTER.json` diffs two runs |
| `uv run --no-sync python -m Utils.fix_connector_labels [--apply]` | Checks the AB-01 lift and stairs labels; **`--apply` writes the DB** (back it up first) |
| `uv run --no-sync python -m Utils.align_connectors [--building AB-01] [--reference 2]` | Checks that the floors line up, using room numbers they share |
| `uv run --no-sync python -m Utils.excel_to_db` | **Rebuilds the teacher DB** from `faculty.xlsx` |

## Known gaps

The low-priority items are in `.claude/notes/backlog.md`:

- 40 teachers have the room "Null" and one has "PAT-OFFICE", so none of them can be routed to until their real rooms are known.
- `Test/test_api.py` needs updating or removing.
