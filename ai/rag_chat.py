import os
import time
import socket
import numpy as np
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

EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
CHAT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
NUM_REVIEWS = 500
TOP_K = 5
EMBED_BATCH = 50
CACHE_FILE = "review_embeddings_gemini.parquet"

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(timeout=60000),  # milliseconds
)


def read_reviews_from_snowflake():
    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.getenv("SNOWFLAKE_ROLE", "DBT_ROLE"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "ZOMATO_WH"),
        database=os.getenv("SNOWFLAKE_DATABASE", "ZOMATO"),
    )
    try:
        query = f"""
            SELECT REVIEW_ID, CITY, RATING, COMMENT
            FROM ZOMATO.STAGING.STG_REVIEWS
            WHERE COMMENT IS NOT NULL AND TRIM(COMMENT) <> ''
            ORDER BY RANDOM()
            LIMIT {NUM_REVIEWS}
        """
        df = conn.cursor().execute(query).fetch_pandas_all()
    finally:
        conn.close()

    df.columns = [col.lower() for col in df.columns]
    df["comment"] = df["comment"].astype(str)
    return df


def embed(texts, task_type):
    """Texts ko batch me embed karo. task_type: RETRIEVAL_DOCUMENT ya RETRIEVAL_QUERY."""
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        for attempt in range(4):
            try:
                result = client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=batch,
                    config=types.EmbedContentConfig(task_type=task_type),
                )
                vectors.extend(e.values for e in result.embeddings)
                break
            except Exception as e:
                print(f"embed attempt {attempt + 1} failed: {e}")
                if attempt == 3:
                    raise
                time.sleep(20 * (attempt + 1))
        if start + EMBED_BATCH < len(texts):
            time.sleep(3)  # free tier ki per-minute limit ke liye
    return vectors


@st.cache_data(show_spinner="Reviews load aur embed ho rahe hain (pehli baar kuch minute lagte hain)...")
def load_reviews():
    if os.path.exists(CACHE_FILE):
        return pd.read_parquet(CACHE_FILE)

    df = read_reviews_from_snowflake()
    df["embedding"] = embed(df["comment"].tolist(), "RETRIEVAL_DOCUMENT")
    df.to_parquet(CACHE_FILE)
    return df


def find_similar_reviews(question, df):
    question_vector = np.array(embed([question], "RETRIEVAL_QUERY")[0])
    matrix = np.vstack(df["embedding"].to_numpy())

    scores = matrix @ question_vector / (
        np.linalg.norm(matrix, axis=1) * np.linalg.norm(question_vector)
    )

    df = df.copy()
    df["score"] = scores
    return df.nlargest(TOP_K, "score")


def ask_llm(question, top_reviews):
    context = ""
    for _, row in top_reviews.iterrows():
        context += f"({row['city']}, {row['rating']} stars) {row['comment']}\n"

    system_prompt = (
        "Answer ONLY using the customer reviews provided. "
        "Be concise. If the reviews don't cover it, say so."
    )
    user_prompt = f"Question: {question}\n\nReviews:\n{context}"

    response = client.models.generate_content(
        model=CHAT_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
        ),
    )
    return response.text


st.title("Chat with your Zomato Reviews")
st.caption(f"Searching {NUM_REVIEWS} reviews, answering with {CHAT_MODEL}")

review_df = load_reviews()

question = st.text_input(
    "Ask a question about your reviews:",
    placeholder="e.g. What are the most common complaints about delivery?",
)

if question:
    try:
        top_reviews = find_similar_reviews(question, review_df)
        answer = ask_llm(question, top_reviews)
    except Exception as e:
        st.error(f"Gemini se jawab nahi aaya: {e}")
    else:
        st.markdown("**Answer:**")
        st.write(answer)

        with st.expander("Reviews used to build this answer"):
            st.dataframe(top_reviews[["city", "rating", "comment"]], hide_index=True)