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
            body = "\n".join