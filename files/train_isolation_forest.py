"""
CPE Anomaly Detection — Isolation Forest Trainer
==================================================
Trains an Isolation Forest on the synthetic Gold-equivalent features.
Outputs:
  - ml_model/cpe_isolation_forest.pkl   (serialised model)
  - ml_model/feature_scaler.pkl         (StandardScaler)
  - ml_model/thresholds.json            (risk_score thresholds)
  - ml_model/training_report.txt        (metrics summary)

For Databricks: copy the notebook version (cpe_model_databricks.py)
which uses spark.read.delta() instead of CSV.
"""

import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import classification_report, confusion_matrix

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent
DATA_PATH    = BASE_DIR / "synthetic_data" / "cpe_telemetry.csv"
MODEL_DIR    = Path(__file__).parent
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# ── Feature config ────────────────────────────────────────────────────────────
# These are the Gold-layer features the model sees at inference time
RAW_FEATURES = [
    "cpu_pct",
    "mem_pct",
    "thermal_c",
    "storage_pct",
    "wan_drops",
    "error_count",
]

# Engineered features (computed per device per 1-hr window in Gold layer)
# Here we compute rolling approximations on the flat CSV for training
ENGINEERED_FEATURES = [
    "cpu_pct",
    "mem_pct",
    "thermal_c",
    "storage_pct",
    "wan_drops",
    "error_count",
    "cpu_spike_flag",
    "mem_leak_flag",
    "thermal_warn",
    "wan_warn",
    "cpu_rolling_mean",   # 5-event rolling mean per device
    "mem_rolling_mean",
    "thermal_rolling_max",
    "mem_rolling_delta",  # mem[t] - mem[t-5]  → leak signal
]

LABEL_COL  = "fault_label"
FAULT_NAMES = {
    0: "normal",
    1: "cpu_degraded",
    2: "memory_leak",
    3: "thermal_spike",
    4: "wan_flapping",
    5: "crash_loop",
}

# ── Load data ─────────────────────────────────────────────────────────────────
def load_data():
    print(f"Loading data from {DATA_PATH} ...")
    df = pd.read_csv(DATA_PATH, parse_dates=["timestamp_iso"])
    df = df.sort_values(["device_id", "timestamp"])
    print(f"  Rows: {len(df):,}  |  Devices: {df['device_id'].nunique()}")
    print(f"  Label distribution:\n{df['fault_type'].value_counts().to_string()}\n")
    return df

