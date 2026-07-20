# Databricks 1-Hour Session — Prep Guide (Customer Churn Demo)

Follow this top to bottom on **Databricks Free Edition** (the current free tier — it replaced the old Community Edition in 2025, and it's serverless-only but includes Unity Catalog, Workflows, and MLflow). Everything below is designed to run end-to-end on it.

---

## 0. Account & Workspace Setup

1. Go to **https://login.databricks.com/** and sign up for **Free Edition** (no credit card, no business email needed).
2. Databricks provisions a workspace for you automatically — this takes 1–2 minutes.
3. Once inside, click **Create > Notebook**, name it `churn_demo`, set the default language to **Python**, and attach it to the **Serverless** compute (it's the only option, and that's fine — no cluster config needed).
4. In the left sidebar, open **Catalog** and confirm you have a default catalog (usually named after your workspace or `workspace`). Unity Catalog is on by default here.

Run this first cell to create your own catalog structure for the demo:

```python
# Cell 1 — Setup catalog/schema (Unity Catalog)
spark.sql("CREATE CATALOG IF NOT EXISTS churn_demo")
spark.sql("USE CATALOG churn_demo")
spark.sql("CREATE SCHEMA IF NOT EXISTS bronze")
spark.sql("CREATE SCHEMA IF NOT EXISTS silver")
spark.sql("CREATE SCHEMA IF NOT EXISTS gold")
print("Catalog and schemas ready.")
```

---

> **🗣️ Say this (intro, ~5 min):**
> "Most of you already work with Spark, warehouses, maybe a lake sitting next to a warehouse with duplicated data between them. That duplication — one copy for BI, another for ML — is exactly the problem Databricks set out to remove. The idea is called a Lakehouse: one copy of data, governed in one place, that both your SQL analysts and your ML pipelines read from directly. Today I'm not going to just tell you that — I'm going to build one thing, live, in front of you: a customer churn pipeline, from raw file to a trained model to a dashboard, all on the same data. By the end you'll have seen ingestion, Delta Lake, transformations, ML with MLflow, orchestration, and SQL — as one connected example, not eight disconnected demos."

> **🗣️ Say this (architecture tour, ~8 min):**
> "Before I touch any code, here's the map. Everything you write lands as Delta tables — that's the storage format under the hood, and it's what gives us ACID transactions and time travel, which I'll show you in a minute. Sitting above storage is Unity Catalog — think of it as the governance layer: who can see which table, lineage of where data came from, and it's what lets me register an ML model right next to my tables, in the same namespace. And for compute, in the free tier we're on Serverless — no cluster sizing, it just spins up. In production you'd have job clusters, SQL warehouses, and serverless side by side, but the workspace experience is identical."

## 1. Bronze Layer — Raw Ingestion

We'll pull the well-known IBM Telco Customer Churn dataset directly from a public URL — no manual upload needed.

> **🗣️ Say this (~8 min):**
> "This is the Bronze layer — we land data exactly as it arrives, no cleaning, no assumptions. Right now I'm just reading a CSV, but in production you'd point this at cloud storage with Auto Loader, and it would pick up new files incrementally without you managing checkpoints yourself. The point of Bronze is: if something downstream breaks, you can always replay from here, because nothing's been thrown away or transformed yet."

```python
# Cell 2 — Ingest raw CSV into Bronze
import pandas as pd

url = "https://raw.githubusercontent.com/IBM/telco-customer-churn-on-icp4d/master/data/Telco-Customer-Churn.csv"
pdf = pd.read_csv(url)

raw_df = spark.createDataFrame(pdf)

raw_df.write.mode("overwrite").saveAsTable("churn_demo.bronze.customers_raw")

display(raw_df.limit(10))
```

**Talking point for the session:** this is the "just land it as-is" layer — no cleaning, no assumptions, just captured exactly as it arrived. Mention `Auto Loader` here as the production-grade way to do this incrementally from cloud storage, even though here we're doing a one-off load for time.

---

## 2. Delta Lake Essentials (quick live demo)

Run these right after loading Bronze — this is the "wow" moment for engineers coming from plain Parquet.

> **🗣️ Say this (~7 min):**
> "This table I just wrote isn't a Parquet file — it's Delta. Watch what that gets me. `DESCRIBE HISTORY` shows every write that's ever happened to this table, with a version number and timestamp — this is free, I didn't build any of this myself. Now watch — I'm going to append some new rows, simulating a correction batch landing. Row count goes up. But if I query `VERSION AS OF 0`, I get the exact state before that write — that's time travel. No custom snapshotting logic, no separate audit table. This is the kind of thing that used to take a real engineering effort to build on plain Parquet or Hive, and here it's a one-line query."

```python
# Cell 3 — Delta Lake features demo
# Time travel / history
spark.sql("DESCRIBE HISTORY churn_demo.bronze.customers_raw").show(truncate=False)

# Show current row count
print("Row count:", spark.table("churn_demo.bronze.customers_raw").count())
```

```python
# Cell 4 — Simulate an update to show ACID + time travel
from pyspark.sql.functions import lit

# Pretend we got a correction batch
updated = raw_df.limit(5).withColumn("SeniorCitizen", lit(1))
updated.write.mode("append").saveAsTable("churn_demo.bronze.customers_raw")

# Query version 0 (before the append) vs latest
spark.sql("SELECT COUNT(*) FROM churn_demo.bronze.customers_raw VERSION AS OF 0").show()
spark.sql("SELECT COUNT(*) FROM churn_demo.bronze.customers_raw").show()
```

Explain schema enforcement by trying (and catching) a bad schema write if time allows — otherwise just narrate it.

---

## 3. Silver Layer — Cleaning

> **🗣️ Say this (before running, ~1 min in):**
> "Bronze to Silver is where the actual data engineering happens — type casting, deduplication, handling nulls that shouldn't be there. Notice `TotalCharges` came in as a string with blank values instead of nulls — that's exactly the kind of thing you catch here, not further downstream where it silently breaks a model."

```python
# Cell 5 — Bronze to Silver
from pyspark.sql.functions import col, when, trim

bronze_df = spark.table("churn_demo.bronze.customers_raw")

silver_df = (
    bronze_df
    .withColumn("TotalCharges", when(trim(col("TotalCharges")) == "", None)
                                 .otherwise(col("TotalCharges")).cast("double"))
    .withColumn("MonthlyCharges", col("MonthlyCharges").cast("double"))
    .withColumn("tenure", col("tenure").cast("int"))
    .dropDuplicates(["customerID"])
    .na.drop(subset=["TotalCharges"])
)

silver_df.write.mode("overwrite").saveAsTable("churn_demo.silver.customers_clean")
display(silver_df.limit(10))
```

**Talking point:** this is where you'd normally also do joins across sources (e.g. billing system + CRM) — for the demo it's a single source, but call that out explicitly so it doesn't look like an oversight.

---

## 4. Gold Layer — Feature Engineering

> **🗣️ Say this (~10 min for Silver+Gold combined):**
> "Gold is business-ready, feature-ready data — this is what a dashboard or a model actually consumes. I'm bucketing tenure into ranges and creating the churn label here. Notice this table lives right next to the Silver and Bronze ones, same catalog, same governance — an analyst querying Gold for a dashboard and a data scientist training a model off Gold are reading the exact same table. That's the unification story, made concrete."

```python
# Cell 6 — Silver to Gold (features for ML)
from pyspark.sql.functions import when as sql_when

gold_df = (
    silver_df
    .withColumn("tenure_bucket",
        sql_when(col("tenure") <= 12, "0-12")
        .when(col("tenure") <= 24, "13-24")
        .when(col("tenure") <= 48, "25-48")
        .otherwise("49+"))
    .withColumn("churn_label", sql_when(col("Churn") == "Yes", 1).otherwise(0))
)

gold_df.write.mode("overwrite").saveAsTable("churn_demo.gold.customer_features")
display(gold_df.limit(10))
```

---

## 5. ML with MLflow

> **🗣️ Say this (~10 min):**
> "This is the payoff moment, so let me slow down here. I'm training a simple logistic regression to predict churn — nothing exotic, because the point isn't the model, it's where the model lives. I turned on `mlflow.autolog()`, which means every parameter, every metric, the model artifact itself, all get logged automatically — I didn't write any manual logging code. Let me flip over to the Experiments tab... here's the run, here's accuracy and AUC, here's the model file. Now I'm registering this model into Unity Catalog, the same catalog my tables live in. That means access control, lineage, and versioning apply to this model exactly the way they apply to a table. This is the difference between ML being a side project on someone's laptop versus ML being a first-class citizen of your data platform."
import mlflow
import mlflow.sklearn
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.metrics import accuracy_score, roc_auc_score

pdf_gold = spark.table("churn_demo.gold.customer_features").toPandas()

categorical_cols = ["Contract", "InternetService", "PaymentMethod", "tenure_bucket"]
numeric_cols = ["tenure", "MonthlyCharges", "TotalCharges"]

X = pdf_gold[categorical_cols + numeric_cols]
y = pdf_gold["churn_label"]

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

preprocessor = ColumnTransformer([
    ("cat", OneHotEncoder(handle_unknown="ignore"), categorical_cols)
], remainder="passthrough")

pipeline = Pipeline([
    ("preprocess", preprocessor),
    ("clf", LogisticRegression(max_iter=1000))
])

mlflow.sklearn.autolog()

with mlflow.start_run(run_name="churn_logreg") as run:
    pipeline.fit(X_train, y_train)
    preds = pipeline.predict(X_test)
    probs = pipeline.predict_proba(X_test)[:, 1]

    acc = accuracy_score(y_test, preds)
    auc = roc_auc_score(y_test, probs)
    mlflow.log_metric("test_accuracy", acc)
    mlflow.log_metric("test_auc", auc)

    print(f"Accuracy: {acc:.3f}, AUC: {auc:.3f}")
    print("Run ID:", run.info.run_id)
```

```python
# Cell 8 — Register the model in Unity Catalog Model Registry
model_name = "churn_demo.gold.churn_classifier"
mlflow.register_model(
    model_uri=f"runs:/{run.info.run_id}/model",
    name=model_name
)
```

Open the **Experiments** tab in the left sidebar during the session to show the MLflow UI live — params, metrics, and the registered model.

---

## 6. Orchestration with Workflows (UI steps, no code)

> **🗣️ Say this (~7 min):**
> "Everything so far ran as cells I clicked through manually — that's fine for a demo, but nobody runs production pipelines by hand. This is where Workflows comes in. I'm taking the exact three stages we just walked through — ingest, transform, train — and turning them into a job with explicit dependencies. Watch the graph: Task 2 won't start until Task 1 succeeds. I can schedule this nightly, trigger it on file arrival, get alerted on failure — this is what turns a notebook demo into something you'd actually trust in production."

Do this live in the workspace UI:

1. Go to **Workflows > Create Job**.
2. Add Task 1: `ingest_bronze` → notebook task pointing at the ingestion cells.
3. Add Task 2: `transform_silver_gold` → depends on Task 1.
4. Add Task 3: `train_model` → depends on Task 2.
5. Click **Run now** and show the job graph executing with dependencies.

(For the demo, it's fine to split your single notebook into 3 separate notebooks beforehand, one per stage, so the job graph is visually clean.)

---

## 7. SQL / BI Angle (optional, if time allows)

```sql
-- Cell 9 — quick SQL cell for a Databricks SQL Dashboard
SELECT tenure_bucket, Contract, COUNT(*) AS customers,
       ROUND(AVG(churn_label) * 100, 1) AS churn_rate_pct
FROM churn_demo.gold.customer_features
GROUP BY tenure_bucket, Contract
ORDER BY tenure_bucket
```

Pin this as a visualization and add it to a quick Databricks SQL Dashboard to show the BI angle in the last few minutes.

> **🗣️ Say this (~5 min):**
> "Last stop — same Gold table, no copy, no export, straight into a SQL query and a dashboard visual. This is the table our model trained on, and it's the same table powering this chart. That's the Lakehouse pitch from the very first slide, except now you've watched it happen end to end instead of just hearing about it."

> **🗣️ Say this (closing, folded into final minutes):**
> "So in the last hour: we landed raw data in Bronze, saw Delta Lake's ACID and time-travel for free, cleaned it into Silver, engineered features into Gold, trained and registered a model with MLflow, wired it all into a Workflow, and queried the same table from SQL. One dataset, one thread, every core piece of Databricks touched at least once. Happy to take questions, or dig into any one of these pieces deeper."

---

## Pre-Session Checklist

- [ ] Signed up for Databricks Free Edition and confirmed workspace loads
- [ ] Ran Cells 1–9 successfully at least once, end to end
- [ ] Saved a **backup notebook** with all outputs already run (in case something fails live)
- [ ] Split ingestion/transform/train into 3 notebooks for the Workflows demo
- [ ] Created the Workflow job once, confirmed it runs successfully
- [ ] Practiced the timing — aim to rehearse once fully at real pace before the actual session
