# app.py
# -*- coding: utf-8 -*-
import io
import re
import json
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
import pandas as pd

import pdfplumber

# OCR 可选：默认不启用。若你在 Streamlit Cloud 想启用 OCR，需要系统层 tesseract
# （Cloud 可用 packages.txt 安装），否则这里会自动提示“不可用”并跳过。
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    import pytesseract  # 需要系统 tesseract
except Exception:
    pytesseract = None


# -----------------------------
# 基础：通用工具
# -----------------------------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def clean_cell(x: Any) -> str:
    if x is None:
        return ""
    s = str(x)
    s = s.replace("\u3000", " ").strip()
    s = re.sub(r"[ \t]+", " ", s)
    return s


def normalize_table(tbl: List[List[Any]]) -> List[List[str]]:
    if not tbl:
        return []
    max_len = max(len(r) for r in tbl)
    out = []
    for r in tbl:
        rr = [clean_cell(c) for c in r]
        if len(rr) < max_len:
            rr += [""] * (max_len - len(rr))
        out.append(rr)
    # 去掉全空行
    out = [r for r in out if any(c.strip() for c in r)]
    return out


def table_to_df(tbl: List[List[str]], header_rows: int = 1) -> pd.DataFrame:
    if not tbl:
        return pd.DataFrame()

    if header_rows <= 0:
        header = [f"col_{i+1}" for i in range(len(tbl[0]))]
        body = tbl
        return pd.DataFrame(body, columns=header)

    # 只拿前 header_rows 行做表头，其余为数据
    hdr = tbl[:header_rows]
    body = tbl[header_rows:]

    # 1 行表头
    if header_rows == 1:
        header = hdr[0]
        header = [h if h else f"col_{i+1}" for i, h in enumerate(header)]
        return pd.DataFrame(body, columns=header)

    # 多行表头：常见于“毕业要求/指标点(1.1,1.2…)”这种跨列合并
    # 策略：第1行做 group，向右填充；第2行做 sub；最终 header = group_sub
    group = hdr[0][:]
    sub = hdr[1][:]

    # group 向右填充
    last = ""
    for i in range(len(group)):
        if group[i].strip():
            last = group[i].strip()
        else:
            group[i] = last

    header = []
    for i in range(len(group)):
        g = group[i].strip()
        s = sub[i].strip()
        if g and s and g != s:
            header.append(f"{g}_{s}")
        elif s:
            header.append(s)
        elif g:
            header.append(g)
        else:
            header.append(f"col_{i+1}")

    # 避免重名
    seen = {}
    uniq = []
    for h in header:
        if h not in seen:
            seen[h] = 1
            uniq.append(h)
        else:
            seen[h] += 1
            uniq.append(f"{h}_{seen[h]}")
    header = uniq

    return pd.DataFrame(body, columns=header)


# -----------------------------
# PDF 全量抽取：文本 + 表格 +（可选 OCR）
# -----------------------------
@dataclass
class PageExtract:
    page_no: int  # 1-based
    text: str
    tables: List[List[List[str]]]  # 多张表，每张表是二维数组[str]
    ocr_text: str = ""


