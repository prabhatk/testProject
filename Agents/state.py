"""
Shared State & Config
======================
Single TypedDict that flows through every node in the LangGraph.
Every agent reads from and writes to this state.
The supervisor routes based on `next_agent` field.
"""

from typing import TypedDict, Optional, List, Literal, Any
from dataclasses import dataclass, field


# ── Workflow shared state ─────────────────────────────────────────────────────
class AgentState(TypedDict):
    # ── Input (set at entry) ──────────────────────────────────────────────────
    user_id:          str               # from CRM ticket
    ticket_id:        str               # CRM ticket reference
    complaint_text:   str               # what the customer said

    # ── Identity resolution (Agent 2) ────────────────────────────────────────
    device_id:        Optional[str]     # resolved from user_id
    customer_name:    Optional[str]
    customer_plan:    Optional[str]
    customer_region:  Optional[str]

    # ── Support bot conversation (Agent 1) ───────────────────────────────────
    bot_messages:     List[dict]        # full conversation history
    bot_resolved:     bool              # True = resolved, no escalation needed
    bot_turns:        int               # number of bot turns used

    # ── Telemetry (Agent 2) ──────────────────────────────────────────────────
    telemetry:        Optional[dict]    # snapshot from Gold table
    telemetry_error:  Optional[str]     # if Gold table fetch failed

    # ── Anomaly detection (Agent 3) ──────────────────────────────────────────
    risk_score:       Optional[float]   # 0–100
    anomaly_detected: Optional[bool]
    anomaly_details:  Optional[dict]    # per-metric breakdown
    fault_type:       Optional[str]     # top predicted fault

    # ── Ticket management (Agent 4) ──────────────────────────────────────────
    existing_ticket:  Optional[dict]    # existing incident if found
    ticket_action:    Optional[str]     # "created" | "escalated" | "linked"
    new_ticket_id:    Optional[str]

    # ── HITL gate ─────────────────────────────────────────────────────────────
    hitl_required:    bool              # True = pause for human approval
    hitl_approved:    Optional[bool]    # set by human operator
    hitl_notes:       Optional[str]     # operator notes

    # ── Action executor (Agent 5) ────────────────────────────────────────────
    recommended_action: Optional[str]  # REBOOT | WAN_RESTART | ESCALATE_L2 | DISPATCH_TECH | MONITOR
    action_executed:    Optional[bool]
    action_result:      Optional[str]

    # ── Routing ───────────────────────────────────────────────────────────────
    next_agent:       Optional[str]     # supervisor sets this
    error:            Optional[str]     # any unhandled error

    # ── Audit trail ──────────────────────────────────────────────────────────
    audit_log:        List[str]         # timestamped log entries


# ── User → Device mapping (replace with DB query in production) ───────────────
USER_DEVICE_MAP = {
    "USR-001": {"device_id": "CPE-NOR-0000", "name": "Amit Shah",    "plan": "Fibre 300", "region": "Mumbai-North"},
    "USR-002": {"device_id": "CPE-CPU-0000", "name": "Priya Menon",  "plan": "Fibre 100", "region": "Pune-West"},
    "USR-003": {"device_id": "CPE-MEM-0000", "name": "Rajesh Kumar", "plan": "Fibre 500", "region": "Indore-North"},
    "USR-004": {"device_id": "CPE-THE-0000", "name": "Sneha Reddy",  "plan": "Fibre 100", "region": "Hyderabad-Central"},
    "USR-005": {"device_id": "CPE-WAN-0000", "name": "Arjun Patel",  "plan": "Fibre 200", "region": "Ahmedabad-East"},
    "USR-006": {"device_id": "CPE-CRA-0000", "name": "Kavitha Nair", "plan": "Fibre 300", "region": "Kochi-South"},
    "USR-007": {"device_id": "CPE-NOR-0001", "name": "Suresh Iyer",  "plan": "Fibre 100", "region": "Chennai-West"},
    "USR-008": {"device_id": "CPE-CPU-0001", "name": "Deepa Sharma", "plan": "Fibre 500", "region": "Delhi-South"},
}

# ── In-memory ticket store (replace with real CRM API in production) ──────────
TICKET_STORE: List[dict] = []

# ── Decision thresholds ───────────────────────────────────────────────────────
THRESHOLDS = {
    "watch_max":      40.0,
    "proactive_min":  40.0,
    "reactive_min":   65.0,
}

# ── Routing constants ─────────────────────────────────────────────────────────
AGENT_SUPPORT_BOT   = "support_bot"
AGENT_TELEMETRY     = "telemetry_agent"
AGENT_ANOMALY       = "anomaly_agent"
AGENT_TICKET        = "ticket_agent"
AGENT_HITL          = "hitl_gate"
AGENT_ACTION        = "action_agent"
AGENT_END           = "__end__"
