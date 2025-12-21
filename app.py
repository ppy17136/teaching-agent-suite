import os
import io
import re
import json
import time
import base64
import hashlib
import sqlite3
import datetime
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple

import requests
import numpy as np
import pandas as pd
import streamlit as st

from PIL import Image, ImageOps
import cv2

import pdfplumber
import fitz  # PyMuPDF
from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH


# =========================
# 基础配置
# =========================
st.set_page_config(page_title="教学智能体平台（教评一体化）", layout="wide")

BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TEXT_MODEL = "qwen-max"
VISION_MODEL = "qwen-vl-plus"   # 没权限可换 qwen-vl-max / 或关闭视觉AI

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "app.db")
os.makedirs(DATA_DIR, exist_ok=True)


# =========================
# 小工具
# =========================
def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    return name.strip()[:120] if name.strip() else "file"

def get_qwen_api_key() -> str:
    return st.secrets.get("QWEN_API_KEY", os.environ.get("QWEN_API_KEY", "")).strip()

def has_api() -> bool:
    return bool(get_qwen_api_key())

def info_mode() -> str:
    return "在线模式（千问）" if has_api() else "演示模式（无API）"


# =========================
# 数据库（证据链/版本/项目）
# =========================
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS artifacts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        title TEXT NOT NULL,
        content_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        parent_hash TEXT,
        self_hash TEXT NOT NULL,
        FOREIGN KEY(project_id) REFERENCES projects(id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        file_hash TEXT NOT NULL,
        mime TEXT,
        uploaded_at TEXT NOT NULL,
        note TEXT,
        FOREIGN KEY(project_id) REFERENCES projects(id)
    );
    """)
    conn.commit()
    conn.close()

init_db()


def list_projects() -> List[Tuple[int, str]]:
    conn = db()
    rows = conn.execute("SELECT id,name FROM projects ORDER BY id DESC").fetchall()
    conn.close()
    return rows

def create_project(name: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO projects(name,created_at) VALUES (?,?)", (name, now_str()))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid

def save_file_record(project_id: int, filename: str, file_hash: str, mime: str, note: str=""):
    conn = db()
    conn.execute(
        "INSERT INTO files(project_id,filename,file_hash,mime,uploaded_at,note) VALUES (?,?,?,?,?,?)",
        (project_id, filename, file_hash, mime, now_str(), note)
    )
    conn.commit()
    conn.close()

def list_files(project_id: int) -> pd.DataFrame:
    conn = db()
    rows = conn.execute(
        "SELECT filename,file_hash,mime,uploaded_at,note FROM files WHERE project_id=? ORDER BY id DESC",
        (project_id,)
    ).fetchall()
    conn.close()
    return pd.DataFrame(rows, columns=["文件名","SHA256","MIME","上传时间","备注"])

def save_artifact(project_id: int, a_type: str, title: str, content: Dict[str,Any], parent_hash: Optional[str]=None) -> str:
    content_json = json.dumps(content, ensure_ascii=False, indent=2)
    self_hash = sha256_text(a_type + title + content_json + (parent_hash or ""))
    conn = db()
    conn.execute(
        "INSERT INTO artifacts(project_id,type,title,content_json,created_at,parent_hash,self_hash) VALUES (?,?,?,?,?,?,?)",
        (project_id, a_type, title, content_json, now_str(), parent_hash, self_hash)
    )
    conn.commit()
    conn.close()
    return self_hash

def list_artifacts(project_id: int, a_type: Optional[str]=None) -> pd.DataFrame:
    conn = db()
    if a_type:
        rows = conn.execute(
            "SELECT type,title,created_at,parent_hash,self_hash FROM artifacts WHERE project_id=? AND type=? ORDER BY id DESC",
            (project_id, a_type)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT type,title,created_at,parent_hash,self_hash FROM artifacts WHERE project_id=? ORDER BY id DESC",
            (project_id,)
        ).fetchall()
    conn.close()
    return pd.DataFrame(rows, columns=["类型","标题","创建时间","父哈希","自身哈希"])

def get_latest_artifact(project_id: int, a_type: str) -> Optional[Dict[str,Any]]:
    conn = db()
    row = conn.execute(
        "SELECT content_json,self_hash FROM artifacts WHERE project_id=? AND type=? ORDER BY id DESC LIMIT 1",
        (project_id, a_type)
    ).fetchone()
    conn.close()
    if not row:
        return None
    content = json.loads(row[0])
    content["_self_hash"] = row[1]
    return content


# =========================
# 文档读取（PDF/DOCX/TXT）
# =========================
def read_pdf_text_pdfplumber(file_bytes: bytes, max_pages: int=50) -> str:
    text = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            t = page.extract_text() or ""
            if t.strip():
                text.append(t)
    return "\n".join(text)

def read_pdf_text_fitz(file_bytes: bytes, max_pages: int=50) -> str:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    text = []
    for i in range(min(max_pages, doc.page_count)):
        page = doc.load_page(i)
        t = page.get_text("text") or ""
        if t.strip():
            text.append(t)
    return "\n".join(text)

def read_docx_text(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paras)

def read_any_text(uploaded) -> Tuple[str,str,str]:
    """return text, mime, sha"""
    b = uploaded.getvalue()
    file_hash = sha256_bytes(b)
    name = uploaded.name
    mime = uploaded.type or ""
    ext = os.path.splitext(name)[1].lower()

    if ext == ".pdf":
        # 先用 pdfplumber，失败再 fitz
        try:
            txt = read_pdf_text_pdfplumber(b)
            if len(txt.strip()) < 50:
                txt2 = read_pdf_text_fitz(b)
                if len(txt2.strip()) > len(txt.strip()):
                    txt = txt2
        except Exception:
            txt = read_pdf_text_fitz(b)
        return txt, (mime or "application/pdf"), file_hash

    if ext == ".docx":
        return read_docx_text(b), (mime or "application/vnd.openxmlformats-officedocument.wordprocessingml.document"), file_hash

    if ext in [".txt", ".md"]:
        return b.decode("utf-8", errors="ignore"), (mime or "text/plain"), file_hash

    # 老 .doc 不稳定：提示用户转docx
    return "", (mime or "application/octet-stream"), file_hash


# =========================
# 千问调用（文本/视觉）
# =========================
def qwen_chat(messages: List[Dict[str,Any]], model: str, temperature=0.3, max_tokens=1200, timeout=45) -> str:
    api_key = get_qwen_api_key()
    if not api_key:
        raise RuntimeError("No API KEY")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    resp = requests.post(BASE_URL + "/chat/completions", headers=headers, json=data, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"LLM non-200 {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]

@st.cache_data(ttl=600, show_spinner=False)
def qwen_json_strict(prompt: str, model: str = TEXT_MODEL) -> Dict[str,Any]:
    """要求严格输出 JSON；解析失败会自动抽取花括号再解析"""
    if not has_api():
        return {"_demo": True, "text": "DEMO"}
    messages = [
        {"role":"system","content":"你是严谨的教学文件生成与审核助手。必须输出严格JSON，不要markdown，不要额外文字。"},
        {"role":"user","content":prompt}
    ]
    text = qwen_chat(messages, model=model, temperature=0.2, max_tokens=1400)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        return {"error":"JSON解析失败", "raw": text[:500]}

def img_to_data_url(face_rgb: np.ndarray) -> str:
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(face_rgb, cv2.COLOR_RGB2BGR), [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise RuntimeError("encode jpg failed")
    b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"

@st.cache_data(ttl=600, show_spinner=False)
def qwen_vl_attention(face_dataurl: str, context: str) -> Dict[str,Any]:
    """视觉判断专注度/情绪"""
    if not has_api():
        return {"attention":"需要关注","emotion":"未知","reason":"演示模式"}
    prompt = f"""
