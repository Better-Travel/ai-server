"""Streamlit UI for trying Betty locally: chat on the left, function debugger on the right."""

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
CHAT_HEIGHT = 560
# Debug functions: key -> (title, endpoint)
FUNCTIONS = {
    "summarize": ("Summarize", "/api/v1/summarize"),
    "check": ("Readiness check", "/api/v1/chat"),
}

st.set_page_config(
    page_title="Betty Trip Planner",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="collapsed",
)

api_url: str = st.sidebar.text_input("API URL", DEFAULT_API_URL).rstrip("/")

for key, default in (
    ("messages", []),
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


def history_request_body(tool: str) -> dict[str, Any]:
    """The current chat in the exact shape the function's endpoint expects."""
    messages = list(st.session_state.messages)
    body: dict[str, Any] = {"messages": messages}
    if tool == "summarize":
        if st.session_state.trip_context:
            body["trip_context"] = st.session_state.trip_context
    else:
        # /chat needs the last message to come from the user, so re-check the latest user turn.
        while messages and messages[-1]["role"] == "assistant":
            messages.pop()
    return body


def parse_input(text: str) -> dict[str, Any] | None:
    """Accept request JSON, a bare message list, or plain text (sent as one user message)."""
    text = text.strip()
    if not text:
        st.warning("Input is empty.")
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"messages": [{"role": "user", "content": text}]}
    if isinstance(parsed, list):
        return {"messages": parsed}
    if isinstance(parsed, dict):
        return parsed
    return {"messages": [{"role": "user", "content": text}]}


def copy_button(text: str) -> None:
    """Small button that copies `text` to the clipboard (Streamlit has no native one)."""
    # The history contains user/LLM text and the iframe has same-origin access, so escape
    # everything that could terminate the script or open a tag.
    payload = (
        json.dumps(text).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    st.iframe(
        f"""
        <button id="b" title="Copy chat history as JSON" style="cursor:pointer;
          border:1px solid rgba(128,128,128,.5); background:transparent; color:#888;
          border-radius:6px; padding:3px 10px; font:13px sans-serif;">📋 Copy</button>
        <script>
        const text = {payload};
        const b = document.getElementById("b");
        b.onclick = async () => {{
          try {{ await navigator.clipboard.writeText(text); }}
          catch (e) {{
            const t = document.createElement("textarea"); t.value = text;
            document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove();
          }}
          b.textContent = "✓ Copied";
          setTimeout(() => (b.textContent = "📋 Copy"), 1500);
        }};
        </script>
        """,
        height=40,
    )


def run_function(tool: str, body: dict[str, Any] | None) -> None:
    """Call the function's endpoint and remember request + response for display."""
    if body is None:
        return
    _, path = FUNCTIONS[tool]
    with st.spinner(f"Calling {path}..."):
        response = post(path, body)
    st.session_state.debug_output[tool] = {"request": body, "response": response}


def render_output(tool: str) -> None:
    result = st.session_state.debug_output.get(tool)
    if result is None or result["response"] is None:
        return
    response = result["response"]
    if tool == "summarize":
        st.info(response.get("summary", ""))
    else:
        if response.get("ready"):
            st.success("ready = true: enough information gathered")
        else:
            st.warning("ready = false: Betty needs more information")
        st.write(f"**Betty's reply:** {response.get('reply', '')}")
    with st.expander("Request sent"):
        st.json(result["request"])
    with st.expander("Raw response"):
        st.json(response)


if st.sidebar.button("Reset conversation"):
    st.session_state.messages = []
    st.session_state.trip_context = None
    st.session_state.debug_output = {}
    st.rerun()

left, right = st.columns(2, gap="large")

# ---- left: chat
with left:
    header = st.container()
    messages_box = st.container(height=CHAT_HEIGHT)
    status_box = st.container()
    prompt = st.chat_input("Where would you like to go?")

with messages_box:
    with st.chat_message("assistant"):
        st.write(GREETING)
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with messages_box:
        with st.chat_message("user"):
            st.write(prompt)
        with st.chat_message("assistant"), st.spinner("Betty is typing..."):
            data = post("/api/v1/chat", {"messages": st.session_state.messages})
    if data:
        if data.get("ready"):
            st.session_state.trip_context = data["trip_context"]
        st.session_state.messages.append({"role": "assistant", "content": data["reply"]})
        st.rerun()  # redraw so the header's copy button and the right side see the new turn
    st.session_state.messages.pop()  # drop the unanswered turn so the user can retry

with header:
    title_col, copy_col = st.columns([1, 2.5], vertical_alignment="center")
    title_col.subheader("🧭 Betty")
    with copy_col:
        copy_button(json.dumps(st.session_state.messages, indent=2, ensure_ascii=False))

with status_box:
    if st.session_state.trip_context:
        st.success("✅ Ready to plan: Betty has enough information.")
        with st.expander("Collected trip context"):
            st.json(st.session_state.trip_context)

# ---- right: functions
with right:
    st.subheader("🔧 Functions")
    st.caption(
        'Input: request JSON `{"messages": [{"role": "user"|"assistant", '
        '"content": "..."}]}`, or plain text (sent as one user message). '
        "The readiness check calls `/api/v1/chat`; with chat history it uses the latest user turn."
    )
    columns = st.columns(len(FUNCTIONS), gap="medium")
    for column, (tool, (title, path)) in zip(columns, FUNCTIONS.items(), strict=True):
        with column:
            st.markdown(f"**{title}**  \n`{path}`")
            text = st.text_area(
                f"Input for {title}", key=f"input_{tool}", height=200, label_visibility="collapsed"
            )
            if st.button("Run with input", key=f"run_input_{tool}", width="stretch"):
                run_function(tool, parse_input(text))
            if st.button(
                "Run with chat history",
                key=f"run_history_{tool}",
                type="primary",
                width="stretch",
                disabled=not st.session_state.messages,
            ):
                run_function(tool, history_request_body(tool))
            render_output(tool)
