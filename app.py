# app.py
# ------------------------------------------------------------
# Teaching-Agent-Suite (Template-first, Project-based)
# - 修复 sidebar/logo 渲染为纯文本的问题
# - 修复 pdfplumber 抽表 table_settings 版本不兼容导致 TypeError 崩溃的问题（自动 fallback）
# - 抽取培养方案后：右侧 1-11 全联动显示（1-6 文本，7-10 抓后面表格，11 图）
# - 修复 data_editor 重复 key（graph_nodes/graph_edges 等）
# ------------------------------------------------------------

from __future__ import annotations

import io
import os
import re
import json
import uuid
import zipfile
import hashlib
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
import pandas as pd
import requests

# ---- Optional deps ----
try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except Exception:
    Document = None


# =========================
# Globals / constants
# =========================

APP_NAME = "Teaching Agent Suite"
APP_VERSION = "v0.6 (base(1-11)+appendix tables+logo fixed)"
DATA_ROOT = Path("data/projects")

SECTION_TITLES = [
    "一、培养目标",
    "二、毕业要求",
    "三、专业定位与特色",
    "四、主干学科、专业核心课程和主要实践性教学环节",
    "五、标准学制与授予学位",
    "六、毕业条件",
    "七、专业教学计划表",
    "八、学分统计表",
    "九、教学进程表",
    "十、课程设置对毕业要求支撑关系表",
    "十一、课程设置逻辑思维导图",
]

APPENDIX_TITLES = SECTION_TITLES[6:10]  # 7-10

TEMPLATE_TYPES = [
    "课程大纲",
    "教学日历",
    "授课手册",
    "达成度评价依据审核表",
    "达成度评价报告",
    "调查问卷",
]


# =========================
# Utilities
# =========================

def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def safe_json_load(s: str, default: Any = None) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return default

def clean_text(s: str) -> str:
    if s is None:
        return ""
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    # 保留换行更利于分段
    s = s.replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def clamp(s: str, n: int = 12000) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n…(截断，原长度 {len(s)})"

def dataframe_safe(df: pd.DataFrame) -> pd.DataFrame:
    """
    解决 Streamlit/pyarrow 常见崩溃：列里出现 dict/list/None 混合类型。
    统一转成 str，保证可展示、可导出。
    """
    if df is None:
        return pd.DataFrame()
    df2 = df.copy()
    df2.columns = [str(c) for c in df2.columns]
    for c in df2.columns:
        df2[c] = df2[c].map(lambda x: "" if x is None else str(x))
    return df2

def render_table_html(df: pd.DataFrame, height: int = 420) -> None:
    """
    兜底展示：不用 st.dataframe 避免 arrow 推断类型问题。
    """
    df2 = dataframe_safe(df)
    html = df2.to_html(index=False, escape=True)
    st.components.v1.html(
        f"<div style='max-height:{height}px; overflow:auto; border:1px solid #eee; padding:8px'>{html}</div>",
        height=min(height + 40, 900),
        scrolling=True,
    )

def json_download_button(label: str, obj: Any, filename: str, key: Optional[str] = None):
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(label, data=data, file_name=filename, mime="application/json", key=key)

def to_xlsx_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        for name, df in sheets.items():
            name = str(name)[:31]
            dataframe_safe(df).to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


# =========================
# Persistence (Project DB)
# =========================

@dataclass
class Project:
    project_id: str
    name: str
    llm: Dict[str, Any] = field(default_factory=dict)  # 保存项目默认LLM配置（不含Key）
    created_at: str = field(default_factory=now_str)
    updated_at: str = field(default_factory=now_str)
    logo_file: str = ""   # "logo.png" 或 "logo.svg"

def project_dir(pid: str) -> Path:
    return DATA_ROOT / pid

def project_meta_path(pid: str) -> Path:
    return project_dir(pid) / "project.json"

def base_plan_path(pid: str) -> Path:
    return project_dir(pid) / "base_training_plan.json"

def docs_dir(pid: str) -> Path:
    return project_dir(pid) / "docs"

def doc_path(pid: str, doc_id: str) -> Path:
    return docs_dir(pid) / f"{doc_id}.json"

def assets_dir(pid: str) -> Path:
    return project_dir(pid) / "assets"

def list_projects() -> List[Project]:
    ensure_dir(DATA_ROOT)
    out: List[Project] = []
    for p in DATA_ROOT.iterdir():
        if not p.is_dir():
            continue
        meta_file = p / "project.json"
        if not meta_file.exists():
            continue
        meta = safe_json_load(meta_file.read_text("utf-8"), {})
        if meta and "project_id" in meta:
            try:
                out.append(Project(**meta))
            except Exception:
                pass
    out.sort(key=lambda x: x.updated_at, reverse=True)
    return out

def save_project(prj: Project) -> None:
    ensure_dir(project_dir(prj.project_id))
    ensure_dir(docs_dir(prj.project_id))
    ensure_dir(assets_dir(prj.project_id))
    prj.updated_at = now_str()
    project_meta_path(prj.project_id).write_text(
        json.dumps(prj.__dict__, ensure_ascii=False, indent=2), "utf-8"
    )

def load_project(pid: str) -> Optional[Project]:
    p = project_meta_path(pid)
    if not p.exists():
        return None
    meta = safe_json_load(p.read_text("utf-8"), {})
    try:
        return Project(**meta) if meta else None
    except Exception:
        return None

def load_base_plan(pid: str) -> Dict[str, Any]:
    p = base_plan_path(pid)
    return safe_json_load(p.read_text("utf-8"), {}) if p.exists() else {}

def save_base_plan(pid: str, plan: Dict[str, Any]) -> None:
    ensure_dir(project_dir(pid))
    base_plan_path(pid).write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")

def list_docs(pid: str) -> List[Dict[str, Any]]:
    ensure_dir(docs_dir(pid))
    out: List[Dict[str, Any]] = []
    for p in docs_dir(pid).glob("*.json"):
        obj = safe_json_load(p.read_text("utf-8"), {})
        if obj:
            out.append(obj)
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out

def save_doc(pid: str, doc_obj: Dict[str, Any]) -> None:
    ensure_dir(docs_dir(pid))
    doc_obj["updated_at"] = now_str()
    doc_path(pid, doc_obj["doc_id"]).write_text(json.dumps(doc_obj, ensure_ascii=False, indent=2), "utf-8")

def delete_doc(pid: str, doc_id: str) -> None:
    p = doc_path(pid, doc_id)
    if p.exists():
        p.unlink()


# =========================
# LLM Config + Provider presets
# =========================

@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str = "openai_compat"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: int = 60
    temperature: float = 0.2
    max_tokens: int = 2048
    extra_headers_json: str = ""
    extra_params_json: str = ""
    api_version: str = ""
    endpoint_url: str = ""

def _safe_json_loads(s: str) -> Dict[str, Any]:
    if not s or not str(s).strip():
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _read_llm_defaults() -> Dict[str, Any]:
    s: Dict[str, Any] = {}
    try:
        s = dict(st.secrets.get("llm", {}))
    except Exception:
        s = {}
    return {
        "enabled": bool(s.get("enabled", False)),
        "provider": str(s.get("provider", os.environ.get("LLM_PROVIDER", "openai_compat"))),
        "api_key": str(s.get("api_key", os.environ.get("LLM_API_KEY", ""))),
        "base_url": str(s.get("base_url", os.environ.get("LLM_BASE_URL", ""))),
        "model": str(s.get("model", os.environ.get("LLM_MODEL", ""))),
        "timeout": int(s.get("timeout", int(os.environ.get("LLM_TIMEOUT", 60)))),
        "temperature": float(s.get("temperature", float(os.environ.get("LLM_TEMPERATURE", 0.2)))),
        "max_tokens": int(s.get("max_tokens", int(os.environ.get("LLM_MAX_TOKENS", 2048)))),
        "extra_headers_json": str(s.get("extra_headers_json", "")),
        "extra_params_json": str(s.get("extra_params_json", "")),
        "endpoint_url": str(s.get("endpoint_url", "")),
        "api_version": str(s.get("api_version", "")),
    }

PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "OpenAI / OpenAI兼容（通用）": {
        "provider": "openai_compat",
        "default_base_url": "https://api.openai.com/v1",
        "models": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"],
        "default_model": "gpt-4.1-mini",
        "base_url_hint": "OpenAI兼容一般是 https://xxx/v1",
    },
    "DeepSeek（OpenAI兼容）": {
        "provider": "openai_compat",
        "default_base_url": "",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "base_url_hint": "填 DeepSeek 提供的 OpenAI 兼容 base_url（通常以 /v1 结尾）",
    },
    "月之暗面 Kimi（OpenAI兼容）": {
        "provider": "openai_compat",
        "default_base_url": "",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "default_model": "moonshot-v1-8k",
        "base_url_hint": "填 Kimi 的 OpenAI 兼容 base_url（通常以 /v1 结尾）",
    },
    "Claude (Anthropic) 原生接口": {
        "provider": "anthropic",
        "default_base_url": "https://api.anthropic.com",
        "models": ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"],
        "default_model": "claude-3-5-sonnet-latest",
        "base_url_hint": "不确定可用默认；如走网关可改。",
    },
    "Gemini 原生接口": {
        "provider": "gemini",
        "default_base_url": "",
        "models": ["gemini-1.5-pro", "gemini-1.5-flash"],
        "default_model": "gemini-1.5-flash",
        "base_url_hint": "通常不需要 Base URL；可用 endpoint_url 覆盖。",
    },
    "自定义 REST（任意平台/私有模型）": {
        "provider": "custom_rest",
        "default_base_url": "",
        "models": [],
        "default_model": "",
        "base_url_hint": "填完整URL（例如 https://host/path）",
    },
}

def llm_available(cfg: LLMConfig) -> bool:
    if not cfg or not cfg.enabled:
        return False
    if cfg.provider == "openai_compat":
        return bool(cfg.api_key) and bool(cfg.base_url) and bool(cfg.model)
    if cfg.provider == "anthropic":
        return bool(cfg.api_key) and bool(cfg.model)
    if cfg.provider == "gemini":
        return bool(cfg.api_key) and bool(cfg.model)
    if cfg.provider == "custom_rest":
        return bool(cfg.endpoint_url or cfg.base_url)
    return False


