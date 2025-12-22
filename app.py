# app.py
# -*- coding: utf-8 -*-
"""
培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）
- 目标：尽量“全量、可追溯、可扩展”。后续所有教学文件都可以以该基础库为依据。
- 设计原则：
  1) 全文/分页文本保留（用于溯源与二次解析）
  2) 所有附表尽量抽取为结构化表格（并做合并单元格“向下填充”修复）
  3) 关键结构化信息：培养目标、毕业要求（12条+分项）、各大章标题
  4) 明确专业方向：焊接 / 无损检测（页面级 + 行级提示）
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import streamlit as st

# 依赖：pdfplumber + camelot
import pdfplumber

try:
    import camelot
except Exception:  # pragma: no cover
    camelot = None


# ----------------------------
# 基础工具
# ----------------------------
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


def safe_df_from_tablepack(t: Dict[str, Any]) -> pd.DataFrame:
    """rows/columns 不匹配时也能生成 DataFrame，避免 ValueError。"""
    cols = t.get("columns") or []
    rows = t.get("rows") or []

    cols = make_unique_columns([clean_text(c) for c in cols]) if cols else []

    if rows and isinstance(rows[0], dict):
        df = pd.DataFrame(rows)
        if cols:
            df = df.reindex(columns=cols, fill_value="")
        else:
            df.columns = make_unique_columns([str(c) for c in df.columns])
        return postprocess_table_df(df)

    if rows and isinstance(rows[0], (str, int, float)):
        df = pd.DataFrame({cols[0] if cols else "text": rows})
        return postprocess_table_df(df)

    # list[list]
    max_len = max((len(r) for r in rows), default=0)
    if not cols:
        cols = make_unique_columns([f"col{i+1}" for i in range(max_len)])
    else:
        max_len = max(max_len, len(cols))
        if len(cols) < max_len:
            cols = cols + [f"col{len(cols)+i+1}" for i in range(max_len - len(cols))]

    fixed_rows: List[List[Any]] = []
    for r in rows:
        r = list(r) if isinstance(r, (list, tuple)) else [r]
        if len(r) < len(cols):
            r = r + [""] * (len(cols) - len(r))
        elif len(r) > len(cols):
            r = r[: len(cols)]
        fixed_rows.append(r)

    df = pd.DataFrame(fixed_rows, columns=cols)
    return postprocess_table_df(df)


def make_tables_zip(tables: List[Dict[str, Any]]) -> bytes:
    """CSV + tables.json 打包（不依赖 openpyxl）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("tables.json", json.dumps(tables, ensure_ascii=False, indent=2))
        for idx, t in enumerate(tables, start=1):
            title = clean_text(t.get("title") or f"table_{idx}")
            title_safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "_", title)[:80].strip("_") or f"table_{idx}"

            df = safe_df_from_tablepack(t)

            # 方向列（便于后续）
            direction = clean_text(t.get("direction") or "")
            if direction and "专业方向" not in df.columns:
                df.insert(0, "专业方向", direction)

            csv_bytes = df.to_csv(index=False, encoding="utf-8-sig")
            zf.writestr(f"{idx:02d}_{title_safe}.csv", csv_bytes)
    return buf.getvalue()


# ----------------------------
# PDF 抽取：文本 + 表格
# ----------------------------
def extract_pages_text(pdf_bytes: bytes) -> Tuple[List[str], str]:
    """返回：每页文本列表 + 全文"""
    pages: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            txt = p.extract_text() or ""
            pages.append(normalize_multiline(txt))
    full_text = "\n".join(pages)
    return pages, full_text


def _clean_cell(x):
    if x is None:
        return ""
    s = str(x)
    s = s.replace("\u00a0", " ").replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _normalize_rows(rows):
    rows = rows or []
    norm = []
    max_len = 0
    for r in rows:
        rr = [_clean_cell(c) for c in (r or [])]
        norm.append(rr)
        max_len = max(max_len, len(rr))
    if max_len == 0:
        return []
    out = []
    for rr in norm:
        if len(rr) < max_len:
            rr = rr + [""] * (max_len - len(rr))
        elif len(rr) > max_len:
            rr = rr[:max_len]
        out.append(rr)
    return out


def _make_unique_columns(cols):
    seen = {}
    out = []
    for c in cols:
        base = (str(c).strip() if c is not None else "") or "col"
        if base in seen:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 1
            out.append(base)
    return out


def _rows_to_df(rows):
    rows = _normalize_rows(rows)
    if not rows:
        return pd.DataFrame()

    header = rows[0]
    body = rows[1:] if len(rows) > 1 else []
    empty_ratio = sum(1 for c in header if not c) / max(1, len(header))

    if empty_ratio > 0.6:
        columns = [f"col{i+1}" for i in range(len(header))]
        body = rows
    else:
        columns = header

    columns = _make_unique_columns(columns)
    df = pd.DataFrame(body, columns=columns)
    return df