def extract_pdf_all(
    pdf_bytes: bytes,
    do_ocr: bool = False,
    ocr_dpi: int = 200,
) -> Dict[str, Any]:
    """
    目标：把培养方案 PDF 的“所有基础材料”一次抽全，后续任何功能都以此为底库。
    返回结构：
      {
        "meta": {...},
        "pages": [PageExtract... as dict],
        "all_text": "...",
        "tables_flat": [{"page":..,"table_index":..,"n_rows":..,"n_cols":..,"table":..}, ...]
      }
    """
    pages: List[PageExtract] = []
    tables_flat: List[Dict[str, Any]] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n_pages = len(pdf.pages)

        for i, p in enumerate(pdf.pages):
            page_no = i + 1
            text = p.extract_text() or ""
            text = text.replace("\x00", "").strip()

            # 抽取表格：对大矩阵很关键（附表4 常跨多页）
            tbls_raw = []
            try:
                tbls_raw = p.extract_tables() or []
            except Exception:
                tbls_raw = []

            tbls = []
            for t in tbls_raw:
                nt = normalize_table(t)
                if nt:
                    tbls.append(nt)

            pe = PageExtract(page_no=page_no, text=text, tables=tbls)

            # OCR：仅在用户勾选，且该页 text 很少时启用（防止浪费）
            if do_ocr and fitz is not None and pytesseract is not None:
                if len(text.strip()) < 30:
                    try:
                        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                        page = doc.load_page(i)
                        pix = page.get_pixmap(dpi=ocr_dpi)
                        img_bytes = pix.tobytes("png")
                        from PIL import Image  # pillow 常见可用
                        img = Image.open(io.BytesIO(img_bytes))
                        ocr_text = pytesseract.image_to_string(img, lang="chi_sim+eng")
                        pe.ocr_text = (ocr_text or "").strip()
                    except Exception:
                        pe.ocr_text = ""

            pages.append(pe)

            # flatten tables
            for ti, t in enumerate(tbls):
                n_rows = len(t)
                n_cols = max(len(r) for r in t) if t else 0
                tables_flat.append(
                    {
                        "page": page_no,
                        "table_index": ti,
                        "n_rows": n_rows,
                        "n_cols": n_cols,
                        "table": t,
                    }
                )

    all_text = "\n\n".join(
        [
            f"---PAGE {pe.page_no}---\n{pe.text}\n{('OCR:'+pe.ocr_text) if pe.ocr_text else ''}".strip()
            for pe in pages
        ]
    )

    meta = {
        "n_pages": len(pages),
        "sha256": sha256_bytes(pdf_bytes),
        "ocr_enabled": bool(do_ocr),
        "ocr_available": bool(fitz is not None and pytesseract is not None),
    }

    return {
        "meta": meta,
        "pages": [pe.__dict__ for pe in pages],
        "all_text": all_text,
        "tables_flat": tables_flat,
    }


# -----------------------------
# 结构化解析：培养目标 / 毕业要求 / 支撑矩阵合并
# -----------------------------
def parse_training_objectives(pages: List[Dict[str, Any]]) -> List[str]:
    """
    解析“培养目标”：通常在第2页附近，格式类似：
      培养目标：目标 1：... 目标 2：... 目标 3：...
    做跨页拼接：只要遇到“培养目标”开始，直到“毕业要求/课程体系/主干学科”等结束。
    """
    full = "\n".join([(p.get("text") or "") + "\n" + (p.get("ocr_text") or "") for p in pages])
    # 找“培养目标”起点
    m = re.search(r"(培养目标[:：]?)", full)
    if not m:
        return []

    start = m.start()
    tail = full[start:]

    # 终止点：毕业要求/主干学科/课程 等
    end_m = re.search(r"(毕业要求[:：]|主干学科[:：]|专业核心课程|课程设置|专业教学计划表)", tail)
    segment = tail if not end_m else tail[: end_m.start()]

    # 提取目标条目
    # 兼容：目标1：/目标 1：/（1）等
    items: List[Tuple[int, str]] = []
    # 先按“目标\d”切
    parts = re.split(r"(目标\s*\d+\s*[:：])", segment)
    if len(parts) <= 1:
        # fallback：按（1）（2）
        parts2 = re.split(r"(\(\s*\d+\s*\)|（\s*\d+\s*）)", segment)
        if len(parts2) > 1:
            cur = ""
            for i in range(1, len(parts2), 2):
                tag = parts2[i]
                content = parts2[i + 1] if i + 1 < len(parts2) else ""
                cur = clean_cell(content)
                if cur:
                    items.append((i, cur))
            return [x[1] for x in items]
        return []

    for i in range(1, len(parts), 2):
        tag = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        num_m = re.search(r"(\d+)", tag)
        num = int(num_m.group(1)) if num_m else 0
        txt = clean_cell(content)
        # 去掉段内可能重复出现的“培养目标”标题
        txt = re.sub(r"^培养目标[:：]?\s*", "", txt)
        if txt:
            items.append((num, txt))

    # 按序
    items.sort(key=lambda x: x[0])
    return [x[1] for x in items]


