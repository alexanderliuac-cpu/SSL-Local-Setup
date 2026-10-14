import os
import re
import json
import time
import socket
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path
from typing import Tuple, Dict, Any, List
import streamlit as st

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
MAX_AGENT_STEPS = 4

IS_EXTERNAL_SSD_MOUNTED = SSD_MARKER_DIR.exists() and SANDBOX_DIR.exists()

def is_local_gpu_server_online(host: str = "127.0.0.1", port: int = 8080) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False

LOCAL_GPU_ONLINE = is_local_gpu_server_online()

def get_api_key(key_name: str) -> str:
    val = os.getenv(key_name, "").strip()
    if val:
        return val
    try:
        if key_name in st.secrets:
            return str(st.secrets[key_name]).strip()
    except Exception:
        pass
    return ""


# =========================================================
# 2. DUAL-MODE MEMORY ENGINE (Stop-Word Filtered FTS5)
# =========================================================
STOP_WORDS = {
    "a", "an", "the", "to", "me", "in", "on", "of", "for", "and", "or", "is",
    "are", "was", "were", "be", "been", "how", "what", "why", "where", "when",
    "who", "which", "this", "that", "these", "those", "it", "its", "with", "as",
    "by", "at", "from", "up", "about", "into", "over", "after", "can", "could",
    "would", "should", "will", "do", "does", "did", "please", "write", "build",
    "make", "create", "update", "show", "tell", "give", "test", "my", "your", "i"
}

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
            ("coding_style", "Prefer clean, modular Python with pathlib, type hints, and strict sandbox boundaries."),
            ("storage", "Local models and databases run from an external SSD via a 40Gbps / 240W USB4 cable."),
            ("hardware", "Primary laptop is a Surface Laptop Studio 1st Gen (i7-11370H, 16GB RAM, RTX 3050 Ti 4GB VRAM)."),
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

def recall_memories(query: str = "", provider_hint: str = "", limit: int = 5) -> str:
    conn = get_db_connection()
    raw_words = re.findall(r"[a-zA-Z0-9_]+", query.lower())
    keywords = [w for w in raw_words if len(w) > 2 and w not in STOP_WORDS]

    if provider_hint == "claude" or any(w in raw_words for w in ("code", "app", "python", "script", "word", "excel")):
        if "coding_style" not in keywords:
            keywords.insert(0, "coding_style")

    rows = []
    if keywords:
        fts_query = " OR ".join(f"{kw}*" for kw in keywords[:5])
        try:
            rows = conn.execute(
                "SELECT topic, fact, timestamp FROM memories WHERE memories MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit),
            ).fetchall()
        except Exception:
            rows = []

    if not rows:
        rows = conn.execute(
            "SELECT topic, fact, timestamp FROM memories ORDER BY rowid ASC LIMIT ?",
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
            "readme_sandbox.txt": (
                "Workspace Policy: All generated scripts must use pathlib.Path, include type hints, "
                "and remain strictly inside safe_workspace."
            )
        }

def list_sandbox_files() -> List[str]:
    if IS_EXTERNAL_SSD_MOUNTED:
        return [str(p.relative_to(SANDBOX_DIR).as_posix()) for p in SANDBOX_DIR.rglob("*") if p.is_file()]
    init_virtual_sandbox()
    return sorted(list(st.session_state.virtual_fs.keys()))

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
        return target.read_text(encoding="utf-8") if target.exists() else f"Error: '{clean_rel}' not found."
    else:
        init_virtual_sandbox()
        return st.session_state.virtual_fs.get(clean_rel, f"Error: '{clean_rel}' not found in virtual RAM sandbox.")

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
# 4. ROUTER & ZERO-DEPENDENCY API DISPATCHER
# =========================================================
MODEL_CONFIGS = {
    "local": {
        "model": "local-model" if LOCAL_GPU_ONLINE else "gemini-2.5-flash",
        "label": "Local RTX 3050 Ti (Llama-3.2-3B)" if LOCAL_GPU_ONLINE else "Simulated Local Tier (Gemini 2.5 Flash)",
        "required_key": "NONE" if LOCAL_GPU_ONLINE else "GEMINI_API_KEY",
    },
    "gemini": {
        "model": "gemini-2.5-pro",
        "label": "Google Gemini 2.5 Pro",
        "required_key": "GEMINI_API_KEY",
    },
    "chatgpt": {
        "model": "gpt-4o",
        "label": "OpenAI GPT-4o",
        "required_key": "OPENAI_API_KEY",
    },
    "claude": {
        "model": "claude-3-7-sonnet-latest",
        "label": "Anthropic Claude 3.7 Sonnet",
        "required_key": "ANTHROPIC_API_KEY",
    },
}