你是课堂观察助手。请根据学生脸部截图，结合课堂内容“{context}”，估计其课堂状态。
只允许输出严格JSON：
{{
  "attention": "专注/需要关注/状态不佳",
  "emotion": "平静/困倦/紧张/好奇/烦躁/未知",
  "reason": "不超过20字依据"
}}
注意：这是概率估计，不要涉及身份识别。
""".strip()
    messages = [
        {"role":"system","content":"必须输出严格JSON。"},
        {"role":"user","content":[
            {"type":"text","text":prompt},
            {"type":"image_url","image_url":{"url": face_dataurl}}
        ]}
    ]
    text = qwen_chat(messages, model=VISION_MODEL, temperature=0.2, max_tokens=220, timeout=60)
    try:
        return json.loads(text.strip())
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        return {"attention":"未知","emotion":"未知","reason":"解析失败"}


# =========================
# 可验证生成：一致性校验 + 证据链
# =========================
def verify_chain(training: Optional[Dict[str,Any]],
                 syllabus: Optional[Dict[str,Any]],
                 calendar: Optional[Dict[str,Any]]) -> List[Dict[str,str]]:
    """非常实用的“可验证生成”演示：检查字段是否齐、是否引用上游信息"""
    issues = []
    if training:
        if not training.get("program_name"):
            issues.append({"level":"WARN","item":"培养方案","msg":"缺少 program_name（专业/方案名称）"})
        if not training.get("graduate_requirements"):
            issues.append({"level":"WARN","item":"培养方案","msg":"缺少 graduate_requirements（毕业要求/指标点）"})
    else:
        issues.append({"level":"ERROR","item":"培养方案","msg":"未提供培养方案（请上传或生成）"})

    if syllabus:
        if not syllabus.get("course_name"):
            issues.append({"level":"ERROR","item":"教学大纲","msg":"缺少 course_name"})
        if not syllabus.get("course_objectives"):
            issues.append({"level":"ERROR","item":"教学大纲","msg":"缺少 course_objectives（课程目标）"})
        if training and training.get("graduate_requirements") and not syllabus.get("support_matrix"):
            issues.append({"level":"WARN","item":"教学大纲","msg":"建议给出 课程目标-毕业要求指标点 支撑矩阵 support_matrix"})
    else:
        issues.append({"level":"ERROR","item":"教学大纲","msg":"未生成教学大纲"})

    if calendar:
        weeks = calendar.get("weeks", [])
        if not weeks:
            issues.append({"level":"ERROR","item":"教学日历","msg":"weeks为空"})
        if syllabus and syllabus.get("course_content") and len(weeks) < 6:
            issues.append({"level":"WARN","item":"教学日历","msg":"周次过少，可能未覆盖大纲内容"})
    else:
        issues.append({"level":"ERROR","item":"教学日历","msg":"未生成教学日历"})

    return issues


# =========================
# 文档生成：演示模板 + 在线模式 JSON 生成
# =========================
def demo_training_program() -> Dict[str,Any]:
    return {
        "program_name":"材料成型及控制工程（示例培养方案）",
        "degree":"工学学士",
        "length_years":4,
        "total_credits":165,
        "graduate_requirements":[
            {"id":"GR1","name":"工程知识","indicators":["GR1-1","GR1-2"]},
            {"id":"GR2","name":"问题分析","indicators":["GR2-1","GR2-2"]},
            {"id":"GR3","name":"设计/开发解决方案","indicators":["GR3-1"]},
        ],
        "curriculum_structure":{
            "required_credits":120,
            "elective_credits":45,
            "practice_credits":30
        },
        "notes":"演示模式自动生成。"
    }

def demo_syllabus(course_name: str) -> Dict[str,Any]:
    return {
        "course_name": course_name,
        "credits": 2.0,
        "hours_total": 32,
        "course_objectives":[
            {"id":"CO1","desc":"掌握课程核心概念与基本方法"},
            {"id":"CO2","desc":"能够将方法应用到典型问题并进行结果分析"},
            {"id":"CO3","desc":"具备工程表达与规范化报告能力"},
        ],
        "support_matrix":[
            {"course_objective":"CO1","supports":["GR1-1","GR2-1"]},
            {"course_objective":"CO2","supports":["GR2-2","GR3-1"]},
            {"course_objective":"CO3","supports":["GR1-2"]},
        ],
        "assessment":{
            "components":[
                {"name":"平时作业","weight":0.3,"maps":["CO1","CO2"]},
                {"name":"课程大作业/报告","weight":0.2,"maps":["CO3"]},
                {"name":"期末考试","weight":0.5,"maps":["CO1","CO2","CO3"]}
            ],
            "attainment_threshold":0.65
        },
        "course_content":[
            {"chapter":"第1章","topics":["概述","关键概念"],"hours":4},
            {"chapter":"第2章","topics":["核心方法A","案例"],"hours":8},
            {"chapter":"第3章","topics":["方法B","误差与验证"],"hours":8},
            {"chapter":"第4章","topics":["综合应用","工程报告"],"hours":12},
        ],
        "notes":"演示模式自动生成。"
    }

def demo_calendar(syllabus: Dict[str,Any], weeks: int=16) -> Dict[str,Any]:
    content = syllabus.get("course_content", [])
    # 简单展开到周次
    w = []
    cur = 1
    for ch in content:
        hours = int(ch.get("hours", 2))
        need_weeks = max(1, round(hours/2))
        for k in range(need_weeks):
            w.append({
                "week": cur,
                "topic": f"{ch['chapter']}：{ch['topics'][min(k, len(ch['topics'])-1)]}",
                "hours": 2,
                "activity": "讲授+例题+互动",
                "homework": "课后练习/小测"
            })
            cur += 1
    while cur <= weeks:
        w.append({
            "week": cur,
            "topic": "复习/答疑/测评/课程总结",
            "hours": 2,
            "activity": "讨论+答疑",
            "homework": "整理笔记/复习提纲"
        })
        cur += 1
    return {"weeks": w, "notes":"演示模式自动生成。"}

def llm_generate_training_program(user_constraints: str, reference_text: str="") -> Dict[str,Any]:
    prompt = f"""