def _ffill_merged_like_columns(df: pd.DataFrame, empty_threshold: float = 0.35) -> pd.DataFrame:
    """对“疑似合并单元格导致的空白列”做前向填充（课程体系/类别等常见）。

    注意：表格可能出现重复列名，因此按列序号处理，避免 df[col] 返回 DataFrame。"""
    if df is None or df.empty:
        return df

    out = df.copy()
    for j, _ in enumerate(list(out.columns)):
        s = out.iloc[:, j].astype(str)
        s = s.replace("nan", "").replace("None", "").str.strip()
        empties = (s == "") | (s == "—") | (s == "-")
        ratio = float(empties.mean()) if len(s) else 0.0
        if ratio >= empty_threshold:
            out.iloc[:, j] = s.mask(empties, pd.NA).ffill().fillna("")
        else:
            out.iloc[:, j] = s
    return out


def _guess_table_title_from_page(page, bbox, max_lines: int = 3) -> str:
    """尝试从表格上方区域抓取标题（如“七 专业教学计划表（附表1）”）。"""
    if not bbox:
        return ""
    try:
        x0, top, x1, bottom = bbox
        y0 = max(0, top - 180)
        crop = page.crop((0, y0, page.width, top))
        txt = crop.extract_text() or ""
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        cand = lines[-max_lines:][::-1] if lines else []
        for line in cand:
            if "附表" in line or re.search(r"附表\s*\d+", line):
                return line
        for line in cand:
            if "表" in line:
                return line
        return lines[-1] if lines else ""
    except Exception:
        return ""


def _detect_appendix_from_text(text: str):
    if not text:
        return None
    m = re.search(r"附表\s*([0-9]+)", text)
    if m:
        return f"附表{m.group(1)}"
    m = re.search(r"（\s*附表\s*([0-9]+)\s*）", text)
    if m:
        return f"附表{m.group(1)}"
    return None


def extract_tables_by_page(pdf_path: str, max_pages: int = None) -> Dict[int, List[Dict[str, Any]]]:
    """使用 pdfplumber 抽取表格（Streamlit Cloud 不依赖 ghostscript/camelot）。

    返回：{page_no: [ {'df': DataFrame, 'bbox': (x0, top, x1, bottom) or None, 'title_guess': str}, ... ] }

    """
    tables_by_page: Dict[int, List[Dict[str, Any]]] = {}

    table_settings = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "edge_min_length": 3,
        "min_words_vertical": 1,
        "min_words_horizontal": 1,
        "intersection_tolerance": 3,
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            n = min(total, max_pages) if max_pages else total

            for i in range(n):
                page = pdf.pages[i]
                page_no = i + 1
                page_items: List[Dict[str, Any]] = []

                # 1) 优先 find_tables（能得到 bbox）
                try:
                    found = page.find_tables(table_settings=table_settings) or []
                except Exception:
                    found = []

                if found:
                    for t in found:
                        try:
                            rows = t.extract() or []
                            df = _ffill_merged_like_columns(_rows_to_df(rows), empty_threshold=0.30)
                            if df is None or df.empty:
                                continue
                            page_items.append({
                                "df": df,
                                "bbox": tuple(t.bbox) if getattr(t, "bbox", None) else None,
                                "title_guess": _guess_table_title_from_page(page, getattr(t, "bbox", None)),
                            })
                        except Exception:
                            continue
                else:
                    # 2) 兜底：extract_tables（没有 bbox，但尽量拿到内容）
                    try:
                        raw_tables = page.extract_tables(table_settings=table_settings) or []
                    except Exception:
                        raw_tables = []
                    for rows in raw_tables:
                        df = _ffill_merged_like_columns(_rows_to_df(rows), empty_threshold=0.30)
                        if df is None or df.empty:
                            continue
                        page_items.append({"df": df, "bbox": None, "title_guess": ""})

                if page_items:
                    tables_by_page[page_no] = page_items

    except Exception:
        return {}

    return tables_by_page

def split_sections(full_text: str) -> Dict[str, str]:
    """
    按 “一、/二、/三、...” 大章切分。
    兼容：三、 / 三. / 三．
    """
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
    """抽取“附表X -> 标题（可能含七、八…）”"""
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
    提取“培养目标”条目。返回 items(list[str]) + raw。
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

    # 定位“二、毕业要求”
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

            # 处理“工程知识：...”这种
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
# 表格标题/方向
# ----------------------------
def guess_table_appendix_by_page(page_no: int, page_text: str = None) -> Optional[str]:
    """优先从页面文本中识别“附表X”，否则按页码做保底映射。"""
    ap = _detect_appendix_from_text(page_text or "")
    if ap:
        return ap

    # 保底映射（不同学校模板可能不同；这里仅作为最后兜底）
    mapping = {
        12: "附表1",
        13: "附表1",
        14: "附表2",
        15: "附表3",
        16: "附表4",
        17: "附表5",
        18: "附表5",
    }
    return mapping.get(page_no)

