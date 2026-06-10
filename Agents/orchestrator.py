"""
Reactive Action Orchestrator — LangGraph Supervisor Pattern
=============================================================
Builds a StateGraph with:
  - Supervisor node that reads `next_agent` from shared state
  - 5 specialist agent nodes
  - Conditional edges for routing
  - HITL interrupt point before any device action

Graph flow:
  entry → support_bot (loop up to 3 turns)
        → [escalate] → telemetry_agent
                     → anomaly_agent
                     → ticket_agent
                     → [if action needed] → hitl_gate
                                          → action_agent
                     → END

Run:
  python orchestrator.py --user USR-005 --ticket TKT-9001 --complaint "No internet since this morning"
  python orchestrator.py --demo          # runs all 6 user scenarios
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Literal

# ── LangGraph import (graceful fallback) ─────────────────────────────────────
try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    print("[WARN] langgraph not installed. Install with: pip install langgraph")
    print("       Falling back to sequential runner.\n")

from state import (
    AgentState,
    AGENT_SUPPORT_BOT, AGENT_TELEMETRY, AGENT_ANOMALY,
    AGENT_TICKET, AGENT_HITL, AGENT_ACTION, AGENT_END,
    USER_DEVICE_MAP, TICKET_STORE,
)
from agents import (
    support_bot_agent, telemetry_agent, anomaly_agent,
    ticket_agent, hitl_gate, action_agent,
)


# ── Router: reads next_agent from state ──────────────────────────────────────
def route(state: AgentState) -> str:
    next_a = state.get("next_agent", AGENT_END)
    # Guard: if support bot resolved, skip everything
    if state.get("bot_resolved"):
        return AGENT_END
    return next_a


# ── Build graph ───────────────────────────────────────────────────────────────
def build_graph():
    if not LANGGRAPH_AVAILABLE:
        return None

    graph = StateGraph(AgentState)

    # Register all nodes
    graph.add_node(AGENT_SUPPORT_BOT, support_bot_agent)
    graph.add_node(AGENT_TELEMETRY,   telemetry_agent)
    graph.add_node(AGENT_ANOMALY,     anomaly_agent)
    graph.add_node(AGENT_TICKET,      ticket_agent)
    graph.add_node(AGENT_HITL,        hitl_gate)
    graph.add_node(AGENT_ACTION,      action_agent)

    # Entry point
    graph.set_entry_point(AGENT_SUPPORT_BOT)

    # All routing is driven by next_agent field in state
    all_nodes = [
        AGENT_SUPPORT_BOT,
        AGENT_TELEMETRY,
        AGENT_ANOMALY,
        AGENT_TICKET,
        AGENT_HITL,
        AGENT_ACTION,
    ]

    route_map = {n: n for n in all_nodes}
    route_map[AGENT_END] = END
    route_map["__end__"] = END

    for node in all_nodes:
        graph.add_conditional_edges(node, route, route_map)

    return graph.compile()


# ── Sequential fallback (no LangGraph) ───────────────────────────────────────
def run_sequential(state: AgentState) -> AgentState:
    """Runs the same pipeline without LangGraph, for environments where it's unavailable."""
    pipeline = [
        (AGENT_SUPPORT_BOT, support_bot_agent),
        (AGENT_TELEMETRY,   telemetry_agent),
        (AGENT_ANOMALY,     anomaly_agent),
        (AGENT_TICKET,      ticket_agent),
        (AGENT_HITL,        hitl_gate),
        (AGENT_ACTION,      action_agent),
    ]

    for agent_name, agent_fn in pipeline:
        next_a = state.get("next_agent", AGENT_END)
        if next_a == AGENT_END or next_a == "__end__":
            break
        if next_a != agent_name and agent_name != AGENT_SUPPORT_BOT:
            if state.get("next_agent") != agent_name:
                continue
        update = agent_fn(state)
        state.update(update)

    return state


# ── Initial state factory ─────────────────────────────────────────────────────
def make_initial_state(user_id: str, ticket_id: str, complaint: str) -> AgentState:
    return AgentState(
        user_id          = user_id,
        ticket_id        = ticket_id,
        complaint_text   = complaint,
        device_id        = None,
        customer_name    = None,
        customer_plan    = None,
        customer_region  = None,
        bot_messages     = [],
        bot_resolved     = False,
        bot_turns        = 0,
        telemetry        = None,
        telemetry_error  = None,
        risk_score       = None,
        anomaly_detected = None,
        anomaly_details  = None,
        fault_type       = None,
        existing_ticket  = None,
        ticket_action    = None,
        new_ticket_id    = None,
        hitl_required    = False,
        hitl_approved    = None,
        hitl_notes       = None,
        recommended_action = None,
        action_executed  = None,
        action_result    = None,
        next_agent       = AGENT_SUPPORT_BOT,
        error            = None,
        audit_log        = [],
    )


