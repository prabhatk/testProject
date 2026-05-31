"""
CPE Device Synthetic Data Generator
====================================
Generates realistic telemetry for 6 device behaviour patterns:
  0 - normal          : healthy device, baseline noise
  1 - cpu_degraded    : sustained high CPU (runaway process / heavy traffic)
  2 - memory_leak     : slowly climbing memory, never released
  3 - thermal_spike   : temperature surge (fan failure / poor ventilation)
  4 - wan_flapping    : WAN interface drops intermittently
  5 - crash_loop      : device near failure, erratic across all metrics

Each row = one telemetry event from one device at one timestamp.
Output: CSV  -> synthetic_data/cpe_telemetry.csv
        JSON -> synthetic_data/cpe_telemetry.jsonl  (Kafka-ready, one message per line)
"""

import json
import math
import random
import csv
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

# ── Config ──────────────────────────────────────────────────────────────────
NUM_DEVICES       = 20          # total simulated devices
DAYS              = 7           # how many days of history to generate
INTERVAL_SECONDS  = 60          # one telemetry event per minute per device
OUTPUT_DIR        = Path(__file__).parent

DEVICE_PROFILES = {
    "normal":       {"count": 8,  "label": 0},
    "cpu_degraded": {"count": 3,  "label": 1},
    "memory_leak":  {"count": 3,  "label": 2},
    "thermal_spike":{"count": 2,  "label": 3},
    "wan_flapping": {"count": 2,  "label": 4},
    "crash_loop":   {"count": 2,  "label": 5},
}

# ── Helpers ──────────────────────────────────────────────────────────────────
def clamp(val, lo=0.0, hi=100.0):
    return max(lo, min(hi, val))

def noise(scale=1.0):
    return random.gauss(0, scale)

def make_device_id(profile, idx):
    prefix = profile[:3].upper()
    return f"CPE-{prefix}-{idx:04d}"

# ── Per-pattern telemetry generators ────────────────────────────────────────

def gen_normal(t, state):
    """Healthy device: low stable metrics with small random noise."""
    return {
        "cpu_pct":      clamp(25 + noise(4)),
        "mem_pct":      clamp(35 + noise(3)),
        "thermal_c":    clamp(48 + noise(3), 0, 120),
        "storage_pct":  clamp(state.get("storage", 40) + random.uniform(0, 0.005)),
        "wan_drops":    0 if random.random() > 0.02 else 1,
        "error_count":  random.choices([0, 1], weights=[95, 5])[0],
    }

def gen_cpu_degraded(t, state):
    """CPU climbs and stays elevated — simulates a runaway process."""
    elapsed_hrs = state["elapsed_hrs"]
    # CPU ramps up over first 2 hours then saturates
    cpu_base = clamp(25 + min(elapsed_hrs * 20, 60) + noise(5))
    return {
        "cpu_pct":      cpu_base,
        "mem_pct":      clamp(45 + noise(4)),
        "thermal_c":    clamp(52 + (cpu_base - 25) * 0.3 + noise(2), 0, 120),
        "storage_pct":  clamp(state.get("storage", 42) + random.uniform(0, 0.003)),
        "wan_drops":    0,
        "error_count":  random.choices([0, 1, 2], weights=[70, 20, 10])[0],
    }

def gen_memory_leak(t, state):
    """Memory climbs monotonically — never freed, classic leak pattern."""
    elapsed_hrs = state["elapsed_hrs"]
    # ~1% per hour leak rate
    mem_base = clamp(30 + elapsed_hrs * 1.0 + noise(2))
    return {
        "cpu_pct":      clamp(30 + (mem_base - 30) * 0.2 + noise(4)),
        "mem_pct":      mem_base,
        "thermal_c":    clamp(50 + noise(2), 0, 120),
        "storage_pct":  clamp(state.get("storage", 40) + random.uniform(0, 0.004)),
        "wan_drops":    0,
        "error_count":  random.choices([0, 1], weights=[85, 15])[0],
    }

def gen_thermal_spike(t, state):
    """Temperature surges in waves — fan fault or blocked vent."""
    # Spike every ~4 hours, lasts ~30 min
    minute_of_day = (t.hour * 60 + t.minute) % 240
    spike = 1.0 if 100 < minute_of_day < 130 else 0.0
    temp_base = 50 + spike * 35 + noise(3)
    return {
        "cpu_pct":      clamp(30 + spike * 15 + noise(5)),
        "mem_pct":      clamp(38 + noise(3)),
        "thermal_c":    clamp(temp_base, 0, 120),
        "storage_pct":  clamp(state.get("storage", 41) + random.uniform(0, 0.003)),
        "wan_drops":    0,
        "error_count":  random.choices([0, 1, 3], weights=[60, 30, 10])[0],
    }

def gen_wan_flapping(t, state):
    """WAN drops frequently, device otherwise healthy."""
    # Flap burst every ~20 min, lasts ~5 min
    minute = t.minute % 20
    flapping = 1 if 15 <= minute <= 20 else 0
    drops = random.randint(3, 12) if flapping else 0
    return {
        "cpu_pct":      clamp(28 + flapping * 20 + noise(5)),
        "mem_pct":      clamp(37 + noise(3)),
        "thermal_c":    clamp(49 + noise(2), 0, 120),
        "storage_pct":  clamp(state.get("storage", 40) + random.uniform(0, 0.003)),
        "wan_drops":    drops,
        "error_count":  drops,
    }

