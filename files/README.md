# CPE Smart Reboot — POC Setup Guide

## What this POC proves end-to-end
1. Synthetic CPE telemetry flows through Kafka into Delta Lake (Bronze)
2. Spark transforms Bronze → Silver → Gold with anomaly flags
3. Isolation Forest ML model detects 5 fault types and scores 0–100
4. CRM diagnosis API takes a user_id → finds device → returns ML diagnosis
5. Action layer issues TR-369 reboot command when risk_score > 80.9

---

## Directory structure
```
cpe_poc/
├── synthetic_data/
│   ├── generate_cpe_data.py     ← run first
│   ├── cpe_telemetry.csv        ← 201,600 rows, 7 days, 20 devices
│   └── cpe_telemetry.jsonl      ← Kafka-ready messages (one per line)
├── kafka_producer/
│   └── cpe_kafka_producer.py    ← streams JSONL to Kafka
├── ml_model/
│   ├── train_isolation_forest.py ← trains the model
│   ├── cpe_isolation_forest.pkl  ← trained model
│   ├── feature_scaler.pkl        ← StandardScaler
│   ├── thresholds.json           ← auto-computed risk thresholds
│   └── cpe_model_databricks.py   ← paste into Databricks notebook
├── crm_api/
│   └── crm_diagnosis_api.py     ← FastAPI diagnosis endpoint
└── notebooks/
    (copy Databricks notebook code here)
```

---

## Step 1 — Generate synthetic data
```bash
python synthetic_data/generate_cpe_data.py
# Output: cpe_telemetry.csv  (201,600 rows)
#         cpe_telemetry.jsonl (Kafka-ready)
```

**6 fault patterns generated:**
| Pattern        | Devices | Behaviour                              |
|----------------|---------|----------------------------------------|
| normal         | 8       | Stable CPU ~25%, mem ~35%, quiet       |
| cpu_degraded   | 3       | CPU climbs to 85%+ (runaway process)   |
| memory_leak    | 3       | Memory grows 1%/hr, never released     |
| thermal_spike  | 2       | Temperature surges >80°C in waves      |
| wan_flapping   | 2       | WAN drops 3–12x per burst window       |
| crash_loop     | 2       | All metrics erratic, worsening over time|

---

## Step 2 — Start Kafka (Docker)
```bash
# docker-compose.yml (minimal single-node)
version: '3'
services:
  zookeeper:
    image: confluentinc/cp-zookeeper:7.4.0
    environment:
      ZOOKEEPER_CLIENT_PORT: 2181
  kafka:
    image: confluentinc/cp-kafka:7.4.0
    depends_on: [zookeeper]
    ports: ["9092:9092"]
    environment:
      KAFKA_BROKER_ID: 1
      KAFKA_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://localhost:9092
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1

docker compose up -d

# Create the 3 topics
docker exec kafka kafka-topics --create --topic cpe.telemetry.raw      --bootstrap-server localhost:9092 --partitions 4
docker exec kafka kafka-topics --create --topic cpe.telemetry.enriched --bootstrap-server localhost:9092 --partitions 4
docker exec kafka kafka-topics --create --topic cpe.alerts             --bootstrap-server localhost:9092 --partitions 2
```

---

## Step 3 — Stream data into Kafka
```bash
pip install kafka-python

# Replay 7-day history at 100x speed (~10 minutes)
python kafka_producer/cpe_kafka_producer.py --mode replay --speed 100

# OR live streaming (new events every 60s per device)
python kafka_producer/cpe_kafka_producer.py --mode live --devices 10

# DRY-RUN (no Kafka needed — prints to stdout):
python kafka_producer/cpe_kafka_producer.py --mode replay --speed 200
```

---

## Step 4 — Databricks: Bronze → Silver → Gold
Copy `ml_model/cpe_model_databricks.py` content into a Databricks notebook.
Alternatively use the notebook cells below for the POC:

