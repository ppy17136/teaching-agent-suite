# -*- coding: utf-8 -*-
"""
教学智能体平台 - 整合PDF全量抽取版（增强版）
整合了完整的PDF解析能力和教学文档链管理，确保对培养方案PDF的所有章节和表格完整提取和显示。
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
from dataclasses import asdict, dataclass


# -------- 可选解析依赖 --------
try:
    import pdfplumber
except Exception:
    pdfplumber = None
    st.error("缺少依赖 pdfplumber，请安装：pip install pdfplumber")

try:
    from docx import Document
except Exception:
    Document = None

# ---------------------------
# 基础配置
# ---------------------------
st.set_page_config(page_title="教学智能体平台", layout="wide")

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

/* 确保表格列名有效 */
.stDataFrame th {
    font-weight: 600 !important;
}
</style>
""",
        unsafe_allow_html=True,
    )

inject_css()

# ---------------------------
# PDF全量抽取核心功能（增强版：确保所有章节和表格完整提取）
# ---------------------------

def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def safe_strip(x):
    if x is None:
        return ""
    return str(x).strip()


def normalize_table(raw_table):
    """
    pdfplumber.extract_tables() 返回 list[list[str|None]]
    这里做基础清洗：去空行、补齐列数、去掉全空列
    """
    if not raw_table:
        return None

    rows = []
    max_cols = 0
    for r in raw_table:
        if r is None:
            continue
        rr = [safe_strip(c) for c in r]
        # 跳过全空行
        if all(c == "" for c in rr):
            continue
        rows.append(rr)
        max_cols = max(max_cols, len(rr))

    if not rows or max_cols == 0:
        return None

    # 补齐列数
    for i in range(len(rows)):
        if len(rows[i]) < max_cols:
            rows[i] = rows[i] + [""] * (max_cols - len(rows[i]))

    # 去掉全空列
    keep_cols = []
    for j in range(max_cols):
        col = [rows[i][j] for i in range(len(rows))]
        if any(c != "" for c in col):
            keep_cols.append(j)

    if not keep_cols:
        return None

    cleaned = [[row[j] for j in keep_cols] for row in rows]
    return cleaned