# ── Result printer ────────────────────────────────────────────────────────────
def print_result(final_state: AgentState):
    print(f"\n{'='*60}")
    print(f"  WORKFLOW RESULT")
    print(f"{'='*60}")
    print(f"  Customer      : {final_state.get('customer_name','?')} ({final_state.get('user_id','?')})")
    print(f"  Device        : {final_state.get('device_id','?')}")
    print(f"  Fault type    : {final_state.get('fault_type','?')}")
    print(f"  Risk score    : {final_state.get('risk_score','?')}/100")
    print(f"  Anomaly       : {final_state.get('anomaly_detected','?')}")
    print(f"  Action taken  : {final_state.get('recommended_action','?')}")
    print(f"  Action result : {final_state.get('action_result','?')}")
    print(f"  Ticket        : {final_state.get('new_ticket_id','?')} [{final_state.get('ticket_action','?')}]")
    print(f"  Bot turns     : {final_state.get('bot_turns',0)}")
    print(f"\n  Audit log:")
    for entry in final_state.get("audit_log", []):
        print(f"    {entry}")
    print(f"{'='*60}\n")


# ── Single run ────────────────────────────────────────────────────────────────
def run_workflow(user_id: str, ticket_id: str, complaint: str, graph=None) -> AgentState:
    print(f"\n[WORKFLOW] Starting — user={user_id} ticket={ticket_id}")
    print(f"           complaint: '{complaint}'")

    initial = make_initial_state(user_id, ticket_id, complaint)

    if graph and LANGGRAPH_AVAILABLE:
        final = graph.invoke(initial)
    else:
        final = run_sequential(dict(initial))

    return final


# ── Demo scenarios ────────────────────────────────────────────────────────────
DEMO_SCENARIOS = [
    {
        "user_id":   "USR-001",
        "ticket_id": "TKT-9001",
        "complaint": "No internet since this morning, tried rebooting already",
    },
    {
        "user_id":   "USR-002",
        "ticket_id": "TKT-9002",
        "complaint": "Router is very slow, pages loading very slowly",
    },
    {
        "user_id":   "USR-003",
        "ticket_id": "TKT-9003",
        "complaint": "Internet keeps dropping every few minutes",
    },
    {
        "user_id":   "USR-005",
        "ticket_id": "TKT-9005",
        "complaint": "No internet at all, WAN light is red",
    },
    {
        "user_id":   "USR-006",
        "ticket_id": "TKT-9006",
        "complaint": "Router restarting by itself multiple times today",
    },
]


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reactive Action Orchestrator")
    parser.add_argument("--user",      default="",    help="user_id (e.g. USR-005)")
    parser.add_argument("--ticket",    default="",    help="CRM ticket ID")
    parser.add_argument("--complaint", default="",    help="Customer complaint text")
    parser.add_argument("--demo",      action="store_true", help="Run all demo scenarios")
    parser.add_argument("--output",    default="",    help="Save results to JSON file")
    args = parser.parse_args()

    graph = build_graph()
    if LANGGRAPH_AVAILABLE and graph:
        print("[OK] LangGraph graph compiled successfully")
    else:
        print("[WARN] Running in sequential fallback mode")

    results = []

    if args.demo:
        print(f"\nRunning {len(DEMO_SCENARIOS)} demo scenarios ...\n")
        for scenario in DEMO_SCENARIOS:
            final = run_workflow(
                scenario["user_id"],
                scenario["ticket_id"],
                scenario["complaint"],
                graph,
            )
            print_result(final)
            results.append({
                "user_id":    final.get("user_id"),
                "device_id":  final.get("device_id"),
                "fault_type": final.get("fault_type"),
                "risk_score": final.get("risk_score"),
                "action":     final.get("recommended_action"),
                "result":     final.get("action_result"),
                "ticket":     final.get("new_ticket_id"),
            })

    elif args.user:
        final = run_workflow(
            args.user,
            args.ticket or f"TKT-{int(datetime.now().timestamp())}",
            args.complaint or "I have an internet problem",
            graph,
        )
        print_result(final)
        results.append(final)

    else:
        # Default: run one scenario interactively
        print("No arguments provided. Running default scenario (USR-005).")
        final = run_workflow(
            "USR-005", "TKT-9999",
            "No internet at all since this morning, WAN light is off",
            graph,
        )
        print_result(final)
        results.append(final)

    # Save results
    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"Results saved to {args.output}")

    print(f"\nTickets created this session: {len(TICKET_STORE)}")
    for t in TICKET_STORE:
        print(f"  {t['id']} | {t['priority']} | {t['title'][:60]}")