# ── Feature engineering ───────────────────────────────────────────────────────
def engineer_features(df):
    print("Engineering features ...")
    grp = df.groupby("device_id")

    df["cpu_rolling_mean"]   = grp["cpu_pct"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["mem_rolling_mean"]   = grp["mem_pct"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    df["thermal_rolling_max"]= grp["thermal_c"].transform(lambda x: x.rolling(5, min_periods=1).max())
    df["mem_rolling_delta"]  = grp["mem_pct"].transform(lambda x: x.diff(5).fillna(0))

    df = df.dropna(subset=ENGINEERED_FEATURES)
    print(f"  Features ready. Shape: {df.shape}\n")
    return df

# ── Train / test split (time-based — last 20% of each device's timeline) ─────
def split_data(df):
    train_rows, test_rows = [], []
    for _, grp in df.groupby("device_id"):
        split = int(len(grp) * 0.8)
        train_rows.append(grp.iloc[:split])
        test_rows.append(grp.iloc[split:])
    train_df = pd.concat(train_rows)
    test_df  = pd.concat(test_rows)

    # Isolation Forest trains only on NORMAL samples (unsupervised)
    normal_train = train_df[train_df[LABEL_COL] == 0]
    print(f"Train set (normal only): {len(normal_train):,} rows")
    print(f"Test set  (all classes): {len(test_df):,} rows\n")
    return normal_train, test_df

# ── Train ─────────────────────────────────────────────────────────────────────
def train_model(normal_train_df):
    X_train = normal_train_df[ENGINEERED_FEATURES].values

    scaler  = StandardScaler()
    X_scaled = scaler.fit_transform(X_train)

    print("Training Isolation Forest ...")
    t0 = time.time()
    model = IsolationForest(
        n_estimators=200,       # more trees = more stable scores
        max_samples="auto",
        contamination=0.05,     # ~5% expected anomaly rate
        max_features=1.0,
        bootstrap=False,
        n_jobs=-1,
        random_state=42,
    )
    model.fit(X_scaled)
    print(f"  Trained in {time.time() - t0:.1f}s\n")
    return model, scaler

# ── Evaluate ──────────────────────────────────────────────────────────────────
def evaluate(model, scaler, test_df):
    X_test  = test_df[ENGINEERED_FEATURES].values
    X_scaled = scaler.transform(X_test)
    y_true  = test_df[LABEL_COL].values

    # Isolation Forest: predict() returns +1 (normal) or -1 (anomaly)
    raw_pred    = model.predict(X_scaled)
    anomaly_pred = (raw_pred == -1).astype(int)  # 1 = anomaly, 0 = normal
    binary_true  = (y_true > 0).astype(int)       # 1 = any fault, 0 = normal

    # Raw anomaly scores (more negative = more anomalous)
    scores       = model.score_samples(X_scaled)
    # Normalise to 0-100 risk scale (0 = normal, 100 = most anomalous)
    s_min, s_max = scores.min(), scores.max()
    risk_scores  = 100 * (1 - (scores - s_min) / (s_max - s_min + 1e-9))

    print("=" * 55)
    print("BINARY CLASSIFICATION (normal vs anomaly)")
    print("=" * 55)
    print(classification_report(
        binary_true, anomaly_pred,
        target_names=["normal", "anomaly"]
    ))

    print("=" * 55)
    print("CONFUSION MATRIX (normal=0 vs anomaly=1)")
    print("=" * 55)
    cm = confusion_matrix(binary_true, anomaly_pred)
    print(f"  True  Normal  → predicted Normal: {cm[0][0]:>6,}  | Anomaly: {cm[0][1]:>6,}")
    print(f"  True  Anomaly → predicted Normal: {cm[1][0]:>6,}  | Anomaly: {cm[1][1]:>6,}")
    fp_rate = cm[0][1] / (cm[0][0] + cm[0][1] + 1e-9) * 100
    print(f"\n  False positive rate (normal misclassified as anomaly): {fp_rate:.1f}%")

    print("\nRISK SCORE DISTRIBUTION by fault type:")
    test_df = test_df.copy()
    test_df["risk_score"] = risk_scores
    for label, name in FAULT_NAMES.items():
        subset = test_df[test_df[LABEL_COL] == label]["risk_score"]
        if len(subset):
            print(f"  {name:<16} mean={subset.mean():>5.1f}  p95={subset.quantile(0.95):>5.1f}  max={subset.max():>5.1f}")

    return risk_scores, test_df

# ── Compute decision thresholds from risk score distribution ──────────────────
def compute_thresholds(test_df):
    normal_scores  = test_df[test_df[LABEL_COL] == 0]["risk_score"]
    anomaly_scores = test_df[test_df[LABEL_COL] >  0]["risk_score"]

    # Proactive threshold: above 95th percentile of normal scores
    proactive_thr  = float(normal_scores.quantile(0.95))
    # Reactive threshold: 75th percentile of actual anomaly scores
    reactive_thr   = float(anomaly_scores.quantile(0.75))

    proactive_thr  = round(proactive_thr, 1)
    reactive_thr   = round(max(reactive_thr, proactive_thr + 10), 1)

    thresholds = {
        "watch_max":       round(proactive_thr - 0.1, 1),
        "proactive_min":   proactive_thr,
        "reactive_min":    reactive_thr,
        "description": {
            "watch":     f"risk_score < {proactive_thr}  → monitor, no action",
            "proactive": f"{proactive_thr} <= risk_score < {reactive_thr} → schedule reboot",
            "reactive":  f"risk_score >= {reactive_thr}  → immediate reboot + alert",
        }
    }
    print(f"\nDECISION THRESHOLDS (auto-computed from data):")
    for k, v in thresholds["description"].items():
        print(f"  {k:<12} {v}")
    return thresholds

# ── Databricks notebook version (pasted directly into a cell) ─────────────────
DATABRICKS_NOTEBOOK = '''# Databricks Notebook — CPE Isolation Forest Trainer
# Paste this into a Databricks Python notebook cell
# Assumes Gold Delta table exists at: /delta/cpe_gold

from pyspark.sql import functions as F
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import mlflow, mlflow.sklearn
import pandas as pd

GOLD_TABLE = "/delta/cpe_gold"
FEATURES = [
    "cpu_pct", "mem_pct", "thermal_c", "storage_pct",
    "wan_drops", "error_count",
    "cpu_spike_flag", "mem_leak_flag", "thermal_warn", "wan_warn",
]

# Load Gold table — last 7 days of data
gold_df = spark.read.format("delta").load(GOLD_TABLE)
pdf = gold_df.toPandas()

# Train on normal devices only (risk_score < 40 approximates normal)
normal_df = pdf[pdf.get("risk_score", pd.Series([0]*len(pdf))) < 40]
X_train = normal_df[FEATURES].fillna(0).values

scaler = StandardScaler()
X_scaled = scaler.fit_transform(X_train)

model = IsolationForest(n_estimators=200, contamination=0.05, random_state=42, n_jobs=-1)

with mlflow.start_run(run_name="isolation_forest_v1"):
    model.fit(X_scaled)
    mlflow.sklearn.log_model(model,  "isolation_forest")
    mlflow.sklearn.log_model(scaler, "scaler")
    mlflow.log_params({"n_estimators": 200, "contamination": 0.05})
    # Score all devices and write back to Gold table
    X_all    = pdf[FEATURES].fillna(0).values
    X_all_sc = scaler.transform(X_all)
    raw_sc   = model.score_samples(X_all_sc)
    s_min, s_max = raw_sc.min(), raw_sc.max()
    pdf["anomaly_score"] = 1 - (raw_sc - s_min) / (s_max - s_min + 1e-9)
    pdf["risk_score"]    = (pdf["anomaly_score"] * 100).round(1)
    pdf["action_label"]  = pdf["risk_score"].apply(
        lambda s: "REACTIVE" if s >= 65 else ("PROACTIVE" if s >= 40 else "WATCH")
    )
    result_sdf = spark.createDataFrame(pdf)
    result_sdf.write.format("delta").mode("overwrite").save(GOLD_TABLE + "_scored")
    print("Model trained and Gold table scored. Check MLflow for run details.")
'''

# ── Save everything ───────────────────────────────────────────────────────────
def save_artifacts(model, scaler, thresholds, report_lines):
    with open(MODEL_DIR / "cpe_isolation_forest.pkl", "wb") as f:
        pickle.dump(model, f)
    with open(MODEL_DIR / "feature_scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)
    with open(MODEL_DIR / "thresholds.json", "w") as f:
        json.dump(thresholds, f, indent=2)
    with open(MODEL_DIR / "feature_list.json", "w") as f:
        json.dump({"features": ENGINEERED_FEATURES}, f, indent=2)
    with open(MODEL_DIR / "training_report.txt", "w") as f:
        f.write("\n".join(report_lines))
    with open(MODEL_DIR / "cpe_model_databricks.py", "w") as f:
        f.write(DATABRICKS_NOTEBOOK)
    print(f"\nArtifacts saved to {MODEL_DIR}/")
    for p in sorted(MODEL_DIR.glob("*")):
        print(f"  {p.name}")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import io, sys

    df        = load_data()
    df        = engineer_features(df)
    normal_tr, test_df = split_data(df)
    model, scaler       = train_model(normal_tr)

    # Capture printed output for report
    buf = io.StringIO()
    sys.stdout = buf

    risk_scores, test_df = evaluate(model, scaler, test_df)
    thresholds           = compute_thresholds(test_df)

    sys.stdout = sys.__stdout__
    report_lines = buf.getvalue().splitlines()
    print(buf.getvalue())

    save_artifacts(model, scaler, thresholds, report_lines)
    print("\nTraining complete.")
