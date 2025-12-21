# -*- coding: utf-8 -*-
import os
import io
import re
import json
import time
import base64
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple

import streamlit as st
import requests
import numpy as np
from PIL import Image, ImageOps

# 可选：用于解析PDF/DOC/DOCX（你仓库已有这些依赖时就能用）
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

# ---------------------------
# 基础配置（云端友好）
# ---------------------------
st.set_page_config(page_title="教学智能体平台", layout="wide")

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_TEXT_MODEL = "qwen-max"
DEFAULT_VL_MODEL = "qwen-vl-plus"  # 可选，用于“课堂照片→状态摘要”，不做身份识别

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")


# ---------------------------
# UI 美化（CSS）
# ---------------------------
def inject_css():
    st.markdown(
        """
<style>
/* 全局排版 */
.main .block-container { padding-top: 1.0rem; padding-bottom: 2rem; max-width: 1280px; }
h1, h2, h3 { letter-spacing: .2px; }

/* 顶部标题条 */
.topbar{
  padding: 18px 18px;
  border-radius: 18px;
  background: linear-gradient(90deg, #0ea5e9 0%, #6366f1 55%, #8b5cf6 100%);
  color: white;
  box-shadow: 0 8px 24px rgba(0,0,0,.12);
}
.topbar .title{ font-size: 30px; font-weight: 800; }
.topbar .sub{ opacity: .9; margin-top: 6px; font-size: 14px; }

/* 卡片 */
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

/* 依赖条 */
.depbar{
  display:flex; gap:8px; flex-wrap: wrap; padding: 10px 0;
}
.depitem{
  padding: 8px 10px; border-radius: 14px; border: 1px solid rgba(0,0,0,.10);
  background: rgba(255,255,255,.7); font-size: 13px;
}
.depitem b{ margin-right:6px; }

/* 文档预览区 */
.docbox{
  border: 1px solid rgba(0,0,0,.10);
  border-radius: 18px;
  padding: 14px 16px;
  background: rgba(255,255,255,.75);
}

/* Sidebar 标题 */
section[data-testid="stSidebar"] .stMarkdown h2{
  font-size: 18px; font-weight: 800;
}

/* 表格更紧凑 */
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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
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
  type TEXT NOT NULL,                -- training_plan / syllabus / calendar / lesson_plan / assessment / review / report / manual / evidence
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


init_db()


def now_ts() -> int:
    return int(time.time())


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def compute_hash(content_md: str, content_json: Dict[str, Any], parent_hashes: List[str]) -> str:
    payload = {
        "content_md": content_md,
        "content_json": content_json,
        "parents": parent_hashes,
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def get_projects() -> List[Tuple[int, str]]:
    conn = db()
    rows = conn.execute("SELECT id, name FROM projects ORDER BY id DESC;").fetchall()
    conn.close()
    return rows


def create_project(name: str, meta: Dict[str, Any]) -> int:
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
    rows = conn.execute(
        "SELECT id, type, title, hash, updated_at FROM artifacts WHERE project_id=? ORDER BY updated_at DESC",
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


def get_parent_hashes(project_id: int, child_id: int) -> List[str]:
    conn = db()
    rows = conn.execute(
        "SELECT a.hash FROM edges e JOIN artifacts a ON e.parent_artifact_id=a.id "
        "WHERE e.project_id=? AND e.child_artifact_id=?",
        (project_id, child_id),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def set_edges(project_id: int, child_id: int, parent_ids: List[int]):
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
    parent_hashes = []
    for pid in parent_ids:
        conn = db()
        row = conn.execute("SELECT hash FROM artifacts WHERE id=? AND project_id=?", (pid, project_id)).fetchone()
        conn.close()
        if row:
            parent_hashes.append(row[0])

    new_hash = compute_hash(content_md, content_json, parent_hashes)
    ts = now_ts()

    conn = db()
    if existing:
        # 写入版本表（旧版本）
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
        aid = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO artifacts(project_id, type, title, content_md, content_json, hash, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                project_id,
                a_type,
                title,
                content_md,
                json.dumps(content_json, ensure_ascii=False),
                new_hash,
                ts,
                ts,
            ),
        )
        conn.commit()
        aid = cur.lastrowid
    conn.close()

    set_edges(project_id, aid, parent_ids)

    return get_artifact(project_id, a_type)


# ---------------------------
# 依赖规则（文档链）
# ---------------------------
DOC_TYPES = [
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
]

DEP_RULES = {
    "training_plan": [],
    "syllabus": ["training_plan"],
    "calendar": ["syllabus"],
    "lesson_plan": ["calendar"],
    "assessment": ["syllabus"],
    "review": ["assessment", "syllabus"],
    "report": ["syllabus"],  # 可选加成绩
    "manual": ["lesson_plan"],  # 可选加证据
    "evidence": [],
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

    # fallback
    file.seek(0)
    try:
        return file.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


# ---------------------------
# 千问：文本生成（可选）
# ---------------------------
def get_qwen_key() -> str:
    return st.secrets.get("QWEN_API_KEY", os.environ.get("QWEN_API_KEY", "")).strip()

def qwen_chat(messages: List[Dict[str, Any]], model: str = DEFAULT_TEXT_MODEL, temperature: float = 0.3, max_tokens: int = 1400) -> str:
    key = get_qwen_key()
    if not key:
        raise RuntimeError("未配置 QWEN_API_KEY（当前为演示模式可不填）")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    data = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = requests.post(BASE_URL + "/chat/completions", headers=headers, json=data, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM接口错误：{resp.status_code} {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


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

def template_syllabus(course_name: str, hours_total: int, credits: float, extra_req: str, tp_text: str) -> Tuple[str, Dict[str, Any]]:
    # 简化：从培养方案提取“毕业要求关键词”作为映射底座
    outcomes = []
    for line in tp_text.splitlines():
        m = re.match(r"^\s*\d+\.\s*(.+)$", line.strip())
        if m:
            outcomes.append(m.group(1).strip())
    outcomes = outcomes[:8] or ["工程知识", "问题分析", "设计/开发解决方案", "现代工具使用"]

    obj = [
        {"id": "CO1", "desc": "理解课程核心概念与基本方法", "map_to": outcomes[0]},
        {"id": "CO2", "desc": "能基于案例进行建模/分析并解释结果", "map_to": outcomes[1]},
        {"id": "CO3", "desc": "能够使用软件工具完成课程实践任务", "map_to": outcomes[min(3, len(outcomes)-1)]},
    ]

    md = f"""# 《{course_name}》课程教学大纲（严格依赖培养方案）

## 1. 课程基本信息
- 学分：{credits}
- 总学时：{hours_total}
- 课程性质：专业课/方向课（示例）

## 2. 课程目标（CO）与毕业要求映射
| 课程目标 | 描述 | 对应毕业要求 |
|---|---|---|
""" + "\n".join([f"| {x['id']} | {x['desc']} | {x['map_to']} |" for x in obj]) + f"""

## 3. 考核方式与比例（可调整）
- 平时：30%
- 作业/项目：20%
- 期末：50%

## 4. 教学内容与学时分配（示例）
- 第1章：导论（2学时）
- 第2章：方法与工具（6学时）
- 第3章：案例与实践（10学时）
- 第4章：综合项目与答辩（{max(2, hours_total-18)}学时）

## 5. 实践与要求
{extra_req or "结合工程案例，强调表达与规范文档产出。"}
"""
    js = {"course_name": course_name, "hours_total": hours_total, "credits": credits, "CO": obj}
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
    rows = calendar_json.get("rows", [])[:4]  # 演示先出前4周
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
""" + "\n".join([f"- **{q['qid']}**（{q['type']}，对应{q['target_co']}）：{q['stem']}\n  - 评分细则：{q['rubric']}" for q in bank])
    return md, {"bank": bank}


def template_review_forms(course_name: str, assessment_json: Dict[str, Any], syllabus_json: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
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
""" + "\n".join([f"| {q['qid']} | {q['type']} | {q['target_co']} | 覆盖{q['target_co']}关键能力 | 通过 |" for q in bank]) + f"""

## B. 课程目标达成评价依据合理性审核（示例）
| 课程目标 | 评价证据 | 证据充分性 | 备注 |
|---|---|---|---|
""" + "\n".join([f"| {c} | 题库/作业/项目/期末 | 较充分 | 可持续优化 |" for c in co]) + f"""

## C. 覆盖检查
""" + "\n".join([f"- {k}：{v} 题" for k, v in cover.items()])
    return md, {"coverage": cover}


def template_report(course_name: str, syllabus_json: Dict[str, Any], note: str = "") -> Tuple[str, Dict[str, Any]]:
    co = [x["id"] for x in syllabus_json.get("CO", [])] or ["CO1", "CO2", "CO3"]
    # 演示：没有成绩就给一个合理的“占位达成度”
    achieve = {c: round(0.72 - i*0.05, 2) for i, c in enumerate(co)}
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
# 课堂证据（可选）：上传图片→生成“状态摘要”
# 说明：不做身份识别，只输出 Stu 编号 + 概率估计
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
        return "（演示模式：未配置QWEN_API_KEY，课堂证据摘要暂用占位文本）\n- Stu1：专注\n- Stu2：需要关注"
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
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_dataurl}},
            ]}
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

import html

def render_doc_preview(md: str):
    safe = html.escape(md).replace("\n", "<br>")
    st.markdown(f'<div class="docbox">{safe}</div>', unsafe_allow_html=True)


def md_textarea(label: str, value: str, height: int = 420, key: str = "") -> str:
    return st.text_area(label, value=value, height=height, key=key)

def artifact_toolbar(a: Dict[str, Any]):
    st.markdown(
        f"""
<div class="card">
  <div style="display:flex; justify-content:space-between; gap:12px; align-items:center;">
    <div>
      <div style="font-size:18px; font-weight:800;">{a['title']}</div>
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

def export_docx_bytes(md: str) -> bytes:
    # 极简导出：把 Markdown 当作纯文本段落
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

topbar()
st.write("")

# Sidebar
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
module = st.sidebar.radio(
    "导航",
    [name for _, name in DOC_TYPES],
    index=3,
)

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
    parent_ids = []
    for r in req:
        pa = get_artifact(project_id, r)
        if pa:
            parent_ids.append(pa["id"])
    # manual 可选加 evidence
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
    st.caption("你可以：①一键生成示例培养方案；②上传已有培养方案（PDF/DOC/DOCX）并抽取文本；③在线编辑并保存版本。")

    tab1, tab2, tab3, tab4 = st.tabs(["生成/上传", "预览", "编辑", "版本/导出"])

    with tab1:
        col1, col2 = st.columns([1,1])
        with col1:
            st.markdown("#### 方式A：一键生成（演示/快速）")
            major = st.text_input("专业", value="材料成型及控制工程", key="tp_major")
            grade = st.text_input("年级", value="22", key="tp_grade")
            group = st.text_input("课程体系/方向", value="材料成型-数值模拟方向", key="tp_group")
            if st.button("生成培养方案并保存", type="primary"):
                md = template_training_plan(major, grade, group)
                a = upsert_artifact(project_id, "training_plan", f"{grade}级《{major}》培养方案", md, {"major": major, "grade": grade}, [], note="generate")
                st.success("已保存培养方案（可作为后续文件依赖底座）")
                st.rerun()

        with col2:
            st.markdown("#### 方式B：上传已有培养方案（建议用于申报）")
            up = st.file_uploader("上传培养方案文件", type=["pdf","doc","docx","txt"], key="tp_upload")
            if up is not None and st.button("抽取并保存为培养方案", key="tp_extract"):
                txt = extract_text_from_upload(up)
                if not txt.strip():
                    st.error("未抽取到文本，请换更清晰的PDF或DOCX。")
                else:
                    md = "# 培养方案（上传抽取）\n\n" + txt
                    a = upsert_artifact(project_id, "training_plan", f"培养方案（上传抽取）-{up.name}", md, {"source": up.name}, [], note="upload")
                    st.success("已保存培养方案（上传抽取版）")
                    st.rerun()

    with tab2:
        if not a:
            st.info("暂无培养方案。请先生成或上传。")
        else:
            artifact_toolbar(a)
            render_doc_preview(a["content_md"])

    with tab3:
        if not a:
            st.info("暂无培养方案。请先生成或上传。")
        else:
            edited = md_textarea("在线编辑培养方案（支持直接修改）", a["content_md"], key="tp_edit")
            note = st.text_input("保存说明（可选）", value="edit", key="tp_note")
            if st.button("保存修改（生成新版本）", type="primary", key="tp_save"):
                a = upsert_artifact(project_id, "training_plan", a["title"], edited, a["content_json"], [], note=note)
                st.success("已保存。后续依赖文件将引用更新后的培养方案。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无培养方案。")
        else:
            vers = get_versions(a["id"])
            st.markdown("#### 版本记录")
            if not vers:
                st.caption("暂无历史版本（第一次保存后才会出现版本）。")
            else:
                st.dataframe(vers, use_container_width=True)
            st.markdown("#### 导出")
            docx_bytes = export_docx_bytes(a["content_md"])
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
    st.caption("推荐流程：上传/生成培养方案 → 在此生成大纲 → 预览/编辑 → 保存版本。")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["填写/生成", "预览", "编辑", "版本/导出", "依赖追溯"])

    with tab1:
        if not tp:
            st.warning("缺少上游依赖：培养方案。请先到“培养方案（底座）”模块生成/上传。")
        course_name = st.text_input("课程名称", value="数值模拟在材料成型中的应用", key="sy_course")
        credits = st.number_input("学分", min_value=0.5, max_value=10.0, value=2.0, step=0.5)
        hours_total = st.number_input("总学时", min_value=8, max_value=128, value=32, step=2)
        extra = st.text_area("对大纲的补充要求（考核比例/教学方法/实践要求等）", value="课程目标3-5个；平时30%+大作业20%+期末50%；强调工程表达与案例；写明CO-毕业要求映射。", height=120)

        use_ai = st.checkbox("使用千问生成更完整的大纲（需要QWEN_API_KEY）", value=(run_mode.startswith("在线")))
        if st.button("生成并保存教学大纲（JSON+可读预览）", type="primary"):
            if not tp:
                st.error("请先提供培养方案。")
            else:
                tp_text = tp["content_md"]
                if use_ai and get_qwen_key():
                    # AI：生成结构化 JSON + Markdown
                    sys = "你是高校教学文件撰写专家，输出必须规范、可落地。"
                    user = f"""请依据以下培养方案，为课程《{course_name}》撰写教学大纲。
要求：给出课程信息、课程目标CO(3-5)、CO-毕业要求映射、学时分配、教学方法、考核比例、实践要求。
补充要求：{extra}
培养方案文本：
{tp_text[:5000]}
输出：先输出 JSON（字段：course_name, credits, hours_total, CO[{id,desc,map_to}], assessment, outline），然后输出一份Markdown大纲。
"""
                    try:
                        out = qwen_chat(
                            [{"role":"system","content":sys},{"role":"user","content":user}],
                            model=DEFAULT_TEXT_MODEL,
                            temperature=0.2,
                            max_tokens=1600
                        )
                        # 尽量提取 JSON
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
                        md, js = template_syllabus(course_name, int(hours_total), float(credits), extra, tp_text)
                else:
                    md, js = template_syllabus(course_name, int(hours_total), float(credits), extra, tp_text)

                parents = [tp["id"]]
                a = upsert_artifact(project_id, "syllabus", f"《{course_name}》教学大纲", md, js, parents, note="generate")
                st.success("已保存教学大纲（后续日历/教案/试卷等将依赖它）")
                st.rerun()

    with tab2:
        if not a:
            st.info("暂无教学大纲。请在“填写/生成”中生成并保存。")
        else:
            artifact_toolbar(a)
            # 更好看的预览：把 JSON 摘要成卡片 + 大纲正文
            js = a["content_json"] or {}
            st.markdown('<div class="card"><b>结构化摘要</b></div>', unsafe_allow_html=True)
            c1, c2, c3 = st.columns(3)
            c1.metric("课程", js.get("course_name","-"))
            c2.metric("学分", js.get("credits","-"))
            c3.metric("总学时", js.get("hours_total","-"))
            st.markdown("#### 大纲正文")
            render_doc_preview(a["content_md"])

    with tab3:
        if not a:
            st.info("暂无教学大纲。")
        else:
            edited = md_textarea("在线编辑教学大纲", a["content_md"], key="sy_edit")
            note = st.text_input("保存说明（可选）", value="edit", key="sy_note")
            if st.button("保存修改（生成新版本）", type="primary", key="sy_save"):
                parents = pick_parents_for(project_id, "syllabus")
                a = upsert_artifact(project_id, "syllabus", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无教学大纲。")
        else:
            vers = get_versions(a["id"])
            st.markdown("#### 版本记录")
            st.dataframe(vers if vers else [], use_container_width=True)
            st.markdown("#### 导出")
            docx_bytes = export_docx_bytes(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="教学大纲.docx")
            st.download_button("下载 JSON（结构化）", data=json.dumps(a["content_json"], ensure_ascii=False, indent=2), file_name="教学大纲.json")

    with tab5:
        if not a:
            st.info("暂无教学大纲。")
        else:
            st.markdown("#### 上游依赖（可验证）")
            parents = pick_parents_for(project_id, "syllabus")
            if not parents:
                st.warning("未记录到依赖边。")
            else:
                conn = db()
                rows = conn.execute("SELECT id, type, title, hash FROM artifacts WHERE id IN (%s)" % ",".join(["?"]*len(parents)), parents).fetchall()
                conn.close()
                for r in rows:
                    st.write(f"- **{type_label(r[1])}**：{r[2]} ｜ hash={r[3][:16]}")

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
                md, js = template_calendar(sy["content_json"].get("course_name","课程"), int(weeks), sy["content_json"])
                parents = [sy["id"]]
                a = upsert_artifact(project_id, "calendar", f"《{sy['content_json'].get('course_name','课程')}》教学日历", md, js, parents, note="generate")
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
                a = upsert_artifact(project_id, "calendar", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()
    with tab4:
        if not a:
            st.info("暂无教学日历。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes(a["content_md"])
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
                    course_name = sy["content_json"].get("course_name","课程")
                md, js = template_lesson_plan(course_name, cal["content_json"])
                parents = [cal["id"]]
                a = upsert_artifact(project_id, "lesson_plan", f"《{course_name}》教案", md, js, parents, note="generate")
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
                a = upsert_artifact(project_id, "lesson_plan", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无教案。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes(a["content_md"])
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
                course_name = sy["content_json"].get("course_name","课程")
                md, js = template_assessment(course_name, sy["content_json"])
                parents = [sy["id"]]
                a = upsert_artifact(project_id, "assessment", f"《{course_name}》试卷方案/题库", md, js, parents, note="generate")
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
                a = upsert_artifact(project_id, "assessment", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无试卷方案。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes(a["content_md"])
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
                course_name = sy["content_json"].get("course_name","课程")
                md, js = template_review_forms(course_name, ass["content_json"], sy["content_json"])
                parents = [ass["id"], sy["id"]]
                a = upsert_artifact(project_id, "review", f"《{course_name}》审核表集合", md, js, parents, note="generate")
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
                a = upsert_artifact(project_id, "review", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无审核表。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes(a["content_md"])
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
                course_name = sy["content_json"].get("course_name","课程")
                md, js = template_report(course_name, sy["content_json"], note=note)
                parents = [sy["id"]]
                a = upsert_artifact(project_id, "report", f"《{course_name}》课程目标达成报告", md, js, parents, note="generate")
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
                a = upsert_artifact(project_id, "report", a["title"], edited, a["content_json"], parents, note=note2)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无达成报告。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes(a["content_md"])
            if docx_bytes:
                st.download_button("下载 DOCX（简版导出）", data=docx_bytes, file_name="达成报告.docx")

def page_evidence():
    ensure_project()
    render_depbar(project_id, "evidence")
    a = get_artifact(project_id, "evidence")

    st.markdown("### 课堂状态与过程证据（上传照片生成摘要）")
    st.caption("合规提示：不做身份识别，仅输出 Stu 编号 + 状态估计，用于“过程证据”支撑。")

    context = st.text_input("课堂内容（用于生成更贴合的摘要）", value="微积分：链式法则讲解", key="ev_ctx")
    up = st.file_uploader("上传课堂照片（JPG/PNG）", type=["jpg","jpeg","png"], key="ev_img")

    if up is not None:
        img = ImageOps.exif_transpose(Image.open(up)).convert("RGB")
        st.image(img, caption="上传的课堂照片（仅用于生成摘要）", use_container_width=True)
        if st.button("生成并保存过程证据摘要", type="primary"):
            dataurl = img_to_dataurl(img)
            summary = qwen_vl_classroom_summary(dataurl, context)
            md = f"# 课堂过程证据摘要\n\n- 课堂内容：{context}\n\n{summary}\n"
            a = upsert_artifact(project_id, "evidence", "课堂过程证据摘要", md, {"context": context, "source": up.name}, [], note="generate")
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
        if not lp:
            st.warning("缺少上游依赖：教案。")
        use_ev = st.checkbox("引用课堂过程证据摘要（如果存在）", value=True)
        if st.button("生成并保存授课手册", type="primary"):
            if not lp:
                st.error("请先生成教案。")
            else:
                sy = get_artifact(project_id, "syllabus")
                course_name = sy["content_json"].get("course_name","课程") if sy else "课程"
                ev_md = ev["content_md"] if (use_ev and ev) else ""
                md, js = template_manual(course_name, lp["content_json"], ev_md)
                parents = pick_parents_for(project_id, "manual")
                a = upsert_artifact(project_id, "manual", f"《{course_name}》授课手册", md, js, parents, note="generate")
                st.success("已保存授课手册。")
                st.rerun()

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
                a = upsert_artifact(project_id, "manual", a["title"], edited, a["content_json"], parents, note=note)
                st.success("已保存。")
                st.rerun()

    with tab4:
        if not a:
            st.info("暂无授课手册。")
        else:
            st.dataframe(get_versions(a["id"]) or [], use_container_width=True)
            docx_bytes = export_docx_bytes(a["content_md"])
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

    # 展示清单
    rows = []
    for a in arts:
        rows.append({
            "类型": a["type"],
            "名称": a["title"],
            "Hash": a["hash"][:16],
            "更新时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["updated_at"])),
        })
    st.markdown('<div class="card"><b>文档清单（hash 作为可验证标识）</b></div>', unsafe_allow_html=True)
    st.dataframe(rows, use_container_width=True)

    # 展示依赖边
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
    if not e:
        st.info("暂无依赖边（还未生成依赖型文件）。")
    else:
        rows2 = []
        for r in e:
            rows2.append({
                "Child": f"{r[0]} | {r[1]} | {r[2][:12]}",
                "Parent": f"{r[3]} | {r[4]} | {r[5][:12]}",
            })
        st.dataframe(rows2, use_container_width=True)

    # 导出证据链日志
    export = {"project_id": project_id, "artifacts": arts, "edges": rows2 if e else []}
    st.download_button("下载 VGE 证据链日志（JSON）", data=json.dumps(export, ensure_ascii=False, indent=2), file_name="vge_log.json")


# ---------------------------
# 路由：按模块显示
# ---------------------------
ROUTES = {
    "首页总览": page_overview,
    "培养方案（底座）": page_training_plan,
    "课程教学大纲（依赖培养方案）": page_syllabus,
    "教学日历（依赖大纲）": page_calendar,
    "教案（依赖日历）": page_lesson_plan,
    "作业/题库/试卷方案（依赖大纲）": page_assessment,
    "审核表（依赖试卷方案/大纲）": page_review,
    "课程目标达成报告（依赖大纲/成绩）": page_report,
    "授课手册（依赖教案/过程证据）": page_manual,
    "课堂状态与过程证据（可选）": page_evidence,
    "证据链与可验证生成（VGE）": page_vge,
}

# 根据 sidebar 的 current_type 映射到路由
if current_type == "training_plan":
    ROUTES["培养方案（底座）"]()
elif current_type == "syllabus":
    ROUTES["课程教学大纲（依赖培养方案）"]()
elif current_type == "calendar":
    ROUTES["教学日历（依赖大纲）"]()
elif current_type == "lesson_plan":
    ROUTES["教案（依赖日历）"]()
elif current_type == "assessment":
    ROUTES["作业/题库/试卷方案（依赖大纲）"]()
elif current_type == "review":
    ROUTES["审核表（依赖试卷方案/大纲）"]()
elif current_type == "report":
    ROUTES["课程目标达成报告（依赖大纲/成绩）"]()
elif current_type == "manual":
    ROUTES["授课手册（依赖教案/过程证据）"]()
elif current_type == "evidence":
    ROUTES["课堂状态与过程证据（可选）"]()
elif current_type == "vge":
    ROUTES["证据链与可验证生成（VGE）"]()
else:
    ROUTES["首页总览"]()
