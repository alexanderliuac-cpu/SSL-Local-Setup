import os
import re
import io
import uuid
import json
import time
import socket
import zipfile
import sqlite3
import threading
import urllib.request
import urllib.parse
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
# 1. HARDWARE, TIMEZONE & MAIN-THREAD SECRET READER
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
SSD_MARKER_DIR = BASE_DIR / "memory_store"
SANDBOX_DIR = (BASE_DIR / "safe_workspace").resolve()
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

def read_secret_main_thread(key_name: str) -> str:
    """Must be called from the main Streamlit thread to safely read st.secrets."""
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
# 2. THREAD-SAFE SHARED KERNEL HUB (Streamlit + Telegram)
# =========================================================
@st.cache_resource
def get_shared_kernel() -> Dict[str, Any]:
    if IS_EXTERNAL_SSD_MOUNTED:
        db_path = SSD_MARKER_DIR / "local_memory.sqlite"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
    else:
        conn = sqlite3.connect(":memory:", check_same_thread=False)

    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(topic, fact, timestamp)"
    )
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

    return {
        "lock": threading.RLock(),
        "db": conn,
        "gemini_key": "",
        "tg_token": "",
        "tg_configured_chat_id": "",
        "virtual_fs": {
            "readme_sandbox.txt": (
                "Workspace Policy: All generated scripts and daily/weekly reports must remain "
                "strictly inside safe_workspace and require Human-in-the-Loop approval before publishing."
            )
        },
        "chat_history": [],
        "pending_mcp_call": None,
        "pending_daily_draft": None,
        "compiled_weekly_md": None,
        "total_tokens_saved": 0,
        "last_compiled_info": None,
        "hitl_enabled": True,
        "read_only_lock": False,
        "routing_mode": "Auto-Route (JIT Hybrid Compiler)",
        # Telegram Daemon State
        "tg_thread_running": False,
        "tg_webhook_cleared": False,
        "tg_bot_username": "",
        "tg_allowed_chat_id": None,
        "tg_last_update_id": 0,
        "tg_last_error": "",
        "tg_log": [],
    }

KERNEL = get_shared_kernel()

# Sync secrets from the main Streamlit thread into shared KERNEL memory immediately
_main_gemini = read_secret_main_thread("GEMINI_API_KEY")
_main_tg = read_secret_main_thread("TELEGRAM_BOT_TOKEN")
_main_chat_id = read_secret_main_thread("TELEGRAM_CHAT_ID")
with KERNEL["lock"]:
    if _main_gemini:
        KERNEL["gemini_key"] = _main_gemini
    if _main_tg:
        KERNEL["tg_token"] = _main_tg
    if _main_chat_id:
        KERNEL["tg_configured_chat_id"] = _main_chat_id


# =========================================================
# 3. SHARED MEMORY & SANDBOXED FILE GUARD
# =========================================================
STOP_WORDS = {
    "a", "an", "the", "to", "me", "in", "on", "of", "for", "and", "or", "is",
    "are", "was", "were", "be", "been", "how", "what", "why", "where", "when",
    "who", "which", "this", "that", "these", "those", "it", "its", "with", "as",
    "by", "at", "from", "up", "about", "into", "over", "after", "can", "could",
    "would", "should", "will", "do", "does", "did", "please", "write", "build",
    "make", "create", "update", "show", "tell", "give", "test", "my", "your", "i"
}

def save_memory(topic: str, fact: str, custom_ts: str = "") -> str:
    with KERNEL["lock"]:
        ts = custom_ts or datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        KERNEL["db"].execute(
            "INSERT INTO memories (topic, fact, timestamp) VALUES (?, ?, ?)",
            (topic.strip(), fact.strip(), ts),
        )
        KERNEL["db"].commit()
        return f"Indexed fact under topic '{topic}' at {ts}."

def get_memories_by_topic(topic_prefix: str) -> List[Tuple[str, str, str]]:
    with KERNEL["lock"]:
        return KERNEL["db"].execute(
            "SELECT topic, fact, timestamp FROM memories WHERE topic LIKE ? ORDER BY timestamp ASC",
            (f"{topic_prefix}%",),
        ).fetchall()