请生成“培养方案”的结构化JSON。必须包含字段：
program_name, degree, length_years, total_credits,
graduate_requirements[{{
  id,name,indicators[]
}}],
curriculum_structure{{required_credits,elective_credits,practice_credits}},
notes

约束条件（用户输入）：
{user_constraints}

参考文本（可能来自上传培养方案，若为空则自主生成）：
{reference_text[:6000]}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        return demo_training_program()
    return j

def llm_generate_syllabus(course_name: str, requirements: str, training_program: Dict[str,Any], reference_text: str="") -> Dict[str,Any]:
    prompt = f"""
你是教学文件生成专家。请依据培养方案与用户要求，生成课程《{course_name}》教学大纲的结构化JSON。
必须包含字段：
course_name, credits, hours_total,
course_objectives[{{id,desc}}],
support_matrix[{{course_objective,supports[]}}],
assessment{{components[{{name,weight,maps[]}}], attainment_threshold}},
course_content[{{chapter,topics[],hours}}],
notes

培养方案（JSON）：
{json.dumps(training_program, ensure_ascii=False)[:6000]}

用户对课程大纲的额外要求：
{requirements}

参考文本（可能来自上传大纲）：
{reference_text[:6000]}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        return demo_syllabus(course_name)
    return j

def llm_generate_calendar(syllabus: Dict[str,Any], weeks: int, requirements: str, reference_text: str="") -> Dict[str,Any]:
    prompt = f"""
请基于课程教学大纲生成教学日历JSON。
必须包含字段：weeks[{{week,topic,hours,activity,homework}}], notes
总周次 = {weeks}

课程教学大纲（JSON）：
{json.dumps(syllabus, ensure_ascii=False)[:6000]}

用户要求：
{requirements}

参考文本（可能来自已有日历）：
{reference_text[:6000]}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        return demo_calendar(syllabus, weeks=weeks)
    return j

def llm_generate_lesson_plans(calendar: Dict[str,Any], requirements: str) -> Dict[str,Any]:
    prompt = f"""
请根据教学日历生成“教案包”的JSON：
必须包含 lessons[{{week, title, objectives[], key_points[], difficulties[], procedure[], interaction[], homework, assessment}}], notes

教学日历：
{json.dumps(calendar, ensure_ascii=False)[:6000]}

用户要求：
{requirements}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        lessons = []
        for w in calendar.get("weeks", [])[:12]:
            lessons.append({
                "week": w["week"],
                "title": w["topic"],
                "objectives":["理解本节核心概念","能完成典型例题","形成要点笔记"],
                "key_points":["核心概念","典型解题步骤"],
                "difficulties":["边界条件理解","结果解释"],
                "procedure":["导入(5min)","讲授(40min)","例题(30min)","小测(10min)","总结(5min)"],
                "interaction":["随堂提问","同伴讨论2分钟"],
                "homework": w.get("homework","课后练习"),
                "assessment":"随堂小测+作业"
            })
        return {"lessons": lessons, "notes":"演示模式自动生成。"}
    return j

def llm_generate_question_bank_and_exam(syllabus: Dict[str,Any], requirements: str) -> Dict[str,Any]:
    prompt = f"""
请依据教学大纲生成：题库与试卷方案（JSON）。
必须包含字段：
question_bank[{{id, type, difficulty, maps[], stem, answer, points}}],
exam_paper{{structure[], total_points, coverage_report}},
notes
题目类型可包含：单选/填空/简答/计算/综合；difficulty用1-5。
覆盖关系 maps[] 必须引用课程目标ID（如CO1/CO2...）。

教学大纲：
{json.dumps(syllabus, ensure_ascii=False)[:6000]}

用户要求：
{requirements}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        qb = []
        for i in range(1, 16):
            qb.append({
                "id": f"Q{i}",
                "type": "简答" if i%3==0 else "单选",
                "difficulty": (i%5)+1,
                "maps": ["CO1"] if i<6 else (["CO2"] if i<11 else ["CO3"]),
                "stem": f"示例题目{i}：请回答/选择……",
                "answer": "示例答案……",
                "points": 5 if i%3==0 else 2
            })
        exam = {
            "structure":[
                {"section":"一、选择题","count":10,"each":2,"maps":["CO1","CO2"]},
                {"section":"二、简答题","count":4,"each":5,"maps":["CO1","CO2","CO3"]},
                {"section":"三、综合题","count":1,"each":10,"maps":["CO2","CO3"]}
            ],
            "total_points": 10*2 + 4*5 + 10,
            "coverage_report":"演示：CO1/CO2/CO3均覆盖。"
        }
        return {"question_bank": qb, "exam_paper": exam, "notes":"演示模式自动生成。"}
    return j

