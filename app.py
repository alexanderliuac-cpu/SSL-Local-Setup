import os
import re
import json
import time
import socket
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path
from typing import Tuple, Dict, Any
import streamlit as st

# Optional dotenv support if installed locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =========================================================
# 1. HARDWARE & ENVIRONMENT AUTO-DETECTION
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
SSD_MARKER_DIR = BASE_DIR / "memory_store"
SANDBOX_DIR = (BASE_DIR / "safe_workspace").resolve()
MAX_WRITE_BYTES = 1024 * 500  # 500 KB hard cap

IS_EXTERNAL_SSD_MOUNTED = SSD_MARKER_DIR.exists() and SANDBOX_DIR.exists()

def is_local_gpu_server_online(host: str = "127.0.0.1", port: int = 8080) -> bool:
    """Fast 250ms socket check to see if the RTX 3050 Ti Llamafile/Ollama server is active."""
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False

LOCAL_GPU_ONLINE = is_local_gpu_server_online()


# =========================================================
# 2. DUAL-MODE MEMORY ENGINE (RAM ":memory:" vs External SSD)
# =========================================================
def get_db_connection() -> sqlite3.Connection:
    if IS_EXTERNAL_SSD_MOUNTED:
        db_path = SSD_MARKER_DIR / "local_memory.sqlite"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
    else:
        if "ram_db_conn" not in st.session_state:
            st.session_state.ram_db_conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn = st.session_state.ram_db_conn

    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(topic, fact, timestamp)"
    )
    return conn

def seed_default_memories():
    conn = get_db_connection()
    count = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    if count == 0:
        defaults = [
            ("hardware", "Primary laptop is a Surface Laptop Studio 1st Gen (i7-11370H, 16GB RAM, RTX 3050 Ti 4GB VRAM)."),
            ("storage", "Local models and databases run from an external SSD via a 40Gbps / 240W USB4 cable."),
            ("coding_style", "Prefer clean, modular Python with pathlib, type hints, and strict sandbox boundaries."),
        ]
        for topic, fact in defaults:
            conn.execute(
                "INSERT INTO memories (topic, fact, timestamp) VALUES (?, ?, datetime('now'))",
                (topic, fact),
            )
        conn.commit()

def save_memory(topic: str, fact: str):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO memories (topic, fact, timestamp) VALUES (?, ?, datetime('now'))",
        (topic.strip(), fact.strip()),
    )
    conn.commit()

