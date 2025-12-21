# -*- coding: utf-8 -*-
"""
教学智能体平台（单文件版 app.py）- 修改增强版
增强点：
1) 培养方案上传识别 -> 识别清单（可编辑）-> 用户确认/修正 -> 再保存（结构化+可追溯）
2) 表格以 data_editor 形式展示，便于确认/修正；图/导图用“边表+Graphviz”替代识别还原不佳
3) 侧边栏新增备份/还原（zip）
4) 修复 ensure_db_schema 与 db() WAL 设置不一致导致的潜在 OperationalError

说明：
- 不依赖 OCR；pdfplumber 能抽到表就用表，否则退化为“手工边表/手工矩阵”
- 在线模式可用千问做进一步“纠错/补全”（可选）
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
from typing import List, Optional, Dict, Any, Tuple
import pandas as pd
import streamlit as st
import requests
import numpy as np
from PIL import Image, ImageOps

# -------- 可选解析依赖（缺失也能跑） --------
try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    import mammoth
except Exception:
    mammoth = None

# docxtpl（模板化导出用，可选）
try:
    from docxtpl import DocxTemplate
except Exception:
    DocxTemplate = None

# pandas（用于表格编辑更舒服，可选）
try:
    import pandas as pd
except Exception:
    pd = None


# ---------------------------
# 基础配置（云端友好）
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
.main .block-container { padding-top: 1.0rem; padding-bottom: 2rem; max-width: 1600px; }
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
# 数据层：SQLite + 版本管理 + 依赖边
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
    # 与 db() 行为保持一致，不在这里强制 WAL（某些环境会炸）
    init_db()


def now_ts() -> int:
    return int(time.time())


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def compute_hash(content_md: str, content_json: Dict[str, Any], parent_hashes: List[str]) -> str:
    payload = {"content_md": content_md, "content_json": content_json, "parents": parent_hashes}
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


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

    out = []
    for r in rows:
        out.append({"id": r[0], "type": r[1], "title": r[2], "hash": r[3], "updated_at": r[4]})
    return out


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
# 文件抽取（上传）
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

    if name.endswith(".doc") and mammoth is not None:
        file.seek(0)
        res = mammoth.convert_to_text(file)
        return (res.value or "").strip()

    file.seek(0)
    try:
        return file.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def extract_pdf_tables(upload_file) -> List[Dict[str, Any]]:
    """
    尝试从 PDF 抽取表格，返回：
    [
      {"page": 12, "tables": [ [ [cell, ...], ... ], ... ]},
      ...
    ]
    """
    if pdfplumber is None:
        return []
    upload_file.seek(0)
    out: List[Dict[str, Any]] = []
    try:
        with pdfplumber.open(upload_file) as pdf:
            for i, p in enumerate(pdf.pages, start=1):
                try:
                    tabs = p.extract_tables() or []
                except Exception:
                    tabs = []
                good_tabs = []
                for t in tabs:
                    # 过滤掉太小/太碎的
                    if not t or len(t) < 2:
                        continue
                    max_cols = max([len(r) for r in t if r] + [0])
                    if max_cols >= 3 and len(t) >= 3:
                        good_tabs.append(t)
                if good_tabs:
                    out.append({"page": i, "tables": good_tabs})
    except Exception:
        return []
    return out


def table_to_dataframe(table_2d: List[List[Any]]) -> Optional["pd.DataFrame"]:
    if pd is None:
        return None
    if not table_2d:
        return pd.DataFrame()
    # 取第一行为表头（若空则给默认）
    header = table_2d[0]
    header = [(h or "").strip() if isinstance(h, str) else (str(h) if h is not None else "") for h in header]
    if all([h == "" for h in header]):
        header = [f"col{i+1}" for i in range(len(header))]
    rows = table_2d[1:]
    # 补齐列数
    ncol = len(header)
    norm_rows = []
    for r in rows:
        r = r or []
        rr = list(r)[:ncol] + [""] * max(0, ncol - len(r))
        norm_rows.append(rr)
    return pd.DataFrame(norm_rows, columns=header)


def guess_course_support_matrix(tables_pack: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    尝试在抽到的表格里猜“课程-毕业要求支撑矩阵”
    输出：
    {
      "found": bool,
      "hint": "...",
      "matrix": [ {"course": "...", "supports": {"1.1":"H", "2.3":"M", ...}}, ... ],
      "raw_tables": [...]
    }
    """
    res = {"found": False, "hint": "未自动识别到支撑矩阵（可手工录入/修正）", "matrix": [], "raw_tables": []}
    if not tables_pack:
        return res

    # 经验：支撑矩阵往往表头含“毕业要求/指标点/达成/支撑/H/M/L”等字样
    key_words = ["毕业要求", "指标", "支撑", "达成", "H", "M", "L"]
    candidates = []

    for pack in tables_pack:
        page = pack["page"]
        for t in pack["tables"]:
            flat = " ".join([" ".join([(c or "") for c in (r or [])]) for r in t if r])
            hit = sum([1 for k in key_words if k in flat])
            if hit >= 2:
                candidates.append({"page": page, "table": t, "hit": hit})

    if not candidates:
        res["raw_tables"] = tables_pack
        return res

    candidates.sort(key=lambda x: x["hit"], reverse=True)
    best = candidates[0]
    res["found"] = True
    res["hint"] = f"自动挑选了第 {best['page']} 页的疑似支撑矩阵（命中关键词数={best['hit']}）。请在下方确认/修正。"
    res["raw_tables"] = tables_pack

    # 先不做复杂智能解析：把 best table 原样交给用户编辑确认
    res["best_table"] = {"page": best["page"], "table": best["table"]}
    return res


def extract_training_plan_checklist(text: str) -> Dict[str, Any]:
    """
    从文本粗抽一些清单字段（可编辑，最终以用户确认为准）
    """
    # 简单正则：培养目标通常“培养目标/目标”附近；毕业要求通常“毕业要求”附近
    major = ""
    grade = ""
    # 从类似“2024版/2024级”等中猜年级
    m = re.search(r"(\d{2,4})级", text)
    if m:
        grade = m.group(1)

    # 专业名称猜测
    m2 = re.search(r"(材料成型及控制工程|机械工程|电气工程及其自动化|计算机科学与技术|土木工程|航空航天|测控技术与仪器)", text)
    if m2:
        major = m2.group(1)

    # 粗提“培养目标”段落
    goals = []
    m3 = re.search(r"(培养目标[\s\S]{0,2000}?)(毕业要求|三、|四、|五、|课程体系|$)", text)
    if m3:
        chunk = m3.group(1)
        for line in chunk.splitlines():
            line = line.strip()
            if re.match(r"^(\d+[\.\、]|[-•])", line):
                goals.append(re.sub(r"^(\d+[\.\、]|[-•])\s*", "", line))

    # 粗提“毕业要求”编号行
    outcomes = []
    # 找到“毕业要求”后 2000 字内的 “1.”、“2.” 等
    m4 = re.search(r"(毕业要求[\s\S]{0,2500})", text)
    if m4:
        chunk = m4.group(1)
        for line in chunk.splitlines():
            line = line.strip()
            mm = re.match(r"^(\d{1,2})[\.、]\s*(.+)$", line)
            if mm:
                outcomes.append({"no": mm.group(1), "name": mm.group(2).strip()})

    return {
        "major_guess": major,
        "grade_guess": grade,
        "goals_guess": goals[:8],
        "outcomes_guess": outcomes[:20],
    }


# ---------------------------
# 千问：文本生成（可选）
# ---------------------------
def get_qwen_key() -> str:
    return st.secrets.get("QWEN_API_KEY", os.environ.get("QWEN_API_KEY", "")).strip()


