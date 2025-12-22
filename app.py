# -*- coding: utf-8 -*-
"""培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）

v3 修复与增强（针对你反馈的“表格不显示/合并格为空/导出”）：
1) 表格展示：优先用 pdfplumber 抽取 + pandas 安全对齐，保证 st.dataframe 一定能显示。
2) 合并单元格空白：对疑似“课程体系/课程类别/专业方向”等列做纵向 forward-fill。
3) 导出：提供 JSON、CSV(zip)、Excel(xlsx) 三种下载。

说明：
- 本脚本是“独立页面版”最稳（你也可以把其中的核心函数粘回 teaching-agent-suite 的某个 page 里）。
- 不依赖在线大模型；QWEN 是否强大不是核心矛盾——缺失多来自 PDF 表格/分页/合并单元格抽取策略。
"""

from __future__ import annotations

import io
import re
import json
import hashlib
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

# pdfplumber 是表格抽取的主力（相比 camelot 更容易在 Streamlit Cloud 跑通）
import pdfplumber


# ------------------------- 基础工具 -------------------------

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def clean_cell(x: Any) -> str:
    if x is None:
        return ""
    s = str(x)
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def safe_sheet_name(name: str, fallback: str) -> str:
    """Excel sheet 名称限制：<=31，且不能含 : \ / ? * [ ]"""
    s = (name or "").strip() or fallback
    s = re.sub(r"[:\\/?*\[\]]", "_", s)
    s = re.sub(r"\s+", " ", s)
    return s[:31]


# ------------------------- 结构化识别：标题/章节 -------------------------

SECTION_RE = re.compile(r"^(?P<no>[一二三四五六七八九十十一十二]{1,3})\s*、\s*(?P<title>.+?)\s*$")


def extract_major_headings(pages_text: List[str]) -> List[Dict[str, Any]]:
    """抽取形如“三、专业定位与特色”的大标题。"""
    out: List[Dict[str, Any]] = []
    for pi, txt in enumerate(pages_text, start=1):
        for line in (txt or "").splitlines():
            line = line.strip()
            m = SECTION_RE.match(line)
            if m:
                out.append({"page": pi, "no": m.group("no"), "title": m.group("title"), "raw": line})
    # 去重（同页重复）
    seen = set()
    uniq = []
    for x in out:
        key = (x["page"], x["raw"])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


# ------------------------- 表格抽取（pdfplumber） -------------------------

@dataclass
class TablePack:
    title: str
    page: int
    columns: List[str]
    rows: List[List[str]]


def _plumber_table_settings() -> Dict[str, Any]:
    # 这里的策略偏“稳”：尽量用线条识别 + 容忍一定误差
    return {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "edge_min_length": 3,
        "min_words_vertical": 1,
        "min_words_horizontal": 1,
        "intersection_tolerance": 3,
    }


def guess_table_titles_for_page(page_text: str) -> List[str]:
    """从页面文字猜测表名，如“七 专业教学计划表（附表1）”。"""
    if not page_text:
        return []
    candidates: List[str] = []
    # 常见："七 专业教学计划表（附表1）" / "附表1" 等
    for m in re.finditer(r"(附表\s*\d+\s*[^\n]{0,40})", page_text):
        s = m.group(1).strip()
        if s:
            candidates.append(s)
    for m in re.finditer(r"([一二三四五六七八九十]+\s*[^\n]{0,30}表\s*（?附表\s*\d+\)?])", page_text):
        candidates.append(m.group(1).strip())
    # 去重
    seen = set()
    out = []
    for c in candidates:
        c = re.sub(r"\s+", " ", c)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def table_to_pack(raw_table: List[List[Any]], title: str, page: int) -> Optional[TablePack]:
    """把 pdfplumber 的 table（list[list]）转换为 TablePack，并做安全对齐。"""
    if not raw_table or len(raw_table) < 2:
        return None

    # 清洗
    cleaned = [[clean_cell(c) for c in row] for row in raw_table]

    # 如果第一行看起来像表头，就用它当 columns
    columns = cleaned[0]
    rows = cleaned[1:]

    # 计算最大列数，做 pad/truncate
    max_cols = max(len(columns), max((len(r) for r in rows), default=0))
    if max_cols == 0:
        return None

    def pad(row: List[str]) -> List[str]:
        if len(row) < max_cols:
            return row + [""] * (max_cols - len(row))
        return row[:max_cols]

    columns = pad(columns)
    rows = [pad(r) for r in rows]

    # 如果 columns 全为空，就用默认列名
    if all((c.strip() == "" for c in columns)):
        columns = [f"col_{i+1}" for i in range(max_cols)]

    return TablePack(title=title, page=page, columns=columns, rows=rows)