def recall_memories(query: str = "", limit: int = 5) -> str:
    conn = get_db_connection()
    clean_q = re.sub(r"[^a-zA-Z0-9\s]", "", query).strip()
    rows = []
    if clean_q:
        fts_query = " OR ".join(clean_q.split()[:4])
        try:
            rows = conn.execute(
                "SELECT topic, fact, timestamp FROM memories WHERE memories MATCH ? LIMIT ?",
                (fts_query, limit),
            ).fetchall()
        except Exception:
            rows = []
    if not rows:
        rows = conn.execute(
            "SELECT topic, fact, timestamp FROM memories ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return "\n".join([f"- [{r[2]}] ({r[0]}): {r[1]}" for r in rows])

def export_memories_json() -> str:
    conn = get_db_connection()
    rows = conn.execute("SELECT topic, fact, timestamp FROM memories").fetchall()
    return json.dumps([{"topic": r[0], "fact": r[1], "timestamp": r[2]} for r in rows], indent=2)


# =========================================================
# 3. DUAL-MODE SANDBOXED FILE GUARD (Virtual RAM vs SSD)
# =========================================================
def init_virtual_sandbox():
    if "virtual_fs" not in st.session_state:
        st.session_state.virtual_fs = {
            "readme_sandbox.txt": "This is a virtual sandboxed file stored in RAM. Zero bytes written to internal disk."
        }

def _validate_relative_path(relative_path: str) -> str:
    norm = Path(relative_path)
    if norm.is_absolute() or ".." in norm.parts:
        raise PermissionError("Access denied: Directory traversal ('..') or absolute paths are blocked.")
    return str(norm.as_posix())

def safe_read_file(relative_path: str) -> str:
    clean_rel = _validate_relative_path(relative_path)
    if IS_EXTERNAL_SSD_MOUNTED:
        target = (SANDBOX_DIR / clean_rel).resolve()
        if not target.is_relative_to(SANDBOX_DIR):
            raise PermissionError("Access denied: Path escapes safe_workspace.")
        return target.read_text(encoding="utf-8") if target.exists() else "Error: File not found."
    else:
        init_virtual_sandbox()
        return st.session_state.virtual_fs.get(clean_rel, "Error: File not found in virtual RAM sandbox.")

def safe_write_file(relative_path: str, content: str, read_only_lock: bool = False) -> str:
    if read_only_lock:
        raise PermissionError("Write blocked: Strict Read-Only Lock Mode is enabled.")
    clean_rel = _validate_relative_path(relative_path)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise ValueError(f"Write rejected: {len(encoded)} bytes exceeds the 500 KB cap.")

    if IS_EXTERNAL_SSD_MOUNTED:
        target = (SANDBOX_DIR / clean_rel).resolve()
        if not target.is_relative_to(SANDBOX_DIR):
            raise PermissionError("Access denied: Path escapes safe_workspace.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        return f"Saved {len(encoded)} bytes to External SSD: safe_workspace/{clean_rel}"
    else:
        init_virtual_sandbox()
        st.session_state.virtual_fs[clean_rel] = content
        return f"Saved {len(encoded)} bytes to Virtual RAM Sandbox: {clean_rel} (0 disk writes)"


# =========================================================
# 4. ZERO-DEPENDENCY UNIFIED ROUTER (No LiteLLM Deadlock)
# =========================================================
MODEL_CONFIGS = {
    "local": {
        "model": "local-model" if LOCAL_GPU_ONLINE else "gemini-2.5-flash",
        "label": "Local RTX 3050 Ti (Llama-3.2-3B)" if LOCAL_GPU_ONLINE else "Simulated Local Tier (Gemini 2.5 Flash)",
    },
    "gemini": {
        "model": "gemini-2.5-pro",
        "label": "Google Gemini 2.5 Pro",
    },
    "chatgpt": {
        "model": "gpt-4o",
        "label": "OpenAI GPT-4o",
    },
    "claude": {
        "model": "claude-3-7-sonnet-latest",
        "label": "Anthropic Claude 3.7 Sonnet",
    },
}

SIMPLE_PATTERNS = [
    r"^(format|clean up|fix typo|rephrase|summarize|translate|convert)\b",
    r"\b(json|csv|yaml|markdown|bullet points)\b",
]
COMPLEX_CODE_PATTERNS = [
    r"\b(python|script|refactor|debug|sql|architecture|class|function|api)\b",
]
COMPLEX_REASONING_PATTERNS = [
    r"\b(compare|trade-offs|analyze|strategy|thermodynamic|electrolyzer|financial|proof)\b",
]

def route_query(prompt: str) -> Tuple[str, str]:
    clean = prompt.strip().lower()
    words = len(clean.split())

    if words < 22 and any(re.search(p, clean) for p in SIMPLE_PATTERNS):
        return "local", "Tier 1 Heuristic: Short formatting/summary task -> Local Fast Tier"
    if any(re.search(p, clean) for p in COMPLEX_CODE_PATTERNS):
        return "claude", "Tier 1 Heuristic: Code/architecture keywords -> Claude 3.7 Sonnet"
    if any(re.search(p, clean) for p in COMPLEX_REASONING_PATTERNS) or words > 45:
        return "gemini", "Tier 1 Heuristic: Deep analytical query -> Gemini 2.5 Pro"
    if words < 12:
        return "local", "Tier 2 Length Check: Concise general prompt -> Local Fast Tier"
    return "chatgpt", "Tier 2 General Complex Route -> OpenAI GPT-4o"

def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def execute_llm_call(prompt: str, provider_key: str, local_context: str) -> Tuple[str, Dict[str, Any]]:
    start_t = time.perf_counter()
    target = MODEL_CONFIGS.get(provider_key, MODEL_CONFIGS["gemini"])
    model_name = target["model"]

    system_msg = (
        "You are a local-first hybrid AI assistant configured for a Surface Laptop Studio. "
        "Personalize responses using these stored local memories when relevant:\n"
        f"{local_context}"
    )

    try:
        # 1. Local Llamafile / Ollama Server (OpenAI-compatible endpoint on port 8080)
        if provider_key == "local" and LOCAL_GPU_ONLINE:
            res = _post_json(
                "http://127.0.0.1:8080/v1/chat/completions",
                {"Content-Type": "application/json"},
                {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            reply = res["choices"][0]["message"]["content"]

        # 2. Google Gemini API (or Simulated Local Flash Tier when outside)
        elif provider_key in ("gemini", "local"):
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            if not api_key:
                raise ValueError("GEMINI_API_KEY is not set.")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            res = _post_json(
                url,
                {"Content-Type": "application/json"},
                {
                    "system_instruction": {"parts": [{"text": system_msg}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                },
            )
            reply = res["candidates"][0]["content"]["parts"][0]["text"]

        # 3. OpenAI ChatGPT API
        elif provider_key == "chatgpt":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise ValueError("OPENAI_API_KEY is not set.")
            res = _post_json(
                "https://api.openai.com/v1/chat/completions",
                {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            reply = res["choices"][0]["message"]["content"]

        # 4. Anthropic Claude API
        elif provider_key == "claude":
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY is not set.")
            res = _post_json(
                "https://api.anthropic.com/v1/messages",
                {
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                {
                    "model": model_name,
                    "max_tokens": 1024,
                    "system": system_msg,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            reply = res["content"][0]["text"]

        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        return reply, {
            "provider": provider_key,
            "model_label": target["label"],
            "latency_ms": elapsed_ms,
            "status": "Success",
        }

    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        return (
            f"**[Demo Simulation Mode — Ready for Live Key]**\n\n"
            f"- **Routed Target:** `{target['label']}` (`{model_name}`)\n"
            f"- **Injected Local Memories:**\n{local_context}\n"
            f"- **Note:** Add your `{provider_key.upper()}` API key in the sidebar (or start Llamafile on port 8080 when on your Surface) to run live inference.\n"
            f"- **Diagnostic:** `{str(e)[:180]}`",
            {
                "provider": provider_key,
                "model_label": f"{target['label']} (Simulated)",
                "latency_ms": elapsed_ms,
                "status": "Simulated",
            },
        )


# =========================================================
# 5. STREAMLIT UI & DEMONSTRATION DASHBOARD
# =========================================================
st.set_page_config(page_title="Surface Hybrid AI Hub", layout="wide")
seed_default_memories()
init_virtual_sandbox()

st.title("Surface Laptop Studio — Hybrid Local AI Hub")
col1, col2, col3 = st.columns(3)
with col1:
    st.metric(
        "Storage & Memory Mode",
        "External SSD (NTFS)" if IS_EXTERNAL_SSD_MOUNTED else "RAM-Only Mode (Outside)",
        "Persistent on D:\\" if IS_EXTERNAL_SSD_MOUNTED else "Zero C:\\ Disk Writes",
    )
with col2:
    st.metric(
        "RTX 3050 Ti Local Server",
        "Online (:8080)" if LOCAL_GPU_ONLINE else "Offline (Cloud Sim)",
        "Llamafile mmap active" if LOCAL_GPU_ONLINE else "Using Flash fallback",
    )
with col3:
    st.metric("Sandbox File Guard", "Path-Jailed", "500 KB Max Write Cap")

st.divider()

with st.sidebar:
    st.header("1. Routing & Security")
    routing_mode = st.selectbox(
        "Provider Selection",
        ["Auto-Route (2-Tier Smart Router)", "local", "gemini", "chatgpt", "claude"],
    )
    read_only_lock = st.toggle("Strict Read-Only Lock Mode", value=False)

    with st.expander("API Keys (Session-Only RAM)", expanded=False):
        st.caption("Keys stay in RAM for this session and are never written to disk.")
        g_key = st.text_input("GEMINI_API_KEY", value=os.getenv("GEMINI_API_KEY", ""), type="password")
        o_key = st.text_input("OPENAI_API_KEY", value=os.getenv("OPENAI_API_KEY", ""), type="password")
        a_key = st.text_input("ANTHROPIC_API_KEY", value=os.getenv("ANTHROPIC_API_KEY", ""), type="password")
        if g_key: os.environ["GEMINI_API_KEY"] = g_key
        if o_key: os.environ["OPENAI_API_KEY"] = o_key
        if a_key: os.environ["ANTHROPIC_API_KEY"] = a_key

    st.divider()
    st.header("2. Local Memory Store")
    with st.form("add_memory_form", clear_on_submit=True):
        m_topic = st.text_input("Topic", placeholder="e.g., project_spec")
        m_fact = st.text_area("Fact to Remember", placeholder="e.g., Target stack uses Python and Streamlit.")
        if st.form_submit_button("Save Memory") and m_topic and m_fact:
            save_memory(m_topic, m_fact)
            st.success("Memory stored!")

    st.caption("**Active Local Memories:**")
    st.code(recall_memories(limit=6), language="markdown")

    st.download_button(
        "Download Memory Snapshot (.json)",
        data=export_memories_json(),
        file_name="local_memory_snapshot.json",
        mime="application/json",
    )

    st.divider()
    st.header("3. Sandboxed File Guard")
    s_file = st.text_input("Relative File Path", value="notes/demo.txt")
    s_content = st.text_area("File Content", value="Testing sandboxed write protection.")
    c_read, c_write = st.columns(2)
    if c_write.button("Test Safe Write"):
        try:
            msg = safe_write_file(s_file, s_content, read_only_lock=read_only_lock)
            st.success(msg)
        except Exception as err:
            st.error(str(err))
    if c_read.button("Test Safe Read"):
        try:
            st.info(safe_read_file(s_file))
        except Exception as err:
            st.error(str(err))

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

for item in st.session_state.chat_history:
    with st.chat_message(item["role"]):
        if "meta" in item:
            st.caption(
                f"**Route:** `{item['meta']['model_label']}` | "
                f"**Reason:** {item['meta']['reason']} | "
                f"**Latency:** `{item['meta']['latency_ms']} ms`"
            )
        st.markdown(item["content"])

if user_prompt := st.chat_input("Test a prompt (e.g., 'Format this list as JSON' vs 'Architect a Python telemetry pipeline')..."):
    st.session_state.chat_history.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.markdown(user_prompt)

    if routing_mode == "Auto-Route (2-Tier Smart Router)":
        chosen_provider, reason = route_query(user_prompt)
    else:
        chosen_provider = routing_mode
        reason = f"Manual override -> {routing_mode.upper()}"

    local_ctx = recall_memories(user_prompt, limit=4)

    with st.chat_message("assistant"):
        reply_text, telemetry = execute_llm_call(user_prompt, chosen_provider, local_ctx)
        telemetry["reason"] = reason
        st.caption(
            f"**Route:** `{telemetry['model_label']}` | "
            f"**Reason:** {telemetry['reason']} | "
            f"**Latency:** `{telemetry['latency_ms']} ms`"
        )
        st.markdown(reply_text)
        st.session_state.chat_history.append(
            {"role": "assistant", "content": reply_text, "meta": telemetry}
        )
