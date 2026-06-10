"""
Specialist Agents
==================
Five agents, each a pure function: (state) -> partial_state_update.
No agent calls another agent directly — all routing goes through the supervisor.

Agent 1  — support_bot      : RAG-based troubleshooter (Claude Sonnet)
Agent 2  — telemetry_agent  : fetch device data from Gold table / CSV fallback
Agent 3  — anomaly_agent    : run Isolation Forest + rule engine, return risk_score
Agent 4  — ticket_agent     : check / create / escalate CRM tickets
Agent 5  — action_agent     : execute TR-369 reboot, WAN restart, dispatch, etc.
"""

import json
import os
import pickle
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from state import (
    AgentState, USER_DEVICE_MAP, TICKET_STORE, THRESHOLDS,
    AGENT_TELEMETRY, AGENT_ANOMALY, AGENT_TICKET, AGENT_ACTION, AGENT_END,
)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
MODEL_DIR  = BASE_DIR / "ml_model"
DATA_PATH  = BASE_DIR / "synthetic_data" / "cpe_telemetry.csv"

# ── Load model once at module import ─────────────────────────────────────────
def _load_model():
    mp = MODEL_DIR / "cpe_isolation_forest.pkl"
    sp = MODEL_DIR / "feature_scaler.pkl"
    tp = MODEL_DIR / "thresholds.json"
    if mp.exists() and sp.exists():
        with open(mp, "rb") as f: model = pickle.load(f)
        with open(sp, "rb") as f: scaler = pickle.load(f)
        thr = json.loads(tp.read_text()) if tp.exists() else THRESHOLDS
        return model, scaler, thr
    return None, None, THRESHOLDS

_MODEL, _SCALER, _THRESHOLDS = _load_model()

FEATURES = [
    "cpu_pct", "mem_pct", "thermal_c", "storage_pct",
    "wan_drops", "error_count",
    "cpu_spike_flag", "mem_leak_flag", "thermal_warn", "wan_warn",
    "cpu_rolling_mean", "mem_rolling_mean",
    "thermal_rolling_max", "mem_rolling_delta",
]

# ── Telemetry cache (load once from CSV) ─────────────────────────────────────
_TELEMETRY_DF: pd.DataFrame = pd.DataFrame()

def _get_telemetry_df() -> pd.DataFrame:
    global _TELEMETRY_DF
    if _TELEMETRY_DF.empty and DATA_PATH.exists():
        df = pd.read_csv(DATA_PATH)
        df = df.sort_values(["device_id", "timestamp"])
        grp = df.groupby("device_id")
        df["cpu_rolling_mean"]    = grp["cpu_pct"].transform(lambda x: x.rolling(5, min_periods=1).mean())
        df["mem_rolling_mean"]    = grp["mem_pct"].transform(lambda x: x.rolling(5, min_periods=1).mean())
        df["thermal_rolling_max"] = grp["thermal_c"].transform(lambda x: x.rolling(5, min_periods=1).max())
        df["mem_rolling_delta"]   = grp["mem_pct"].transform(lambda x: x.diff(5).fillna(0))
        _TELEMETRY_DF = df
    return _TELEMETRY_DF

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

# ─────────────────────────────────────────────────────────────────────────────
# AGENT 1 — Support bot (RAG-based, Claude Sonnet via Anthropic API)
# ─────────────────────────────────────────────────────────────────────────────
SUPPORT_KB = """
You are a first-line CPE device support assistant for an ISP.
Help customers troubleshoot their home internet / router problems.

KNOWLEDGE BASE:
- No internet: ask customer to check WAN LED, try rebooting the router (unplug 30s), check cables
- Slow internet: ask to run speed test, check Wi-Fi channel, reboot router
- Wi-Fi not visible: check router is powered, 2.4GHz vs 5GHz, factory reset last resort
- Router rebooting itself: may indicate firmware issue or hardware fault, escalate after 2 attempts
- All devices affected: likely router or ISP issue, escalate if reboot doesn't help
- One device affected: likely client device issue, not router

ESCALATION RULE:
If the customer issue cannot be resolved in 3 turns OR involves:
- Repeated reboots not helping
- Device-level hardware concerns
- Ongoing outage for more than 2 hours
...then respond with the EXACT text: "ESCALATE_TO_AGENT"

Keep responses concise and friendly. Max 3 sentences per reply.
"""

