import os
import re
import json
import socket
import pandas as pd
import streamlit as st
import snowflake.connector
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Is network par IPv6 kaam nahi karta, isliye sirf IPv4 use karo
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_ROWS = 1000

FORBIDDEN_WORDS = [
    "drop", "delete", "truncate", "alter", "update", "insert", "create",
    "replace", "grant", "revoke", "merge", "call", "copy",
]

EXAMPLE_QUESTIONS = [
    "Top 10 cities by GMV",
    "Which cuisine has the most orders?",
    "Average delivery time by city, worst first",
    "Cancel rate by payment method",
]

NOTES = "Note: gmv means delivered revenue. Prefer the MART_ tables when they fit the question."

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(timeout=60000),  # milliseconds
)


@st.cache_resource
def get_connection():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.getenv("SNOWFLAKE_ROLE", "DBT_ROLE"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "ZOMATO_WH"),
        database=os.getenv("SNOWFLAKE_DATABASE", "ZOMATO"),
        schema="MARTS",
    )


@st.cache_data(ttl=3600, show_spinner="Tables ki list padh rahe hain...")
def load_schema():
    conn = get_connection()
    df = conn.cursor().execute("""
        SELECT table_name, column_name, data_type
        FROM ZOMATO.INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema = 'MARTS'
        ORDER BY table_name, ordinal_position
    """).fetch_pandas_all()

    if df.empty:
        raise RuntimeError("ZOMATO.MARTS me koi table nahi mili. Pehle dbt build chalao.")

    df.columns = [c.lower() for c in df.columns]
    lines = []
    for table, group in df.groupby("table_name", sort=True):
        cols = ", ".join(f"{r.column_name} {r.data_type}" for r in group.itertuples())
        lines.append(f"{table}({cols})")
    return "\n".join(lines)


def build_system_prompt(schema_text):
    return f"""
You are a Snowflake SQL expert. Write ONE SELECT query that answers the question.

Rules:
- SELECT queries only, never modify data.
- Use bare table names (FCT_ORDERS, not ZOMATO.MARTS.FCT_ORDERS).
- Use only the tables and columns listed below.
- Add a LIMIT of 100 or less, unless the question asks for a single total.
- Reply as JSON in this exact format: {{"sql": "your query here"}}

Tables available (Snowflake):
{schema_text}

{NOTES}
"""


def generate_sql(question, system_prompt):
    response = client.models.generate_content(
        model=MODEL,
        contents=question,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    sql = json.loads(response.text)["sql"]
    sql = sql.replace("ZOMATO.MARTS.", "").replace("ZOMATO.", "")
    return sql.strip().rstrip(";")


def is_safe(sql):
    lowered = sql.strip().lower()

    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False

    if ";" in lowered:
        return False

    for word in FORBIDDEN_WORDS:
        if re.search(rf"\b{word}\b", lowered):
            return False

    return True


def run_query(sql):
    conn = get_connection()
    cursor = conn.cursor()
    df = cursor.execute(sql).fetch_pandas_all()
    return df.head(MAX_ROWS)


st.title("Chat with your Zomato Data")
st.caption(f"Ask in English, {MODEL} writes the SQL, Snowflake runs it")

with st.sidebar:
    st.header("Example Questions")
    for q in EXAMPLE_QUESTIONS:
        st.markdown(f" - {q}")

try:
    schema_text = load_schema()
except Exception as e:
    st.error(f"Snowflake se tables ki list nahi mili: {e}")
    st.stop()

with st.expander("Tables jo app dekh raha hai"):
    st.text(schema_text)

system_prompt = build_system_prompt(schema_text)

question = st.text_input(
    "Enter your question here",
    placeholder="e.g. Top 10 restaurants by revenue in Bangalore",
)

if question:
    try:
        sql = generate_sql(question, system_prompt)
    except Exception as e:
        st.error(f"Gemini se SQL nahi ban paya: {e}")
        st.stop()

    st.code(sql, language="sql")

    if not is_safe(sql):
        st.error("The generated SQL is not safe to run. Please modify your question.")
    else:
        try:
            df = run_query(sql)
            st.success(f"{len(df)} rows returned")
            st.dataframe(df, hide_index=True)

            if len(df.columns) == 2 and pd.api.types.is_numeric_dtype(df.iloc[:, 1]):
                st.bar_chart(df, x=df.columns[0], y=df.columns[1])

        except Exception as e:
            st.error(f"Error running query: {e}")