def recall_memories(query: str = "", provider_hint: str = "", limit: int = 5) -> str:
    with KERNEL["lock"]:
        raw_words = re.findall(r"[a-zA-Z0-9_]+", query.lower())
        keywords = [w for w in raw_words if len(w) > 2 and w not in STOP_WORDS]

        if provider_hint == "claude" or any(w in raw_words for w in ("code", "app", "python", "script", "word", "excel")):
            if "coding_style" not in keywords:
                keywords.insert(0, "coding_style")

        rows = []
        if keywords:
            fts_query = " OR ".join(f"{kw}*" for kw in keywords[:5])
            try:
                rows = KERNEL["db"].execute(
                    "SELECT topic, fact, timestamp FROM memories WHERE memories MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit),
                ).fetchall()
            except Exception:
                rows = []

        if not rows:
            rows = KERNEL["db"].execute(
                "SELECT topic, fact, timestamp FROM memories ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return "\n".join([f"- [{r[2]}] ({r[0]}): {r[1]}" for r in rows])

def list_sandbox_files() -> List[str]:
    if IS_EXTERNAL_SSD_MOUNTED:
        return [str(p.relative_to(SANDBOX_DIR).as_posix()) for p in SANDBOX_DIR.rglob("*") if p.is_file()]
    with KERNEL["lock"]:
        return sorted(list(KERNEL["virtual_fs"].keys()))

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
    with KERNEL["lock"]:
        return KERNEL["virtual_fs"].get(clean_rel, f"Error: '{clean_rel}' not found in virtual RAM sandbox.")

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
    with KERNEL["lock"]:
        KERNEL["virtual_fs"][clean_rel] = content
        return f"Saved {len(encoded)} bytes to Virtual RAM Sandbox: {clean_rel}"


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
MCP_SKILLS: List[Dict[str, Any]] = [
    {
        "skill_id": "read_sandbox_file",
        "origin": "mcp://local-sandbox/fs-reader",
        "mutating": False,
        "triggers": ["read", "inspect", "open", "policy", "readme", "file", "check", "contents", "sandbox"],
        "declaration": {
            "name": "read_sandbox_file",
            "description": "Reads the text content of a file inside safe_workspace.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"path": {"type": "STRING", "description": "Relative path, e.g., readme_sandbox.txt"}},
                "required": ["path"],
            },
        },
    },
    {
        "skill_id": "write_sandbox_file",
        "origin": "mcp://local-sandbox/fs-writer",
        "mutating": True,
        "triggers": ["write", "save", "create", "script", "generate", "file", "py", "md", "txt", "code", "store"],
        "declaration": {
            "name": "write_sandbox_file",
            "description": "Writes or updates a file inside safe_workspace (triggers Human Approval if HITL is active).",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Relative file path, e.g., scripts/tool.py"},
                    "content": {"type": "STRING", "description": "Complete text or code content"},
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
                "properties": {"query": {"type": "STRING", "description": "Search keyword, e.g., daily_report or hardware"}},
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
            "description": "Saves a durable fact into the local SQLite FTS5 memory database.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "topic": {"type": "STRING", "description": "Category tag, e.g., project_rule or daily_report"},
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
            "description": "Fetches live real-time currency exchange rates (e.g., USD to TWD, JPY, EUR).",
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
            "description": "Compiles all approved daily_report entries from SQLite memory into a Microsoft Word (.docx) file.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"report_title": {"type": "STRING", "description": "Document header title"}},
                "required": ["report_title"],
            },
        },
    },
]

