"""Copilot Dashboard - FastAPI backend for GitHub Copilot session analytics."""
from __future__ import annotations

import importlib.util
import sys
from collections import defaultdict
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ─── Import copilot-manager.py (hyphenated filename requires importlib) ─────────
_MANAGER_PATH = Path(__file__).resolve().parent.parent / "copilot-hud" / "local" / "copilot-manager.py"
spec = importlib.util.spec_from_file_location("copilot_manager", str(_MANAGER_PATH))
mod = importlib.util.module_from_spec(spec)  # type: ignore
sys.modules["copilot_manager"] = mod  # Required for @dataclass to resolve module
spec.loader.exec_module(mod)  # type: ignore

# Re-export what we need
load_all_sessions = mod.load_all_sessions
load_session = mod.load_session
SessionInfo = mod.SessionInfo
PRICING = mod.PRICING
COPILOT_HOME = mod.COPILOT_HOME
calc_cost_from_metrics = mod.calc_cost_from_metrics
_get_model_pricing = mod._get_model_pricing
parse_date = mod.parse_date

# ─── App setup ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Copilot Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Session cache ──────────────────────────────────────────────────────────────
_session_cache: list = []
_cache_loaded_at: Optional[datetime] = None


def _load_cache() -> None:
    global _session_cache, _cache_loaded_at
    _session_cache = load_all_sessions()
    _cache_loaded_at = datetime.now(timezone.utc)


@app.on_event("startup")
def startup():
    _load_cache()


# ─── Helpers ────────────────────────────────────────────────────────────────────

def _session_to_dict(s, short: bool = True) -> dict[str, Any]:
    """Convert SessionInfo to JSON-safe dict."""
    d: dict[str, Any] = {
        "id": s.id,
        "summary": s.summary or "",
        "repository": s.repository or "",
        "branch": s.branch or "",
        "model": s.primary_model or s.current_model or "",
        "created_at": s.created_at or "",
        "updated_at": s.updated_at or "",
        "cost": round(s.cost, 4),
        "output_tokens": s.output_tokens,
        "input_tokens": s.input_tokens,
        "cache_read_tokens": s.cache_read_tokens,
        "cache_hit_rate": round(s.cache_hit_rate * 100, 2),
        "tool_calls": s.tool_call_count,
        "subagent_count": s.subagent_count,
        "premium_requests": s.premium_requests,
        "compactions": s.compaction_count,
        "lines_added": s.lines_added,
        "lines_removed": s.lines_removed,
        "has_shutdown": s.has_shutdown,
        "has_events": s.has_events,
    }
    if not short:
        d.update({
            "cwd": s.cwd,
            "git_root": s.git_root,
            "host_type": s.host_type,
            "account": s.account,
            "cache_write_tokens": s.cache_write_tokens,
            "subagent_tokens": s.subagent_tokens,
            "assistant_message_count": s.assistant_message_count,
            "api_duration_ms": s.api_duration_ms,
            "total_tokens": s.total_tokens,
            "summary_count": s.summary_count,
            "files_modified": s.files_modified,
            "model_metrics": s.model_metrics,
            "tool_distribution": dict(s.tool_distribution) if s.tool_distribution else {},
        })
    return d


def _date_key(created_at: str) -> str:
    """Extract YYYY-MM-DD from ISO datetime string."""
    dt = parse_date(created_at)
    if dt:
        return dt.strftime("%Y-%m-%d")
    return created_at[:10] if len(created_at) >= 10 else ""


# ─── API Endpoints ──────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(str(Path(__file__).parent / "web" / "index.html"))


@app.get("/api/refresh")
def refresh():
    _load_cache()
    return {"status": "ok", "sessions": len(_session_cache), "loaded_at": _cache_loaded_at.isoformat() if _cache_loaded_at else None}


@app.get("/api/overview")
def overview():
    sessions = _session_cache
    total = len(sessions)
    with_data = sum(1 for s in sessions if s.has_events)
    total_cost = sum(s.cost for s in sessions)
    total_out = sum(s.output_tokens for s in sessions)
    total_in = sum(s.input_tokens for s in sessions)
    total_cr = sum(s.cache_read_tokens for s in sessions)
    cache_rate = (total_cr / total_in * 100) if total_in > 0 else 0.0
    total_premium = sum(s.premium_requests for s in sessions)
    total_api_ms = sum(s.api_duration_ms for s in sessions)

    most_expensive = max(sessions, key=lambda s: s.cost) if sessions else None

    return {
        "total_sessions": total,
        "sessions_with_data": with_data,
        "total_cost": round(total_cost, 2),
        "total_output_tokens": round(total_out / 1_000_000, 2),
        "total_output_tokens_raw": total_out,
        "total_input_tokens": total_in,
        "total_cache_read_tokens": total_cr,
        "cache_hit_rate": round(cache_rate, 2),
        "total_premium_requests": total_premium,
        "most_expensive_session": {
            "id": most_expensive.id,
            "summary": most_expensive.summary,
            "cost": round(most_expensive.cost, 2),
        } if most_expensive else None,
        "total_api_hours": round(total_api_ms / 3_600_000, 2),
        "cache_loaded_at": _cache_loaded_at.isoformat() if _cache_loaded_at else None,
    }


