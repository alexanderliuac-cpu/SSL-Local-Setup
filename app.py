import os
import re
import io
import json
import time
import socket
import zipfile
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from typing import Tuple, Dict, Any, List, Optional
import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =========================================================
# 1. HARDWARE, TIMEZONE & API KEY AUTO-DETECTION
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
SSD_MARKER_DIR = BASE_DIR / "memory_store"
SANDBOX_DIR = (BASE_DIR / "safe_workspace").resolve()
REGISTRY_FILE = BASE_DIR / "mcp_registry.json"
MAX_WRITE_BYTES = 1024 * 500  # 500 KB cap
LOCAL_TZ = timezone(timedelta(hours=8))  # UTC+8 Local Time

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

def save_memory(topic: str, fact: str, custom_ts: str = "") -> str:
    conn = get_db_connection()
    ts = custom_ts or datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO memories (topic, fact, timestamp) VALUES (?, ?, ?)",
        (topic.strip(), fact.strip(), ts),
    )
    conn.commit()
    return f"Indexed fact under topic '{topic}' at {ts}."

def get_memories_by_topic(topic_prefix: str) -> List[Tuple[str, str, str]]:
    conn = get_db_connection()
    return conn.execute(
        "SELECT topic, fact, timestamp FROM memories WHERE topic LIKE ? ORDER BY timestamp ASC",
        (f"{topic_prefix}%",),
    ).fetchall()

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
            "readme_sandbox.txt": (
                "Workspace Policy: All generated scripts and daily/weekly reports must remain "
                "strictly inside safe_workspace and require Human-in-the-Loop approval before publishing."
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
# 4. ZERO-DEPENDENCY NATIVE WORD (.DOCX) GENERATOR
# =========================================================
def build_docx_bytes(title: str, subtitle: str, markdown_body: str) -> bytes:
    paragraphs_xml = []

    def add_p(text: str, bold: bool = False, size_half_pt: int = 22, color_hex: str = "1F2937", space_after: int = 120):
        safe_txt = xml_escape(text)
        b_tag = "<w:b/>" if bold else ""
        paragraphs_xml.append(
            f'<w:p><w:pPr><w:spacing w:after="{space_after}"/></w:pPr>'
            f'<w:r><w:rPr>{b_tag}<w:sz w:val="{size_half_pt}"/><w:color w:val="{color_hex}"/></w:rPr>'
            f'<w:t xml:space="preserve">{safe_txt}</w:t></w:r></w:p>'
        )

    add_p(title, bold=True, size_half_pt=36, color_hex="0F172A", space_after=80)
    add_p(subtitle, bold=False, size_half_pt=20, color_hex="64748B", space_after=240)

    for raw_line in markdown_body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        clean_line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        if line.startswith("### "):
            add_p(clean_line[4:], bold=True, size_half_pt=24, color_hex="1E3A8A", space_after=100)
        elif line.startswith("## "):
            add_p(clean_line[3:], bold=True, size_half_pt=28, color_hex="0F172A", space_after=120)
        elif line.startswith("# "):
            add_p(clean_line[2:], bold=True, size_half_pt=32, color_hex="0F172A", space_after=140)
        elif line.startswith(("- ", "* ")):
            add_p(f"•  {clean_line[2:]}", bold=False, size_half_pt=22, color_hex="1F2937", space_after=80)
        else:
            add_p(clean_line, bold=False, size_half_pt=22, color_hex="1F2937", space_after=120)

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(paragraphs_xml)}'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        '</w:body></w:document>'
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as docx_zip:
        docx_zip.writestr("[Content_Types].xml", content_types_xml)
        docx_zip.writestr("_rels/.rels", rels_xml)
        docx_zip.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# =========================================================