def compile_mcp_skillset(user_prompt: str) -> Dict[str, Any]:
    clean_lower = user_prompt.lower()
    tokens = set(re.findall(r"[a-z0-9_]+", clean_lower))
    matched_skills = [s for s in MCP_SKILLS if any(t in tokens or t in clean_lower for t in s["triggers"])]

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
    try:
        if fn_name == "read_sandbox_file":
            return safe_read_file(args.get("path", "readme_sandbox.txt"))
        elif fn_name == "write_sandbox_file":
            return safe_write_file(args.get("path", "scripts/out.txt"), args.get("content", ""), read_only_lock=read_only_lock)
        elif fn_name == "query_local_memory":
            return recall_memories(args.get("query", ""), limit=6) or "No matching memories found."
        elif fn_name == "save_memory_fact":
            if read_only_lock:
                return "Blocked: Strict Read-Only Lock Mode is enabled."
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
                return f"Live Exchange Rate ({base} -> {target}): 1 {base} = {rate} {target}"
            return f"Currency '{target}' not found for base {base}."
        elif fn_name == "export_weekly_docx":
            title = args.get("report_title", "Weekly Executive Operations Report")
            rows = get_memories_by_topic("daily_report")
            body = "\n".join([f"- [{r[2]}] {r[1]}" for r in rows]) if rows else "- No approved daily reports yet."
            md = f"## Executive Summary\nCompiled {len(rows)} approved daily reports from SQLite memory.\n\n## Daily Logs\n{body}"
            with KERNEL["lock"]:
                KERNEL["compiled_weekly_md"] = md
            safe_write_file("weekly_reports/Weekly_Operations_Summary.md", md, read_only_lock=False)
            return f"Compiled '{title}' ({len(rows)} daily logs) into safe_workspace and unlocked .docx download."
        return f"Executed MCP skill '{fn_name}'."
    except Exception as err:
        return f"MCP Tool Execution Error ({fn_name}): {err}"


# =========================================================
# 6. LIVE GEMINI API & HYBRID MCP TURN CONTROLLER
# =========================================================
def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: int = 35) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def call_gemini_with_mcp(
    contents: List[Dict[str, Any]],
    system_msg: str,
    declarations: List[Dict[str, Any]],
    model_name: str = "gemini-2.5-flash",
) -> Dict[str, Any]:
    with KERNEL["lock"]:
        api_key = KERNEL["gemini_key"]
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
        if model_name != "gemini-2.5-flash":
            url_fb = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            return _post_json(url_fb, {"Content-Type": "application/json"}, payload)
        raise RuntimeError(f"Gemini HTTP {http_err.code}")

def generate_daily_report_draft(report_date: str, raw_notes: str) -> Tuple[str, str]:
    with KERNEL["lock"]:
        has_gemini = bool(KERNEL["gemini_key"])
    if has_gemini:
        try:
            prompt = (
                f"Create a structured Daily Operation Report for {report_date} (18:00 Local Time) "
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

def process_prompt_shared(user_prompt: str, source_tag: str = "Web") -> Tuple[str, Optional[Dict[str, Any]]]:
    start_t = time.perf_counter()
    compiled = compile_mcp_skillset(user_prompt)

    with KERNEL["lock"]:
        KERNEL["total_tokens_saved"] += compiled["saved_tokens"]
        KERNEL["last_compiled_info"] = compiled
        hitl_enabled = KERNEL["hitl_enabled"]
        read_only_lock = KERNEL["read_only_lock"]
        KERNEL["chat_history"].append({"role": "user", "content": f"[{source_tag}] {user_prompt}"})

    local_ctx = recall_memories(user_prompt, limit=4)
    files_list = ", ".join(list_sandbox_files())
    system_msg = (
        "You are a Local-First Hybrid AI Agent on a Surface Laptop Studio accessible via Streamlit and Telegram. "
        f"Current files in safe_workspace: [{files_list}]. "
        "Use your provided MCP tools whenever the user asks to read/write files, check live exchange rates, "
        "search/save memories, or compile weekly Word reports.\n"
        f"Injected Local Memories:\n{local_ctx}"
    )

    contents: List[Dict[str, Any]] = [{"role": "user", "parts": [{"text": user_prompt}]}]
    step_logs: List[Dict[str, Any]] = []

    try:
        res = call_gemini_with_mcp(contents, system_msg, compiled["declarations"], model_name="gemini-2.5-flash")
        cand_content = res["candidates"][0]["content"]
        parts = cand_content.get("parts", [])
        fn_call_part = next((p for p in parts if "functionCall" in p), None)

        if fn_call_part:
            fn_call = fn_call_part["functionCall"]
            fn_name = fn_call["name"]
            fn_args = fn_call.get("args", {})
            is_mutating = any(s["skill_id"] == fn_name and s.get("mutating") for s in compiled["matched_skills"])

            if hitl_enabled and is_mutating:
                pending_obj = {
                    "fn_name": fn_name,
                    "fn_args": fn_args,
                    "contents": contents,
                    "cand_content": cand_content,
                    "system_msg": system_msg,
                    "declarations": compiled["declarations"],
                    "model_name": "gemini-2.5-flash",
                    "model_label": "Gemini 2.5 Flash",
                    "compiled_info": compiled,
                    "start_t": start_t,
                    "source_tag": source_tag,
                }
                with KERNEL["lock"]:
                    KERNEL["pending_mcp_call"] = pending_obj
                return (
                    f"⏸️ Human Approval Required\nTool: {fn_name}\nArgs: {json.dumps(fn_args)[:200]}",
                    pending_obj,
                )

            tool_output = execute_mcp_tool(fn_name, fn_args, read_only_lock=read_only_lock)
            step_logs.append({"step": 1, "tool": f"{fn_name}({json.dumps(fn_args)})", "observation": tool_output})
            contents.append(cand_content)
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": fn_name, "response": {"result": tool_output}}}],
            })
            res2 = call_gemini_with_mcp(contents, system_msg, compiled["declarations"], model_name="gemini-2.5-flash")
            final_parts = res2["candidates"][0]["content"].get("parts", [])
            reply_text = "\n".join([p["text"] for p in final_parts if "text" in p]) or tool_output
        else:
            reply_text = "\n".join([p["text"] for p in parts if "text" in p])

        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        with KERNEL["lock"]:
            KERNEL["chat_history"].append({
                "role": "assistant",
                "content": reply_text,
                "meta": {
                    "model_label": f"Gemini 2.5 Flash ({source_tag} 🟢)",
                    "mcp_mode": compiled["allocation_mode"],
                    "compiled_skills": [s["skill_id"] for s in compiled["matched_skills"]],
                    "saved_tokens": compiled["saved_tokens"],
                    "latency_ms": elapsed_ms,
                    "steps": step_logs,
                },
            })
        return reply_text, None

    except Exception as err:
        err_msg = f"Diagnostic: {str(err)[:200]}"
        with KERNEL["lock"]:
            KERNEL["chat_history"].append({"role": "assistant", "content": err_msg})
        return err_msg, None

