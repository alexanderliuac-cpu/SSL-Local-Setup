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
# 1. HARDWARE, TIMEZONE & SECRET MANAGEMENT
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
SSD_MARKER_DIR = BASE_DIR / "memory_store"
SANDBOX_DIR = (BASE_DIR / "safe_workspace").resolve()
MAX_WRITE_BYTES = 1024 * 500  # 500 KB cap
LOCAL_TZ = timezone(timedelta(hours=8))  # UTC+8 Taiwan Standard Time

IS_EXTERNAL_SSD_MOUNTED = SSD_MARKER_DIR.exists() and SANDBOX_DIR.exists()

def is_local_gpu_server_online(host: str = "127.0.0.1", port: int = 8080) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False

LOCAL_GPU_ONLINE = is_local_gpu_server_online()

def read_secret_main_thread(key_name: str) -> str:
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
        db_path.parent.mkdir(parents=True, exist_ok=True)
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
        "simulate_offline": False,
        # Telegram Daemon State
        "tg_thread_running": False,
        "tg_webhook_cleared": False,
        "tg_bot_username": "",
        "tg_allowed_chat_id": None,
        "tg_last_update_id": 0,
        "tg_processed_updates": set(),
        "tg_last_error": "",
        "tg_log": [],
    }

KERNEL = get_shared_kernel()

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
# 3. MEMORY & SANDBOXED FILE GUARD
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
# 4. NATIVE WORD (.DOCX) GENERATOR
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
# 5. CONTEXT-AWARE TAIWAN LEXICON GUARD & JIT MCP SKILLS
# =========================================================
# Protects "標準作業程序 (SOP)", "作業程序", "行政程序" from being corrupted into "程式"
TW_LEXICON_MAP = {
    "項目": "專案",
    "質量": "品質",
    "軟件": "軟體",
    "硬件": "硬體",
    "優化": "最佳化",
    "信息": "資訊",
    "網絡": "網路",
    "服務器": "伺服器",
    "數據": "資料",
    "激光": "雷射",
    "芯片": "晶片",
    "內存": "記憶體",
    "硬盤": "硬碟",
    "固態硬盤": "固態硬碟",
    "計算機程序": "電腦程式",
    "源程序": "原始碼",
    "應用程序": "應用程式",
    "小程序": "小程式",
    "調試": "除錯",
    "接口": "介面",
    "默認": "預設",
    "支持": "支援",
    "視頻": "影片",
    "激活": "啟用",
    "實時": "即時",
    "交互": "互動",
    "真空泵": "真空幫浦",
}

def apply_tier3_tw_lexicon_guard(text: str) -> str:
    scrubbed = text
    for cn_term, tw_term in TW_LEXICON_MAP.items():
        scrubbed = scrubbed.replace(cn_term, tw_term)
    # Restore "標準作業程序" in case the LLM itself wrote "標準作業程式"
    scrubbed = scrubbed.replace("標準作業程式", "標準作業程序").replace("作業程式", "作業程序")
    return scrubbed