def add_direction_column_rowwise(df: pd.DataFrame, page_direction: str) -> pd.DataFrame:
    """
    行级方向识别：若表内有“焊接方向/无损检测方向”分隔行，则从该行开始向下标注。
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
    pages_text: List[str]
    sections: Dict[str, str]
    appendix_titles: Dict[str, str]
    training_objectives: Dict[str, Any]
    graduation_requirements: Dict[str, Any]
    tables: List[Dict[str, Any]]  # TablePack as dict


# ----------------------------
# 主流程
# ----------------------------
def sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def run_full_extract(pdf_bytes: bytes, use_ocr: bool = False) -> ExtractResult:
    # 1) 文本
    pages_text, full_text = extract_pages_text(pdf_bytes)
    sections = split_sections(full_text)
    appendix_titles = extract_appendix_titles(full_text)

    # 2) 关键结构化：培养目标、毕业要求
    #   - 培养目标通常在“一、培养目标”
    obj_key = next((k for k in sections.keys() if "培养目标" in k), "")
    obj = parse_training_objectives(sections.get(obj_key, "") or full_text)

    grad = parse_graduation_requirements(full_text)

    # 3) 表格（需要落盘一个临时文件给 camelot）
    tables_by_page: Dict[int, List[pd.DataFrame]] = {}
    tables: List[TablePack] = []
    if camelot is not None:
        tmp_path = "/tmp/training_plan.pdf"
        with open(tmp_path, "wb") as f:
            f.write(pdf_bytes)
                # 3) 表格（使用 pdfplumber 抽取，避免 Streamlit Cloud 上 camelot 依赖）
        tables_by_page = extract_tables_by_page(tmp_path, max_pages=meta.total_pages)
        tables: List[TablePack] = []

        appendix_name_map = {
            "附表1": "七 专业教学计划表",
            "附表2": "八 学分统计表",
            "附表3": "九 教学进程表",
            "附表4": "十 课程设置对毕业要求支撑关系表",
            "附表5": "十一 课程设置逻辑思维导图",
        }

        for page_no, items in tables_by_page.items():
            page_text = pages[page_no - 1]['text'] if 1 <= page_no <= len(pages) else ''
            appendix = guess_table_appendix_by_page(page_no, page_text=page_text)
            appendix_name = appendix_name_map.get(appendix, '') if appendix else ''
            multi = len(items) > 1

            for t_idx, item in enumerate(items, start=1):
                df = item.get('df')
                if df is None or df.empty:
                    continue

                title_guess = (item.get('title_guess') or '').strip()
                title_guess = re.sub(r'\s+', ' ', title_guess)
                base_title = title_guess or appendix_name or f'表格{t_idx}'
                if appendix and ('附表' not in base_title):
                    base_title = f"{base_title}（{appendix}）"
                if multi:
                    base_title = f"{base_title}-{t_idx}"
                title = f"{base_title}（第{page_no}页）"

                # 合并格导致的空白列：稳健填充
                df2 = _ffill_merged_like_columns(df.copy(), empty_threshold=0.30)
                df2 = add_direction_column_rowwise(df2)

                tables.append(TablePack(
                    page=page_no,
                    title=title,
                    appendix=appendix or '',
                    direction='ALL',
                    columns=list(df2.columns),
                    rows=df2.values.tolist(),
                ))


    result = ExtractResult(
        page_count=len(pages_text),
        table_count=sum(len(v) for v in tables_by_page.values()) if tables_by_page else 0,
        ocr_used=bool(use_ocr),
        file_sha256=sha256_bytes(pdf_bytes),
        pages_text=pages_text,
        sections=sections,
        appendix_titles=appendix_titles,
        training_objectives=obj,
        graduation_requirements=grad,
        tables=[asdict(t) for t in tables],
    )
    return result


# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="培养方案PDF全量抽取（基础库）", layout="wide")

st.markdown("# 培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）")
st.info("上传培养方案 PDF → 一键抽取全文文本、章节结构、毕业要求、培养目标、附表表格，并可下载 JSON/CSV。")

with st.sidebar:
    st.markdown("## 上传与抽取")
    uploaded = st.file_uploader("上传培养方案 PDF", type=["pdf"])
    use_ocr = st.checkbox("对无文本页启用 OCR（可选）", value=False, help="本版本默认不做 OCR（避免部署复杂度），保留开关以便后续扩展。")
    run_btn = st.button("开始全量抽取", type="primary")

if "extract_result" not in st.session_state:
    st.session_state["extract_result"] = None

if run_btn:
    if not uploaded:
        st.warning("请先上传 PDF。")
    else:
        pdf_bytes = uploaded.getvalue()
        with st.spinner("正在抽取…"):
            st.session_state["extract_result"] = run_full_extract(pdf_bytes, use_ocr=use_ocr)

result: Optional[ExtractResult] = st.session_state.get("extract_result")

if result is None:
    st.stop()

# 概览指标
c1, c2, c3, c4 = st.columns(4)
c1.metric("总页数", result.page_count)
c2.metric("表格总数", result.table_count)
c3.metric("OCR启用", "是" if result.ocr_used else "否")
c4.caption(f"SHA256: {result.file_sha256}")

tabs = st.tabs(["概览与下载", "章节大标题（全部）", "培养目标", "毕业要求（12条）", "附表表格（可下载CSV）", "分页原文（溯源）"])

# ---- Tab 0 概览与下载
with tabs[0]:
    st.markdown("### 结构化识别结果（可先在这里校对）")

    # 下载 JSON（全量）
    json_bytes = json.dumps(asdict(result), ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "下载抽取结果 JSON（全量基础库）",
        data=json_bytes,
        file_name="training_plan_full_extract.json",
        mime="application/json",
        use_container_width=True,
    )

    if result.tables:
        zip_bytes = make_tables_zip(result.tables)
        st.download_button(
            "下载表格 ZIP（CSV + tables.json）",
            data=zip_bytes,
            file_name="training_plan_tables.zip",
            mime="application/zip",
            use_container_width=True,
        )

    st.markdown("#### 附表标题映射（用于给表格命名）")
    if result.appendix_titles:
        st.json(result.appendix_titles)
    else:
        st.info("未在正文中检测到附表标题映射（不影响表格抽取，但表名可能不够精准）。")

# ---- Tab 1 章节大标题
with tabs[1]:
    st.markdown("### 章节大标题（用于确保“三~六”等内容不丢）")
    st.caption("这里展示 split_sections 抽到的全部大章标题，点击可展开查看正文（用于溯源和校对）。")
    for k in result.sections.keys():
        with st.expander(k, expanded=False):
            st.text(result.sections.get(k, ""))

# ---- Tab 2 培养目标
with tabs[2]:
    st.markdown("### 1）培养目标（可编辑/校对）")
    st.caption("若培养目标有多方向版本（焊接/无损），后续可在此基础上增强为分方向抽取。")

    obj = result.training_objectives
    st.write(f"识别条目数：**{obj.get('count', 0)}**")
    st.text_area("培养目标（逐条）", value="\n".join(obj.get("items", [])), height=220)
    with st.expander("原始文本（培养目标段）"):
        st.text(obj.get("raw", ""))

# ---- Tab 3 毕业要求
with tabs[3]:
    st.markdown("### 2）毕业要求（12条 + 分项）")
    grad = result.graduation_requirements
    st.write(f"识别主条目数：**{grad.get('count', 0)}**（理想为 12）")

    items = grad.get("items", [])
    if not items:
        st.warning("未识别到毕业要求，请在“分页原文”中确认 PDF 是否可提取文本。")
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
    with st.expander("原始文本（毕业要求段）"):
        st.text(grad.get("raw", ""))

# ---- Tab 4 表格
with tabs[4]:
    st.markdown("### 3）附表表格（表名 + 方向尽量清晰）")
    if not result.tables:
        st.info("未检测到表格。若在本地能抽到而云端抽不到，通常是 camelot 依赖缺失。")
    else:
        # 方向过滤
        all_dirs = sorted({clean_text(t.get("direction") or "") for t in result.tables if clean_text(t.get("direction") or "")})
        opt_dirs = ["全部"] + all_dirs
        sel = st.selectbox("方向过滤", opt_dirs, index=0)

        for t in result.tables:
            direction = clean_text(t.get("direction") or "")
            if sel != "全部" and direction != sel:
                continue

            st.subheader(f"第{t.get('page')}页｜{t.get('title')}")
            if direction:
                st.caption(f"页面方向提示：{direction}")

            df = safe_df_from_tablepack(t)
            # 这里 df 已含“专业方向”列（行级），且对合并格做了向下填充
            st.dataframe(df, use_container_width=True, hide_index=True)

# ---- Tab 5 分页原文
with tabs[5]:
    st.markdown("### 4）分页原文（用于溯源/调试抽取缺失）")
    for i, txt in enumerate(result.pages_text, start=1):
        with st.expander(f"第{i}页文本", expanded=False):
            st.text(txt)