def resolve_pending_mcp_call(approved: bool, actor: str = "User") -> str:
    with KERNEL["lock"]:
        p = KERNEL["pending_mcp_call"]
        if not p:
            return "No pending tool call waiting for approval."
        read_only_lock = KERNEL["read_only_lock"]
        KERNEL["pending_mcp_call"] = None

    if not approved:
        msg = f"🛑 Tool {p['fn_name']} was REJECTED by {actor}. Zero files or memories modified."
        with KERNEL["lock"]:
            KERNEL["chat_history"].append({"role": "assistant", "content": msg})
        return msg

    tool_out = execute_mcp_tool(p["fn_name"], p["fn_args"], read_only_lock=read_only_lock)
    p["contents"].append(p["cand_content"])
    p["contents"].append({
        "role": "user",
        "parts": [{"functionResponse": {"name": p["fn_name"], "response": {"result": tool_out}}}],
    })
    try:
        res_final = call_gemini_with_mcp(p["contents"], p["system_msg"], p["declarations"], model_name=p["model_name"])
        final_text = "\n".join([pt["text"] for pt in res_final["candidates"][0]["content"].get("parts", []) if "text" in pt]) or tool_out
    except Exception:
        final_text = f"✅ Approved & Executed {p['fn_name']}:\n{tool_out}"

    with KERNEL["lock"]:
        KERNEL["chat_history"].append({
            "role": "assistant",
            "content": final_text,
            "meta": {
                "model_label": f"{p['model_label']} (Approved via {actor})",
                "mcp_mode": p["compiled_info"]["allocation_mode"],
                "compiled_skills": [s["skill_id"] for s in p["compiled_info"]["matched_skills"]],
                "saved_tokens": p["compiled_info"]["saved_tokens"],
                "latency_ms": int((time.perf_counter() - p["start_t"]) * 1000),
                "steps": [{"step": 1, "tool": f"{p['fn_name']} [APPROVED BY {actor}]", "observation": tool_out}],
            },
        })
    return final_text


# =========================================================
# 7. THREAD-SAFE TELEGRAM BOT POLLER (Sync + Background Daemon)
# =========================================================
POLL_LOCK = threading.Lock()