def parse_graduation_requirements(pages: List[Dict[str, Any]]) -> List[str]:
    """
    解析“毕业要求 1-12 大条”：通常在第2-4页。
    形式常见：
      毕业要求：1 能够... 2 能够... ... 12 ...
    我们做跨页聚合，提取 1..12，每条保留完整段落。
    """
    full = "\n".join([(p.get("text") or "") + "\n" + (p.get("ocr_text") or "") for p in pages])

    # 定位“毕业要求”
    m = re.search(r"毕业要求[:：]?", full)
    if not m:
        return []

    tail = full[m.end() :]

    # 终止点：后续大章节
    end_m = re.search(r"(三、|四、|主干学科|专业核心课程|标准学制|毕业条件|专业教学计划表)", tail)
    segment = tail if not end_m else tail[: end_m.start()]

    # 先统一一下：把“1 能够…”这样的编号放到行首更好分割
    # 常见 PDF 抽取会把换行打乱，这里靠正则尽量分
    # 分割点：(^|非数字)\s*(1|2|...|12)\s
    # 但要避免把 1.1 指标点误切，所以要求后面不是点
    seg = segment
    seg = seg.replace("．", ".").replace("。", "。")

    # 用捕获分割： (编号)(内容)
    parts = re.split(r"(?<!\d)\b(1[0-2]|[1-9])\b(?!\.)\s*", seg)
    # parts 结构：[前导, num1, content1, num2, content2, ...]
    if len(parts) < 3:
        return []

    req_map: Dict[int, str] = {}
    for i in range(1, len(parts), 2):
        try:
            num = int(parts[i])
        except Exception:
            continue
        content = parts[i + 1] if i + 1 < len(parts) else ""
        content = clean_cell(content)
        if not content:
            continue
        # 把下一条编号前的残留截断：split 已处理，这里再做轻微清理
        req_map[num] = content

    # 只保留 1..12，按序输出
    out = []
    for n in range(1, 13):
        if n in req_map:
            out.append(req_map[n])
    return out


def find_support_matrix_tables(tables_flat: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    识别“课程设置对毕业要求支撑关系表（附表4）”：
    特征：列数非常多（通常 >= 25）。
    返回该表跨页的 tables 列表（按页顺序）。
    """
    candidates = []
    for t in tables_flat:
        if t["n_cols"] >= 25 and t["n_rows"] >= 5:
            candidates.append(t)

    # 常见情况：附表4 跨连续页（如 12-16）
    candidates.sort(key=lambda x: (x["page"], x["table_index"]))
    # 取最长的一段连续页
    best = []
    cur = []
    last_page = None
    for c in candidates:
        if last_page is None or c["page"] == last_page or c["page"] == last_page + 1:
            cur.append(c)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [c]
        last_page = c["page"]
    if len(cur) > len(best):
        best = cur

    return best


def merge_support_matrix(tables: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    合并跨页大矩阵：
    - 第一页通常包含多行表头（2行）
    - 后续页常重复表头，需要去重
    """
    if not tables:
        return pd.DataFrame()

    # 先拿第一页判断表头行数：若第2行包含 1.1 / 2.3 这种指标点，则认为是2行表头
    first = tables[0]["table"]
    first = normalize_table(first)
    header_rows = 1
    if len(first) >= 2:
        row2 = " ".join(first[1])
        if re.search(r"\b\d+\.\d+\b", row2):
            header_rows = 2

    dfs = []
    base_header = None

    for idx, t in enumerate(tables):
        raw = normalize_table(t["table"])
        if not raw:
            continue

        # 将该页转换为 df
        df = table_to_df(raw, header_rows=header_rows)

        # 去掉重复表头（后续页可能把表头当数据）
        if base_header is None:
            base_header = list(df.columns)
            dfs.append(df)
        else:
            # 若该页 df 的列完全一致，直接追加
            if list(df.columns) == base_header:
                # 删除“表头重复行”：有时第一页表头被当数据行
                # 简单策略：若第一行像表头（包含"课程"或"毕业要求"或大量 1.1），则删
                if len(df) >= 1:
                    row0 = " ".join([clean_cell(x) for x in df.iloc[0].tolist()])
                    if ("课程" in row0) or ("毕业要求" in row0) or re.search(r"\b\d+\.\d+\b", row0):
                        df = df.iloc[1:].reset_index(drop=True)
                dfs.append(df)
            else:
                # 列不同：尽量对齐（按列名并集）
                df = df.reindex(columns=base_header, fill_value="")
                dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    merged = pd.concat(dfs, axis=0, ignore_index=True)

    # 清理全空行
    merged = merged.loc[~(merged.apply(lambda r: all(clean_cell(x) == "" for x in r), axis=1))].reset_index(drop=True)
    return merged


def build_structured_result(extracted: Dict[str, Any]) -> Dict[str, Any]:
    pages = extracted["pages"]
    objectives = parse_training_objectives(pages)
    grad_reqs = parse_graduation_requirements(pages)

    matrix_tables = find_support_matrix_tables(extracted["tables_flat"])
    matrix_df = merge_support_matrix(matrix_tables) if matrix_tables else pd.DataFrame()

    result = {
        "meta": extracted["meta"],
        "structured": {
            "培养目标": objectives,
            "毕业要求_12条": grad_reqs,
            "支撑矩阵_pages": [t["page"] for t in matrix_tables],
            "支撑矩阵_detected": bool(not matrix_df.empty),
            "支撑矩阵_shape": [int(matrix_df.shape[0]), int(matrix_df.shape[1])] if not matrix_df.empty else [0, 0],
        },
        # 全量原始数据（后续功能增强可直接复用）
        "raw": {
            "pages": extracted["pages"],
            "tables_flat": [
                {k: v for k, v in t.items() if k != "table"} for t in extracted["tables_flat"]
            ],
        },
    }

    # matrix 单独放（避免 JSON 太大；但你也可以存原表）
    result["_matrix_df"] = matrix_df
    result["_matrix_tables_full"] = matrix_tables
    return result


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="培养方案全量抽取（基础底库）", layout="wide")

