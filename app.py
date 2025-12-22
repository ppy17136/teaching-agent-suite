# -*- coding: utf-8 -*-
"""
教学智能体平台（单文件版 app.py）- 整合PDF全量抽取版
整合了优化后的PDF抽取功能，具备：
1) 培养方案PDF全量抽取（文本+表格+结构化解析）
2) 识别清单可编辑确认后再保存
3) 表格以data_editor形式展示便于修正
4) 保留原有的依赖追溯和版本管理
"""

import os
import io
import re
import json
import time
import base64
import hashlib
import sqlite3
import zipfile
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
import pandas as pd
import streamlit as st
import requests
import numpy as np
from PIL import Image, ImageOps

# -------- 可选解析依赖 --------
try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    from docx import Document
except Exception:
    Document = None

# ---------------------------
# 基础配置
# ---------------------------
st.set_page_config(page_title="教学智能体平台", layout="wide")

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_TEXT_MODEL = "qwen-max"
DEFAULT_VL_MODEL = "qwen-vl-plus"

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")

_DB_LOCK = threading.Lock()

# ---------------------------
# UI 美化（CSS）
# ---------------------------
def inject_css():
    st.markdown(
        """
<style>
.main .block-container {
    padding-top: 1.0rem;
    padding-bottom: 2rem;
    max-width: 100% !important;
    padding-left: 2rem;
    padding-right: 2rem;
}
h1, h2, h3 { letter-spacing: .2px; }
code { font-size: 0.9em; }

.topbar{
    padding: 18px 18px;
    border-radius: 18px;
    background: linear-gradient(90deg, #0ea5e9 0%, #6366f1 55%, #8b5cf6 100%);
    color: white;
    box-shadow: 0 8px 24px rgba(0,0,0,.12);
}
.topbar .title{ font-size: 30px; font-weight: 800; }
.topbar .sub{ opacity: .9; margin-top: 6px; font-size: 14px; }

.card{
    border: 1px solid rgba(0,0,0,.08);
    border-radius: 18px;
    padding: 16px 16px;
    background: rgba(255,255,255,.6);
    box-shadow: 0 6px 16px rgba(0,0,0,.06);
}
.badge{
    display:inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 12px; border: 1px solid rgba(0,0,0,.12); margin-right: 6px;
}
.badge.ok { background:#ecfdf5; color:#065f46; border-color:#a7f3d0; }
.badge.warn { background:#fffbeb; color:#92400e; border-color:#fde68a; }
.badge.bad { background:#fef2f2; color:#991b1b; border-color:#fecaca; }

.depbar{ display:flex; gap:8px; flex-wrap: wrap; padding: 10px 0; }
.depitem{
    padding: 8px 10px; border-radius: 14px; border: 1px solid rgba(0,0,0,.10);
    background: rgba(255,255,255,.7); font-size: 13px;
}
.depitem b{ margin-right:6px; }

.docbox{
    border: 1px solid rgba(0,0,0,.10);
    border-radius: 18px;
    padding: 14px 16px;
    background: rgba(255,255,255,.75);
    line-height: 1.55;
    white-space: normal;
}
section[data-testid="stSidebar"] .stMarkdown h2{ font-size: 18px; font-weight: 800; }
div[data-testid="stDataFrame"] { border-radius: 14px; overflow:hidden; }
</style>
""",
        unsafe_allow_html=True,
    )

inject_css()

# ---------------------------
# 数据库层
# ---------------------------
def db() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        conn.execute("PRAGMA journal_mode=DELETE;")
    return conn

def init_db():
    with _DB_LOCK:
        conn = db()
        conn.execute(
            """
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    meta_json TEXT DEFAULT '{}',
    created_at INTEGER NOT NULL
);
"""
        )
        conn.execute(
            """
CREATE TABLE IF NOT EXISTS artifacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    content_md TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
"""
        )
        conn.execute(
            """
CREATE TABLE IF NOT EXISTS versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    content_md TEXT NOT NULL,
    content_json TEXT NOT NULL,
    hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    note TEXT DEFAULT '',
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
"""
        )
        conn.execute(
            """
CREATE TABLE IF NOT EXISTS edges(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    child_artifact_id INTEGER NOT NULL,
    parent_artifact_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(child_artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
);
"""
        )
        conn.commit()
        conn.close()

def ensure_db_schema():
    init_db()

def now_ts() -> int:
    return int(time.time())

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def compute_hash(content_md: str, content_json: Dict[str, Any], parent_hashes: List[str]) -> str:
    payload = {"content_md": content_md, "content_json": content_json, "parents": parent_hashes}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))

# ---------------------------
# 数据库操作
# ---------------------------
def get_projects() -> List[Tuple[int, str]]:
    conn = db()
    rows = conn.execute("SELECT id, name FROM projects ORDER BY id DESC;").fetchall()
    conn.close()
    return rows

def get_project_meta(project_id: int) -> Dict[str, Any]:
    conn = db()
    row = conn.execute("SELECT meta_json FROM projects WHERE id=?", (project_id,)).fetchone()
    conn.close()
    if not row:
        return {}
    try:
        return json.loads(row[0] or "{}")
    except Exception:
        return {}