def tg_api_call(token: str, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    return _post_json(url, {"Content-Type": "application/json"}, payload or {}, timeout=timeout)

def tg_send_message(token: str, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None):
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tg_api_call(token, "sendMessage", payload, timeout=15)
    except Exception as e:
        with KERNEL["lock"]:
            KERNEL["tg_last_error"] = f"sendMessage error: {e}"

def tg_send_docx_document(token: str, chat_id: int, filename: str, docx_bytes: bytes, caption: str = ""):
    boundary = f"----SurfaceMCPBoundary{uuid.uuid4().hex}"
    body = io.BytesIO()

    def write_field(name: str, val: str):
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())

    write_field("chat_id", str(chat_id))
    if caption:
        write_field("caption", caption[:900])

    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode())
    body.write(b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n")
    body.write(docx_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode())

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = urllib.request.Request(
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def poll_telegram_once(poll_timeout: int = 1) -> int:
    """
    Thread-safe single poll pass. Reads KERNEL['tg_token'] from shared memory
    (never touches st.secrets directly), verifies getMe, clears any old webhook,
    and processes all queued messages.
    """
    with KERNEL["lock"]:
        token = KERNEL["tg_token"].strip()
    if not token:
        return 0

    if not POLL_LOCK.acquire(blocking=False):
        return 0

    processed_count = 0
    try:
        # 1. Verify bot identity & clear any old webhook once
        if not KERNEL["tg_bot_username"]:
            me = tg_api_call(token, "getMe", timeout=10)
            if me.get("ok"):
                with KERNEL["lock"]:
                    KERNEL["tg_bot_username"] = f"@{me['result'].get('username', 'bot')}"
                    KERNEL["tg_last_error"] = ""
            if not KERNEL["tg_webhook_cleared"]:
                tg_api_call(token, "deleteWebhook", {"drop_pending_updates": False}, timeout=10)
                with KERNEL["lock"]:
                    KERNEL["tg_webhook_cleared"] = True

        # 2. Fetch queued updates from Telegram
        offset = KERNEL["tg_last_update_id"] + 1
        updates = tg_api_call(
            token,
            "getUpdates",
            {"offset": offset, "timeout": poll_timeout},
            timeout=poll_timeout + 8,
        )

        for upd in updates.get("result", []):
            processed_count += 1
            with KERNEL["lock"]:
                KERNEL["tg_last_update_id"] = upd["update_id"]

            # Handle Inline Button Clicks
            if "callback_query" in upd:
                cb = upd["callback_query"]
                chat_id = cb["message"]["chat"]["id"]
                data = cb.get("data", "")
                tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb["id"]}, timeout=10)

                if data == "mcp_approve":
                    out = resolve_pending_mcp_call(True, actor="Telegram")
                    tg_send_message(token, chat_id, f"✅ Approved!\n\n{out}")
                elif data == "mcp_reject":
                    out = resolve_pending_mcp_call(False, actor="Telegram")
                    tg_send_message(token, chat_id, out)
                elif data == "daily_approve":
                    with KERNEL["lock"]:
                        pd = KERNEL["pending_daily_draft"]
                        KERNEL["pending_daily_draft"] = None
                    if pd:
                        safe_write_file(f"daily_reports/{pd['date_slug']}.md", pd["draft_md"], read_only_lock=False)
                        save_memory("daily_report", f"[{pd['date_str']}] {pd['raw_summary']}", custom_ts=f"{pd['date_slug']} 18:00:00")
                        tg_send_message(token, chat_id, f"✅ Published {pd['date_slug']}.md to sandbox and indexed into SQLite memory!")
                    else:
                        tg_send_message(token, chat_id, "No pending daily report draft found.")
                elif data == "daily_reject":
                    with KERNEL["lock"]:
                        KERNEL["pending_daily_draft"] = None
                    tg_send_message(token, chat_id, "🛑 Daily report draft discarded.")
                continue

            # Handle Text Messages & Slash Commands
            msg = upd.get("message")
            if not msg or "text" not in msg:
                continue

            chat_id = msg["chat"]["id"]
            text = msg["text"].strip()

            with KERNEL["lock"]:
                cfg_id = KERNEL["tg_configured_chat_id"]
                if cfg_id:
                    KERNEL["tg_allowed_chat_id"] = int(cfg_id)
                elif KERNEL["tg_allowed_chat_id"] is None:
                    KERNEL["tg_allowed_chat_id"] = chat_id
                allowed_id = KERNEL["tg_allowed_chat_id"]

            if chat_id != allowed_id:
                tg_send_message(token, chat_id, f"🔒 Unauthorized Chat ID ({chat_id}). Bot is locked to owner.")
                continue

            with KERNEL["lock"]:
                KERNEL["tg_log"].append(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] TG ({chat_id}): {text[:70]}")

            if text.startswith("/start") or text.startswith("/help"):
                welcome = (
                    f"🤖 Surface Hybrid MCP Kernel Linked!\n"
                    f"Pinned Owner Chat ID: {chat_id}\n\n"
                    f"Commands:\n"
                    f"• Send any prompt → Runs through JIT MCP Skill Compiler + Live Gemini\n"
                    f"• /daily <notes> → Drafts 6:00 PM Daily Ops Report with Approve/Reject buttons\n"
                    f"• /weekly → Compiles approved daily reports & sends .docx Word file here\n"
                    f"• /memory → Lists stored SQLite FTS5 memories\n"
                    f"• /files → Lists files in safe_workspace"
                )
                tg_send_message(token, chat_id, welcome)

            elif text.startswith("/memory"):
                tg_send_message(token, chat_id, f"🧠 Active SQLite Memories:\n{recall_memories(limit=8)}")

            elif text.startswith("/files"):
                tg_send_message(token, chat_id, "📂 Sandbox Files:\n" + "\n".join([f"• {f}" for f in list_sandbox_files()]))

            elif text.startswith("/daily"):
                raw_notes = text[6:].strip() or (
                    "- Completed on-site QC line inspection across 3 batches (yield: 98.7%).\n"
                    "- Verified chemical dosing logs and waste neutralization pH (7.2)."
                )
                now_dt = datetime.now(LOCAL_TZ)
                d_str = now_dt.strftime("%Y-%m-%d (%a)")
                d_slug = now_dt.strftime("%Y-%m-%d")
                draft_md, model_used = generate_daily_report_draft(d_str, raw_notes)
                with KERNEL["lock"]:
                    KERNEL["pending_daily_draft"] = {
                        "date_str": d_str,
                        "date_slug": d_slug,
                        "draft_md": draft_md,
                        "raw_summary": " | ".join([l.strip("-•* ") for l in raw_notes.splitlines() if l.strip()][:3]),
                        "model_used": model_used,
                    }
                kb = {
                    "inline_keyboard": [[
                        {"text": "✅ Approve & Memorize", "callback_data": "daily_approve"},
                        {"text": "🛑 Discard Draft", "callback_data": "daily_reject"},
                    ]]
                }
                tg_send_message(token, chat_id, f"📋 Pending 6:00 PM Daily Report ({d_str}):\n\n{draft_md}", reply_markup=kb)

            elif text.startswith("/weekly"):
                execute_mcp_tool("export_weekly_docx", {"report_title": "Weekly Executive Operations Report"}, read_only_lock=False)
                with KERNEL["lock"]:
                    weekly_md = KERNEL["compiled_weekly_md"] or "No daily reports yet."
                rows = get_memories_by_topic("daily_report")
                docx_bytes = build_docx_bytes(
                    title="Weekly Executive Operations Report",
                    subtitle=f"Compiled from {len(rows)} Approved Daily Reports via Telegram",
                    markdown_body=weekly_md,
                )
                fname = f"Weekly_Operations_Report_{datetime.now(LOCAL_TZ).strftime('%Y%m%d')}.docx"
                tg_send_docx_document(token, chat_id, fname, docx_bytes, caption=f"📑 Compiled from {len(rows)} approved daily reports.")

            else:
                reply_text, pending_hitl = process_prompt_shared(text, source_tag="Telegram")
                if pending_hitl:
                    kb = {
                        "inline_keyboard": [[
                            {"text": "✅ Approve Tool Write", "callback_data": "mcp_approve"},
                            {"text": "🛑 Reject Write", "callback_data": "mcp_reject"},
                        ]]
                    }
                    tg_send_message(token, chat_id, reply_text, reply_markup=kb)
                else:
                    tg_send_message(token, chat_id, reply_text)

    except Exception as err:
        with KERNEL["lock"]:
            KERNEL["tg_last_error"] = str(err)[:180]
    finally:
        POLL_LOCK.release()

    return processed_count