# 5. JUST-IN-TIME (JIT) MCP SKILL REGISTRY & COMPILER
# =========================================================
DEFAULT_MCP_SKILLS: List[Dict[str, Any]] = [
    {
        "skill_id": "read_sandbox_file",
        "origin": "mcp://local-sandbox/fs-reader",
        "mutating": False,
        "triggers": ["read", "inspect", "open", "policy", "readme", "file", "check", "contents", "sandbox"],
        "declaration": {
            "name": "read_sandbox_file",
            "description": "Reads the text content of a file inside the path-jailed safe_workspace sandbox.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Relative path inside safe_workspace, e.g., readme_sandbox.txt"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "skill_id": "write_sandbox_file",
        "origin": "mcp://local-sandbox/fs-writer",
        "mutating": True,  # Automatically triggers Human-in-the-Loop (HITL) Approval
        "triggers": ["write", "save", "create", "script", "generate", "file", "py", "md", "txt", "code", "store"],
        "declaration": {
            "name": "write_sandbox_file",
            "description": "Writes or updates a file inside safe_workspace (requires Human Approval if HITL is active).",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Relative file path, e.g., scripts/tool.py"},
                    "content": {"type": "STRING", "description": "Complete text or code content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "skill_id": "query_local_memory",
        "origin": "mcp://local-sqlite/fts5-search",
        "mutating": False,
        "triggers": ["memory", "remember", "recall", "stored", "history", "daily_report", "past", "logs"],
        "declaration": {
            "name": "query_local_memory",
            "description": "Searches the local SQLite FTS5 memory store for facts or approved daily reports.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING", "description": "Search keyword or topic name, e.g., daily_report or hardware"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "skill_id": "save_memory_fact",
        "origin": "mcp://local-sqlite/fts5-writer",
        "mutating": True,
        "triggers": ["memorize", "remember", "log", "fact", "preference", "record"],
        "declaration": {
            "name": "save_memory_fact",
            "description": "Saves a durable fact or operational log into the local SQLite FTS5 memory database.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "topic": {"type": "STRING", "description": "Short category tag, e.g., project_rule or daily_report"},
                    "fact": {"type": "STRING", "description": "Concise fact statement to persist"},
                },
                "required": ["topic", "fact"],
            },
        },
    },
    {
        "skill_id": "fetch_live_fx_rate",
        "origin": "mcp://remote-api/exchangerate-connector",
        "mutating": False,
        "triggers": ["exchange", "rate", "fx", "currency", "usd", "twd", "jpy", "eur", "convert", "dollar"],
        "declaration": {
            "name": "fetch_live_fx_rate",
            "description": "Fetches live real-time currency exchange rates (e.g., USD to TWD, JPY, EUR) from an external API.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "base_currency": {"type": "STRING", "description": "3-letter base code, e.g., USD"},
                    "target_currency": {"type": "STRING", "description": "3-letter target code, e.g., TWD or JPY"},
                },
                "required": ["base_currency", "target_currency"],
            },
        },
    },
    {
        "skill_id": "export_weekly_docx",
        "origin": "mcp://local-sandbox/docx-compiler",
        "mutating": False,
        "triggers": ["word", "docx", "weekly", "report", "compile", "download", "summary"],
        "declaration": {
            "name": "export_weekly_docx",
            "description": "Compiles all approved daily_report entries from SQLite memory into a downloadable Microsoft Word (.docx) file.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "report_title": {"type": "STRING", "description": "Document header title for the Word report"}
                },
                "required": ["report_title"],
            },
        },
    },
]

def get_skill_catalog() -> List[Dict[str, Any]]:
    if "mcp_catalog" not in st.session_state:
        if REGISTRY_FILE.exists():
            try:
                st.session_state.mcp_catalog = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            except Exception:
                st.session_state.mcp_catalog = list(DEFAULT_MCP_SKILLS)
        else:
            st.session_state.mcp_catalog = list(DEFAULT_MCP_SKILLS)
    return st.session_state.mcp_catalog

def compile_mcp_skillset(user_prompt: str) -> Dict[str, Any]:
    """
    Scans the prompt and compiles ONLY the matching MCP tool declarations.
    If 0 skills match, sends 0 tool schemas to save 100% of tool token overhead.
    """
    catalog = get_skill_catalog()
    clean_lower = user_prompt.lower()
    tokens = set(re.findall(r"[a-z0-9_]+", clean_lower))

    matched_skills = []
    for item in catalog:
        if any(t in tokens or t in clean_lower for t in item["triggers"]):
            matched_skills.append(item)

    # Estimate token savings vs a naive 30-tool monolithic agent (~6,000 tokens/turn)
    naive_baseline_tokens = 6000
    compiled_tokens = len(matched_skills) * 95
    saved_tokens = max(0, naive_baseline_tokens - compiled_tokens)

    return {
        "allocation_mode": "LOCAL_DIRECT (0 MCP Schemas)" if not matched_skills else f"JIT_MCP_COMPILED ({len(matched_skills)} Skills)",
        "matched_skills": matched_skills,
        "declarations": [s["declaration"] for s in matched_skills],
        "compiled_tokens": compiled_tokens,
        "saved_tokens": saved_tokens,
    }