SIMPLE_PATTERNS = [
    r"^(format|clean up|fix typo|rephrase|summarize|translate|convert)\b",
    r"\b(json|csv|yaml|markdown|bullet points|weather|hello|hi|time)\b",
]
COMPLEX_CODE_PATTERNS = [
    r"\b(code|coding|python|script|app|application|program|refactor|debug|sql|architecture|class|function|api|automate|microsoft word|docx|excel|streamlit)\b",
    r"\b(build|create|develop|write)\b.+\b(app|tool|script|code|bot|pipeline|dashboard|document|word)\b",
]
COMPLEX_REASONING_PATTERNS = [
    r"\b(compare|trade-offs|analyze|strategy|thermodynamic|electrolyzer|financial|proof|architecture|design|evaluate)\b",
]

def route_query(prompt: str) -> Tuple[str, str]:
    clean = prompt.strip().lower()
    words = len(clean.split())

    if any(re.search(p, clean) for p in COMPLEX_CODE_PATTERNS):
        return "claude", "Tier 1 Code & Build Intent -> Routed to Claude 3.7 Sonnet"
    if any(re.search(p, clean) for p in COMPLEX_REASONING_PATTERNS) or words > 40:
        return "gemini", "Tier 1 Deep Analytical / Multi-Constraint -> Routed to Gemini 2.5 Pro"
    if words < 20 and any(re.search(p, clean) for p in SIMPLE_PATTERNS):
        return "local", "Tier 1 Fast Utility / Everyday Query -> Local Fast Tier"
    if words < 10:
        return "local", "Tier 2 Concise General Prompt (<10 words) -> Local Fast Tier"
    return "chatgpt", "Tier 2 General Multi-Step Query -> OpenAI GPT-4o"

def is_provider_live(provider_key: str) -> bool:
    if provider_key == "local" and LOCAL_GPU_ONLINE:
        return True
    req_key = MODEL_CONFIGS.get(provider_key, MODEL_CONFIGS["gemini"])["required_key"]
    return bool(get_api_key(req_key))

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

        elif provider_key in ("gemini", "local"):
            api_key = get_api_key("GEMINI_API_KEY")
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

        elif provider_key == "chatgpt":
            api_key = get_api_key("OPENAI_API_KEY")
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

        elif provider_key == "claude":
            api_key = get_api_key("ANTHROPIC_API_KEY")
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
        req_key = target["required_key"]
        return (
            f"**[Demo Simulation Mode — Ready for Live Key]**\n\n"
            f"- **Routed Target:** `{target['label']}` (`{model_name}`)\n"
            f"- **Injected Local Memories:**\n{local_context}\n"
            f"- **To Enable Live Inference:** Add `{req_key}` in the sidebar (or in Streamlit Cloud **Manage app → Settings → Secrets**).\n"
            f"- **Diagnostic:** `{str(e)[:180]}`",
            {
                "provider": provider_key,
                "model_label": f"{target['label']} (Simulated)",
                "latency_ms": elapsed_ms,
                "status": "Simulated",
            },
        )


# =========================================================
# 5. AUTONOMOUS AGENT ENGINE (ReAct + HITL + Mock Simulator)
# =========================================================
AGENT_SYSTEM_PROMPT = """You are a Local-First Autonomous Agent with strict sandboxed tool access.
Respond ONLY with a single valid JSON object matching ONE of these two formats:

Format 1 (Call a tool):
{
  "thought": "Brief explanation of why this step is needed",
  "tool": "read_file" | "write_file" | "recall_memory" | "save_memory" | "ask_specialist",
  "args": { ...tool specific arguments... }
}

Tool Signatures:
- "read_file": {"path": "readme_sandbox.txt"}
- "write_file": {"path": "scripts/solution.py", "content": "full file content"}
- "recall_memory": {"query": "search keywords"}
- "save_memory": {"topic": "project_log", "fact": "concise fact to store"}
- "ask_specialist": {"provider": "claude" | "gemini" | "chatgpt", "task": "detailed sub-prompt"}

Format 2 (Final Answer once complete):
{
  "thought": "All steps complete",
  "tool": "final_answer",
  "answer": "Your complete Markdown response summarizing what was done"
}"""