def llm_generate_exam_review_forms(syllabus: Dict[str,Any], exam_pack: Dict[str,Any], requirements: str) -> Dict[str,Any]:
    """
    生成：试题审核表、评价依据合理性审核表
    """
    prompt = f"""
请生成两份表单的JSON：
1) exam_review_form（试题审核表）
2) evidence_rationality_form（课程目标达成评价依据合理性审核表）

要求字段建议（你可增补）：
exam_review_form {{
  course_name, term, reviewer, items[{{section, question_ids[], maps[], points, rationale}}], conclusion
}}
evidence_rationality_form {{
  course_name, threshold, evidence_items[{{name, weight, observation_points[], method_desc, maps[]}}], conclusion
}}

教学大纲：
{json.dumps(syllabus, ensure_ascii=False)[:6000]}

题库与试卷：
{json.dumps(exam_pack, ensure_ascii=False)[:6000]}

用户要求：
{requirements}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        return {
            "exam_review_form":{
                "course_name": syllabus.get("course_name","课程"),
                "term": "2024-2025-1",
                "reviewer": "示例审核人",
                "items":[
                    {"section":"选择题","question_ids":["Q1","Q2","Q3"],"maps":["CO1"],"points":6,"rationale":"覆盖基础概念与辨析"},
                    {"section":"简答题","question_ids":["Q6","Q9"],"maps":["CO2"],"points":10,"rationale":"考查方法应用与解释"},
                ],
                "conclusion":"试题覆盖课程目标合理，难度梯度合适。"
            },
            "evidence_rationality_form":{
                "course_name": syllabus.get("course_name","课程"),
                "threshold": syllabus.get("assessment",{}).get("attainment_threshold",0.65),
                "evidence_items":[
                    {"name":"平时作业","weight":0.3,"observation_points":["作业正确率","过程规范"],"method_desc":"按rubric评分后折算达成度","maps":["CO1","CO2"]},
                    {"name":"期末考试","weight":0.5,"observation_points":["关键题得分率"],"method_desc":"按题目映射CO统计得分","maps":["CO1","CO2","CO3"]},
                ],
                "conclusion":"评价依据覆盖CO且权重合理，计算方法可复核。"
            }
        }
    return j

def llm_generate_attainment_report(syllabus: Dict[str,Any], exam_pack: Dict[str,Any], requirements: str, demo_scores: Optional[Dict[str,float]]=None) -> Dict[str,Any]:
    """
    生成：课程目标达成情况评价报告（含原因分析与改进）
    """
    prompt = f"""
请生成“课程目标达成情况评价报告”JSON：
必须字段：
course_name, threshold,
attainment[{{objective_id, score, achieved, evidence}}], 
analysis, improvements, notes

教学大纲：
{json.dumps(syllabus, ensure_ascii=False)[:6000]}

题库与试卷：
{json.dumps(exam_pack, ensure_ascii=False)[:6000]}

用户要求：
{requirements}

如果给定demo_scores则参考：
{json.dumps(demo_scores or {}, ensure_ascii=False)}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        th = syllabus.get("assessment",{}).get("attainment_threshold",0.65)
        cos = [co["id"] for co in syllabus.get("course_objectives",[])]
        demo = demo_scores or {cid: float(np.clip(np.random.normal(0.72,0.08),0.3,0.95)) for cid in cos}
        att = []
        for cid in cos:
            s = float(demo.get(cid,0.7))
            att.append({"objective_id":cid, "score": round(s,3), "achieved": (s>=th), "evidence":"作业+考试映射统计"})
        return {
            "course_name": syllabus.get("course_name","课程"),
            "threshold": th,
            "attainment": att,
            "analysis":"若某CO未达阈值，常见原因：练习不足/反馈滞后/题目覆盖不均。",
            "improvements":[
                "增加针对未达成CO的分层练习与随堂测",
                "在教学日历中提前安排‘关键难点复盘’",
                "优化题库：提高该CO对应题目数量与梯度"
            ],
            "notes":"演示模式自动生成。"
        }
    return j

def llm_generate_teaching_manual(calendar: Dict[str,Any], classroom_evidence: List[Dict[str,Any]], requirements: str) -> Dict[str,Any]:
    """
    授课手册：过程记录+总结+改进措施+证据链引用
    """
    prompt = f"""
请生成“授课手册”JSON，包含：
meta{{term,teacher,class,course_name}},
records[{{week, topic, hours, method, homework, reflection}}],
summary, improvements[], evidence_refs[], notes

教学日历：
{json.dumps(calendar, ensure_ascii=False)[:6000]}

课堂过程证据（可能为空）：
{json.dumps(classroom_evidence, ensure_ascii=False)[:3000]}

用户要求：
{requirements}
""".strip()
    j = qwen_json_strict(prompt, model=TEXT_MODEL)
    if j.get("_demo"):
        recs = []
        for w in calendar.get("weeks", [])[:12]:
            recs.append({
                "week": w["week"],
                "topic": w["topic"],
                "hours": w.get("hours",2),
                "method": "讲授+互动+随堂测",
                "homework": w.get("homework","练习"),
                "reflection": "学生易错点：……；下次改进：……"
            })
        return {
            "meta":{"term":"2024-2025-1","teacher":"示例教师","class":"材料2201","course_name":"示例课程"},
            "records":recs,
            "summary":"整体进度与大纲一致；采用题库随机抽题减少抄袭；课堂互动提升参与度。",
            "improvements":["加强对薄弱CO的分层训练","增加案例驱动与工程表达训练"],
            "evidence_refs":["生成日志哈希","课堂照片状态摘要"],
            "notes":"演示模式自动生成。"
        }
    return j


# =========================
# DOCX 输出
# =========================
def docx_set_title(doc: Document, title: str):
    p = doc.add_paragraph()
    run = p.add_run(title)
    run.bold = True
    run.font.size = Pt(18)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

def docx_add_kv(doc: Document, k: str, v: str):
    p = doc.add_paragraph()
    r1 = p.add_run(f"{k}：")
    r1.bold = True
    p.add_run(v)

def to_docx_bytes(title: str, sections: List[Tuple[str, Any]]) -> bytes:
    doc = Document()
    docx_set_title(doc, title)
    doc.add_paragraph()

    for sec_title, sec_content in sections:
        h = doc.add_paragraph()
        hr = h.add_run(sec_title)
        hr.bold = True
        hr.font.size = Pt(14)

        if isinstance(sec_content, str):
            doc.add_paragraph(sec_content)
        elif isinstance(sec_content, list):
            for item in sec_content:
                doc.add_paragraph(str(item))
        elif isinstance(sec_content, dict):
            doc.add_paragraph(json.dumps(sec_content, ensure_ascii=False, indent=2))
        else:
            doc.add_paragraph(str(sec_content))
        doc.add_paragraph()

    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


