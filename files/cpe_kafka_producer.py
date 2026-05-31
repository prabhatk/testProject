"""
CPE Telemetry Kafka Producer
==============================
Two modes:
  --mode replay   : reads cpe_telemetry.jsonl and replays at configurable speed
  --mode live     : generates fresh telemetry in real-time (no file needed)

Usage:
  pip install kafka-python

  # Replay historical synthetic data (100x speed = 1 week in ~10 minutes)
  python cpe_kafka_producer.py --mode replay --speed 100

  # Live real-time generation (one event per device per 60 seconds)
  python cpe_kafka_producer.py --mode live

  # Point at a remote Kafka broker
  python cpe_kafka_producer.py --mode live --broker my-broker:9092
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Kafka import (graceful fallback for environments without kafka-python) ──
try:
    from kafka import KafkaProducer
    from kafka.errors import NoBrokersAvailable
    KAFKA_AVAILABLE = True
except ImportError:
    KAFKA_AVAILABLE = False
    print("[WARN] kafka-python not installed. Running in DRY-RUN mode (stdout only).")
    print("       Install with: pip install kafka-python\n")

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_BROKER   = "localhost:9092"
RAW_TOPIC        = "cpe.telemetry.raw"
JSONL_PATH       = Path(__file__).parent.parent / "synthetic_data" / "cpe_telemetry.jsonl"

# ── Noise helper (same as generator) ─────────────────────────────────────────
def clamp(val, lo=0.0, hi=100.0):
    return max(lo, min(hi, val))

def noise(scale=1.0):
    return random.gauss(0, scale)

# ── Live generator (simplified normal + anomaly patterns for streaming demo) ─
LIVE_DEVICE_STATES = {}

def init_live_devices(n=10):
    profiles = (
        ["normal"] * 5 +
        ["cpu_degraded"] * 2 +
        ["memory_leak"] * 1 +
        ["wan_flapping"] * 1 +
        ["crash_loop"] * 1
    )
    for i, profile in enumerate(profiles[:n]):
        dev_id = f"CPE-LIVE-{i:04d}"
        LIVE_DEVICE_STATES[dev_id] = {
            "profile":     profile,
            "start_ts":    time.time(),
            "storage":     random.uniform(30, 55),
        }

def gen_live_event(device_id):
    state   = LIVE_DEVICE_STATES[device_id]
    profile = state["profile"]
    elapsed = (time.time() - state["start_ts"]) / 3600.0
    storage = state["storage"]
    ts_ms   = int(time.time() * 1000)
    now     = datetime.now()

    if profile == "normal":
        cpu = clamp(25 + noise(4))
        mem = clamp(35 + noise(3))
        thm = clamp(48 + noise(3), 0, 120)
        wan = 0
        err = 0
    elif profile == "cpu_degraded":
        cpu = clamp(25 + min(elapsed * 20, 60) + noise(5))
        mem = clamp(45 + noise(4))
        thm = clamp(52 + (cpu - 25) * 0.3 + noise(2), 0, 120)
        wan = 0
        err = random.choices([0, 1, 2], weights=[70, 20, 10])[0]
    elif profile == "memory_leak":
        cpu = clamp(30 + noise(4))
        mem = clamp(30 + elapsed * 1.0 + noise(2))
        thm = clamp(50 + noise(2), 0, 120)
        wan = 0
        err = random.choices([0, 1], weights=[85, 15])[0]
    elif profile == "wan_flapping":
        minute = now.minute % 20
        flap   = 1 if 15 <= minute else 0
        cpu    = clamp(28 + flap * 20 + noise(5))
        mem    = clamp(37 + noise(3))
        thm    = clamp(49 + noise(2), 0, 120)
        wan    = random.randint(3, 12) if flap else 0
        err    = wan
    else:  # crash_loop
        sev    = min(elapsed / 6.0, 1.0)
        cpu    = clamp(random.uniform(20, 40) + sev * random.uniform(30, 60) + noise(8))
        mem    = clamp(random.uniform(40, 60) + sev * random.uniform(20, 40) + noise(6))
        thm    = clamp(random.uniform(55, 70) + sev * random.uniform(10, 25) + noise(4), 0, 120)
        wan    = random.randint(0, int(sev * 20))
        err    = random.randint(0, int(sev * 15))
        storage = min(storage + random.uniform(0, 0.02), 100)

    state["storage"] = min(storage + random.uniform(0, 0.005), 100)

    return {
        "device_id":      device_id,
        "timestamp":      ts_ms,
        "timestamp_iso":  datetime.now().isoformat(),
        "cpu_pct":        round(cpu, 2),
        "mem_pct":        round(mem, 2),
        "thermal_c":      round(thm, 2),
        "storage_pct":    round(storage, 2),
        "wan_drops":      int(wan),
        "error_count":    int(err),
        "cpu_spike_flag": int(cpu > 80),
        "mem_leak_flag":  int(mem > 85),
        "thermal_warn":   int(thm > 78),
        "wan_warn":       int(wan > 5),
        "fault_type":     profile,
    }

# ── Producer wrapper ──────────────────────────────────────────────────────────
class DryRunProducer:
    """Prints to stdout when Kafka is unavailable."""
    def __init__(self, *args, **kwargs):
        pass
    def send(self, topic, value=None, key=None):
        msg = json.loads(value.decode()) if isinstance(value, bytes) else value
        print(f"[DRY-RUN] topic={topic} | device={msg.get('device_id')} "
              f"| cpu={msg.get('cpu_pct')}% mem={msg.get('mem_pct')}% "
              f"thm={msg.get('thermal_c')}°C profile={msg.get('fault_type')}")
    def flush(self): pass
    def close(self): pass

def make_producer(broker):
    if not KAFKA_AVAILABLE:
        return DryRunProducer()
    try:
        producer = KafkaProducer(
            bootstrap_servers=[broker],
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k else None,
            acks="all",
            retries=3,
            linger_ms=5,
        )
        print(f"[OK] Connected to Kafka broker: {broker}")
        return producer
    except NoBrokersAvailable:
        print(f"[WARN] No Kafka broker at {broker} — switching to DRY-RUN mode.")
        return DryRunProducer()

# ── Replay mode ───────────────────────────────────────────────────────────────
def run_replay(producer, speed, max_messages):
    if not JSONL_PATH.exists():
        print(f"[ERROR] JSONL file not found: {JSONL_PATH}")
        print("        Run synthetic_data/generate_cpe_data.py first.")
        sys.exit(1)

    print(f"[REPLAY] Reading {JSONL_PATH}")
    print(f"[REPLAY] Speed: {speed}x  |  Max messages: {max_messages or 'unlimited'}")
    print(f"[REPLAY] Topic: {RAW_TOPIC}\n")

    sent      = 0
    prev_ts   = None
    interval  = 1.0 / speed  # real seconds between messages

    with open(JSONL_PATH) as f:
        for line in f:
            if max_messages and sent >= max_messages:
                break
            try:
                record = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            # Use device_id as partition key so one device always hits same partition
            producer.send(RAW_TOPIC, value=record, key=record["device_id"])
            sent += 1

            if sent % 1000 == 0:
                producer.flush()
                print(f"[REPLAY] Sent {sent:,} messages ...")

            time.sleep(interval)

    producer.flush()
    print(f"\n[REPLAY] Complete. Total sent: {sent:,}")

# ── Live mode ─────────────────────────────────────────────────────────────────
def run_live(producer, interval_sec, num_devices):
    init_live_devices(num_devices)
    device_ids = list(LIVE_DEVICE_STATES.keys())
    print(f"[LIVE] Streaming {len(device_ids)} virtual devices every {interval_sec}s")
    print(f"[LIVE] Profiles: {[LIVE_DEVICE_STATES[d]['profile'] for d in device_ids]}")
    print(f"[LIVE] Topic: {RAW_TOPIC}\n")

    sent = 0
    while True:
        batch_start = time.time()
        for device_id in device_ids:
            event = gen_live_event(device_id)
            producer.send(RAW_TOPIC, value=event, key=device_id)
            sent += 1
        producer.flush()

        elapsed = time.time() - batch_start
        sleep_for = max(0, interval_sec - elapsed)
        print(f"[LIVE] Batch sent: {len(device_ids)} events | total={sent} | sleep={sleep_for:.1f}s")
        time.sleep(sleep_for)

# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CPE Telemetry Kafka Producer")
    parser.add_argument("--mode",         choices=["replay", "live"], default="live",
                        help="replay: stream from JSONL file | live: generate in real time")
    parser.add_argument("--broker",       default=DEFAULT_BROKER,
                        help=f"Kafka broker address (default: {DEFAULT_BROKER})")
    parser.add_argument("--speed",        type=float, default=60.0,
                        help="Replay speed multiplier (default: 60 = 1 hour in 1 minute)")
    parser.add_argument("--max-messages", type=int,   default=0,
                        help="Replay: stop after N messages (0 = unlimited)")
    parser.add_argument("--interval",     type=float, default=60.0,
                        help="Live mode: seconds between batches (default: 60)")
    parser.add_argument("--devices",      type=int,   default=10,
                        help="Live mode: number of virtual devices (default: 10)")
    args = parser.parse_args()

    producer = make_producer(args.broker)
    try:
        if args.mode == "replay":
            run_replay(producer, args.speed, args.max_messages)
        else:
            run_live(producer, args.interval, args.devices)
    except KeyboardInterrupt:
        print("\n[STOP] Producer stopped by user.")
    finally:
        producer.close()