def get_mock_agent_action(user_goal: str, step_num: int, scratchpad: str) -> Dict[str, Any]:
    """Deterministic 4-step ReAct simulation when testing without live API keys."""
    slug = re.sub(r"[^a-z0-9]+", "_", user_goal.lower()).strip("_")[:18] or "task"
    target_script = f"scripts/{slug}_tool.py"

    if step_num == 1:
        return {
            "thought": "Inspect workspace policy in readme_sandbox.txt before generating any files.",
            "tool": "read_file",
            "args": {"path": "readme_sandbox.txt"},
        }
    elif step_num == 2:
        return {
            "thought": "Delegate modular Python code generation to specialist Claude 3.7 Sonnet using stored coding_style preferences.",
            "tool": "ask_specialist",
            "args": {
                "provider": "claude",
                "task": f"Write a clean pathlib-based Python module for: {user_goal}",
            },
        }
    elif step_num == 3:
        sample_code = (
            "from pathlib import Path\nfrom typing import Optional\n\n"
            f"# Auto-generated by Surface Hybrid Agent for: {user_goal}\n"
            "SANDBOX_ROOT = Path(__file__).resolve().parent.parent\n\n"
            "def run_sandboxed_task(filename: str, text: str) -> Path:\n"
            "    target: Path = (SANDBOX_ROOT / filename).resolve()\n"
            "    if not target.is_relative_to(SANDBOX_ROOT):\n"
            "        raise PermissionError('Path escapes sandbox')\n"
            "    target.write_text(text, encoding='utf-8')\n"
            "    return target\n"
        )
        return {
            "thought": f"Save the generated Python utility to '{target_script}' inside safe_workspace (requires Human Approval if HITL is active).",
            "tool": "write_file",
            "args": {"path": target_script, "content": sample_code},
        }
    else:
        was_rejected = "REJECTED" in scratchpad or "Write blocked" in scratchpad
        status_line = (
            f"Write to `{target_script}` was **rejected by human operator**; no files were modified."
            if was_rejected
            else f"Successfully wrote `{target_script}` into the sandbox (`safe_workspace`)."
        )
        return {
            "thought": "Summarize the multi-step execution trace and present the final result.",
            "tool": "final_answer",
            "answer": (
                f"### Autonomous Agent Execution Complete (4 Steps)\n\n"
                f"1. **Workspace Inspection (`read_file`):** Verified sandbox rules from `readme_sandbox.txt`.\n"
                f"2. **Specialist Delegation (`ask_specialist` → `Claude 3.7 Sonnet`):** Drafted a `pathlib` + type-hinted script tailored to *{user_goal}*.\n"
                f"3. **Sandboxed File Commit (`write_file`):** {status_line}\n\n"
                f"> Open the **🔍 Agent Step Trace (4 Steps)** expander above to inspect the exact JSON thoughts, tool arguments, and observations at each turn."
            ),
        }

def dispatch_agent_tool(tool_name: str, args: Dict[str, Any], read_only_lock: bool = False) -> str:
    try:
        if tool_name == "read_file":
            return safe_read_file(args.get("path", "readme_sandbox.txt"))
        elif tool_name == "write_file":
            return safe_write_file(
                args.get("path", "scripts/output.txt"),
                args.get("content", ""),
                read_only_lock=read_only_lock,
            )
        elif tool_name == "recall_memory":
            return recall_memories(args.get("query", ""), limit=5) or "No matching memories."
        elif tool_name == "save_memory":
            if read_only_lock:
                return "Write blocked: Strict Read-Only Lock Mode is enabled."
            save_memory(args.get("topic", "agent_note"), args.get("fact", ""))
            return f"Saved fact to SQLite memory under topic '{args.get('topic')}'."
        elif tool_name == "ask_specialist":
            spec_provider = args.get("provider", "claude")
            spec_task = args.get("task", "")
            spec_ctx = recall_memories(spec_task, provider_hint=spec_provider, limit=3)
            if is_provider_live(spec_provider):
                reply, _ = execute_llm_call(spec_task, spec_provider, local_context=spec_ctx)
                return f"[Live {spec_provider.upper()} Output]:\n{reply[:400]}"
            return (
                f"[Simulated {spec_provider.upper()} Specialist Output]: Generated modular Python implementation "
                f"using pathlib.Path and type annotations following local coding_style memory."
            )
        return f"Error: Unknown tool '{tool_name}'."
    except Exception as err:
        return f"Tool Execution Error: {err}"