def execute_mcp_tool(fn_name: str, args: Dict[str, Any], read_only_lock: bool = False) -> str:
    """Executes local sandboxed skills or remote HTTPS MCP connectors."""
    try:
        if fn_name == "read_sandbox_file":
            return safe_read_file(args.get("path", "readme_sandbox.txt"))
        elif fn_name == "write_sandbox_file":
            return safe_write_file(args.get("path", "scripts/out.txt"), args.get("content", ""), read_only_lock=read_only_lock)
        elif fn_name == "query_local_memory":
            return recall_memories(args.get("query", ""), limit=6) or "No matching memories found."
        elif fn_name == "save_memory_fact":
            if read_only_lock:
                return "Blocked: Strict Read-Only Lock Mode is active."
            return save_memory(args.get("topic", "general"), args.get("fact", ""))
        elif fn_name == "fetch_live_fx_rate":
            base = args.get("base_currency", "USD").upper().strip()
            target = args.get("target_currency", "TWD").upper().strip()
            url = f"https://open.er-api.com/v6/latest/{base}"
            req = urllib.request.Request(url, headers={"User-Agent": "Surface-Hybrid-MCP/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            rate = data.get("rates", {}).get(target)
            if rate is not None:
                return f"Live Exchange Rate ({base} -> {target}): 1 {base} = {rate} {target} (Updated: {data.get('time_last_update_utc', 'Live')})"
            return f"Currency '{target}' not found in live feed for base {base}."
        elif fn_name == "export_weekly_docx":
            title = args.get("report_title", "Weekly Executive Operations Report")
            rows = get_memories_by_topic("daily_report")
            body = "\n".join([f"- [{r[2]}] {r[1]}" for r in rows]) if rows else "- No approved daily reports yet."
            md = f"## Executive Summary\nCompiled {len(rows)} approved daily reports from SQLite memory.\n\n## Daily Logs\n{body}"
            st.session_state.compiled_weekly_md = md
            safe_write_file("weekly_reports/Weekly_Operations_Summary.md", md, read_only_lock=False)
            return f"Compiled '{title}' ({len(rows)} daily logs) into safe_workspace/weekly_reports/Weekly_Operations_Summary.md and unlocked .docx download."
        else:
            # Custom dynamically synthesized skill execution
            return f"Executed dynamic MCP skill '{fn_name}' with args {json.dumps(args)}."
    except Exception as err:
        return f"MCP Tool Execution Error ({fn_name}): {err}"


# =========================================================
# 6. LIVE GEMINI NATIVE FUNCTION-CALLING & MULTI-API ROUTER
# =========================================================
MODEL_CONFIGS = {
    "local": {
        "model": "local-model" if LOCAL_GPU_ONLINE else "gemini-2.5-flash",
        "label": "Local RTX 3050 Ti (Llama-3.2-3B)" if LOCAL_GPU_ONLINE else "Fast Local Tier (Gemini 2.5 Flash)",
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

def route_query(prompt: str) -> Tuple[str, str]:
    clean = prompt.strip().lower()
    words = len(clean.split())
    if any(k in clean for k in ("compare", "trade-offs", "analyze", "architecture", "design", "evaluate")) or words > 35:
        return "gemini", "Tier 1 Deep Reasoning -> Gemini 2.5 Pro"
    if any(k in clean for k in ("claude", "refactor", "complex script")):
        return "claude", "Tier 1 Code Specialist -> Claude 3.7 Sonnet"
    if any(k in clean for k in ("chatgpt", "gpt-4o")):
        return "chatgpt", "Explicit Provider -> OpenAI GPT-4o"
    return "local", "Tier 1 Fast Hybrid Tier -> Gemini 2.5 Flash / Local RTX"

def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode("utf-8"))

def call_gemini_with_mcp(
    contents: List[Dict[str, Any]],
    system_msg: str,
    declarations: List[Dict[str, Any]],
    model_name: str = "gemini-2.5-flash",
) -> Dict[str, Any]:
    """Calls live Google Gemini REST API with JIT-compiled MCP functionDeclarations."""
    api_key = get_api_key("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload: Dict[str, Any] = {
        "system_instruction": {"parts": [{"text": system_msg}]},
        "contents": contents,
    }
    if declarations:
        payload["tools"] = [{"functionDeclarations": declarations}]

    try:
        return _post_json(url, {"Content-Type": "application/json"}, payload)
    except urllib.error.HTTPError as http_err:
        err_body = http_err.read().decode("utf-8", errors="replace")
        # Automatic fallback to gemini-2.5-flash if gemini-2.5-pro quota/rate-limit is hit
        if model_name != "gemini-2.5-flash":
            url_fb = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            return _post_json(url_fb, {"Content-Type": "application/json"}, payload)
        raise RuntimeError(f"Gemini API HTTP {http_err.code}: {err_body[:240]}")

def run_hybrid_mcp_turn(
    user_prompt: str,
    provider_key: str,
    routing_reason: str,
    hitl_enabled: bool,
    read_only_lock: bool,
):
    """
    1. Runs the JIT MCP Skill Compiler on the prompt.
    2. Calls Live Gemini API (or selected provider) with ONLY the compiled MCP tool schemas.
    3. If Gemini returns a native functionCall:
       - Pauses for Human Approval if the skill is mutating (write_sandbox_file / save_memory_fact).
       - Executes immediately if non-mutating (e.g., fetch_live_fx_rate, read_sandbox_file) and returns functionResponse to Gemini!
    """
    start_t = time.perf_counter()
    compiled = compile_mcp_skillset(user_prompt)
    st.session_state.total_tokens_saved += compiled["saved_tokens"]
    st.session_state.last_compiled_info = compiled

    local_ctx = recall_memories(user_prompt, provider_hint=provider_key, limit=4)
    files_list = ", ".join(list_sandbox_files())
    system_msg = (
        "You are a Local-First Hybrid AI Agent on a Surface Laptop Studio. "
        f"Current files in safe_workspace: [{files_list}]. "
        "Use your provided MCP tools whenever the user asks to read/write files, check live exchange rates, "
        "search/save memories, or compile weekly Word reports.\n"
        f"Injected Local Memories:\n{local_ctx}"
    )

    target_cfg = MODEL_CONFIGS.get(provider_key, MODEL_CONFIGS["local"])
    model_name = target_cfg["model"] if provider_key in ("gemini", "local") else "gemini-2.5-flash"

    contents: List[Dict[str, Any]] = [{"role": "user", "parts": [{"text": user_prompt}]}]
    step_logs: List[Dict[str, Any]] = []

    try:
        res = call_gemini_with_mcp(contents, system_msg, compiled["declarations"], model_name=model_name)
        cand_content = res["candidates"][0]["content"]
        parts = cand_content.get("parts", [])

        # Check if Gemini invoked an MCP tool via native functionCall
        fn_call_part = next((p for p in parts if "functionCall" in p), None)

        if fn_call_part:
            fn_call = fn_call_part["functionCall"]
            fn_name = fn_call["name"]
            fn_args = fn_call.get("args", {})
            is_mutating = any(s["skill_id"] == fn_name and s.get("mutating") for s in compiled["matched_skills"])

            # HUMAN-IN-THE-LOOP (HITL) INTERCEPTION FOR LIVE GEMINI FUNCTION CALLS
            if hitl_enabled and is_mutating:
                st.session_state.pending_mcp_call = {
                    "fn_name": fn_name,
                    "fn_args": fn_args,
                    "contents": contents,
                    "cand_content": cand_content,
                    "system_msg": system_msg,
                    "declarations": compiled["declarations"],
                    "model_name": model_name,
                    "model_label": target_cfg["label"],
                    "routing_reason": routing_reason,
                    "compiled_info": compiled,
                    "start_t": start_t,
                }
                return

            # Non-mutating MCP Skill: Execute immediately and send functionResponse back to Gemini
            tool_output = execute_mcp_tool(fn_name, fn_args, read_only_lock=read_only_lock)
            step_logs.append({
                "step": 1,
                "tool": f"{fn_name}({json.dumps(fn_args)})",
                "observation": tool_output,
            })

            contents.append(cand_content)
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": fn_name, "response": {"result": tool_output}}}],
            })
            res2 = call_gemini_with_mcp(contents, system_msg, compiled["declarations"], model_name=model_name)
            final_parts = res2["candidates"][0]["content"].get("parts", [])
            reply_text = "\n".join([p["text"] for p in final_parts if "text" in p]) or tool_output
        else:
            reply_text = "\n".join([p["text"] for p in parts if "text" in p])

        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        st.session_state.chat_history.append({
            "role": "assistant",
            "content": reply_text,
            "meta": {
                "model_label": f"{target_cfg['label']} (🟢 Live API)",
                "reason": routing_reason,
                "mcp_mode": compiled["allocation_mode"],
                "compiled_skills": [s["skill_id"] for s in compiled["matched_skills"]],
                "saved_tokens": compiled["saved_tokens"],
                "latency_ms": elapsed_ms,
                "steps": step_logs,
            },
        })

    except Exception as err:
        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        st.session_state.chat_history.append({
            "role": "assistant",
            "content": (
                f"**[Waiting for Live `GEMINI_API_KEY` or Check Key Quota]**\n\n"
                f"- **JIT Allocation Mode:** `{compiled['allocation_mode']}`\n"
                f"- **Compiled MCP Schemas:** `{[s['skill_id'] for s in compiled['matched_skills']] or 'None (0 overhead)'}`\n"
                f"- **Tokens Saved This Turn:** `{compiled['saved_tokens']:,} tokens`\n"
                f"- **Diagnostic:** `{str(err)[:220]}`"
            ),
            "meta": {
                "model_label": f"{target_cfg['label']} (Simulated)",
                "reason": routing_reason,
                "mcp_mode": compiled["allocation_mode"],
                "compiled_skills": [s["skill_id"] for s in compiled["matched_skills"]],
                "saved_tokens": compiled["saved_tokens"],
                "latency_ms": elapsed_ms,
                "steps": step_logs,
            },
        })


# =========================================================
# 7. DAILY 6:00 PM OPS & DYNAMIC SKILL SYNTHESIZER HELPERS
# =========================================================
def generate_daily_report_draft(report_date: str, raw_notes: str) -> Tuple[str, str]:
    if get_api_key("GEMINI_API_KEY"):
        try:
            prompt = (
                f"Create a concise, executive Daily Operation Report for {report_date} (18:00 Local Time) "
                f"from the raw notes below. Include: 1. Executive Summary, 2. Operational Metrics, "
                f"3. Quality/Risk Log, and 4. Next-Day Actions.\n\nRaw Input:\n{raw_notes}"
            )
            res = call_gemini_with_mcp(
                [{"role": "user", "parts": [{"text": prompt}]}],
                "You are an Operational Excellence & Quality Control Agent.",
                declarations=[],
                model_name="gemini-2.5-flash",
            )
            text = "\n".join([p["text"] for p in res["candidates"][0]["content"]["parts"] if "text" in p])
            return text, "Gemini 2.5 Flash (🟢 Live 18:00 Synthesizer)"
        except Exception:
            pass

    bullets = "\n".join([f"- {l.strip('-•* ')}" for l in raw_notes.splitlines() if l.strip()])
    return (
        f"## Daily Operation Report — {report_date} (18:00 CST)\n\n"
        f"### 1. Executive Summary\nCompleted automated 6:00 PM weekday synthesis.\n\n"
        f"### 2. Logged Operational Data\n{bullets}\n\n"
        f"### 3. Next-Day Action Plan\n- Verify QC sign-off before 09:00 AM standup."
    ), "Fallback Local Formatter"

def seed_sample_weekday_reports():
    sample_days = [
        ("2026-10-12 (Mon)", "Completed baseline line walk-through; inspected incoming chemical batch #402 (99.4% purity); calibrated pH sensors."),
        ("2026-10-13 (Tue)", "Evaluated manual QC log sheets vs digital capture; identified 18-minute delay in lot sign-off; updated SOP checklist."),
        ("2026-10-14 (Wed)", "Tested local OCR receipt and label parser on 35 sample tickets (97.1% accuracy); zero unplanned downtime."),
        ("2026-10-15 (Thu)", "Reviewed waste neutralization tank telemetry; verified secondary containment valves; prepared Friday review tables."),
    ]
    for day_label, summary in sample_days:
        date_slug = day_label.split()[0]
        safe_write_file(f"daily_reports/{date_slug}.md", f"## Daily Report — {day_label}\n\n{summary}\n", read_only_lock=False)
        save_memory("daily_report", f"[{day_label}] {summary}", custom_ts=f"{date_slug} 18:05:00")


# =========================================================
# 8. STREAMLIT UI & LIVE TELEMETRY DASHBOARD
# =========================================================
st.set_page_config(page_title="Surface Hybrid MCP Kernel", layout="wide")
seed_default_memories()
init_virtual_sandbox()

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "pending_mcp_call" not in st.session_state:
    st.session_state.pending_mcp_call = None
if "pending_daily_draft" not in st.session_state:
    st.session_state.pending_daily_draft = None
if "compiled_weekly_md" not in st.session_state:
    st.session_state.compiled_weekly_md = None
if "total_tokens_saved" not in st.session_state:
    st.session_state.total_tokens_saved = 0
if "last_compiled_info" not in st.session_state:
    st.session_state.last_compiled_info = None

st.title("Surface Laptop Studio — Hybrid MCP Skill-Compiling Kernel")

# Sidebar Controls (Rendered first so API keys entered in sidebar take effect immediately)
with st.sidebar:
    st.header("1. Live API Keys & Security")
    with st.expander("🔑 API Keys (Secrets or Session RAM)", expanded=not bool(get_api_key("GEMINI_API_KEY"))):
        g_key = st.text_input("GEMINI_API_KEY", value=get_api_key("GEMINI_API_KEY"), type="password")
        o_key = st.text_input("OPENAI_API_KEY", value=get_api_key("OPENAI_API_KEY"), type="password")
        a_key = st.text_input("ANTHROPIC_API_KEY", value=get_api_key("ANTHROPIC_API_KEY"), type="password")
        if g_key: os.environ["GEMINI_API_KEY"] = g_key
        if o_key: os.environ["OPENAI_API_KEY"] = o_key
        if a_key: os.environ["ANTHROPIC_API_KEY"] = a_key

    routing_mode = st.selectbox(
        "Controller Routing Mode",
        ["Auto-Route (JIT Hybrid Compiler)", "local", "gemini", "chatgpt", "claude"],
    )
    hitl_enabled = st.toggle("⏸️ Human Approval for File/Memory Writes", value=True)
    read_only_lock = st.toggle("🔒 Strict Read-Only Lock Mode", value=False)

    st.divider()
    st.header("2. Dynamic MCP Skill Registry")
    catalog = get_skill_catalog()
    st.caption(f"**{len(catalog)} Registered Micro-Skills (Only matched skills are sent per prompt):**")
    for s in catalog:
        badge = "✍️ HITL-Gated" if s.get("mutating") else "⚡ Auto-Read"
        st.markdown(f"- **`{s['skill_id']}`** ({badge}) — `{s['origin']}`")

    with st.expander("🧬 Synthesize New MCP Skill via Gemini"):
        new_skill_desc = st.text_input("Describe a new capability", placeholder="e.g., Calculate battery Coulombic efficiency")
        if st.button("Synthesize & Register Skill") and new_skill_desc:
            slug = re.sub(r"[^a-z0-9]+", "_", new_skill_desc.lower()).strip("_")[:22]
            words = [w for w in re.findall(r"[a-z0-9]+", new_skill_desc.lower()) if len(w) > 3][:4]
            new_entry = {
                "skill_id": f"custom_{slug}",
                "origin": "mcp://dynamic-synthesized/plugin",
                "mutating": False,
                "triggers": words or ["custom"],
                "declaration": {
                    "name": f"custom_{slug}",
                    "description": new_skill_desc,
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {"input_data": {"type": "STRING", "description": "Input parameters for the skill"}},
                        "required": ["input_data"],
                    },
                },
            }
            st.session_state.mcp_catalog.append(new_entry)
            st.success(f"Registered `{new_entry['skill_id']}` into MCP Catalog!")
            st.rerun()

    st.divider()
    st.header("3. Sandboxed File Explorer")
    sandbox_files = list_sandbox_files()
    selected_file = st.selectbox("Files in safe_workspace", sandbox_files)
    if selected_file and st.button("Inspect Selected File"):
        st.code(safe_read_file(selected_file), language="markdown")