def telegram_polling_worker():
    """Continuous background poller that reads token strictly from thread-safe KERNEL dict."""
    while True:
        with KERNEL["lock"]:
            has_token = bool(KERNEL["tg_token"])
        if not has_token:
            time.sleep(2)
            continue
        poll_telegram_once(poll_timeout=6)
        time.sleep(0.5)

with KERNEL["lock"]:
    if not KERNEL["tg_thread_running"]:
        t = threading.Thread(target=telegram_polling_worker, daemon=True)
        t.start()
        KERNEL["tg_thread_running"] = True


# =========================================================
# 8. STREAMLIT UI & SYNCHRONOUS TELEGRAM HANDSHAKE
# =========================================================
st.set_page_config(page_title="Surface Hybrid MCP + Telegram Hub", layout="wide")
st.title("Surface Laptop Studio — Hybrid MCP Kernel + Telegram Bridge")

with st.sidebar:
    st.header("1. API Keys & Telegram Bot Token")
    with st.expander("🔑 Configure Keys (Secrets or Session RAM)", expanded=not bool(KERNEL["tg_token"])):
        g_key = st.text_input("GEMINI_API_KEY", value=KERNEL["gemini_key"], type="password")
        tg_tok = st.text_input("TELEGRAM_BOT_TOKEN", value=KERNEL["tg_token"], type="password")
        if g_key and g_key != KERNEL["gemini_key"]:
            with KERNEL["lock"]:
                KERNEL["gemini_key"] = g_key
            os.environ["GEMINI_API_KEY"] = g_key
        if tg_tok and tg_tok != KERNEL["tg_token"]:
            with KERNEL["lock"]:
                KERNEL["tg_token"] = tg_tok
                KERNEL["tg_bot_username"] = ""
                KERNEL["tg_webhook_cleared"] = False
            os.environ["TELEGRAM_BOT_TOKEN"] = tg_tok

    if st.button("⚡ Poll Telegram Inbox & Sync UI Now", type="primary", use_container_width=True):
        poll_telegram_once(poll_timeout=1)
        st.rerun()

    KERNEL["hitl_enabled"] = st.toggle("⏸️ Human Approval for File/Memory Writes", value=KERNEL["hitl_enabled"])
    KERNEL["read_only_lock"] = st.toggle("🔒 Strict Read-Only Lock Mode", value=KERNEL["read_only_lock"])

    st.divider()
    st.header("2. Telegram Live Activity Log")
    if KERNEL["tg_bot_username"]:
        st.success(f"Connected to **{KERNEL['tg_bot_username']}** | Owner ID: `{KERNEL['tg_allowed_chat_id'] or 'Ready for /start'}`")
    elif KERNEL["tg_token"]:
        st.info("Token detected! Connecting to Telegram API...")
    else:
        st.warning("No `TELEGRAM_BOT_TOKEN` found in Secrets or Sidebar yet.")

    if KERNEL["tg_last_error"]:
        st.error(f"Telegram API Note: {KERNEL['tg_last_error']}")
    if KERNEL["tg_log"]:
        st.code("\n".join(KERNEL["tg_log"][-6:]), language="text")

    st.divider()
    st.header("3. Sandboxed File Explorer")
    sandbox_files = list_sandbox_files()
    selected_file = st.selectbox("Files in safe_workspace", sandbox_files)
    if selected_file and st.button("Inspect Selected File"):
        st.code(safe_read_file(selected_file), language="markdown")

