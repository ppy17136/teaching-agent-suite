# -*- coding: utf-8 -*-
"""培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）

你关心的点：
- 毕业要求必须完整（1~12 + 1.1/1.2…）
- 三~六等章节大标题内容要完整显示
- 附表 1~5 的表名要显示，并尽可能把表格抽取出来
- 表格中合并单元格导致的空白要尽量补全
- 焊接/无损检测两方向要尽量在展示与导出里区分

实现策略（不依赖大模型）：
- 使用 pdfplumber 抽取每页文本（分页原文可溯源）
- 用规则解析“培养目标/毕业要求/章节内容”
- 用 pdfplumber 线框策略抽取表格（无需 camelot/ghostscript）
- 对表格做“行长度对齐 + 空列剔除 + 合并格常见空白填充 + 方向推断”
- 提供 JSON / CSV(zip) / Excel 三种导出

备注：OCR 开关保留，但 Streamlit Cloud 若未安装 OCR 依赖将自动降级为仅文本抽取。
"""

from __future__ import annotations

import io
import json
import re
import hashlib
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

try:
    import pdfplumber
except Exception as e:  # pragma: no cover
    pdfplumber = None

# -----------------------------
# Utilities
# -----------------------------

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def clean_text(s: Any) -> str:
    if s is None:
        return ""
    return str(s).replace("\u00a0", " ").strip()


def is_empty(s: Any) -> bool:
    return clean_text(s) == ""