def create_project(name: str, meta: Dict[str, Any]) -> int:
    with _DB_LOCK:
        conn = db()
        ts = now_ts()
        cur = conn.execute(
            "INSERT INTO projects(name, meta_json, created_at) VALUES(?,?,?)",
            (name, json.dumps(meta, ensure_ascii=False), ts),
        )
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        return pid

def list_artifacts(project_id: int) -> List[Dict[str, Any]]:
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, type, title, hash, updated_at "
            "FROM artifacts WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        ensure_db_schema()
        conn = db()
        rows = conn.execute(
            "SELECT id, type, title, hash, updated_at "
            "FROM artifacts WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    conn.close()
    return [{"id": r[0], "type": r[1], "title": r[2], "hash": r[3], "updated_at": r[4]} for r in rows]

def get_artifact(project_id: int, a_type: str) -> Optional[Dict[str, Any]]:
    conn = db()
    row = conn.execute(
        "SELECT id, title, content_md, content_json, hash, created_at, updated_at "
        "FROM artifacts WHERE project_id=? AND type=? ORDER BY updated_at DESC LIMIT 1",
        (project_id, a_type),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "type": a_type,
        "title": row[1],
        "content_md": row[2],
        "content_json": json.loads(row[3] or "{}"),
        "hash": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }

def get_versions(artifact_id: int) -> List[Dict[str, Any]]:
    conn = db()
    rows = conn.execute(
        "SELECT version_no, hash, created_at, note FROM versions WHERE artifact_id=? ORDER BY version_no DESC",
        (artifact_id,),
    ).fetchall()
    conn.close()
    return [{"version_no": r[0], "hash": r[1], "created_at": r[2], "note": r[3]} for r in rows]

def set_edges(project_id: int, child_id: int, parent_ids: List[int]):
    with _DB_LOCK:
        conn = db()
        conn.execute("DELETE FROM edges WHERE project_id=? AND child_artifact_id=?", (project_id, child_id))
        ts = now_ts()
        for pid in parent_ids:
            conn.execute(
                "INSERT INTO edges(project_id, child_artifact_id, parent_artifact_id, created_at) VALUES(?,?,?,?)",
                (project_id, child_id, pid, ts),
            )
        conn.commit()
        conn.close()

def upsert_artifact(
    project_id: int,
    a_type: str,
    title: str,
    content_md: str,
    content_json: Dict[str, Any],
    parent_ids: List[int],
    note: str = "",
) -> Dict[str, Any]:
    existing = get_artifact(project_id, a_type)
    
    parent_hashes: List[str] = []
    for pid in parent_ids:
        conn = db()
        row = conn.execute("SELECT hash FROM artifacts WHERE id=? AND project_id=?", (pid, project_id)).fetchone()
        conn.close()
        if row:
            parent_hashes.append(row[0])
    
    new_hash = compute_hash(content_md, content_json, parent_hashes)
    ts = now_ts()
    
    with _DB_LOCK:
        conn = db()
        if existing:
            cur_ver = conn.execute("SELECT MAX(version_no) FROM versions WHERE artifact_id=?", (existing["id"],)).fetchone()
            next_ver = (cur_ver[0] or 0) + 1
            conn.execute(
                "INSERT INTO versions(artifact_id, version_no, content_md, content_json, hash, created_at, note) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    existing["id"],
                    next_ver,
                    existing["content_md"],
                    json.dumps(existing["content_json"], ensure_ascii=False),
                    existing["hash"],
                    ts,
                    note or "auto-save",
                ),
            )
            conn.execute(
                "UPDATE artifacts SET title=?, content_md=?, content_json=?, hash=?, updated_at=? "
                "WHERE id=? AND project_id=?",
                (title, content_md, json.dumps(content_json, ensure_ascii=False), new_hash, ts, existing["id"], project_id),
            )
            conn.commit()
        else:
            conn.execute(
                "INSERT INTO artifacts(project_id, type, title, content_md, content_json, hash, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (project_id, a_type, title, content_md, json.dumps(content_json, ensure_ascii=False), new_hash, ts, ts),
            )
            conn.commit()
        conn.close()
    
    a = get_artifact(project_id, a_type)
    if a:
        set_edges(project_id, a["id"], parent_ids)
    return a

# ---------------------------
# 文档链 & 依赖规则
# ---------------------------
DOC_TYPES = [
    ("overview", "首页总览"),
    ("training_plan", "培养方案（底座）"),
    ("syllabus", "课程教学大纲（依赖培养方案）"),
    ("calendar", "教学日历（依赖大纲）"),
    ("lesson_plan", "教案（依赖日历）"),
    ("assessment", "作业/题库/试卷方案（依赖大纲）"),
    ("review", "审核表（依赖试卷方案/大纲）"),
    ("report", "课程目标达成报告（依赖大纲/成绩）"),
    ("manual", "授课手册（依赖教案/过程证据）"),
    ("evidence", "课堂状态与过程证据（可选）"),
    ("vge", "证据链与可验证生成（VGE）"),
    ("dep_graph", "依赖图可视化（树/Graphviz）"),
    ("docx_export", "模板化DOCX导出（字段映射填充）"),
]