# =========================
# 人脸检测（DNN）+ 颜色
# =========================
@st.cache_resource
def load_dnn_face_net():
    os.makedirs(os.path.join(DATA_DIR, "models"), exist_ok=True)
    proto_path = os.path.join(DATA_DIR, "models", "deploy.prototxt")
    model_path = os.path.join(DATA_DIR, "models", "res10_300x300_ssd_iter_140000.caffemodel")

    PROTO_URL = "https://raw.githubusercontent.com/opencv/opencv/master/samples/dnn/face_detector/deploy.prototxt"
    MODEL_URL = "https://raw.githubusercontent.com/opencv/opencv_3rdparty/dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel"

    def download(url, path):
        if os.path.exists(path) and os.path.getsize(path) > 1000:
            return
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)

    download(PROTO_URL, proto_path)
    download(MODEL_URL, model_path)
    net = cv2.dnn.readNetFromCaffe(proto_path, model_path)
    return net

def detect_faces_dnn(frame_rgb: np.ndarray, conf_thr=0.65) -> List[Tuple[int,int,int,int,float]]:
    h, w = frame_rgb.shape[:2]
    net = load_dnn_face_net()
    frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    blob = cv2.dnn.blobFromImage(frame_bgr, 1.0, (300, 300), (104.0, 177.0, 123.0))
    net.setInput(blob)
    dets = net.forward()

    boxes = []
    for i in range(dets.shape[2]):
        conf = float(dets[0, 0, i, 2])
        if conf < conf_thr:
            continue
        x1 = int(dets[0, 0, i, 3] * w)
        y1 = int(dets[0, 0, i, 4] * h)
        x2 = int(dets[0, 0, i, 5] * w)
        y2 = int(dets[0, 0, i, 6] * h)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        fw, fh = x2-x1, y2-y1
        if fw < 40 or fh < 40:
            continue
        boxes.append((x1,y1,x2,y2,conf))
    boxes.sort(key=lambda b: (b[2]-b[0])*(b[3]-b[1]), reverse=True)
    return boxes

def state_to_color(att: str) -> str:
    if att == "专注":
        return "#2ecc71"
    elif att == "需要关注":
        return "#f1c40f"
    elif att == "状态不佳":
        return "#e74c3c"
    return "#95a5a6"


# =========================
# UI：侧边栏导航 + 项目选择
# =========================
st.sidebar.title("教学智能体平台")
st.sidebar.caption(f"运行模式：**{info_mode()}**")

# 项目区
with st.sidebar.expander("① 项目（专业/年级/课程体系）", expanded=True):
    projects = list_projects()
    options = ["（新建项目）"] + [f"{pid} - {name}" for pid,name in projects]
    sel = st.selectbox("选择项目", options=options, index=0)

    if sel == "（新建项目）":
        new_name = st.text_input("新项目名称", value="材料成型-教评一体化示例")
        if st.button("创建项目", use_container_width=True):
            pid = create_project(new_name)
            st.success(f"已创建项目：{pid}")
            st.rerun()
        project_id = None
    else:
        project_id = int(sel.split(" - ")[0])

page = st.sidebar.radio(
    "② 功能模块",
    [
        "首页总览",
        "数据底座（上传/抽取/版本）",
        "培养方案（生成/上传）",
        "课程教学大纲（依据培养方案）",
        "教学日历（依据大纲）",
        "教案（依据日历）",
        "作业/题库/试卷（依据大纲）",
        "教评闭环（审核表/达成报告）",
        "授课手册（过程记录/总结）",
        "课堂状态监测（过程证据）",
        "证据链与可验证生成（VGE）",
        "导出与打包（DOCX）"
    ]
)

if project_id is None:
    st.info("请先在左侧创建或选择一个项目。")
    st.stop()

# 缓存：课堂证据
if "classroom_evidence" not in st.session_state:
    st.session_state.classroom_evidence = []


# =========================
# 页面：首页总览
# =========================
if page == "首页总览":
    st.title("教学智能体平台（教评一体化，可验证生成与闭环优化）")
    st.write("建议申报呈现方式：**一个平台 + 三个闭环（设计链、教评链、过程证据链）**。")

    c1,c2,c3,c4 = st.columns(4)
    with c1:
        st.metric("项目ID", project_id)
    with c2:
        st.metric("已上传文件", int(len(list_files(project_id))))
    with c3:
        st.metric("已生成文档版本", int(len(list_artifacts(project_id))))
    with c4:
        st.metric("课堂证据条目", len(st.session_state.classroom_evidence))

    st.subheader("闭环流程（建议写进申报书）")
    st.markdown("""
- **教学文件链**：培养方案 → 教学大纲 → 教学日历 → 教案/作业/试卷  
- **教评闭环**：试题审核表 → 评价依据合理性审核 → 课程目标达成评价报告 → 改进措施回写  
- **过程证据链**：课堂状态（照片/记录）→ 干预建议 → 授课手册过程记录与总结 → 支撑评价
""")

    st.subheader("最新产物与版本（证据链）")
    st.dataframe(list_artifacts(project_id).head(12), use_container_width=True)


# =========================
# 页面：数据底座
# =========================
elif page == "数据底座（上传/抽取/版本）":
    st.title("数据底座：上传文件 → 抽取结构化要素 → 版本管理")
    st.caption("支持 PDF / DOCX / TXT。老 .doc 请先转成 .docx。")

    up = st.file_uploader("上传底层材料（培养方案/大纲/日历/表单等）", type=["pdf","docx","txt","md"])
    if up:
        text, mime, h = read_any_text(up)
        save_file_record(project_id, up.name, h, mime, note="原始材料")
        st.success(f"已入库：{up.name} | hash={h[:12]}...")
        if text:
            st.text_area("抽取到的文本预览（可用于后续生成依据）", value=text[:4000], height=220)
        else:
            st.warning("未抽取到文本（可能是扫描PDF/或不支持格式）。")

    st.subheader("已上传文件列表")
    st.dataframe(list_files(project_id), use_container_width=True)

    st.subheader("已生成文档版本（Artifacts）")
    st.dataframe(list_artifacts(project_id).head(20), use_container_width=True)