def table_to_df(cleaned_table):
    """
    尝试把第一行当表头；如果表头太差就用默认列名。
    """
    if not cleaned_table or len(cleaned_table) == 0:
        return None
    if len(cleaned_table) == 1:
        # 只有一行，做单行df
        return pd.DataFrame([cleaned_table[0]])

    header = cleaned_table[0]
    body = cleaned_table[1:]

    # 表头判定：至少有一半单元格非空
    non_empty = sum(1 for x in header if safe_strip(x) != "")
    if non_empty >= max(1, len(header) // 2):
        cols = [h if h else f"col_{i+1}" for i, h in enumerate(header)]
        return pd.DataFrame(body, columns=cols)

    # 否则不用表头
    return pd.DataFrame(cleaned_table)


def try_ocr_page(plumber_page) -> str:
    """
    可选OCR：仅在 pytesseract 存在且系统有 tesseract 时可用。
    不满足条件则返回空串，不抛异常。
    """
    try:
        import pytesseract  # noqa
        from PIL import Image  # noqa
    except Exception:
        return ""

    try:
        img = plumber_page.to_image(resolution=220).original
        # pytesseract 对中文需要 chi_sim；若环境没装中文语言包也可能效果一般
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception:
        return ""


# ----------------------------
# core: extract
# ----------------------------
@st.cache_data(show_spinner=False)
def extract_pdf_all(pdf_bytes: bytes, enable_ocr: bool = False):
    if pdfplumber is None:
        raise RuntimeError("缺少依赖 pdfplumber，请先在 requirements.txt 安装：pdfplumber")

    meta = {
        "sha256": sha256_bytes(pdf_bytes),
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "ocr_enabled": bool(enable_ocr),
    }

    pages = []
    total_tables = 0

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        meta["n_pages"] = len(pdf.pages)

        # table settings：偏“宽松”，提升跨页/复杂表格提取成功率
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
            text = page.extract_text() or ""
            text = text.strip()

            # 如果这一页几乎没字且用户勾选OCR，就尝试OCR补救
            if enable_ocr and len(text) < 20:
                ocr_text = try_ocr_page(page)
                if len(ocr_text) > len(text):
                    text = ocr_text

            raw_tables = []
            try:
                raw_tables = page.extract_tables(table_settings=table_settings) or []
            except Exception:
                raw_tables = []

            cleaned_tables = []
            for t in raw_tables:
                ct = normalize_table(t)
                if ct:
                    cleaned_tables.append(ct)

            total_tables += len(cleaned_tables)

            pages.append(
                {
                    "page": idx,
                    "text": text,
                    "tables": cleaned_tables,  # list of list-of-rows
                }
            )

    meta["n_tables"] = total_tables
    return {"meta": meta, "pages": pages}


# ----------------------------
# core: structure parse
# ----------------------------
def join_all_text(pages):
    chunks = []
    for p in pages:
        t = safe_strip(p.get("text", ""))
        if t:
            chunks.append(f"[PAGE {p['page']}]\n{t}")
    return "\n\n".join(chunks)


def extract_section(full_text: str, start_keywords, end_keywords):
    """
    从全文中按“起止关键词”粗切片（适用于培养目标、毕业要求等）
    """
    start_pat = "|".join(map(re.escape, start_keywords))
    end_pat = "|".join(map(re.escape, end_keywords))

    m = re.search(rf"({start_pat})", full_text)
    if not m:
        return ""

    start = m.start()
    tail = full_text[start:]

    m2 = re.search(rf"({end_pat})", tail[10:])  # 略过开头10字符，避免同词误触
    if not m2:
        return tail.strip()

    end = 10 + m2.start()
    return tail[:end].strip()


def parse_objectives(section_text: str):
    """
    培养目标常见格式：(1)(2)... 或 1. 2. / 1、2、
    """
    if not section_text:
        return []

    # 去掉页码标记
    txt = re.sub(r"\[PAGE\s+\d+\]", "", section_text)

    # 先抓 (1) (2)...
    items = re.split(r"\(\s*\d+\s*\)", txt)
    items = [i.strip() for i in items if i.strip()]
    if len(items) >= 2:
        return items

    # 再抓 1. / 1、 2.
    parts = re.split(r"(?m)^\s*\d+\s*[\.、]\s*", txt)
    parts = [p.strip() for p in parts if p.strip()]
    # 过滤掉明显是标题/过短
    parts = [p for p in parts if len(p) >= 10]
    return parts


def parse_graduation_requirements(section_text: str):
    """
    目标：尽量整理出 1-12 条毕业要求
    """
    if not section_text:
        return {}

    txt = re.sub(r"\[PAGE\s+\d+\]", "", section_text)
    txt = txt.replace("：", ":")
    # 常见： "毕业要求1" / "毕业要求 1" / "1." / "1、"
    # 先统一把“毕业要求X”变成换行 + X.
    txt = re.sub(r"毕业要求\s*([1-9]|1[0-2])\s*", r"\n\1. ", txt)

    # 用行首数字切
    chunks = re.split(r"(?m)^\s*([1-9]|1[0-2])\s*[\.、]\s*", txt)
    # re.split 会得到： [pre, num1, text1, num2, text2,...]
    req = {}
    if len(chunks) >= 3:
        pre = chunks[0].strip()
        it = chunks[1:]
        for i in range(0, len(it) - 1, 2):
            num = int(it[i])
            content = it[i + 1].strip()
            # 截断到下一个大标题前的残留（经验性）
            content = re.split(r"\n\s*(课程体系|课程设置|课程结构|课程一览|学分|附表)", content)[0].strip()
            if content:
                req[num] = content

    # 若仍不够，尝试再从文本中找“X）/X)” 形式
    if len(req) < 10:
        alt = re.split(r"(?m)^\s*([1-9]|1[0-2])\s*[\)）]\s*", txt)
        if len(alt) >= 3:
            it = alt[1:]
            for i in range(0, len(it) - 1, 2):
                num = int(it[i])
                content = it[i + 1].strip()
                content = re.split(r"\n\s*(课程体系|课程设置|课程结构|课程一览|学分|附表)", content)[0].strip()
                if content and num not in req:
                    req[num] = content

    # 保序输出
    return dict(sorted(req.items(), key=lambda x: x[0]))


def collect_course_tables(pages):
    """
    从所有表格里找“像课程表”的表：包含关键词（课程/学分/学时/性质/类别等）
    并将同类表尽量合并。
    """
    dfs = []
    for p in pages:
        for t in p.get("tables", []):
            df = table_to_df(t)
            if df is None or df.empty:
                continue
            # 判断是否像课程表
            flat = " ".join([safe_strip(c) for c in df.columns]) + " " + " ".join(
                safe_strip(x) for x in df.head(3).astype(str).values.flatten().tolist()
            )
            key_hits = sum(
                1
                for kw in ["课程", "学分", "学时", "性质", "类别", "必修", "选修", "开课", "理论", "实践", "周学时"]
                if kw in flat
            )
            if key_hits >= 2:
                df2 = df.copy()
                df2.insert(0, "__page__", p["page"])
                dfs.append(df2)

    if not dfs:
        return None

    # 简单合并：按列名完全一致优先concat；否则直接返回列表
    # 这里保守处理，避免强行对齐导致错位
    groups = {}
    for df in dfs:
        sig = tuple(df.columns.tolist())
        groups.setdefault(sig, []).append(df)

    merged = []
    for sig, group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            merged.append(pd.concat(group, ignore_index=True))

    return merged


def parse_structured(extracted):
    pages = extracted["pages"]
    full_text = join_all_text(pages)

    # 根据常见培养方案结构做粗切
    objectives_text = extract_section(
        full_text,
        start_keywords=["培养目标", "一、培养目标", "（一）培养目标"],
        end_keywords=["毕业要求", "二、毕业要求", "（二）毕业要求", "课程体系", "课程设置", "课程结构"],
    )

    gradreq_text = extract_section(
        full_text,
        start_keywords=["毕业要求", "二、毕业要求", "（二）毕业要求"],
        end_keywords=["课程体系", "课程设置", "课程结构", "课程一览", "课程表", "学分要求", "附表"],
    )

    objectives = parse_objectives(objectives_text)
    gradreq = parse_graduation_requirements(gradreq_text)

    course_tables = collect_course_tables(pages)

    structured = {
        "objectives": objectives,
        "graduation_requirements": gradreq,  # dict {1: "...", ..., 12:"..."}
        "course_tables_count": 0 if not course_tables else len(course_tables),
    }

    return structured, full_text, course_tables


# ----------------------------
# export builders
# ----------------------------
def build_json_bytes(extracted, structured, full_text):
    pack = {
        "meta": extracted["meta"],
        "structured": structured,
        "full_text": full_text,
        "pages": extracted["pages"],
    }
    return json.dumps(pack, ensure_ascii=False, indent=2).encode("utf-8")


def build_csv_zip_bytes(extracted, course_tables):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # pages text
        rows = []
        for p in extracted["pages"]:
            rows.append({"page": p["page"], "text": p.get("text", "")})
        df_pages = pd.DataFrame(rows)
        z.writestr("pages_text.csv", df_pages.to_csv(index=False, encoding="utf-8-sig"))

        # all tables as csv (逐表输出)
        t_index = 0
        for p in extracted["pages"]:
            for t in p.get("tables", []):
                t_index += 1
                df = table_to_df(t)
                if df is None:
                    continue
                name = f"tables/page_{p['page']}_table_{t_index}.csv"
                z.writestr(name, df.to_csv(index=False, encoding="utf-8-sig"))

        # course tables merged
        if course_tables:
            for i, df in enumerate(course_tables, start=1):
                z.writestr(f"course_tables_merged_{i}.csv", df.to_csv(index=False, encoding="utf-8-sig"))

    return buf.getvalue()


def build_excel_bytes(extracted, structured, course_tables):
    """
    尝试导出 xlsx：
    - 若 openpyxl 或 xlsxwriter 存在则可用
    - 两者都不存在则返回 None
    """
    engine = None
    try:
        import openpyxl  # noqa
        engine = "openpyxl"
    except Exception:
        pass

    if engine is None:
        try:
            import xlsxwriter  # noqa
            engine = "xlsxwriter"
        except Exception:
            pass

    if engine is None:
        return None, "未检测到 openpyxl/xlsxwriter，无法导出xlsx（已提供JSON/CSV导出）。"

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine=engine) as writer:
        # meta
        meta_df = pd.DataFrame([extracted["meta"]])
        meta_df.to_excel(writer, index=False, sheet_name="meta")

        # objectives
        obj_df = pd.DataFrame(
            [{"idx": i + 1, "培养目标": t} for i, t in enumerate(structured.get("objectives", []))]
        )
        obj_df.to_excel(writer, index=False, sheet_name="培养目标")

        # graduation requirements
        gr = structured.get("graduation_requirements", {})
        gr_df = pd.DataFrame([{"编号": k, "毕业要求": v} for k, v in gr.items()])
        gr_df.to_excel(writer, index=False, sheet_name="毕业要求")

        # pages text (长文本放一列)
        pages_df = pd.DataFrame([{"page": p["page"], "text": p.get("text", "")} for p in extracted["pages"]])
        pages_df.to_excel(writer, index=False, sheet_name="pages_text")

        # course tables merged
        if course_tables:
            for i, df in enumerate(course_tables, start=1):
                sheet = f"课程表合并_{i}"
                # sheet名最长31字符
                sheet = sheet[:31]
                df.to_excel(writer, index=False, sheet_name=sheet)

    return output.getvalue(), f"已使用 {engine} 导出xlsx。"