st.title("培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）")

with st.sidebar:
    st.header("上传与抽取")
    up = st.file_uploader("上传培养方案 PDF", type=["pdf"])

    do_ocr = st.checkbox(
        "对“无文本页”启用 OCR（可选）",
        value=False,
        help="仅当页面抽不出文字且环境具备 OCR 能力时才会尝试。Streamlit Cloud 需系统安装 tesseract。",
    )

    if do_ocr:
        ocr_ok = (fitz is not None) and (pytesseract is not None)
        if not ocr_ok:
            st.warning("当前环境 OCR 不可用（缺 PyMuPDF 或 pytesseract 或系统 tesseract），将自动跳过 OCR。")

    run_btn = st.button("开始全量抽取", type="primary", use_container_width=True)

if not up:
    st.info("请先在左侧上传你的《2024培养方案.pdf》之类的培养方案文件，然后点击“开始全量抽取”。")
    st.stop()

pdf_bytes = up.read()
pdf_hash = sha256_bytes(pdf_bytes)

@st.cache_data(show_spinner=False)
def cached_extract(pdf_hash_key: str, pdf_bytes_key: bytes, do_ocr_key: bool) -> Dict[str, Any]:
    extracted = extract_pdf_all(pdf_bytes_key, do_ocr=do_ocr_key)
    return extracted

@st.cache_data(show_spinner=False)
def cached_build_structured(pdf_hash_key: str, extracted: Dict[str, Any]) -> Dict[str, Any]:
    return build_structured_result(extracted)

if run_btn:
    with st.spinner("正在抽取：全页文本 + 全表格（并尝试结构化解析/矩阵合并）..."):
        extracted = cached_extract(pdf_hash, pdf_bytes, do_ocr)
        structured = cached_build_structured(pdf_hash, extracted)
        st.session_state["extracted"] = extracted
        st.session_state["structured"] = structured

structured = st.session_state.get("structured")
extracted = st.session_state.get("extracted")

if not structured or not extracted:
    st.warning("请点击左侧“开始全量抽取”。")
    st.stop()

matrix_df: pd.DataFrame = structured.get("_matrix_df", pd.DataFrame())

# 概览区
meta = structured["meta"]
s = structured["structured"]

colA, colB, colC, colD = st.columns([1, 1, 1, 2])
with colA:
    st.metric("总页数", meta.get("n_pages", 0))
with colB:
    st.metric("表格总数", len(extracted.get("tables_flat", [])))
with colC:
    st.metric("OCR启用", "是" if meta.get("ocr_enabled") else "否")
with colD:
    st.caption(f"SHA256: {meta.get('sha256')}")

tabs = st.tabs(["概览与下载", "培养目标", "毕业要求(12条)", "支撑矩阵(合并)", "原始页(文本/表格)"])

