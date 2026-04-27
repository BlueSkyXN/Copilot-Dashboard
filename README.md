# Copilot Dashboard

Web analytics dashboard for GitHub Copilot CLI session data. Visualizes token usage, costs, model distribution, and session history from `~/.copilot/session-state/`.

## Prerequisites

- Python 3.9+

## Quick Start

```bash
pip3 install -r requirements.txt
uvicorn app:app --port 8765
```

Open http://localhost:8765

## Features

- **Summary cards**: Total sessions, estimated cost, output tokens, cache hit rate
- **Cost trend**: Daily cost chart (30d / 90d / All selectable)
- **Model distribution**: Doughnut chart + breakdown table with totals
- **Session list**: Filterable by repository, model, date range; sortable by cost/tokens
- **Session detail**: Per-session token breakdown, tool distribution, model switches, SQLite enrichment (turn counts, files modified, refs)
- **Pricing transparency**: `/api/pricing` exposes all rates (Copilot Enterprise internal)

## Architecture

```
app.py              FastAPI backend — imports parsing/pricing from lib/
lib/session_parser.py  Self-contained session parser and pricing engine
web/index.html      Single-page dashboard — Alpine.js + Chart.js + Tailwind (CDN)
```

Fully independent — no external repo dependencies. All session parsing and Copilot Enterprise pricing logic lives in `lib/session_parser.py`.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /api/overview` | Summary statistics |
| `GET /api/cost-trend?days=30` | Daily cost trend (omit `days` for all-time) |
| `GET /api/models` | Model distribution with totals |
| `GET /api/sessions?limit=50&repo=&model=&since=YYYY-MM-DD&sort=cost` | Session list |
| `GET /api/session/{id}` | Full session detail (supports 8+ char ID prefix) |
| `GET /api/repos` | Repository list with session counts |
| `GET /api/pricing` | Current pricing table |
| `POST /api/refresh` | Reload session cache from disk |

## Development

```bash
# Watch mode (auto-reload on code changes)
uvicorn app:app --port 8765 --reload
```

Sessions are cached in memory at startup (~10s for 300+ sessions). Use `POST /api/refresh` or restart to pick up new sessions.