def get_next_agent_action(
    user_goal: str,
    controller_provider: str,
    step_num: int,
    scratchpad: str,
    local_ctx: str,
    force_mock: bool,
) -> Dict[str, Any]:
    if force_mock or not is_provider_live(controller_provider):
        return get_mock_agent_action(user_goal, step_num, scratchpad)

    prompt_payload = f"{AGENT_SYSTEM_PROMPT}\n\n{scratchpad}\nRespond with JSON for Step {step_num}:"
    raw_reply, _ = execute_llm_call(prompt_payload, controller_provider, local_ctx)
    json_match = re.search(r"\{.*\}", raw_reply, re.DOTALL)
    if not json_match:
        return {"thought": "Direct response", "tool": "final_answer", "answer": raw_reply}
    try:
        return json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return {"thought": "Fallback parse", "tool": "final_answer", "answer": raw_reply}

def run_or_resume_agent(state: Dict[str, Any], hitl_enabled: bool, read_only_lock: bool):
    """
    Executes the ReAct agent loop from state['step_num'] up to MAX_AGENT_STEPS.
    If hitl_enabled is True and the agent requests 'write_file' or 'save_memory',
    saves state into st.session_state.pending_agent and pauses for human button click.
    """
    user_goal = state["user_goal"]
    controller = state["controller_provider"]
    local_ctx = state["local_ctx"]
    force_mock = state["force_mock"]

    while state["step_num"] <= MAX_AGENT_STEPS:
        step_num = state["step_num"]
        action = get_next_agent_action(
            user_goal, controller, step_num, state["scratchpad"], local_ctx, force_mock
        )
        thought = action.get("thought", "Reasoning...")
        tool = action.get("tool", "final_answer")
        args = action.get("args", {})

        # Check if task is complete
        if tool == "final_answer" or "answer" in action:
            state["step_logs"].append({
                "step": step_num,
                "thought": thought,
                "tool": "final_answer",
                "observation": "Delivered final response to user.",
            })
            elapsed_ms = int((time.perf_counter() - state["start_t"]) * 1000)
            mode_tag = "Mock Agent Sim" if (force_mock or not is_provider_live(controller)) else "Live Agent"
            meta = {
                "model_label": f"{MODEL_CONFIGS[controller]['label']} ({mode_tag})",
                "reason": state["reason"],
                "latency_ms": elapsed_ms,
                "steps": state["step_logs"],
            }
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": action.get("answer", "Task finished."),
                "meta": meta,
            })
            st.session_state.pending_agent = None
            return

        # HUMAN-IN-THE-LOOP (HITL) GATE FOR MUTATING TOOLS
        if hitl_enabled and tool in ("write_file", "save_memory"):
            state["pending_action"] = action
            st.session_state.pending_agent = state
            return  # Pause execution and render approval UI!

        # Otherwise execute non-destructive tool immediately
        obs = dispatch_agent_tool(tool, args, read_only_lock=read_only_lock)
        state["step_logs"].append({
            "step": step_num,
            "thought": thought,
            "tool": f"{tool}({json.dumps(args)})",
            "observation": obs,
        })
        state["scratchpad"] += (
            f"Step {step_num} Action: {json.dumps(action)}\n"
            f"Step {step_num} Observation: {obs}\n\n"
        )
        state["step_num"] += 1

    # If MAX_AGENT_STEPS reached without final_answer
    elapsed_ms = int((time.perf_counter() - state["start_t"]) * 1000)
    st.session_state.chat_history.append({
        "role": "assistant",
        "content": "Agent reached the maximum 4-step safety boundary. Inspect the step trace above for details.",
        "meta": {
            "model_label": f"{MODEL_CONFIGS[controller]['label']} (Max Steps)",
            "reason": state["reason"],
            "latency_ms": elapsed_ms,
            "steps": state["step_logs"],
        },
    })
    st.session_state.pending_agent = None


