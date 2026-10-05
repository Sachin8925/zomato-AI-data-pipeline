# Zomato ELT Pipeline: S3 → Snowflake → dbt → Airflow → Streamlit

End-to-end batch data pipeline on a food-delivery dataset. Raw CSVs land in Amazon S3, get loaded into Snowflake, are modelled with dbt (staging + marts, with data-quality tests), orchestrated by Apache Airflow (Docker), and explored through Streamlit apps (text-to-SQL and a review chat).

## Data Source

CSV files uploaded to S3 under `raw/<table>/`:

| Table | Description | Rows |
|---|---|---|
| `restaurant` | Restaurant master (city, cuisine, rating, cost) | 148,541 |
| `users` | Customer master | 100,000 |
| `food` | Food items | 371,561 |
| `menu` | Restaurant-wise menu with prices | 1,179,936 |
| `orders` | Order headers (generated) | 499,997 |
| `order_items` | Order line items (generated) | 499,997 |
| `reviews` | Free-text customer reviews (generated) | 300,000 |

## Architecture

```
        Amazon S3  (s3://<bucket>/raw/<table>/*.csv)
              |
              |  Storage Integration (IAM role) + External Stage
              |  COPY INTO  (file formats per source)
              v
+----------------------------+
|  RAW  (Snowflake)          |   7 tables, loaded as-is
+----------------------------+
              |
              |  dbt: clean, cast, rename, filter blank rows
              v
+----------------------------+
|  STAGING  (dbt views)      |   stg_orders, stg_order_items, stg_reviews, ...
+----------------------------+
              |
              |  dbt: joins, incremental facts, aggregates
              v
+----------------------------+
|  MARTS  (dbt tables)       |   dim_*, fct_*, mart_*
+----------------------------+
              |
              v
   Streamlit apps (text-to-SQL, review chat)

Orchestration: Airflow DAG  zomato_batch =  reload_raw  ->  dbt_build_core
```

## Tech Stack

| Layer | Tool |
|---|---|
| Storage | Amazon S3 |
| Warehouse | Snowflake |
| Transformation / testing | dbt Core (`dbt-snowflake`) |
| Orchestration | Apache Airflow 3 (Docker Compose) |
| Apps | Streamlit, Gemini API |
| Language | SQL, Python |

## Project Structure

```
.
├── airflow/
│   ├── dags/zomato_batch.py        # Airflow DAG
│   ├── Dockerfile
│   ├── docker-compose.yaml
│   └── .env.example                # copy to .env and fill in
├── zomato/                         # dbt project
│   ├── models/
│   │   ├── staging/                # sources.yml + stg_*.sql (views)
│   │   └── marts/                  # dim_*, fct_*, mart_* (tables)
│   ├── macros/generate_schema_name.sql
│   ├── dbt_project.yml
│   └── profiles.yml                # env_var based, contains no secrets
├── AI/
│   ├── text_to_sql.py              # English -> SQL -> Snowflake -> chart
│   ├── rag_chat.py                 # chat over customer reviews
│   ├── enrich_reviews.py           # LLM enrichment of reviews (optional)
│   └── .env.example
├── snowflake/                      # setup SQL (warehouse, schemas, role, integration, tables)
├── sample_data/                    # small CSV samples (full data not in repo)
└── docs/images/                    # screenshots
```

## Data Model

### Staging (views, `ZOMATO.STAGING`)
`stg_restaurants`, `stg_users`, `stg_food`, `stg_menu`, `stg_orders`, `stg_order_items`, `stg_reviews`

### Marts (tables, `ZOMATO.MARTS`)

| Model | Type | Rows | Description |
|---|---|---|---|
| `dim_date` | Dimension | 1,096 | Calendar dimension |
| `dim_restaurants` | Dimension | 148,541 | Restaurant attributes |
| `dim_customers` | Dimension | 100,000 | Customer attributes |
| `dim_food` | Dimension | 371,561 | Food items |
| `fct_orders` | Fact (incremental) | 499,997 | One row per order |
| `fct_order_items` | Fact (incremental) | 499,997 | One row per order line |
| `mart_daily_city_revenue` | Mart | 126,877 | Revenue by day, city, cuisine |
| `mart_delivery_sla` | Mart | 9,998 | Delivery-time performance |
| `mart_restaurant_performance` | Mart | 130,897 | Restaurant KPIs |

