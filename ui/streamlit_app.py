"""Minimal Streamlit chat UI for trying Betty locally against the FastAPI server."""

import os
from typing import Any

import httpx
import streamlit as st

DEFAULT_API_URL = os.getenv("BETTY_API_URL", "http://localhost:8000")
GREETING = (
    "Hi, I'm Betty, your local guide! Tell me where you'd like to go "
    "and I'll help shape the perfect trip."
)

st.set_page_config(page_title="Betty Trip Planner", page_icon="🧭")
st.title("🧭 Betty, your local guide")

api_url: str = st.sidebar.text_input("API URL", DEFAULT_API_URL).rstrip("/")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "summary" not in st.session_state:
    st.session_state.summary = None
if "trip_context" not in st.session_state:
    st.session_state.trip_context = None


def post(path: str) -> dict[str, Any] | None:
    """POST the conversation to the backend; show an error and return None on failure."""
    body: dict[str, Any] = {"messages": st.session_state.messages}
    if path.endswith("/summarize") and st.session_state.trip_context:
        body["trip_context"] = st.session_state.trip_context
    try:
        response = httpx.post(f"{api_url}{path}", json=body, timeout=90)
        response.raise_for_status()
        return dict(response.json())
    except httpx.HTTPStatusError as exc:
        st.error(f"API error {exc.response.status_code}: {exc.response.text}")
    except httpx.HTTPError as exc:
        st.error(f"Could not reach the API at {api_url}: {exc}")
    return None


if st.sidebar.button("Summarize trip", disabled=not st.session_state.messages):
    with st.spinner("Betty is writing the summary..."):
        data = post("/api/v1/summarize")
    st.session_state.summary = data["summary"] if data else None

if st.sidebar.button("Reset conversation"):
    st.session_state.messages = []
    st.session_state.summary = None
    st.session_state.trip_context = None
    st.rerun()

with st.chat_message("assistant"):
    st.write(GREETING)
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])

if prompt := st.chat_input("Where would you like to go?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state.summary = None
    with st.chat_message("user"):
        st.write(prompt)
    with st.chat_message("assistant"), st.spinner("Betty is typing..."):
        data = post("/api/v1/chat")
        if data:
            st.write(data["reply"])
            if data.get("ready"):
                st.session_state.trip_context = data["trip_context"]
            st.session_state.messages.append({"role": "assistant", "content": data["reply"]})
        else:
            st.session_state.messages.pop()  # drop the unanswered turn so the user can retry

if st.session_state.trip_context:
    st.divider()
    st.success("✅ Ready to plan: Betty has enough information.")
    with st.expander("Collected trip context"):
        st.json(st.session_state.trip_context)

if st.session_state.summary:
    st.divider()
    st.subheader("Trip summary")
    st.info(st.session_state.summary)
