"""Streamlit chat UI for trying Betty locally against the FastAPI server, with debug tools."""

import json
import os
from typing import Any

import httpx
import streamlit as st

DEFAULT_API_URL = os.getenv("BETTY_API_URL", "http://localhost:8000")
GREETING = (
    "Hi, I'm Betty, your local guide! Tell me where you'd like to go "
    "and I'll help shape the perfect trip."
)
# Debug tools: name -> (endpoint, label of the run button)
DEBUG_TOOLS = {
    "summarize": ("/api/v1/summarize", "Run summarize"),
    "check": ("/api/v1/chat", "Run readiness check"),
}

st.set_page_config(page_title="Betty Trip Planner", page_icon="🧭", layout="wide")
st.title("🧭 Betty, your local guide")

api_url: str = st.sidebar.text_input("API URL", DEFAULT_API_URL).rstrip("/")

for key, default in (
    ("messages", []),
    ("summary", None),
    ("trip_context", None),
    ("debug_output", {}),
):
    if key not in st.session_state:
        st.session_state[key] = default


def post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """POST `body` to the backend; show an error and return None on failure."""
    try:
        response = httpx.post(f"{api_url}{path}", json=body, timeout=120)
        response.raise_for_status()
        return dict(response.json())
    except httpx.HTTPStatusError as exc:
        st.error(f"API error {exc.response.status_code}: {exc.response.text}")
    except httpx.HTTPError as exc:
        st.error(f"Could not reach the API at {api_url}: {exc}")
    return None


def current_request_body() -> dict[str, Any]:
    """The current chat in the exact shape the API expects."""
    body: dict[str, Any] = {"messages": st.session_state.messages}
    if st.session_state.trip_context:
        body["trip_context"] = st.session_state.trip_context
    return body


def load_current_chat(tool: str) -> None:
    """Button callback: fill a debug input with the current chat."""
    st.session_state[f"debug_input_{tool}"] = json.dumps(
        current_request_body(), indent=2, ensure_ascii=False
    )


def parse_debug_input(text: str) -> dict[str, Any] | None:
    """Accept request JSON, or plain text which is sent as a single user message."""
    text = text.strip()
    if not text:
        st.warning("Input is empty.")
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"messages": [{"role": "user", "content": text}]}
    if isinstance(parsed, list):  # a bare list of messages
        return {"messages": parsed}
    if isinstance(parsed, dict):
        return parsed
    return {"messages": [{"role": "user", "content": text}]}


# ---- sidebar actions
if st.sidebar.button("Summarize trip", disabled=not st.session_state.messages):
    with st.spinner("Betty is writing the summary..."):
        data = post("/api/v1/summarize", current_request_body())
    st.session_state.summary = data["summary"] if data else None

if st.sidebar.button("Reset conversation"):
    st.session_state.messages = []
    st.session_state.summary = None
    st.session_state.trip_context = None
    st.rerun()

# ---- chat
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
        data = post("/api/v1/chat", {"messages": st.session_state.messages})
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

# ---- debug tools
st.divider()
st.subheader("🔧 Debug tools")

history_json = json.dumps(st.session_state.messages, indent=2, ensure_ascii=False)
with st.expander("📋 Chat history (use the copy icon at the top right of the box)"):
    st.code(history_json, language="json")

st.caption(
    'Each tool takes request JSON (`{"messages": [{"role": "user"|"assistant", '
    '"content": "..."}], "trip_context": {...}}`). Plain text is sent as one user message. '
    "The readiness check calls `/api/v1/chat`, so the last message must come from the user."
)
tabs = st.tabs(["Summarize", "Readiness check"])
for tab, (tool, (path, run_label)) in zip(tabs, DEBUG_TOOLS.items(), strict=True):
    with tab:
        st.text_area(
            f"Input for {path}", key=f"debug_input_{tool}", height=220, placeholder="Paste or type"
        )
        run_col, load_col = st.columns(2)
        run = run_col.button(run_label, key=f"run_{tool}", type="primary")
        load_col.button(
            "Load current chat", key=f"load_{tool}", on_click=load_current_chat, args=(tool,)
        )
        if run:
            body = parse_debug_input(st.session_state[f"debug_input_{tool}"])
            if body is not None:
                with st.spinner(f"Calling {path}..."):
                    st.session_state.debug_output[tool] = post(path, body)
        output = st.session_state.debug_output.get(tool)
        if output is not None:
            if tool == "summarize":
                st.info(output.get("summary", ""))
            else:
                if output.get("ready"):
                    st.success("ready = true: enough information gathered")
                else:
                    st.warning("ready = false: Betty needs more information")
                st.write(f"**Betty's reply:** {output.get('reply', '')}")
            with st.expander("Raw response"):
                st.json(output)