## Pipeline Orchestration

The Airflow DAG `zomato_batch` runs daily:

1. **`reload_raw`** (`SQLExecuteQueryOperator`): `COPY INTO` all 7 RAW tables from the S3 stage. No `FORCE`, so Snowflake's load history prevents duplicate loads.
2. **`dbt_build_core`** (`BashOperator`): `dbt build` runs models and tests in dependency order (AI-tagged models excluded).

## Data Quality

dbt tests on the RAW sources: `not_null` and `unique` on primary keys (11 tests, all passing). Dimension tests are set to `warn`, fact keys are strict.

Real issues caught while building this:

- **Duplicate loads**: `food` and `menu` were loaded twice (row count exactly 2x). Found by comparing `COUNT(*)` with `COUNT(DISTINCT id)`, fixed with `TRUNCATE` + a single `COPY`.
- **548,578 blank rows in `orders.csv`**: left behind after cell contents were deleted in a spreadsheet. Caught by the dbt `not_null` test on `order_id`. Fixed by cleaning the file, reloading, and adding a `where order_id is not null` guard in `stg_orders`.
- **Different date formats per file**: `orders` uses `DD-MM-YYYY HH24:MI`, `reviews` uses `YYYY-MM-DD`. Solved with a dedicated Snowflake file format per source.
- **S3 folder naming**: the stage path must match the folder name exactly (`restaurant/` vs `restaurants/`), otherwise `COPY` silently loads 0 files.

## AI Lane (Streamlit)

- **Text-to-SQL** (`AI/text_to_sql.py`): reads the table and column list from `INFORMATION_SCHEMA`, asks Gemini for one `SELECT`, checks it with a guard (only `SELECT`/`WITH`, a single statement, no write keywords), runs it on Snowflake, and charts the result.
- **Review chat** (`AI/rag_chat.py`): semantic search over customer reviews using embeddings.
- Snowflake Cortex functions are not available on trial accounts, so the LLM calls go to the Gemini API from Python.

## How to Run

### 1. Snowflake
Run the scripts in `snowflake/` in order: warehouse, database and schemas, `DBT_ROLE`, storage integration, file formats, stage, RAW tables.

### 2. AWS
Create an IAM role with read access to the bucket, then set its trust policy using the values from `DESC INTEGRATION` in Snowflake (`STORAGE_AWS_IAM_USER_ARN`, `STORAGE_AWS_EXTERNAL_ID`).

### 3. Environment variables
```bash
cp airflow/.env.example airflow/.env
cp AI/.env.example AI/.env
# fill in your own values
```

### 4. dbt (local)
```bash
cd zomato
dbt build
dbt docs generate && dbt docs serve
```

### 5. Airflow
```bash
cd airflow
docker compose build
docker compose up airflow-init
docker compose up -d
```
Open the Airflow UI, unpause `zomato_batch` and trigger it.

### 6. Streamlit apps
```bash
cd AI
pip install -r requirements.txt
streamlit run text_to_sql.py
```

## Screenshots

| | |
|---|---|
| Airflow DAG run | `docs/images/airflow_dag.png` |
| dbt lineage graph | `docs/images/dbt_lineage.png` |
| Snowflake MARTS tables | `docs/images/snowflake_marts.png` |
| Text-to-SQL app | `docs/images/text_to_sql.png` |

## Security

No credentials are stored in this repository. Secrets are read from `.env` files (git-ignored) and referenced in dbt through `env_var(...)`.

## Dependencies

- `dbt-core`, `dbt-snowflake`
- `snowflake-connector-python`, `pandas`
- `streamlit`, `google-genai`, `python-dotenv`
- Docker Desktop (for Airflow)