def safe_int(x: str, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def normalize_lines(text: str) -> List[str]:
    lines = [clean_text(x) for x in (text or "").splitlines()]
    return [x for x in lines if x]


# -----------------------------
# Text extraction
# -----------------------------


def ocr_page_fallback(page) -> str:
    """
    可选 OCR：仅当页面无可提取文本且用户勾选时尝试。
    - 若环境缺少 pytesseract / PIL / tesseract / ImageMagick，将安全降级返回空字符串。
    """
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return ""
    try:
        im = page.to_image(resolution=200).original
        if isinstance(im, Image.Image):
            return pytesseract.image_to_string(im, lang="chi_sim+eng") or ""
        return ""
    except Exception:
        return ""


def extract_pages_text(pdf_bytes: bytes, use_ocr: bool = False) -> List[str]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber 未安装，无法解析 PDF")

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = []
        for p in pdf.pages:
            pages.append(p.extract_text() or "")
        return pages


# -----------------------------
# Section parsing (rule-based)
# -----------------------------

CHAPTER_KEYS = [
    ("一", "培养目标"),
    ("二", "毕业要求"),
    ("三", "专业定位与特色"),
    ("四", "主干学科、专业核心课程和主要实践性教学环节"),
    ("五", "标准学制与授予学位"),
    ("六", "毕业条件"),
    ("七", "专业教学计划表"),
    ("八", "学分统计表"),
    ("九", "教学进程表"),
    ("十", "课程设置对毕业要求支撑关系表"),
    ("十一", "课程设置逻辑思维导图"),
]


def find_first_index(pages_text: List[str], pattern: str) -> Optional[int]:
    for i, t in enumerate(pages_text):
        if re.search(pattern, t):
            return i
    return None


def concat_pages(pages_text: List[str], start: int, end: int) -> str:
    start = max(0, start)
    end = min(len(pages_text), end)
    return "\n".join(pages_text[start:end])


def locate_chapter_ranges(pages_text: List[str]) -> Dict[str, Tuple[int, int]]:
    """Return {chapter_name: (start_page_idx, end_page_idx_exclusive)}"""
    # locate page indices by chapter headings
    hits: List[Tuple[int, str]] = []
    for cn, title in CHAPTER_KEYS:
        # match like "三、专业定位与特色" or "三 专业定位与特色"
        pat = rf"{cn}[、\s]+{re.escape(title)}"
        idx = find_first_index(pages_text, pat)
        if idx is not None:
            hits.append((idx, title))

    # sort by page
    hits.sort(key=lambda x: x[0])
    ranges: Dict[str, Tuple[int, int]] = {}
    for k, (idx, title) in enumerate(hits):
        end = hits[k + 1][0] if k + 1 < len(hits) else len(pages_text)
        ranges[title] = (idx, end)
    return ranges


def parse_training_objectives(section_text: str) -> List[str]:
    """Parse 培养目标 items like "1." or "（1）" or "1）"."""
    text = section_text or ""
    # Try common patterns
    lines = normalize_lines(text)
    items: List[str] = []

    buf = []
    cur_id = None

    def flush():
        nonlocal buf, cur_id
        if cur_id is not None and buf:
            items.append("".join(buf).strip())
        buf = []
        cur_id = None

    for ln in lines:
        m = re.match(r"^\s*(\d+)[\.、]\s*(.+)$", ln)
        m2 = re.match(r"^\s*[（(]\s*(\d+)\s*[）)]\s*(.+)$", ln)
        if m or m2:
            flush()
            cur_id = (m or m2).group(1)
            buf = [f"{cur_id}. {(m or m2).group(2).strip()} "]
        else:
            if cur_id is None:
                continue
            buf.append(ln.strip() + " ")

    flush()

    # 如果一个都没抓到，退化：取“培养目标”下面的段落（但做分句）
    if not items:
        text2 = re.sub(r"\s+", " ", text).strip()
        if text2:
            items = [x.strip() for x in re.split(r"[；;]\s*", text2) if x.strip()]
    return items


def parse_graduation_requirements(section_text: str) -> Dict[str, Any]:
    """Parse 毕业要求 1~12 and sub-items 1.1/1.2..."""
    lines = normalize_lines(section_text)
    out: Dict[str, Any] = {}

    cur_main = None
    cur_sub = None

    def ensure_main(mid: str, title: str = ""):
        if mid not in out:
            out[mid] = {"title": title, "text": "", "subs": {}}

    for ln in lines:
        # main: "1. 工程知识：..." 允许冒号中英文
        m = re.match(r"^\s*(\d{1,2})[\.、]\s*([^：:]+)[：:]\s*(.*)$", ln)
        if m:
            cur_main = m.group(1)
            cur_sub = None
            ensure_main(cur_main, clean_text(m.group(2)))
            tail = clean_text(m.group(3))
            if tail:
                out[cur_main]["text"] = (out[cur_main]["text"] + " " + tail).strip()
            continue

        # sub: "1.1 能够..." or "10.2 ..."
        m2 = re.match(r"^\s*(\d{1,2}\.\d{1,2})\s+(.+)$", ln)
        if m2:
            cur_sub = m2.group(1)
            mid = cur_sub.split(".")[0]
            ensure_main(mid)
            out[mid]["subs"][cur_sub] = clean_text(m2.group(2))
            continue

        # continuation lines
        if cur_sub:
            mid = cur_sub.split(".")[0]
            out[mid]["subs"][cur_sub] = (out[mid]["subs"][cur_sub] + " " + ln).strip()
        elif cur_main:
            out[cur_main]["text"] = (out[cur_main]["text"] + " " + ln).strip()

    return out


def parse_chapter_content(pages_text: List[str], chapter_ranges: Dict[str, Tuple[int, int]]) -> Dict[str, str]:
    wanted = [
        "专业定位与特色",
        "主干学科、专业核心课程和主要实践性教学环节",
        "标准学制与授予学位",
        "毕业条件",
    ]
    out: Dict[str, str] = {}
    for w in wanted:
        if w in chapter_ranges:
            s, e = chapter_ranges[w]
            out[w] = concat_pages(pages_text, s, e).strip()
        else:
            out[w] = ""
    return out


def extract_appendix_title_map(pages_text: List[str]) -> Dict[str, str]:
    """Try to extract mapping like 附表1->七 专业教学计划表"""
    whole = "\n".join(pages_text)
    # match "七专业教学计划表（附表1）" or "七、专业教学计划表（附表1）"
    mp: Dict[str, str] = {}
    for cn, title in CHAPTER_KEYS:
        if cn not in ["七", "八", "九", "十", "十一"]:
            continue
        # Try to locate the line containing appendix
        pat = rf"{cn}[、\s]*{re.escape(title)}\s*[（(]\s*(附表\s*\d+)\s*[）)]"
        for m in re.finditer(pat, whole):
            key = clean_text(m.group(1)).replace(" ", "")
            mp[key] = f"{cn}、{title}（{key}）"
    return mp


# -----------------------------
# Table extraction (pdfplumber)
# -----------------------------

PDFPLUMBER_TABLE_SETTINGS_LINES = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "edge_min_length": 3,
    "intersection_tolerance": 3,
    "text_tolerance": 3,
}