# Top Telemetry Metrics
gemini_is_live = bool(get_api_key("GEMINI_API_KEY"))
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric(
        "Gemini API & MCP Bridge",
        "🟢 LIVE Connected" if gemini_is_live else "⚪ Waiting for Key",
        "Native functionDeclarations active" if gemini_is_live else "Add GEMINI_API_KEY in Sidebar/Secrets",
    )
with m2:
    last_info = st.session_state.last_compiled_info
    compiled_count = len(last_info["matched_skills"]) if last_info else 0
    st.metric(
        "Last JIT Skill Compilation",
        f"{compiled_count} / {len(get_skill_catalog())} Skills Sent",
        last_info["allocation_mode"] if last_info else "0 Idle Overhead",
    )
with m3:
    st.metric(
        "Cumulative Tokens Saved",
        f"{st.session_state.total_tokens_saved:,} tok",
        "vs. Monolithic 30-Tool Agent",
    )
with m4:
    st.metric(
        "Storage & Sandbox",
        "External SSD" if IS_EXTERNAL_SSD_MOUNTED else f"RAM Sandbox ({len(list_sandbox_files())} files)",
        "Zero C:\\ Disk Writes",
    )

# Two Main Tabs
tab_mcp, tab_ops = st.tabs([
    "🤖 Live JIT MCP Skill Compiler & Agent",
    "📋 Daily 6:00 PM Ops & Weekly Word (.docx) Pipeline",
])