# Run an immediate 1-second synchronous poll on page load so queued /start messages process right away
if KERNEL["tg_token"] and not KERNEL["tg_bot_username"]:
    poll_telegram_once(poll_timeout=1)

# Top Telemetry Bar
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric(
        "Gemini API Status",
        "🟢 LIVE Connected" if KERNEL["gemini_key"] else "⚪ Waiting for Key",
        "Native MCP Function Calling",
    )
with m2:
    st.metric(
        "Telegram Bot Bridge",
        f"🟢 {KERNEL['tg_bot_username']}" if KERNEL["tg_bot_username"] else ("🟡 Connecting..." if KERNEL["tg_token"] else "⚪ Waiting for Token"),
        f"Pinned ID: {KERNEL['tg_allowed_chat_id']}" if KERNEL["tg_allowed_chat_id"] else "Ready for /start",
    )
with m3:
    st.metric("Cumulative Tokens Saved", f"{KERNEL['total_tokens_saved']:,} tok", "JIT MCP Skill Compiler")
with m4:
    st.metric("Approved Daily Reports", f"{len(get_memories_by_topic('daily_report'))} in SQLite", f"{len(list_sandbox_files())} Sandbox Files")

tab_mcp, tab_ops = st.tabs([
    "🤖 Shared Chat (Web + Telegram) & JIT MCP Compiler",
    "📋 Daily 6:00 PM Ops & Weekly Word (.docx) Pipeline",
])