# Fallback for borderless tables (common in some teaching-plan PDFs)
PDFPLUMBER_TABLE_SETTINGS_TEXT = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "edge_min_length": 3,
    "intersection_tolerance": 3,
    "text_tolerance": 3,
    "min_words_vertical": 3,
    "min_words_horizontal": 1,
}


def normalize_table(raw_table: List[List[Any]]) -> Optional[List[List[str]]]:
    """Normalize: strip, drop empty rows, pad rows, drop empty columns."""
    if not raw_table:
        return None
    rows: List[List[str]] = []
    max_cols = 0
    for r in raw_table:
        if r is None:
            continue
        rr = [clean_text(c) for c in r]
        if all(c == "" for c in rr):
            continue
        rows.append(rr)
        max_cols = max(max_cols, len(rr))

    if not rows or max_cols == 0:
        return None

    # pad
    for i in range(len(rows)):
        if len(rows[i]) < max_cols:
            rows[i] += [""] * (max_cols - len(rows[i]))

    # drop empty columns
    keep_cols = []
    for j in range(max_cols):
        col = [rows[i][j] for i in range(len(rows))]
        if any(c != "" for c in col):
            keep_cols.append(j)

    if not keep_cols:
        return None
    out = [[row[j] for j in keep_cols] for row in rows]
    return out


def ffill_merged_cells(table: List[List[str]]) -> List[List[str]]:
    """
    对表格中的“合并单元格导致的空白”做尽量安全的补全。

    规则：
    - 对“课程体系/类别/模块/性质/方向/学期/学年/环节”等典型合并列：强制向下填充（ffill）
    - 其余列：仅在该列空白比例很高时才向下填充（避免误填）
    - 对于明显的表头区域（前几行）：不做填充
    """
    if not table:
        return table

    head_window = min(6, len(table))
    head_idx = max(range(head_window), key=lambda i: sum(1 for x in table[i] if clean_text(x)))
    headers = [clean_text(x) for x in table[head_idx]]

    ncol = max(len(r) for r in table)
    out = [list(r) + [""] * (ncol - len(r)) for r in table]

    def _is_force_col(h: str) -> bool:
        h = h or ""
        keys = [
            "课程体系", "体系", "类别", "课程类别", "模块", "课程模块",
            "性质", "课程性质", "类型", "学期", "学年",
            "方向", "专业方向", "环节", "教学环节", "实践",
            "通识", "学科基础", "专业教育", "集中性实践",
        ]
        return any(k in h for k in keys)

    force_cols = {j for j, h in enumerate(headers) if _is_force_col(h)}

    data_rows = out[head_idx + 1 :]
    if not data_rows:
        return out

    empty_ratio = []
    for j in range(ncol):
        empties = sum(1 for r in data_rows if not clean_text(r[j] if j < len(r) else ""))
        empty_ratio.append(empties / max(1, len(data_rows)))

    fill_cols = set(force_cols) | {j for j, ratio in enumerate(empty_ratio) if ratio >= 0.55}

    last = [""] * ncol
    for i in range(head_idx + 1, len(out)):
        row = out[i]
        for j in range(ncol):
            v = clean_text(row[j] if j < len(row) else "")
            if j in fill_cols:
                if v:
                    last[j] = v
                else:
                    row[j] = last[j]
        out[i] = row

    return out