# =========================================================
# 6. STREAMLIT UI, APPROVAL BANNER & STEP TRACE VIEWER
# =========================================================
st.set_page_config(page_title="Surface Hybrid AI Hub & Agent", layout="wide")
seed_default_memories()
init_virtual_sandbox()

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "pending_agent" not in st.session_state:
    st.session_state.pending_agent = None

st.title("Surface Laptop Studio — Hybrid Local AI Hub & Agent")

# Top Hardware Telemetry
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
        "Online (:8080)" if LOCAL_GPU_ONLINE else "Offline (Cloud/Mock Sim)",
        "Llamafile mmap active" if LOCAL_GPU_ONLINE else "Using Fallback / Mock Agent",
    )
with col3:
    st.metric("Sandbox Files", f"{len(list_sandbox_files())} Files", "500 KB Max Write Cap")

# Top Mobile-Friendly Mode Bar (No need to open sidebar on phone)
ctrl1, ctrl2, ctrl3 = st.columns([2, 1.5, 1.5])
with ctrl1:
    exec_architecture = st.radio(
        "Execution Architecture",
        ["🤖 Autonomous Agent (ReAct Loop)", "⚡ Single-Pass Router"],
        horizontal=True,
    )
with ctrl2:
    hitl_enabled = st.toggle("⏸️ Human Approval for Writes (HITL)", value=True)
with ctrl3:
    force_mock_agent = st.toggle("🧪 Force 4-Step Mock Agent Sim", value=False)

st.divider()

# Sidebar Controls
with st.sidebar:
    st.header("1. Routing & Security")
    routing_mode = st.selectbox(
        "Provider Selection",
        ["Auto-Route (2-Tier Smart Router)", "local", "gemini", "chatgpt", "claude"],
    )
    read_only_lock = st.toggle("Strict Read-Only Lock Mode", value=False)

    with st.expander("API Keys (Session-Only RAM)", expanded=False):
        st.caption("Keys stay in RAM for this session and are never written to disk.")
        g_key = st.text_input("GEMINI_API_KEY", value=get_api_key("GEMINI_API_KEY"), type="password")
        o_key = st.text_input("OPENAI_API_KEY", value=get_api_key("OPENAI_API_KEY"), type="password")
        a_key = st.text_input("ANTHROPIC_API_KEY", value=get_api_key("ANTHROPIC_API_KEY"), type="password")
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
    st.header("3. Sandboxed File Explorer")
    sandbox_files = list_sandbox_files()
    selected_file = st.selectbox("Files in safe_workspace", sandbox_files)
    if selected_file and st.button("Inspect Selected File"):
        st.code(safe_read_file(selected_file), language="python")

# Quick-Tap Mobile Test Bar
st.caption("**Quick-Tap Mobile Tests (Works in Both Router & Agent Modes):**")
b1, b2, b3, b4 = st.columns(4)
triggered_prompt = None
if b1.button("⚡ Local Utility", use_container_width=True):
    triggered_prompt = "Format this raw list into clean JSON: apple, banana, cherry"
if b2.button("🛠️ Word Code (Claude)", use_container_width=True):
    triggered_prompt = "Write me a code to create and update Microsoft Word."
if b3.button("🧠 USB4 Analysis (Gemini)", use_container_width=True):
    triggered_prompt = "Compare the trade-offs of running quantized local models on 4GB VRAM over USB4."
if b4.button("🤖 Multi-Step Agent Task", use_container_width=True):
    triggered_prompt = "Read my sandbox policy, generate a Word automation script, and save it to scripts/word_agent.py."

# Render Conversation & Expandable Agent Step Traces
for item in st.session_state.chat_history:
    with st.chat_message(item["role"]):
        if "meta" in item:
            st.caption(
                f"**Route:** `{item['meta']['model_label']}` | "
                f"**Reason:** {item['meta']['reason']} | "
                f"**Latency:** `{item['meta']['latency_ms']} ms`"
            )
            if "steps" in item["meta"] and item["meta"]["steps"]:
                with st.expander(f"🔍 Agent Execution Trace ({len(item['meta']['steps'])} Steps)", expanded=True):
                    for s in item["meta"]["steps"]:
                        st.markdown(
                            f"**Step {s['step']} — `{s['tool']}`**\n"
                            f"- *Thought:* {s['thought']}\n"
                            f"- *Observation:* `{s['observation'][:240]}`"
                        )
        st.markdown(item["content"])