def support_bot_agent(state: AgentState) -> dict:
    """
    Agent 1: Try to resolve via guided troubleshooting.
    Uses Claude Sonnet API. Falls back to rule-based if API key not set.
    Escalates to supervisor after max 3 turns or when unresolvable.
    """
    log = state.get("audit_log", [])
    turns = state.get("bot_turns", 0)
    messages = state.get("bot_messages", [])
    complaint = state.get("complaint_text", "")

    log.append(f"[{_ts()}] support_bot: turn {turns+1}, complaint='{complaint[:60]}'")

    # Add customer message to history
    if not messages:
        messages = [{"role": "user", "content": complaint}]

    # ── Try Claude Sonnet API ─────────────────────────────────────────────────
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    bot_reply = None

    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                system=SUPPORT_KB,
                messages=messages,
            )
            bot_reply = response.content[0].text
        except Exception as e:
            log.append(f"[{_ts()}] support_bot: API error — {e}, using rule fallback")

    # ── Rule-based fallback (no API key needed for POC demo) ─────────────────
    if bot_reply is None:
        complaint_lower = complaint.lower()
        if turns == 0:
            if "no internet" in complaint_lower or "not working" in complaint_lower:
                bot_reply = "I'm sorry to hear that. Please check if the WAN LED on your router is lit. If it's off or blinking red, try unplugging the router for 30 seconds and plugging it back in."
            elif "slow" in complaint_lower or "speed" in complaint_lower:
                bot_reply = "Let's check your connection speed. Please run a speed test at fast.com and share the result. Also, are all devices affected or just one?"
            elif "wifi" in complaint_lower or "wi-fi" in complaint_lower:
                bot_reply = "Please check if the Wi-Fi LED on your router is on. Try connecting on 2.4GHz band if you usually use 5GHz. How long has this been happening?"
            else:
                bot_reply = "I understand you're having trouble. Can you describe exactly what you're seeing — any LED lights blinking, error messages, or specific devices affected?"
        elif turns == 1:
            bot_reply = "Thank you for that information. Let's try a router reboot — please hold the reset button for 10 seconds. If the issue continues after 2 minutes, I'll escalate this to our technical team."
        else:
            bot_reply = "ESCALATE_TO_AGENT"

    # ── Check for escalation signal ───────────────────────────────────────────
    if "ESCALATE_TO_AGENT" in bot_reply or turns >= 2:
        log.append(f"[{_ts()}] support_bot: escalating to specialist agents after {turns+1} turns")
        messages.append({"role": "assistant", "content": bot_reply})
        return {
            "bot_messages":  messages,
            "bot_resolved":  False,
            "bot_turns":     turns + 1,
            "next_agent":    AGENT_TELEMETRY,
            "audit_log":     log,
        }

    # ── Not resolved yet, continue bot conversation ───────────────────────────
    messages.append({"role": "assistant", "content": bot_reply})
    log.append(f"[{_ts()}] support_bot: replied, awaiting customer response")

    # For demo/POC: auto-simulate customer saying "still not working" after turn 1
    if turns >= 1:
        messages.append({"role": "user", "content": "Still not working, same problem."})

    return {
        "bot_messages":  messages,
        "bot_resolved":  False,
        "bot_turns":     turns + 1,
        "next_agent":    "support_bot",   # loop back for next turn
        "audit_log":     log,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 2 — Telemetry agent (Gold table via CSV for POC / Databricks for prod)
# ─────────────────────────────────────────────────────────────────────────────
def telemetry_agent(state: AgentState) -> dict:
    """
    Agent 2: Resolve user_id → device_id, fetch last 30-min telemetry from Gold table.
    POC: reads from CSV (synthetic data).
    Production: replace _fetch_from_gold() with Databricks SQL endpoint call.
    """
    log = state.get("audit_log", [])
    user_id = state.get("user_id", "")

    log.append(f"[{_ts()}] telemetry_agent: resolving user_id={user_id}")

    # ── Step 1: user_id → device_id ──────────────────────────────────────────
    mapping = USER_DEVICE_MAP.get(user_id)
    if not mapping:
        log.append(f"[{_ts()}] telemetry_agent: ERROR — user_id not found")
        return {
            "telemetry_error": f"user_id '{user_id}' not found in device mapping",
            "next_agent":      AGENT_END,
            "audit_log":       log,
        }

    device_id = mapping["device_id"]
    log.append(f"[{_ts()}] telemetry_agent: resolved device_id={device_id}")

    # ── Step 2: fetch telemetry ───────────────────────────────────────────────
    telemetry, error = _fetch_from_gold(device_id)

    if error:
        log.append(f"[{_ts()}] telemetry_agent: data error — {error}")

    return {
        "device_id":       device_id,
        "customer_name":   mapping["name"],
        "customer_plan":   mapping["plan"],
        "customer_region": mapping["region"],
        "telemetry":       telemetry,
        "telemetry_error": error,
        "next_agent":      AGENT_ANOMALY,
        "audit_log":       log,
    }


def _fetch_from_gold(device_id: str):
    """
    POC: read last 30 rows for device from CSV (Gold table equivalent).

    PRODUCTION REPLACEMENT:
    ─────────────────────────────────────────────────────
    from databricks import sql
    conn = sql.connect(
        server_hostname = os.getenv("DATABRICKS_HOST"),
        http_path       = os.getenv("DATABRICKS_HTTP_PATH"),
        access_token    = os.getenv("DATABRICKS_TOKEN"),
    )
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT device_id, window_start,
               cpu_avg_1h, cpu_max_1h, mem_avg_1h, thermal_max_1h,
               storage_pct, wan_drops_1h, error_count_1h,
               anomaly_score, risk_score, action_label
        FROM   delta.`/delta/cpe_gold`
        WHERE  device_id = '{device_id}'
          AND  window_start >= CURRENT_TIMESTAMP - INTERVAL 2 HOURS
        ORDER  BY window_start DESC
        LIMIT  5
    ''')
    rows = cursor.fetchall()
    ─────────────────────────────────────────────────────
    """
    df = _get_telemetry_df()
    if df.empty:
        return None, "Telemetry CSV not found — run generate_cpe_data.py first"

    device_df = df[df["device_id"] == device_id]
    if device_df.empty:
        return None, f"No telemetry found for device_id '{device_id}'"

    recent = device_df.tail(30)
    last   = recent.iloc[-1]

    telemetry = {
        # Live snapshot (last event)
        "device_id":       device_id,
        "last_seen":       str(last.get("timestamp_iso", "unknown")),
        "fault_type":      str(last.get("fault_type", "unknown")),
        # Aggregated stats (last 30 min ~ Gold layer)
        "cpu_avg":         round(float(recent["cpu_pct"].mean()), 2),
        "cpu_max":         round(float(recent["cpu_pct"].max()),  2),
        "mem_avg":         round(float(recent["mem_pct"].mean()), 2),
        "mem_max":         round(float(recent["mem_pct"].max()),  2),
        "thermal_max":     round(float(recent["thermal_c"].max()), 2),
        "storage_pct":     round(float(last["storage_pct"]), 2),
        "wan_drops_total": int(recent["wan_drops"].sum()),
        "error_count":     int(recent["error_count"].sum()),
        # Flag summary
        "cpu_spike_flag":  bool(int(last["cpu_spike_flag"])),
        "mem_leak_flag":   bool(int(last["mem_leak_flag"])),
        "thermal_warn":    bool(int(last["thermal_warn"])),
        "wan_warn":        bool(int(last["wan_warn"])),
        # Raw features for ML
        "_features_df":    recent[FEATURES].fillna(0).values.tolist(),
    }
    return telemetry, None


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 3 — Anomaly detection agent
# ─────────────────────────────────────────────────────────────────────────────
def anomaly_agent(state: AgentState) -> dict:
    """
    Agent 3: Run Isolation Forest on device telemetry.
    Falls back to rule-based scoring if model not available.
    Returns risk_score, anomaly_detected, fault classification, recommended_action.
    """
    log = state.get("audit_log", [])
    telemetry = state.get("telemetry")
    device_id = state.get("device_id", "unknown")

    log.append(f"[{_ts()}] anomaly_agent: scoring device_id={device_id}")

    if not telemetry:
        log.append(f"[{_ts()}] anomaly_agent: no telemetry — skipping ML, using rule fallback")
        return {
            "risk_score":       50.0,
            "anomaly_detected": True,
            "fault_type":       "unknown",
            "anomaly_details":  {},
            "next_agent":       AGENT_TICKET,
            "audit_log":        log,
        }

    # ── ML scoring ───────────────────────────────────────────────────────────
    risk_score   = None
    fault_type   = telemetry.get("fault_type", "unknown")

    if _MODEL is not None and "_features_df" in telemetry:
        try:
            X = np.array(telemetry["_features_df"])
            X_scaled   = _SCALER.transform(X)
            raw_scores = _MODEL.score_samples(X_scaled)
            s_min, s_max = -0.6, 0.1
            norm_scores  = 100 * (1 - (raw_scores - s_min) / (s_max - s_min + 1e-9))
            norm_scores  = np.clip(norm_scores, 0, 100)
            risk_score   = float(round(np.mean(norm_scores), 1))
            log.append(f"[{_ts()}] anomaly_agent: ML risk_score={risk_score}")
        except Exception as e:
            log.append(f"[{_ts()}] anomaly_agent: ML error — {e}, using rule fallback")

    # ── Rule-based fallback ───────────────────────────────────────────────────
    if risk_score is None:
        t = telemetry
        rule_score = (
            min(t.get("cpu_avg",     0) / 100.0, 1.0) * 30 +
            min(t.get("mem_avg",     0) / 100.0, 1.0) * 30 +
            min(t.get("thermal_max", 0) / 120.0, 1.0) * 20 +
            min(t.get("wan_drops_total", 0) / 30.0, 1.0) * 20
        )
        risk_score = round(rule_score * 100, 1)
        log.append(f"[{_ts()}] anomaly_agent: rule risk_score={risk_score}")

    # ── Derive per-metric anomaly details ─────────────────────────────────────
    t = telemetry
    anomaly_details = {
        "cpu_anomaly":     t.get("cpu_spike_flag",  False),
        "mem_anomaly":     t.get("mem_leak_flag",   False),
        "thermal_anomaly": t.get("thermal_warn",    False),
        "wan_anomaly":     t.get("wan_warn",        False),
        "cpu_avg":         t.get("cpu_avg",         0),
        "mem_avg":         t.get("mem_avg",         0),
        "thermal_max":     t.get("thermal_max",     0),
        "wan_drops":       t.get("wan_drops_total", 0),
    }

    # ── Classify fault type from signals ─────────────────────────────────────
    if fault_type == "unknown":
        if t.get("wan_drops_total", 0) > 10:
            fault_type = "wan_flapping"
        elif t.get("mem_leak_flag"):
            fault_type = "memory_leak"
        elif t.get("cpu_spike_flag"):
            fault_type = "cpu_degraded"
        elif t.get("thermal_warn"):
            fault_type = "thermal_spike"
        elif risk_score > 80:
            fault_type = "crash_loop"
        else:
            fault_type = "normal"

    # ── Map fault type to recommended action ──────────────────────────────────
    action_map = {
        "normal":         "MONITOR",
        "cpu_degraded":   "REBOOT",
        "memory_leak":    "REBOOT",
        "thermal_spike":  "DISPATCH_TECH",
        "wan_flapping":   "WAN_RESTART",
        "crash_loop":     "REBOOT",
        "unknown":        "ESCALATE_L2",
    }
    recommended_action = action_map.get(fault_type, "ESCALATE_L2")

    thr = _THRESHOLDS
    anomaly_detected = risk_score >= thr.get("proactive_min", 40.0)

    log.append(f"[{_ts()}] anomaly_agent: fault={fault_type} action={recommended_action} anomaly={anomaly_detected}")

    return {
        "risk_score":         risk_score,
        "anomaly_detected":   anomaly_detected,
        "fault_type":         fault_type,
        "anomaly_details":    anomaly_details,
        "recommended_action": recommended_action,
        "next_agent":         AGENT_TICKET,
        "audit_log":          log,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 4 — Ticket management agent
# ─────────────────────────────────────────────────────────────────────────────
def ticket_agent(state: AgentState) -> dict:
    """
    Agent 4: Check for existing incident tickets for this device.
    If anomaly detected:
      - Existing ticket → escalate priority
      - No ticket       → create new with anomaly details
    If no anomaly:
      - Create informational ticket marked as normal
    """
    log         = state.get("audit_log", [])
    device_id   = state.get("device_id", "")
    anomaly     = state.get("anomaly_detected", False)
    risk_score  = state.get("risk_score", 0)
    fault_type  = state.get("fault_type", "unknown")
    ticket_id   = state.get("ticket_id", "")
    action      = state.get("recommended_action", "MONITOR")

    log.append(f"[{_ts()}] ticket_agent: device={device_id} anomaly={anomaly} risk={risk_score}")

    # ── Search for existing open ticket for this device ───────────────────────
    existing = _find_open_ticket(device_id)

    if not anomaly:
        # No anomaly → close gracefully with info note
        new_id = _create_ticket({
            "device_id":   device_id,
            "ticket_ref":  ticket_id,
            "type":        "INFO",
            "status":      "CLOSED",
            "priority":    "P4",
            "title":       f"Customer complaint — device within normal parameters",
            "risk_score":  risk_score,
            "fault_type":  "normal",
            "action":      "MONITOR",
            "notes":       "All telemetry within normal range. No device action required.",
        })
        log.append(f"[{_ts()}] ticket_agent: no anomaly — created info ticket {new_id}")
        return {
            "existing_ticket": existing,
            "ticket_action":   "created_info",
            "new_ticket_id":   new_id,
            "next_agent":      AGENT_END,
            "audit_log":       log,
        }

    # ── Anomaly found ─────────────────────────────────────────────────────────
    priority = _risk_to_priority(risk_score)

    if existing:
        # Escalate existing ticket
        old_priority = existing.get("priority", "P3")
        new_priority = _escalate_priority(old_priority, priority)
        existing["priority"]  = new_priority
        existing["status"]    = "ESCALATED"
        existing["risk_score"] = risk_score
        existing["notes"]     = f"Re-triggered by customer complaint. New risk_score={risk_score}. Escalated {old_priority}→{new_priority}."
        log.append(f"[{_ts()}] ticket_agent: escalated existing ticket {existing['id']} {old_priority}→{new_priority}")
        return {
            "existing_ticket": existing,
            "ticket_action":   "escalated",
            "new_ticket_id":   existing["id"],
            "hitl_required":   action in ("REBOOT", "WAN_RESTART", "DISPATCH_TECH"),
            "next_agent":      AGENT_ACTION if not (action in ("REBOOT", "WAN_RESTART", "DISPATCH_TECH")) else "hitl_gate",
            "audit_log":       log,
        }
    else:
        # Create new incident ticket
        new_id = _create_ticket({
            "device_id":    device_id,
            "ticket_ref":   ticket_id,
            "type":         "INCIDENT",
            "status":       "OPEN",
            "priority":     priority,
            "title":        f"Anomaly detected — {fault_type.replace('_',' ')} (risk={risk_score})",
            "risk_score":   risk_score,
            "fault_type":   fault_type,
            "action":       action,
            "anomaly":      state.get("anomaly_details", {}),
            "notes":        f"Auto-created by reactive action workflow. Customer ticket: {ticket_id}",
        })
        log.append(f"[{_ts()}] ticket_agent: created incident ticket {new_id} priority={priority}")
        return {
            "existing_ticket": None,
            "ticket_action":   "created",
            "new_ticket_id":   new_id,
            "hitl_required":   action in ("REBOOT", "WAN_RESTART", "DISPATCH_TECH"),
            "next_agent":      AGENT_ACTION if not (action in ("REBOOT", "WAN_RESTART", "DISPATCH_TECH")) else "hitl_gate",
            "audit_log":       log,
        }


def _find_open_ticket(device_id: str):
    for t in TICKET_STORE:
        if t.get("device_id") == device_id and t.get("status") in ("OPEN", "ESCALATED"):
            return t
    return None


def _create_ticket(data: dict) -> str:
    ticket_id = f"INC-{len(TICKET_STORE)+1001:04d}"
    data["id"]         = ticket_id
    data["created_at"] = datetime.now().isoformat()
    TICKET_STORE.append(data)
    return ticket_id


def _risk_to_priority(score: float) -> str:
    if score >= 80: return "P1"
    if score >= 65: return "P2"
    if score >= 40: return "P3"
    return "P4"


def _escalate_priority(current: str, new: str) -> str:
    order = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
    return current if order.get(current, 4) <= order.get(new, 4) else new


# ─────────────────────────────────────────────────────────────────────────────
# HITL gate — pause and wait for human approval
# ─────────────────────────────────────────────────────────────────────────────
def hitl_gate(state: AgentState) -> dict:
    """
    HITL: In production this pauses the graph (LangGraph interrupt).
    For POC: auto-approves after printing a clear prompt to the operator.
    In production: replace with webhook, Slack approval, or NOC dashboard action.
    """
    log    = state.get("audit_log", [])
    action = state.get("recommended_action", "MONITOR")
    device = state.get("device_id", "unknown")
    score  = state.get("risk_score", 0)

    print(f"\n{'='*55}")
    print(f"  ⚠  HITL APPROVAL REQUIRED")
    print(f"{'='*55}")
    print(f"  Device    : {device}")
    print(f"  Customer  : {state.get('customer_name','?')} ({state.get('user_id','?')})")
    print(f"  Risk score: {score}/100")
    print(f"  Fault     : {state.get('fault_type','?')}")
    print(f"  Action    : {action}")
    print(f"  Ticket    : {state.get('new_ticket_id','?')}")
    print(f"{'='*55}")

    # POC: auto-approve. In production: pause graph and wait for operator input.
    approved = True
    notes    = "Auto-approved by POC workflow (replace with real approval in production)"

    try:
        answer = input("  Approve action? [Y/n] (auto-Y in 5s): ").strip().lower()
        if answer == "n":
            approved = False
            notes    = "Operator declined action"
    except (EOFError, KeyboardInterrupt):
        pass  # non-interactive — auto-approve

    log.append(f"[{_ts()}] hitl_gate: action={action} approved={approved}")

    return {
        "hitl_approved": approved,
        "hitl_notes":    notes,
        "next_agent":    AGENT_ACTION if approved else AGENT_END,
        "audit_log":     log,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 5 — Action executor
# ─────────────────────────────────────────────────────────────────────────────
def action_agent(state: AgentState) -> dict:
    """
    Agent 5: Execute the decided action against the CPE device.

    REBOOT       → TR-369 USP Operate: Device.Reboot()
    WAN_RESTART  → TR-369 set Device.IP.Interface.1.Enable = true
    DISPATCH_TECH→ create field visit work order
    ESCALATE_L2  → flag to L2 support queue
    MONITOR      → no action, mark as watched

    POC: simulates all actions with print + mock response.
    Production: replace _execute_* with real USP Controller API calls.
    """
    log    = state.get("audit_log", [])
    action = state.get("recommended_action", "MONITOR")
    device = state.get("device_id", "unknown")

    if not state.get("hitl_approved", True) and state.get("hitl_required", False):
        log.append(f"[{_ts()}] action_agent: HITL declined — no action taken")
        return {
            "action_executed": False,
            "action_result":   "Declined by operator",
            "next_agent":      AGENT_END,
            "audit_log":       log,
        }

    log.append(f"[{_ts()}] action_agent: executing action={action} on device={device}")

    dispatch = {
        "REBOOT":        _execute_reboot,
        "WAN_RESTART":   _execute_wan_restart,
        "DISPATCH_TECH": _execute_dispatch_tech,
        "ESCALATE_L2":   _execute_escalate_l2,
        "MONITOR":       _execute_monitor,
    }
    fn = dispatch.get(action, _execute_monitor)
    result = fn(state)

    log.append(f"[{_ts()}] action_agent: result='{result}'")

    return {
        "action_executed": True,
        "action_result":   result,
        "next_agent":      AGENT_END,
        "audit_log":       log,
    }


def _execute_reboot(state: AgentState) -> str:
    """
    PRODUCTION: POST to USP Controller → triggers TR-369 Operate message
    ──────────────────────────────────────────────────────────────────────
    import requests
    resp = requests.post(
        f"{USP_CONTROLLER_URL}/api/devices/{state['device_id']}/operate",
        json={"command": "Device.Reboot()", "send_resp": True},
        headers={"Authorization": f"Bearer {USP_API_TOKEN}"},
        timeout=10,
    )
    return f"Reboot dispatched: {resp.json()}"
    ──────────────────────────────────────────────────────────────────────
    """
    device = state.get("device_id")
    print(f"\n  [ACTION] TR-369 USP Operate → Device.Reboot() → {device}")
    time.sleep(0.3)  # simulate network call
    return f"TR-369 reboot command sent to {device}. Device will restart in ~30 seconds."


def _execute_wan_restart(state: AgentState) -> str:
    """
    PRODUCTION: TR-369 set Device.IP.Interface.1.Enable = false, then true
    """
    device = state.get("device_id")
    print(f"\n  [ACTION] TR-369 → WAN interface restart → {device}")
    time.sleep(0.3)
    return f"WAN interface restart command sent to {device}. Interface will re-negotiate in ~60 seconds."


def _execute_dispatch_tech(state: AgentState) -> str:
    """
    PRODUCTION: POST to field service management system (e.g. ServiceNow, Salesforce FSM)
    """
    region = state.get("customer_region", "unknown")
    name   = state.get("customer_name",   "unknown")
    ticket = state.get("new_ticket_id",   "unknown")
    print(f"\n  [ACTION] Dispatch technician → {name} ({region}) ref={ticket}")
    return f"Field visit work order created for {name} in {region}. Reference: {ticket}. ETA: next business day."


def _execute_escalate_l2(state: AgentState) -> str:
    ticket = state.get("new_ticket_id", "unknown")
    print(f"\n  [ACTION] Escalating ticket {ticket} to L2 support queue")
    return f"Ticket {ticket} escalated to L2 support queue. L2 team will contact customer within 2 hours."


def _execute_monitor(state: AgentState) -> str:
    device = state.get("device_id", "unknown")
    print(f"\n  [ACTION] No action — monitoring device {device}")
    return f"Device {device} is within normal parameters. Monitoring continues."