def table_to_df(t: ExtractedTable) -> pd.DataFrame:
    def _cell_to_str(x: Any) -> str:
        if x is None:
            return ""
        # pdfplumber sometimes yields non-str objects; stringify everything
        try:
            s = str(x)
        except Exception:
            s = ""
        return s.replace("\r", "").strip()

    def _make_unique_columns(cols_in: List[str]) -> List[str]:
        # Streamlit uses pyarrow under the hood; duplicate column names will crash.
        seen: Dict[str, int] = {}
        out: List[str] = []
        for c in cols_in:
            base = c
            if base in seen:
                seen[base] += 1
                out.append(f"{base}_{seen[base]}")
            else:
                seen[base] = 1
                out.append(base)
        return out

    cols_raw = ["" if c is None else str(c).strip() for c in (t.columns or [])]
    rows_raw = t.rows or []

    # robust align lengths
    max_len = max([len(cols_raw)] + [len(r) for r in rows_raw] + [0])
    cols_norm: List[str] = []
    for i in range(max_len):
        name = cols_raw[i] if i < len(cols_raw) else ""
        name = re.sub(r"\s+", " ", (name or "").strip())
        if not name:
            name = f"col_{i+1}"
        cols_norm.append(name)
    cols_norm = _make_unique_columns(cols_norm)

    fixed_rows: List[List[str]] = []
    for r in rows_raw:
        rr = [ _cell_to_str(x) for x in (r or []) ]
        if len(rr) < max_len:
            rr = rr + [""] * (max_len - len(rr))
        elif len(rr) > max_len:
            rr = rr[:max_len]
        fixed_rows.append(rr)

    # force all-string dataframe to avoid pyarrow dtype issues
    df = pd.DataFrame(fixed_rows, columns=cols_norm)
    return df.astype("string")