with tab_mcp:
    st.caption("**Messages sent in Telegram appear here automatically (or tap '⚡ Poll Telegram Inbox & Sync UI Now' in the sidebar):**")
    for item in KERNEL["chat_history"]:
        with st.chat_message(item["role"]):
            if "meta" in item:
                meta = item["meta"]
                st.caption(
                    f"**Engine:** `{meta['model_label']}` | **JIT Mode:** `{meta.get('mcp_mode')}` | "
                    f"**Skills:** `{meta.get('compiled_skills') or '0'}` | **Saved:** `{meta.get('saved_tokens', 0):,} tok`"
                )
            st.markdown(item["content"])

    if KERNEL["pending_mcp_call"] is not None:
        p = KERNEL["pending_mcp_call"]
        st.warning(
            f"### ⏸️ Pending Human Approval (Triggered via {p['source_tag']})\n"
            f"**Skill:** `{p['fn_name']}` — *Approve right here OR tap **[✅ Approve Tool Write]** inside your Telegram chat!*"
        )
        st.code(json.dumps(p["fn_args"], indent=2), language="json")
        c_ap, c_rj = st.columns(2)
        if c_ap.button("✅ Approve Write (Web)", type="primary", use_container_width=True):
            resolve_pending_mcp_call(True, actor="Web UI")
            st.rerun()
        if c_rj.button("🛑 Reject Write (Web)", use_container_width=True):
            resolve_pending_mcp_call(False, actor="Web UI")
            st.rerun()

    if web_prompt := st.chat_input("Send a prompt from Web (or message your bot in Telegram)..."):
        process_prompt_shared(web_prompt, source_tag="Web")
        st.rerun()

with tab_ops:
    st.subheader("Daily 6:00 PM Ops & Weekly Word (.docx) — Synced with Telegram")
    st.info("💡 **Try it in Telegram:** Send `/daily` to draft today's report with inline Approve/Reject buttons, or send `/weekly` to receive the `.docx` Word file directly in your Telegram chat!")

    if KERNEL["pending_daily_draft"] is not None:
        pd = KERNEL["pending_daily_draft"]
        st.warning(f"⏸️ **Pending Daily Report Review:** `{pd['date_str']}` (Approve here or via Telegram inline button)")
        edited_md = st.text_area("Review & Edit Draft:", value=pd["draft_md"], height=220)
        rc1, rc2 = st.columns(2)
        if rc1.button("✅ Approve, Publish & Memorize", type="primary", use_container_width=True):
            safe_write_file(f"daily_reports/{pd['date_slug']}.md", edited_md, read_only_lock=False)
            save_memory("daily_report", f"[{pd['date_str']}] {pd['raw_summary']}", custom_ts=f"{pd['date_slug']} 18:00:00")
            if KERNEL["tg_allowed_chat_id"] and KERNEL["tg_token"]:
                tg_send_message(KERNEL["tg_token"], KERNEL["tg_allowed_chat_id"], f"✅ Daily Report {pd['date_slug']} approved via Web UI!")
            KERNEL["pending_daily_draft"] = None
            st.rerun()
        if rc2.button("🛑 Discard Draft", use_container_width=True):
            KERNEL["pending_daily_draft"] = None
            st.rerun()