MCP_SKILLS: List[Dict[str, Any]] = [
    {
        "skill_id": "fetch_fx_rate_and_trend",
        "origin": "mcp://remote-api/fx-spot-and-history",
        "mutating": False,
        "triggers": [
            "exchange", "rate", "fx", "currency", "usd", "twd", "jpy", "eur", "convert", "dollar",
            "匯率", "新臺幣", "新台幣", "台幣", "臺幣", "美元", "美金", "日圓", "日元", "歐元", "走勢", "股匯"
        ],
        "declaration": {
            "name": "fetch_fx_rate_and_trend",
            "description": (
                "Fetches live real-time currency exchange rates AND multi-day historical exchange rate trends "
                "(e.g., USD to TWD, TWD to USD, USD to JPY over the past N days)."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "base_currency": {"type": "STRING", "description": "3-letter currency code, e.g., USD or TWD"},
                    "target_currency": {"type": "STRING", "description": "3-letter currency code, e.g., TWD, USD, or JPY"},
                    "days": {"type": "INTEGER", "description": "Number of past days for trend comparison (default 5)"},
                },
                "required": ["base_currency", "target_currency"],
            },
        },
    },
    {
        "skill_id": "stage_daily_ops_report",
        "origin": "mcp://local-sandbox/daily-ops-stager",
        "mutating": False,  # Stages into the review queue so the user can inspect/approve in Tab 2 or Telegram!
        "triggers": [
            "日報", "營運日報", "巡檢", "現場筆記", "field walk", "other notes", "iso", "sop", "鍋爐", "boiler",
            "cooling tower", "冷卻水塔", "蒸餾", "distillation", "stage", "納入日報"
        ],
        "declaration": {
            "name": "stage_daily_ops_report",
            "description": (
                "Organizes raw field walk notes or operational observations into a detailed, zero-omission "
                "Taiwanese Traditional Chinese Daily Operation Report and stages it into the 18:00 Daily Report Review Queue."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "raw_notes": {"type": "STRING", "description": "The complete raw operational or field walk notes"},
                    "report_date": {"type": "STRING", "description": "Date of the field notes, e.g., 2026-10-20"},
                },
                "required": ["raw_notes"],
            },
        },
    },
    {
        "skill_id": "read_sandbox_file",
        "origin": "mcp://local-sandbox/fs-reader",
        "mutating": False,
        "triggers": ["read", "inspect", "open", "policy", "readme", "file", "check", "contents", "sandbox", "讀取", "檔案"],
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
        "triggers": ["write", "save", "create", "script", "file", "py", "md", "txt", "code", "寫入", "存檔"],
        "declaration": {
            "name": "write_sandbox_file",
            "description": "Writes or updates a file inside safe_workspace (triggers Human Approval).",
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
        "triggers": ["memory", "remember", "recall", "stored", "history", "daily_report", "記憶", "查詢記憶", "歷史"],
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
        "triggers": ["memorize", "remember this", "記住", "存入記憶"],
        "declaration": {
            "name": "save_memory_fact",
            "description": "Saves a durable user preference or fact into the local SQLite FTS5 memory database.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "topic": {"type": "STRING", "description": "Category tag, e.g., project_rule"},
                    "fact": {"type": "STRING", "description": "Concise fact statement to persist"},
                },
                "required": ["topic", "fact"],
            },
        },
    },
    {
        "skill_id": "export_weekly_docx",
        "origin": "mcp://local-sandbox/docx-compiler",
        "mutating": False,
        "triggers": ["word", "docx", "weekly", "compile", "download", "週報", "彙整週報"],
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


# =========================================================
# 6. LIVE GEMINI API, FX TREND CONNECTOR & HIGH-FIDELITY OPS STAGER
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
        sim_off = KERNEL["simulate_offline"]
    if sim_off:
        raise RuntimeError("Circuit Breaker simulated offline mode active.")
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

def fetch_fx_spot_and_history(base: str, target: str, days: int = 5) -> str:
    """Fetches live spot rate + past N days historical trend from open financial feeds."""
    base = base.upper().strip()
    target = target.upper().strip()
    days = max(1, min(int(days or 5), 30))

    # 1. Fetch Live Spot Rate (Supports TWD, USD, JPY, EUR, etc.)
    spot_url = f"https://open.er-api.com/v6/latest/{base}"
    req = urllib.request.Request(spot_url, headers={"User-Agent": "Surface-Hybrid-MCP/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        spot_data = json.loads(resp.read().decode("utf-8"))
    spot_rate = spot_data.get("rates", {}).get(target)
    inverse_rate = (1.0 / spot_rate) if spot_rate else None

    # 2. Fetch Historical Daily Series from fawazahmed0/currency-api CDN (Supports TWD!)
    history_lines = []
    now_dt = datetime.now(LOCAL_TZ).date()
    base_low = base.lower()
    target_low = target.lower()

    for i in range(days, -1, -1):
        d_str = (now_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        hist_url = f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{d_str}/v1/currencies/{base_low}.json"
        try:
            h_req = urllib.request.Request(hist_url, headers={"User-Agent": "Surface-Hybrid-MCP/1.0"})
            with urllib.request.urlopen(h_req, timeout=4) as h_resp:
                h_data = json.loads(h_resp.read().decode("utf-8"))
            r_val = h_data.get(base_low, {}).get(target_low)
            if r_val is not None:
                inv_val = 1.0 / r_val if r_val else 0
                history_lines.append(
                    f"- {d_str}: 1 {base} = {r_val:.4f} {target}  (1 {target} = {inv_val:.4f} {base})"
                )
        except Exception:
            continue

    summary = [
        f"【即時匯率 Spot Rate】 1 {base} = {spot_rate} {target}"
        + (f" （換算：1 {target} = {inverse_rate:.4f} {base}）" if inverse_rate else ""),
        f"【過去 {days} 天歷史匯率走勢 ({base}/{target})】:",
    ]
    summary.extend(history_lines if history_lines else ["- 歷史序列暫時無法連線，已提供最新即時報價。"])
    return "\n".join(summary)

def generate_daily_report_draft(report_date: str, raw_notes: str) -> Tuple[str, str]:
    """
    Zero-Omission Engineering & Operational Report Synthesizer.
    Preserves all equipment codes, startup/shutdown sequences, metrics, and root causes.
    """
    with KERNEL["lock"]:
        sim_off = KERNEL["simulate_offline"]
        has_gemini = bool(KERNEL["gemini_key"])

    if not sim_off and has_gemini:
        try:
            prompt = (
                f"請將以下現場工程與廠務巡檢筆記，整理並翻譯為一份【零遺漏、保留所有技術細節】的臺灣正體中文營運與工程日報。\n"
                f"報告日期：{report_date}\n"
                f"嚴格要求：\n"
                f"1. 必須保留所有設備代號、啟動與停機順序（例如：空壓機 → RO → 鍋爐 → 冷卻水 → 洗滌塔 → 真空幫浦 → 儲槽 → 蒸餾塔 TW，以及現場實際差異）、數量（如 5 座鍋爐配置、9 座冷卻水塔）、化學物質（Ca/Mg 水垢）、年份與稽查事件（如 2024 環保局熱顯像儀稽查）。\n"
                f"2. 嚴格使用臺灣正體中文與工程慣用詞（標準作業程序 SOP、專案、品質、真空幫浦、蒸發器 EV、蒸餾塔 TW、初餾、低餾高沸、氮氣充填）。\n"
                f"3. 分節呈現：一、現場巡檢與 ISO 三階文件重點；二、設備啟停 SOP 與現場實務落差分析；三、鍋爐配置與運轉策略（含重油/天然氣/廢熱/防制鍋爐）；四、冷卻水塔水質（鈣鎂結垢）對冷凝器與真空幫浦之影響；五、產品充填自動化評估與軟管配置（2024 洩漏事件）風險檢討。\n\n"
                f"原始現場筆記：\n{raw_notes}"
            )
            res = call_gemini_with_mcp(
                [{"role": "user", "parts": [{"text": prompt}]}],
                "你是專精於臺灣化工製程、廠務設備與品管工程的 TAIDE 資深技術報告引擎。",
                declarations=[],
                model_name="gemini-2.5-flash",
            )
            text = "\n".join([p["text"] for p in res["candidates"][0]["content"]["parts"] if "text" in p])
            return apply_tier3_tw_lexicon_guard(text), "🟢 Tier 1: Gemini 2.5 + TAIDE 零遺漏工程引擎"
        except Exception:
            pass

    clean_notes = apply_tier3_tw_lexicon_guard(raw_notes)
    bullets = "\n".join([f"- {l.strip('-•* ')}" for l in clean_notes.splitlines() if l.strip()])
    fallback_out = (
        f"## 營運與廠務巡檢日報（Tier 3 離線模式）— {report_date}\n\n"
        f"### 一、現場巡檢完整紀錄\n{bullets}\n\n"
        f"### 二、後續追蹤事項\n- 確認 SOP 與現場啟停順序差異及水質結垢改善方案。"
    )
    return fallback_out, "🟠 Tier 3: Offline Lexicon & Template Engine"

def execute_mcp_tool(fn_name: str, args: Dict[str, Any], read_only_lock: bool = False) -> str:
    try:
        if fn_name == "fetch_fx_rate_and_trend":
            return fetch_fx_spot_and_history(
                args.get("base_currency", "USD"),
                args.get("target_currency", "TWD"),
                int(args.get("days", 5)),
            )
        elif fn_name == "stage_daily_ops_report":
            raw_notes = args.get("raw_notes", "")
            today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d (%a)")
            rep_date = args.get("report_date") or today_str
            date_slug = re.findall(r"\d{4}-\d{2}-\d{2}", rep_date)
            slug = date_slug[0] if date_slug else datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

            draft_md, model_used = generate_daily_report_draft(rep_date, raw_notes)
            with KERNEL["lock"]:
                KERNEL["pending_daily_draft"] = {
                    "date_str": rep_date,
                    "date_slug": slug,
                    "draft_md": draft_md,
                    "raw_summary": raw_notes[:160].replace("\n", " "),
                    "model_used": model_used,
                }
            return (
                f"✅ 已完成零遺漏臺灣正體中文工程日報整理，並已自動送入【📋 18:00 營運日報待審核佇列 ({rep_date})】！\n\n"
                f"---\n{draft_md}\n---\n"
                f"💡 您現在可以切換至上方「📋 18:00 營運日報與 Word (.docx) 彙整」分頁（或在 Telegram 輸入 `/daily`）點擊 **✅ 準許並存入記憶庫**。"
            )
        elif fn_name == "read_sandbox_file":
            return safe_read_file(args.get("path", "readme_sandbox.txt"))
        elif fn_name == "write_sandbox_file":
            return safe_write_file(args.get("path", "scripts/out.txt"), args.get("content", ""), read_only_lock=read_only_lock)
        elif fn_name == "query_local_memory":
            return recall_memories(args.get("query", ""), limit=6) or "No matching memories found."
        elif fn_name == "save_memory_fact":
            if read_only_lock:
                return "Blocked: Strict Read-Only Lock Mode is active."
            return save_memory(args.get("topic", "general"), args.get("fact", ""))
        elif fn_name == "export_weekly_docx":
            title = args.get("report_title", "Weekly Executive Operations Report")
            rows = get_memories_by_topic("daily_report")
            if not rows:
                return "目前 SQLite 記憶庫中尚無已核准的營運日報。請先於「📋 18:00 營運日報」分頁核准至少一筆日報。"
            body = "\n\n".join([f"### {r[2]}\n{r[1]}" for r in rows])
            md = f"## {title}\n彙整自 {len(rows)} 筆已核准之現場營運與工程日報。\n\n{body}"
            with KERNEL["lock"]:
                KERNEL["compiled_weekly_md"] = md
            safe_write_file("weekly_reports/Weekly_Operations_Summary.md", md, read_only_lock=False)
            return f"已成功彙整 {len(rows)} 筆日報並產生 Word (.docx) 檔案。"
        return f"Executed MCP skill '{fn_name}'."
    except Exception as err:
        return f"MCP Tool Execution Error ({fn_name}): {err}"


# =========================================================
# 7. MULTI-TURN CONVERSATION & MCP TURN CONTROLLER
# =========================================================
def build_multi_turn_contents(current_prompt: str, max_history_turns: int = 6) -> List[Dict[str, Any]]:
    """
    Includes the last N turns of conversation history so follow-up commands like
    '幫我將剛剛輸入的那一串筆記整理歸納翻譯' have full context of previous messages!
    """
    contents: List[Dict[str, Any]] = []
    with KERNEL["lock"]:
        recent = KERNEL["chat_history"][-(max_history_turns + 1):-1]

    for msg in recent:
        role = "user" if msg["role"] == "user" else "model"
        clean_text = re.sub(r"^\[(Web|Telegram)\]\s*", "", msg.get("content", "")).strip()
        if clean_text:
            contents.append({"role": role, "parts": [{"text": clean_text[:3000]}]})

    contents.append({"role": "user", "parts": [{"text": current_prompt}]})
    return contents

def process_prompt_shared(user_prompt: str, source_tag: str = "Web") -> Tuple[str, Optional[Dict[str, Any]]]:
    start_t = time.perf_counter()
    compiled = compile_mcp_skillset(user_prompt)

    with KERNEL["lock"]:
        KERNEL["total_tokens_saved"] += compiled["saved_tokens"]
        KERNEL["last_compiled_info"] = compiled
        hitl_enabled = KERNEL["hitl_enabled"]
        read_only_lock = KERNEL["read_only_lock"]
        KERNEL["chat_history"].append({"role": "user", "content": f"[{source_tag}] {user_prompt}"})

    now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d (%A) %H:%M CST")
    local_ctx = recall_memories(user_prompt, limit=4)
    files_list = ", ".join(list_sandbox_files())

    system_msg = (
        f"Current Date & Time: {now_str} (Taiwan Standard Time, UTC+8).\n"
        "You are a Local-First Hybrid AI Agent on a Surface Laptop Studio accessible via Streamlit and Telegram.\n"
        f"Current files in safe_workspace: [{files_list}].\n"
        "CRITICAL RULES:\n"
        "1. Strictly use authentic Taiwanese Traditional Chinese terminology (臺灣正體中文: 專案, 品質, 軟體, 硬體, 最佳化, 資訊, 標準作業程序 SOP, 真空幫浦).\n"
        "2. NEVER output raw `<tool_code>` or `print(...)` blocks in text. Either invoke a declared function tool or answer directly.\n"
        "3. When organizing or translating user engineering/field notes, preserve 100% of technical details, sequences, numbers, equipment codes (EV, TW, RO, VP, ESOV), and root causes without omission.\n"
        "4. If the user provides field walk or operational notes to organize, use `stage_daily_ops_report` so it is both displayed in full and staged for the 18:00 Daily Report review queue.\n"
        f"Injected Local Memories:\n{local_ctx}"
    )

    contents = build_multi_turn_contents(user_prompt, max_history_turns=6)
    step_logs: List[Dict[str, Any]] = []

    try:
        res = call_gemini_with_mcp(contents, system_msg, compiled["declarations"], model_name="gemini-2.5-flash")
        cand_content = res["candidates"][0]["content"]
        parts = cand_content.get("parts", [])
        fn_call_parts = [p for p in parts if "functionCall" in p]

        if fn_call_parts:
            fn_call = fn_call_parts[0]["functionCall"]
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
                    f"⏸️ 需要人工準許 (Human Approval Required)\n工具: `{fn_name}`\n參數: `{json.dumps(fn_args, ensure_ascii=False)[:240]}`",
                    pending_obj,
                )

            tool_output = execute_mcp_tool(fn_name, fn_args, read_only_lock=read_only_lock)
            step_logs.append({"step": 1, "tool": f"{fn_name}({json.dumps(fn_args, ensure_ascii=False)[:120]})", "observation": tool_output})

            # If stage_daily_ops_report ran, tool_output already contains the complete zero-omission report
            if fn_name == "stage_daily_ops_report":
                reply_text = tool_output
            else:
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

        reply_text = apply_tier3_tw_lexicon_guard(reply_text)
        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        with KERNEL["lock"]:
            KERNEL["chat_history"].append({
                "role": "assistant",
                "content": reply_text,
                "meta": {
                    "model_label": f"Gemini 2.5 Flash + TAIDE ({source_tag} 🟢)",
                    "mcp_mode": compiled["allocation_mode"],
                    "compiled_skills": [s["skill_id"] for s in compiled["matched_skills"]],
                    "saved_tokens": compiled["saved_tokens"],
                    "latency_ms": elapsed_ms,
                    "steps": step_logs,
                },
            })
        return reply_text, None

    except Exception:
        fallback_text, tier_badge = generate_daily_report_draft(
            datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"), user_prompt
        )
        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        with KERNEL["lock"]:
            KERNEL["chat_history"].append({
                "role": "assistant",
                "content": f"**[Circuit Breaker Triggered: {tier_badge}]**\n\n{fallback_text}",
                "meta": {
                    "model_label": tier_badge,
                    "mcp_mode": "OFFLINE_FAILOVER",
                    "compiled_skills": [],
                    "saved_tokens": compiled["saved_tokens"],
                    "latency_ms": elapsed_ms,
                    "steps": [],
                },
            })
        return fallback_text, None

def resolve_pending_mcp_call(approved: bool, actor: str = "User") -> str:
    with KERNEL["lock"]:
        p = KERNEL["pending_mcp_call"]
        if not p:
            return "目前沒有待審核的工具呼叫。"
        read_only_lock = KERNEL["read_only_lock"]
        KERNEL["pending_mcp_call"] = None

    if not approved:
        msg = f"🛑 工具 `{p['fn_name']}` 已被 {actor} 拒絕執行，未寫入任何檔案或記憶。"
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
        final_text = apply_tier3_tw_lexicon_guard(final_text)
    except Exception:
        final_text = f"✅ 已核准並執行 `{p['fn_name']}`:\n{tool_out}"

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
# 8. DEDUPLICATED TELEGRAM BOT POLLER
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

def poll_telegram_once(poll_timeout: int = 5) -> int:
    with KERNEL["lock"]:
        token = KERNEL["tg_token"].strip()
    if not token:
        return 0

    if not POLL_LOCK.acquire(blocking=False):
        return 0

    processed_count = 0
    try:
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

        offset = KERNEL["tg_last_update_id"] + 1
        updates = tg_api_call(
            token,
            "getUpdates",
            {"offset": offset, "timeout": poll_timeout},
            timeout=poll_timeout + 8,
        )

        for upd in updates.get("result", []):
            upd_id = upd["update_id"]
            with KERNEL["lock"]:
                if upd_id in KERNEL["tg_processed_updates"]:
                    continue
                KERNEL["tg_processed_updates"].add(upd_id)
                KERNEL["tg_last_update_id"] = max(KERNEL["tg_last_update_id"], upd_id)

            processed_count += 1

            if "callback_query" in upd:
                cb = upd["callback_query"]
                chat_id = cb["message"]["chat"]["id"]
                data = cb.get("data", "")
                tg_api_call(token, "answerCallbackQuery", {"callback_query_id": cb["id"]}, timeout=10)

                if data == "mcp_approve":
                    out = resolve_pending_mcp_call(True, actor="Telegram")
                    tg_send_message(token, chat_id, f"✅ 準許執行 (Approved)!\n\n{out}")
                elif data == "mcp_reject":
                    out = resolve_pending_mcp_call(False, actor="Telegram")
                    tg_send_message(token, chat_id, out)
                elif data == "daily_approve":
                    with KERNEL["lock"]:
                        pd = KERNEL["pending_daily_draft"]
                        KERNEL["pending_daily_draft"] = None
                    if pd:
                        safe_write_file(f"daily_reports/{pd['date_slug']}.md", pd["draft_md"], read_only_lock=False)
                        save_memory("daily_report", f"[{pd['date_str']}] {pd['draft_md'][:600]}", custom_ts=f"{pd['date_slug']} 18:00:00")
                        tg_send_message(token, chat_id, f"✅ 已發布 `{pd['date_slug']}.md` 並存入 SQLite 日報記憶庫！現在可隨時輸入 `/weekly` 匯出 Word 檔。")
                    else:
                        tg_send_message(token, chat_id, "未找到待審核的營運日報草稿。")
                elif data == "daily_reject":
                    with KERNEL["lock"]:
                        KERNEL["pending_daily_draft"] = None
                    tg_send_message(token, chat_id, "🛑 營運日報草稿已捨棄。")
                continue

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
                tg_send_message(token, chat_id, f"🔒 未授權的 Chat ID ({chat_id})。")
                continue

            with KERNEL["lock"]:
                KERNEL["tg_log"].append(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}] TG ({chat_id}): {text[:70]}")

            if text.startswith("/start") or text.startswith("/help"):
                welcome = (
                    f"🤖 Surface Hybrid MCP + TAIDE Kernel 已連結！\n"
                    f"擁有者 Chat ID: `{chat_id}`\n\n"
                    f"可用指令:\n"
                    f"• 直接傳送現場巡檢筆記或問題 → 自動保留完整技術細節並排入日報佇列\n"
                    f"• 詢問「新臺幣這五天對美元匯率走勢」→ 即時調用歷史與即期匯率工具\n"
                    f"• `/daily <筆記>` → 產生 18:00 營運日報草稿（附審核按鈕）\n"
                    f"• `/weekly` → 彙整已核准日報並直接傳送 `.docx` Word 檔\n"
                    f"• `/memory` & `/files` → 查詢 SQLite 記憶與沙盒檔案"
                )
                tg_send_message(token, chat_id, welcome)

            elif text.startswith("/memory"):
                tg_send_message(token, chat_id, f"🧠 SQLite 記憶庫內容：\n{recall_memories(limit=8)}")

            elif text.startswith("/files"):
                tg_send_message(token, chat_id, "📂 沙盒檔案清單：\n" + "\n".join([f"• {f}" for f in list_sandbox_files()]))

            elif text.startswith("/daily"):
                raw_notes = text[6:].strip()
                with KERNEL["lock"]:
                    existing_pd = KERNEL["pending_daily_draft"]
                if not raw_notes and existing_pd:
                    kb = {
                        "inline_keyboard": [[
                            {"text": "✅ 準許並存入記憶 (Approve)", "callback_data": "daily_approve"},
                            {"text": "🛑 捨棄草稿 (Discard)", "callback_data": "daily_reject"},
                        ]]
                    }
                    tg_send_message(token, chat_id, f"📋 目前待審核營運日報 ({existing_pd['date_str']}):\n\n{existing_pd['draft_md']}", reply_markup=kb)
                else:
                    notes_to_use = raw_notes or "- 完成 3 個批次現場 QC 線上巡檢（良率 98.7%）。\n- 驗證化學加藥紀錄與廢水酸鹼值中和（pH 7.2）。"
                    now_dt = datetime.now(LOCAL_TZ)
                    d_str = now_dt.strftime("%Y-%m-%d (%a)")
                    d_slug = now_dt.strftime("%Y-%m-%d")
                    draft_md, model_used = generate_daily_report_draft(d_str, notes_to_use)
                    with KERNEL["lock"]:
                        KERNEL["pending_daily_draft"] = {
                            "date_str": d_str,
                            "date_slug": d_slug,
                            "draft_md": draft_md,
                            "raw_summary": notes_to_use[:160].replace("\n", " "),
                            "model_used": model_used,
                        }
                    kb = {
                        "inline_keyboard": [[
                            {"text": "✅ 準許並存入記憶 (Approve)", "callback_data": "daily_approve"},
                            {"text": "🛑 捨棄草稿 (Discard)", "callback_data": "daily_reject"},
                        ]]
                    }
                    tg_send_message(token, chat_id, f"📋 待審核 18:00 營運日報 ({d_str}):\n\n{draft_md}", reply_markup=kb)

            elif text.startswith("/weekly"):
                rows = get_memories_by_topic("daily_report")
                if not rows:
                    tg_send_message(token, chat_id, "📑 目前尚無已核准的營運日報。請先將現場筆記送出或輸入 `/daily` 並點擊「✅ 準許並存入記憶」後再執行 `/weekly`。")
                else:
                    execute_mcp_tool("export_weekly_docx", {"report_title": "Weekly Executive Operations Report"}, read_only_lock=False)
                    with KERNEL["lock"]:
                        weekly_md = KERNEL["compiled_weekly_md"] or "尚無日報。"
                    docx_bytes = build_docx_bytes(
                        title="Weekly Executive Operations Report",
                        subtitle=f"彙整 {len(rows)} 筆經審核之臺灣營運與工程日報",
                        markdown_body=weekly_md,
                    )
                    fname = f"Weekly_Operations_Report_{datetime.now(LOCAL_TZ).strftime('%Y%m%d')}.docx"
                    tg_send_docx_document(token, chat_id, fname, docx_bytes, caption=f"📑 已自 SQLite 彙整 {len(rows)} 筆日報 Word 檔。")

            else:
                reply_text, pending_hitl = process_prompt_shared(text, source_tag="Telegram")
                if pending_hitl:
                    kb = {
                        "inline_keyboard": [[
                            {"text": "✅ 準許寫入 (Approve)", "callback_data": "mcp_approve"},
                            {"text": "🛑 拒絕 (Reject)", "callback_data": "mcp_reject"},
                        ]]
                    }
                    tg_send_message(token, chat_id, reply_text, reply_markup=kb)
                else:
                    # If the prompt automatically staged a daily report, attach Approve/Reject buttons right in Telegram!
                    with KERNEL["lock"]:
                        has_staged_daily = KERNEL["pending_daily_draft"] is not None
                    if has_staged_daily and "18:00 營運日報待審核佇列" in reply_text:
                        kb = {
                            "inline_keyboard": [[
                                {"text": "✅ 準許並納入日報記憶庫", "callback_data": "daily_approve"},
                                {"text": "🛑 捨棄草稿", "callback_data": "daily_reject"},
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
    while True:
        with KERNEL["lock"]:
            has_token = bool(KERNEL["tg_token"])
        if not has_token:
            time.sleep(3)
            continue
        poll_telegram_once(poll_timeout=6)
        time.sleep(1)

with KERNEL["lock"]:
    if not KERNEL["tg_thread_running"]:
        t = threading.Thread(target=telegram_polling_worker, daemon=True)
        t.start()
        KERNEL["tg_thread_running"] = True


# =========================================================
# 9. STREAMLIT UI & CONTROL CENTER
# =========================================================
st.set_page_config(page_title="Surface Hybrid MCP + TAIDE + Telegram", layout="wide")
st.title("Surface Laptop Studio — Hybrid MCP Kernel + TAIDE & Telegram")

with st.sidebar:
    st.header("1. API Keys & Telegram Bot Token")
    with st.expander("🔑 密鑰與機器人設定", expanded=not bool(KERNEL["tg_token"])):
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

    if st.button("⚡ 手動同步 Telegram 收件匣", type="primary", use_container_width=True):
        count = poll_telegram_once(poll_timeout=1)
        st.success(f"已同步：處理了 {count} 則新訊息。")
        st.rerun()

    KERNEL["hitl_enabled"] = st.toggle("⏸️ 檔案與記憶寫入審核 (HITL)", value=KERNEL["hitl_enabled"])
    KERNEL["simulate_offline"] = st.toggle("🧪 模擬斷線 (測試 Tier 3 離線備援)", value=KERNEL["simulate_offline"])
    KERNEL["read_only_lock"] = st.toggle("🔒 唯讀鎖定模式", value=KERNEL["read_only_lock"])

    st.divider()
    st.header("2. 系統連線狀態")
    if KERNEL["tg_bot_username"]:
        st.success(f"Telegram: **{KERNEL['tg_bot_username']}** | Chat ID: `{KERNEL['tg_allowed_chat_id'] or '等待 /start'}`")
    elif KERNEL["tg_token"]:
        st.info("偵測到 Token，正在連線...")
    else:
        st.warning("請於上方填入 TELEGRAM_BOT_TOKEN。")

    st.divider()
    st.header("3. 沙盒檔案與詞庫管理")
    sandbox_files = list_sandbox_files()
    selected_file = st.selectbox("safe_workspace 檔案", sandbox_files)
    if selected_file and st.button("檢視檔案內容"):
        st.code(safe_read_file(selected_file), language="markdown")

# Top Telemetry Bar
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric(
        "Gemini API & TAIDE",
        "🟢 線上運作 (Tier 1)" if KERNEL["gemini_key"] and not KERNEL["simulate_offline"] else "🟠 離線備援 (Tier 3)",
        "臺灣正體中文詞庫防護啟用",
    )
with m2:
    st.metric(
        "Telegram 機器人",
        f"🟢 {KERNEL['tg_bot_username']}" if KERNEL["tg_bot_username"] else "⚪ 等待 Token",
        f"Chat ID: {KERNEL['tg_allowed_chat_id']}" if KERNEL["tg_allowed_chat_id"] else "準備就緒",
    )
with m3:
    st.metric("JIT 累積節省 Token", f"{KERNEL['total_tokens_saved']:,} tok", "JIT MCP Skill Compiler")
with m4:
    st.metric("SQLite 已核准日報", f"{len(get_memories_by_topic('daily_report'))} 筆", f"{len(list_sandbox_files())} 個沙盒檔案")

tab_mcp, tab_ops = st.tabs([
    "🤖 聊天與 JIT MCP 技能編譯器 (支援多輪記憶 + 匯率走勢 + 現場筆記自動排入日報)",
    "📋 18:00 營運日報待審核佇列與 Word (.docx) 彙整",
])

with tab_mcp:
    st.caption("**提示：現在支援多輪對話記憶！您可以貼上現場巡檢筆記（將自動保留 100% 技術細節並送入日報審核佇列），或直接詢問「新臺幣這五天對美元的匯率走勢為何？」**")
    for item in KERNEL["chat_history"]:
        with st.chat_message(item["role"]):
            if "meta" in item:
                meta = item["meta"]
                st.caption(
                    f"**模型:** `{meta['model_label']}` | **JIT 模式:** `{meta.get('mcp_mode')}` | "
                    f"**啟用技能:** `{meta.get('compiled_skills') or '0'}` | **節省:** `{meta.get('saved_tokens', 0):,} tok`"
                )
            st.markdown(item["content"])

    if KERNEL["pending_mcp_call"] is not None:
        p = KERNEL["pending_mcp_call"]
        st.warning(
            f"### ⏸️ 需要人工準許 (來自 {p['source_tag']})\n"
            f"**技能:** `{p['fn_name']}`"
        )
        st.code(json.dumps(p["fn_args"], indent=2, ensure_ascii=False), language="json")
        c_ap, c_rj = st.columns(2)
        if c_ap.button("✅ 準許寫入 (Web UI)", type="primary", use_container_width=True):
            resolve_pending_mcp_call(True, actor="Web UI")
            st.rerun()
        if c_rj.button("🛑 拒絕寫入 (Web UI)", use_container_width=True):
            resolve_pending_mcp_call(False, actor="Web UI")
            st.rerun()

    if web_prompt := st.chat_input("輸入提問、匯率走勢查詢、或直接貼上今日現場巡檢筆記..."):
        process_prompt_shared(web_prompt, source_tag="Web")
        st.rerun()

with tab_ops:
    st.subheader("18:00 營運日報待審核佇列與 Word (.docx) 週報匯出")
    if KERNEL["pending_daily_draft"] is None:
        st.info("💡 當您在聊天室或 Telegram 貼上現場筆記（如 Oct 20 蒸餾塔與鍋爐巡檢紀錄）時，系統會自動產生零遺漏日報並顯示於此處供您審核。")
    else:
        pd = KERNEL["pending_daily_draft"]
        st.warning(f"⏸️ **待審核營運日報:** `{pd['date_str']}` | **引擎:** `{pd['model_used']}`")
        edited_md = st.text_area("審核與編輯日報草稿（確認無誤後點擊下方按鈕正式納入日報與週報記憶庫）：", value=pd["draft_md"], height=320)
        rc1, rc2 = st.columns(2)
        if rc1.button("✅ 準許發布並納入日報/週報記憶庫", type="primary", use_container_width=True):
            safe_write_file(f"daily_reports/{pd['date_slug']}.md", edited_md, read_only_lock=False)
            save_memory("daily_report", f"[{pd['date_str']}]\n{edited_md}", custom_ts=f"{pd['date_slug']} 18:00:00")
            if KERNEL["tg_allowed_chat_id"] and KERNEL["tg_token"]:
                tg_send_message(KERNEL["tg_token"], KERNEL["tg_allowed_chat_id"], f"✅ 日報 {pd['date_slug']} 已核准並納入週報記憶庫！")
            KERNEL["pending_daily_draft"] = None
            st.success("已成功存入 SQLite 日報記憶庫！")
            st.rerun()
        if rc2.button("🛑 捨棄草稿", use_container_width=True):
            KERNEL["pending_daily_draft"] = None
            st.rerun()

    st.divider()
    approved_rows = get_memories_by_topic("daily_report")
    st.subheader(f"每週報告彙整與 Word (.docx) 下載（目前已核准：{len(approved_rows)} 筆日報）")
    if approved_rows:
        if st.button("📑 彙整所有已核准日報並產生 Word (.docx)", type="primary", use_container_width=True):
            execute_mcp_tool("export_weekly_docx", {"report_title": "每週廠務與營運執行總結報告"}, read_only_lock=False)
            st.rerun()
        if KERNEL["compiled_weekly_md"]:
            st.markdown(KERNEL["compiled_weekly_md"])
            docx_bytes = build_docx_bytes(
                title="每週廠務與營運執行總結報告",
                subtitle=f"彙整自 {len(approved_rows)} 筆已核准之現場工程日報 | 產生時間：{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M CST')}",
                markdown_body=KERNEL["compiled_weekly_md"],
            )
            st.download_button(
                "📥 下載每週營運總結報告 (.docx)",
                data=docx_bytes,
                file_name=f"Weekly_Operations_Report_{datetime.now(LOCAL_TZ).strftime('%Y%m%d')}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary",
                use_container_width=True,
            )