def make_tables_zip(tables: List[ExtractedTable]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, t in enumerate(tables, start=1):
            df = table_to_df(t)
            safe_name = re.sub(r"[\\/:*?\"<>|]", "_", t.title)[:60]
            filename = f"{idx:02d}_{safe_name}_P{t.page}.csv"
            zf.writestr(filename, df.to_csv(index=False))
    return buf.getvalue()


def make_tables_excel(tables: List[ExtractedTable]) -> bytes:
    buf = io.BytesIO()
    # Prefer xlsxwriter, fallback openpyxl
    engine = "xlsxwriter"
    try:
        import xlsxwriter  # noqa: F401
    except Exception:
        engine = "openpyxl"

    with pd.ExcelWriter(buf, engine=engine) as writer:
        for i, t in enumerate(tables, start=1):
            df = table_to_df(t)
            # sheet name length limit 31
            name = re.sub(r"[\\/:*?\[\]]", "_", t.appendix or f"表{i}")
            name = name[:28]  # keep room
            sheet = f"{name}_{i}" if len(name) <= 20 else name
            sheet = sheet[:31]
            df.to_excel(writer, sheet_name=sheet, index=False)
    return buf.getvalue()



def extract_headings_all(pages_text: List[str]) -> List[str]:
    """
    粗略提取全文中的“章节大标题”，用于显示/校对/LLM补漏。
    """
    out: List[str] = []
    pat = re.compile(r"^(第[一二三四五六七八九十]+[章部分节]|[一二三四五六七八九十]+[、\.．]|\d+\))\s*.+$")
    for t in pages_text:
        for raw in (t or "").splitlines():
            line = clean_text(raw)
            if not line:
                continue
            if pat.match(line):
                out.append(line)
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq



# -----------------------------
# LLM 校对与修正（可选）
# -----------------------------

def _safe_json_load(s: str) -> Optional[dict]:
    if not s:
        return None
    s = s.strip()
    if "{" in s and "}" in s:
        s = s[s.find("{") : s.rfind("}") + 1]
    try:
        return json.loads(s)
    except Exception:
        return None


def llm_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[dict],
    temperature: float = 0.0,
    timeout: int = 60,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": float(temperature)}
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or ""


def extract_between_markers(
    text: str,
    start_markers: List[str],
    end_markers: List[str],
    max_chars: int = 12000,
) -> str:
    t = text or ""
    if not t.strip():
        return ""
    t2 = t.replace("\u3000", " ").replace("\xa0", " ")
    start = 0
    for s in start_markers:
        p = t2.find(s)
        if p != -1:
            start = p
            break
    end = min(len(t2), start + max_chars)
    for e in end_markers:
        p = t2.find(e, start + 10)
        if p != -1:
            end = min(end, p)
            break
    return t2[start:end].strip()


def refine_with_llm(result: Dict[str, Any], llm_cfg: dict) -> Dict[str, Any]:
    base_url = (llm_cfg.get("base_url") or "").strip()
    api_key = (llm_cfg.get("api_key") or "").strip()
    model = (llm_cfg.get("model") or "").strip()
    temperature = float(llm_cfg.get("temperature", 0.0) or 0.0)

    if not (base_url and api_key and model):
        return result

    pages_text = result.get("pages_text") or []
    full_text = "\n".join(pages_text)

    obj_raw = extract_between_markers(
        full_text,
        start_markers=["培养目标", "一、培养目标", "（一）培养目标"],
        end_markers=["毕业要求", "二、毕业要求", "专业定位", "三、专业定位"],
        max_chars=8000,
    )
    grad_raw = extract_between_markers(
        full_text,
        start_markers=["毕业要求", "二、毕业要求"],
        end_markers=["专业教学计划表", "七", "附表", "专业教学计划", "三、专业定位"],
        max_chars=20000,
    )

    headings_raw = "\n".join((result.get("headings_all") or [])[:200])

    tables = result.get("tables_data", [])
    table_briefs = []
    for i, t in enumerate(tables[:10]):
        cols = [clean_text(x) for x in (t.get("columns") or [])][:30]
        rows = t.get("rows") or []
        sample_rows = [[clean_text(c) for c in r[: min(len(r), 12)]] for r in rows[:4]]
        table_briefs.append(
            {
                "id": i,
                "appendix": t.get("appendix", ""),
                "title": t.get("appendix_title", "") or t.get("title", ""),
                "page": t.get("page", None),
                "columns": cols,
                "sample_rows": sample_rows,
            }
        )

    sys = {"role": "system", "content": "你是高校培养方案PDF解析与纠错助手。你只输出严格JSON，不要输出多余文字。"}
    user = {
        "role": "user",
        "content": json.dumps(
            {
                "task": "校对并补全培养方案结构化信息。输出应尽量与原文一致，避免臆造。",
                "inputs": {
                    "obj_raw": obj_raw,
                    "grad_raw": grad_raw,
                    "headings_raw": headings_raw,
                    "appendix_map": result.get("appendix_map", {}),
                    "tables": table_briefs,
                },
                "output_schema": {
                    "training_objectives": ["..."],
                    "graduation_requirements": [
                        {"no": 1, "title": "工程知识", "text": "...", "subs": [{"code": "1.1", "text": "..." }]}
                    ],
                    "headings_all": ["一、...", "二、...", "三、..."],
                    "tables": [{"id": 0, "appendix": "附表1", "title": "七 专业教学计划表", "direction": "焊接+无损检测"}],
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
    }

    try:
        content = llm_chat(base_url, api_key, model, [sys, user], temperature=temperature)
    except Exception:
        return result

    patch = _safe_json_load(content)
    if not patch:
        return result

    out = dict(result)

    if isinstance(patch.get("training_objectives"), list) and patch["training_objectives"]:
        out["training_objectives"] = [clean_text(x) for x in patch["training_objectives"] if clean_text(x)]

    gr = patch.get("graduation_requirements")
    if isinstance(gr, list) and len(gr) >= 10:
        gr_dict = {}
        for item in gr:
            try:
                no = int(item.get("no"))
            except Exception:
                continue
            title = clean_text(item.get("title", "")) or f"{no}"
            text = clean_text(item.get("text", ""))
            subs = item.get("subs") or []
            items: List[str] = []
            if text:
                items.append(text)
            if isinstance(subs, list):
                for s in subs:
                    code = clean_text((s or {}).get("code", ""))
                    txt = clean_text((s or {}).get("text", ""))
                    if not txt:
                        continue
                    items.append(f"{code} {txt}".strip() if code else txt)
            gr_dict[str(no)] = {"name": title, "items": items}
        ok_cnt = sum(1 for i in range(1, 13) if str(i) in gr_dict)
        if ok_cnt >= 10:
            out["graduation_requirements"] = gr_dict

    hs = patch.get("headings_all")
    if isinstance(hs, list) and len(hs) >= 6:
        out["headings_all"] = [clean_text(x) for x in hs if clean_text(x)]

    tpatch = patch.get("tables")
    if isinstance(tpatch, list) and out.get("tables_data"):
        tables2 = list(out["tables_data"])
        for tp in tpatch:
            try:
                i = int(tp.get("id"))
            except Exception:
                continue
            if 0 <= i < len(tables2):
                if clean_text(tp.get("appendix", "")):
                    tables2[i]["appendix"] = clean_text(tp.get("appendix", ""))
                if clean_text(tp.get("title", "")):
                    tables2[i]["appendix_title"] = clean_text(tp.get("title", ""))
                if clean_text(tp.get("direction", "")):
                    tables2[i]["direction"] = clean_text(tp.get("direction", ""))
        out["tables_data"] = tables2

    out.setdefault("meta", {})
    out["meta"]["llm_refined"] = True
    out["meta"]["llm_model"] = model
    return out


# -----------------------------
# Full extraction pipeline
# -----------------------------


def run_full_extract(pdf_bytes: bytes, use_ocr: bool = False, llm_cfg: Optional[dict] = None) -> Dict[str, Any]:
    pages_text = extract_pages_text(pdf_bytes, use_ocr=use_ocr)
    chapter_ranges = locate_chapter_ranges(pages_text)

    # training objectives
    if "培养目标" in chapter_ranges:
        s, e = chapter_ranges["培养目标"]
        obj_text = concat_pages(pages_text, s, e)
    else:
        obj_text = "\n".join(pages_text)
    training_objectives = parse_training_objectives(obj_text)

    # graduation requirements
    grad_text = ""
    if "毕业要求" in chapter_ranges:
        s, e = chapter_ranges["毕业要求"]
        grad_text = concat_pages(pages_text, s, e)
    graduation_requirements = parse_graduation_requirements(grad_text)

    # chapters 3-6 content
    chapter_content = parse_chapter_content(pages_text, chapter_ranges)

    # appendix map
    appendix_map = extract_appendix_title_map(pages_text)

    # tables
    tables = extract_tables_pdfplumber(pdf_bytes, pages_text)

    headings_all = extract_headings_all(pages_text)

    result = {
        "meta": {
            "sha256": sha256_bytes(pdf_bytes),
            "pages": len(pages_text),
            "tables": len(tables),
        },
        "chapter_ranges": {k: [v[0] + 1, v[1]] for k, v in chapter_ranges.items()},
        "appendix_map": appendix_map,
        "training_objectives": training_objectives,
        "graduation_requirements": graduation_requirements,
        "chapter_content": chapter_content,
        "pages_text": pages_text,
        "full_text": "\n".join(pages_text),
        "headings_all": headings_all,
        "tables_data": [
            {
                "appendix": t.appendix,
                "appendix_title": t.appendix_title,
                "page": t.page,
                "title": t.title,
                "direction": t.direction,
                "columns": t.columns,
                "rows": t.rows,
            }
            for t in tables
        ],
    }
    # LLM 可选校对与修正
    if llm_cfg and llm_cfg.get('enabled'):
        try:
            result = refine_with_llm(result, llm_cfg)
        except Exception:
            pass

    return result


# -----------------------------
# Streamlit UI
# -----------------------------

st.set_page_config(page_title="培养方案PDF全量抽取", layout="wide")

st.title("培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）")

with st.sidebar:
    st.markdown("## 上传与抽取")
    up = st.file_uploader("上传培养方案 PDF", type=["pdf"], accept_multiple_files=False)
    use_ocr = st.checkbox("对无文本页启用 OCR（可选）", value=False, help="若部署环境无 OCR 依赖，将自动降级")

    st.markdown("## LLM 校对（可选）")
    enable_llm = st.checkbox(
        "启用 LLM 校对与修正（推荐）",
        value=False,
        help="用于补全培养目标/毕业要求/大标题，以及附表表名与方向；不启用也可正常抽取。",
    )
    llm_cfg = {"enabled": False}

    if enable_llm:
        with st.expander("LLM 配置", expanded=True):
            base_url = st.text_input(
                "Base URL（OpenAI兼容）",
                value=st.secrets.get("LLM_BASE_URL", ""),
                placeholder="例如：https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
            model = st.text_input(
                "Model",
                value=st.secrets.get("LLM_MODEL", "qwen-turbo"),
                placeholder="例如：qwen-plus / qwen-max / deepseek-chat 等",
            )
            api_key = st.text_input(
                "API Key",
                value=st.secrets.get("LLM_API_KEY", ""),
                type="password",
                placeholder="从 secrets 或此处输入",
            )
            temperature = st.slider("温度（越低越稳定）", 0.0, 1.0, 0.0, 0.05)
            llm_cfg = {
                "enabled": True,
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
                "temperature": temperature,
            }

    run_btn = st.button("开始全量抽取", type="primary", disabled=up is None)


if "result" not in st.session_state:
    st.session_state["result"] = None

if up is not None:
    pdf_bytes = up.getvalue()
    file_hash = sha256_bytes(pdf_bytes)[:12]
else:
    pdf_bytes = b""
    file_hash = ""

if run_btn and up is not None:
    with st.spinner("正在抽取全文与表格，请稍等…"):
        res = run_full_extract(pdf_bytes, use_ocr=use_ocr, llm_cfg=llm_cfg)
        st.session_state["result"] = res

res = st.session_state.get("result")

if not res:
    st.info("请先在左侧上传培养方案 PDF，然后点击“开始全量抽取”。")
    st.stop()

# Summary row
c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 3])
with c1:
    st.metric("总页数", res["meta"]["pages"])
with c2:
    st.metric("表格总数", res["meta"]["tables"])
with c3:
    st.metric("OCR启用", "是" if use_ocr else "否")
with c4:
    st.caption(f"SHA256: {res['meta']['sha256']}")

# Tabs
TAB_NAMES = [
    "概览与下载",
    "章节大标题（全部）",
    "培养目标",
    "毕业要求（12条）",
    "附表表格",
    "分页原文（溯源）",
]


tabs = st.tabs(TAB_NAMES)

# 1) 概览与下载
with tabs[0]:
    st.subheader("结构化识别结果（可先在这里校对）")
    if res.get("meta", {}).get("llm_refined"):
        st.success(f"已启用 LLM 校对：{res.get('meta', {}).get('llm_model', '')}")

    # quick counts
    st.write(
        {
            "培养目标条数": len(res.get("training_objectives", [])),
            "毕业要求大项数": len(res.get("graduation_requirements", {})),
            "附表标题映射": res.get("appendix_map", {}),
        }
    )

    # downloads
    json_bytes = json.dumps(res, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "下载抽取结果 JSON（全量基础库）",
        data=json_bytes,
        file_name=f"培养方案抽取_{file_hash}.json",
        mime="application/json",
        use_container_width=True,
    )

    # tables downloads
    if res["tables_data"]:
        # rebuild ExtractedTable list for export
        tables_obj = [
            ExtractedTable(
                appendix=t.get("appendix", ""),
                appendix_title=t.get("appendix_title", ""),
                page=int(t.get("page", 0)),
                title=t.get("title", ""),
                columns=t.get("columns", []),
                rows=t.get("rows", []),
                direction=t.get("direction", ""),
            )
            for t in res["tables_data"]
        ]

        zip_bytes = make_tables_zip(tables_obj)
        st.download_button(
            "下载附表表格 CSV（zip）",
            data=zip_bytes,
            file_name=f"附表表格_{file_hash}.zip",
            mime="application/zip",
            use_container_width=True,
        )

        try:
            xlsx_bytes = make_tables_excel(tables_obj)
            st.download_button(
                "下载附表表格 Excel（xlsx）",
                data=xlsx_bytes,
                file_name=f"附表表格_{file_hash}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except Exception as e:
            st.warning(f"Excel 导出失败：{e}")
    else:
        st.warning("未检测到表格。若 PDF 为扫描件或线框不明显，表格识别可能失败。")

# 2) 章节大标题
with tabs[1]:
    st.subheader("三~六 章节内容（原文拼接，可溯源）")
    chap = res.get("chapter_content", {})
    for k, v in chap.items():
        st.markdown(f"### {k}")
        if v:
            st.text_area("", value=v, height=220, key=f"chap_{k}")
        else:
            st.info("未在 PDF 中定位到该章节标题（可能格式不一致）。")

# 3) 培养目标
with tabs[2]:
    st.subheader("培养目标（可编辑/校对）")
    objs = res.get("training_objectives", [])
    if not objs:
        st.warning("未解析到培养目标条目。可在“分页原文”里确认 PDF 文本是否可提取。")
    else:
        for i, item in enumerate(objs, start=1):
            st.markdown(f"**{i}.** {item}")

# 4) 毕业要求
with tabs[3]:
    st.subheader("毕业要求（应为 12 大条 + 子项）")
    gr = res.get("graduation_requirements", {})
    if not gr:
        st.warning("未解析到毕业要求。")
    else:
        # order by numeric
        keys = sorted(gr.keys(), key=lambda x: safe_int(x, 999))
        for k in keys:
            item = gr[k]
            st.markdown(f"### {k}. {item.get('title','')}")
            if item.get("text"):
                st.write(item["text"])
            subs = item.get("subs", {})
            if subs:
                for sk in sorted(subs.keys(), key=lambda x: [safe_int(p) for p in x.split(".")]):
                    st.markdown(f"- **{sk}** {subs[sk]}")

# 5) 附表表格
with tabs[4]:
    st.subheader("附表表格（表名 + 方向尽量清晰）")

    tables_data = res.get("tables_data", [])
    if not tables_data:
        st.info("未检测到表格。")
    else:
        # group by appendix
        by_app: Dict[str, List[ExtractedTable]] = {}
        for t in tables_data:
            obj = ExtractedTable(
                appendix=t.get("appendix", ""),
                appendix_title=t.get("appendix_title", ""),
                page=int(t.get("page", 0)),
                title=t.get("title", ""),
                columns=t.get("columns", []),
                rows=t.get("rows", []),
                direction=t.get("direction", ""),
            )
            key = obj.appendix or "未分类"
            by_app.setdefault(key, []).append(obj)

        # tabs per appendix
        app_keys = list(by_app.keys())
        # order: 附表1..附表5, then others
        def app_sort(k: str) -> int:
            m = re.search(r"(\d+)", k)
            if m:
                return safe_int(m.group(1), 99)
            return 99

        app_keys = sorted(app_keys, key=app_sort)
        app_tabs = st.tabs(app_keys)

        for tab_key, app_tab in zip(app_keys, app_tabs):
            with app_tab:
                lst = sorted(by_app[tab_key], key=lambda x: (x.page, x.title))
                for i, t in enumerate(lst, start=1):
                    st.markdown(f"#### {t.title}")
                    if t.direction:
                        st.caption(f"方向（推断）：{t.direction}")
                    df = table_to_df(t)
                    # Streamlit uses PyArrow for rendering; some edge cases (e.g., duplicate cols / odd dtypes)
                    # may still fail. We already normalize to strings & unique cols, but keep a safe fallback.
                    try:
                        st.dataframe(df, use_container_width=True, hide_index=True)
                    except Exception:
                        st.warning("该表格渲染遇到兼容性问题，已退回为文本表格显示。")
                        st.markdown(df.to_markdown(index=False))

# 6) 分页原文
with tabs[5]:
    st.subheader("分页原文（用于溯源/调试抽取缺失）")
    pages = res.get("pages_text", [])
    for i, txt in enumerate(pages, start=1):
        with st.expander(f"第{i}页文本", expanded=(i == 1)):
            st.text(txt)
