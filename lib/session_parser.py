"""Copilot session data parser and pricing engine.

Extracted from copilot-hud/local/copilot-manager.py — contains only the data
model, parsing, pricing, and loading logic. Terminal rendering and CLI code
are intentionally excluded.

Priority data source: session.shutdown event in events.jsonl (contains
complete modelMetrics, codeChanges, premium requests). Falls back to
line-by-line assistant.message parsing when shutdown event is absent.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ─── Configuration ───────────────────────────────────────────────────────────────
COPILOT_HOME = os.path.expanduser("~/.copilot")

# ─── Pricing Table (aligned with src/pricing.ts, Copilot Enterprise rates) ───────
# Format: model_id -> (input $/M, output $/M, cacheRead $/M)
# cache_write is simplified to input price (already factored into input rate).
# Formula: cost = (inputTokens - cacheReadTokens) * input
#               + cacheReadTokens * cacheRead
#               + outputTokens * output
PRICING: dict[str, tuple[float, float, float]] = {
    "claude-opus-4.6":   (6.25,  25.00, 0.50),
    "claude-opus-4.7":   (6.25,  25.00, 0.50),
    "claude-opus-4.5":   (6.25,  25.00, 0.50),
    "claude-sonnet-4.6": (3.75,  15.00, 0.30),
    "claude-sonnet-4.5": (3.75,  15.00, 0.30),
    "claude-sonnet-4":   (3.75,  15.00, 0.30),
    "claude-haiku-4.5":  (1.25,   5.00, 0.10),
    "gpt-5.5":           (5.00,  30.00, 0.50),
    "gpt-5.4":           (2.50,  15.00, 0.25),
    "gpt-5.4-mini":      (0.75,   4.50, 0.075),
    "gpt-5.3-codex":     (1.75,  14.00, 0.175),
    "gpt-5.2-codex":     (1.75,  14.00, 0.175),
    "gpt-5.2":           (1.75,  14.00, 0.175),
    "gpt-5.1":           (1.25,  10.00, 0.125),
    "gpt-5-mini":        (0.25,   2.00, 0.025),
    "gpt-4.1":           (2.00,   8.00, 0.50),
    "gpt-4o":            (2.50,  10.00, 1.25),
    "gemini-2.5-pro":    (1.25,  10.00, 0.125),
    "gemini-3-flash":    (0.50,   3.00, 0.05),
    "gemini-3.1-pro":    (2.00,  12.00, 0.20),
    "grok-code-fast-1":  (0.20,   1.50, 0.02),
}
_DEFAULT_PRICING = (2.00, 10.00, 0.20)  # fallback for unknown models


def _get_model_pricing(model: str) -> tuple[float, float, float]:
    """Resolve pricing for a model ID with fuzzy matching (mirrors pricing.ts logic)."""
    mid = model.lower()
    if mid in PRICING:
        return PRICING[mid]
    for key, p in PRICING.items():
        if mid.startswith(key) or key in mid:
            return p
    return _DEFAULT_PRICING


def calc_cost_from_metrics(model_metrics: dict[str, dict]) -> float:
    """Calculate total cost from modelMetrics dict.

    Formula mirrors src/pricing.ts estimateCost():
        cost = (inputTokens - cacheReadTokens) × inputPrice
             + cacheReadTokens × cacheReadPrice
             + outputTokens × outputPrice
    """
    total = 0.0
    for model, metrics in model_metrics.items():
        usage = metrics.get("usage", {})
        inp = usage.get("inputTokens", 0)
        out = usage.get("outputTokens", 0)
        cr  = usage.get("cacheReadTokens", 0)
        p_in, p_out, p_cr = _get_model_pricing(model)
        non_cache = max(0, inp - cr)
        total += (non_cache * p_in + cr * p_cr + out * p_out) / 1_000_000
    return total


def calc_cost_simple(model: str, output_tokens: int) -> float:
    """Fallback cost estimate using output tokens only (when no modelMetrics available)."""
    _, p_out, _ = _get_model_pricing(model)
    return (output_tokens / 1_000_000) * p_out


# ─── Formatting Helpers ──────────────────────────────────────────────────────────

def fmt_tokens(n: int | None) -> str:
    """Format token count: 1.2B / 34.5M / 465K / 123."""
    if n is None or n == 0:
        return "-"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(cost: float | None) -> str:
    """Format cost as $X.XX."""
    if cost is None or cost == 0.0:
        return "-"
    if cost >= 100:
        return f"${cost:.0f}"
    if cost >= 10:
        return f"${cost:.1f}"
    return f"${cost:.2f}"


def fmt_duration(ms: int | None) -> str:
    """Format milliseconds to human-readable duration."""
    if not ms or ms == 0:
        return "-"
    secs = ms / 1000
    if secs < 60:
        return f"{secs:.0f}s"
    mins = secs / 60
    if mins < 60:
        return f"{mins:.1f}m"
    hours = mins / 60
    return f"{hours:.1f}h"


def parse_date(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def pct_str(num: int, denom: int) -> str:
    if denom == 0:
        return "-"
    return f"{num / denom * 100:.1f}%"


# ─── Data Model ──────────────────────────────────────────────────────────────────

@dataclass
class SessionInfo:
    """Parsed session data from workspace.yaml + events.jsonl."""
    # From workspace.yaml
    id: str = ""
    cwd: str = ""
    git_root: str = ""
    repository: str = ""
    host_type: str = ""
    branch: str = ""
    summary: str = ""
    summary_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    # From events (quick scan)
    primary_model: str = ""
    current_model: str = ""
    account: str = ""
    output_tokens: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    subagent_tokens: int = 0
    compaction_count: int = 0
    tool_call_count: int = 0
    subagent_count: int = 0
    assistant_message_count: int = 0
    premium_requests: float = 0.0
    api_duration_ms: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    files_modified: list[str] = field(default_factory=list)

    # Model metrics from shutdown event (priority data source)
    model_metrics: dict[str, dict] = field(default_factory=dict)

    # Detailed event data (populated on demand for session detail view)
    model_changes: list[dict[str, Any]] = field(default_factory=list)
    tool_distribution: Counter = field(default_factory=Counter)
    subagent_details: list[dict[str, Any]] = field(default_factory=list)
    compaction_details: list[dict[str, Any]] = field(default_factory=list)

    # Flags
    has_events: bool = False
    has_shutdown: bool = False
    events_parsed: bool = False

    @property
    def total_tokens(self) -> int:
        # When modelMetrics is present, subagent calls are already included in output_tokens.
        # Only add subagent_tokens as supplement when using fallback assistant.message data.
        if self.model_metrics:
            return self.output_tokens
        return self.output_tokens + self.subagent_tokens

    @property
    def cost(self) -> float:
        """Best-effort cost calculation. Prefer modelMetrics, fallback to output-only."""
        if self.model_metrics:
            return calc_cost_from_metrics(self.model_metrics)
        if self.primary_model and self.output_tokens:
            return calc_cost_simple(self.primary_model, self.output_tokens)
        return 0.0

    @property
    def cache_hit_rate(self) -> float:
        """Cache read / total input ratio (0.0–1.0)."""
        if self.input_tokens == 0:
            return 0.0
        return self.cache_read_tokens / self.input_tokens


# ─── YAML Parser ─────────────────────────────────────────────────────────────────

def parse_workspace_yaml(filepath: Path) -> dict[str, str]:
    """Parse simple key: value YAML without external dependencies."""
    result: dict[str, str] = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                idx = line.find(":")
                if idx > 0:
                    key = line[:idx].strip()
                    value = line[idx + 1:].strip()
                    result[key] = value
    except (OSError, UnicodeDecodeError):
        pass
    return result


# ─── Event Parsing ────────────────────────────────────────────────────────────────

def _extract_account(msg: str) -> str:
    """Extract account name from authentication message."""
    ml = msg.lower()
    if "blueskyxn" in ml:
        return "BlueSkyXN"
    if "nanxie" in ml or "nan_xie" in ml or "nan-xie" in ml:
        return "NanXie-Ruijie"
    return ""


def _apply_shutdown_data(info: SessionInfo, data: dict[str, Any]) -> None:
    """Apply data from session.shutdown event to SessionInfo.

    This is the priority/authoritative data source. Called when processing
    the last event in events.jsonl (session.shutdown is always last).
    """
    info.has_shutdown = True
    info.premium_requests = data.get("totalPremiumRequests", 0) or 0
    info.api_duration_ms = data.get("totalApiDurationMs", 0) or 0
    info.current_model = data.get("currentModel", "")

    cc = data.get("codeChanges", {})
    if cc:
        info.lines_added = cc.get("linesAdded", 0) or 0
        info.lines_removed = cc.get("linesRemoved", 0) or 0
        info.files_modified = cc.get("filesModified", []) or []

    mm = data.get("modelMetrics", {})
    if mm:
        info.model_metrics = mm
        # Aggregate token totals from modelMetrics (overrides assistant.message accumulation)
        total_out = 0
        total_inp = 0
        total_cache_r = 0
        total_cache_w = 0
        for _model, metrics in mm.items():
            usage = metrics.get("usage", {})
            total_out += usage.get("outputTokens", 0)
            total_inp += usage.get("inputTokens", 0)
            total_cache_r += usage.get("cacheReadTokens", 0)
            total_cache_w += usage.get("cacheWriteTokens", 0)
        # Override only when we got real data from modelMetrics
        if total_out > 0:
            info.output_tokens = total_out
        info.input_tokens = total_inp
        info.cache_read_tokens = total_cache_r
        info.cache_write_tokens = total_cache_w

        # Primary model = one with most requests
        info.primary_model = max(
            mm.items(),
            key=lambda x: x[1].get("requests", {}).get("count", 0),
        )[0]


def parse_events(filepath: Path, *, detailed: bool = False) -> SessionInfo:
    """Parse events.jsonl and return a populated SessionInfo.

    Args:
        filepath: Path to events.jsonl.
        detailed: If True, also collect model_changes, tool_distribution,
                  compaction_details, subagent_details for session detail view.

    IMPORTANT: session.shutdown is always the last event. It overrides output_tokens
    accumulated from assistant.message events when modelMetrics is present.
    Do not change event processing order.
    """
    info = SessionInfo(has_events=True)
    models_seen: Counter = Counter()

    tool_dist: Counter = Counter()
    model_changes: list[dict[str, Any]] = []
    compaction_details: list[dict[str, Any]] = []
    subagent_details: list[dict[str, Any]] = []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type", "")
                data = evt.get("data", {})
                ts = evt.get("timestamp", "")

                if etype == "session.shutdown":
                    # ★ Priority data source — always the last event
                    _apply_shutdown_data(info, data)

                elif etype == "session.model_change":
                    model = data.get("newModel", "")
                    if model:
                        models_seen[model] += 1
                    if detailed:
                        model_changes.append({
                            "timestamp": ts,
                            "newModel": data.get("newModel", ""),
                            "previousModel": data.get("previousModel", ""),
                            "reasoningEffort": data.get("reasoningEffort", ""),
                            "previousReasoningEffort": data.get("previousReasoningEffort", ""),
                        })

                elif etype == "session.info":
                    if data.get("infoType") == "authentication":
                        acct = _extract_account(data.get("message", ""))
                        if acct:
                            info.account = acct

                elif etype == "assistant.message":
                    info.assistant_message_count += 1
                    out_tok = data.get("outputTokens", 0)
                    if out_tok:
                        # Accumulate for fallback (overridden by shutdown modelMetrics)
                        info.output_tokens += out_tok

                elif etype == "session.compaction_complete":
                    info.compaction_count += 1
                    if detailed:
                        compaction_details.append({
                            "timestamp": ts,
                            "success": data.get("success"),
                            "preCompactionTokens": data.get("preCompactionTokens", 0),
                            "preCompactionMessagesLength": data.get("preCompactionMessagesLength", 0),
                            "checkpointNumber": data.get("checkpointNumber"),
                        })

                elif etype == "subagent.completed":
                    info.subagent_count += 1
                    # subagent.totalTokens is input+output combined and is already
                    # included in session.shutdown modelMetrics — only used as fallback.
                    info.subagent_tokens += data.get("totalTokens", 0)
                    subagent_details.append({
                        "agentName": data.get("agentName", ""),
                        "agentDisplayName": data.get("agentDisplayName", ""),
                        "model": data.get("model", ""),
                        "totalToolCalls": data.get("totalToolCalls", 0),
                        "totalTokens": data.get("totalTokens", 0),
                        "durationMs": data.get("durationMs", 0),
                    })

                elif etype == "tool.execution_start":
                    info.tool_call_count += 1
                    tool_dist[data.get("toolName", "unknown")] += 1

    except (OSError, UnicodeDecodeError):
        return info

    # Primary model fallback (when no shutdown modelMetrics)
    if not info.primary_model:
        if info.current_model:
            info.primary_model = info.current_model
        elif models_seen:
            info.primary_model = models_seen.most_common(1)[0][0]

    info.subagent_details = subagent_details
    info.events_parsed = True

    if detailed:
        info.tool_distribution = tool_dist
        info.model_changes = model_changes
        info.compaction_details = compaction_details

    return info


# ─── Session Loading ─────────────────────────────────────────────────────────────

def load_session(session_dir: Path, *, detailed: bool = False) -> SessionInfo | None:
    """Load a single session from its directory."""
    workspace_path = session_dir / "workspace.yaml"
    if not workspace_path.exists():
        return None

    ws = parse_workspace_yaml(workspace_path)
    if not ws.get("id"):
        return None

    events_path = session_dir / "events.jsonl"
    if events_path.exists():
        info = parse_events(events_path, detailed=detailed)
    else:
        info = SessionInfo()

    # Populate from workspace.yaml
    info.id = ws.get("id", "")
    info.cwd = ws.get("cwd", "")
    info.git_root = ws.get("git_root", "")
    info.repository = ws.get("repository", "")
    info.host_type = ws.get("host_type", "")
    info.branch = ws.get("branch", "")
    info.summary = ws.get("summary", "")
    info.created_at = ws.get("created_at", "")
    info.updated_at = ws.get("updated_at", "")
    try:
        info.summary_count = int(ws.get("summary_count", "0") or "0")
    except ValueError:
        info.summary_count = 0

    return info


def load_all_sessions(
    copilot_home: str = COPILOT_HOME,
    *,
    repo_filter: str | None = None,
    account_filter: str | None = None,
    since_filter: str | None = None,
) -> list[SessionInfo]:
    """Load all sessions from session-state directory, newest first."""
    session_state_dir = Path(copilot_home) / "session-state"
    if not session_state_dir.exists():
        return []

    sessions: list[SessionInfo] = []

    for d in sorted(session_state_dir.iterdir()):
        if not d.is_dir():
            continue
        try:
            info = load_session(d)
            if info is None:
                continue

            if repo_filter and repo_filter.lower() not in (info.repository or "").lower():
                continue
            if account_filter and account_filter.lower() not in (info.account or "").lower():
                continue
            if since_filter:
                created = parse_date(info.created_at)
                if created:
                    try:
                        since_dt = datetime.fromisoformat(since_filter + "T00:00:00+00:00")
                        if created < since_dt:
                            continue
                    except ValueError:
                        pass

            sessions.append(info)
        except Exception:
            continue

    return sessions


# ─── SQLite Helpers ───────────────────────────────────────────────────────────────

def get_db_connection(copilot_home: str = COPILOT_HOME) -> sqlite3.Connection | None:
    """Get read-only connection to session-store.db."""
    db_path = Path(copilot_home) / "session-store.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def get_db_session_info(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Get enrichment data from SQLite database for a given session."""
    result: dict[str, Any] = {"turns_count": 0, "checkpoints_count": 0, "files": [], "refs": []}
    try:
        result["turns_count"] = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        result["checkpoints_count"] = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        result["files"] = [
            dict(r) for r in conn.execute(
                "SELECT file_path, tool_name, turn_index FROM session_files "
                "WHERE session_id = ? ORDER BY turn_index", (session_id,)
            ).fetchall()
        ]
        result["refs"] = [
            dict(r) for r in conn.execute(
                "SELECT ref_type, ref_value, turn_index FROM session_refs "
                "WHERE session_id = ? ORDER BY turn_index", (session_id,)
            ).fetchall()
        ]
    except sqlite3.Error:
        pass
    return result