with tabs[0]:
    st.subheader("结构化识别结果（可先在这里核对）")
    st.write("**培养目标条数：**", len(s.get("培养目标", [])))
    st.write("**毕业要求条数：**", len(s.get("毕业要求_12条", [])))
    st.write("**支撑矩阵页：**", s.get("支撑矩阵_pages", []))
    st.write("**支撑矩阵合并结果：**", s.get("支撑矩阵_shape", [0, 0]))

    # JSON 下载：包含“全量元数据 + pages文本/表格索引 + 结构化解析”
    json_blob = json.dumps(
        {
            "meta": structured["meta"],
            "structured": structured["structured"],
            "raw": structured["raw"],
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    st.download_button(
        "下载抽取结果 JSON（全量底库索引）",
        data=json_blob,
        file_name=f"培养方案抽取_{pdf_hash[:10]}.json",
        mime="application/json",
        use_container_width=True,
    )

    # matrix excel 下载
    if matrix_df is not None and not matrix_df.empty:
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            matrix_df.to_excel(writer, index=False, sheet_name="支撑矩阵_合并")
        st.download_button(
            "下载支撑矩阵 Excel（合并后）",
            data=out.getvalue(),
            file_name=f"支撑矩阵_{pdf_hash[:10]}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    else:
        st.warning("未检测到/未能合并支撑矩阵（附表4）。你可以在“原始页”里检查对应页的表格是否被抽取到。")

with tabs[1]:
    st.subheader("培养目标（跨页解析）")
    objs = s.get("培养目标", [])
    if not objs:
        st.warning("未解析到培养目标。建议到“原始页”核对：是否 PDF 抽取文本异常，或标题写法不同。")
    else:
        for i, t in enumerate(objs, 1):
            st.markdown(f"**目标{i}：** {t}")

with tabs[2]:
    st.subheader("毕业要求（1–12条，跨页解析）")
    reqs = s.get("毕业要求_12条", [])
    if not reqs:
        st.warning("未解析到毕业要求。建议到“原始页”核对第2–4页文本是否完整。")
    else:
        if len(reqs) != 12:
            st.warning(f"解析到 {len(reqs)} 条，理论应为 12 条。请到“原始页”核对第2–4页原文，并据此调整正则/规则。")
        for i, t in enumerate(reqs, 1):
            st.markdown(f"**{i}.** {t}")

with tabs[3]:
    st.subheader("课程设置对毕业要求支撑关系表（附表4）— 自动合并跨页")
    if matrix_df is None or matrix_df.empty:
        st.warning("矩阵未合并出来。请到“原始页”查看第12–16页是否存在宽表（列数>=25），若存在但未被识别，可把识别规则再放宽。")
    else:
        st.caption(f"合并结果：{matrix_df.shape[0]} 行 × {matrix_df.shape[1]} 列")
        st.dataframe(matrix_df, use_container_width=True, height=520)

with tabs[4]:
    st.subheader("原始页（文本 + 表格）核对区")
    pages = extracted.get("pages", [])
    page_no = st.number_input("选择页码", min_value=1, max_value=len(pages), value=1, step=1)
    p = pages[page_no - 1]
    st.markdown(f"### 第 {page_no} 页")

    text_show = (p.get("text") or "").strip()
    ocr_show = (p.get("ocr_text") or "").strip()

    st.markdown("#### 文本抽取")
    if text_show:
        st.text_area("page_text", value=text_show, height=240)
    else:
        st.info("该页未抽取到文本。")
    if ocr_show:
        st.markdown("#### OCR 结果（可选）")
        st.text_area("page_ocr", value=ocr_show, height=200)

    st.markdown("#### 表格抽取")
    tbls = p.get("tables", []) or []
    st.write(f"该页表格数：**{len(tbls)}**")
    for ti, tbl in enumerate(tbls):
        st.markdown(f"**表 {ti+1}**（{len(tbl)} 行 × {max(len(r) for r in tbl) if tbl else 0} 列）")
        # 尝试判断是否多行表头
        header_rows = 2 if (len(tbl) >= 2 and re.search(r"\b\d+\.\d+\b", " ".join(tbl[1]))) else 1
        df = table_to_df(tbl, header_rows=header_rows)
        st.dataframe(df, use_container_width=True, height=240)