# =========================
# 页面：培养方案
# =========================
elif page == "培养方案（生成/上传）":
    st.title("培养方案：两种路径（生成 / 上传已有）")

    tab1, tab2 = st.tabs(["A. 重新生成培养方案（有约束）", "B. 上传已有培养方案（抽取后下游使用）"])

    with tab1:
        st.subheader("A 重新生成（建议用于展示“智能生成 + 可验证”）")
        constraints = st.text_area(
            "必要约束条件（建议写得像学校真实要求）",
            value="专业：材料成型及控制工程；学制4年；总学分165；实践学分≥30；核心课程包含：数值模拟在材料成型中的应用；毕业要求采用工程教育认证框架。",
            height=120
        )
        ref = st.text_area("可选：参考文本（从上传文件复制粘贴）", value="", height=120)

        if st.button("生成培养方案（JSON）", use_container_width=True):
            tp = llm_generate_training_program(constraints, ref)
            parent_hash = sha256_text(constraints + ref)
            h = save_artifact(project_id, "training_program", "培养方案", tp, parent_hash=parent_hash)
            st.success(f"生成完成，已入库（hash={h[:12]}...）")
            st.json(tp)

    with tab2:
        st.subheader("B 上传已有培养方案（推荐用于真实落地演示）")
        up = st.file_uploader("上传培养方案（PDF/DOCX/TXT）", type=["pdf","docx","txt"], key="upload_training")
        if up:
            txt, mime, fh = read_any_text(up)
            save_file_record(project_id, up.name, fh, mime, note="培养方案原文")
            st.success("已上传。下面进行结构化抽取（可编辑）。")

            # 抽取成 training_program JSON（在线/演示）
            if has_api():
                prompt = f"""
请把以下培养方案文本抽取为培养方案JSON，字段同前：program_name, degree, length_years, total_credits,
graduate_requirements[{{
 id,name,indicators[]
}}], curriculum_structure{{required_credits,elective_credits,practice_credits}}, notes
文本：
{txt[:9000]}
"""
                tp = qwen_json_strict(prompt, model=TEXT_MODEL)
            else:
                tp = demo_training_program()
                tp["notes"] = "演示模式：未启用API，使用示例结构。"

            tp_edit = st.text_area("抽取后的JSON（可编辑）", value=json.dumps(tp, ensure_ascii=False, indent=2), height=260)
            if st.button("保存为当前培养方案版本", use_container_width=True):
                tp2 = json.loads(tp_edit)
                h = save_artifact(project_id, "training_program", "培养方案（上传抽取）", tp2, parent_hash=fh)
                st.success(f"已保存（hash={h[:12]}...）")

    st.subheader("当前最新培养方案")
    latest = get_latest_artifact(project_id, "training_program")
    if latest:
        st.json(latest)
    else:
        st.info("暂无培养方案，请先生成或上传。")


# =========================
# 页面：课程教学大纲
# =========================
elif page == "课程教学大纲（依据培养方案）":
    st.title("课程教学大纲：严格依赖培养方案（可验证）")

    training = get_latest_artifact(project_id, "training_program")
    if not training:
        st.error("未找到培养方案。请先在“培养方案”页生成或上传。")
        st.stop()

    course_name = st.text_input("课程名称", value="数值模拟在材料成型中的应用")
    req = st.text_area(
        "对大纲的补充要求（例如：课程目标数量、考核比例、教学方法、实践要求）",
        value="课程目标3-5个；考核比例：平时30%+大作业20%+期末50%；强调工程表达与案例；写明课程目标-毕业要求指标点映射。",
        height=120
    )
    ref = st.text_area("可选：参考大纲文本（从上传材料复制粘贴）", value="", height=120)

    if st.button("生成教学大纲（JSON）", use_container_width=True):
        syl = llm_generate_syllabus(course_name, req, training, reference_text=ref)
        parent_hash = training.get("_self_hash") or sha256_text(json.dumps(training, ensure_ascii=False))
        h = save_artifact(project_id, "syllabus", f"教学大纲-{course_name}", syl, parent_hash=parent_hash)
        st.success(f"已生成并入库（hash={h[:12]}...）")
        st.json(syl)

    st.subheader("最新教学大纲")
    latest = get_latest_artifact(project_id, "syllabus")
    if latest:
        st.json(latest)
    else:
        st.info("暂无教学大纲版本。")


# =========================
# 页面：教学日历
# =========================
elif page == "教学日历（依据大纲）":
    st.title("教学日历：依据教学大纲自动排布（可微调）")

    syl = get_latest_artifact(project_id, "syllabus")
    if not syl:
        st.error("未找到教学大纲。请先生成教学大纲。")
        st.stop()

    weeks = st.slider("学期周次", 8, 20, 16)
    req = st.text_area("对日历的要求（例如：每章周次、实验/上机安排、考核点）", value="每周2学时；第8周安排阶段测；第16周复习与总结。", height=100)
    ref = st.text_area("可选：参考日历文本", value="", height=100)

    if st.button("生成教学日历（JSON）", use_container_width=True):
        cal = llm_generate_calendar(syl, weeks, req, reference_text=ref)
        parent_hash = syl.get("_self_hash") or sha256_text(json.dumps(syl, ensure_ascii=False))
        h = save_artifact(project_id, "calendar", "教学日历", cal, parent_hash=parent_hash)
        st.success(f"生成完成（hash={h[:12]}...）")
        st.json(cal)

    latest = get_latest_artifact(project_id, "calendar")
    if latest:
        st.subheader("最新教学日历预览")
        df = pd.DataFrame(latest.get("weeks", []))
        st.dataframe(df, use_container_width=True)
    else:
        st.info("暂无教学日历。")


# =========================
# 页面：教案
# =========================
elif page == "教案（依据日历）":
    st.title("教案：按周次/次课生成（基于教学日历）")

    cal = get_latest_artifact(project_id, "calendar")
    if not cal:
        st.error("未找到教学日历。请先生成教学日历。")
        st.stop()

    req = st.text_area("对教案的要求（目标、重点难点、互动、作业、评价方式等）", value="每次课包含：目标、重点难点、过程(分钟级)、互动设计、作业与评价。", height=100)

    if st.button("生成教案包（JSON）", use_container_width=True):
        plans = llm_generate_lesson_plans(cal, req)
        parent_hash = cal.get("_self_hash") or sha256_text(json.dumps(cal, ensure_ascii=False))
        h = save_artifact(project_id, "lesson_plans", "教案包", plans, parent_hash=parent_hash)
        st.success(f"已生成（hash={h[:12]}...）")
        st.json(plans)

    latest = get_latest_artifact(project_id, "lesson_plans")
    if latest:
        st.subheader("教案预览（前几节）")
        st.dataframe(pd.DataFrame(latest.get("lessons", [])[:8]), use_container_width=True)
    else:
        st.info("暂无教案包。")