def qwen_chat(
    messages: List[Dict[str, Any]],
    model: str = DEFAULT_TEXT_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 1400,
) -> str:
    key = get_qwen_key()
    if not key:
        raise RuntimeError("未配置 QWEN_API_KEY（当前为演示模式可不填）")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    data = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    resp = requests.post(BASE_URL + "/chat/completions", headers=headers, json=data, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM接口错误：{resp.status_code} {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------
# 备份/还原（zip）
# ---------------------------
def safe_extract_zip(zip_bytes: bytes, target_dir: str):
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as z:
        for member in z.infolist():
            # 防路径穿越
            p = os.path.normpath(member.filename).replace("\\", "/")
            if p.startswith("../") or p.startswith("..\\") or p.startswith("/"):
                continue
            out_path = os.path.join(target_dir, p)
            # 创建目录
            if member.is_dir():
                os.makedirs(out_path, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with z.open(member, "r") as src, open(out_path, "wb") as dst:
                dst.write(src.read())


def make_backup_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(DATA_DIR):
            for root, _, files in os.walk(DATA_DIR):
                for fn in files:
                    ap = os.path.join(root, fn)
                    rel = os.path.relpath(ap, ".")
                    z.write(ap, rel)
    return buf.getvalue()


# ---------------------------
# 生成模板（无API也可）
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


def template_syllabus(
    course_name: str,
    hours_total: int,
    credits: float,
    extra_req: str,
    tp_text: str,
    support_points: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    outcomes = []
    for line in tp_text.splitlines():
        m = re.match(r"^\s*\d+\.\s*(.+)$", line.strip())
        if m:
            outcomes.append(m.group(1).strip())
    outcomes = outcomes[:8] or ["工程知识", "问题分析", "设计/开发解决方案", "现代工具使用"]

    obj = [
        {"id": "CO1", "desc": "理解课程核心概念与基本方法", "map_to": outcomes[0]},
        {"id": "CO2", "desc": "能基于案例进行建模/分析并解释结果", "map_to": outcomes[1]},
        {"id": "CO3", "desc": "能够使用软件工具完成课程实践任务", "map_to": outcomes[min(3, len(outcomes) - 1)]},
    ]
    sp = support_points or []
    sp_md = "、".join(sp) if sp else "（尚未从培养方案支撑矩阵中确认，可手工补充）"

    md = f"""# 《{course_name}》课程教学大纲（严格依赖培养方案）

## 1. 课程基本信息
- 学分：{credits}
- 总学时：{hours_total}
- 课程性质：专业课/方向课（示例）

## 2. 本课程支撑的毕业要求指标点（来自培养方案确认）
- {sp_md}

## 3. 课程目标（CO）与毕业要求映射
| 课程目标 | 描述 | 对应毕业要求 |
|---|---|---|
""" + "\n".join([f"| {x['id']} | {x['desc']} | {x['map_to']} |" for x in obj]) + f"""

## 4. 考核方式与比例（可调整）
- 平时：30%
- 作业/项目：20%
- 期末：50%

## 5. 教学内容与学时分配（示例）
- 第1章：导论（2学时）
- 第2章：方法与工具（6学时）
- 第3章：案例与实践（10学时）
- 第4章：综合项目与答辩（{max(2, hours_total-18)}学时）

## 6. 实践与要求
{extra_req or "结合工程案例，强调表达与规范文档产出。"}
"""
    js = {
        "course_name": course_name,
        "hours_total": hours_total,
        "credits": credits,
        "support_points": sp,
        "CO": obj
    }
    return md, js


def template_calendar(course_name: str, weeks: int, syllabus_json: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    co = syllabus_json.get("CO", [])
    rows = []
    for w in range(1, weeks + 1):
        rows.append(
            {
                "week": w,
                "topic": f"第{w}周：主题与案例（示例）",
                "activity": "讲授+讨论+练习",
                "homework": "小练习/阅读",
                "co": co[(w - 1) % len(co)]["id"] if co else "CO1",
            }
        )
    md = f"""# 《{course_name}》教学日历（依赖教学大纲）

| 周次 | 教学主题 | 教学活动 | 作业/任务 | 对应课程目标 |
|---:|---|---|---|---|
""" + "\n".join([f"| {r['week']} | {r['topic']} | {r['activity']} | {r['homework']} | {r['co']} |" for r in rows])
    return md, {"weeks": weeks, "rows": rows}


def template_lesson_plan(course_name: str, calendar_json: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    rows = calendar_json.get("rows", [])[:4]
    md = f"# 《{course_name}》教案（依赖教学日历）\n\n"
    plans = []
    for r in rows:
        md += f"""## {r['topic']}
- 教学目标：围绕 {r['co']} 达成
- 重点难点：核心概念+工程案例解释
- 教学过程：导入 → 讲解 → 讨论 → 练习 → 小结
- 作业：{r['homework']}

"""
        plans.append({"week": r["week"], "co": r["co"], "topic": r["topic"]})
    return md.strip(), {"plans": plans}


def template_assessment(course_name: str, syllabus_json: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    co = syllabus_json.get("CO", [])
    bank = []
    for i, x in enumerate(co, start=1):
        bank.append(
            {
                "qid": f"Q{i}",
                "type": "简答/计算/案例",
                "target_co": x["id"],
                "stem": f"围绕 {x['id']}：说明关键概念，并给出一个工程示例。",
                "rubric": "概念正确(40)+推理清晰(40)+表达规范(20)",
            }
        )
    md = f"""# 《{course_name}》作业/题库/试卷方案（依赖教学大纲）

## 题库（示例）
""" + "\n".join(
        [
            f"- **{q['qid']}**（{q['type']}，对应{q['target_co']}）：{q['stem']}\n  - 评分细则：{q['rubric']}"
            for q in bank
        ]
    )
    return md, {"bank": bank}


def template_review_forms(
    course_name: str, assessment_json: Dict[str, Any], syllabus_json: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    bank = assessment_json.get("bank", [])
    co = [x.get("id") for x in syllabus_json.get("CO", [])]
    cover = {c: 0 for c in co}
    for q in bank:
        if q.get("target_co") in cover:
            cover[q["target_co"]] += 1

    md = f"""# 《{course_name}》审核表集合（依赖试卷方案/教学大纲）

## A. 试题审核表（示例）
| 题号 | 题型 | 对应CO | 覆盖说明 | 结论 |
|---|---|---|---|---|
""" + "\n".join(
        [f"| {q['qid']} | {q['type']} | {q['target_co']} | 覆盖{q['target_co']}关键能力 | 通过 |" for q in bank]
    ) + f"""

## B. 课程目标达成评价依据合理性审核（示例）
| 课程目标 | 评价证据 | 证据充分性 | 备注 |
|---|---|---|---|
""" + "\n".join([f"| {c} | 题库/作业/项目/期末 | 较充分 | 可持续优化 |" for c in co]) + f"""

## C. 覆盖检查
""" + "\n".join([f"- {k}：{v} 题" for k, v in cover.items()])
    return md, {"coverage": cover}


def template_report(course_name: str, syllabus_json: Dict[str, Any], note: str = "") -> Tuple[str, Dict[str, Any]]:
    co = [x["id"] for x in syllabus_json.get("CO", [])] or ["CO1", "CO2", "CO3"]
    achieve = {c: round(0.72 - i * 0.05, 2) for i, c in enumerate(co)}
    md = f"""# 《{course_name}》课程目标达成情况评价报告（依赖教学大纲）

## 1. 评价方法
- 依据：作业、项目、期末试题与CO映射
- 指标：达成度（0~1）

## 2. 达成度结果（示例）
| 课程目标 | 达成度 | 结论 |
|---|---:|---|
""" + "\n".join([f"| {c} | {achieve[c]} | {'达成' if achieve[c] >= 0.6 else '需改进'} |" for c in co]) + f"""

## 3. 问题分析与改进措施
- 对达成度较低的目标，建议增加针对性案例与形成性评价。
- 改进闭环：下轮教学日历与作业题库将依据本报告自动调整。

## 4. 备注
{note or "（演示版：可上传成绩表后生成真实达成度）"}
"""
    return md, {"achieve": achieve}


def template_manual(course_name: str, lesson_json: Dict[str, Any], evidence_md: str = "") -> Tuple[str, Dict[str, Any]]:
    plans = lesson_json.get("plans", [])
    md = f"""# 《{course_name}》授课手册（依赖教案/过程证据）

## 1. 授课过程记录（示例）
""" + "\n".join([f"- 第{p['week']}周：{p['topic']}（对应{p['co']}）" for p in plans]) + f"""

## 2. 过程证据摘要（可选）
{evidence_md or "（尚未添加课堂状态证据，可在“课堂状态与过程证据”模块上传）"}

## 3. 反思与改进
- 本周学生反馈：……
- 需要强化的知识点：……
- 下周调整：……
"""
    return md, {"weeks": len(plans)}


# ---------------------------
# 课堂证据（可选）
# ---------------------------
def img_to_dataurl(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


@st.cache_data(ttl=600, show_spinner=False)
def qwen_vl_classroom_summary(image_dataurl: str, context: str) -> str:
    key = get_qwen_key()
    if not key:
        return "（演示模式：未配置QWEN_API_KEY，课堂证据摘要暂用占位文本）\n- Stu1：专注（坐姿稳定）\n- Stu2：需要关注（目光游离）"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    prompt = f"""
你是课堂过程证据记录助手。请仅根据课堂照片给出“班级状态摘要”。
要求：
1) 不进行身份识别，不推断姓名，仅用 Stu1/Stu2... 编号；
2) 每个编号给出：专注/需要关注/状态不佳 三选一；
3) 给出不超过15字依据；
4) 输出为Markdown列表；
课堂内容：{context}
"""
    data = {
        "model": DEFAULT_VL_MODEL,
        "messages": [
            {"role": "system", "content": "你是严谨的课堂过程证据记录助手。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_dataurl}},
                ],
            },
        ],
        "temperature": 0.2,
        "max_tokens": 450,
    }
    resp = requests.post(BASE_URL + "/chat/completions", headers=headers, json=data, timeout=60)
    if resp.status_code != 200:
        return f"（课堂证据接口调用失败：{resp.status_code}）"
    return resp.json()["choices"][0]["message"]["content"].strip()


# ---------------------------
# 通用组件：依赖条 + 预览 + 编辑
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


import html as _html


def render_doc_preview(md: str):
    safe = _html.escape(md).replace("\n", "<br>")
    st.markdown(f'<div class="docbox">{safe}</div>', unsafe_allow_html=True)

# ====== 追加/确保有这个 import（放在顶部 import 区）======
try:
    import pandas as pd
except Exception:
    pd = None


# ====== 表格工具：列名清洗 & 通用表格转DF ======
def _make_unique_columns(cols) -> List[str]:
    """
    Streamlit st.data_editor 要求：列名必须是唯一、非空、可序列化的字符串。
    这里把任意 cols 变成满足要求的列名。
    """
    seen = {}
    out = []
    for i, c in enumerate(list(cols)):
        # 1) 统一转字符串
        if c is None:
            name = ""
        else:
            name = str(c).strip()

        # 2) 空列名兜底
        if not name:
            name = f"col_{i+1}"

        # 3) 去掉一些容易引起混乱的字符（可选，但建议）
        name = name.replace("\n", " ").replace("\r", " ").strip()

        # 4) 去重：同名加后缀 _2 _3 ...
        if name in seen:
            seen[name] += 1
            name2 = f"{name}_{seen[name]}"
            out.append(name2)
        else:
            seen[name] = 1
            out.append(name)

    return out


def table_to_df(table: Any) -> "pd.DataFrame":
    """
    支持几种常见识别输出：
    - list[list]：二维数组（可能包含表头行）
    - list[dict]：每行一个dict（key作为列）
    - dict: {"headers": [...], "rows": [[...], ...]}
    - 其它：尽量转成一列文本
    """
    if pd is None:
        raise RuntimeError("缺少 pandas，无法使用表格编辑功能。请在 requirements.txt 加入 pandas")

    # dict 结构
    if isinstance(table, dict):
        headers = table.get("headers")
        rows = table.get("rows")
        if isinstance(headers, list) and isinstance(rows, list):
            df = pd.DataFrame(rows, columns=headers)
        else:
            # 兜底：把 dict 展开成两列
            df = pd.DataFrame([{"key": k, "value": v} for k, v in table.items()])
        df.columns = _make_unique_columns(df.columns)
        return df

    # list[dict]
    if isinstance(table, list) and table and all(isinstance(x, dict) for x in table):
        df = pd.DataFrame(table)
        df.columns = _make_unique_columns(df.columns)
        return df

    # list[list] 或 list[tuple]
    if isinstance(table, list) and table and all(isinstance(x, (list, tuple)) for x in table):
        # 尝试把第一行当表头：如果第一行“更像字符串”，就当 header
        first = list(table[0])
        rest = table[1:]

        def _stringish_ratio(row):
            if not row:
                return 0.0
            s = 0
            for x in row:
                if isinstance(x, str):
                    s += 1
            return s / max(1, len(row))

        if _stringish_ratio(first) >= 0.5 and len(first) >= 2:
            headers = [str(x).strip() if x is not None else "" for x in first]
            headers = _make_unique_columns(headers)
            # 行长度对齐
            maxw = max(len(headers), max((len(r) for r in rest), default=0))
            headers = headers + [f"col_{i+1}" for i in range(len(headers), maxw)]
            headers = _make_unique_columns(headers)
            rows2 = []
            for r in rest:
                r = list(r)
                if len(r) < maxw:
                    r = r + [""] * (maxw - len(r))
                else:
                    r = r[:maxw]
                rows2.append(r)
            df = pd.DataFrame(rows2, columns=headers)
        else:
            # 不把第一行当表头，直接生成默认列名
            maxw = max(len(r) for r in table)
            cols = _make_unique_columns([f"col_{i+1}" for i in range(maxw)])
            rows2 = []
            for r in table:
                r = list(r)
                if len(r) < maxw:
                    r = r + [""] * (maxw - len(r))
                else:
                    r = r[:maxw]
                rows2.append(r)
            df = pd.DataFrame(rows2, columns=cols)

        df.columns = _make_unique_columns(df.columns)
        return df

    # 其它：兜底成单列
    df = pd.DataFrame({"text": [json.dumps(table, ensure_ascii=False)]})
    df.columns = _make_unique_columns(df.columns)
    return df


def df_to_markdown_preview(df: "pd.DataFrame", max_rows: int = 30) -> str:
    """用于让用户更好地核对表格：转成Markdown表格预览。"""
    if pd is None:
        return ""
    df2 = df.head(max_rows).copy()
    # 避免 Nan 影响观感
    df2 = df2.fillna("")
    try:
        return df2.to_markdown(index=False)
    except Exception:
        # 兼容某些环境没有 tabulate
        return df2.to_string(index=False)


def render_table_editor(table: Any, key: str, title: str = "识别到的表格") -> Tuple[Any, bool]:
    """
    返回 (edited_table, confirmed)
    - edited_table: 以 list[dict] 形式返回，便于存 JSON
    - confirmed: 用户是否点击确认采用
    """
    if pd is None:
        st.warning("当前环境缺少 pandas，无法编辑表格。")
        st.code(json.dumps(table, ensure_ascii=False, indent=2), language="json")
        return table, False

    df = table_to_df(table)

    st.markdown(f"#### {title}")

    view_mode = st.radio(
        "显示方式（便于核对）",
        ["表格编辑（推荐）", "Markdown预览（更适合确认）", "JSON（原始结构）"],
        horizontal=True,
        key=f"{key}_viewmode",
    )

    if view_mode == "JSON（原始结构）":
        st.code(json.dumps(table, ensure_ascii=False, indent=2), language="json")
        confirmed = st.checkbox("我确认该表格无误，采用此结果", key=f"{key}_confirm_json")
        return table, confirmed

    if view_mode == "Markdown预览（更适合确认）":
        md = df_to_markdown_preview(df, max_rows=50)
        st.markdown(md)
        confirmed = st.checkbox("我确认该表格无误，采用此结果", key=f"{key}_confirm_md")
        # 仍返回规范化后的数据
        return df.fillna("").to_dict(orient="records"), confirmed

    # 表格编辑
    # 再次强制列名合规（保险）
    df = df.copy()
    df.columns = _make_unique_columns(df.columns)

    edited_df = st.data_editor(
        df,
        use_container_width=True,
        num_rows="dynamic",
        key=key,
    )

    # 编辑后也再清洗一次列名（避免用户把表头改成空或重复）
    edited_df = edited_df.copy()
    edited_df.columns = _make_unique_columns(edited_df.columns)

    st.caption("提示：如果你想更快核对，请切到“Markdown预览”。")
    confirmed = st.checkbox("我确认该表格已修正完成，采用此结果", key=f"{key}_confirm_editor")

    # 统一返回 list[dict] 方便保存进 content_json
    return edited_df.fillna("").to_dict(orient="records"), confirmed


def render_recognition_checklist(items: List[Dict[str, Any]], key_prefix: str) -> List[Dict[str, Any]]:
    """
    items: [{'name': '表1 ...', 'type': 'table', 'payload': ...}, ...]
    返回：用户确认后的 items（payload可能被编辑过）
    """
    confirmed_items = []
    if not items:
        st.info("暂无可确认的识别结果。")
        return confirmed_items

    st.markdown("### 识别结果清单（请确认/修正）")
    st.caption("建议：先在这里把识别出的表格/图表逐个确认，确认后再写入文档或数据库。")

    for i, it in enumerate(items):
        it_key = f"{key_prefix}_{i}"
        with st.expander(f"{i+1}. {it.get('name','未命名')}（{it.get('type','unknown')}）", expanded=(i == 0)):
            t = it.get("type")
            payload = it.get("payload")

            if t == "table":
                edited_payload, ok = render_table_editor(payload, key=f"{it_key}_table", title=it.get("name", "表格"))
                if ok:
                    it2 = dict(it)
                    it2["payload"] = edited_payload
                    confirmed_items.append(it2)
                    st.success("已确认采用该表格。")
                else:
                    st.warning("未确认：该表格不会写入最终结果。")

            elif t == "chart":
                # 图表先用“结构化信息 + 用户确认”方式（更稳）
                st.markdown("#### 图表（结构化显示，便于核对）")
                st.code(json.dumps(payload, ensure_ascii=False, indent=2), language="json")
                ok = st.checkbox("我确认该图表信息无误，采用此结果", key=f"{it_key}_chart_ok")
                if ok:
                    confirmed_items.append(it)
                    st.success("已确认采用该图表。")
                else:
                    st.warning("未确认：该图表不会写入最终结果。")

            else:
                # 其它类型：直接文本确认
                st.markdown("#### 文本/其它识别内容")
                st.text_area("内容（可修正）", value=str(payload), height=180, key=f"{it_key}_text")
                ok = st.checkbox("确认采用", key=f"{it_key}_text_ok")
                if ok:
                    confirmed_items.append(it)

    return confirmed_items

def md_textarea(label: str, value: str, height: int = 420, key: str = "") -> str:
    return st.text_area(label, value=value, height=height, key=key)


def artifact_toolbar(a: Dict[str, Any]):
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


def export_docx_bytes_plaintext(md: str) -> bytes:
    try:
        from docx import Document as DocxDoc
    except Exception:
        return b""
    doc = DocxDoc()
    for line in md.splitlines():
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------
# 依赖图可视化（树 + Graphviz）
# ---------------------------
DOC_ORDER = [
    ("training_plan", "培养方案"),
    ("syllabus", "教学大纲"),
    ("calendar", "教学日历"),
    ("lesson_plan", "教案"),
    ("assessment", "作业/题库/试卷方案"),
    ("review", "审核表"),
    ("report", "达成评价报告"),
    ("manual", "授课手册"),
    ("evidence", "过程证据"),
    ("vge", "证据链/VGE"),
]


def build_edges_for_project(project_id: int) -> List[Tuple[str, str]]:
    conn = db()
    rows = conn.execute(
        "SELECT p.type, c.type "
        "FROM edges e "
        "JOIN artifacts c ON e.child_artifact_id=c.id "
        "JOIN artifacts p ON e.parent_artifact_id=p.id "
        "WHERE e.project_id=?",
        (project_id,),
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def render_dep_tree_from_db(project_id: int):
    st.subheader("依赖关系（树状）")
    docs_present = {t for (t, _) in DOC_ORDER if get_artifact(project_id, t) is not None}

    for k, name in DOC_ORDER:
        if k in docs_present:
            a = get_artifact(project_id, k)
            vcount = len(get_versions(a["id"])) if a else 0
            deps = DEP_RULES.get(k, [])
            dep_txt = "、".join(deps) if deps else "无"
            st.markdown(f"- ✅ **{name}**  ｜版本：{vcount} ｜依赖：{dep_txt}")
        else:
            st.markdown(f"- ⬜ {name}（未生成/未上传）")


def build_dot_from_db(project_id: int) -> str:
    labels = {k: name for k, name in DOC_ORDER}
    nodes = set()
    edges = build_edges_for_project(project_id)

    for p, c in edges:
        nodes.add(p)
        nodes.add(c)

    for k, _ in DOC_ORDER:
        if get_artifact(project_id, k) is not None:
            nodes.add(k)

    lines = [
        "digraph G {",
        'rankdir="LR";',
        'node [shape=box, style="rounded,filled", fillcolor="#ffffff"];',
        'edge [color="#64748b"];',
    ]

    for n in nodes:
        a = get_artifact(project_id, n)
        lab = labels.get(n, n)
        if a:
            lines.append(f'"{n}" [label="{lab}\\n{a["hash"][:8]}", fillcolor="#E8F5E9"];')
        else:
            lines.append(f'"{n}" [label="{lab}\\n(缺失)", fillcolor="#FFEBEE", style="rounded,dashed,filled"];')

    for p, c in edges:
        lines.append(f'"{p}" -> "{c}";')

    if not edges:
        for child, reqs in DEP_RULES.items():
            for parent in reqs:
                lines.append(f'"{parent}" -> "{child}" [style=dashed];')

    lines.append("}")
    return "\n".join(lines)


def page_dep_graph():
    ensure_project()
    st.markdown("### 依赖图可视化（树状图 / Graphviz）")
    st.caption("用于展示“培养方案→大纲→日历→教案→试卷/审核→达成→手册→证据链”的依赖关系与可追溯性。")

    c1, c2 = st.columns([1, 1])
    with c1:
        render_dep_tree_from_db(project_id)
    with c2:
        st.subheader("依赖关系（Graphviz）")
        dot = build_dot_from_db(project_id)
        st.graphviz_chart(dot)

    st.markdown("---")
    st.subheader("提示")
    st.markdown(
        "- 只有在生成/保存依赖型文档时，系统才会记录真实依赖边（edges）。\n"
        "- 若你上传了某个文档作为底座（如培养方案/大纲），后续生成的文档会自动指向它。\n"
        "- 申报展示时，可以把这张图作为“教评一体化、可验证生成、证据链”的核心亮点之一。"
    )


# ---------------------------
# 模板化 DOCX 导出（docxtpl）
# ---------------------------
def docx_render_from_template(template_bytes: bytes, context: Dict[str, Any]) -> bytes:
    if DocxTemplate is None:
        raise RuntimeError("当前环境未安装 docxtpl。请在 requirements.txt 添加：docxtpl jinja2 lxml")
    tpl = DocxTemplate(io.BytesIO(template_bytes))
    tpl.render(context)
    out = io.BytesIO()
    tpl.save(out)
    return out.getvalue()


def flatten_syllabus_to_context(project_meta: Dict[str, Any], syllabus: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ctx = {
        "major": project_meta.get("major", ""),
        "grade": project_meta.get("grade", ""),
        "course_group": project_meta.get("course_group", ""),
        "course_name": "",
        "credits": "",
        "hours_total": "",
        "course_objectives": "",
        "co_table": [],
        "assessment_ratio": "平时30%+作业/项目20%+期末50%",
    }
    if syllabus:
        js = syllabus.get("content_json") or {}
        ctx["course_name"] = js.get("course_name", "")
        ctx["credits"] = js.get("credits", "")
        ctx["hours_total"] = js.get("hours_total", "")
        co = js.get("CO", []) or []
        ctx["co_table"] = co
        ctx["course_objectives"] = "\n".join([f"{x.get('id','')}：{x.get('desc','')}" for x in co]).strip()
    return ctx


def flatten_calendar_to_context(calendar: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ctx = {"calendar_rows": []}
    if calendar:
        js = calendar.get("content_json") or {}
        ctx["calendar_rows"] = js.get("rows", []) or []
    return ctx


def page_docx_export():
    ensure_project()
    st.markdown("### 模板化 DOCX 导出（字段映射填充）")
    st.caption("把学校正式模板（docx）改成 {{字段}} 占位符，即可导出“像学校文件”的版本。")

    if DocxTemplate is None:
        st.warning("当前环境缺少 docxtpl。要启用模板化导出，请在 requirements.txt 添加：docxtpl jinja2 lxml")
        st.info("你仍可使用各模块里的“简版DOCX导出”。")
        return

    meta = get_project_meta(project_id)
    sy = get_artifact(project_id, "syllabus")
    cal = get_artifact(project_id, "calendar")
    tp = get_artifact(project_id, "training_plan")
    rv = get_artifact(project_id, "review")
    rp = get_artifact(project_id, "report")
    mn = get_artifact(project_id, "manual")

    doc_kind = st.selectbox(
        "选择要导出的正式文件类型",
        [
            "教学大纲（模板）",
            "教学日历（模板）",
            "试题审核表（模板）",
            "评价依据合理性审核表（模板）",
            "课程目标达成评价报告（模板）",
            "授课手册（模板）",
            "培养方案（模板）",
        ],
    )

    tpl = st.file_uploader("上传对应 DOCX 模板（必须是 .docx）", type=["docx"])
    if not tpl:
        st.info("请先上传模板 docx（模板内用 {{字段}} 标注要填充的位置）。")
        with st.expander("模板字段示例（复制到 Word 模板里）", expanded=False):
            st.code(
                """常用字段（你可按需取用）：
{{ major }}  {{ grade }}  {{ course_group }}
{{ course_name }} {{ credits }} {{ hours_total }}
{{ course_objectives }}   （多行文本）
{{ assessment_ratio }}

循环表格（docxtpl）示例：
- CO表循环：{% for x in co_table %} ... {{ x.id }} ... {{ x.desc }} ... {% endfor %}
- 日历循环：{% for r in calendar_rows %} ... {{ r.week }} ... {{ r.topic }} ... {% endfor %}
""",
                language="text",
            )
        return

    base_ctx = flatten_syllabus_to_context(meta, sy)
    base_ctx.update(flatten_calendar_to_context(cal))
    base_ctx.update(
        {
            "training_plan_text": (tp["content_md"] if tp else ""),
            "review_text": (rv["content_md"] if rv else ""),
            "report_text": (rp["content_md"] if rp else ""),
            "manual_text": (mn["content_md"] if mn else ""),
            "export_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
    )

    st.subheader("字段映射（可修改）")
    c1, c2, c3 = st.columns(3)
    with c1:
        major = st.text_input("major", value=str(base_ctx.get("major", "")))
        grade = st.text_input("grade", value=str(base_ctx.get("grade", "")))
        course_group = st.text_input("course_group", value=str(base_ctx.get("course_group", "")))
    with c2:
        course_name = st.text_input("course_name", value=str(base_ctx.get("course_name", "")))
        credits = st.text_input("credits", value=str(base_ctx.get("credits", "")))
        hours_total = st.text_input("hours_total", value=str(base_ctx.get("hours_total", "")))
    with c3:
        assessment_ratio = st.text_input("assessment_ratio", value=str(base_ctx.get("assessment_ratio", "")))
        export_time = st.text_input("export_time", value=str(base_ctx.get("export_time", "")))

    course_objectives = st.text_area(
        "course_objectives（多行文本）",
        value=str(base_ctx.get("course_objectives", "")),
        height=120,
    )

    with st.expander("高级字段：CO表 / 日历表（JSON，可用于模板循环）", expanded=False):
        co_json_str = st.text_area(
            "co_table（JSON 数组）",
            value=json.dumps(base_ctx.get("co_table", []), ensure_ascii=False, indent=2),
            height=180,
        )
        cal_json_str = st.text_area(
            "calendar_rows（JSON 数组）",
            value=json.dumps(base_ctx.get("calendar_rows", []), ensure_ascii=False, indent=2),
            height=180,
        )

    ctx = dict(base_ctx)
    ctx.update(
        {
            "major": major,
            "grade": grade,
            "course_group": course_group,
            "course_name": course_name,
            "credits": credits,
            "hours_total": hours_total,
            "assessment_ratio": assessment_ratio,
            "course_objectives": course_objectives,
            "export_time": export_time,
        }
    )

    try:
        ctx["co_table"] = json.loads(co_json_str) if co_json_str.strip() else []
    except Exception:
        st.warning("co_table JSON 解析失败，已回退为空。")
        ctx["co_table"] = []

    try:
        ctx["calendar_rows"] = json.loads(cal_json_str) if cal_json_str.strip() else []
    except Exception:
        st.warning("calendar_rows JSON 解析失败，已回退为空。")
        ctx["calendar_rows"] = []

    if st.button("生成 DOCX（模板填充）", type="primary"):
        try:
            out_bytes = docx_render_from_template(tpl.read(), ctx)
            fname = f"{doc_kind}-{course_name or '课程'}.docx"
            st.success("已生成。")
            st.download_button(
                "下载 DOCX",
                data=out_bytes,
                file_name=fname,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        except Exception as e:
            st.error(f"模板渲染失败：{e}")


# ---------------------------
# 顶部与侧边栏：项目 + 模式 + 模块导航
# ---------------------------
def topbar():
    st.markdown(
        """
<div class="topbar">
  <div class="title">教学智能体平台</div>
  <div class="sub">培养方案 → 大纲 → 日历 → 教案 → 试卷/审核 → 达成报告 → 授课手册 ｜ 支持上传、修改、版本与依赖追溯（VGE）</div>
</div>
""",
        unsafe_allow_html=True,
    )


# 初始化 DB
ensure_db_schema()

topbar()
st.write("")

st.sidebar.markdown("## 运行模式")
run_mode = st.sidebar.radio("运行模式", ["演示模式（无API）", "在线模式（千问API）"], index=0)
st.sidebar.caption("演示模式不需要 Key；在线模式请在 Secrets 中配置 QWEN_API_KEY。")

st.sidebar.markdown("## 数据库维护")
cA, cB = st.sidebar.columns(2)
with cA:
    if st.button("备份(zip)"):
        b = make_backup_zip_bytes()
        st.sidebar.download_button("下载备份", data=b, file_name="teaching_agent_backup.zip")
with cB:
    restore = st.sidebar.file_uploader("还原(zip)", type=["zip"], label_visibility="collapsed")
    if restore is not None:
        try:
            safe_extract_zip(restore.read(), ".")
            st.sidebar.success("已还原（建议刷新/重启应用）")
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"还原失败：{e}")

if st.sidebar.button("⚠️ 重置数据库（删除 app.db）"):
    try:
        for p in [DB_PATH, DB_PATH + "-shm", DB_PATH + "-wal"]:
            if os.path.exists(p):
                os.remove(p)
        st.sidebar.success("已删除数据库文件，将自动重建。")
        st.rerun()
    except Exception as e:
        st.sidebar.error(f"重置失败：{e}")


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
# 主区域：模块页面
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
        st.info("当前项目还没有任何文档。建议先从“培养方案（底座）”开始。")
        return

    st.markdown('<div class="card">📌 当前项目已有文档（最近更新在前）</div>', unsafe_allow_html=True)
    rows = []
    for a in arts:
        rows.append(
            {
                "类型": type_label(a["type"]),
                "标题": a["title"],
                "Hash(前12)": a["hash"][:12],
                "更新时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["updated_at"])),
            }
        )
    st.dataframe(rows, use_container_width=True)

    st.markdown("---")
    st.markdown("### 快速入口")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown('<div class="card"><b>① 从底座开始</b><br>先识别/确认培养方案，再生成大纲。</div>', unsafe_allow_html=True)
    with c2:
        st.markdown('<div class="card"><b>② 看依赖链</b><br>到“依赖图可视化”查看可追溯关系。</div>', unsafe_allow_html=True)
    with c3:
        st.markdown('<div class="card"><b>③ 正式导出</b><br>到“模板化DOCX导出”用学校模板生成正式文件。</div>', unsafe_allow_html=True)


# ============ NEW：培养方案识别确认 UI ============

def dot_from_edge_rows(edge_rows: List[Dict[str, str]]) -> str:
    lines = [
        "digraph G {",
        'rankdir="LR";',
        'node [shape=box, style="rounded,filled", fillcolor="#ffffff"];',
        'edge [color="#64748b"];'
    ]
    for e in edge_rows:
        s = (e.get("source") or "").strip()
        t = (e.get("target") or "").strip()
        if s and t:
            lines.append(f'"{s}" -> "{t}";')
    lines.append("}")
    return "\n".join(lines)


def page_training_plan():
    ensure_project()
    a = get_artifact(project_id, "training_plan")
    render_depbar(project_id, "training_plan")

    st.markdown("### 培养方案（底座）")
    st.caption("推荐：上传培养方案 → 自动识别 → 识别清单确认/修正 → 保存（结构化底座）。")

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
            st.markdown("#### 方式B：上传已有培养方案（识别→确认→保存）")
            up = st.file_uploader("上传培养方案文件", type=["pdf", "doc", "docx", "txt"], key="tp_upload")

            use_ai_fix = st.checkbox("（可选）用千问对识别结果做纠错/补全", value=False, disabled=not run_mode.startswith("在线"))
            if up is not None and st.button("开始识别（生成清单）", key="tp_start_extract"):
                txt = extract_text_from_upload(up)
                tables_pack = extract_pdf_tables(up) if (up.name.lower().endswith(".pdf")) else []
                checklist = extract_training_plan_checklist(txt)
                matrix_guess = guess_course_support_matrix(tables_pack)

                st.session_state["tp_extract"] = {
                    "source": up.name,
                    "text": txt,
                    "tables_pack": tables_pack,
                    "checklist": checklist,
                    "matrix_guess": matrix_guess,
                    # 初始边表：空，靠用户补
                    "course_edges": [
                        {"source": "先修课程A", "target": "后续课程B"},
                    ],
                }
                st.success("已生成识别清单，请在下方确认/修正，然后再保存。")

            if "tp_extract" in st.session_state:
                ex = st.session_state["tp_extract"]
                st.markdown("---")
                st.markdown("### 识别清单（请确认/修正）")
                st.caption("原则：系统先尽力抽取，最终以你的确认结果为准；确认后的结构化信息会用于后续大纲自动填充。")

                ck = ex["checklist"]
                colA, colB, colC = st.columns(3)
                with colA:
                    major2 = st.text_input("专业（可修正）", value=ck.get("major_guess", ""), key="tp_major_fix")
                    grade2 = st.text_input("年级（可修正）", value=ck.get("grade_guess", ""), key="tp_grade_fix")
                with colB:
                    course_group2 = st.text_input("课程体系/方向（可补充）", value="", key="tp_group_fix")
                    confirmed_flag = st.checkbox("我已确认以上信息大体正确", value=False, key="tp_confirm_flag")
                with colC:
                    st.markdown("**识别来源**")
                    st.code(ex.get("source", ""), language="text")

                st.markdown("#### 1) 培养目标（可编辑）")
                goals_init = ck.get("goals_guess", []) or []
                goals_text = st.text_area(
                    "每行一个目标（可增删/改写）",
                    value="\n".join(goals_init) if goals_init else "",
                    height=140,
                    key="tp_goals_edit",
                )
                goals_final = [x.strip() for x in goals_text.splitlines() if x.strip()]

                st.markdown("#### 2) 毕业要求（可编辑）")
                out_init = ck.get("outcomes_guess", []) or []
                if pd is not None:
                    df_out = pd.DataFrame(out_init) if out_init else pd.DataFrame(columns=["no", "name"])
                    df_out2 = st.data_editor(df_out, use_container_width=True, num_rows="dynamic", key="tp_out_editor")
                    outcomes_final = [{"no": str(r["no"]), "name": str(r["name"])} for _, r in df_out2.iterrows() if str(r.get("no","")).strip()]
                else:
                    outcomes_json = st.text_area(
                        "毕业要求 JSON（数组）",
                        value=json.dumps(out_init, ensure_ascii=False, indent=2),
                        height=160,
                        key="tp_out_json",
                    )
                    try:
                        outcomes_final = json.loads(outcomes_json) if outcomes_json.strip() else []
                    except Exception:
                        outcomes_final = out_init

                st.markdown("#### 3) 课程-毕业要求支撑矩阵（表格→可编辑）")
                mg = ex["matrix_guess"]
                st.info(mg.get("hint", ""))
                best = mg.get("best_table", None)

                # 编辑“疑似矩阵表”
                edited_best_table = None
                if best and best.get("table"):
                    st.markdown(f"**疑似矩阵表（第 {best.get('page')} 页）**：请直接改表格内容（包括表头）")
                    #edited_best_table = render_table_editor(best["table"], key="tp_matrix_table_editor")
                    
                    edited_best_table, ok_table = render_table_editor(best["table"], key="tp_matrix_table_editor", title="毕业要求-课程目标矩阵（识别结果）")
                    if ok_table:
                        # 这里写入你的最终 content_json（示例）
                        st.session_state["tp_matrix_table_confirmed"] = edited_best_table
                        st.success("该表格已确认并缓存为最终版本。")
                    else:
                        st.info("请确认或修正表格后再采用。")

                    
                    
                    
                else:
                    st.warning("未抽到疑似支撑矩阵表格。你可以：1) 在 PDF 更清晰时再试；2) 下面手工录入本课程支撑点。")

                st.markdown("#### 4) 课程关系图（边表→可编辑 + Graphviz 预览）")
                st.caption("很多 PDF 导图是图片，不易自动还原。这里用可编辑“边表”更可靠：你只需填“先修→后续”。")

                edges = ex.get("course_edges", [{"source": "", "target": ""}])
                if pd is not None:
                    df_e = pd.DataFrame(edges)
                    df_e2 = st.data_editor(df_e, use_container_width=True, num_rows="dynamic", key="tp_edges_editor")
                    edges_final = [{"source": str(r["source"]), "target": str(r["target"])} for _, r in df_e2.iterrows()]
                else:
                    edges_json = st.text_area(
                        "边表 JSON（数组）",
                        value=json.dumps(edges, ensure_ascii=False, indent=2),
                        height=160,
                        key="tp_edges_json",
                    )
                    try:
                        edges_final = json.loads(edges_json) if edges_json.strip() else edges
                    except Exception:
                        edges_final = edges

                dot = dot_from_edge_rows(edges_final)
                st.graphviz_chart(dot)

                st.markdown("#### 5) 从支撑矩阵中提取“某门课程的支撑指标点”（用于大纲默认填充）")
                st.caption("如果你在上面的矩阵表里能看到课程行，可以在这里选择课程并勾选支撑点。")

                # 简单：让用户手工录入当前项目“默认课程”的支撑点（后面大纲页可选课程名再映射）
                support_points_text = st.text_input(
                    "当前要重点支持的课程指标点（逗号分隔，如 1.1,2.3,3.2）",
                    value="",
                    key="tp_support_points_text",
                )
                support_points = [x.strip() for x in re.split(r"[，,;\s]+", support_points_text) if x.strip()]

                st.markdown("---")
                if st.button("✅ 确认并保存为培养方案底座", type="primary", disabled=not confirmed_flag):
                    # 可选：用千问做一次“结构补全”（只对文本，不强依赖）
                    text_final = ex.get("text", "") or ""
                    if use_ai_fix and get_qwen_key():
                        try:
                            sys = "你是高校培养方案抽取与校正助手。输出必须是JSON+简短说明。"
                            user = f"""
请对以下培养方案文本做结构化抽取并校正，重点抽取：培养目标、毕业要求列表（含编号与名称）、任何出现的“课程-毕业要求支撑关系”提示。
返回JSON字段：goals(list[str]), outcomes(list[{{no,name}}]), notes(str)。
文本（截断）：{text_final[:8000]}
"""
                            out = qwen_chat([{"role": "system", "content": sys}, {"role": "user", "content": user}], temperature=0.2, max_tokens=1200)
                            m = re.search(r"\{[\s\S]*\}", out)
                            if m:
                                js_ai = json.loads(m.group(0))
                                # 仅在用户未填时补充
                                if not goals_final and js_ai.get("goals"):
                                    goals_final = js_ai.get("goals", [])
                                if (not outcomes_final) and js_ai.get("outcomes"):
                                    outcomes_final = js_ai.get("outcomes", [])
                        except Exception as e:
                            st.warning(f"AI校正失败（忽略，不影响保存）：{e}")

                    # 组装结构化 content_json（确认版）
                    content_json = {
                        "source": ex.get("source", ""),
                        "confirmed": True,
                        "major": major2,
                        "grade": grade2,
                        "course_group": course_group2,
                        "goals": goals_final,
                        "outcomes": outcomes_final,
                        "support_points_default": support_points,  # 默认/示例：可用于大纲
                        "support_matrix_best_table": {
                            "page": best.get("page") if best else None,
                            "table": edited_best_table if edited_best_table is not None else (best.get("table") if best else None),
                        },
                        "course_edges": edges_final,
                    }

                    # 生成一份更“可读”的 md（方便预览）
                    md = f"# 培养方案（上传识别-已确认）\n\n"
                    md += f"- 专业：{major2}\n- 年级：{grade2}\n- 课程体系/方向：{course_group2}\n\n"
                    md += "## 一、培养目标（确认版）\n" + ("\n".join([f"- {x}" for x in goals_final]) if goals_final else "- （未填）") + "\n\n"
                    md += "## 二、毕业要求（确认版）\n" + ("\n".join([f"- {o.get('no','')}. {o.get('name','')}" for o in outcomes_final]) if outcomes_final else "- （未填）") + "\n\n"
                    md += "## 三、课程支撑指标点（默认/示例）\n" + (("、".join(support_points)) if support_points else "（未填）") + "\n\n"
                    md += "## 四、原始抽取文本（供追溯）\n\n" + (ex.get("text","")[:20000] if ex.get("text") else "")

                    title = f"培养方案（确认版）-{ex.get('source','上传')}"
                    a2 = upsert_artifact(project_id, "training_plan", title, md, content_json, [], note="upload-confirm")
                    st.success("已保存“确认版培养方案底座”。后续生成大纲会优先使用结构化字段。")
                    st.session_state.pop("tp_extract", None)
                    st.rerun()

                if st.button("清除本次识别结果（不保存）"):
                    st.session_state.pop("tp_extract", None)
                    st.info("已清除。")

    with tab2:
        if not a:
            st.info("暂无培养方案。请先生成或上传并确认。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])
            st.markdown("#### 结构化（确认版）JSON")
            st.json(a.get("content_json") or {})

            # 额外展示：课程关系图
            cj = a.get("content_json") or {}
            edges = cj.get("course_edges", []) or []
            if edges:
                st.markdown("#### 课程关系图（Graphviz）")
                st.graphviz_chart(dot_from_edge_rows(edges))

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
            st.markdown("#### 导出（简版）")
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="培养方案.docx")
            else:
                st.warning("当前环境缺少 python-docx，无法导出 DOCX。")


def page_syllabus():
    ensure_project()
    render_depbar(project_id, "syllabus")
    tp = get_artifact(project_id, "training_plan")
    a = get_artifact(project_id, "syllabus")

    st.markdown("### 课程教学大纲：严格依赖培养方案（可验证）")
    st.caption("增强：若培养方案已确认结构化支撑点，将自动填充到大纲。并支持上传已有大纲做对齐检查（简版）。")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["填写/生成", "上传已有大纲&对齐检查", "预览", "编辑", "版本/导出"])

    with tab1:
        if not tp:
            st.warning("缺少上游依赖：培养方案。请先到“培养方案（底座）”模块上传并确认。")

        course_name = st.text_input("课程名称", value="数值模拟在材料成型中的应用", key="sy_course")
        credits = st.number_input("学分", min_value=0.5, max_value=10.0, value=2.0, step=0.5)
        hours_total = st.number_input("总学时", min_value=8, max_value=128, value=32, step=2)

        tp_json = (tp.get("content_json") if tp else {}) or {}
        default_support = tp_json.get("support_points_default", []) or []
        support_points_text = st.text_input(
            "本课程支撑的毕业要求指标点（可改，逗号分隔）",
            value=",".join(default_support) if default_support else "",
            key="sy_support_points",
        )
        support_points = [x.strip() for x in re.split(r"[，,;\s]+", support_points_text) if x.strip()]

        extra = st.text_area(
            "对大纲的补充要求（考核比例/教学方法/实践要求等）",
            value="课程目标3-5个；平时30%+大作业20%+期末50%；强调工程表达与案例；写明CO-毕业要求映射。",
            height=120,
        )

        use_ai = st.checkbox("使用千问生成更完整的大纲（需要QWEN_API_KEY）", value=run_mode.startswith("在线"))
        if st.button("生成并保存教学大纲（JSON+可读预览）", type="primary"):
            if not tp:
                st.error("请先提供培养方案。")
            else:
                tp_text = tp["content_md"]
                if use_ai and get_qwen_key():
                    sys = "你是高校教学文件撰写专家，输出必须规范、可落地。"
                    user = f"""请依据以下培养方案，为课程《{course_name}》撰写教学大纲。
要求：给出课程信息、课程目标CO(3-5)、CO-毕业要求映射、学时分配、教学方法、考核比例、实践要求。
并明确写出本课程支撑的毕业要求指标点：{support_points}
补充要求：{extra}
培养方案文本：
{tp_text[:5000]}
输出：先输出 JSON（字段：course_name, credits, hours_total, support_points, CO[{id,desc,map_to}], assessment, outline），然后输出一份Markdown大纲。
"""
                    try:
                        out = qwen_chat(
                            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
                            model=DEFAULT_TEXT_MODEL,
                            temperature=0.2,
                            max_tokens=1600,
                        )
                        m = re.search(r"\{[\s\S]*\}", out)
                        js = {}
                        if m:
                            try:
                                js = json.loads(m.group(0))
                            except Exception:
                                js = {}
                        md = out
                    except Exception as e:
                        st.warning(f"AI生成失败，已回退到模板生成：{e}")
                        md, js = template_syllabus(course_name, int(hours_total), float(credits), extra, tp_text, support_points)
                else:
                    md, js = template_syllabus(course_name, int(hours_total), float(credits), extra, tp_text, support_points)

                parents = [tp["id"]]
                a2 = upsert_artifact(project_id, "syllabus", f"《{course_name}》教学大纲", md, js, parents, note="generate")
                st.success("已保存教学大纲（后续日历/教案/试卷等将依赖它）")
                st.rerun()

    with tab2:
        if not tp:
            st.warning("请先有培养方案（确认版更好），否则对齐检查意义不大。")
        st.markdown("#### 上传已有教学大纲（PDF/DOC/DOCX/TXT）")
        up2 = st.file_uploader("上传已有课程教学大纲", type=["pdf", "doc", "docx", "txt"], key="sy_upload_existing")
        if up2 is not None and st.button("分析并给出对齐检查（简版）", key="sy_check_align"):
            sy_txt = extract_text_from_upload(up2)
            tp_json = (tp.get("content_json") if tp else {}) or {}
            tp_outcomes = tp_json.get("outcomes", []) or []
            tp_support_default = tp_json.get("support_points_default", []) or []

            # 简单对齐：是否包含默认支撑点字符串
            missing = []
            for p in tp_support_default:
                if p and (p not in sy_txt):
                    missing.append(p)

            st.markdown("### 对齐检查结果（简版）")
            st.write(f"- 大纲文件：{up2.name}")
            st.write(f"- 培养方案默认支撑点：{', '.join(tp_support_default) if tp_support_default else '（未设置）'}")
            if tp_support_default and missing:
                st.warning("大纲文本中未明显出现以下支撑指标点（可能缺失或表述不一致）：")
                st.write(", ".join(missing))
            else:
                st.success("大纲中已包含/或无需检查默认支撑点。")

            # 建议（演示版）：在线模式可让 LLM 给修订建议
            if run_mode.startswith("在线") and get_qwen_key():
                if st.button("用千问给出修订建议（可选）", key="sy_llm_suggest"):
                    sys = "你是教学大纲合规审校专家。输出简明的差异清单与建议改写段落。"
                    user = f"""
请基于培养方案毕业要求（可能含指标点）与该教学大纲文本，检查一致性与不足，并给出可执行的修订建议。
培养方案（结构化摘要）：{json.dumps(tp_json, ensure_ascii=False)[:6000]}
教学大纲文本（截断）：{sy_txt[:8000]}
输出：1) 差异清单（条目化） 2) 建议修订段落（可直接替换） 3) 一句话总体评价。
"""
                    try:
                        out = qwen_chat([{"role":"system","content":sys},{"role":"user","content":user}], temperature=0.2, max_tokens=1200)
                        st.markdown(out)
                    except Exception as e:
                        st.error(f"生成失败：{e}")

            with st.expander("查看大纲抽取文本（截断）", expanded=False):
                st.code(sy_txt[:12000], language="text")

    with tab3:
        if not a:
            st.info("暂无教学大纲。请在“填写/生成”中生成并保存。")
        else:
            artifact_toolbar(a)
            js = a["content_json"] or {}
            st.markdown('<div class="card"><b>结构化摘要</b></div>', unsafe_allow_html=True)
            c1, c2, c3 = st.columns(3)
            c1.metric("课程", js.get("course_name", "-"))
            c2.metric("学分", js.get("credits", "-"))
            c3.metric("总学时", js.get("hours_total", "-"))
            st.markdown("#### 支撑指标点")
            st.write("、".join(js.get("support_points", []) or []) or "（未填）")
            st.markdown("#### 大纲正文")
            render_doc_preview(a["content_md"])

    with tab4:
        if not a:
            st.info("暂无教学大纲。")
        else:
            edited = md_textarea("在线编辑教学大纲", a["content_md"], key="sy_edit")
            note = st.text_input("保存说明（可选）", value="edit", key="sy_note")
            if st.button("保存修改（生成新版本）", type="primary", key="sy_save"):
                parents = pick_parents_for(project_id, "syllabus")
                a2 = upsert_artifact(project_id, "syllabus", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab5:
        if not a:
            st.info("暂无教学大纲。")
        else:
            vers = get_versions(a["id"])
            st.markdown("#### 版本记录")
            st.dataframe(vers if vers else [], use_container_width=True)
            st.markdown("#### 导出（简版）")
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="教学大纲.docx")
            st.download_button(
                "下载 JSON（结构化）",
                data=json.dumps(a["content_json"], ensure_ascii=False, indent=2),
                file_name="教学大纲.json",
            )


def page_calendar():
    ensure_project()
    render_depbar(project_id, "calendar")
    sy = get_artifact(project_id, "syllabus")
    a = get_artifact(project_id, "calendar")

    st.markdown("### 教学日历：依据教学大纲自动生成（可编辑）")

    tab1, tab2, tab3, tab4 = st.tabs(["生成", "预览", "编辑", "版本/导出"])
    with tab1:
        if not sy:
            st.warning("缺少上游依赖：教学大纲。请先生成大纲。")
        weeks = st.number_input("周数", min_value=4, max_value=20, value=16, step=1)
        if st.button("生成并保存教学日历", type="primary"):
            if not sy:
                st.error("请先生成教学大纲。")
            else:
                md, js = template_calendar(sy["content_json"].get("course_name", "课程"), int(weeks), sy["content_json"])
                parents = [sy["id"]]
                a2 = upsert_artifact(
                    project_id,
                    "calendar",
                    f"《{sy['content_json'].get('course_name','课程')}》教学日历",
                    md,
                    js,
                    parents,
                    note="generate",
                )
                st.success("已保存教学日历。")
                st.rerun()
    with tab2:
        if not a:
            st.info("暂无教学日历。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])
    with tab3:
        if not a:
            st.info("暂无教学日历。")
        else:
            edited = md_textarea("在线编辑教学日历", a["content_md"], key="cal_edit")
            note = st.text_input("保存说明", value="edit", key="cal_note")
            if st.button("保存修改", type="primary", key="cal_save"):
                parents = pick_parents_for(project_id, "calendar")
                a2 = upsert_artifact(project_id, "calendar", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()
    with tab4:
        if not a:
            st.info("暂无教学日历。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="教学日历.docx")


def page_lesson_plan():
    ensure_project()
    render_depbar(project_id, "lesson_plan")
    cal = get_artifact(project_id, "calendar")
    a = get_artifact(project_id, "lesson_plan")

    st.markdown("### 教案：依据教学日历生成（可编辑）")
    tab1, tab2, tab3, tab4 = st.tabs(["生成", "预览", "编辑", "版本/导出"])

    with tab1:
        if not cal:
            st.warning("缺少上游依赖：教学日历。请先生成日历。")
        if st.button("生成并保存教案（示例：前4周）", type="primary"):
            if not cal:
                st.error("请先生成教学日历。")
            else:
                course_name = "课程"
                sy = get_artifact(project_id, "syllabus")
                if sy:
                    course_name = sy["content_json"].get("course_name", "课程")
                md, js = template_lesson_plan(course_name, cal["content_json"])
                parents = [cal["id"]]
                a2 = upsert_artifact(project_id, "lesson_plan", f"《{course_name}》教案", md, js, parents, note="generate")
                st.success("已保存教案。")
                st.rerun()

    with tab2:
        if not a:
            st.info("暂无教案。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])

    with tab3:
        if not a:
            st.info("暂无教案。")
        else:
            edited = md_textarea("在线编辑教案", a["content_md"], key="lp_edit")
            note = st.text_input("保存说明", value="edit", key="lp_note")
            if st.button("保存修改", type="primary", key="lp_save"):
                parents = pick_parents_for(project_id, "lesson_plan")
                a2 = upsert_artifact(project_id, "lesson_plan", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无教案。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="教案.docx")


def page_assessment():
    ensure_project()
    render_depbar(project_id, "assessment")
    sy = get_artifact(project_id, "syllabus")
    a = get_artifact(project_id, "assessment")

    st.markdown("### 作业/题库/试卷方案：依据教学大纲生成（可编辑）")
    tab1, tab2, tab3, tab4 = st.tabs(["生成", "预览", "编辑", "版本/导出"])

    with tab1:
        if not sy:
            st.warning("缺少上游依赖：教学大纲。请先生成大纲。")
        if st.button("生成并保存试卷方案", type="primary"):
            if not sy:
                st.error("请先生成教学大纲。")
            else:
                course_name = sy["content_json"].get("course_name", "课程")
                md, js = template_assessment(course_name, sy["content_json"])
                parents = [sy["id"]]
                a2 = upsert_artifact(project_id, "assessment", f"《{course_name}》试卷方案/题库", md, js, parents, note="generate")
                st.success("已保存试卷方案。")
                st.rerun()

    with tab2:
        if not a:
            st.info("暂无试卷方案。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])

    with tab3:
        if not a:
            st.info("暂无试卷方案。")
        else:
            edited = md_textarea("在线编辑试卷方案", a["content_md"], key="as_edit")
            note = st.text_input("保存说明", value="edit", key="as_note")
            if st.button("保存修改", type="primary", key="as_save"):
                parents = pick_parents_for(project_id, "assessment")
                a2 = upsert_artifact(project_id, "assessment", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无试卷方案。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="试卷方案.docx")


def page_review():
    ensure_project()
    render_depbar(project_id, "review")
    sy = get_artifact(project_id, "syllabus")
    ass = get_artifact(project_id, "assessment")
    a = get_artifact(project_id, "review")

    st.markdown("### 审核表：依据试卷方案/教学大纲生成（可编辑）")
    tab1, tab2, tab3, tab4 = st.tabs(["生成", "预览", "编辑", "版本/导出"])

    with tab1:
        if not (sy and ass):
            st.warning("缺少上游依赖：需要 教学大纲 + 试卷方案。")
        if st.button("生成并保存审核表", type="primary"):
            if not (sy and ass):
                st.error("请先生成教学大纲与试卷方案。")
            else:
                course_name = sy["content_json"].get("course_name", "课程")
                md, js = template_review_forms(course_name, ass["content_json"], sy["content_json"])
                parents = [ass["id"], sy["id"]]
                a2 = upsert_artifact(project_id, "review", f"《{course_name}》审核表集合", md, js, parents, note="generate")
                st.success("已保存审核表。")
                st.rerun()

    with tab2:
        if not a:
            st.info("暂无审核表。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])

    with tab3:
        if not a:
            st.info("暂无审核表。")
        else:
            edited = md_textarea("在线编辑审核表", a["content_md"], key="rv_edit")
            note = st.text_input("保存说明", value="edit", key="rv_note")
            if st.button("保存修改", type="primary", key="rv_save"):
                parents = pick_parents_for(project_id, "review")
                a2 = upsert_artifact(project_id, "review", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无审核表。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="审核表.docx")


def page_report():
    ensure_project()
    render_depbar(project_id, "report")
    sy = get_artifact(project_id, "syllabus")
    a = get_artifact(project_id, "report")

    st.markdown("### 课程目标达成评价报告：依据教学大纲生成（可编辑）")
    tab1, tab2, tab3, tab4 = st.tabs(["生成/上传成绩", "预览", "编辑", "版本/导出"])

    with tab1:
        if not sy:
            st.warning("缺少上游依赖：教学大纲。")
        note = st.text_area("补充说明（如：本轮教学特点/问题）", value="可在此写入教学反思与改进闭环说明。", height=100)
        st.caption("成绩表上传（可选）：后续可扩展为自动计算达成度（演示版暂不计算）。")
        st.file_uploader("上传成绩表（CSV/Excel）", type=["csv", "xlsx"], key="grade_up")

        if st.button("生成并保存达成报告", type="primary"):
            if not sy:
                st.error("请先生成教学大纲。")
            else:
                course_name = sy["content_json"].get("course_name", "课程")
                md, js = template_report(course_name, sy["content_json"], note=note)
                parents = [sy["id"]]
                a2 = upsert_artifact(project_id, "report", f"《{course_name}》课程目标达成报告", md, js, parents, note="generate")
                st.success("已保存达成报告。")
                st.rerun()

    with tab2:
        if not a:
            st.info("暂无达成报告。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])

    with tab3:
        if not a:
            st.info("暂无达成报告。")
        else:
            edited = md_textarea("在线编辑达成报告", a["content_md"], key="rp_edit")
            note2 = st.text_input("保存说明", value="edit", key="rp_note")
            if st.button("保存修改", type="primary", key="rp_save"):
                parents = pick_parents_for(project_id, "report")
                a2 = upsert_artifact(project_id, "report", a["title"], edited, a["content_json"], parents, note=note2)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无达成报告。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="达成报告.docx")


def page_evidence():
    ensure_project()
    render_depbar(project_id, "evidence")
    a = get_artifact(project_id, "evidence")

    st.markdown("### 课堂状态与过程证据（上传照片生成摘要）")
    st.caption("合规提示：不做身份识别，仅输出 Stu 编号 + 状态估计，用于“过程证据”支撑。")

    context = st.text_input("课堂内容（用于生成更贴合的摘要）", value="微积分：链式法则讲解", key="ev_ctx")
    up = st.file_uploader("上传课堂照片（JPG/PNG）", type=["jpg", "jpeg", "png"], key="ev_img")

    if up is not None:
        img = ImageOps.exif_transpose(Image.open(up)).convert("RGB")
        st.image(img, caption="上传的课堂照片（仅用于生成摘要）", use_container_width=True)
        if st.button("生成并保存过程证据摘要", type="primary"):
            dataurl = img_to_dataurl(img)
            summary = qwen_vl_classroom_summary(dataurl, context)
            md = f"# 课堂过程证据摘要\n\n- 课堂内容：{context}\n\n{summary}\n"
            a2 = upsert_artifact(
                project_id,
                "evidence",
                "课堂过程证据摘要",
                md,
                {"context": context, "source": up.name},
                [],
                note="generate",
            )
            st.success("已保存过程证据摘要。可在“授课手册”模块自动引用。")
            st.rerun()

    st.markdown("#### 当前证据")
    if not a:
        st.info("暂无过程证据。你可以上传一张课堂照片生成摘要。")
    else:
        artifact_toolbar(a)
        render_doc_preview(a["content_md"])


def page_manual():
    ensure_project()
    render_depbar(project_id, "manual")
    lp = get_artifact(project_id, "lesson_plan")
    ev = get_artifact(project_id, "evidence")
    a = get_artifact(project_id, "manual")

    st.markdown("### 授课手册：依赖教案（可选引用过程证据）")
    tab1, tab2, tab3, tab4 = st.tabs(["生成", "预览", "编辑", "版本/导出"])

    with tab1:
        st.markdown("#### 生成/上传 → 识别确认 → 保存（推荐先用方式B）")

        method = st.radio(
            "选择方式",
            ["方式B：上传已有培养方案（识别→确认→保存）", "方式A：一键生成（演示/快速）"],
            horizontal=True,
            index=0,
            key="tp_method_switch",
        )

        # -------------------- 方式B（全宽） --------------------
        if method.startswith("方式B"):
            st.markdown("### 方式B：上传已有培养方案（识别→确认→保存）")

            up = st.file_uploader(
                "上传培养方案文件",
                type=["pdf", "doc", "docx", "txt"],
                key="tp_upload",
            )

            use_ai_fix = st.checkbox(
                "（可选）用千问对识别结果做纠错/补全",
                value=False,
                disabled=not run_mode.startswith("在线"),
                key="tp_use_ai_fix",
            )

            cbtn1, cbtn2 = st.columns([1, 3])
            with cbtn1:
                start = st.button("开始识别（生成清单）", key="tp_start_extract", type="primary", disabled=(up is None))
            with cbtn2:
                st.caption("建议：PDF 若是扫描图片或表格是图片，pdfplumber 可能抓不到表；此时可跳过矩阵自动识别，直接手工录入支撑点。")

            if up is not None and start:
                txt = extract_text_from_upload(up)
                tables_pack = extract_pdf_tables(up) if (up.name.lower().endswith(".pdf")) else []
                checklist = extract_training_plan_checklist(txt)
                matrix_guess = guess_course_support_matrix(tables_pack)

                st.session_state["tp_extract"] = {
                    "source": up.name,
                    "text": txt,
                    "tables_pack": tables_pack,
                    "checklist": checklist,
                    "matrix_guess": matrix_guess,
                    "course_edges": [{"source": "先修课程A", "target": "后续课程B"}],
                }
                st.success("已生成识别清单 ✅ 请在下方全宽区域确认/修正，然后再保存。")

        # -------------------- 方式A（全宽） --------------------
        else:
            st.markdown("### 方式A：一键生成（演示/快速）")
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

        # =========================================================
        # 识别清单：一定要放在 columns 外面，才能全宽显示（关键修复点）
        # =========================================================
        if "tp_extract" in st.session_state:
            ex = st.session_state["tp_extract"]

            st.markdown("---")
            st.markdown("## 识别清单（请确认/修正）")
            st.caption("原则：系统先尽力抽取，最终以你的确认结果为准；确认后的结构化信息会用于后续大纲自动填充。")

            ck = ex["checklist"]

            colA, colB, colC = st.columns([1, 1, 1])
            with colA:
                major2 = st.text_input("专业（可修正）", value=ck.get("major_guess", ""), key="tp_major_fix")
                grade2 = st.text_input("年级（可修正）", value=ck.get("grade_guess", ""), key="tp_grade_fix")
            with colB:
                course_group2 = st.text_input("课程体系/方向（可补充）", value="", key="tp_group_fix")
                confirmed_flag = st.checkbox("我已确认以上信息大体正确", value=False, key="tp_confirm_flag")
            with colC:
                st.markdown("**识别来源**")
                st.code(ex.get("source", ""), language="text")

            st.markdown("### 1) 培养目标（可编辑）")
            goals_init = ck.get("goals_guess", []) or []
            goals_text = st.text_area(
                "每行一个目标（可增删/改写）",
                value="\n".join(goals_init) if goals_init else "",
                height=140,
                key="tp_goals_edit",
            )
            goals_final = [x.strip() for x in goals_text.splitlines() if x.strip()]

            st.markdown("### 2) 毕业要求（可编辑）")
            out_init = ck.get("outcomes_guess", []) or []
            if pd is not None:
                df_out = pd.DataFrame(out_init) if out_init else pd.DataFrame(columns=["no", "name"])
                df_out2 = st.data_editor(df_out, use_container_width=True, num_rows="dynamic", key="tp_out_editor")
                outcomes_final = [
                    {"no": str(r["no"]), "name": str(r["name"])}
                    for _, r in df_out2.iterrows()
                    if str(r.get("no", "")).strip()
                ]
            else:
                outcomes_json = st.text_area(
                    "毕业要求 JSON（数组）",
                    value=json.dumps(out_init, ensure_ascii=False, indent=2),
                    height=160,
                    key="tp_out_json",
                )
                try:
                    outcomes_final = json.loads(outcomes_json) if outcomes_json.strip() else []
                except Exception:
                    outcomes_final = out_init

            st.markdown("### 3) 课程-毕业要求支撑矩阵（表格→可编辑）")
            mg = ex["matrix_guess"]
            st.info(mg.get("hint", ""))
            best = mg.get("best_table", None)

            edited_best_table = None
            if best and best.get("table"):
                st.markdown(f"**疑似矩阵表（第 {best.get('page')} 页）**：请直接改表格内容（包括表头）")
                edited_best_table, ok_table = render_table_editor(
                    best["table"],
                    key="tp_matrix_table_editor",
                    title="毕业要求-课程目标矩阵（识别结果）",
                )
                if ok_table:
                    st.session_state["tp_matrix_table_confirmed"] = edited_best_table
                    st.success("该表格已确认并缓存为最终版本。")
                else:
                    st.warning("未勾选确认：该表格暂不会作为最终版本写入。")
            else:
                st.warning("未抽到疑似支撑矩阵表格。你可以：1) PDF更清晰时再试；2) 下面手工录入支撑点。")

            st.markdown("### 4) 课程关系图（边表→可编辑 + Graphviz 预览）")
            st.caption("很多PDF导图是图片不易还原；用“边表”最稳：填“先修→后续”。")

            edges = ex.get("course_edges", [{"source": "", "target": ""}])
            if pd is not None:
                df_e = pd.DataFrame(edges)
                df_e2 = st.data_editor(df_e, use_container_width=True, num_rows="dynamic", key="tp_edges_editor")
                edges_final = [{"source": str(r["source"]), "target": str(r["target"])} for _, r in df_e2.iterrows()]
            else:
                edges_json = st.text_area(
                    "边表 JSON（数组）",
                    value=json.dumps(edges, ensure_ascii=False, indent=2),
                    height=160,
                    key="tp_edges_json",
                )
                try:
                    edges_final = json.loads(edges_json) if edges_json.strip() else edges
                except Exception:
                    edges_final = edges

            st.graphviz_chart(dot_from_edge_rows(edges_final))

            st.markdown("### 5) 支撑指标点（用于大纲默认填充）")
            support_points_text = st.text_input(
                "当前要重点支持的课程指标点（逗号分隔，如 1.1,2.3,3.2）",
                value="",
                key="tp_support_points_text",
            )
            support_points = [x.strip() for x in re.split(r"[，,;\s]+", support_points_text) if x.strip()]

            st.markdown("---")
            btn_save, btn_clear = st.columns([1, 1])
            with btn_save:
                if st.button("✅ 确认并保存为培养方案底座", type="primary", disabled=not confirmed_flag):
                    text_final = ex.get("text", "") or ""

                    # 可选：AI校正（保持你的逻辑不变）
                    if st.session_state.get("tp_use_ai_fix", False) and get_qwen_key():
                        try:
                            sys = "你是高校培养方案抽取与校正助手。输出必须是JSON+简短说明。"
                            user = f"""
    请对以下培养方案文本做结构化抽取并校正，重点抽取：培养目标、毕业要求列表（含编号与名称）、任何出现的“课程-毕业要求支撑关系”提示。
    返回JSON字段：goals(list[str]), outcomes(list[{{no,name}}]), notes(str)。
    文本（截断）：{text_final[:8000]}
    """
                            out = qwen_chat(
                                [{"role": "system", "content": sys}, {"role": "user", "content": user}],
                                temperature=0.2,
                                max_tokens=1200,
                            )
                            m = re.search(r"\{[\s\S]*\}", out)
                            if m:
                                js_ai = json.loads(m.group(0))
                                if not goals_final and js_ai.get("goals"):
                                    goals_final = js_ai.get("goals", [])
                                if (not outcomes_final) and js_ai.get("outcomes"):
                                    outcomes_final = js_ai.get("outcomes", [])
                        except Exception as e:
                            st.warning(f"AI校正失败（忽略，不影响保存）：{e}")

                    content_json = {
                        "source": ex.get("source", ""),
                        "confirmed": True,
                        "major": major2,
                        "grade": grade2,
                        "course_group": course_group2,
                        "goals": goals_final,
                        "outcomes": outcomes_final,
                        "support_points_default": support_points,
                        "support_matrix_best_table": {
                            "page": best.get("page") if best else None,
                            "table": edited_best_table if edited_best_table is not None else (best.get("table") if best else None),
                        },
                        "course_edges": edges_final,
                    }

                    md = "# 培养方案（上传识别-已确认）\n\n"
                    md += f"- 专业：{major2}\n- 年级：{grade2}\n- 课程体系/方向：{course_group2}\n\n"
                    md += "## 一、培养目标（确认版）\n" + ("\n".join([f"- {x}" for x in goals_final]) if goals_final else "- （未填）") + "\n\n"
                    md += "## 二、毕业要求（确认版）\n" + ("\n".join([f"- {o.get('no','')}. {o.get('name','')}" for o in outcomes_final]) if outcomes_final else "- （未填）") + "\n\n"
                    md += "## 三、课程支撑指标点（默认/示例）\n" + (("、".join(support_points)) if support_points else "（未填）") + "\n\n"
                    md += "## 四、原始抽取文本（供追溯）\n\n" + (ex.get("text", "")[:20000] if ex.get("text") else "")

                    title = f"培养方案（确认版）-{ex.get('source','上传')}"
                    upsert_artifact(project_id, "training_plan", title, md, content_json, [], note="upload-confirm")
                    st.success("已保存“确认版培养方案底座”。后续生成大纲会优先使用结构化字段。")
                    st.session_state.pop("tp_extract", None)
                    st.rerun()

            with btn_clear:
                if st.button("清除本次识别结果（不保存）"):
                    st.session_state.pop("tp_extract", None)
                    st.info("已清除。")


    with tab2:
        if not a:
            st.info("暂无授课手册。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])

    with tab3:
        if not a:
            st.info("暂无授课手册。")
        else:
            edited = md_textarea("在线编辑授课手册", a["content_md"], key="mn_edit")
            note = st.text_input("保存说明", value="edit", key="mn_note")
            if st.button("保存修改", type="primary", key="mn_save"):
                parents = pick_parents_for(project_id, "manual")
                a2 = upsert_artifact(project_id, "manual", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无授课手册。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes_plaintext(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="授课手册.docx")


def page_vge():
    ensure_project()
    st.markdown("### 证据链与可验证生成（VGE）")
    st.caption("展示：每份文档的 hash、依赖边、可追溯关系（用于申报“可验证生成/证据链”亮点）。")

    arts = list_artifacts(project_id)
    if not arts:
        st.info("暂无文档。请先生成培养方案/大纲等。")
        return

    rows = []
    for a in arts:
        rows.append(
            {
                "类型": a["type"],
                "名称": a["title"],
                "Hash": a["hash"][:16],
                "更新时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["updated_at"])),
            }
        )
    st.markdown('<div class="card"><b>文档清单（hash 作为可验证标识）</b></div>', unsafe_allow_html=True)
    st.dataframe(rows, use_container_width=True)

    conn = db()
    e = conn.execute(
        "SELECT c.type, c.title, c.hash, p.type, p.title, p.hash "
        "FROM edges e "
        "JOIN artifacts c ON e.child_artifact_id=c.id "
        "JOIN artifacts p ON e.parent_artifact_id=p.id "
        "WHERE e.project_id=? ORDER BY e.id DESC",
        (project_id,),
    ).fetchall()
    conn.close()

    st.markdown('<div class="card"><b>依赖关系（child ← parent）</b></div>', unsafe_allow_html=True)
    rows2 = []
    if not e:
        st.info("暂无依赖边（还未生成依赖型文件）。")
    else:
        for r in e:
            rows2.append({"Child": f"{r[0]} | {r[1]} | {r[2][:12]}", "Parent": f"{r[3]} | {r[4]} | {r[5][:12]}"})
        st.dataframe(rows2, use_container_width=True)

    export = {"project_id": project_id, "artifacts": arts, "edges": rows2}
    st.download_button("下载 VGE 证据链日志（JSON）", data=json.dumps(export, ensure_ascii=False, indent=2), file_name="vge_log.json")


# ---------------------------
# 路由：按模块显示
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

fn = ROUTES.get(current_type, page_overview)
fn()

st.caption("注：演示版支持无API生成；在线模式可启用千问；模板化DOCX导出需 docxtpl。")