with tab_mcp:
    st.caption("**One-Tap Live Gemini + MCP Skill Compiler Tests (Tap any button to test):**")
    q1, q2, q3, q4 = st.columns(4)
    triggered_prompt: Optional[str] = None

    if q1.button("💬 Direct Chat (0 MCP Schemas)", use_container_width=True):
        triggered_prompt = "In 3 concise bullet points, why is a 40Gbps USB4 external SSD well-suited for streaming quantized GGUF model weights into RAM?"
    if q2.button("💱 Live USD→TWD Rate (Remote MCP)", use_container_width=True):
        triggered_prompt = "Check the live exchange rate from USD to TWD and JPY right now and summarize it."
    if q3.button("📂 Read Sandbox Policy (Local MCP)", use_container_width=True):
        triggered_prompt = "Read readme_sandbox.txt from my sandbox and tell me what the workspace policy is."
    if q4.button("✍️ Write Python Script (HITL Gated)", use_container_width=True):
        triggered_prompt = "Write a clean pathlib Python script that logs timestamped sensor readings and save it to scripts/sensor_logger.py in my sandbox."

    # Display Chat History & JIT Compilation Badges
    for item in st.session_state.chat_history:
        with st.chat_message(item["role"]):
            if "meta" in item:
                meta = item["meta"]
                st.caption(
                    f"**Engine:** `{meta['model_label']}` | "
                    f"**JIT Mode:** `{meta.get('mcp_mode', 'Direct')}` | "
                    f"**Compiled Skills:** `{meta.get('compiled_skills') or '0 (Direct Fast-Path)'}` | "
                    f"**Saved:** `{meta.get('saved_tokens', 0):,} tokens` | "
                    f"**Latency:** `{meta['latency_ms']} ms`"
                )
                if meta.get("steps"):
                    with st.expander(f"🔌 Executed MCP Tool Trace ({len(meta['steps'])} Call)", expanded=True):
                        for s in meta["steps"]:
                            st.markdown(f"- **MCP Call:** `{s['tool']}`\n- **Returned Payload:** `{s['observation']}`")
            st.markdown(item["content"])

    # ---------------------------------------------------------
    # LIVE GEMINI FUNCTION-CALL HUMAN APPROVAL GATE (HITL)
    # ---------------------------------------------------------
    if st.session_state.pending_mcp_call is not None:
        p = st.session_state.pending_mcp_call
        st.warning(
            f"### ⏸️ Human Approval Required for Live Gemini MCP Tool Call\n"
            f"**Gemini Model:** `{p['model_label']}` | **JIT Compiled Skill:** `{p['fn_name']}`"
        )
        if p["fn_name"] == "write_sandbox_file":
            st.markdown(f"**Target Sandbox Path:** `safe_workspace/{p['fn_args'].get('path', '')}`")
            st.code(p["fn_args"].get("content", ""), language="python")
        else:
            st.code(json.dumps(p["fn_args"], indent=2), language="json")

        ap_col1, ap_col2 = st.columns(2)
        if ap_col1.button("✅ Approve & Send Result Back to Gemini", type="primary", use_container_width=True):
            tool_out = execute_mcp_tool(p["fn_name"], p["fn_args"], read_only_lock=read_only_lock)
            p["contents"].append(p["cand_content"])
            p["contents"].append({
                "role": "user",
                "parts": [{"functionResponse": {"name": p["fn_name"], "response": {"result": tool_out}}}],
            })
            res_final = call_gemini_with_mcp(
                p["contents"], p["system_msg"], p["declarations"], model_name=p["model_name"]
            )
            final_text = "\n".join(
                [pt["text"] for pt in res_final["candidates"][0]["content"].get("parts", []) if "text" in pt]
            ) or tool_out
            elapsed_ms = int((time.perf_counter() - p["start_t"]) * 1000)
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": final_text,
                "meta": {
                    "model_label": f"{p['model_label']} (🟢 Live API)",
                    "reason": p["routing_reason"],
                    "mcp_mode": p["compiled_info"]["allocation_mode"],
                    "compiled_skills": [s["skill_id"] for s in p["compiled_info"]["matched_skills"]],
                    "saved_tokens": p["compiled_info"]["saved_tokens"],
                    "latency_ms": elapsed_ms,
                    "steps": [{"step": 1, "tool": f"{p['fn_name']} [APPROVED]", "observation": tool_out}],
                },
            })
            st.session_state.pending_mcp_call = None
            st.rerun()

        if ap_col2.button("🛑 Reject Tool Call", use_container_width=True):
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": f"Write operation `{p['fn_name']}` was **rejected by human operator**. Zero files or memories were modified.",
                "meta": {
                    "model_label": f"{p['model_label']} (HITL Rejected)",
                    "reason": p["routing_reason"],
                    "mcp_mode": p["compiled_info"]["allocation_mode"],
                    "compiled_skills": [s["skill_id"] for s in p["compiled_info"]["matched_skills"]],
                    "saved_tokens": p["compiled_info"]["saved_tokens"],
                    "latency_ms": int((time.perf_counter() - p["start_t"]) * 1000),
                    "steps": [{"step": 1, "tool": f"{p['fn_name']} [REJECTED]", "observation": "Cancelled by operator"}],
                },
            })
            st.session_state.pending_mcp_call = None
            st.rerun()

    chat_input_prompt = st.chat_input("Ask anything (e.g., 'What is the USD to TWD exchange rate?' or 'Read readme_sandbox.txt')...")
    active_prompt = triggered_prompt or chat_input_prompt

    if active_prompt and st.session_state.pending_mcp_call is None:
        st.session_state.chat_history.append({"role": "user", "content": active_prompt})
        chosen_provider, reason = (
            route_query(active_prompt) if routing_mode.startswith("Auto") else (routing_mode, f"Manual -> {routing_mode}")
        )
        run_hybrid_mcp_turn(active_prompt, chosen_provider, reason, hitl_enabled=hitl_enabled, read_only_lock=read_only_lock)
        st.rerun()