def gen_crash_loop(t, state):
    """All metrics erratic — device near failure / crash-loop."""
    elapsed_hrs = state["elapsed_hrs"]
    severity = min(elapsed_hrs / 6.0, 1.0)  # worsens over time
    return {
        "cpu_pct":      clamp(random.uniform(20, 40) + severity * random.uniform(30, 60) + noise(8)),
        "mem_pct":      clamp(random.uniform(40, 60) + severity * random.uniform(20, 40) + noise(6)),
        "thermal_c":    clamp(random.uniform(55, 70) + severity * random.uniform(10, 25) + noise(4), 0, 120),
        "storage_pct":  clamp(state.get("storage", 55) + random.uniform(0, 0.02)),
        "wan_drops":    random.randint(0, int(severity * 20)),
        "error_count":  random.randint(0, int(severity * 15)),
    }

GENERATORS = {
    "normal":        gen_normal,
    "cpu_degraded":  gen_cpu_degraded,
    "memory_leak":   gen_memory_leak,
    "thermal_spike": gen_thermal_spike,
    "wan_flapping":  gen_wan_flapping,
    "crash_loop":    gen_crash_loop,
}

# ── Build device registry ────────────────────────────────────────────────────
def build_devices():
    devices = []
    for profile, cfg in DEVICE_PROFILES.items():
        for i in range(cfg["count"]):
            devices.append({
                "device_id": make_device_id(profile, i),
                "profile":   profile,
                "label":     cfg["label"],
                # randomise start storage between devices
                "base_storage": random.uniform(30, 60),
            })
    return devices

# ── Main generation loop ─────────────────────────────────────────────────────
def generate():
    devices   = build_devices()
    start_ts  = datetime.now() - timedelta(days=DAYS)
    total_pts = int(DAYS * 24 * 3600 / INTERVAL_SECONDS)

    records   = []
    print(f"Generating {len(devices)} devices × {total_pts} time points = "
          f"{len(devices) * total_pts:,} rows ...")

    for device in devices:
        profile  = device["profile"]
        gen_fn   = GENERATORS[profile]
        storage  = device["base_storage"]
        elapsed_sec = 0

        for step in range(total_pts):
            ts = start_ts + timedelta(seconds=step * INTERVAL_SECONDS)
            elapsed_hrs = elapsed_sec / 3600.0

            state = {
                "elapsed_hrs": elapsed_hrs,
                "storage":     storage,
            }

            metrics = gen_fn(ts, state)
            storage = metrics["storage_pct"]  # persist storage state
            elapsed_sec += INTERVAL_SECONDS

            # Derive flag columns (matches Silver layer logic)
            cpu_spike_flag  = int(metrics["cpu_pct"]   > 80)
            mem_leak_flag   = int(metrics["mem_pct"]   > 85)
            thermal_warn    = int(metrics["thermal_c"] > 78)
            wan_warn        = int(metrics["wan_drops"] > 5)

            record = {
                # identity
                "device_id":       device["device_id"],
                "timestamp":       int(ts.timestamp() * 1000),  # epoch ms
                "timestamp_iso":   ts.isoformat(),
                # raw metrics
                "cpu_pct":         round(metrics["cpu_pct"], 2),
                "mem_pct":         round(metrics["mem_pct"], 2),
                "thermal_c":       round(metrics["thermal_c"], 2),
                "storage_pct":     round(metrics["storage_pct"], 2),
                "wan_drops":       int(metrics["wan_drops"]),
                "error_count":     int(metrics["error_count"]),
                # derived flags (Silver layer equivalent)
                "cpu_spike_flag":  cpu_spike_flag,
                "mem_leak_flag":   mem_leak_flag,
                "thermal_warn":    thermal_warn,
                "wan_warn":        wan_warn,
                # ground truth label (for ML training only — not available in prod)
                "fault_type":      profile,
                "fault_label":     device["label"],
            }
            records.append(record)

    return records

# ── Write outputs ────────────────────────────────────────────────────────────
def write_csv(records, path):
    if not records:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    print(f"CSV written  → {path}  ({len(records):,} rows)")

def write_jsonl(records, path):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"JSONL written → {path}  ({len(records):,} messages)")

def write_summary(records, path):
    from collections import Counter
    counts = Counter(r["fault_type"] for r in records)
    summary = {
        "total_records":    len(records),
        "num_devices":      NUM_DEVICES,
        "days":             DAYS,
        "interval_seconds": INTERVAL_SECONDS,
        "records_per_pattern": dict(counts),
        "columns": list(records[0].keys()) if records else [],
    }
    with open(path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary      → {path}")
    print("\nRecord counts per fault type:")
    for k, v in counts.items():
        print(f"  {k:<16} {v:>8,} rows")

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    records = generate()
    write_csv(records,     OUTPUT_DIR / "cpe_telemetry.csv")
    write_jsonl(records,   OUTPUT_DIR / "cpe_telemetry.jsonl")
    write_summary(records, OUTPUT_DIR / "dataset_summary.json")
    print(f"\nDone. Total records: {len(records):,}")
