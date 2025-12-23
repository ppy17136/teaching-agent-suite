# app.py
# ------------------------------------------------------------
# Teaching-Agent-Suite (Template-first, Project-based)
# 核心思路：
# 1) “培养方案基座”=项目的权威内容库（可PDF抽取/可手工/可LLM校对）
# 2) 其他教学文件=固定模板（课程大纲/教学日历/授课手册/达成度表等）
#    支持：上传docx/粘贴全文 → 离线抽取填充 →（可选LLM）结构化重建/校对 → 人工编辑 → 导出规范docx/xlsx
# 3) 数据持久化：data/projects/<project_id>/... 便于后续“记住并作为校准依据”
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

try:
    import requests
except Exception:
    requests = None


# =========================
# Globals / constants
# =========================

APP_NAME = "Teaching Agent Suite"
APP_VERSION = "v0.4 (template-first)"
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
    return s.strip()

def clamp(s: str, n: int = 12000) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n…(截断，原长度 {len(s)})"

def dataframe_safe(df: pd.DataFrame) -> pd.DataFrame:
    """
    解决 Streamlit/pyarrow 常见崩溃：列里出现 dict/list/None 混合类型。
    统一转成 str。
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
        height=min(height + 40, 800),
        scrolling=True,
    )

def json_download_button(label: str, obj: Any, filename: str):
    data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(label, data=data, file_name=filename, mime="application/json")

def to_xlsx_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        for name, df in sheets.items():
            name = name[:31]
            df2 = dataframe_safe(df)
            df2.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


# =========================
# Persistence (Project DB)
# =========================

@dataclass
class Project:
    project_id: str
    name: str
    created_at: str = field(default_factory=now_str)
    updated_at: str = field(default_factory=now_str)

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
            out.append(Project(**meta))
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
    return Project(**meta) if meta else None

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
# LLM (optional, OpenAI-compatible endpoint)
# =========================

@dataclass
class LLMConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = "qwen-plus"
    timeout: int = 60

def llm_available(cfg: LLMConfig) -> bool:
    return cfg.enabled and bool(cfg.base_url) and bool(cfg.api_key) and requests is not None

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
    """
    调 OpenAI-compatible /v1/chat/completions
    """
    if not llm_available(cfg):
        return None, "LLM 未启用或配置不完整。"

    url = cfg.base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": (user.strip() + ("\n\nJSON schema hint:\n" + schema_hint if schema_hint else "")).strip()},
    ]
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0.2,
        # 尽量要求直接 JSON；有些厂商忽略也没事，我们会兜底解析
        "response_format": {"type": "json_object"},
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=cfg.timeout)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
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
    """
    把 docx 的段落合并成文本；把所有表格转成 DataFrame 列表
    """
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

def docx_export_simple(template_title: str, sections: List[Tuple[str, str]], tables: List[Tuple[str, pd.DataFrame]] = None) -> bytes:
    """
    最简洁、稳定的 Word 导出：标题 + 一级标题 + 段落 + 表格
    """
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
                {
                    "week": "",
                    "lesson_no": "",
                    "content": "",
                    "key_points": "",
                    "hours": "",
                    "method": "",
                    "others": "",
                    "support_objective": "",
                }
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
    """
    只保留 schema 里的字段（避免 LLM 返回乱七八糟字段导致展示崩）
    """
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
        # schedule table: header has 周次/课次/教学内容
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
            # 例如：xxx《课程名》教学大纲
            for ln in lines[:30]:
                if "《" in ln and "》" in ln and ("教学大纲" in ln):
                    m = re.findall(r"《([^》]+)》", ln)
                    if m:
                        data["course_name"] = m[0]
                        break
        return data

    # 其他模板先给空壳（后续你可以继续补 heuristic 规则）
    return data


# =========================
# Training plan base (minimal PDF extractor)
# 你后续可以把你现有的强解析器接进来替换这里
# =========================

def pdf_extract_pages_text(pdf_bytes: bytes) -> List[str]:
    if pdfplumber is None:
        return []
    pages: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages

def split_sections_from_pages(pages_text: List[str]) -> Dict[str, str]:
    full = "\n".join([t for t in pages_text if t])
    if not full.strip():
        return {}
    full2 = full.replace("\r", "\n")

    heads: List[Tuple[str, int]] = []
    for title in SECTION_TITLES[:6]:
        key = re.escape(title.split("、", 1)[0]) + r"\s*、\s*" + re.escape(title.split("、", 1)[1])
        m = re.search(key, full2)
        if m:
            heads.append((title, m.start()))
    heads.sort(key=lambda x: x[1])

    out: Dict[str, str] = {}
    for i, (h, pos) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(full2)
        out[h] = clean_text(full2[pos:end])
    return out

def base_plan_minimal_from_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    pages = pdf_extract_pages_text(pdf_bytes)
    sections = split_sections_from_pages(pages)
    return {
        "meta": {"sha256": sha256_bytes(pdf_bytes), "created_at": now_str(), "extractor": "minimal-pdfplumber"},
        "sections": sections,
        "appendices": {},
        "raw_pages_text": pages,  # 方便你 debug 是否文本可抽
        "course_graph": {"nodes": [], "edges": []},  # 逻辑图（后续增强）
    }


# =========================
# Consistency checks (base version)
# =========================

def run_consistency_checks(template_type: str, data: Dict[str, Any], plan: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    sections = plan.get("sections", {})

    if template_type in ("教学日历", "课程大纲", "授课手册", "达成度评价依据审核表", "达成度评价报告"):
        if not clean_text(data.get("course_name", "")):
            warnings.append("课程名称为空：建议填写以便后续一致性校验/自动填充。")

    # 支撑码一致性：从培养方案“毕业要求”里提取 5.2/10.1 之类
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

    # 其他模板先用 JSON 文本导出（你后续可按学校规范再细化）
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

            # 导出 docx/xlsx（失败就写 error 文件）
            try:
                docx_bytes = export_docx_for_template(d["template_type"], d.get("data", {}), d.get("title", doc_id))
                z.writestr(f"exports/{d.get('title',doc_id)}.docx", docx_bytes)
            except Exception as e:
                z.writestr(f"exports/{d.get('title',doc_id)}.docx.ERROR.txt", str(e))

            try:
                x = export_xlsx_for_template(d["template_type"], d.get("data", {}))
                if x:
                    z.writestr(f"exports/{d.get('title',doc_id)}.xlsx", x)
            except Exception as e:
                z.writestr(f"exports/{d.get('title',doc_id)}.xlsx.ERROR.txt", str(e))

        # assets 目录原样打包（你后续可以把“附表/思维导图文件”放这里）
        ap = assets_dir(pid)
        if ap.exists():
            for fp in ap.glob("*"):
                z.write(fp, arcname=f"assets/{fp.name}")

    return buf.getvalue()


# =========================
# UI: editors
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

    # 其余模板：先给 JSON 形式，后续你再逐个做“专用编辑器”
    st.info("该模板暂未定制编辑器（先以JSON展示/编辑）。后续可以按学校规范做成专用表单。")
    st.json(data)
    return data


# =========================
# Sidebar / Pages
# =========================

def ui_project_sidebar() -> Tuple[Project, LLMConfig]:
    st.sidebar.markdown(f"### {APP_NAME}")
    st.sidebar.caption(APP_VERSION)

    projects = list_projects()
    names = ["➕ 新建项目"] + [f"{p.name}  ({p.project_id})" for p in projects]
    choice = st.sidebar.selectbox("项目", names, index=0)

    if choice.startswith("➕"):
        with st.sidebar.expander("新建项目", expanded=True):
            new_name = st.text_input("项目名称", value=f"项目-{dt.datetime.now().strftime('%Y%m%d-%H%M')}")
            if st.button("创建项目", use_container_width=True):
                pid = uuid.uuid4().hex[:10]
                prj = Project(project_id=pid, name=new_name)
                save_project(prj)
                st.session_state["active_project_id"] = pid
                st.rerun()
        active = None
        pid = st.session_state.get("active_project_id")
        if pid:
            active = load_project(pid)
        if not active and projects:
            active = projects[0]
    else:
        pid = choice.split("(")[-1].strip(")")
        st.session_state["active_project_id"] = pid
        active = load_project(pid)

    assert active is not None, "No active project"

    # ---- LLM toggle (你要的开关在这里) ----
    st.sidebar.markdown("---")
    st.sidebar.markdown("#### LLM 校对与修正（可选）")
    enabled = st.sidebar.checkbox("启用 LLM 校对与修正", value=False)
    base_url = st.sidebar.text_input("Base URL（OpenAI兼容）", value=os.environ.get("LLM_BASE_URL", ""))
    api_key = st.sidebar.text_input("API Key", value=os.environ.get("LLM_API_KEY", ""), type="password")
    model = st.sidebar.text_input("Model", value=os.environ.get("LLM_MODEL", "qwen-plus"))
    timeout = st.sidebar.slider("超时（秒）", 10, 180, 60)
    llm_cfg = LLMConfig(enabled=enabled, base_url=base_url, api_key=api_key, model=model, timeout=timeout)

    # ---- Export zip ----
    st.sidebar.markdown("---")
    st.sidebar.markdown("#### 导出/打包")
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

def ui_header(prj: Project):
    st.markdown(
        f"""
        <div style="padding: 14px 16px; border-radius: 16px; background: linear-gradient(90deg, #f7f8ff 0%, #f8fbff 100%); border: 1px solid #eef;">
          <div style="font-size: 28px; font-weight: 800;">教学文件工作台</div>
          <div style="margin-top: 6px; color: #666;">项目：<b>{prj.name}</b>（{prj.project_id}） · 最后更新：{prj.updated_at}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write("")