# =========================
# LLM unified call
# =========================

def llm_chat(messages: List[Dict[str, str]], cfg: LLMConfig) -> str:
    if not cfg.enabled:
        raise RuntimeError("LLM is disabled")

    if cfg.provider == "openai_compat":
        return _call_openai_compat(messages, cfg)
    if cfg.provider == "anthropic":
        return _call_anthropic(messages, cfg)
    if cfg.provider == "gemini":
        return _call_gemini(messages, cfg)
    if cfg.provider == "custom_rest":
        return _call_custom_rest(messages, cfg)
    raise ValueError(f"Unknown provider: {cfg.provider}")

def _call_openai_compat(messages: List[Dict[str, str]], cfg: LLMConfig) -> str:
    base = (cfg.base_url or "").rstrip("/")
    if base.endswith("/v1"):
        url = base + "/chat/completions"
    elif base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/v1/chat/completions"

    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    headers.update(_safe_json_loads(cfg.extra_headers_json))

    payload: Dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    payload.update(_safe_json_loads(cfg.extra_params_json))

    r = requests.post(url, headers=headers, json=payload, timeout=cfg.timeout)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

def _call_anthropic(messages: List[Dict[str, str]], cfg: LLMConfig) -> str:
    endpoint = cfg.endpoint_url.strip()
    if not endpoint:
        base = (cfg.base_url or "https://api.anthropic.com").rstrip("/")
        endpoint = base + "/v1/messages"

    system_text = "\n".join([m["content"] for m in messages if m["role"] == "system"]).strip()

    user_parts: List[Dict[str, str]] = []
    for m in messages:
        if m["role"] == "user":
            user_parts.append({"type": "text", "text": m["content"]})
        elif m["role"] == "assistant":
            user_parts.append({"type": "text", "text": f"(assistant) {m['content']}"})

    headers: Dict[str, str] = {
        "x-api-key": cfg.api_key,
        "content-type": "application/json",
        "anthropic-version": cfg.api_version.strip() or "2023-06-01",
    }
    headers.update(_safe_json_loads(cfg.extra_headers_json))

    payload: Dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "messages": [{"role": "user", "content": user_parts or [{"type": "text", "text": "(empty)"}]}],
    }
    if system_text:
        payload["system"] = system_text
    payload.update(_safe_json_loads(cfg.extra_params_json))

    r = requests.post(endpoint, headers=headers, json=payload, timeout=cfg.timeout)
    r.raise_for_status()
    data = r.json()
    parts = data.get("content", [])
    texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
    return "\n".join([t for t in texts if t]).strip()

def _call_gemini(messages: List[Dict[str, str]], cfg: LLMConfig) -> str:
    endpoint = cfg.endpoint_url.strip()
    if not endpoint:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.model}:generateContent?key={cfg.api_key}"

    contents: List[Dict[str, Any]] = []
    for m in messages:
        role = "user" if m["role"] != "assistant" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"temperature": cfg.temperature, "maxOutputTokens": cfg.max_tokens},
    }
    payload.update(_safe_json_loads(cfg.extra_params_json))

    headers = {"content-type": "application/json"}
    headers.update(_safe_json_loads(cfg.extra_headers_json))

    r = requests.post(endpoint, headers=headers, json=payload, timeout=cfg.timeout)
    r.raise_for_status()
    data = r.json()
    c0 = (data.get("candidates") or [{}])[0]
    content = c0.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
    return "\n".join([t for t in texts if t]).strip()

def _call_custom_rest(messages: List[Dict[str, str]], cfg: LLMConfig) -> str:
    url = cfg.endpoint_url.strip() or cfg.base_url.strip()
    if not url:
        raise ValueError("custom_rest requires endpoint_url or base_url")

    headers = _safe_json_loads(cfg.extra_headers_json)
    if cfg.api_key and "authorization" not in {k.lower() for k in headers.keys()}:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    payload: Dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    payload.update(_safe_json_loads(cfg.extra_params_json))

    r = requests.post(url, headers=headers, json=payload, timeout=cfg.timeout)
    r.raise_for_status()
    data = r.json()

    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        pass
    for k in ["text", "output", "result", "answer", "content"]:
        if isinstance(data.get(k), str):
            return data[k]
    return json.dumps(data, ensure_ascii=False)[:4000]


# =========================
# LLM JSON helpers
# =========================

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    obj = safe_json_load(text, None)
    if isinstance(obj, dict):
        return obj

    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        obj = safe_json_load(m.group(1), None)
        if isinstance(obj, dict):
            return obj

    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        obj = safe_json_load(m.group(1), None)
        if isinstance(obj, dict):
            return obj
    return None

def llm_chat_json(cfg: LLMConfig, system: str, user: str, schema_hint: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
    if not llm_available(cfg):
        return None, "LLM 未启用或配置不完整。"

    messages = [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": (user.strip() + ("\n\nJSON schema hint:\n" + schema_hint if schema_hint else "")).strip()},
    ]

    # openai_compat 尝试 response_format 强制 JSON（不保证所有网关支持）
    if cfg.provider == "openai_compat":
        base = (cfg.base_url or "").rstrip("/")
        if base.endswith("/v1"):
            url = base + "/chat/completions"
        elif base.endswith("/chat/completions"):
            url = base
        else:
            url = base + "/v1/chat/completions"

        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
        headers.update(_safe_json_loads(cfg.extra_headers_json))

        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "response_format": {"type": "json_object"},
        }
        payload.update(_safe_json_loads(cfg.extra_params_json))

        try:
            r = requests.post(url, headers=headers, json=payload, timeout=cfg.timeout)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            obj = extract_json_from_text(content)
            return obj, content
        except Exception as e:
            return None, f"LLM 调用失败：{e}"

    try:
        content = llm_chat(messages, cfg)
        obj = extract_json_from_text(content)
        return obj, content
    except Exception as e:
        return None, f"LLM 调用失败：{e}"


# =========================
# DOCX parsing / exporting
# =========================

def docx_bytes_to_document(file_bytes: bytes):
    if Document is None:
        raise RuntimeError("python-docx 未安装或不可用。")
    return Document(io.BytesIO(file_bytes))

def docx_extract_text_tables(file_bytes: bytes) -> Tuple[str, List[pd.DataFrame]]:
    doc = docx_bytes_to_document(file_bytes)
    paras: List[str] = []
    for p in doc.paragraphs:
        t = clean_text(p.text)
        if t:
            paras.append(t)

    dfs: List[pd.DataFrame] = []
    for tbl in doc.tables:
        rows: List[List[str]] = []
        for row in tbl.rows:
            rows.append([clean_text(c.text) for c in row.cells])

        maxlen = max((len(r) for r in rows), default=0)
        rows2 = [r + [""] * (maxlen - len(r)) for r in rows]

        if rows2:
            header = rows2[0]
            body = rows2[1:] if len(rows2) > 1 else []
            if sum(1 for x in header if x) <= 1:
                header = [f"列{i+1}" for i in range(maxlen)]
                body = rows2
            df = pd.DataFrame(body, columns=header)
        else:
            df = pd.DataFrame()
        dfs.append(df)

    return "\n".join(paras), dfs

def docx_export_simple(template_title: str, sections: List[Tuple[str, str]], tables: Optional[List[Tuple[str, pd.DataFrame]]] = None) -> bytes:
    if Document is None:
        raise RuntimeError("python-docx 未安装或不可用。")
    tables = tables or []

    doc = Document()
    t = doc.add_paragraph()
    run = t.add_run(template_title)
    run.bold = True
    run.font.size = Pt(16)
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph("")

    for h, body in sections:
        doc.add_heading(h, level=1)
        for para in (body or "").splitlines():
            para = para.strip()
            if para:
                doc.add_paragraph(para)
        doc.add_paragraph("")

    for name, df in tables:
        doc.add_heading(name, level=2)
        df2 = dataframe_safe(df)
        nrows, ncols = df2.shape
        if ncols == 0:
            doc.add_paragraph("(空表)")
            doc.add_paragraph("")
            continue

        word_tbl = doc.add_table(rows=nrows + 1, cols=ncols)
        word_tbl.style = "Table Grid"
        for j, col in enumerate(df2.columns):
            word_tbl.cell(0, j).text = str(col)
        for i in range(nrows):
            for j in range(ncols):
                word_tbl.cell(i + 1, j).text = str(df2.iat[i, j])
        doc.add_paragraph("")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# =========================
# Schemas / Templates
# =========================

def schema_for(template_type: str) -> Dict[str, Any]:
    if template_type == "教学日历":
        return {
            "course_name": "",
            "term": "",
            "major_and_grade": "",
            "teacher": "",
            "total_hours": "",
            "weeks": "",
            "assessment": "",
            "grade_rule": "",
            "textbook": [{"name": "", "press": "", "year": ""}],
            "references": [{"name": "", "press": "", "year": ""}],
            "schedule_rows": [
                {"week": "", "lesson_no": "", "content": "", "key_points": "", "hours": "", "method": "", "others": "", "support_objective": ""}
            ],
        }
    if template_type == "课程大纲":
        return {
            "course_name": "",
            "course_code": "",
            "credits": "",
            "hours_total": "",
            "hours_theory": "",
            "hours_practice": "",
            "course_nature": "",
            "prerequisites": "",
            "teaching_objectives": [{"id": "1", "text": "", "support_grad_req": ""}],
            "content_outline": [{"module": "", "hours": "", "topics": ""}],
            "assessment": [{"item": "", "weight": "", "notes": ""}],
            "textbooks": [{"name": "", "press": "", "year": ""}],
            "references": [{"name": "", "press": "", "year": ""}],
            "remarks": "",
        }
    if template_type == "授课手册":
        return {
            "course_name": "",
            "term": "",
            "class": "",
            "teacher": "",
            "weekly_log": [{"date": "", "progress": "", "issues": "", "actions": ""}],
            "summary": "",
            "exam_analysis": "",
            "improvement": "",
        }
    if template_type == "达成度评价依据审核表":
        return {
            "course_name": "",
            "term": "",
            "evidence_used": {"期末试卷": True, "平时考试": True, "作业": True, "实验": False, "讨论小论文": False},
            "calc_method": "",
            "conclusion": "",
            "review_team": "",
            "review_date": "",
        }
    if template_type == "达成度评价报告":
        return {
            "course_name": "",
            "term": "",
            "threshold": "0.65",
            "objectives": [{"obj": "1", "support_grad_req": "", "direct_score": "", "self_score": "", "achieved": ""}],
            "overall_comment": "",
            "analysis": "",
            "improvements": "",
            "weakness": "",
            "next_suggestions": "",
            "responsible": "",
            "date": "",
            "reviewer": "",
            "review_date": "",
        }
    if template_type == "调查问卷":
        return {"title": "", "target": "", "questions": [{"q": "", "type": "单选/多选/量表/填空", "options": []}]}
    return {}