# =========================================================
# 7. HUMAN-IN-THE-LOOP (HITL) PENDING APPROVAL CARD
# =========================================================
if st.session_state.pending_agent is not None:
    pending = st.session_state.pending_agent
    p_action = pending["pending_action"]
    p_tool = p_action.get("tool", "write_file")
    p_args = p_action.get("args", {})

    st.warning(
        f"### ⏸️ Human Approval Required (Agent Step {pending['step_num']} of {MAX_AGENT_STEPS})\n"
        f"**Agent Thought:** {p_action.get('thought')}\n\n"
        f"**Requested Tool:** `{p_tool}`"
    )
    if p_tool == "write_file":
        st.markdown(f"**Target Sandbox Path:** `safe_workspace/{p_args.get('path', '')}`")
        st.code(p_args.get("content", ""), language="python")
    else:
        st.code(json.dumps(p_args, indent=2), language="json")

    ap_col1, ap_col2 = st.columns(2)
    if ap_col1.button("✅ Approve Write & Resume Agent", type="primary", use_container_width=True):
        obs = dispatch_agent_tool(p_tool, p_args, read_only_lock=read_only_lock)
        pending["step_logs"].append({
            "step": pending["step_num"],
            "thought": p_action.get("thought", ""),
            "tool": f"{p_tool} [APPROVED BY USER]",
            "observation": obs,
        })
        pending["scratchpad"] += (
            f"Step {pending['step_num']} Action: {json.dumps(p_action)}\n"
            f"Step {pending['step_num']} Observation (APPROVED): {obs}\n\n"
        )
        pending["step_num"] += 1
        run_or_resume_agent(pending, hitl_enabled=hitl_enabled, read_only_lock=read_only_lock)
        st.rerun()

    if ap_col2.button("🛑 Reject Write & Resume Agent", use_container_width=True):
        obs = "User REJECTED this write operation. Do not modify disk; proceed to final summary."
        pending["step_logs"].append({
            "step": pending["step_num"],
            "thought": p_action.get("thought", ""),
            "tool": f"{p_tool} [REJECTED BY USER]",
            "observation": obs,
        })
        pending["scratchpad"] += (
            f"Step {pending['step_num']} Action: {json.dumps(p_action)}\n"
            f"Step {pending['step_num']} Observation (REJECTED): {obs}\n\n"
        )
        pending["step_num"] += 1
        run_or_resume_agent(pending, hitl_enabled=hitl_enabled, read_only_lock=read_only_lock)
        st.rerun()

# =========================================================
# 8. CHAT INPUT & DISPATCH TRIGGER
# =========================================================
chat_box_prompt = st.chat_input("Ask anything or tap a quick-test button above...")
active_prompt = triggered_prompt or chat_box_prompt

if active_prompt and st.session_state.pending_agent is None:
    st.session_state.chat_history.append({"role": "user", "content": active_prompt})

    if routing_mode == "Auto-Route (2-Tier Smart Router)":
        chosen_provider, reason = route_query(active_prompt)
    else:
        chosen_provider = routing_mode
        reason = f"Manual override -> {routing_mode.upper()}"

    local_ctx = recall_memories(active_prompt, provider_hint=chosen_provider, limit=4)

    if exec_architecture.startswith("🤖"):
        # Start Autonomous ReAct Agent Loop
        agent_state = {
            "user_goal": active_prompt,
            "controller_provider": chosen_provider,
            "reason": f"Agent Controller ({reason})",
            "local_ctx": local_ctx,
            "force_mock": force_mock_agent,
            "step_num": 1,
            "scratchpad": f"User Goal: {active_prompt}\n\n",
            "step_logs": [],
            "pending_action": None,
            "start_t": time.perf_counter(),
        }
        run_or_resume_agent(agent_state, hitl_enabled=hitl_enabled, read_only_lock=read_only_lock)
        st.rerun()
    else:
        # Single-Pass Router Mode
        reply_text, telemetry = execute_llm_call(active_prompt, chosen_provider, local_ctx)
        telemetry["reason"] = reason
        st.session_state.chat_history.append(
            {"role": "assistant", "content": reply_text, "meta": telemetry}
        )
        st.rerun()