def ffill_merged_like_columns(df: pd.DataFrame) -> pd.DataFrame:
    """处理合并单元格常见的“竖向空白”：对疑似分类列做 forward-fill。"""
    if df.empty:
        return df

    df2 = df.copy()

    # 候选列：名字里含“体系/类别/方向/模块/性质/环节”，或该列空白占比较高
    cols = list(df2.columns)

    def blank_ratio(s: pd.Series) -> float:
        v = s.astype(str).map(lambda x: x.strip())
        return (v == "").mean()

    candidate_cols = []
    for c in cols:
        cname = str(c)
        if any(k in cname for k in ["体系", "类别", "方向", "模块", "性质", "环节"]):
            candidate_cols.append(c)
        else:
            if blank_ratio(df2[c]) >= 0.35:
                candidate_cols.append(c)

    for c in candidate_cols:
        s = df2[c].astype(str).map(lambda x: x.strip())
        s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
        df2[c] = s.ffill().fillna("")

    return df2


def split_by_tracks(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """粗略把表按“焊接/无损检测”方向切分（若表里出现方向分隔行，则传播标记）。"""
    if df.empty:
        return df, df

    # 先找一列最可能含方向提示
    cols = list(df.columns)
    scan_cols = cols[: min(3, len(cols))]

    track = []
    cur = ""
    for _, row in df.iterrows():
        joined = " ".join(str(row[c]) for c in scan_cols)
        if re.search(r"焊接\s*方向", joined):
            cur = "焊接"
        elif re.search(r"无损\s*检测\s*方向", joined):
            cur = "无损检测"
        track.append(cur)

    df2 = df.copy()
    df2.insert(0, "专业方向(推断)", track)

    weld = df2[df2["专业方向(推断)"] == "焊接"].copy()
    ndt = df2[df2["专业方向(推断)"] == "无损检测"].copy()

    # 如果全空，返回原表（说明没有方向分隔）
    if (weld.empty and ndt.empty):
        return df, df

    return weld, ndt



def extract_appendix_title_map(pages_text):
    """从全文中识别“附表1-5”的标题行，并记录每个附表首次出现的页码。

    返回:
        appendix_title: dict，如 {"附表1": "七、专业教学计划表（附表1）", ...}
        appendix_start_page: dict，如 {"附表1": 12, ...}
    """
    appendix_title = {}
    appendix_start_page = {}

    for page_no, t in enumerate(pages_text, start=1):
        t = t or ""
        lines = [clean_text(x) for x in t.splitlines() if clean_text(x)]
        for n in range(1, 6):
            key = f"附表{n}"
            if key not in t:
                continue
            # 优先取包含“附表n”的整行作为标题
            picked = None
            for line in lines:
                if key in line:
                    picked = line
                    break
            if picked:
                appendix_title.setdefault(key, picked)
                appendix_start_page.setdefault(key, page_no)

    # 若标题识别不全，补默认值（避免空标题）
    defaults = {
        "附表1": "七、专业教学计划表（附表1）",
        "附表2": "八、学分统计表（附表2）",
        "附表3": "九、教学进程表（附表3）",
        "附表4": "十、课程设置对毕业要求支撑关系表（附表4）",
        "附表5": "十一、课程设置逻辑思维导图（附表5）",
    }
    for k, v in defaults.items():
        appendix_title.setdefault(k, v)

    return appendix_title, appendix_start_page

def extract_tables_pdfplumber(pdf_bytes: bytes, pages_text: List[str]) -> List[TablePack]:
    packs: List[TablePack] = []
    settings = _plumber_table_settings()
    # fallback：有些 PDF 的表格没有画线，用 text 策略更容易抓到
    settings_text = dict(settings)
    settings_text.update({"vertical_strategy": "text", "horizontal_strategy": "text"})

    appendix_title, appendix_start_page = extract_appendix_title_map(pages_text)
    ordered_apps = sorted(appendix_start_page.items(), key=lambda x: x[1])

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            guessed_titles = guess_table_titles_for_page(page_text)

            # 根据页码推断当前处于哪个“附表”段落
            cur_app = ""
            for app, p0 in ordered_apps:
                if p0 <= i:
                    cur_app = app
                else:
                    break
            base_title = appendix_title.get(cur_app, "") if cur_app else ""

            raw_tables = []
            try:
                raw_tables = page.extract_tables(table_settings=settings) or []
            except Exception:
                raw_tables = []

            if not raw_tables:
                try:
                    raw_tables = page.extract_tables(table_settings=settings_text) or []
                except Exception:
                    raw_tables = []

            # 逐表打包
            for ti, raw in enumerate(raw_tables, start=1):
                title = (f"{base_title} - 子表{ti}" if (base_title and ti > 1) else base_title) if base_title else (guessed_titles[ti - 1] if ti - 1 < len(guessed_titles) else f"表格 P{i}-{ti}")
                pack = table_to_pack(raw, title=title, page=i)
                if pack is not None:
                    # 过滤掉几乎全空的表
                    nonempty = sum(1 for r in pack.rows for c in r if c.strip())
                    if nonempty >= 4:
                        packs.append(pack)

    return packs


# ------------------------- 文本抽取 -------------------------


def extract_pages_text(pdf_bytes: bytes) -> List[str]:
    pages: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            pages.append(txt)
    return pages


# ------------------------- 导出：CSV(zip) / Excel / JSON -------------------------


def tablepack_to_df(tp: TablePack) -> pd.DataFrame:
    df = pd.DataFrame(tp.rows, columns=tp.columns)
    df = df.applymap(lambda x: clean_cell(x))
    df = ffill_merged_like_columns(df)
    return df


def make_tables_zip(tablepacks: List[TablePack]) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        for idx, tp in enumerate(tablepacks, start=1):
            df = tablepack_to_df(tp)
            csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
            name = safe_sheet_name(tp.title, f"table_{idx}")
            z.writestr(f"{idx:02d}_{name}_P{tp.page}.csv", csv_bytes)
    return mem.getvalue()


def make_excel_bytes(pages_text: List[str], headings: List[Dict[str, Any]], tablepacks: List[TablePack], meta: Dict[str, Any]) -> bytes:
    mem = io.BytesIO()

    # 优先 xlsxwriter（更适合写新文件），没有再退 openpyxl
    engine = "xlsxwriter"
    try:
        import xlsxwriter  # noqa: F401
    except Exception:
        engine = "openpyxl"

    with pd.ExcelWriter(mem, engine=engine) as writer:
        # meta
        pd.DataFrame([meta]).to_excel(writer, index=False, sheet_name="meta")

        # headings
        if headings:
            pd.DataFrame(headings).to_excel(writer, index=False, sheet_name="headings")

        # pages text（每页一行）
        pd.DataFrame({"page": list(range(1, len(pages_text) + 1)), "text": pages_text}).to_excel(
            writer, index=False, sheet_name="pages_text"
        )

        # tables
        used = set(["meta", "headings", "pages_text"])
        for idx, tp in enumerate(tablepacks, start=1):
            df = tablepack_to_df(tp)

            # 尝试方向拆分：若拆分成功，就写两个 sheet
            weld, ndt = split_by_tracks(df)
            base = safe_sheet_name(tp.title, f"T{idx}")

            def unique_sheet(nm: str) -> str:
                nm2 = nm
                k = 2
                while nm2 in used:
                    nm2 = safe_sheet_name(f"{nm}_{k}", nm)
                    k += 1
                used.add(nm2)
                return nm2

            if not weld.empty and not ndt.empty:
                weld.to_excel(writer, index=False, sheet_name=unique_sheet(base + "_焊接"))
                ndt.to_excel(writer, index=False, sheet_name=unique_sheet(base + "_无损"))
            else:
                df.to_excel(writer, index=False, sheet_name=unique_sheet(base))

    return mem.getvalue()


# ------------------------- Streamlit UI -------------------------

st.set_page_config(page_title="培养方案 PDF 全量抽取（文本+表格+结构化解析）", layout="wide")

st.title("培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）")

with st.sidebar:
    st.header("上传与抽取")
    up = st.file_uploader("上传培养方案 PDF", type=["pdf"], accept_multiple_files=False)
    run = st.button("开始全量抽取", type="primary", use_container_width=True)

if up is None:
    st.info("请先在左侧上传 PDF，然后点击【开始全量抽取】。")
    st.stop()

pdf_bytes = up.getvalue()

if run:
    with st.spinner("正在抽取文本与表格…"):
        pages_text = extract_pages_text(pdf_bytes)
        headings = extract_major_headings(pages_text)
        tables = extract_tables_pdfplumber(pdf_bytes, pages_text)

        meta = {
            "filename": up.name,
            "sha256": sha256_bytes(pdf_bytes),
            "pages": len(pages_text),
            "tables": len(tables),
        }

        # 保存到 session
        st.session_state["tp_pages_text"] = pages_text
        st.session_state["tp_headings"] = headings
        st.session_state["tp_tables"] = tables
        st.session_state["tp_meta"] = meta

# 读取 session
pages_text = st.session_state.get("tp_pages_text")
headings = st.session_state.get("tp_headings")
tables: List[TablePack] = st.session_state.get("tp_tables")
meta = st.session_state.get("tp_meta")

if not pages_text:
    st.warning("尚未抽取到内容。请点击左侧【开始全量抽取】。")
    st.stop()

# 顶部统计
c1, c2, c3, c4 = st.columns([1, 1, 1, 4])
with c1:
    st.metric("总页数", meta.get("pages", len(pages_text)) if meta else len(pages_text))
with c2:
    st.metric("表格数", meta.get("tables", len(tables)) if meta else len(tables))
with c3:
    st.metric("SHA256", "已计算")
with c4:
    st.caption(f"SHA256: {meta.get('sha256','')}" if meta else "")

# 下载区
download_col1, download_col2, download_col3 = st.columns([1, 1, 1])

# JSON（全量）
full_json = {
    "meta": meta,
    "headings": headings,
    "pages_text": pages_text,
    "tables": [
        {"title": t.title, "page": t.page, "columns": t.columns, "rows": t.rows}
        for t in (tables or [])
    ],
}
json_bytes = json.dumps(full_json, ensure_ascii=False, indent=2).encode("utf-8")

with download_col1:
    st.download_button(
        "下载抽取结果 JSON",
        data=json_bytes,
        file_name=f"{up.name}_extract.json",
        mime="application/json",
        use_container_width=True,
    )

# CSV(zip)
with download_col2:
    if tables:
        zip_bytes = make_tables_zip(tables)
        st.download_button(
            "下载表格 CSV.zip",
            data=zip_bytes,
            file_name=f"{up.name}_tables.zip",
            mime="application/zip",
            use_container_width=True,
        )
    else:
        st.button("下载表格 CSV.zip", disabled=True, use_container_width=True)

# Excel
with download_col3:
    xlsx_bytes = make_excel_bytes(pages_text, headings, tables or [], meta or {})
    st.download_button(
        "下载 Excel(xlsx)",
        data=xlsx_bytes,
        file_name=f"{up.name}_extract.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

st.divider()

# 主体 tabs
tab_text, tab_head, tab_tables = st.tabs(["逐页文本", "大标题（章节）", "附表表格"])

with tab_text:
    st.subheader("逐页文本（可检索/校对）")
    q = st.text_input("检索关键词（可选）", value="")
    for i, txt in enumerate(pages_text, start=1):
        if q.strip() and q.strip() not in (txt or ""):
            continue
        with st.expander(f"第 {i} 页", expanded=(i == 1 and not q.strip())):
            st.text(txt or "")

with tab_head:
    st.subheader("识别到的大标题")
    if headings:
        st.dataframe(pd.DataFrame(headings), use_container_width=True, hide_index=True)
    else:
        st.info("未识别到大标题（可能 PDF 的标题不是按 '三、xxx' 这种格式排版）。")

with tab_tables:
    st.subheader("抽取到的表格（已尝试补全合并格空白）")
    if not tables:
        st.info("未抽取到表格。若你的 PDF 是扫描图片表格，需要 OCR/图片表格识别方案。")
    else:
        for idx, tp in enumerate(tables, start=1):
            df = tablepack_to_df(tp)
            title = tp.title or f"表格 {idx}"
            with st.expander(f"{idx:02d}. {title}  （第 {tp.page} 页）", expanded=(idx <= 1)):
                st.dataframe(df, use_container_width=True)

                # 如果检测到方向分隔，则给出拆分预览
                weld, ndt = split_by_tracks(df)
                if not weld.empty and not ndt.empty:
                    st.caption("检测到‘焊接/无损检测’方向分隔，下面是拆分预览（导出的 Excel 也会分 sheet）。")
                    st.write("焊接方向：")
                    st.dataframe(weld, use_container_width=True)
                    st.write("无损检测方向：")
                    st.dataframe(ndt, use_container_width=True)