def ui_base_training_plan(pid: str, llm_cfg: LLMConfig):
    st.subheader("培养方案基座（全量内容库）")
    st.caption("建议先把培养方案整理成权威内容库；后续所有教学文件将以此做一致性校验与自动填充。")

    plan = load_base_plan(pid) or {"meta": {}, "sections": {}, "appendices": {}, "course_graph": {"nodes": [], "edges": []}}

    colL, colR = st.columns([1, 2])

    with colL:
        up = st.file_uploader("上传培养方案 PDF（可选）", type=["pdf"])
        if up:
            pdf_bytes = up.read()
            st.info("已读取PDF。你可以点击“抽取并写入基座”。")
            if st.button("抽取并写入基座", type="primary", use_container_width=True):
                extracted = base_plan_minimal_from_pdf(pdf_bytes)
                save_base_plan(pid, extracted)
                st.success("已写入培养方案基座。")
                st.rerun()

        st.write("")
        json_download_button("下载基座JSON", plan, f"base_training_plan-{pid}.json")

        st.write("")
        if st.button("检查：是否缺少关键栏目", use_container_width=True):
            missing = [t for t in SECTION_TITLES[:6] if not clean_text(plan.get("sections", {}).get(t, ""))]
            if missing:
                st.warning("缺少栏目：\n- " + "\n- ".join(missing))
            else:
                st.success("6个核心栏目均已存在（仍建议人工快速扫读）。")

        st.write("")
        st.markdown("##### 关系图（课程逻辑思维导图的最小表达）")
        gtxt = st.text_area(
            "course_graph JSON（nodes/edges）",
            value=json.dumps(plan.get("course_graph", {"nodes": [], "edges": []}), ensure_ascii=False, indent=2),
            height=180,
        )
        gobj = safe_json_load(gtxt, None)
        if isinstance(gobj, dict) and "nodes" in gobj and "edges" in gobj:
            if st.button("保存关系图", use_container_width=True):
                plan["course_graph"] = gobj
                save_base_plan(pid, plan)
                st.success("已保存关系图。")
        else:
            st.warning("格式不正确：需要包含 nodes / edges。")

    with colR:
        st.markdown("##### 核心栏目（可编辑）")
        sections = plan.get("sections", {})
        tabs = st.tabs(SECTION_TITLES[:6])

        for i, title in enumerate(SECTION_TITLES[:6]):
            with tabs[i]:
                sections[title] = st.text_area(title, value=sections.get(title, ""), height=240)

                # 可选：对单栏用 LLM 校对（更强的“纠断行/补编号/修错别字/结构化提炼”）
                if llm_available(llm_cfg):
                    if st.button(f"用 LLM 校对该栏目：{title}", key=f"llm_fix_{i}"):
                        system = "你是高校培养方案的严谨审校助手。对给定栏目做纠错、断行修复、编号修复；尽量不改原意。只输出JSON。"
                        schema_hint = json.dumps({
                            "title": title,
                            "corrected_text": "纠错后的完整栏目文本（保持原意，修正常见断行/丢字/编号）",
                            "key_points": ["可选：要点列表"],
                            "warnings": ["可选：缺失/疑点提示"],
                        }, ensure_ascii=False, indent=2)
                        user = f"栏目标题：{title}\n\n原文：\n{sections[title]}\n"
                        obj, raw = llm_chat_json(llm_cfg, system, user, schema_hint=schema_hint)
                        if obj and obj.get("corrected_text"):
                            sections[title] = obj["corrected_text"]
                            plan["sections"] = sections
                            plan.setdefault("llm_log", []).append({"at": now_str(), "title": title, "raw": raw})
                            save_base_plan(pid, plan)
                            st.success("已应用LLM修正。")
                            st.rerun()
                        else:
                            st.error("LLM未返回可用JSON。")
                            st.code(raw)

        st.write("")
        if st.button("保存基座（手工编辑）", type="primary", use_container_width=True):
            plan["sections"] = sections
            plan.setdefault("meta", {})["updated_at"] = now_str()
            save_base_plan(pid, plan)
            st.success("已保存。")