# ---------------------------
# PDF全量抽取核心功能（增强版：确保所有章节和表格完整提取）
# ---------------------------
def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()

def clean_text(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def normalize_multiline(text: str) -> str:
    """保留换行，做基础清理，便于正则分段。"""
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
    """表格后处理：去空白、去 NaN、合并格造成的空白做向下填充。"""
    if df is None or df.empty:
        return df

    df = df.copy()
    df = df.replace({None: ""}).fillna("")
    for c in df.columns:
        df[c] = df[c].astype(str).map(lambda x: clean_text(x))

    # 1) 删除完全空行
    mask_all_empty = df.apply(lambda r: all((clean_text(x) == "" for x in r.values.tolist())), axis=1)
    df = df.loc[~mask_all_empty].reset_index(drop=True)

    # 2) 向下填充（合并格常见列）
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

def normalize_table(raw_table: List[List[Any]]) -> List[List[str]]:
    """
    pdfplumber.extract_tables() 返回 list[list[str|None]]
    这里做基础清洗：去空行、补齐列数、去掉全空列
    """
    if not raw_table:
        return []

    rows = []
    max_cols = 0
    for r in raw_table:
        if r is None:
            continue
        rr = [clean_text(c) for c in r]
        # 跳过全空行
        if all(c == "" for c in rr):
            continue
        rows.append(rr)
        max_cols = max(max_cols, len(rr))

    if not rows or max_cols == 0:
        return []

    # 补齐列数
    for i in range(len(rows)):
        if len(rows[i]) < max_cols:
            rows[i] = rows[i] + [""] * (max_cols - len(rows[i]))

    # 去掉全空列
    keep_cols = []
    for j in range(max_cols):
        col = [rows[i][j] for i in range(len(rows))]
        if any(c != "" for c in col):
            keep_cols.append(j)

    if not keep_cols:
        return []

    cleaned = [[row[j] for j in keep_cols] for row in rows]
    return cleaned

def table_to_df(cleaned_table: List[List[str]]) -> pd.DataFrame:
    """
    尝试把第一行当表头；如果表头太差就用默认列名。
    """
    if not cleaned_table or len(cleaned_table) == 0:
        return pd.DataFrame()
    
    if len(cleaned_table) == 1:
        # 只有一行，做单行df
        return pd.DataFrame([cleaned_table[0]])

    header = cleaned_table[0]
    body = cleaned_table[1:]

    # 表头判定：至少有一半单元格非空
    non_empty = sum(1 for x in header if clean_text(x) != "")
    if non_empty >= max(1, len(header) // 2):
        cols = [h if h else f"col_{i+1}" for i, h in enumerate(header)]
        df = pd.DataFrame(body, columns=cols)
    else:
        # 否则不用表头
        df = pd.DataFrame(cleaned_table)

    return postprocess_table_df(df)



# ----------------------------
# PDF 抽取：文本 + 表格 (使用 pdfplumber 的表格提取，增强设置以捕获所有表格)
# ----------------------------
def extract_pages_text_and_tables(pdf_bytes: bytes, enable_ocr: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    """
    提取每页的文本和表格，确保所有页面的内容完整提取
    返回：页面数据列表（含文本和表格），全文文本
    """
    if pdfplumber is None:
        return [], ""
    
    pages_data = []
    full_text_parts = []
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # 表格设置：更宽松设置，确保捕获复杂表格，包括跨页表格
        table_settings = {
            "vertical_strategy": "lines_strict",
            "horizontal_strategy": "lines_strict",
            "intersection_tolerance": 10,
            "snap_tolerance": 5,
            "join_tolerance": 5,
            "edge_min_length": 2,
            "min_words_vertical": 1,
            "min_words_horizontal": 1,
            "text_tolerance": 3,
            "keep_blank_chars": True,
            "text_x_tolerance": 2,
            "text_y_tolerance": 2
        }
        
        for idx, page in enumerate(pdf.pages, start=1):
            # 提取文本（增强：使用layout模式以保留结构）
            text = page.extract_text(layout=True) or ""
            text = normalize_multiline(text)
            
            # 如果需要OCR且文本太少
            if enable_ocr and len(text) < 50:
                try:
                    import pytesseract
                    from PIL import Image
                    img = page.to_image(resolution=300).original  # 提高分辨率
                    ocr_text = pytesseract.image_to_string(img, lang="chi_sim+eng")
                    if len(ocr_text) > len(text):
                        text = normalize_multiline(ocr_text)
                except Exception:
                    pass
            
            full_text_parts.append(text)
            
            # 提取表格（增强：尝试多次设置以捕获所有）
            raw_tables = []
            try:
                raw_tables = page.extract_tables(table_settings=table_settings) or []
            except Exception:
                # 备用设置
                alt_settings = {"vertical_strategy": "text", "horizontal_strategy": "text"}
                raw_tables = page.extract_tables(table_settings=alt_settings) or []
            
            # 清洗表格
            cleaned_tables = []
            for t in raw_tables:
                ct = normalize_table(t)
                if ct:
                    cleaned_tables.append(ct)
            
            pages_data.append({
                "page": idx,
                "text": text,
                "tables": cleaned_tables,
                "tables_count": len(cleaned_tables)
            })
    
    full_text = "\n".join(full_text_parts)
    return pages_data, full_text

# ----------------------------
# 结构化解析：章节/毕业要求/培养目标/附表标题（增强：捕获所有章节，包括七到十一）
# ----------------------------
def split_sections(full_text: str) -> Dict[str, str]:
    """
    按 "一、/二、/三、..." 大章切分，确保捕获所有，包括七、八、九、十、十一等。
    兼容：三、 / 三. / 三． / 十一、
    """
    text = normalize_multiline(full_text)
    lines = text.splitlines()
    pat = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、\.．]\s*([^\n\r]+?)\s*$")  # 支持十一等

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
    """抽取"附表X -> 标题（可能含七、八…）"""
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
    """
    提取"培养目标"条目。返回 items(list[str]) + raw。
    尽量包容：1) / 1． / 1、 / （1）等。
    """
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

    # 如果没抓到编号条目，退化：取前若干行（不丢信息）
    if not items:
        items = lines[:30]

    return {"count": len(items), "items": items, "raw": raw}

def parse_graduation_requirements(text_any: str) -> Dict[str, Any]:
    """
    抽取 12 条毕业要求及其分项 1.1/1.2…
    返回结构：{"count":..,"items":[{"no":1,"title":"工程知识","body":"...","subitems":[...]}], "raw":...}
    """
    text = normalize_multiline(text_any or "")

    # 定位"二、毕业要求"
    start = re.search(r"(?m)^\s*(二\s*[、\.．]?\s*毕业要求|毕业要求)\s*$", text)
    if start:
        tail = text[start.start():]
    else:
        tail = text

    # 截断到下一大章
    end = re.search(r"(?m)^\s*[三四五六七八九十十一十二]\s*[、\.．]", tail)  # 扩展到十二
    if end:
        tail = tail[:end.start()]

    lines = [ln.strip() for ln in tail.splitlines()]

    main_pat = re.compile(r"^(?P<no>\d{1,2})\s*[\.、](?!\d)\s*(?P<body>.+)$")   # 1. xxx (排除 1.1)
    sub_pat = re.compile(r"^(?P<no>\d{1,2}\.\d{1,2})\s+(?P<body>.+)$")       # 1.1 xxx

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


# ----------------------------
# 表格标题/方向识别（增强：扩展页映射到18页，确保所有附表）
# ----------------------------
def guess_table_appendix_by_page(page_no: int) -> Optional[str]:
    """
    针对常见培养方案（18 页）：扩展映射到所有可能附表
    """
    mapping = {
        10: "附表1", 11: "附表1",
        12: "附表2",
        13: "附表3", 14: "附表3",
        15: "附表4",
        16: "附表5",
        17: "附表5",  # 扩展
        18: "附表5"   # 扩展
    }
    return mapping.get(page_no)

def infer_table_title_from_page_text(page_text: str, appendix: Optional[str], appendix_titles: Dict[str, str], page_no: int) -> str:
    if appendix and appendix in appendix_titles:
        return appendix_titles[appendix]

    if appendix:
        m = re.search(rf"(?P<title>[^\n\r]{{2,120}}?)\s*[（(]\s*{re.escape(appendix)}\s*[)）]", page_text)
        if m:
            return clean_text(m.group("title"))

    m = re.search(r"(附表\s*\d+)\s*[:：]\s*([^\n\r]{2,120})", page_text)
    if m:
        return clean_text(m.group(2))

    # 增强：从页面文本推断标题
    title_pat = re.search(r"^(七|八|九|十|十一|十二)\s*[、\.．]\s*(.+)$", page_text, re.MULTILINE)
    if title_pat:
        return clean_text(title_pat.group(2))

    return appendix or f"第{page_no}页表格"

def infer_direction_for_page(page_text: str) -> str:
    has_weld = "焊接" in page_text
    has_ndt = ("无损" in page_text) or ("无损检测" in page_text)
    if has_weld and has_ndt:
        return "混合（焊接+无损检测）"
    if has_weld:
        return "焊接"
    if has_ndt:
        return "无损检测"
    return ""

def add_direction_column_rowwise(df: pd.DataFrame, page_direction: str) -> pd.DataFrame:
    """
    行级方向识别：若表内有"焊接方向/无损检测方向"分隔行，则从该行开始向下标注。
    若识别不到，则使用 page_direction。
    """
    if df is None or df.empty:
        return df

    df = df.copy()
    cur_dir = ""
    dirs = []
    for _, row in df.iterrows():
        row_txt = " ".join([clean_text(x) for x in row.values.tolist()])
        if re.search(r"焊接.*方向", row_txt):
            cur_dir = "焊接"
        elif re.search(r"无损.*方向", row_txt) or re.search(r"无损检测.*方向", row_txt):
            cur_dir = "无损检测"

        dirs.append(cur_dir or page_direction)

    # 插到最前
    if "专业方向" not in df.columns:
        df.insert(0, "专业方向", dirs)
    else:
        df["专业方向"] = [d or page_direction for d in dirs]

    return df



# ----------------------------
# 输出结构
# ----------------------------
@dataclass
class TablePack:
    page: int
    title: str
    appendix: str
    direction: str
    columns: List[str]
    rows: List[List[Any]]

@dataclass
class ExtractResult:
    page_count: int
    table_count: int
    ocr_used: bool
    file_sha256: str
    extracted_at: str
    pages_data: List[Dict[str, Any]]
    sections: Dict[str, str]
    appendix_titles: Dict[str, str]
    training_objectives: Dict[str, Any]
    graduation_requirements: Dict[str, Any]
    tables: List[Dict[str, Any]]  # TablePack as dict

# ----------------------------
# 主流程（增强：处理所有页，确保无遗漏）
# ----------------------------
def run_full_extract(pdf_bytes: bytes, use_ocr: bool = False) -> ExtractResult:
    # 1) 提取页面文本和表格
    pages_data, full_text = extract_pages_text_and_tables(pdf_bytes, enable_ocr=use_ocr)
    
    # 2) 结构化解析
    sections = split_sections(full_text)
    appendix_titles = extract_appendix_titles(full_text)
    
    # 3) 关键结构化：培养目标、毕业要求
    obj_key = next((k for k in sections.keys() if "培养目标" in k), "")
    obj = parse_training_objectives(sections.get(obj_key, "") or full_text)
    grad = parse_graduation_requirements(full_text)
    
    # 4) 处理表格（增强：遍历所有页，确保所有附表）
    tables: List[TablePack] = []
    total_tables = 0
    
    for page_data in pages_data:
        page_no = page_data["page"]
        page_text = page_data["text"]
        page_tables = page_data["tables"]
        
        total_tables += len(page_tables)
        
        appendix = guess_table_appendix_by_page(page_no) or ""
        base_title = infer_table_title_from_page_text(page_text, appendix or None, appendix_titles, page_no)
        title = f"{base_title}（{appendix}）" if appendix and appendix not in base_title else base_title
        page_dir = infer_direction_for_page(page_text)
        
        for i, table_data in enumerate(page_tables):
            df = table_to_df(table_data)
            if df is not None and not df.empty:
                df2 = add_direction_column_rowwise(df, page_dir)
                sub_title = title if len(page_tables) == 1 else f"{title} - 表{i+1}"
                pack = TablePack(
                    page=page_no,
                    title=sub_title,
                    appendix=appendix,
                    direction=page_dir,
                    columns=[str(c) for c in df2.columns],
                    rows=df2.values.tolist(),
                )
                tables.append(pack)
    
    result = ExtractResult(
        page_count=len(pages_data),
        table_count=total_tables,
        ocr_used=use_ocr,
        file_sha256=sha256_bytes(pdf_bytes),
        extracted_at=datetime.now().isoformat(timespec="seconds"),
        pages_data=pages_data,
        sections=sections,
        appendix_titles=appendix_titles,
        training_objectives=obj,
        graduation_requirements=grad,
        tables=[asdict(t) for t in tables],
    )
    return result




# ----------------------------
# 导出功能
# ----------------------------
def safe_df_from_tablepack(t: Dict[str, Any]) -> pd.DataFrame:
    """从 TablePack 字典创建 DataFrame"""
    cols = t.get("columns") or []
    rows = t.get("rows") or []
    
    if rows and len(rows) > 0:
        df = pd.DataFrame(rows, columns=cols)
        return postprocess_table_df(df)
    return pd.DataFrame()

def make_tables_zip(tables: List[Dict[str, Any]]) -> bytes:
    """CSV + tables.json 打包"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("tables.json", json.dumps(tables, ensure_ascii=False, indent=2))
        for idx, t in enumerate(tables, start=1):
            title = clean_text(t.get("title") or f"table_{idx}")
            title_safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "_", title)[:80].strip("_") or f"table_{idx}"

            df = safe_df_from_tablepack(t)

            # 方向列
            direction = clean_text(t.get("direction") or "")
            if direction and "专业方向" not in df.columns:
                df.insert(0, "专业方向", direction)

            csv_bytes = df.to_csv(index=False, encoding="utf-8-sig")
            zf.writestr(f"{idx:02d}_{title_safe}.csv", csv_bytes)
    return buf.getvalue()

# ----------------------------
# 数据库函数（从框架脚本保留）
# ----------------------------
# ... (此处省略框架中的数据库函数，如init_db, get_projects等，因为长度限制。假设它们保持不变。)

# ----------------------------
# Streamlit UI（增强：在培养方案页面显示所有章节和表格）
# ----------------------------
# ... (此处省略框架中的其他页面函数，只聚焦page_training_plan的增强)



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
    <div class="title">教学智能体平台 - PDF全量抽取版</div>
    <div class="sub">培养方案PDF全量抽取（文本+表格+结构）→ 大纲 → 日历 → 教案 → 试卷/审核 → 达成报告 → 授课手册</div>
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
        st.info("当前项目还没有任何文档。建议先从'培养方案（底座）'开始。")
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
    render_depbar(project_id, "training_plan")
    tp = get_artifact(project_id, "training_plan")
    
    st.markdown("### 培养方案底座（training_plan）")
    st.caption("培养方案是教学文档链的起点，需上传PDF并确认提取结果，或直接编辑。")
    
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["PDF上传/抽取/确认", "查看当前", "编辑", "版本", "PDF全量抽取独立界面（增强）"])
    
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
                    "extract_result": asdict(extract_result),
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
                major2 = st.text_input("专业", 
                                      value=extract_result.get("major_guess", "") or "材料成型及控制工程", 
                                      key="tp_major_fix")
                grade2 = st.text_input("年级", 
                                      value=extract_result.get("grade_guess", "") or "22", 
                                      key="tp_grade_fix")
            with colB:
                course_group2 = st.text_input("课程体系/方向", 
                                             value=extract_result.get("course_group_guess", "") or "材料成型方向", 
                                             key="tp_group_fix")
                confirmed_flag = st.checkbox("我已确认以上信息大体正确", value=False, key="tp_confirm_flag")
            with colC:
                st.metric("总页数", extract_result.get("page_count", 0))
                st.metric("表格总数", extract_result.get("table_count", 0))
            
            st.markdown("#### 1) 培养目标（可编辑）")
            goals = extract_result.get("training_objectives", {}).get("items", [])
            goals_text = st.text_area(
                "每行一个目标（可增删/改写）",
                value="\n".join(goals) if goals else "",
                height=140,
                key="tp_goals_edit",
            )
            goals_final = [x.strip() for x in goals_text.splitlines() if x.strip()]
            
            st.markdown("#### 2) 毕业要求（可编辑）")
            grad_items = extract_result.get("graduation_requirements", {}).get("items", [])
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
                            "title": str(row["标题"]).strip(),
                            "body": str(row["内容"]).strip()
                        })
            else:
                st.info("未识别到毕业要求，请手工录入")
                grad_json = st.text_area(
                    "毕业要求 JSON",
                    value=json.dumps([{"no": "1", "title": "工程知识", "body": ""}], ensure_ascii=False, indent=2),
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
                    st.markdown(f"**表格{i+1}（第{table_info.get('page', '?')}页）**")
                    
                    # 确保DataFrame有正确的列名
                    try:
                        df = safe_df_from_tablepack(table_info)
                        if not df.empty:
                            # 使用st.data_editor
                            df_edited = st.data_editor(df, use_container_width=True, key=f"tp_table_{i}")
                            
                            confirm_table = st.checkbox(f"确认采用此表格", value=True, key=f"tp_table_confirm_{i}")
                            if confirm_table:
                                confirmed_tables.append({
                                    "page": table_info.get("page", 0),
                                    "title": table_info.get("title", ""),
                                    "data": df_edited.values.tolist(),
                                    "columns": df_edited.columns.tolist()
                                })
                    except Exception as e:
                        st.error(f"表格{i+1}显示错误: {str(e)}")
                        # 显示原始数据
                        st.write("原始数据:", table_info.get("data", []))
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
                        "page_count": extract_result.get("page_count", 0),
                        "table_count": extract_result.get("table_count", 0),
                        "sections_count": len(sections),
                        "extracted_at": extract_result.get("extracted_at", "")
                    },
                    "full_extract": extract_result  # 保存完整的抽取结果
                }
                
                # 生成markdown
                md = f"# 培养方案（PDF抽取-已确认）\n\n"
                md += f"- 专业：{major2}\n- 年级：{grade2}\n- 课程体系/方向：{course_group2}\n\n"
                md += "## 一、培养目标（确认版）\n" + ("\n".join([f"- {x}" for x in goals_final]) if goals_final else "- （未填）") + "\n\n"
                md += "## 二、毕业要求（确认版）\n" + ("\n".join([f"- {o.get('no','')}. {o.get('title','')}: {o.get('body','')}" for o in outcomes_final]) if outcomes_final else "- （未填）") + "\n\n"
                md += "## 三、抽取表格（共{}个）\n".format(len(confirmed_tables))
                for i, tbl in enumerate(confirmed_tables, 1):
                    md += f"- 表格{i}（第{tbl['page']}页）: {tbl['title']}\n"
                md += "\n## 四、章节结构\n"
                for section_name in list(sections.keys())[:5]:
                    md += f"- {section_name}\n"
                
                title = f"培养方案（PDF抽取确认版）-{ex['source']}"
                a2 = upsert_artifact(project_id, "training_plan", title, md, content_json, [], note="pdf-extract-confirm")
                st.success("已保存'确认版培养方案底座'。后续生成大纲会优先使用结构化字段。")
                st.session_state.pop("tp_extract", None)
                st.rerun()
            
            if st.button("清除本次抽取结果（不保存）"):
                st.session_state.pop("tp_extract", None)
                st.info("已清除。")
    
    with tab2:
        if not tp:
            st.info("暂无培养方案。请先生成或上传并确认。")
        else:
            artifact_toolbar(a)
            st.markdown("#### 结构化内容")
            st.json(a.get("content_json") or {})
            st.markdown("#### Markdown预览")
            st.markdown(a["content_md"][:2000] + "..." if len(a["content_md"]) > 2000 else a["content_md"])
    
    with tab3:
        if not tp:
            st.info("暂无培养方案。请先生成或上传。")
        else:
            edited = md_textarea("在线编辑培养方案（支持直接修改）", a["content_md"], key="tp_edit")
            note = st.text_input("保存说明（可选）", value="edit", key="tp_note")
            if st.button("保存修改（生成新版本）", type="primary", key="tp_save"):
                a2 = upsert_artifact(project_id, "training_plan", a["title"], edited, a["content_json"], [], note=note)
                st.success("已保存。后续依赖文件将引用更新后的培养方案。")
                st.rerun()
    
    with tab4:
        if not tp:
            st.info("暂无培养方案。")
        else:
            vers = get_versions(a["id"])
            st.markdown("#### 版本记录")
            st.dataframe(vers if vers else [], use_container_width=True)
    
    with tab5:
        st.markdown("### PDF全量抽取独立界面（增强版）")
        st.caption("确保显示所有章节（如一到六）和所有附表（如附表1到5，对应七到十一）")
        
        if "extract_result" not in st.session_state:
            st.session_state["extract_result"] = None
        
        uploaded = st.file_uploader("上传培养方案 PDF", type=["pdf"], key="full_extract_upload")
        use_ocr = st.checkbox("对无文本页启用 OCR（可选）", value=True, key="full_extract_ocr")  # 默认启用OCR以确保完整
        
        if uploaded and st.button("开始全量抽取", type="primary", key="full_extract_btn"):
            pdf_bytes = uploaded.getvalue()
            with st.spinner("正在抽取所有内容…"):
                extract_result = run_full_extract(pdf_bytes, use_ocr=use_ocr)
                st.session_state["extract_result"] = extract_result
        
        result = st.session_state.get("extract_result")
        if result is None:
            st.stop()
        
        # 概览指标
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("总页数", result.page_count)
        c2.metric("表格总数", result.table_count)
        c3.metric("OCR启用", "是" if result.ocr_used else "否")
        c4.caption(f"SHA256: {result.file_sha256[:16]}...")
        
        tabs_full = st.tabs(["概览与下载", "章节大标题（全部）", "培养目标", "毕业要求", "附表表格（全部）", "分页原文与表格"])
        
        with tabs_full[0]:
            st.markdown("### 结构化识别结果（可先在这里校对）")
            
            # 下载 JSON（全量）
            json_bytes = json.dumps(asdict(result), ensure_ascii=False, indent=2).encode("utf-8")
            st.download_button(
                "下载抽取结果 JSON",
                data=json_bytes,
                file_name="training_plan_full_extract.json",
                mime="application/json",
                use_container_width=True,
            )
            
            if result.tables:
                zip_bytes = make_tables_zip(result.tables)
                st.download_button(
                    "下载表格 ZIP",
                    data=zip_bytes,
                    file_name="training_plan_tables.zip",
                    mime="application/zip",
                    use_container_width=True,
                )
            
            st.markdown("#### 附表标题映射")
            if result.appendix_titles:
                st.json(result.appendix_titles)
            else:
                st.info("未检测到附表标题映射。")
        
        with tabs_full[1]:
            st.markdown("### 章节大标题（全部，包括一到十一）")
            st.caption("显示所有大标题及其内容，确保无遗漏")
            for k in sorted(result.sections.keys()):  # 排序显示
                with st.expander(k, expanded=True):  # 默认展开以确保查看
                    st.text(result.sections.get(k, ""))
      

        with tabs_full[2]:
            st.markdown("### 培养目标")
            obj = result.training_objectives
            st.write(f"识别条目数：**{obj.get('count', 0)}**")
            st.text_area("培养目标（逐条）", value="\n".join(obj.get("items", [])), height=220, key="full_obj")
            with st.expander("原始文本"):
                st.text(obj.get("raw", ""))
        
        with tabs_full[3]:
            st.markdown("### 毕业要求（12条 + 分项）")
            grad = result.graduation_requirements
            st.write(f"识别主条目数：**{grad.get('count', 0)}**")
            
            items = grad.get("items", [])
            if not items:
                st.warning("未识别到毕业要求")
            else:
                for it in items:
                    no = it.get("no")
                    title = it.get("title") or ""
                    body = it.get("body") or ""
                    header = f"{no}. {title}".strip()
                    with st.expander(header, expanded=(no in [1, 2])):
                        st.write(body)
                        subs = it.get("subitems", [])
                        if subs:
                            st.markdown("**分项：**")
                            for s in subs:
                                st.write(f"- {s.get('no')}: {s.get('body')}")
            with st.expander("原始文本"):
                st.text(grad.get("raw", ""))


        
        with tabs_full[4]:
            st.markdown("### 附表表格（全部，包括附表1到5，对应七到十一）")
            if not result.tables:
                st.info("未检测到表格。请检查PDF是否有表格，或尝试启用OCR。")
            else:
                all_dirs = sorted({clean_text(t.get("direction") or "") for t in result.tables if clean_text(t.get("direction") or "")})
                opt_dirs = ["全部"] + all_dirs
                sel = st.selectbox("方向过滤", opt_dirs, index=0)
                
                for t in sorted(result.tables, key=lambda x: x.get('page', 0)):  # 按页排序
                    direction = clean_text(t.get("direction") or "")
                    if sel != "全部" and direction != sel:
                        continue
                    
                    st.subheader(f"第{t.get('page')}页｜{t.get('title')}")
                    if direction:
                        st.caption(f"页面方向提示：{direction}")
                    
                    df = safe_df_from_tablepack(t)
                    st.dataframe(df, use_container_width=True, hide_index=True)
        
        with tabs_full[5]:
            st.markdown("### 分页原文与表格（用于溯源/调试抽取缺失）")
            
            for page_data in result.pages_data:  # 这里添加 result.
                page_no = page_data["page"]
                page_text = page_data["text"]
                page_tables = page_data["tables"]
                
                with st.expander(f"第{page_no}页（{len(page_tables)}个表格）", expanded=False):
                    st.text(page_text)
                    
                    if page_tables:
                        st.markdown(f"**表格 ({len(page_tables)}个):**")
                        for i, table_data in enumerate(page_tables, start=1):
                            df = table_to_df(table_data)
                            if not df.empty:
                                st.markdown(f"**表格 {i}:**")
                                st.dataframe(df, use_container_width=True)
                            else:
                                st.info(f"表格 {i} 为空")



# 其他页面函数（简化实现）
def page_syllabus():
    ensure_project()
    render_depbar(project_id, "syllabus")
    tp = get_artifact(project_id, "training_plan")
    a = get_artifact(project_id, "syllabus")
    
    st.markdown("### 课程教学大纲")
    
    if not tp:
        st.warning("请先创建培养方案")
    else:
        st.info("功能开发中...")

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

def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def safe_strip(x):
    if x is None:
        return ""
    return str(x).strip()


def normalize_table(raw_table):
    """
    pdfplumber.extract_tables() 返回 list[list[str|None]]
    这里做基础清洗：去空行、补齐列数、去掉全空列
    """
    if not raw_table:
        return None

    rows = []
    max_cols = 0
    for r in raw_table:
        if r is None:
            continue
        rr = [safe_strip(c) for c in r]
        # 跳过全空行
        if all(c == "" for c in rr):
            continue
        rows.append(rr)
        max_cols = max(max_cols, len(rr))

    if not rows or max_cols == 0:
        return None

    # 补齐列数
    for i in range(len(rows)):
        if len(rows[i]) < max_cols:
            rows[i] = rows[i] + [""] * (max_cols - len(rows[i]))

    # 去掉全空列
    keep_cols = []
    for j in range(max_cols):
        col = [rows[i][j] for i in range(len(rows))]
        if any(c != "" for c in col):
            keep_cols.append(j)

    if not keep_cols:
        return None

    cleaned = [[row[j] for j in keep_cols] for row in rows]
    return cleaned


def table_to_df(cleaned_table):
    """
    尝试把第一行当表头；如果表头太差就用默认列名。
    """
    if not cleaned_table or len(cleaned_table) == 0:
        return None
    if len(cleaned_table) == 1:
        # 只有一行，做单行df
        return pd.DataFrame([cleaned_table[0]])

    header = cleaned_table[0]
    body = cleaned_table[1:]

    # 表头判定：至少有一半单元格非空
    non_empty = sum(1 for x in header if safe_strip(x) != "")
    if non_empty >= max(1, len(header) // 2):
        cols = [h if h else f"col_{i+1}" for i, h in enumerate(header)]
        return pd.DataFrame(body, columns=cols)

    # 否则不用表头
    return pd.DataFrame(cleaned_table)


def try_ocr_page(plumber_page) -> str:
    """
    可选OCR：仅在 pytesseract 存在且系统有 tesseract 时可用。
    不满足条件则返回空串，不抛异常。
    """
    try:
        import pytesseract  # noqa
        from PIL import Image  # noqa
    except Exception:
        return ""

    try:
        img = plumber_page.to_image(resolution=220).original
        # pytesseract 对中文需要 chi_sim；若环境没装中文语言包也可能效果一般
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception:
        return ""


# ----------------------------
# core: extract
# ----------------------------
@st.cache_data(show_spinner=False)
def extract_pdf_all(pdf_bytes: bytes, enable_ocr: bool = False):
    if pdfplumber is None:
        raise RuntimeError("缺少依赖 pdfplumber，请先在 requirements.txt 安装：pdfplumber")

    meta = {
        "sha256": sha256_bytes(pdf_bytes),
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "ocr_enabled": bool(enable_ocr),
    }

    pages = []
    total_tables = 0

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        meta["n_pages"] = len(pdf.pages)

        # table settings：偏“宽松”，提升跨页/复杂表格提取成功率
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
            text = page.extract_text() or ""
            text = text.strip()

            # 如果这一页几乎没字且用户勾选OCR，就尝试OCR补救
            if enable_ocr and len(text) < 20:
                ocr_text = try_ocr_page(page)
                if len(ocr_text) > len(text):
                    text = ocr_text

            raw_tables = []
            try:
                raw_tables = page.extract_tables(table_settings=table_settings) or []
            except Exception:
                raw_tables = []

            cleaned_tables = []
            for t in raw_tables:
                ct = normalize_table(t)
                if ct:
                    cleaned_tables.append(ct)

            total_tables += len(cleaned_tables)

            pages.append(
                {
                    "page": idx,
                    "text": text,
                    "tables": cleaned_tables,  # list of list-of-rows
                }
            )

    meta["n_tables"] = total_tables
    return {"meta": meta, "pages": pages}


# ----------------------------
# core: structure parse
# ----------------------------
def join_all_text(pages):
    chunks = []
    for p in pages:
        t = safe_strip(p.get("text", ""))
        if t:
            chunks.append(f"[PAGE {p['page']}]\n{t}")
    return "\n\n".join(chunks)


def extract_section(full_text: str, start_keywords, end_keywords):
    """
    从全文中按“起止关键词”粗切片（适用于培养目标、毕业要求等）
    """
    start_pat = "|".join(map(re.escape, start_keywords))
    end_pat = "|".join(map(re.escape, end_keywords))

    m = re.search(rf"({start_pat})", full_text)
    if not m:
        return ""

    start = m.start()
    tail = full_text[start:]

    m2 = re.search(rf"({end_pat})", tail[10:])  # 略过开头10字符，避免同词误触
    if not m2:
        return tail.strip()

    end = 10 + m2.start()
    return tail[:end].strip()


def parse_objectives(section_text: str):
    """
    培养目标常见格式：(1)(2)... 或 1. 2. / 1、2、
    """
    if not section_text:
        return []

    # 去掉页码标记
    txt = re.sub(r"\[PAGE\s+\d+\]", "", section_text)

    # 先抓 (1) (2)...
    items = re.split(r"\(\s*\d+\s*\)", txt)
    items = [i.strip() for i in items if i.strip()]
    if len(items) >= 2:
        return items

    # 再抓 1. / 1、 2.
    parts = re.split(r"(?m)^\s*\d+\s*[\.、]\s*", txt)
    parts = [p.strip() for p in parts if p.strip()]
    # 过滤掉明显是标题/过短
    parts = [p for p in parts if len(p) >= 10]
    return parts


def parse_graduation_requirements(section_text: str):
    """
    目标：尽量整理出 1-12 条毕业要求
    """
    if not section_text:
        return {}

    txt = re.sub(r"\[PAGE\s+\d+\]", "", section_text)
    txt = txt.replace("：", ":")
    # 常见： "毕业要求1" / "毕业要求 1" / "1." / "1、"
    # 先统一把“毕业要求X”变成换行 + X.
    txt = re.sub(r"毕业要求\s*([1-9]|1[0-2])\s*", r"\n\1. ", txt)

    # 用行首数字切
    chunks = re.split(r"(?m)^\s*([1-9]|1[0-2])\s*[\.、]\s*", txt)
    # re.split 会得到： [pre, num1, text1, num2, text2,...]
    req = {}
    if len(chunks) >= 3:
        pre = chunks[0].strip()
        it = chunks[1:]
        for i in range(0, len(it) - 1, 2):
            num = int(it[i])
            content = it[i + 1].strip()
            # 截断到下一个大标题前的残留（经验性）
            content = re.split(r"\n\s*(课程体系|课程设置|课程结构|课程一览|学分|附表)", content)[0].strip()
            if content:
                req[num] = content

    # 若仍不够，尝试再从文本中找“X）/X)” 形式
    if len(req) < 10:
        alt = re.split(r"(?m)^\s*([1-9]|1[0-2])\s*[\)）]\s*", txt)
        if len(alt) >= 3:
            it = alt[1:]
            for i in range(0, len(it) - 1, 2):
                num = int(it[i])
                content = it[i + 1].strip()
                content = re.split(r"\n\s*(课程体系|课程设置|课程结构|课程一览|学分|附表)", content)[0].strip()
                if content and num not in req:
                    req[num] = content

    # 保序输出
    return dict(sorted(req.items(), key=lambda x: x[0]))


def collect_course_tables(pages):
    """
    从所有表格里找“像课程表”的表：包含关键词（课程/学分/学时/性质/类别等）
    并将同类表尽量合并。
    """
    dfs = []
    for p in pages:
        for t in p.get("tables", []):
            df = table_to_df(t)
            if df is None or df.empty:
                continue
            # 判断是否像课程表
            flat = " ".join([safe_strip(c) for c in df.columns]) + " " + " ".join(
                safe_strip(x) for x in df.head(3).astype(str).values.flatten().tolist()
            )
            key_hits = sum(
                1
                for kw in ["课程", "学分", "学时", "性质", "类别", "必修", "选修", "开课", "理论", "实践", "周学时"]
                if kw in flat
            )
            if key_hits >= 2:
                df2 = df.copy()
                df2.insert(0, "__page__", p["page"])
                dfs.append(df2)

    if not dfs:
        return None

    # 简单合并：按列名完全一致优先concat；否则直接返回列表
    # 这里保守处理，避免强行对齐导致错位
    groups = {}
    for df in dfs:
        sig = tuple(df.columns.tolist())
        groups.setdefault(sig, []).append(df)

    merged = []
    for sig, group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            merged.append(pd.concat(group, ignore_index=True))

    return merged


def parse_structured(extracted):
    pages = extracted["pages"]
    full_text = join_all_text(pages)

    # 根据常见培养方案结构做粗切
    objectives_text = extract_section(
        full_text,
        start_keywords=["培养目标", "一、培养目标", "（一）培养目标"],
        end_keywords=["毕业要求", "二、毕业要求", "（二）毕业要求", "课程体系", "课程设置", "课程结构"],
    )

    gradreq_text = extract_section(
        full_text,
        start_keywords=["毕业要求", "二、毕业要求", "（二）毕业要求"],
        end_keywords=["课程体系", "课程设置", "课程结构", "课程一览", "课程表", "学分要求", "附表"],
    )

    objectives = parse_objectives(objectives_text)
    gradreq = parse_graduation_requirements(gradreq_text)

    course_tables = collect_course_tables(pages)

    structured = {
        "objectives": objectives,
        "graduation_requirements": gradreq,  # dict {1: "...", ..., 12:"..."}
        "course_tables_count": 0 if not course_tables else len(course_tables),
    }

    return structured, full_text, course_tables


# ----------------------------
# export builders
# ----------------------------
def build_json_bytes(extracted, structured, full_text):
    pack = {
        "meta": extracted["meta"],
        "structured": structured,
        "full_text": full_text,
        "pages": extracted["pages"],
    }
    return json.dumps(pack, ensure_ascii=False, indent=2).encode("utf-8")


def build_csv_zip_bytes(extracted, course_tables):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # pages text
        rows = []
        for p in extracted["pages"]:
            rows.append({"page": p["page"], "text": p.get("text", "")})
        df_pages = pd.DataFrame(rows)
        z.writestr("pages_text.csv", df_pages.to_csv(index=False, encoding="utf-8-sig"))

        # all tables as csv (逐表输出)
        t_index = 0
        for p in extracted["pages"]:
            for t in p.get("tables", []):
                t_index += 1
                df = table_to_df(t)
                if df is None:
                    continue
                name = f"tables/page_{p['page']}_table_{t_index}.csv"
                z.writestr(name, df.to_csv(index=False, encoding="utf-8-sig"))

        # course tables merged
        if course_tables:
            for i, df in enumerate(course_tables, start=1):
                z.writestr(f"course_tables_merged_{i}.csv", df.to_csv(index=False, encoding="utf-8-sig"))

    return buf.getvalue()


def build_excel_bytes(extracted, structured, course_tables):
    """
    尝试导出 xlsx：
    - 若 openpyxl 或 xlsxwriter 存在则可用
    - 两者都不存在则返回 None
    """
    engine = None
    try:
        import openpyxl  # noqa
        engine = "openpyxl"
    except Exception:
        pass

    if engine is None:
        try:
            import xlsxwriter  # noqa
            engine = "xlsxwriter"
        except Exception:
            pass

    if engine is None:
        return None, "未检测到 openpyxl/xlsxwriter，无法导出xlsx（已提供JSON/CSV导出）。"

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine=engine) as writer:
        # meta
        meta_df = pd.DataFrame([extracted["meta"]])
        meta_df.to_excel(writer, index=False, sheet_name="meta")

        # objectives
        obj_df = pd.DataFrame(
            [{"idx": i + 1, "培养目标": t} for i, t in enumerate(structured.get("objectives", []))]
        )
        obj_df.to_excel(writer, index=False, sheet_name="培养目标")

        # graduation requirements
        gr = structured.get("graduation_requirements", {})
        gr_df = pd.DataFrame([{"编号": k, "毕业要求": v} for k, v in gr.items()])
        gr_df.to_excel(writer, index=False, sheet_name="毕业要求")

        # pages text (长文本放一列)
        pages_df = pd.DataFrame([{"page": p["page"], "text": p.get("text", "")} for p in extracted["pages"]])
        pages_df.to_excel(writer, index=False, sheet_name="pages_text")

        # course tables merged
        if course_tables:
            for i, df in enumerate(course_tables, start=1):
                sheet = f"课程表合并_{i}"
                # sheet名最长31字符
                sheet = sheet[:31]
                df.to_excel(writer, index=False, sheet_name=sheet)

    return output.getvalue(), f"已使用 {engine} 导出xlsx。"


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