def merge_by_schema(schema: Dict[str, Any], obj: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(schema, dict) or not isinstance(obj, dict):
        return obj if obj is not None else schema

    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k in obj:
            if isinstance(v, dict) and isinstance(obj[k], dict):
                out[k] = merge_by_schema(v, obj[k])
            elif isinstance(v, list) and isinstance(obj[k], list):
                out[k] = obj[k]
            else:
                out[k] = obj[k]
        else:
            out[k] = v

    if "warnings" in obj and "warnings" not in out:
        out["warnings"] = obj["warnings"]
    return out

def new_doc_object(template_type: str, title: str = "") -> Dict[str, Any]:
    doc_id = uuid.uuid4().hex[:12]
    return {
        "doc_id": doc_id,
        "template_type": template_type,
        "title": title or f"{template_type}-{doc_id}",
        "created_at": now_str(),
        "updated_at": now_str(),
        "source": {"uploaded_filename": "", "sha256": ""},
        "data": {},
        "raw": {"text": "", "tables": []},
        "llm": {"last_prompt": "", "last_raw_response": ""},
        "history": [],
    }


# =========================
# Heuristic extraction (offline baseline)
# =========================

def heuristic_fill(template_type: str, raw_text: str, raw_tables: List[pd.DataFrame]) -> Dict[str, Any]:
    raw_text = raw_text or ""
    lines = [clean_text(x) for x in raw_text.splitlines() if clean_text(x)]
    data = schema_for(template_type)
    if not data:
        return {}

    def find_after(pattern: str, max_ahead: int = 2) -> str:
        pat = re.compile(pattern)
        for i, ln in enumerate(lines):
            if pat.search(ln):
                for j in range(1, max_ahead + 1):
                    if i + j < len(lines) and lines[i + j]:
                        return lines[i + j]
        return ""

    if template_type == "教学日历":
        schedule: List[Dict[str, Any]] = []
        for df in raw_tables:
            df2 = dataframe_safe(df)
            header = " ".join([str(c) for c in df2.columns])
            if ("周次" in header and "课次" in header) or ("教学内容" in header and "周次" in header):
                for _, r in df2.iterrows():
                    row = {c: str(r[c]) for c in df2.columns}
                    schedule.append({
                        "week": row.get("周次", ""),
                        "lesson_no": row.get("课次", ""),
                        "content": row.get("教学内容（写明章节标题）", row.get("教学内容", "")),
                        "key_points": row.get("学习重点、教学要求及", row.get("学习重点", "")),
                        "hours": row.get("学时", ""),
                        "method": row.get("教学方法", ""),
                        "others": row.get("其它（作业、习题课、实验等）", row.get("其它", "")),
                        "support_objective": row.get("支撑教学目标", ""),
                    })
        if schedule:
            data["schedule_rows"] = schedule
        if not data.get("course_name"):
            data["course_name"] = find_after(r"课程\s*名\s*称|课程名称", 1)
        return data

    if template_type == "课程大纲":
        objectives = []
        for ln in lines:
            m = re.match(r"(课程目标|教学目标)\s*([0-9]+)\s*[:：]\s*(.+)", ln)
            if m:
                objectives.append({"id": m.group(2), "text": m.group(3), "support_grad_req": ""})
        if objectives:
            data["teaching_objectives"] = objectives
        if not data.get("course_name"):
            for ln in lines[:40]:
                if "《" in ln and "》" in ln and ("教学大纲" in ln):
                    m = re.findall(r"《([^》]+)》", ln)
                    if m:
                        data["course_name"] = m[0]
                        break
        return data

    return data


# =========================
# Base training plan: PDF extractor (text + appendix tables best-effort)
# =========================

def pdf_extract_pages_text(pdf_bytes: bytes) -> List[str]:
    if pdfplumber is None:
        return []
    pages: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages

def _norm_head(s: str) -> str:
    s = s.strip()
    s = s.replace(" ", "")
    s = s.replace("：", ":")
    return s

def split_sections_from_pages(pages_text: List[str]) -> Dict[str, str]:
    """
    用 1-11 的标题做分段，避免“六”把后面 7-11 的标题全吃进去。
    """
    full = "\n".join([t for t in pages_text if t])
    full2 = full.replace("\r", "\n")

    # 找每个标题出现的位置（尽量宽松：允许空格/换行）
    heads: List[Tuple[str, int]] = []
    for title in SECTION_TITLES:
        # 形如 "六、毕业条件"
        parts = title.split("、", 1)
        if len(parts) != 2:
            continue
        key = re.escape(parts[0]) + r"\s*、\s*" + re.escape(parts[1])
        m = re.search(key, full2)
        if m:
            heads.append((title, m.start()))

    heads.sort(key=lambda x: x[1])

    out: Dict[str, str] = {}
    for i, (h, pos) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(full2)
        out[h] = clean_text(full2[pos:end])
    return out

def _find_page_indices_by_keywords(pages_text: List[str], keywords: List[str]) -> List[int]:
    """
    在分页文本中查找包含任一关键词的页号（0-based）
    """
    idx = []
    for i, t in enumerate(pages_text):
        tt = _norm_head(t)
        if any(_norm_head(k) in tt for k in keywords):
            idx.append(i)
    return idx

def _extract_tables_from_pages(pdf_bytes: bytes, page_idx_list: List[int]) -> List[List[List[str]]]:
    """
    用 pdfplumber 从指定页抽表。
    - 先尝试最小 table_settings（兼容更多版本）
    - 不行就 fallback 到默认 extract_tables()，保证不崩溃
    返回：tables -> table -> row -> cell_text
    """
    if pdfplumber is None:
        return []

    page_idx_list = sorted(set([int(x) for x in page_idx_list if isinstance(x, (int, float))]))
    if not page_idx_list:
        return []

    out_tables: List[List[List[str]]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        max_i = len(pdf.pages) - 1
        for pi in page_idx_list:
            if pi < 0 or pi > max_i:
                continue
            page = pdf.pages[pi]

            # ✅ 最小兼容 settings（很多版本都支持）
            settings_min = {
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            }

            tables = []
            try:
                tables = page.extract_tables(table_settings=settings_min) or []
            except Exception:
                # ✅ fallback：不带 table_settings，避免 TableSettings.resolve 崩溃
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []

            for tb in tables:
                if not tb:
                    continue
                # 清洗
                tb2: List[List[str]] = []
                for row in tb:
                    if row is None:
                        continue
                    tb2.append([clean_text(x) for x in row])
                if tb2:
                    out_tables.append(tb2)

    return out_tables

def _tables_to_records(tables: List[List[List[str]]], max_tables: int = 6) -> List[Dict[str, Any]]:
    """
    把抽到的 table list 转为 records list（便于存 JSON）
    """
    records: List[Dict[str, Any]] = []
    for ti, tb in enumerate(tables[:max_tables]):
        # 取第一行作为 header（如果太空就生成）
        header = tb[0] if tb else []
        body = tb[1:] if len(tb) > 1 else []

        # header 稀疏则生成列名
        nonempty = sum(1 for x in header if x)
        if nonempty <= 1:
            width = max((len(r) for r in tb), default=0)
            header = [f"列{i+1}" for i in range(width)]
            body = tb

        # 对齐列数
        width = max(len(header), max((len(r) for r in body), default=0))
        header = (header + [""] * (width - len(header)))[:width]

        for r in body:
            r2 = (r + [""] * (width - len(r)))[:width]
            rec = {header[i] or f"列{i+1}": r2[i] for i in range(width)}
            # 空行就跳过
            if any(v.strip() for v in rec.values()):
                records.append(rec)

    return records

def extract_appendix_tables_best_effort(pdf_bytes: bytes, pages_text: List[str]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """
    尽力自动抓 7-10 的表格。
    策略：
    - 先用分页文本定位包含 “附表”“专业教学计划表”“学分统计表”“教学进程表”“支撑关系表” 等关键词的页
    - 在这些页（及其后1页）抽表
    - 把抽到的表 records 写入 appendices.tables
    """
    debug: Dict[str, Any] = {"strategy": "keywords+fallback", "hits": {}}
    out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in APPENDIX_TITLES}

    if not pages_text:
        return out, debug

    # 每个附表关键词（你可以按你学校培养方案实际措辞再加）
    kw_map = {
        "七、专业教学计划表": ["专业教学计划表", "附表1", "附表 1", "课程计划", "教学计划表"],
        "八、学分统计表": ["学分统计表", "附表2", "附表 2", "学分统计"],
        "九、教学进程表": ["教学进程表", "附表3", "附表 3", "教学进程"],
        "十、课程设置对毕业要求支撑关系表": ["支撑关系表", "毕业要求支撑", "附表4", "附表 4", "课程设置对毕业要求"],
    }

    # 先定位每个附表出现页
    for title, kws in kw_map.items():
        hits = _find_page_indices_by_keywords(pages_text, kws)
        debug["hits"][title] = hits

        # 扩展页：命中页 + 后1页（很多 PDF 表格跨页）
        page_idx_list = []
        for h in hits:
            page_idx_list.extend([h, h + 1])

        raw_tables = _extract_tables_from_pages(pdf_bytes, page_idx_list)
        records = _tables_to_records(raw_tables)
        out[title] = records

    return out, debug

def base_plan_from_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    pages = pdf_extract_pages_text(pdf_bytes)
    sections = split_sections_from_pages(pages)

    # 7-10 附表：自动抽取（尽力而为）
    auto_tables, debug_meta = extract_appendix_tables_best_effort(pdf_bytes, pages)

    # 把 7-10 的“正文分段”也保留（通常只有标题，没表）
    append_tables = {t: auto_tables.get(t, []) for t in APPENDIX_TITLES}

    # 关系图先空着
    graph = {"nodes": [], "edges": []}

    # rev 用于 UI key 去重
    rev = int(dt.datetime.now().timestamp())

    return {
        "meta": {
            "sha256": sha256_bytes(pdf_bytes),
            "created_at": now_str(),
            "updated_at": now_str(),
            "extractor": "pdfplumber(text+appendix_best_effort)",
            "rev": rev,
            "appendix_debug": debug_meta,
        },
        "sections": sections,  # 1-11 的文本（7-11 多半只有标题）
        "appendices": {"tables": append_tables},
        "raw_pages_text": pages,
        "course_graph": graph,
    }


# =========================
# Consistency checks
# =========================

def run_consistency_checks(template_type: str, data: Dict[str, Any], plan: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    sections = plan.get("sections", {}) if isinstance(plan, dict) else {}

    if template_type in ("教学日历", "课程大纲", "授课手册", "达成度评价依据审核表", "达成度评价报告"):
        if not clean_text(str(data.get("course_name", ""))):
            warnings.append("课程名称为空：建议填写以便后续一致性校验/自动填充。")

    if template_type in ("课程大纲", "达成度评价报告"):
        grad_req = sections.get("二、毕业要求", "")
        codes_in_plan = set(re.findall(r"\b\d+\.\d+\b", grad_req)) if grad_req else set()
        codes_in_doc: set[str] = set()

        if template_type == "课程大纲":
            for row in data.get("teaching_objectives", []):
                codes_in_doc |= set(re.findall(r"\b\d+\.\d+\b", str(row.get("support_grad_req", ""))))
        else:
            for row in data.get("objectives", []):
                codes_in_doc |= set(re.findall(r"\b\d+\.\d+\b", str(row.get("support_grad_req", ""))))

        unknown = sorted([c for c in codes_in_doc if c and c not in codes_in_plan])
        if unknown:
            warnings.append("发现不在培养方案‘毕业要求’中的支撑码： " + ", ".join(unknown))

    return warnings


# =========================
# Export (docx/xlsx + project zip)
# =========================

def export_docx_for_template(template_type: str, data: Dict[str, Any], title: str) -> bytes:
    sections: List[Tuple[str, str]] = []
    tables: List[Tuple[str, pd.DataFrame]] = []

    if template_type == "教学日历":
        sections = [
            ("基本信息", "\n".join([
                f"课程名称：{data.get('course_name','')}",
                f"学期：{data.get('term','')}",
                f"专业及年级：{data.get('major_and_grade','')}",
                f"主讲教师：{data.get('teacher','')}",
                f"总学时：{data.get('total_hours','')}",
                f"上课周数：{data.get('weeks','')}",
                f"考核方式：{data.get('assessment','')}",
                f"成绩计算方法：{data.get('grade_rule','')}",
            ])),
        ]
        tables = [("教学进度表", pd.DataFrame(data.get("schedule_rows", [])))]
        return docx_export_simple(title, sections, tables)

    if template_type == "课程大纲":
        sections = [
            ("基本信息", "\n".join([
                f"课程名称：{data.get('course_name','')}",
                f"课程代码：{data.get('course_code','')}",
                f"学分：{data.get('credits','')}",
                f"总学时：{data.get('hours_total','')}",
                f"理论学时：{data.get('hours_theory','')}",
                f"实践学时：{data.get('hours_practice','')}",
                f"课程性质：{data.get('course_nature','')}",
                f"先修课程：{data.get('prerequisites','')}",
            ])),
            ("备注", data.get("remarks", "")),
        ]
        tables = [
            ("课程目标", pd.DataFrame(data.get("teaching_objectives", []))),
            ("内容大纲", pd.DataFrame(data.get("content_outline", []))),
            ("考核方式", pd.DataFrame(data.get("assessment", []))),
        ]
        return docx_export_simple(title, sections, tables)

    return docx_export_simple(title, [("内容", json.dumps(data, ensure_ascii=False, indent=2))], [])

def export_xlsx_for_template(template_type: str, data: Dict[str, Any]) -> Optional[bytes]:
    sheets: Dict[str, pd.DataFrame] = {}
    if template_type == "教学日历":
        sheets["教学进度表"] = pd.DataFrame(data.get("schedule_rows", []))
    if template_type == "课程大纲":
        sheets["课程目标"] = pd.DataFrame(data.get("teaching_objectives", []))
        sheets["内容大纲"] = pd.DataFrame(data.get("content_outline", []))
        sheets["考核方式"] = pd.DataFrame(data.get("assessment", []))
    if not sheets:
        return None
    return to_xlsx_bytes(sheets)

def export_project_zip(pid: str) -> bytes:
    prj = load_project(pid)
    plan = load_base_plan(pid)
    docs = list_docs(pid)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if prj:
            z.writestr("project.json", json.dumps(prj.__dict__, ensure_ascii=False, indent=2))
        z.writestr("base_training_plan.json", json.dumps(plan, ensure_ascii=False, indent=2))

        for d in docs:
            doc_id = d["doc_id"]
            z.writestr(f"docs/{doc_id}.json", json.dumps(d, ensure_ascii=False, indent=2))

            safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", d.get("title", doc_id))
            try:
                docx_bytes = export_docx_for_template(d["template_type"], d.get("data", {}), d.get("title", doc_id))
                z.writestr(f"exports/{safe_title}.docx", docx_bytes)
            except Exception as e:
                z.writestr(f"exports/{safe_title}.docx.ERROR.txt", str(e))

            try:
                x = export_xlsx_for_template(d["template_type"], d.get("data", {}))
                if x:
                    z.writestr(f"exports/{safe_title}.xlsx", x)
            except Exception as e:
                z.writestr(f"exports/{safe_title}.xlsx.ERROR.txt", str(e))

        ap = assets_dir(pid)
        if ap.exists():
            for fp in ap.glob("*"):
                if fp.is_file():
                    z.write(fp, arcname=f"assets/{fp.name}")

    return buf.getvalue()


# =========================
# UI helpers (logo)
# =========================

def default_logo_svg(size: int = 44) -> str:
    return f"""
    <svg width="{size}" height="{size}" viewBox="0 0 64 64" fill="none"
         xmlns="http://www.w3.org/2000/svg" aria-label="logo">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="64" y2="64">
          <stop offset="0" stop-color="#3B82F6"/>
          <stop offset="1" stop-color="#6366F1"/>
        </linearGradient>
      </defs>
      <circle cx="32" cy="32" r="30" fill="url(#g)" opacity="0.95"/>
      <path d="M20 22c6-4 18-4 24 0v22c-6-4-18-4-24 0V22z"
            fill="white" opacity="0.95"/>
      <path d="M20 22c6 4 18 4 24 0" stroke="#E5E7EB" stroke-width="2" opacity="0.9"/>
      <circle cx="28" cy="30" r="2.6" fill="#111827" opacity="0.85"/>
      <circle cx="36" cy="28" r="2.6" fill="#111827" opacity="0.85"/>
      <circle cx="40" cy="34" r="2.6" fill="#111827" opacity="0.85"/>
      <path d="M28 30L36 28L40 34L28 30" stroke="#111827" stroke-width="2" opacity="0.7"/>
    </svg>
    """

def _read_project_logo(prj: Project) -> Tuple[str, Optional[bytes], str]:
    """
    返回：
      kind: "svg" / "png" / "default"
      data: bytes for png (or None)
      svg_text: for svg/default
    """
    if prj and prj.logo_file:
        fp = assets_dir(prj.project_id) / prj.logo_file
        if fp.exists():
            if fp.suffix.lower() == ".svg":
                try:
                    return "svg", None, fp.read_text("utf-8")
                except Exception:
                    pass
            if fp.suffix.lower() == ".png":
                try:
                    return "png", fp.read_bytes(), ""
                except Exception:
                    pass
    return "default", None, default_logo_svg(44)

def ui_header(prj: Project):
    kind, png_bytes, svg_text = _read_project_logo(prj)

    if kind == "png" and png_bytes:
        import base64
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        logo_html = f"<img src='data:image/png;base64,{b64}' style='width:44px;height:44px;border-radius:12px;'/>"
    else:
        logo_html = svg_text

    st.markdown(
        f"""
        <div style="padding: 14px 16px; border-radius: 16px;
                    background: linear-gradient(90deg, #f7f8ff 0%, #f8fbff 100%);
                    border: 1px solid #eef;">
          <div style="display:flex; align-items:center; gap:12px;">
            <div style="width:44px;height:44px; display:flex; align-items:center; justify-content:center;">
              {logo_html}
            </div>
            <div>
              <div style="font-size: 28px; font-weight: 800;">教学文件工作台</div>
              <div style="margin-top: 6px; color: #666;">
                项目：<b>{prj.name}</b>（{prj.project_id}） · 最后更新：{prj.updated_at}
              </div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write("")

def ui_sidebar_brand(prj: Project):
    """
    ✅ 修复 sidebar 顶部 logo/HTML 显示成纯文本的问题
    """
    kind, png_bytes, svg_text = _read_project_logo(prj)

    if kind == "png" and png_bytes:
        st.sidebar.image(png_bytes, width=46)
    else:
        # svg 用 components.html，避免 markdown 渲染为文本
        st.sidebar.components.v1.html(svg_text, height=54)

    st.sidebar.markdown(f"### {APP_NAME}")
    st.sidebar.caption(APP_VERSION)


# =========================
# Sidebar: LLM
# =========================

def ui_llm_sidebar(project_obj=None) -> LLMConfig:
    st.sidebar.markdown("---")
    st.sidebar.markdown("### LLM（可选：用于校对/修正/补全）")

    backend = _read_llm_defaults()
    prj_llm = {}
    if project_obj is not None and hasattr(project_obj, "llm") and isinstance(getattr(project_obj, "llm"), dict):
        prj_llm = getattr(project_obj, "llm")

    ui_defaults = {**backend, **prj_llm}

    K_MODE = "llm_cfg_mode"
    K_PRESET = "llm_preset_name"
    K_PROVIDER = "llm_provider"
    K_ENABLED = "llm_enabled"
    K_APIKEY = "llm_api_key"
    K_MODEL_PICK = "llm_model_pick"
    K_MODEL_CUSTOM = "llm_model_custom"
    K_BASE_PICK = "llm_base_pick"
    K_BASE_CUSTOM = "llm_base_custom"
    K_ENDPOINT = "llm_endpoint_url"
    K_API_VER = "llm_api_version"
    K_TIMEOUT = "llm_timeout"
    K_TEMP = "llm_temperature"
    K_MAXTOK = "llm_max_tokens"
    K_EHEAD = "llm_extra_headers_json"
    K_EPARM = "llm_extra_params_json"

    def _init_once():
        if "_llm_inited" in st.session_state:
            return
        st.session_state["_llm_inited"] = True

        st.session_state.setdefault(K_MODE, "自动(推荐)")
        st.session_state.setdefault(K_PRESET, list(PROVIDER_PRESETS.keys())[0] if PROVIDER_PRESETS else "OpenAI / OpenAI兼容（通用）")
        st.session_state.setdefault(K_ENABLED, bool(ui_defaults.get("enabled", False)))
        st.session_state.setdefault(K_APIKEY, str(ui_defaults.get("api_key", "")))
        st.session_state.setdefault(K_PROVIDER, str(ui_defaults.get("provider", "openai_compat")))
        st.session_state.setdefault(K_BASE_CUSTOM, str(ui_defaults.get("base_url", "")))
        st.session_state.setdefault(K_MODEL_CUSTOM, str(ui_defaults.get("model", "")))
        st.session_state.setdefault(K_ENDPOINT, str(ui_defaults.get("endpoint_url", "")))
        st.session_state.setdefault(K_API_VER, str(ui_defaults.get("api_version", "")))
        st.session_state.setdefault(K_TIMEOUT, int(ui_defaults.get("timeout", 60)))
        st.session_state.setdefault(K_TEMP, float(ui_defaults.get("temperature", 0.2)))
        st.session_state.setdefault(K_MAXTOK, int(ui_defaults.get("max_tokens", 2048)))
        st.session_state.setdefault(K_EHEAD, str(ui_defaults.get("extra_headers_json", "")))
        st.session_state.setdefault(K_EPARM, str(ui_defaults.get("extra_params_json", "")))
        st.session_state.setdefault(K_BASE_PICK, "自定义…")
        st.session_state.setdefault(K_MODEL_PICK, "自定义…")

    _init_once()

    def _preset_options(preset: dict, field: str) -> list[str]:
        arr = preset.get(field)
        if isinstance(arr, list) and arr:
            return [str(x) for x in arr if str(x).strip()]
        hint_key = "base_url_hint" if field == "base_urls" else "model_hint"
        hint = str(preset.get(hint_key, "")).strip()
        if hint:
            return [f"（参考）{hint}"]
        return []

    def _apply_preset_defaults(preset_name: str):
        preset = PROVIDER_PRESETS.get(preset_name, {})
        provider = preset.get("provider", "openai_compat")
        st.session_state[K_PROVIDER] = provider

        base_opts = _preset_options(preset, "base_urls")
        model_opts = _preset_options(preset, "models")

        if base_opts and not str(base_opts[0]).startswith("（参考）"):
            st.session_state[K_BASE_PICK] = base_opts[0]
            st.session_state[K_BASE_CUSTOM] = base_opts[0]
        else:
            st.session_state[K_BASE_PICK] = "自定义…"

        if model_opts and not str(model_opts[0]).startswith("（参考）"):
            st.session_state[K_MODEL_PICK] = model_opts[0]
            st.session_state[K_MODEL_CUSTOM] = model_opts[0]
        else:
            st.session_state[K_MODEL_PICK] = "自定义…"

        if provider == "anthropic" and not st.session_state.get(K_ENDPOINT, "").strip():
            st.session_state[K_ENDPOINT] = "https://api.anthropic.com/v1/messages"

    mode = st.sidebar.selectbox(
        "配置来源",
        ["自动(推荐)", "仅后台", "仅页面", "合并(页面优先)"],
        key=K_MODE,
    )

    preset_name = st.sidebar.selectbox(
        "Provider 选择",
        list(PROVIDER_PRESETS.keys()),
        key=K_PRESET,
        on_change=lambda: _apply_preset_defaults(st.session_state[K_PRESET]),
    )
    preset = PROVIDER_PRESETS.get(preset_name, {})
    provider = st.session_state.get(K_PROVIDER, preset.get("provider", "openai_compat"))

    enabled = st.sidebar.checkbox("启用 LLM 校对与修正", key=K_ENABLED)
    st.sidebar.caption(f"当前 Provider：`{provider}`")

    model_opts = _preset_options(preset, "models")
    model_pick_list = (model_opts + ["自定义…"]) if model_opts else ["自定义…"]
    model_pick = st.sidebar.selectbox("Model（可选）", model_pick_list, key=K_MODEL_PICK)

    if model_pick == "自定义…" or str(model_pick).startswith("（参考）"):
        model_custom = st.sidebar.text_input("Model（自定义输入）", key=K_MODEL_CUSTOM)
        model_final = model_custom.strip()
    else:
        model_final = str(model_pick).strip()

    # base url
    base_opts = []  # 兼容：你也可以在 preset 里加 base_urls 列表
    base_pick_list = (base_opts + ["自定义…"]) if base_opts else ["自定义…"]
    base_pick = st.sidebar.selectbox("Base URL（可选）", base_pick_list, key=K_BASE_PICK)

    if base_pick == "自定义…" or str(base_pick).startswith("（参考）"):
        base_custom = st.sidebar.text_input("Base URL（自定义输入）", key=K_BASE_CUSTOM, help=preset.get("base_url_hint", ""))
        base_final = base_custom.strip()
    else:
        base_final = str(base_pick).strip()

    api_key = st.sidebar.text_input("API Key", key=K_APIKEY, type="password")
    endpoint_url = st.sidebar.text_input("Endpoint URL（可选）", key=K_ENDPOINT)

    api_version = ""
    if provider == "anthropic":
        api_version = st.sidebar.text_input("Anthropic-Version（可选）", key=K_API_VER)
    else:
        api_version = st.session_state.get(K_API_VER, "")

    timeout = st.sidebar.slider("超时（秒）", 10, 180, key=K_TIMEOUT)
    temperature = st.sidebar.slider("temperature", 0.0, 1.5, key=K_TEMP)
    max_tokens = st.sidebar.slider("max_tokens", 256, 8192, key=K_MAXTOK, step=256)

    extra_headers_json = st.sidebar.text_area("额外 Headers（JSON，可选）", key=K_EHEAD, height=80)
    extra_params_json = st.sidebar.text_area("额外 Params（JSON，可选）", key=K_EPARM, height=80)

    ui_cfg = dict(
        enabled=bool(enabled),
        provider=str(provider),
        api_key=str(api_key),
        base_url=str(base_final),
        model=str(model_final),
        timeout=int(timeout),
        temperature=float(temperature),
        max_tokens=int(max_tokens),
        extra_headers_json=str(extra_headers_json),
        extra_params_json=str(extra_params_json),
        endpoint_url=str(endpoint_url),
        api_version=str(api_version),
    )

    if mode == "仅后台":
        merged = dict(backend)
    elif mode == "仅页面":
        merged = dict(ui_cfg)
    else:
        merged = dict(backend)
        for k, v in ui_cfg.items():
            if k == "enabled":
                merged[k] = bool(v)
                continue
            if isinstance(v, str):
                if v.strip() != "":
                    merged[k] = v.strip()
            elif v is not None:
                merged[k] = v

    llm_cfg = LLMConfig(**merged)

    if project_obj is not None:
        st.sidebar.markdown("---")
        if st.sidebar.button("保存为本项目默认（不含Key）", use_container_width=True):
            safe = dict(merged)
            safe["api_key"] = ""
            try:
                project_obj.llm = safe
                save_project(project_obj)
                st.sidebar.success("已保存（Key未写入项目）。")
            except Exception as e:
                st.sidebar.error(f"保存失败：{e}")

    return llm_cfg


# =========================
# Sidebar / Pages
# =========================

def ui_project_sidebar() -> Tuple[Project, LLMConfig]:
    projects = list_projects()

    if not projects:
        pid = uuid.uuid4().hex[:10]
        prj = Project(project_id=pid, name=f"默认项目-{dt.datetime.now().strftime('%Y%m%d-%H%M')}")
        save_project(prj)
        projects = [prj]
        st.session_state["active_project_id"] = pid

    active_id = st.session_state.get("active_project_id", projects[0].project_id)
    active = load_project(active_id) or projects[0]

    # ✅ sidebar 顶部品牌区（修复 HTML 显示为文本）
    ui_sidebar_brand(active)

    names = ["➕ 新建项目"] + [f"{p.name}  ({p.project_id})" for p in projects]
    default_index = 1
    for i, p in enumerate(projects, start=1):
        if p.project_id == active.project_id:
            default_index = i
            break

    choice = st.sidebar.selectbox("项目", names, index=default_index, key="project_choice")
    if choice.startswith("➕"):
        with st.sidebar.expander("新建项目", expanded=True):
            new_name = st.text_input("项目名称", value=f"项目-{dt.datetime.now().strftime('%Y%m%d-%H%M')}")
            if st.button("创建项目", use_container_width=True):
                pid = uuid.uuid4().hex[:10]
                prj = Project(project_id=pid, name=new_name)
                save_project(prj)
                st.session_state["active_project_id"] = pid
                st.rerun()
    else:
        pid = choice.split("(")[-1].strip(")")
        st.session_state["active_project_id"] = pid
        active = load_project(pid) or projects[0]

    llm_cfg = ui_llm_sidebar(project_obj=active)

    st.sidebar.markdown("---")
    st.sidebar.markdown("#### 项目 Logo（可选）")

    logo_up = st.sidebar.file_uploader("上传 Logo（PNG/SVG）", type=["png", "svg"], key="logo_uploader")
    if logo_up is not None:
        b = logo_up.read()
        ensure_dir(assets_dir(active.project_id))
        fname = f"logo.{logo_up.name.split('.')[-1].lower()}"
        (assets_dir(active.project_id) / fname).write_bytes(b)
        active.logo_file = fname
        save_project(active)
        st.sidebar.success("Logo 已保存到项目。")
        st.rerun()

    if active.logo_file:
        if st.sidebar.button("清除项目 Logo", use_container_width=True):
            active.logo_file = ""
            save_project(active)
            st.sidebar.success("已清除。")
            st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 导出/打包")
    if st.sidebar.button("打包导出（JSON + Docx/Xlsx）", use_container_width=True):
        z = export_project_zip(active.project_id)
        st.sidebar.download_button(
            "下载项目zip",
            data=z,
            file_name=f"{active.name}-{active.project_id}.zip",
            mime="application/zip",
            use_container_width=True,
        )

    return active, llm_cfg


# =========================
# Base plan UI
# =========================

def _graph_to_dot(g: Dict[str, Any]) -> str:
    nodes = g.get("nodes", []) if isinstance(g, dict) else []
    edges = g.get("edges", []) if isinstance(g, dict) else []
    lines = ["digraph G {", "rankdir=LR;", 'node [shape=box, style="rounded"];']
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", "")).strip() or str(n.get("name", "")).strip()
        label = str(n.get("label", "") or n.get("name", nid))
        if nid:
            label = label.replace('"', '\\"')
            lines.append(f'"{nid}" [label="{label}"];')
    for e in edges:
        if not isinstance(e, dict):
            continue
        a = str(e.get("from", "")).strip()
        b = str(e.get("to", "")).strip()
        lab = str(e.get("label", "")).strip()
        if a and b:
            lab = lab.replace('"', '\\"')
            if lab:
                lines.append(f'"{a}" -> "{b}" [label="{lab}"];')
            else:
                lines.append(f'"{a}" -> "{b}";')
    lines.append("}")
    return "\n".join(lines)

def _get_plan_rev(plan: Dict[str, Any]) -> int:
    try:
        return int((plan.get("meta", {}) or {}).get("rev", 0))
    except Exception:
        return 0

def ui_base_training_plan(pid: str, llm_cfg: LLMConfig):
    st.subheader("培养方案基座（全量内容库）")
    st.caption("先把培养方案整理成权威内容库；后续所有教学文件将以此做一致性校验与自动填充。")

    plan = load_base_plan(pid) or {
        "meta": {"rev": 0},
        "sections": {},
        "appendices": {"tables": {t: [] for t in APPENDIX_TITLES}},
        "course_graph": {"nodes": [], "edges": []},
        "raw_pages_text": [],
    }

    rev = _get_plan_rev(plan)

    colL, colR = st.columns([1, 2])

    with colL:
        up = st.file_uploader("上传培养方案 PDF（可选）", type=["pdf"], key=f"plan_pdf_uploader_{pid}")
        if up:
            pdf_bytes = up.read()
            st.info("已读取PDF。你可以点击“抽取并写入基座”。")
            if st.button("抽取并写入基座", type="primary", use_container_width=True, key=f"btn_extract_plan_{pid}"):
                extracted = base_plan_from_pdf(pdf_bytes)
                save_base_plan(pid, extracted)
                st.success("已写入培养方案基座（已尝试自动抽取 7-10 附表）。")
                st.rerun()

        st.write("")
        json_download_button("下载基座JSON", plan, f"base_training_plan-{pid}.json", key=f"dl_base_plan_json_{pid}")

        st.write("")
        if st.button("检查：是否缺少关键栏目(1-11)", use_container_width=True, key=f"btn_check_plan_missing_{pid}"):
            missing = [t for t in SECTION_TITLES if not clean_text(plan.get("sections", {}).get(t, ""))]
            if missing:
                st.warning("缺少栏目文本（PDF可能只有标题/或抽取失败）：\n- " + "\n- ".join(missing))
            else:
                st.success("1-11 栏目文本均已存在（仍建议人工快速扫读）。")

        st.write("")
        with st.expander("调试：分页原文（raw_pages_text）", expanded=False):
            pages = plan.get("raw_pages_text", [])
            st.write(f"页数：{len(pages)}")
            if pages:
                pno = st.number_input("页码（从0开始）", min_value=0, max_value=max(0, len(pages) - 1), value=0, key=f"pno_{pid}_{rev}")
                st.text_area("该页文本", value=clamp(str(pages[int(pno)]), 20000), height=240, key=f"raw_page_{pid}_{rev}")

        with st.expander("调试：附表抽取信息（appendix_debug）", expanded=False):
            dbg = (plan.get("meta", {}) or {}).get("appendix_debug", {})
            st.json(dbg)

    with colR:
        st.markdown("##### 培养方案内容（按栏目展示，可编辑）")

        sections = plan.get("sections", {}) if isinstance(plan.get("sections", {}), dict) else {}
        append_tables = (plan.get("appendices", {}) or {}).get("tables", {}) or {}
        if not isinstance(append_tables, dict):
            append_tables = {}

        graph = plan.get("course_graph", {"nodes": [], "edges": []})
        if not isinstance(graph, dict):
            graph = {"nodes": [], "edges": []}

        tabs = st.tabs(SECTION_TITLES)

        # 1-6：正文文本
        for i, title in enumerate(SECTION_TITLES[:6]):
            with tabs[i]:
                sections[title] = st.text_area(
                    title,
                    value=sections.get(title, ""),
                    height=280,
                    key=f"plan_text_{pid}_{rev}_{i}",
                )

                if llm_available(llm_cfg):
                    if st.button(f"用 LLM 校对该栏目：{title}", key=f"btn_llm_fix_{pid}_{rev}_{i}"):
                        system = "你是高校培养方案的严谨审校助手。对给定栏目做纠错、断行修复、编号修复；尽量不改原意。只输出JSON。"
                        schema_hint = json.dumps({
                            "title": title,
                            "corrected_text": "纠错后的完整栏目文本（保持原意，修正常见断行/丢字/编号）",
                            "warnings": ["可选：缺失/疑点提示"],
                        }, ensure_ascii=False, indent=2)
                        user = f"栏目标题：{title}\n\n原文：\n{sections[title]}\n"
                        obj, raw = llm_chat_json(llm_cfg, system, user, schema_hint=schema_hint)
                        if obj and obj.get("corrected_text"):
                            sections[title] = obj["corrected_text"]
                            plan["sections"] = sections
                            plan.setdefault("llm_log", []).append({"at": now_str(), "title": title, "raw": raw})
                            plan.setdefault("meta", {})["updated_at"] = now_str()
                            save_base_plan(pid, plan)
                            st.success("已应用LLM修正。")
                            st.rerun()
                        else:
                            st.error("LLM未返回可用JSON。")
                            st.code(raw)

        # 7-10：附表（上面显示“文本抽取结果”，下面自动填表 + 可编辑）
        for j, title in enumerate(SECTION_TITLES[6:10], start=6):
            with tabs[j]:
                st.caption("该栏目多为附表：已尝试从 PDF 后部抽取表格并自动填充；你也可在下方表格区手工修改。")

                # 文本抽取结果（通常只有标题）
                text_excerpt = sections.get(title, "")
                st.markdown(f"**{title}（文本抽取结果）**")
                st.text_area("",
                             value=clamp(text_excerpt, 8000),
                             height=160,
                             key=f"plan_text_excerpt_{pid}_{rev}_{j}")

                st.markdown("**表格区（可编辑，行可增删）**")
                rows = append_tables.get(title, [])
                if not isinstance(rows, list):
                    rows = []
                df = dataframe_safe(pd.DataFrame(rows))
                edited_df = st.data_editor(
                    df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key=f"plan_tbl_{pid}_{rev}_{j}",
                )
                append_tables[title] = edited_df.to_dict(orient="records")

        # 11：关系图
        with tabs[10]:
            st.caption("关系图：用 nodes/edges 表达课程之间的先修/支撑/并行关系。")
            colA, colB = st.columns(2)

            with colA:
                st.markdown("**Nodes**（建议字段：id / name / label）")
                nodes_df = dataframe_safe(pd.DataFrame(graph.get("nodes", [])))
                nodes_df = st.data_editor(
                    nodes_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key=f"graph_nodes_editor_{pid}_{rev}",
                )
                graph["nodes"] = nodes_df.to_dict(orient="records")

            with colB:
                st.markdown("**Edges**（建议字段：from / to / label）")
                edges_df = dataframe_safe(pd.DataFrame(graph.get("edges", [])))
                edges_df = st.data_editor(
                    edges_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key=f"graph_edges_editor_{pid}_{rev}",
                )
                graph["edges"] = edges_df.to_dict(orient="records")

            st.markdown("**预览**")
            try:
                st.graphviz_chart(_graph_to_dot(graph), use_container_width=True)
            except Exception:
                st.code(_graph_to_dot(graph))

            if llm_available(llm_cfg):
                with st.expander("LLM：根据文本生成/完善关系图（可选）", expanded=False):
                    hint = st.text_area("补充要求（可选）", value="尽量不要编造；优先从课程表/课程名中抽取；给出nodes/edges。", height=80, key=f"graph_hint_{pid}_{rev}")
                    if st.button("用LLM生成/完善关系图", key=f"btn_llm_graph_build_{pid}_{rev}"):
                        system = "你是培养方案课程关系图构建助手。根据输入文本，输出JSON：{nodes:[...], edges:[...] }。只输出JSON。"
                        schema_hint = json.dumps({"nodes":[{"id":"", "name":"", "label":""}], "edges":[{"from":"","to":"","label":""}], "warnings":[]}, ensure_ascii=False, indent=2)
                        user = f"培养方案关键文本：\n{sections.get('四、主干学科、专业核心课程和主要实践性教学环节','')}\n\n补充要求：{hint}"
                        obj, raw = llm_chat_json(llm_cfg, system, user, schema_hint=schema_hint)
                        if obj and isinstance(obj, dict) and "nodes" in obj and "edges" in obj:
                            graph["nodes"] = obj.get("nodes", [])
                            graph["edges"] = obj.get("edges", [])
                            plan["course_graph"] = graph
                            plan.setdefault("llm_log", []).append({"at": now_str(), "title": "graph_build", "raw": raw})
                            plan.setdefault("meta", {})["updated_at"] = now_str()
                            save_base_plan(pid, plan)
                            st.success("已写入关系图。")
                            st.rerun()
                        else:
                            st.error("LLM未返回可用 nodes/edges JSON。")
                            st.code(raw)

        st.write("")
        if st.button("保存基座（全部栏目）", type="primary", use_container_width=True, key=f"btn_save_base_all_{pid}_{rev}"):
            plan["sections"] = sections
            plan.setdefault("appendices", {})["tables"] = append_tables
            plan["course_graph"] = graph
            plan.setdefault("meta", {})["updated_at"] = now_str()
            plan.setdefault("meta", {})["rev"] = int(dt.datetime.now().timestamp())
            save_base_plan(pid, plan)
            st.success("已保存。")
            st.rerun()


# =========================
# Template UI (保持你之前的逻辑，略)
# =========================

def ui_edit_table_of_dicts(title: str, rows: List[Dict[str, Any]], columns: List[str]) -> List[Dict[str, Any]]:
    st.caption(title)
    df = pd.DataFrame(rows or [], columns=columns)
    df = dataframe_safe(df)
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True)
    for c in columns:
        if c not in edited.columns:
            edited[c] = ""
    edited = edited[columns]
    return edited.to_dict(orient="records")

def ui_render_editor(template_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    data = merge_by_schema(schema_for(template_type), data or {})

    if template_type == "教学日历":
        st.markdown("###### 基本信息")
        c1, c2, c3 = st.columns(3)
        data["course_name"] = c1.text_input("课程名称", value=data.get("course_name", ""))
        data["term"] = c2.text_input("学期", value=data.get("term", ""))
        data["major_and_grade"] = c3.text_input("专业及年级", value=data.get("major_and_grade", ""))

        c4, c5, c6 = st.columns(3)
        data["teacher"] = c4.text_input("主讲教师", value=data.get("teacher", ""))
        data["total_hours"] = c5.text_input("总学时", value=data.get("total_hours", ""))
        data["weeks"] = c6.text_input("上课周数", value=data.get("weeks", ""))

        c7, c8 = st.columns(2)
        data["assessment"] = c7.text_input("考核方式", value=data.get("assessment", ""))
        data["grade_rule"] = c8.text_input("成绩计算方法", value=data.get("grade_rule", ""))

        st.markdown("###### 教材 / 参考书")
        data["textbook"] = ui_edit_table_of_dicts("教材", data.get("textbook", []), ["name", "press", "year"])
        data["references"] = ui_edit_table_of_dicts("参考书目", data.get("references", []), ["name", "press", "year"])

        st.markdown("###### 教学进度表（可直接编辑）")
        df = dataframe_safe(pd.DataFrame(data.get("schedule_rows", [])))
        edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True)
        data["schedule_rows"] = edited_df.to_dict(orient="records")
        return data

    if template_type == "课程大纲":
        st.markdown("###### 基本信息")
        c1, c2, c3, c4 = st.columns(4)
        data["course_name"] = c1.text_input("课程名称", value=data.get("course_name", ""))
        data["course_code"] = c2.text_input("课程代码", value=data.get("course_code", ""))
        data["credits"] = c3.text_input("学分", value=data.get("credits", ""))
        data["hours_total"] = c4.text_input("总学时", value=data.get("hours_total", ""))

        c5, c6, c7, c8 = st.columns(4)
        data["hours_theory"] = c5.text_input("理论学时", value=data.get("hours_theory", ""))
        data["hours_practice"] = c6.text_input("实践学时", value=data.get("hours_practice", ""))
        data["course_nature"] = c7.text_input("课程性质", value=data.get("course_nature", ""))
        data["prerequisites"] = c8.text_input("先修课程", value=data.get("prerequisites", ""))

        st.markdown("###### 课程目标（建议填支撑毕业要求码，如 5.2）")
        data["teaching_objectives"] = ui_edit_table_of_dicts("课程目标", data.get("teaching_objectives", []), ["id", "text", "support_grad_req"])

        st.markdown("###### 教学内容与学时分配")
        data["content_outline"] = ui_edit_table_of_dicts("内容大纲", data.get("content_outline", []), ["module", "hours", "topics"])

        st.markdown("###### 考核方式与比例")
        data["assessment"] = ui_edit_table_of_dicts("考核项", data.get("assessment", []), ["item", "weight", "notes"])

        data["remarks"] = st.text_area("备注", value=data.get("remarks", ""), height=120)
        return data

    if template_type == "授课手册":
        st.markdown("###### 基本信息")
        c1, c2, c3, c4 = st.columns(4)
        data["course_name"] = c1.text_input("课程名称", value=data.get("course_name", ""))
        data["term"] = c2.text_input("学期", value=data.get("term", ""))
        data["class"] = c3.text_input("班级", value=data.get("class", ""))
        data["teacher"] = c4.text_input("教师", value=data.get("teacher", ""))

        st.markdown("###### 周记录")
        data["weekly_log"] = ui_edit_table_of_dicts("周记录", data.get("weekly_log", []), ["date", "progress", "issues", "actions"])

        st.markdown("###### 总结/分析/改进")
        data["summary"] = st.text_area("课程总结", value=data.get("summary", ""), height=120)
        data["exam_analysis"] = st.text_area("试卷分析", value=data.get("exam_analysis", ""), height=120)
        data["improvement"] = st.text_area("改进措施", value=data.get("improvement", ""), height=120)
        return data

    if template_type == "达成度评价依据审核表":
        st.markdown("###### 基本信息")
        c1, c2 = st.columns(2)
        data["course_name"] = c1.text_input("课程名称", value=data.get("course_name", ""))
        data["term"] = c2.text_input("学期", value=data.get("term", ""))

        st.markdown("###### 评价依据")
        ev = data.get("evidence_used", {})
        cols = st.columns(5)
        keys = list(ev.keys()) if isinstance(ev, dict) else ["期末试卷", "平时考试", "作业", "实验", "讨论小论文"]
        ev2: Dict[str, bool] = {}
        for i, k in enumerate(keys):
            ev2[k] = cols[i % 5].checkbox(k, value=bool(ev.get(k, False)))
        data["evidence_used"] = ev2

        data["calc_method"] = st.text_area("计算方法说明", value=data.get("calc_method", ""), height=120)
        data["conclusion"] = st.text_area("结论", value=data.get("conclusion", ""), height=100)
        c3, c4 = st.columns(2)
        data["review_team"] = c3.text_input("审核小组/人员", value=data.get("review_team", ""))
        data["review_date"] = c4.text_input("审核日期", value=data.get("review_date", ""))
        return data

    if template_type == "达成度评价报告":
        st.markdown("###### 基本信息")
        c1, c2, c3 = st.columns(3)
        data["course_name"] = c1.text_input("课程名称", value=data.get("course_name", ""))
        data["term"] = c2.text_input("学期", value=data.get("term", ""))
        data["threshold"] = c3.text_input("达成阈值（如0.65）", value=str(data.get("threshold", "0.65")))

        st.markdown("###### 课程目标达成情况（可编辑）")
        data["objectives"] = ui_edit_table_of_dicts("目标达成", data.get("objectives", []),
                                                    ["obj", "support_grad_req", "direct_score", "self_score", "achieved"])

        st.markdown("###### 结论与改进")
        data["overall_comment"] = st.text_area("总体评价", value=data.get("overall_comment", ""), height=100)
        data["analysis"] = st.text_area("原因分析", value=data.get("analysis", ""), height=120)
        data["improvements"] = st.text_area("改进措施", value=data.get("improvements", ""), height=120)
        data["weakness"] = st.text_area("薄弱环节", value=data.get("weakness", ""), height=80)
        data["next_suggestions"] = st.text_area("下轮建议", value=data.get("next_suggestions", ""), height=100)

        st.markdown("###### 签字")
        c4, c5, c6, c7 = st.columns(4)
        data["responsible"] = c4.text_input("负责人", value=data.get("responsible", ""))
        data["date"] = c5.text_input("日期", value=data.get("date", ""))
        data["reviewer"] = c6.text_input("审核人", value=data.get("reviewer", ""))
        data["review_date"] = c7.text_input("审核日期", value=data.get("review_date", ""))
        return data

    if template_type == "调查问卷":
        st.markdown("###### 基本信息")
        c1, c2 = st.columns(2)
        data["title"] = c1.text_input("问卷标题", value=data.get("title", ""))
        data["target"] = c2.text_input("调查对象", value=data.get("target", ""))
        st.markdown("###### 题目列表（可编辑）")
        data["questions"] = ui_edit_table_of_dicts("题目", data.get("questions", []), ["q", "type", "options"])
        return data

    st.json(data)
    return data

def ui_templates(pid: str, llm_cfg: LLMConfig):
    st.subheader("模板化教学文件（上传/粘贴 → 抽取填充 → 校对 → 导出）")
    st.caption("把易模式化文件做成固定模板；上传现有文档后抽取填充，人工确认后导出规范文档，并项目化保存/打包。")

    colL, colR = st.columns([1.1, 1.9])

    with colL:
        st.markdown("##### 新建文档")
        ttype = st.selectbox("模板类型", TEMPLATE_TYPES, key="new_ttype")
        title = st.text_input("文档标题（项目内）", value="", key="new_title")

        if st.button("新建文档", type="primary", use_container_width=True, key="btn_new_doc"):
            doc_obj = new_doc_object(ttype, title=title)
            doc_obj["data"] = schema_for(ttype)
            save_doc(pid, doc_obj)
            st.session_state["active_doc_id"] = doc_obj["doc_id"]
            st.success("已新建。")
            st.rerun()

        st.write("")
        st.markdown("##### 导入已有内容")
        up = st.file_uploader("上传 docx（推荐）", type=["docx"], key="docx_uploader")
        pasted = st.text_area("或粘贴全文（可选）", height=120, key="paste_fulltext")

        if st.button("抽取并填充到当前文档", use_container_width=True, key="btn_fill_current"):
            doc_id = st.session_state.get("active_doc_id")
            if not doc_id:
                st.error("请先新建/选择一个文档。")
            else:
                doc_file = doc_path(pid, doc_id)
                if not doc_file.exists():
                    st.error("当前文档不存在。")
                else:
                    doc_obj = safe_json_load(doc_file.read_text("utf-8"), {})
                    src_text, src_tables = "", []
                    if up:
                        b = up.read()
                        src_text, src_tables = docx_extract_text_tables(b)
                        doc_obj["source"]["uploaded_filename"] = up.name
                        doc_obj["source"]["sha256"] = sha256_bytes(b)
                    if pasted.strip():
                        src_text = (src_text + "\n" + pasted).strip()

                    doc_obj["raw"]["text"] = src_text
                    doc_obj["raw"]["tables"] = [dataframe_safe(df).to_dict(orient="records") for df in src_tables]

                    doc_obj["history"].append({"at": now_str(), "action": "heuristic_fill", "data": doc_obj.get("data", {})})
                    doc_obj["data"] = heuristic_fill(doc_obj["template_type"], src_text, src_tables)
                    save_doc(pid, doc_obj)

                    st.success("已填充。请在右侧校对/编辑。")
                    st.rerun()

        st.write("")
        st.markdown("##### 文档列表")
        docs = list_docs(pid)
        if not docs:
            st.info("暂无文档。")
        else:
            opts = [f"{d['title']}  [{d['template_type']}]  ({d['doc_id']})" for d in docs]
            idx = 0
            cur = st.session_state.get("active_doc_id")
            if cur:
                for i, d in enumerate(docs):
                    if d["doc_id"] == cur:
                        idx = i
                        break
            choice = st.selectbox("选择文档", opts, index=idx, key="doc_selector")
            st.session_state["active_doc_id"] = choice.split("(")[-1].strip(")")

            c1, c2 = st.columns(2)
            with c1:
                if st.button("删除该文档", use_container_width=True, key="btn_delete_doc"):
                    delete_doc(pid, st.session_state["active_doc_id"])
                    st.session_state["active_doc_id"] = None
                    st.success("已删除。")
                    st.rerun()
            with c2:
                doc_obj = safe_json_load(doc_path(pid, st.session_state["active_doc_id"]).read_text("utf-8"), {})
                json_download_button("下载该文档JSON", doc_obj, f"{doc_obj.get('title','doc')}-{doc_obj.get('doc_id','')}.json", key="dl_doc_json")

    with colR:
        doc_id = st.session_state.get("active_doc_id")

        if not doc_id:
            st.markdown("##### 模板预览（未保存草稿）")
            draft_type = st.session_state.get("new_ttype", TEMPLATE_TYPES[0])
            draft = st.session_state.get("draft_data")
            if not isinstance(draft, dict) or st.session_state.get("draft_type") != draft_type:
                draft = schema_for(draft_type)
                st.session_state["draft_data"] = draft
                st.session_state["draft_type"] = draft_type

            edited_draft = ui_render_editor(draft_type, draft)
            st.session_state["draft_data"] = edited_draft

            if st.button("保存为新文档", type="primary", use_container_width=True, key="btn_save_draft_as_doc"):
                doc_obj = new_doc_object(draft_type, title=f"{draft_type}-{dt.datetime.now().strftime('%Y%m%d-%H%M')}")
                doc_obj["data"] = edited_draft
                save_doc(pid, doc_obj)
                st.session_state["active_doc_id"] = doc_obj["doc_id"]
                st.success("已保存为新文档。")
                st.rerun()
            return

        doc_file = doc_path(pid, doc_id)
        if not doc_file.exists():
            st.warning("文档不存在。")
            return

        doc_obj = safe_json_load(doc_file.read_text("utf-8"), {})
        if not doc_obj:
            st.warning("文档读取失败。")
            return

        new_type = st.selectbox(
            "当前文档模板类型（切换后右侧编辑器会联动）",
            TEMPLATE_TYPES,
            index=TEMPLATE_TYPES.index(doc_obj["template_type"]),
            key=f"active_doc_type_{doc_id}",
        )
        if new_type != doc_obj["template_type"]:
            doc_obj["history"].append({"at": now_str(), "action": "change_template_type", "data": doc_obj.get("data", {})})
            doc_obj["template_type"] = new_type
            doc_obj["data"] = merge_by_schema(schema_for(new_type), doc_obj.get("data", {}))
            save_doc(pid, doc_obj)
            st.rerun()

        st.markdown(f"##### 编辑：{doc_obj['title']} · {doc_obj['template_type']}")
        st.caption(f"更新时间：{doc_obj.get('updated_at','')} · 来源：{doc_obj.get('source',{}).get('uploaded_filename','(无)')}")

        if llm_available(llm_cfg):
            with st.expander("LLM：结构化重建 / 校对（可选）", expanded=False):
                extra = st.text_area("额外要求（可选）", value="尽量保留原意；修复断行；字段找不到就留空并给warnings。", height=80)
                if st.button("用LLM重建结构化数据", type="primary", key="btn_llm_rebuild_doc"):
                    schema_hint = json.dumps(schema_for(doc_obj["template_type"]), ensure_ascii=False, indent=2)
                    system = (
                        "你是高校教学质量管理系统的结构化抽取助手。"
                        "把给定文档（文本+表格records）抽取成指定JSON结构。"
                        "不要编造；找不到填空；必要时写warnings。只输出JSON。"
                    )
                    user = (
                        f"模板类型：{doc_obj['template_type']}\n"
                        f"文档标题：{doc_obj['title']}\n\n"
                        f"文本：\n{doc_obj.get('raw',{}).get('text','')}\n\n"
                        f"表格records：\n{json.dumps(doc_obj.get('raw',{}).get('tables',[]), ensure_ascii=False)}\n\n"
                        f"额外要求：{extra}\n"
                    )
                    obj, raw = llm_chat_json(llm_cfg, system, user, schema_hint=schema_hint)
                    if obj:
                        doc_obj["history"].append({"at": now_str(), "action": "llm_rebuild", "data": doc_obj.get("data", {})})
                        doc_obj["data"] = merge_by_schema(schema_for(doc_obj["template_type"]), obj)
                        doc_obj["llm"]["last_prompt"] = extra
                        doc_obj["llm"]["last_raw_response"] = raw
                        save_doc(pid, doc_obj)
                        st.success("已应用LLM结果。")
                        st.rerun()
                    else:
                        st.error("LLM未返回有效JSON。")
                        st.code(raw)

        edited = ui_render_editor(doc_obj["template_type"], doc_obj.get("data", {}))

        plan = load_base_plan(pid)
        warnings = run_consistency_checks(doc_obj["template_type"], edited, plan) if plan else []
        if warnings:
            with st.expander("一致性检查提示", expanded=True):
                for w in warnings:
                    st.warning(w)

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("保存", type="primary", use_container_width=True, key="btn_save_doc"):
                doc_obj["data"] = edited
                save_doc(pid, doc_obj)
                st.success("已保存。")
        with c2:
            if st.button("回滚到上一版", use_container_width=True, key="btn_rollback_doc"):
                if doc_obj.get("history"):
                    last = doc_obj["history"].pop()
                    doc_obj["data"] = last.get("data", doc_obj["data"])
                    save_doc(pid, doc_obj)
                    st.success("已回滚。")
                    st.rerun()
                else:
                    st.info("没有历史记录。")
        with c3:
            json_download_button("下载JSON", doc_obj, f"{doc_obj['title']}-{doc_obj['doc_id']}.json", key="dl_doc_json2")

        st.write("")
        c4, c5 = st.columns(2)
        with c4:
            b = export_docx_for_template(doc_obj["template_type"], edited, doc_obj["title"])
            st.download_button(
                "导出并下载 DOCX",
                data=b,
                file_name=f"{doc_obj['title']}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
                key="dl_docx",
            )
        with c5:
            x = export_xlsx_for_template(doc_obj["template_type"], edited)
            if x:
                st.download_button(
                    "导出并下载 XLSX（表格）",
                    data=x,
                    file_name=f"{doc_obj['title']}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    key="dl_xlsx",
                )
            else:
                st.info("该模板无可导出表格。")

        with st.expander("原始抽取（文本/表格）", expanded=False):
            st.text_area("raw_text", value=clamp(doc_obj.get("raw", {}).get("text", "")), height=220, key="raw_text_view")
            rt = doc_obj.get("raw", {}).get("tables", [])
            if rt:
                st.caption(f"raw_tables: {len(rt)} 个（展示第1个）")
                st.json(rt[0])


# =========================
# Main
# =========================

def main():
    st.set_page_config(page_title=APP_NAME, layout="wide")
    ensure_dir(DATA_ROOT)

    prj, llm_cfg = ui_project_sidebar()
    ui_header(prj)

    st.markdown("""
    <style>
    div[data-baseweb="tab-list"] { flex-wrap: wrap !important; gap: 6px !important; }
    button[data-baseweb="tab"] { height: auto !important; padding-top: 6px !important; padding-bottom: 6px !important; }
    </style>
    """, unsafe_allow_html=True)

    tabs = st.tabs(["培养方案基座", "模板化教学文件", "项目概览"])
    with tabs[0]:
        ui_base_training_plan(prj.project_id, llm_cfg)
    with tabs[1]:
        ui_templates(prj.project_id, llm_cfg)
    with tabs[2]:
        st.subheader("项目概览")
        plan = load_base_plan(prj.project_id) or {}
        docs = list_docs(prj.project_id)

        st.write({"project_id": prj.project_id, "name": prj.name, "created_at": prj.created_at, "updated_at": prj.updated_at})
        st.write("")
        st.markdown("##### 基座状态")
        secs = (plan.get("sections", {}) or {})
        st.write(f"栏目文本(1-11)：{sum(1 for t in SECTION_TITLES if clean_text((secs or {}).get(t,'')))} / 11")
        tabs_tbl = ((plan.get("appendices", {}) or {}).get("tables", {}) or {})
        st.write(f"附表(7-10)行数： " + " · ".join([f"{k}:{len(v or [])}" for k, v in tabs_tbl.items()]))

        g = (plan.get("course_graph", {}) or {})
        st.write(f"关系图节点：{len((g.get('nodes',[]) or []))} · 边：{len((g.get('edges',[]) or []))}")

        st.write("")
        st.markdown("##### 文件列表")
        if docs:
            df = pd.DataFrame([{
                "doc_id": d["doc_id"],
                "title": d["title"],
                "type": d["template_type"],
                "updated_at": d.get("updated_at",""),
                "source": d.get("source",{}).get("uploaded_filename",""),
            } for d in docs])
            render_table_html(df, height=320)
        else:
            st.info("暂无教学文件。")

        st.write("")
        z = export_project_zip(prj.project_id)
        st.download_button("下载项目zip（JSON+导出）", data=z, file_name=f"{prj.name}-{prj.project_id}.zip", mime="application/zip", use_container_width=True)

if __name__ == "__main__":
    main()