# =========================
# 页面：作业/题库/试卷
# =========================
elif page == "作业/题库/试卷（依据大纲）":
    st.title("作业/题库/试卷：依据课程目标覆盖率自动生成")

    syl = get_latest_artifact(project_id, "syllabus")
    if not syl:
        st.error("未找到教学大纲。")
        st.stop()

    req = st.text_area("对题库/试卷的要求（题型、难度梯度、覆盖比例、总分、题量）",
                       value="总分100；选择题20分；简答题30分；综合题50分；覆盖CO1/CO2/CO3，CO2占比略高；难度梯度合理。",
                       height=100)

    if st.button("生成题库与试卷方案（JSON）", use_container_width=True):
        pack = llm_generate_question_bank_and_exam(syl, req)
        parent_hash = syl.get("_self_hash") or sha256_text(json.dumps(syl, ensure_ascii=False))
        h = save_artifact(project_id, "exam_pack", "题库与试卷方案", pack, parent_hash=parent_hash)
        st.success(f"已生成（hash={h[:12]}...）")
        st.json(pack)

    latest = get_latest_artifact(project_id, "exam_pack")
    if latest:
        st.subheader("题库预览（前10题）")
        st.dataframe(pd.DataFrame(latest.get("question_bank", [])[:10]), use_container_width=True)
        st.subheader("试卷结构")
        st.json(latest.get("exam_paper", {}))
    else:
        st.info("暂无题库与试卷方案。")


# =========================
# 页面：教评闭环
# =========================
elif page == "教评闭环（审核表/达成报告）":
    st.title("教评闭环：审核表 → 达成度 → 报告 → 改进措施回写")

    syl = get_latest_artifact(project_id, "syllabus")
    pack = get_latest_artifact(project_id, "exam_pack")
    if not syl or not pack:
        st.error("需要教学大纲 + 题库与试卷方案。请先在相应页面生成。")
        st.stop()

    req = st.text_area("对审核表/报告的要求（阈值、证据来源、写法风格）",
                       value="阈值0.65；证据包含作业、期末考试、课堂过程证据；报告包含原因分析与可执行改进措施。",
                       height=100)

    c1,c2 = st.columns(2)
    with c1:
        if st.button("生成：试题审核表 + 评价依据合理性审核表", use_container_width=True):
            forms = llm_generate_exam_review_forms(syl, pack, req)
            parent_hash = pack.get("_self_hash") or sha256_text(json.dumps(pack, ensure_ascii=False))
            h = save_artifact(project_id, "review_forms", "审核表（试题+评价依据）", forms, parent_hash=parent_hash)
            st.success(f"已生成（hash={h[:12]}...）")
            st.json(forms)

    with c2:
        if st.button("生成：课程目标达成评价报告", use_container_width=True):
            # 演示：可输入一个 CO 分数
            demo_scores = {}
            cos = [co["id"] for co in syl.get("course_objectives",[])]
            with st.expander("可选：手动输入演示达成度（留空则自动生成）", expanded=False):
                for cid in cos:
                    demo_scores[cid] = st.slider(f"{cid} 达成度", 0.0, 1.0, 0.72, 0.01)
            rep = llm_generate_attainment_report(syl, pack, req, demo_scores=demo_scores)
            parent_hash = syl.get("_self_hash") or sha256_text(json.dumps(syl, ensure_ascii=False))
            h = save_artifact(project_id, "attainment_report", "课程目标达成评价报告", rep, parent_hash=parent_hash)
            st.success(f"已生成（hash={h[:12]}...）")
            st.json(rep)

    st.subheader("最新审核表")
    latest_forms = get_latest_artifact(project_id, "review_forms")
    if latest_forms:
        st.json(latest_forms)

    st.subheader("最新达成评价报告")
    latest_rep = get_latest_artifact(project_id, "attainment_report")
    if latest_rep:
        st.json(latest_rep)
        df = pd.DataFrame(latest_rep.get("attainment", []))
        st.dataframe(df, use_container_width=True)


# =========================
# 页面：授课手册
# =========================
elif page == "授课手册（过程记录/总结）":
    st.title("授课手册：过程记录 + 总结 + 改进措施（可引用课堂证据）")

    cal = get_latest_artifact(project_id, "calendar")
    if not cal:
        st.error("需要教学日历。")
        st.stop()

    req = st.text_area("对授课手册的要求（教学改革、题库使用、反思结构等）",
                       value="按周记录授课内容、方法、作业、反思；总结写教学改革：题库随机抽题减少抄袭，提升巩固；列改进措施。",
                       height=100)

    if st.button("生成授课手册（JSON）", use_container_width=True):
        manual = llm_generate_teaching_manual(cal, st.session_state.classroom_evidence, req)
        parent_hash = cal.get("_self_hash") or sha256_text(json.dumps(cal, ensure_ascii=False))
        h = save_artifact(project_id, "teaching_manual", "授课手册", manual, parent_hash=parent_hash)
        st.success(f"已生成（hash={h[:12]}...）")
        st.json(manual)

    latest = get_latest_artifact(project_id, "teaching_manual")
    if latest:
        st.subheader("最新授课手册预览")
        st.json(latest)
    else:
        st.info("暂无授课手册。")