DEP_RULES = {
    "training_plan": [],
    "syllabus": ["training_plan"],
    "calendar": ["syllabus"],
    "lesson_plan": ["calendar"],
    "assessment": ["syllabus"],
    "review": ["assessment", "syllabus"],
    "report": ["syllabus"],
    "manual": ["lesson_plan"],
    "evidence": [],
    "vge": [],
    "overview": [],
    "dep_graph": [],
    "docx_export": [],
}

# ---------------------------
# PDF全量抽取核心功能（整合版）
# ---------------------------
def clean_text(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def normalize_multiline(text: str) -> str:
    """保留换行，做基础清理"""
    if text is None:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [clean_text(ln) for ln in text.split("\n")]
    out: List[str] = []
    blank = 0
    for ln in lines:
        if ln.strip() == "":
            blank += 1
            if blank <= 2:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()

def make_unique_columns(cols: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for c in cols:
        c0 = clean_text(c) or "col"
        if c0 not in seen:
            seen[c0] = 1
            out.append(c0)
        else:
            seen[c0] += 1
            out.append(f"{c0}_{seen[c0]}")
    return out

def postprocess_table_df(df: pd.DataFrame) -> pd.DataFrame:
    """表格后处理：去空白、去NaN、合并格向下填充"""
    if df is None or df.empty:
        return df
    
    df = df.copy()
    df = df.replace({None: ""}).fillna("")
    for c in df.columns:
        df[c] = df[c].astype(str).map(lambda x: clean_text(x))
    
    # 删除完全空行
    mask_all_empty = df.apply(lambda r: all((clean_text(x) == "" for x in r.values.tolist())), axis=1)
    df = df.loc[~mask_all_empty].reset_index(drop=True)
    
    # 向下填充（合并格常见列）
    fill_down_keywords = ["课程体系", "课程模块", "课程性质", "课程类别", "类别", "模块", "环节", "学期", "方向"]
    for c in df.columns:
        if any(k in str(c) for k in fill_down_keywords):
            last = ""
            new_col = []
            for v in df[c].tolist():
                if v != "":
                    last = v
                    new_col.append(v)
                else:
                    new_col.append(last)
            df[c] = new_col
    
    return df

def extract_pages_text_and_tables(pdf_bytes: bytes, enable_ocr: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    """
    提取每页的文本和表格
    返回：页面数据列表（含文本和表格），全文文本
    """
    if pdfplumber is None:
        return [], ""
    
    pages_data = []
    full_text_parts = []
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # 表格设置：偏"宽松"，提升跨页/复杂表格提取成功率
        table_settings = {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 5,
            "snap_tolerance": 3,
            "join_tolerance": 3,
            "edge_min_length": 3,
            "min_words_vertical": 1,
            "min_words_horizontal": 1,
            "text_tolerance": 2,
        }
        
        for idx, page in enumerate(pdf.pages, start=1):
            # 提取文本
            text = page.extract_text() or ""
            text = normalize_multiline(text)
            
            # 如果需要OCR且文本太少
            if enable_ocr and len(text) < 50:
                try:
                    import pytesseract
                    from PIL import Image
                    img = page.to_image(resolution=220).original
                    ocr_text = pytesseract.image_to_string(img, lang="chi_sim+eng")
                    if len(ocr_text) > len(text):
                        text = normalize_multiline(ocr_text)
                except Exception:
                    pass
            
            full_text_parts.append(text)
            
            # 提取表格
            raw_tables = []
            try:
                raw_tables = page.extract_tables(table_settings=table_settings) or []
            except Exception:
                raw_tables = []
            
            # 清洗表格
            cleaned_tables = []
            for t in raw_tables:
                if t and len(t) > 0:
                    # 清理表格数据
                    cleaned = []
                    for row in t:
                        cleaned_row = [clean_text(cell) if cell is not None else "" for cell in row]
                        # 跳过全空行
                        if not all(cell == "" for cell in cleaned_row):
                            cleaned.append(cleaned_row)
                    if cleaned:
                        cleaned_tables.append(cleaned)
            
            pages_data.append({
                "page": idx,
                "text": text,
                "tables": cleaned_tables,
                "tables_count": len(cleaned_tables)
            })
    
    full_text = "\n".join(full_text_parts)
    return pages_data, full_text

def split_sections(full_text: str) -> Dict[str, str]:
    """按 "一、/二、/三、..." 大章切分"""
    text = normalize_multiline(full_text)
    lines = text.splitlines()
    pat = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、\.．]\s*([^\n\r]+?)\s*$")
    
    sections: Dict[str, List[str]] = {}
    cur_key = "封面/前言"
    
    for ln in lines:
        m = pat.match(ln)
        if m:
            num = m.group(1)
            title = clean_text(m.group(2))
            cur_key = f"{num}、{title}"
            sections.setdefault(cur_key, [])
        else:
            sections.setdefault(cur_key, []).append(ln)
    
    return {k: "\n".join(v).strip() for k, v in sections.items()}

def extract_appendix_titles(full_text: str) -> Dict[str, str]:
    """抽取"附表X -> 标题" """
    titles: Dict[str, str] = {}
    text = normalize_multiline(full_text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        
        # 1) 附表1：XXXX
        m = re.search(r"(附表\s*\d+)\s*[:：]\s*(.+)$", line)
        if m:
            key = re.sub(r"\s+", "", m.group(1))
            val = clean_text(m.group(2))
            if val:
                titles[key] = val
            continue
        
        # 2) 七、XXXX（附表1）
        m = re.search(r"^(?P<title>.+?)\s*[（(]\s*(?P<key>附表\s*\d+)\s*[)）]\s*$", line)
        if m:
            key = re.sub(r"\s+", "", m.group("key"))
            val = clean_text(m.group("title"))
            if val:
                titles[key] = val
            continue
        
        # 3) 行内出现（附表X）
        m = re.search(r"(?P<title>.+?)\s*[（(]\s*(?P<key>附表\s*\d+)\s*[)）]", line)
        if m:
            key = re.sub(r"\s+", "", m.group("key"))
            val = clean_text(m.group("title"))
            if val and key not in titles:
                titles[key] = val
    
    return titles

def parse_training_objectives(section_text: str) -> Dict[str, Any]:
    """提取"培养目标"条目"""
    raw = normalize_multiline(section_text)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    items: List[str] = []
    
    pat = re.compile(r"^(?:（?\s*\d+\s*）?|\d+\s*[\.、．])\s*(.+)$")
    for ln in lines:
        m = pat.match(ln)
        if m:
            body = clean_text(m.group(1))
            if body:
                items.append(body)
    
    # 如果没抓到编号条目，退化：取前若干行
    if not items:
        items = lines[:30]
    
    return {"count": len(items), "items": items, "raw": raw}

def parse_graduation_requirements(text_any: str) -> Dict[str, Any]:
    """抽取12条毕业要求及其分项"""
    text = normalize_multiline(text_any or "")
    
    # 定位"二、毕业要求"
    start = re.search(r"(?m)^\s*(二\s*[、\.．]?\s*毕业要求|毕业要求)\s*$", text)
    if start:
        tail = text[start.start():]
    else:
        tail = text
    
    # 截断到下一大章
    end = re.search(r"(?m)^\s*[三四五六七八九十]\s*[、\.．]", tail)
    if end:
        tail = tail[:end.start()]
    
    lines = [ln.strip() for ln in tail.splitlines()]
    
    main_pat = re.compile(r"^(?P<no>\d{1,2})\s*[\.、](?!\d)\s*(?P<body>.+)$")
    sub_pat = re.compile(r"^(?P<no>\d{1,2}\.\d{1,2})\s+(?P<body>.+)$")
    
    items: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    cur_sub: Optional[Dict[str, Any]] = None
    
    def flush_sub():
        nonlocal cur_sub, cur
        if cur is not None and cur_sub is not None:
            cur.setdefault("subitems", []).append(cur_sub)
        cur_sub = None
    
    def flush_item():
        nonlocal cur
        if cur is not None:
            cur["title"] = clean_text(cur.get("title", ""))
            cur["body"] = clean_text(cur.get("body", ""))
            for s in cur.get("subitems", []):
                s["body"] = clean_text(s.get("body", ""))
            items.append(cur)
        cur = None
    
    for ln in lines:
        if not ln:
            continue
        
        m_main = main_pat.match(ln)
        m_sub = sub_pat.match(ln)
        
        if m_main:
            flush_sub()
            flush_item()
            no = int(m_main.group("no"))
            body_full = clean_text(m_main.group("body"))
            
            # 处理"工程知识：..."这种
            title = ""
            body = body_full
            if "：" in body_full:
                title, body = body_full.split("：", 1)
                title = clean_text(title)
                body = clean_text(body)
            
            cur = {"no": no, "title": title, "body": body, "subitems": []}
            continue
        
        if m_sub and cur is not None:
            flush_sub()
            cur_sub = {"no": m_sub.group("no"), "body": clean_text(m_sub.group("body"))}
            continue
        
        # 续行
        if cur_sub is not None:
            cur_sub["body"] += " " + ln
        elif cur is not None:
            cur["body"] += " " + ln
    
    flush_sub()
    flush_item()
    
    items = sorted(items, key=lambda x: x.get("no", 999))
    if len(items) > 12:
        items = [x for x in items if 1 <= x.get("no", 0) <= 12]
    
    return {"count": len(items), "items": items, "raw": tail.strip()}

def run_full_extract(pdf_bytes: bytes, use_ocr: bool = False) -> Dict[str, Any]:
    """
    运行全量抽取
    返回结构化的抽取结果
    """
    # 提取页面文本和表格
    pages_data, full_text = extract_pages_text_and_tables(pdf_bytes, enable_ocr=use_ocr)
    
    # 结构化解析
    sections = split_sections(full_text)
    appendix_titles = extract_appendix_titles(full_text)
    
    # 关键结构化：培养目标、毕业要求
    obj_key = next((k for k in sections.keys() if "培养目标" in k), "")
    obj = parse_training_objectives(sections.get(obj_key, "") or full_text)
    grad = parse_graduation_requirements(full_text)
    
    # 处理表格
    all_tables = []
    total_tables = 0
    
    for page_data in pages_data:
        page_no = page_data["page"]
        page_text = page_data["text"]
        page_tables = page_data["tables"]
        
        total_tables += len(page_tables)
        
        for i, table_data in enumerate(page_tables):
            if table_data and len(table_data) > 0:
                # 创建DataFrame
                if len(table_data) > 1:
                    # 尝试将第一行作为表头
                    header = table_data[0]
                    body = table_data[1:]
                    
                    # 判断表头是否可用
                    non_empty = sum(1 for x in header if clean_text(x) != "")
                    if non_empty >= max(1, len(header) // 2):
                        df = pd.DataFrame(body, columns=header)
                    else:
                        df = pd.DataFrame(table_data)
                else:
                    df = pd.DataFrame(table_data)
                
                # 后处理
                df = postprocess_table_df(df)
                
                # 添加到结果
                table_info = {
                    "page": page_no,
                    "title": f"第{page_no}页表格{i+1}",
                    "data": df.values.tolist(),
                    "columns": df.columns.tolist(),
                    "shape": df.shape
                }
                all_tables.append(table_info)
    
    # 构建结果
    result = {
        "page_count": len(pages_data),
        "table_count": total_tables,
        "ocr_used": use_ocr,
        "file_sha256": sha256_bytes(pdf_bytes),
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "pages_data": pages_data,
        "sections": sections,
        "appendix_titles": appendix_titles,
        "training_objectives": obj,
        "graduation_requirements": grad,
        "tables": all_tables,
        "full_text": full_text
    }
    
    return result

# ---------------------------
# 文件处理
# ---------------------------
def extract_text_from_upload(file) -> str:
    name = (file.name or "").lower()
    file.seek(0)
    
    if name.endswith(".pdf") and pdfplumber is not None:
        with pdfplumber.open(file) as pdf:
            texts = []
            for p in pdf.pages:
                t = p.extract_text() or ""
                if t.strip():
                    texts.append(t)
            return "\n".join(texts).strip()
    
    if name.endswith(".docx") and Document is not None:
        file.seek(0)
        doc = Document(file)
        paras = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paras).strip()
    
    file.seek(0)
    try:
        return file.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""

# ---------------------------
# 通用工具函数
# ---------------------------
def type_label(a_type: str) -> str:
    for t, name in DOC_TYPES:
        if t == a_type:
            return name
    return a_type

def dep_status(project_id: int, a_type: str) -> Tuple[bool, List[Tuple[str, bool]]]:
    req = DEP_RULES.get(a_type, [])
    detail = []
    ok = True
    for r in req:
        exists = get_artifact(project_id, r) is not None
        detail.append((r, exists))
        ok = ok and exists
    return ok, detail

def render_depbar(project_id: int, a_type: str):
    ok, detail = dep_status(project_id, a_type)
    chips = []
    for r, exists in detail:
        cls = "ok" if exists else "bad"
        chips.append(f'<span class="badge {cls}">{type_label(r)}</span>')
    st.markdown(
        f"""
<div class="depbar">
    <div class="depitem"><b>依赖检查</b>：{"✅齐全" if ok else "⚠️缺失上游"}</div>
    <div class="depitem">{''.join(chips) if chips else '<span class="badge ok">无上游依赖</span>'}</div>
</div>
""",
        unsafe_allow_html=True,
    )

def artifact_toolbar(a: Dict[str, Any]):
    import html as _html
    st.markdown(
        f"""
<div class="card">
    <div style="display:flex; justify-content:space-between; gap:12px; align-items:center;">
        <div>
            <div style="font-size:18px; font-weight:800;">{_html.escape(a['title'])}</div>
            <div style="opacity:.75; font-size:12px; margin-top:4px;">
                类型：{type_label(a['type'])} ｜ Hash：<code>{a['hash'][:12]}</code> ｜ 更新时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(a['updated_at']))}
            </div>
        </div>
        <div>
            <span class="badge ok">可编辑</span>
            <span class="badge warn">可版本化</span>
            <span class="badge warn">依赖可追溯</span>
        </div>
    </div>
</div>
""",
        unsafe_allow_html=True,
    )

def md_textarea(label: str, value: str, height: int = 420, key: str = "") -> str:
    return st.text_area(label, value=value, height=height, key=key)

# ---------------------------
# 模板函数
# ---------------------------
def template_training_plan(major: str, grade: str, course_group: str) -> str:
    return f"""# {grade}级《{major}》培养方案（示例）

## 一、培养目标
- 面向工程实践，具备扎实的数学/力学/材料基础
- 具备材料成型与制造过程的分析、设计与优化能力
- 具备工程伦理、团队协作与终身学习能力

## 二、毕业要求（示例）
1. 工程知识
2. 问题分析
3. 设计/开发解决方案
4. 研究
5. 现代工具使用
6. 工程与社会
7. 环境与可持续发展
8. 职业规范
9. 个人与团队
10. 沟通
11. 项目管理
12. 终身学习

## 三、课程体系：{course_group}
- 通识与基础
- 专业核心
- 专业方向
- 实践环节
"""

# ---------------------------
# 顶部与侧边栏
# ---------------------------
def topbar():
    st.markdown(
        """
<div class="topbar">
    <div class="title">教学智能体平台（PDF全量抽取版）</div>
    <div class="sub">培养方案PDF全量抽取 → 大纲 → 日历 → 教案 → 试卷/审核 → 达成报告 → 授课手册 ｜ 支持上传、修改、版本与依赖追溯</div>
</div>
""",
        unsafe_allow_html=True,
    )

# 初始化DB
ensure_db_schema()
topbar()

# 侧边栏配置
st.sidebar.markdown("## 运行模式")
run_mode = st.sidebar.radio("运行模式", ["演示模式（无API）", "在线模式（千问API）"], index=0)
st.sidebar.caption("演示模式不需要 Key；在线模式请在 Secrets 中配置 QWEN_API_KEY。")

st.sidebar.markdown("## 项目（专业/年级/课程体系）")
projects = get_projects()
p_names = ["（新建项目）"] + [f"{pid} · {name}" for pid, name in projects]
p_sel = st.sidebar.selectbox("选择项目", p_names, index=0)

if p_sel == "（新建项目）":
    with st.sidebar.expander("创建新项目", expanded=True):
        pname = st.text_input("项目名称", value="材料成型-教评一体化示例", key="new_pname")
        major = st.text_input("专业", value="材料成型及控制工程", key="new_major")
        grade = st.text_input("年级", value="22", key="new_grade")
        course_group = st.text_input("课程体系/方向", value="材料成型-数值模拟方向", key="new_group")
        if st.button("创建项目", type="primary"):
            pid = create_project(pname, {"major": major, "grade": grade, "course_group": course_group})
            st.success("已创建项目，请在下拉中选择它。")
            st.rerun()
    project_id = None
else:
    project_id = int(p_sel.split("·")[0].strip())

st.sidebar.markdown("## 功能模块")
module = st.sidebar.radio("导航", [name for _, name in DOC_TYPES], index=1)
type_by_name = {name: t for t, name in DOC_TYPES}
current_type = type_by_name[module]

# ---------------------------
# 页面路由
# ---------------------------
def ensure_project():
    if project_id is None:
        st.info("请先在左侧创建并选择一个项目。")
        st.stop()

def pick_parents_for(project_id: int, a_type: str) -> List[int]:
    req = DEP_RULES.get(a_type, [])
    parent_ids: List[int] = []
    for r in req:
        pa = get_artifact(project_id, r)
        if pa:
            parent_ids.append(pa["id"])
    if a_type == "manual":
        ev = get_artifact(project_id, "evidence")
        if ev:
            parent_ids.append(ev["id"])
    return parent_ids

def page_overview():
    ensure_project()
    st.markdown("### 首页总览")
    arts = list_artifacts(project_id)
    if not arts:
        st.info("当前项目还没有任何文档。建议先从‘培养方案（底座）’开始。")
        return
    
    st.markdown('<div class="card">📌 当前项目已有文档（最近更新在前）</div>', unsafe_allow_html=True)
    rows = []
    for a in arts:
        rows.append({
            "类型": type_label(a["type"]),
            "标题": a["title"],
            "Hash(前12)": a["hash"][:12],
            "更新时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["updated_at"])),
        })
    st.dataframe(rows, use_container_width=True)

def page_training_plan():
    ensure_project()
    a = get_artifact(project_id, "training_plan")
    render_depbar(project_id, "training_plan")
    
    st.markdown("### 培养方案（底座）")
    st.caption("推荐：上传培养方案PDF → 全量抽取 → 识别清单确认/修正 → 保存（结构化底座）。")
    
    tab1, tab2, tab3, tab4 = st.tabs(["生成/上传&识别确认", "预览", "编辑", "版本/导出"])
    
    with tab1:
        col1, col2 = st.columns([1, 1])
        
        with col1:
            st.markdown("#### 方式A：一键生成（演示/快速）")
            major = st.text_input("专业", value="材料成型及控制工程", key="tp_major")
            grade = st.text_input("年级", value="22", key="tp_grade")
            group = st.text_input("课程体系/方向", value="材料成型-数值模拟方向", key="tp_group")
            if st.button("生成培养方案并保存", type="primary"):
                md = template_training_plan(major, grade, group)
                a = upsert_artifact(
                    project_id,
                    "training_plan",
                    f"{grade}级《{major}》培养方案",
                    md,
                    {"major": major, "grade": grade, "course_group": group, "confirmed": True},
                    [],
                    note="generate",
                )
                st.success("已保存培养方案（可作为后续文件依赖底座）")
                st.rerun()
        
        with col2:
            st.markdown("#### 方式B：上传PDF全量抽取（推荐）")
            up = st.file_uploader("上传培养方案PDF文件", type=["pdf"], key="tp_upload")
            use_ocr = st.checkbox("启用OCR（针对扫描版PDF）", value=False)
            
            if up is not None and st.button("开始全量抽取", key="tp_start_extract"):
                pdf_bytes = up.read()
                with st.spinner("正在全量抽取PDF..."):
                    extract_result = run_full_extract(pdf_bytes, use_ocr=use_ocr)
                
                # 保存抽取结果到session
                st.session_state["tp_extract"] = {
                    "source": up.name,
                    "pdf_bytes": pdf_bytes,
                    "extract_result": extract_result,
                    "confirmed": False
                }
                st.success("PDF抽取完成！请在下方确认/修正抽取结果。")
        
        # 识别清单确认界面
        if "tp_extract" in st.session_state:
            ex = st.session_state["tp_extract"]
            extract_result = ex["extract_result"]
            
            st.markdown("---")
            st.markdown("### PDF全量抽取结果（请确认/修正）")
            
            # 基本信息
            colA, colB, colC = st.columns(3)
            with colA:
                major2 = st.text_input("专业（从PDF中识别）", 
                                      value=extract_result.get("major_guess", "") or "材料成型及控制工程", 
                                      key="tp_major_fix")
                grade2 = st.text_input("年级（从PDF中识别）", 
                                      value=extract_result.get("grade_guess", "") or "22", 
                                      key="tp_grade_fix")
            with colB:
                course_group2 = st.text_input("课程体系/方向", 
                                             value=extract_result.get("course_group_guess", "") or "材料成型方向", 
                                             key="tp_group_fix")
                confirmed_flag = st.checkbox("我已确认以上信息大体正确", value=False, key="tp_confirm_flag")
            with colC:
                st.metric("总页数", extract_result["page_count"])
                st.metric("表格总数", extract_result["table_count"])
            
            st.markdown("#### 1) 培养目标（可编辑）")
            goals = extract_result["training_objectives"].get("items", [])
            goals_text = st.text_area(
                "每行一个目标（可增删/改写）",
                value="\n".join(goals) if goals else "",
                height=140,
                key="tp_goals_edit",
            )
            goals_final = [x.strip() for x in goals_text.splitlines() if x.strip()]
            
            st.markdown("#### 2) 毕业要求（可编辑）")
            grad_items = extract_result["graduation_requirements"].get("items", [])
            if grad_items:
                # 创建可编辑的DataFrame
                grad_data = []
                for item in grad_items:
                    grad_data.append({
                        "编号": item.get("no", ""),
                        "标题": item.get("title", ""),
                        "内容": item.get("body", "")
                    })
                df_grad = pd.DataFrame(grad_data)
                df_grad_edited = st.data_editor(df_grad, use_container_width=True, num_rows="dynamic", key="tp_grad_editor")
                outcomes_final = []
                for _, row in df_grad_edited.iterrows():
                    if str(row["编号"]).strip():
                        outcomes_final.append({
                            "no": str(row["编号"]).strip(),
                            "name": str(row["标题"]).strip(),
                            "body": str(row["内容"]).strip()
                        })
            else:
                st.info("未识别到毕业要求，请手工录入")
                grad_json = st.text_area(
                    "毕业要求 JSON",
                    value=json.dumps([{"no": "1", "name": "工程知识", "body": ""}], ensure_ascii=False, indent=2),
                    height=160,
                    key="tp_grad_json",
                )
                try:
                    outcomes_final = json.loads(grad_json) if grad_json.strip() else []
                except Exception:
                    outcomes_final = []
            
            st.markdown("#### 3) 抽取的表格（可编辑确认）")
            tables = extract_result.get("tables", [])
            confirmed_tables = []
            
            if tables:
                for i, table_info in enumerate(tables[:5]):  # 只显示前5个表格
                    st.markdown(f"**表格{i+1}（第{table_info['page']}页）**")
                    df = pd.DataFrame(table_info["data"], columns=table_info["columns"])
                    df_edited = st.data_editor(df, use_container_width=True, height=200, key=f"tp_table_{i}")
                    
                    confirm_table = st.checkbox(f"确认采用此表格", value=True, key=f"tp_table_confirm_{i}")
                    if confirm_table:
                        confirmed_tables.append({
                            "page": table_info["page"],
                            "title": table_info["title"],
                            "data": df_edited.values.tolist(),
                            "columns": df_edited.columns.tolist()
                        })
            else:
                st.info("未抽取到表格")
            
            st.markdown("#### 4) 章节结构")
            sections = extract_result.get("sections", {})
            with st.expander("查看章节结构", expanded=False):
                for section_name, section_content in list(sections.items())[:10]:  # 显示前10个章节
                    st.markdown(f"**{section_name}**")
                    st.text(section_content[:500] + "..." if len(section_content) > 500 else section_content)
            
            st.markdown("---")
            if st.button("✅ 确认并保存为培养方案底座", type="primary", disabled=not confirmed_flag):
                # 构建content_json
                content_json = {
                    "source": ex["source"],
                    "confirmed": True,
                    "major": major2,
                    "grade": grade2,
                    "course_group": course_group2,
                    "goals": goals_final,
                    "outcomes": outcomes_final,
                    "tables": confirmed_tables,
                    "extract_metadata": {
                        "page_count": extract_result["page_count"],
                        "table_count": extract_result["table_count"],
                        "sections_count": len(sections),
                        "extracted_at": extract_result["extracted_at"]
                    }
                }
                
                # 生成markdown
                md = f"# 培养方案（PDF抽取-已确认）\n\n"
                md += f"- 专业：{major2}\n- 年级：{grade2}\n- 课程体系/方向：{course_group2}\n\n"
                md += "## 一、培养目标（确认版）\n" + ("\n".join([f"- {x}" for x in goals_final]) if goals_final else "- （未填）") + "\n\n"
                md += "## 二、毕业要求（确认版）\n" + ("\n".join([f"- {o.get('no','')}. {o.get('name','')}: {o.get('body','')}" for o in outcomes_final]) if outcomes_final else "- （未填）") + "\n\n"
                md += "## 三、抽取表格（共{}个）\n".format(len(confirmed_tables))
                for i, tbl in enumerate(confirmed_tables, 1):
                    md += f"- 表格{i}（第{tbl['page']}页）: {tbl['title']}\n"
                md += "\n## 四、章节结构\n"
                for section_name in list(sections.keys())[:5]:
                    md += f"- {section_name}\n"
                
                title = f"培养方案（PDF抽取确认版）-{ex['source']}"
                a2 = upsert_artifact(project_id, "training_plan", title, md, content_json, [], note="pdf-extract-confirm")
                st.success("已保存‘确认版培养方案底座’。后续生成大纲会优先使用结构化字段。")
                st.session_state.pop("tp_extract", None)
                st.rerun()
            
            if st.button("清除本次抽取结果（不保存）"):
                st.session_state.pop("tp_extract", None)
                st.info("已清除。")
    
    with tab2:
        if not a:
            st.info("暂无培养方案。请先生成或上传并确认。")
        else:
            artifact_toolbar(a)
            st.markdown("#### 结构化内容")
            st.json(a.get("content_json") or {})
            st.markdown("#### Markdown预览")
            st.markdown(a["content_md"][:2000] + "..." if len(a["content_md"]) > 2000 else a["content_md"])
    
    with tab3:
        if not a:
            st.info("暂无培养方案。请先生成或上传。")
        else:
            edited = md_textarea("在线编辑培养方案（支持直接修改）", a["content_md"], key="tp_edit")
            note = st.text_input("保存说明（可选）", value="edit", key="tp_note")
            if st.button("保存修改（生成新版本）", type="primary", key="tp_save"):
                a2 = upsert_artifact(project_id, "training_plan", a["title"], edited, a["content_json"], [], note=note)
                st.success("已保存。后续依赖文件将引用更新后的培养方案。")
                st.rerun()
    
    with tab4:
        if not a:
            st.info("暂无培养方案。")
        else:
            vers = get_versions(a["id"])
            st.markdown("#### 版本记录")
            st.dataframe(vers if vers else [], use_container_width=True)

# 其他页面函数（保持原有结构，但简化实现）
def page_syllabus():
    ensure_project()
    render_depbar(project_id, "syllabus")
    tp = get_artifact(project_id, "training_plan")
    a = get_artifact(project_id, "syllabus")
    
    st.markdown("### 课程教学大纲")
    tab1, tab2, tab3, tab4 = st.tabs(["生成", "预览", "编辑", "版本/导出"])
    
    with tab1:
        if not tp:
            st.warning("请先创建培养方案")
        else:
            course_name = st.text_input("课程名称", value="数值模拟在材料成型中的应用")
            if st.button("生成教学大纲"):
                md = f"# 《{course_name}》教学大纲\n\n基于培养方案生成的教学大纲..."
                a2 = upsert_artifact(project_id, "syllabus", f"《{course_name}》教学大纲", md, {}, [tp["id"]], note="generate")
                st.success("已生成教学大纲")
                st.rerun()
    
    with tab2:
        if a:
            artifact_toolbar(a)
            st.markdown(a["content_md"])
    
    with tab3:
        if a:
            edited = md_textarea("编辑教学大纲", a["content_md"])
            if st.button("保存"):
                parents = pick_parents_for(project_id, "syllabus")
                a2 = upsert_artifact(project_id, "syllabus", a["title"], edited, a["content_json"], parents, note="edit")
                st.success("已保存")
                st.rerun()

def page_calendar():
    ensure_project()
    render_depbar(project_id, "calendar")
    st.markdown("### 教学日历")
    st.info("功能开发中...")

def page_lesson_plan():
    ensure_project()
    render_depbar(project_id, "lesson_plan")
    st.markdown("### 教案")
    st.info("功能开发中...")

def page_assessment():
    ensure_project()
    render_depbar(project_id, "assessment")
    st.markdown("### 作业/题库/试卷方案")
    st.info("功能开发中...")

def page_review():
    ensure_project()
    render_depbar(project_id, "review")
    st.markdown("### 审核表")
    st.info("功能开发中...")

def page_report():
    ensure_project()
    render_depbar(project_id, "report")
    st.markdown("### 课程目标达成报告")
    st.info("功能开发中...")

def page_manual():
    ensure_project()
    render_depbar(project_id, "manual")
    st.markdown("### 授课手册")
    st.info("功能开发中...")

def page_evidence():
    ensure_project()
    render_depbar(project_id, "evidence")
    st.markdown("### 课堂状态与过程证据")
    st.info("功能开发中...")

def page_vge():
    ensure_project()
    st.markdown("### 证据链与可验证生成（VGE）")
    st.info("功能开发中...")

def page_dep_graph():
    ensure_project()
    st.markdown("### 依赖图可视化")
    st.info("功能开发中...")

def page_docx_export():
    ensure_project()
    st.markdown("### 模板化DOCX导出")
    st.info("功能开发中...")

# ---------------------------
# 路由配置
# ---------------------------
ROUTES = {
    "overview": page_overview,
    "training_plan": page_training_plan,
    "syllabus": page_syllabus,
    "calendar": page_calendar,
    "lesson_plan": page_lesson_plan,
    "assessment": page_assessment,
    "review": page_review,
    "report": page_report,
    "manual": page_manual,
    "evidence": page_evidence,
    "vge": page_vge,
    "dep_graph": page_dep_graph,
    "docx_export": page_docx_export,
}

# 执行当前页面
if project_id:
    fn = ROUTES.get(current_type, page_overview)
    fn()
else:
    st.info("请先在左侧创建或选择项目")