def ui_templates(pid: str, llm_cfg: LLMConfig):
    st.subheader("模板化教学文件（上传/粘贴 → 抽取填充 → 校对 → 导出）")
    st.caption("你提出的方案：把易模式化文件做成固定模板；支持上传现有文档后抽取填充，人工确认后导出规范文档，并项目化保存/打包。")

    colL, colR = st.columns([1.1, 1.9])

    with colL:
        st.markdown("##### 新建文档")
        ttype = st.selectbox("模板类型", TEMPLATE_TYPES)
        title = st.text_input("文档标题（项目内）", value="")
        if st.button("新建文档", type="primary", use_container_width=True):
            doc_obj = new_doc_object(ttype, title=title)
            doc_obj["data"] = schema_for(ttype)
            save_doc(pid, doc_obj)
            st.session_state["active_doc_id"] = doc_obj["doc_id"]
            st.success("已新建。")
            st.rerun()

        st.write("")
        st.markdown("##### 导入已有内容")
        up = st.file_uploader("上传 docx（推荐）", type=["docx"])
        pasted = st.text_area("或粘贴全文（可选）", height=120)

        if st.button("抽取并填充到当前模板", use_container_width=True):
            doc_id = st.session_state.get("active_doc_id")
            if not doc_id:
                st.error("请先新建一个文档。")
            else:
                doc_obj = safe_json_load(doc_path(pid, doc_id).read_text("utf-8"), {})
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
            choice = st.selectbox("选择文档", opts, index=idx)
            st.session_state["active_doc_id"] = choice.split("(")[-1].strip(")")

            c1, c2 = st.columns(2)
            with c1:
                if st.button("删除该文档", use_container_width=True):
                    delete_doc(pid, st.session_state["active_doc_id"])
                    st.session_state["active_doc_id"] = None
                    st.success("已删除。")
                    st.rerun()
            with c2:
                if st.button("下载该文档JSON", use_container_width=True):
                    doc_obj = safe_json_load(doc_path(pid, st.session_state["active_doc_id"]).read_text("utf-8"), {})
                    json_download_button("下载JSON", doc_obj, f"{doc_obj['title']}-{doc_obj['doc_id']}.json")

    with colR:
        doc_id = st.session_state.get("active_doc_id")
        if not doc_id:
            st.info("请在左侧新建/选择一个文档。")
            return

        doc_obj = safe_json_load(doc_path(pid, doc_id).read_text("utf-8"), {})
        if not doc_obj:
            st.warning("文档不存在。")
            return

        st.markdown(f"##### 编辑：{doc_obj['title']} · {doc_obj['template_type']}")
        st.caption(f"更新时间：{doc_obj.get('updated_at','')} · 来源：{doc_obj.get('source',{}).get('uploaded_filename','(无)')}")

        # 可选：LLM结构化重建
        if llm_available(llm_cfg):
            with st.expander("LLM：结构化重建 / 校对（可选）", expanded=False):
                extra = st.text_area("额外要求（可选）", value="尽量保留原意；修复断行；字段找不到就留空并给warnings。", height=80)
                if st.button("用LLM重建结构化数据", type="primary"):
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

        # editor
        edited = ui_render_editor(doc_obj["template_type"], doc_obj.get("data", {}))

        # consistency checks vs base plan
        plan = load_base_plan(pid)
        warnings = run_consistency_checks(doc_obj["template_type"], edited, plan) if plan else []
        if warnings:
            with st.expander("一致性检查提示", expanded=True):
                for w in warnings:
                    st.warning(w)

        # Save / Export
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("保存", type="primary", use_container_width=True):
                doc_obj["data"] = edited
                save_doc(pid, doc_obj)
                st.success("已保存。")
        with c2:
            if st.button("回滚到上一版", use_container_width=True):
                if doc_obj.get("history"):
                    last = doc_obj["history"].pop()
                    doc_obj["data"] = last.get("data", doc_obj["data"])
                    save_doc(pid, doc_obj)
                    st.success("已回滚。")
                    st.rerun()
                else:
                    st.info("没有历史记录。")
        with c3:
            json_download_button("下载JSON", doc_obj, f"{doc_obj['title']}-{doc_obj['doc_id']}.json")

        st.write("")
        c4, c5 = st.columns(2)
        with c4:
            if st.button("导出 DOCX", use_container_width=True):
                b = export_docx_for_template(doc_obj["template_type"], edited, doc_obj["title"])
                st.download_button(
                    "下载 DOCX",
                    data=b,
                    file_name=f"{doc_obj['title']}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    use_container_width=True,
                )
        with c5:
            if st.button("导出 XLSX（表格）", use_container_width=True):
                x = export_xlsx_for_template(doc_obj["template_type"], edited)
                if x:
                    st.download_button(
                        "下载 XLSX",
                        data=x,
                        file_name=f"{doc_obj['title']}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
                else:
                    st.info("该模板无可导出表格。")

        with st.expander("原始抽取（文本/表格）", expanded=False):
            st.text_area("raw_text", value=clamp(doc_obj.get("raw", {}).get("text", "")), height=220)
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

    tabs = st.tabs(["培养方案基座", "模板化教学文件", "项目概览"])
    with tabs[0]:
        ui_base_training_plan(prj.project_id, llm_cfg)
    with tabs[1]:
        ui_templates(prj.project_id, llm_cfg)
    with tabs[2]:
        st.subheader("项目概览")
        plan = load_base_plan(prj.project_id)
        docs = list_docs(prj.project_id)

        st.write({"project_id": prj.project_id, "name": prj.name, "created_at": prj.created_at, "updated_at": prj.updated_at})
        st.write("")
        st.markdown("##### 基座状态")
        secs = plan.get("sections", {})
        st.write(f"核心栏目：{sum(1 for t in SECTION_TITLES[:6] if clean_text(secs.get(t,'')))} / 6")
        st.write(f"关系图节点：{len(plan.get('course_graph',{}).get('nodes',[]))} · 边：{len(plan.get('course_graph',{}).get('edges',[]))}")

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
        if st.button("下载项目zip（JSON+导出）", type="primary"):
            z = export_project_zip(prj.project_id)
            st.download_button("下载 zip", data=z, file_name=f"{prj.name}-{prj.project_id}.zip", mime="application/zip")

if __name__ == "__main__":
    main()
