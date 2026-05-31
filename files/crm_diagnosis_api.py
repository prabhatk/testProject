"""
CPE CRM Diagnosis API
======================
Single FastAPI microservice.  No separate server needed.
This runs alongside your existing stack (same VM / K8s pod is fine).

Architecture answer:
  - user_id → device_id : looked up from a simple mapping table
                          (in prod: query your CRM/BSS database)
  - device telemetry    : queried from Gold Delta table (or CSV for POC)
  - ML diagnosis        : loaded from trained .pkl model
  - Result              : JSON with risk_score, 5 diagnosis confidences,
                          recommended action, AI summary

Run:
  pip install fastapi uvicorn pandas scikit-learn
  uvicorn crm_diagnosis_api:app --reload --port 8000

Test:
  curl "http://localhost:8000/diagnose?user_id=USR-001"
  curl "http://localhost:8000/health"
"""

import json
import pickle
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ── FastAPI (graceful import) ──────────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    print("[WARN] FastAPI not installed. Run: pip install fastapi uvicorn")

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
MODEL_DIR  = BASE_DIR / "ml_model"
DATA_PATH  = BASE_DIR / "synthetic_data" / "cpe_telemetry.csv"

FEATURES = [
    "cpu_pct", "mem_pct", "thermal_c", "storage_pct",
    "wan_drops", "error_count",
    "cpu_spike_flag", "mem_leak_flag", "thermal_warn", "wan_warn",
    "cpu_rolling_mean", "mem_rolling_mean",
    "thermal_rolling_max", "mem_rolling_delta",
]

# ── user_id → device_id mapping (replace with CRM DB query in prod) ───────────
# Format: { user_id: { device_id, customer_name, plan, region } }
USER_DEVICE_MAP = {
    "USR-001": {"device_id": "CPE-NOR-0000", "name": "Amit Shah",      "plan": "Fibre 300", "region": "Mumbai-North"},
    "USR-002": {"device_id": "CPE-CPU-0000", "name": "Priya Menon",     "plan": "Fibre 100", "region": "Pune-West"},
    "USR-003": {"device_id": "CPE-MEM-0000", "name": "Rajesh Kumar",    "plan": "Fibre 500", "region": "Indore-North"},
    "USR-004": {"device_id": "CPE-THE-0000", "name": "Sneha Reddy",     "plan": "Fibre 100", "region": "Hyderabad-Central"},
    "USR-005": {"device_id": "CPE-WAN-0000", "name": "Arjun Patel",     "plan": "Fibre 200", "region": "Ahmedabad-East"},
    "USR-006": {"device_id": "CPE-CRA-0000", "name": "Kavitha Nair",    "plan": "Fibre 300", "region": "Kochi-South"},
    "USR-007": {"device_id": "CPE-NOR-0001", "name": "Suresh Iyer",     "plan": "Fibre 100", "region": "Chennai-West"},
    "USR-008": {"device_id": "CPE-CPU-0001", "name": "Deepa Sharma",    "plan": "Fibre 500", "region": "Delhi-South"},
}

# ── Load model artifacts ────────────────────────────────────────────────────────
def load_model():
    model_path  = MODEL_DIR / "cpe_isolation_forest.pkl"
    scaler_path = MODEL_DIR / "feature_scaler.pkl"
    thr_path    = MODEL_DIR / "thresholds.json"

    if not model_path.exists():
        print("[WARN] Model not found. Run ml_model/train_isolation_forest.py first.")
        return None, None, {"watch_max": 44.3, "proactive_min": 44.3, "reactive_min": 80.9}

    with open(model_path,  "rb") as f: model  = pickle.load(f)
    with open(scaler_path, "rb") as f: scaler = pickle.load(f)
    with open(thr_path)           as f: thr   = json.load(f)
    print(f"[OK] Model loaded from {model_path}")
    return model, scaler, thr