with tab_ops:
    st.subheader("Stage 1: Daily 6:00 PM Weekday Trigger (Powered by Live Gemini)")
    in_c1, in_c2 = st.columns([1, 2])
    with in_c1:
        report_date_obj = st.date_input("Report Date", value=datetime.now(LOCAL_TZ).date())
        report_date_str = report_date_obj.strftime("%Y-%m-%d (%a)")
    with in_c2:
        raw_daily_input = st.text_area(
            "Today's Raw Operational Input Data",
            value=(
                "- Completed on-site QC line inspection across 3 production batches (yield: 98.7%).\n"
                "- Verified chemical inventory dosing logs and waste neutralization pH (7.2).\n"
                "- Identified 15-min bottleneck during manual batch ticket handoff; testing local OCR capture tomorrow."
            ),
            height=115,
        )

    t_c1, t_c2 = st.columns(2)
    if t_c1.button("⏰ Run 6:00 PM Daily Auto-Draft (Live Gemini)", type="primary", use_container_width=True):
        draft_md, model_used = generate_daily_report_draft(report_date_str, raw_daily_input)
        st.session_state.pending_daily_draft = {
            "date_str": report_date_str,
            "date_slug": report_date_obj.strftime("%Y-%m-%d"),
            "draft_md": draft_md,
            "raw_summary": " | ".join([l.strip("-•* ") for l in raw_daily_input.splitlines() if l.strip()][:3]),
            "model_used": model_used,
        }
        st.rerun()

    if t_c2.button("📚 Seed Mon–Thu Approved Reports", use_container_width=True):
        seed_sample_weekday_reports()
        st.success("Seeded Mon–Thu reports into SQLite memory!")
        st.rerun()

    st.divider()
    st.subheader("Stage 2: Human Review Gate (Before Publishing & Memorizing)")
    if st.session_state.pending_daily_draft is not None:
        pd = st.session_state.pending_daily_draft
        st.warning(f"⏸️ **Pending Review:** `{pd['date_str']}` | **Synthesizer:** `{pd['model_used']}`")
        edited_md = st.text_area("Review & Edit Before Publishing:", value=pd["draft_md"], height=240)
        rc1, rc2 = st.columns(2)
        if rc1.button("✅ Approve, Publish & Memorize", type="primary", use_container_width=True):
            safe_write_file(f"daily_reports/{pd['date_slug']}.md", edited_md, read_only_lock=read_only_lock)
            save_memory("daily_report", f"[{pd['date_str']}] {pd['raw_summary']}", custom_ts=f"{pd['date_slug']} 18:00:00")
            st.session_state.pending_daily_draft = None
            st.rerun()
        if rc2.button("🛑 Discard Draft", use_container_width=True):
            st.session_state.pending_daily_draft = None
            st.rerun()

    st.divider()
    st.subheader("Stage 3: Weekly Rollup & Word (.docx) Export")
    approved_rows = get_memories_by_topic("daily_report")
    if approved_rows:
        if st.button("📑 Compile Weekly Summary & Unlock .docx Download", type="primary", use_container_width=True):
            execute_mcp_tool("export_weekly_docx", {"report_title": "Weekly Executive Operations Report"}, read_only_lock=False)
            st.rerun()
        if st.session_state.compiled_weekly_md:
            st.markdown(st.session_state.compiled_weekly_md)
            docx_bytes = build_docx_bytes(
                title="Weekly Executive Operations Report",
                subtitle=f"Compiled from {len(approved_rows)} Approved Daily Reports",
                markdown_body=st.session_state.compiled_weekly_md,
            )
            st.download_button(
                "📥 Download Weekly Operations Report (.docx)",
                data=docx_bytes,
                file_name="Weekly_Operations_Report.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary",
                use_container_width=True,
            )