@app.get("/api/cost-trend")
def cost_trend(days: int = Query(default=9999)):
    by_date: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "sessions": 0, "all_shutdown": True})
    for s in _session_cache:
        dk = _date_key(s.created_at)
        if not dk:
            continue
        by_date[dk]["cost"] += s.cost
        by_date[dk]["sessions"] += 1
        if not s.has_shutdown:
            by_date[dk]["all_shutdown"] = False

    result = []
    for date in sorted(by_date.keys()):
        d = by_date[date]
        result.append({
            "date": date,
            "cost": round(d["cost"], 2),
            "sessions": d["sessions"],
            "has_full_data": d["all_shutdown"],
        })

    if days < 9999:
        result = result[-days:]
    return result


@app.get("/api/models")
def models():
    agg: dict[str, dict] = defaultdict(lambda: {
        "requests": 0, "output_tokens": 0, "input_tokens": 0,
        "cache_read_tokens": 0, "cost": 0.0
    })

    for s in _session_cache:
        if s.model_metrics:
            for model, metrics in s.model_metrics.items():
                usage = metrics.get("usage", {})
                inp = usage.get("inputTokens", 0)
                out = usage.get("outputTokens", 0)
                cr = usage.get("cacheReadTokens", 0)
                req_data = metrics.get("requests", {})
                req = req_data.get("count", 0) if isinstance(req_data, dict) else (req_data or 0)
                p_in, p_out, p_cr = _get_model_pricing(model)
                non_cache = max(0, inp - cr)
                cost = (non_cache * p_in + cr * p_cr + out * p_out) / 1_000_000

                a = agg[model]
                a["requests"] += req
                a["output_tokens"] += out
                a["input_tokens"] += inp
                a["cache_read_tokens"] += cr
                a["cost"] += cost
        elif s.primary_model and s.output_tokens:
            model = s.primary_model
            a = agg[model]
            a["requests"] += s.assistant_message_count or 1
            a["output_tokens"] += s.output_tokens
            a["input_tokens"] += s.input_tokens
            a["cache_read_tokens"] += s.cache_read_tokens
            _, p_out, _ = _get_model_pricing(model)
            a["cost"] += (s.output_tokens / 1_000_000) * p_out

    items = []
    total_req = total_cost = total_out = total_in = total_cr = 0
    for model in sorted(agg.keys(), key=lambda m: agg[m]["cost"], reverse=True):
        a = agg[model]
        p_in, p_out, p_cr = _get_model_pricing(model)
        chr_ = (a["cache_read_tokens"] / a["input_tokens"] * 100) if a["input_tokens"] else 0
        items.append({
            "model": model,
            "display_name": model,
            "requests": a["requests"],
            "output_tokens": a["output_tokens"],
            "input_tokens": a["input_tokens"],
            "cache_read_tokens": a["cache_read_tokens"],
            "cache_hit_rate": round(chr_, 1),
            "cost": round(a["cost"], 2),
            "input_price": p_in,
            "output_price": p_out,
            "cache_read_price": p_cr,
        })
        total_req += a["requests"]
        total_cost += a["cost"]
        total_out += a["output_tokens"]
        total_in += a["input_tokens"]
        total_cr += a["cache_read_tokens"]

    return {
        "models": items,
        "totals": {
            "total_requests": total_req,
            "total_cost": round(total_cost, 2),
            "total_output_tokens": total_out,
            "total_input_tokens": total_in,
            "total_cache_read_tokens": total_cr,
            "cache_hit_rate": round(total_cr / total_in * 100, 1) if total_in else 0,
        }
    }


@app.get("/api/sessions")
def sessions(
    limit: int = Query(default=50),
    since: Optional[str] = Query(default=None),
    repo: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    sort: str = Query(default="date"),
):
    filtered = list(_session_cache)

    if since:
        filtered = [s for s in filtered if _date_key(s.created_at) >= since]
    if repo:
        rl = repo.lower()
        filtered = [s for s in filtered if rl in (s.repository or "").lower()]
    if model:
        ml = model.lower()
        filtered = [s for s in filtered if ml in (s.primary_model or s.current_model or "").lower()]

    sort_keys = {
        "date": lambda s: s.created_at or "",
        "cost": lambda s: s.cost,
        "tokens": lambda s: s.output_tokens,
        "tools": lambda s: s.tool_call_count,
        "premium": lambda s: s.premium_requests,
        "compactions": lambda s: s.compaction_count,
    }
    key_fn = sort_keys.get(sort, sort_keys["date"])
    filtered.sort(key=key_fn, reverse=True)

    return [_session_to_dict(s) for s in filtered[:limit]]


@app.get("/api/session/{session_id}")
def session_detail(session_id: str):
    # Support short ID prefix matching
    for s in _session_cache:
        if s.id.startswith(session_id) or s.id == session_id:
            # Reload with detailed=True for full data
            session_dir = Path(COPILOT_HOME) / "session-state" / s.id
            if session_dir.exists():
                detailed = load_session(session_dir, detailed=True)
                if detailed:
                    return _session_to_dict(detailed, short=False)
            return _session_to_dict(s, short=False)
    return {"error": "Session not found"}


@app.get("/api/pricing")
def pricing():
    return {k: {"input": v[0], "output": v[1], "cache_read": v[2]} for k, v in PRICING.items()}


@app.get("/api/repos")
def repos():
    counts: dict[str, int] = defaultdict(int)
    for s in _session_cache:
        r = s.repository or "(unknown)"
        counts[r] += 1
    return [{"repo": r, "count": c} for r, c in sorted(counts.items(), key=lambda x: -x[1])]


# ─── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