# ── Load telemetry data (Gold table equivalent for POC) ───────────────────────
def load_telemetry():
    if not DATA_PATH.exists():
        print("[WARN] Data not found. Run synthetic_data/generate_cpe_data.py first.")
        return pd.DataFrame()
    df = pd.read_csv(DATA_PATH)
    # Add rolling features
    df = df.sort_values(["device_id", "timestamp"])
    grp = df.groupby("device_id")
    df["cpu_rolling_mean"]    = grp["cpu_pct"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["mem_rolling_mean"]    = grp["mem_pct"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["thermal_rolling_max"] = grp["thermal_c"].transform(lambda x: x.rolling(5, min_periods=1).max())
    df["mem_rolling_delta"]   = grp["mem_pct"].transform(lambda x: x.diff(5).fillna(0))
    print(f"[OK] Telemetry loaded: {len(df):,} rows, {df['device_id'].nunique()} devices")
    return df

# ── Diagnosis logic ────────────────────────────────────────────────────────────
def score_device(device_id: str, df, model, scaler, thresholds):
    """Return ML scores and telemetry snapshot for a single device."""
    device_df = df[df["device_id"] == device_id].copy()
    if device_df.empty:
        return None

    # Last 30 rows (~30 min of data at 1 event/min)
    recent = device_df.tail(30)
    X = recent[FEATURES].fillna(0).values

    if model is not None:
        X_scaled   = scaler.transform(X)
        raw_scores = model.score_samples(X_scaled)
        # Normalise using training distribution bounds (approximate)
        s_min, s_max = -0.6, 0.1
        risk_scores  = 100 * (1 - (raw_scores - s_min) / (s_max - s_min + 1e-9))
        risk_scores  = np.clip(risk_scores, 0, 100)
        risk_score   = float(round(np.mean(risk_scores), 1))
    else:
        # Rule-based fallback when model not available
        last = recent.iloc[-1]
        risk_score = (
            (last["cpu_pct"] / 100) * 30 +
            (last["mem_pct"] / 100) * 30 +
            (last["thermal_c"] / 120) * 20 +
            (min(last["wan_drops"], 20) / 20) * 20
        )
        risk_score = float(round(risk_score * 100, 1))

    last_row   = recent.iloc[-1]
    telemetry  = {
        "cpu_pct":       round(float(recent["cpu_pct"].mean()), 1),
        "mem_pct":       round(float(recent["mem_pct"].mean()), 1),
        "thermal_c":     round(float(recent["thermal_c"].max()),  1),
        "storage_pct":   round(float(last_row["storage_pct"]),    1),
        "wan_drops_hr":  int(recent["wan_drops"].sum()),
        "error_count":   int(recent["error_count"].sum()),
        "cpu_spike_flag":bool(int(last_row["cpu_spike_flag"])),
        "mem_leak_flag": bool(int(last_row["mem_leak_flag"])),
        "thermal_warn":  bool(int(last_row["thermal_warn"])),
        "fault_type":    str(last_row.get("fault_type", "unknown")),
    }

    # Derive 5 diagnosis confidence scores from telemetry signals
    wan_conf      = min(100, telemetry["wan_drops_hr"] * 5 + (30 if risk_score > 50 else 0))
    degraded_conf = min(100, int(telemetry["cpu_spike_flag"]) * 35 +
                              int(telemetry["mem_leak_flag"])  * 25 +
                              max(0, risk_score - 40))
    mem_conf      = min(100, int(telemetry["mem_leak_flag"]) * 50 +
                              max(0, telemetry["mem_pct"] - 70) * 2)
    physical_conf = min(100, int(telemetry["thermal_warn"]) * 40 +
                              max(0, telemetry["thermal_c"] - 75) * 2)
    normal_conf   = max(0, 100 - max(wan_conf, degraded_conf, mem_conf, physical_conf))

    # action label
    thr_pro  = thresholds.get("proactive_min", 44.3)
    thr_reac = thresholds.get("reactive_min",  80.9)
    if risk_score >= thr_reac:
        action = "REACTIVE_REBOOT"
    elif risk_score >= thr_pro:
        action = "PROACTIVE_REBOOT"
    else:
        action = "WATCH"

    return {
        "risk_score":   risk_score,
        "action":       action,
        "telemetry":    telemetry,
        "diagnosis": {
            "wan_connectivity": round(float(wan_conf), 1),
            "device_degraded":  round(float(degraded_conf), 1),
            "memory_spike":     round(float(mem_conf), 1),
            "all_normal":       round(float(normal_conf), 1),
            "physical_fault":   round(float(physical_conf), 1),
        },
    }

def build_ai_summary(device_info, result):
    """Build a plain-English summary for the support agent."""
    t   = result["telemetry"]
    d   = result["diagnosis"]
    rs  = result["risk_score"]
    act = result["action"]

    top_diagnosis = max(d, key=d.get)
    diagnosis_text = {
        "wan_connectivity": "WAN connectivity issue detected — interface dropping frequently",
        "device_degraded":  "device shows degraded performance — CPU and memory under sustained pressure",
        "memory_spike":     "memory leak pattern detected — process not releasing memory over time",
        "all_normal":       "device telemetry is within normal range — issue may be external (line, DNS, app)",
        "physical_fault":   "high thermal readings suggest possible hardware issue or blocked ventilation",
    }.get(top_diagnosis, "unknown issue pattern")

    action_text = {
        "REACTIVE_REBOOT":  "Recommend: trigger immediate remote reboot via TR-369 and alert NOC.",
        "PROACTIVE_REBOOT": "Recommend: schedule a soft reboot during the next low-traffic window (02:00–04:00 AM).",
        "WATCH":            "Recommend: continue monitoring. No immediate action required.",
    }.get(act, "")

    return (
        f"Device {device_info['device_id']} — risk score {rs}/100. "
        f"CPU avg {t['cpu_pct']}%, memory {t['mem_pct']}%, "
        f"thermal peak {t['thermal_c']}°C, {t['wan_drops_hr']} WAN drops in last 30 min. "
        f"Most likely: {diagnosis_text}. {action_text}"
    )

# ── Boot ────────────────────────────────────────────────────────────────────────
model, scaler, thresholds = load_model()
telemetry_df              = load_telemetry()

# ── FastAPI app ─────────────────────────────────────────────────────────────────
if FASTAPI_AVAILABLE:
    app = FastAPI(
        title="CPE CRM Diagnosis API",
        description="Diagnose CPE device issues from a CRM complaint ticket",
        version="1.0.0",
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

    @app.get("/health")
    def health():
        return {
            "status":    "ok",
            "model":     "loaded" if model else "missing — rule-based fallback active",
            "devices":   int(telemetry_df["device_id"].nunique()) if not telemetry_df.empty else 0,
            "timestamp": datetime.utcnow().isoformat(),
        }

    @app.get("/diagnose")
    def diagnose(user_id: str, ticket_id: Optional[str] = None):
        """
        Main endpoint. Takes user_id from CRM ticket.
        Returns device health, ML diagnosis scores, and recommended action.
        """
        if user_id not in USER_DEVICE_MAP:
            raise HTTPException(status_code=404, detail=f"user_id '{user_id}' not found in device mapping")

        device_info = USER_DEVICE_MAP[user_id]
        device_id   = device_info["device_id"]

        result = score_device(device_id, telemetry_df, model, scaler, thresholds)
        if result is None:
            raise HTTPException(status_code=404, detail=f"No telemetry found for device_id '{device_id}'")

        ai_summary = build_ai_summary(device_info, result)

        return {
            "ticket_id":   ticket_id or "N/A",
            "user_id":     user_id,
            "customer":    device_info,
            "risk_score":  result["risk_score"],
            "action":      result["action"],
            "diagnosis":   result["diagnosis"],
            "telemetry":   result["telemetry"],
            "ai_summary":  ai_summary,
            "thresholds":  thresholds,
            "generated_at": datetime.utcnow().isoformat(),
        }

    @app.get("/devices")
    def list_devices():
        """List all known user→device mappings (for POC testing)."""
        return {"mappings": USER_DEVICE_MAP}

# ── CLI test (no FastAPI needed) ──────────────────────────────────────────────
if __name__ == "__main__":
    print("\n=== CRM Diagnosis API — CLI test ===\n")
    test_users = ["USR-001", "USR-002", "USR-003", "USR-004", "USR-005", "USR-006"]
    for uid in test_users:
        info   = USER_DEVICE_MAP.get(uid, {})
        result = score_device(info.get("device_id",""), telemetry_df, model, scaler, thresholds)
        if result:
            summary = build_ai_summary(info, result)
            print(f"[{uid}] {info.get('name','?'):<18} device={info['device_id']}")
            print(f"       risk={result['risk_score']:>5.1f}  action={result['action']}")
            print(f"       top diagnosis: {max(result['diagnosis'], key=result['diagnosis'].get)}")
            print(f"       {summary}")
            print()

    if FASTAPI_AVAILABLE:
        print("\nTo start the API server:")
        print("  uvicorn crm_diagnosis_api:app --reload --port 8000")
        print("  curl 'http://localhost:8000/diagnose?user_id=USR-001'")