```python
# Cell 1 — Bronze: read from Kafka
bronze_df = (
  spark.readStream.format("kafka")
    .option("kafka.bootstrap.servers", "your-broker:9092")
    .option("subscribe", "cpe.telemetry.raw")
    .option("startingOffsets", "earliest")
    .load()
    .selectExpr("CAST(value AS STRING) as json_str")
    .select(from_json("json_str", schema).alias("d")).select("d.*")
)
bronze_df.writeStream.format("delta").option("checkpointLocation", "/tmp/bronze_ckpt").start("/delta/cpe_bronze")

# Cell 2 — Silver: clean + flag
silver_df = spark.read.format("delta").load("/delta/cpe_bronze")
silver_df = silver_df.withColumn("cpu_spike_flag", (col("cpu_pct") > 80).cast("int"))
silver_df = silver_df.withColumn("mem_leak_flag",  (col("mem_pct") > 85).cast("int"))
silver_df.write.format("delta").mode("overwrite").save("/delta/cpe_silver")

# Cell 3 — Gold: 1-hr window aggregates
gold_df = (silver_df.groupBy("device_id", window("event_ts", "1 hour"))
  .agg(avg("cpu_pct").alias("cpu_avg_1h"), max("cpu_pct").alias("cpu_max_1h"),
       avg("mem_pct").alias("mem_avg_1h"), max("thermal_c").alias("thermal_max_1h"),
       sum("wan_drops").alias("wan_drops_1h"), sum("error_count").alias("error_count_1h")))
gold_df.write.format("delta").mode("overwrite").save("/delta/cpe_gold")
```

---

## Step 5 — Train ML model
```bash
pip install scikit-learn pandas numpy

python ml_model/train_isolation_forest.py
# Output: cpe_isolation_forest.pkl
#         feature_scaler.pkl
#         thresholds.json  (auto-computed: watch<44.3, proactive<80.9, reactive>=80.9)
```

**Model performance on test set:**
- Accuracy: 87%
- False positive rate: 6.2%
- `crash_loop`  mean risk score: 96/100
- `cpu_degraded` mean risk score: 76/100
- `normal`       mean risk score: 19/100

---

## Step 6 — CRM diagnosis API
```bash
pip install fastapi uvicorn

uvicorn crm_api.crm_diagnosis_api:app --reload --port 8000

# Test it
curl "http://localhost:8000/health"
curl "http://localhost:8000/diagnose?user_id=USR-002&ticket_id=TKT-001"
curl "http://localhost:8000/devices"
```

**Response includes:**
- `risk_score` 0–100
- `action` : WATCH | PROACTIVE_REBOOT | REACTIVE_REBOOT
- `diagnosis` : confidence % for 5 fault types
- `telemetry` : live snapshot (cpu, mem, thermal, storage, wan_drops)
- `ai_summary` : plain-English summary for the support agent

---

## Answer: Do you need a separate server for CRM?

**NO. One microservice, not a new server.**

```
CRM ticket → [crm_diagnosis_api.py on port 8000]
                   ├── user_id lookup  (mapping table / DB)
                   ├── Gold table query (Databricks SQL or CSV for POC)
                   └── ML model inference (pkl loaded in memory)
                        ↓
                   JSON response → CRM agent screen
```

The API runs in the same Kubernetes namespace or VM as your action service.
It does NOT need its own database, message queue, or logging server.
For production: add authentication (API key or OAuth), rate limiting,
and point the Gold table query at Databricks SQL endpoint instead of CSV.

---

## Decision thresholds (from trained model)
| Risk score  | Action            | Meaning                              |
|-------------|-------------------|--------------------------------------|
| < 44.3      | WATCH             | Normal — continue telemetry          |
| 44.3 – 80.8 | PROACTIVE_REBOOT  | Degrading — schedule 02:00–04:00 AM  |
| ≥ 80.9      | REACTIVE_REBOOT   | Critical — immediate + alert NOC     |

---

## Install all dependencies
```bash
pip install kafka-python scikit-learn pandas numpy fastapi uvicorn
```