# =========================
# 页面：课堂状态监测（过程证据）
# =========================
elif page == "课堂状态监测（过程证据）":
    st.title("课堂状态监测（上传照片演示）→ 写入过程证据链")
    st.caption("隐私建议：只显示 Stu 编号，不做身份识别；仅用于教学参考。")

    context = st.text_input("当前课堂内容描述", value="链式法则讲解（示例）")
    conf_thr = st.slider("人脸检测置信度阈值", 0.40, 0.90, 0.65, 0.01)
    pad = st.slider("人脸框扩展比例", 0.00, 0.20, 0.05, 0.01)

    use_ai = st.checkbox("使用视觉AI判断专注度/情绪（较慢/耗API）", value=has_api())
    max_faces = st.slider("最多AI分析人数", 1, 12, 6)

    up = st.file_uploader("上传课堂照片（jpg/png）", type=["jpg","jpeg","png"])
    if up:
        img = ImageOps.exif_transpose(Image.open(up)).convert("RGB")
        frame_rgb = np.array(img)
        h, w = frame_rgb.shape[:2]
        draw = frame_rgb.copy()

        boxes = detect_faces_dnn(frame_rgb, conf_thr=conf_thr)
        statuses = []
        for i, (x1,y1,x2,y2,conf) in enumerate(boxes):
            bw, bh = x2-x1, y2-y1
            px, py = int(bw*pad), int(bh*pad)
            left, top = max(0, x1-px), max(0, y1-py)
            right, bottom = min(w, x2+px), min(h, y2+py)
            roi = frame_rgb[top:bottom, left:right]
            if roi.size == 0:
                continue

            name = f"Stu{i+1}"
            att, emo, reason = "未知","未知",f"det={conf:.2f}"

            if use_ai and i < max_faces:
                try:
                    r = qwen_vl_attention(img_to_data_url(roi), context)
                    att = r.get("attention","未知")
                    emo = r.get("emotion","未知")
                    reason = r.get("reason","")
                except Exception:
                    att, emo, reason = "未知","未知","AI调用失败"

            color = state_to_color(att)
            statuses.append({"name":name,"attention":att,"emotion":emo,"reason":reason})

            cv2.rectangle(draw, (left,top), (right,bottom), (255,0,0), 2)

            # OpenCV 不支持中文：用英文标签避免 ????
            att_map = {"专注":"FOCUS","需要关注":"WATCH","状态不佳":"ALERT"}
            show = att_map.get(att,"UNK")
            cv2.putText(draw, f"{name} {show}", (left, max(0, top-10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)

        st.image(draw, caption="检测结果（避免中文问号：图上用英文标签）", channels="RGB")

        st.subheader("班级学生状态（卡片）")
        if not statuses:
            st.warning("未检测到人脸：请换更清晰、正面更多的照片。")
        else:
            cols = st.columns(min(6, len(statuses)))
            for idx, s in enumerate(statuses[:len(cols)]):
                with cols[idx]:
                    st.markdown(
                        f"""
                        <div style="background:{state_to_color(s['attention'])};
                                    padding:14px;border-radius:14px;text-align:center;">
                            <b>{s['name']}</b><br>
                            情绪：{s['emotion']}<br>
                            专注度：{s['attention']}<br>
                            <span style="font-size:12px;opacity:0.85;">{s['reason']}</span>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

            if st.button("写入为过程证据（供授课手册/达成报告引用）", use_container_width=True):
                ev = {
                    "time": now_str(),
                    "context": context,
                    "summary": statuses,
                    "note": "课堂状态证据（仅供教学参考）"
                }
                st.session_state.classroom_evidence.append(ev)
                st.success("已写入过程证据链。")


# =========================
# 页面：证据链与可验证生成（VGE）
# =========================
elif page == "证据链与可验证生成（VGE）":
    st.title("可验证生成（VGE）：依赖链检查 + 哈希证据链 + 版本可追溯")

    training = get_latest_artifact(project_id, "training_program")
    syllabus = get_latest_artifact(project_id, "syllabus")
    calendar = get_latest_artifact(project_id, "calendar")

    st.subheader("一致性校验（示例规则，可扩展）")
    issues = verify_chain(training, syllabus, calendar)
    if issues:
        df = pd.DataFrame(issues)
        st.dataframe(df, use_container_width=True)
    else:
        st.success("未发现明显一致性问题。")

    st.subheader("版本与哈希链（Artifacts）")
    st.dataframe(list_artifacts(project_id).head(50), use_container_width=True)

    st.subheader("上传文件与哈希（Files）")
    st.dataframe(list_files(project_id), use_container_width=True)

    st.subheader("导出证据链日志（JSON）")
    art = list_artifacts(project_id)
    fil = list_files(project_id)
    evidence = {
        "project_id": project_id,
        "exported_at": now_str(),
        "mode": info_mode(),
        "artifacts": art.to_dict(orient="records"),
        "files": fil.to_dict(orient="records"),
        "classroom_evidence": st.session_state.classroom_evidence,
        "verification_issues": issues
    }
    b = json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button("下载证据链日志 JSON", data=b, file_name=f"evidence_project_{project_id}.json", use_container_width=True)


# =========================
# 页面：导出与打包（DOCX）
# =========================
elif page == "导出与打包（DOCX）":
    st.title("导出与打包：把关键教学文件一键导出为 DOCX（便于申报展示）")
    st.caption("演示版直接把结构化JSON写入DOCX；你后续可替换为更严格的学校模板排版。")

    training = get_latest_artifact(project_id, "training_program")
    syllabus = get_latest_artifact(project_id, "syllabus")
    calendar = get_latest_artifact(project_id, "calendar")
    plans = get_latest_artifact(project_id, "lesson_plans")
    pack = get_latest_artifact(project_id, "exam_pack")
    forms = get_latest_artifact(project_id, "review_forms")
    rep = get_latest_artifact(project_id, "attainment_report")
    manual = get_latest_artifact(project_id, "teaching_manual")

    def dl_docx(btn: str, title: str, sections: List[Tuple[str,Any]]):
        b = to_docx_bytes(title, sections)
        st.download_button(btn, data=b, file_name=f"{safe_filename(title)}.docx", use_container_width=True)

    c1,c2,c3 = st.columns(3)

    with c1:
        if training:
            dl_docx("下载：培养方案.docx", "培养方案", [("培养方案JSON", training)])
        if syllabus:
            dl_docx("下载：教学大纲.docx", "课程教学大纲", [("教学大纲JSON", syllabus)])

    with c2:
        if calendar:
            dl_docx("下载：教学日历.docx", "教学日历", [("教学日历JSON", calendar)])
        if plans:
            dl_docx("下载：教案包.docx", "教案包", [("教案JSON", plans)])

    with c3:
        if pack:
            dl_docx("下载：题库与试卷方案.docx", "题库与试卷方案", [("题库与试卷JSON", pack)])
        if forms:
            dl_docx("下载：审核表.docx", "审核表（试题+评价依据）", [("审核表JSON", forms)])
        if rep:
            dl_docx("下载：达成评价报告.docx", "课程目标达成评价报告", [("达成报告JSON", rep)])
        if manual:
            dl_docx("下载：授课手册.docx", "授课手册", [("授课手册JSON", manual)])

    st.subheader("一键打包导出（建议：申报材料）")
    st.markdown("""
建议至少准备：
- 平台在线演示链接（Streamlit Cloud）
- 1份“证据链日志 JSON”（可验证生成）
- 7份 DOCX：培养方案/大纲/日历/教案/题库试卷/审核表/达成报告/授课手册
- 2-3分钟演示视频（从上传→生成→校验→导出）
""")
