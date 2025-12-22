# -*- coding: utf-8 -*-
"""
Teaching Agent Suite (Training Plan Extractor)
Robust, full-content extraction for Chinese "培养方案" PDFs:
- Chapters/sections (一/二/三…)
- 培养目标 (list)
- 毕业要求 (1–12, with subitems)
- All appendices/tables (附表1–5 etc.) with best-effort merged-cell filling
- Optional LLM "校对与修正" layer (OpenAI-compatible endpoint)

This file is designed to be a drop-in Streamlit app.py.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

try:
    import pdfplumber  # type: ignore
except Exception as e:
    pdfplumber = None

try:
    import requests  # type: ignore
except Exception:
    requests = None


# -----------------------------
# Text utils
# -----------------------------
_CN_NUM = {
    "零":0,"〇":0,"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,
    "十":10,"十一":11,"十二":12,"十三":13,"十四":14,"十五":15,"十六":16,"十七":17,"十八":18,"十九":19,"二十":20
}

def clean_text(s: Any) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u3000", " ")  # full-width space
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def normalize_for_match(s: str) -> str:
    """Aggressive normalize for heading matching: remove whitespace & punctuation."""
    s = clean_text(s)
    # keep Chinese/letters/digits only
    s = re.sub(r"[\s·•●■◆◇★☆※\-\—–_~`!！?？:：;；,，。\.（）\(\)\[\]【】{}<>《》“”\"'’‘/\\|]+", "", s)
    return s

def normalize_lines(text: str) -> List[str]:
    text = text or ""
    lines = [clean_text(x) for x in text.splitlines()]
    # Drop purely decorative lines
    out = []
    for ln in lines:
        if not ln:
            continue
        if re.fullmatch(r"[-_=—–·•●■◆◇★☆※ ]{3,}", ln):
            continue
        out.append(ln)
    return out


# -----------------------------
# PDF text extraction
# -----------------------------
def extract_pages_text(pdf_bytes: bytes) -> List[str]:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber 未安装或不可用。请在 requirements.txt 添加 pdfplumber。")
    pages_text: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            t = p.extract_text() or ""
            pages_text.append(t)
    return pages_text


# -----------------------------
# Chapter locating (robust)
# -----------------------------
@dataclass
class HeadingHit:
    key: str
    display: str
    page_idx: int  # 0-based
    line_idx: int  # 0-based in that page
    score: float
    raw_line: str

def _heading_patterns() -> List[Tuple[str, str, List[str]]]:
    """
    Returns list of (key, display_title, keywords).
    key is stable id; display_title is shown in UI.
    keywords are used for fuzzy match.
    """
    return [
        ("obj", "一、培养目标", ["培养目标"]),
        ("gradreq", "二、毕业要求", ["毕业要求"]),
        ("pos", "三、专业定位与特色", ["专业定位", "特色"]),
        ("core", "四、主干学科、专业核心课程和主要实践性教学环节", ["主干学科", "专业核心课程", "实践性", "教学环节"]),
        ("degree", "五、标准学制与授予学位", ["标准学制", "授予学位"]),
        ("gradcond", "六、毕业条件", ["毕业条件"]),
        # Appendices often appear as headings too
        ("app1", "七、专业教学计划表（附表1）", ["专业教学计划表", "附表1"]),
        ("app2", "八、学分统计表（附表2）", ["学分统计表", "附表2"]),
        ("app3", "九、教学进程表（附表3）", ["教学进程表", "附表3"]),
        ("app4", "十、课程设置对毕业要求支撑关系表（附表4）", ["支撑关系表", "毕业要求", "附表4"]),
        ("app5", "十一、课程设置逻辑思维导图（附表5）", ["逻辑思维导图", "附表5"]),
    ]

def locate_heading_hits(pages_text: List[str]) -> List[HeadingHit]:
    hits: List[HeadingHit] = []
    patterns = _heading_patterns()

    for page_idx, page_text in enumerate(pages_text):
        for line_idx, raw in enumerate(normalize_lines(page_text)):
            nrm = normalize_for_match(raw)
            if not nrm:
                continue

            for key, display, kws in patterns:
                # scoring: keywords coverage + optional numeral prefix bonus
                cov = 0
                for kw in kws:
                    if normalize_for_match(kw) in nrm:
                        cov += 1
                if cov == 0:
                    continue
                score = cov / max(1, len(kws))

                # bonus if line starts with Chinese numeral + delimiter (e.g., 四、 or 4.)
                if re.match(r"^[一二三四五六七八九十\d]{1,3}[、\.\s]", raw):
                    score += 0.15

                # small bonus if display's main keyword appears early
                main_kw = normalize_for_match(kws[0])
                pos = nrm.find(main_kw)
                if pos != -1 and pos < 3:
                    score += 0.05

                hits.append(HeadingHit(key, display, page_idx, line_idx, score, raw))
    # For each key keep best hit (highest score, then earliest)
    best: Dict[str, HeadingHit] = {}
    for h in sorted(hits, key=lambda x: (-x.score, x.page_idx, x.line_idx)):
        if h.key not in best:
            best[h.key] = h
    # return ordered by appearance
    ordered = sorted(best.values(), key=lambda x: (x.page_idx, x.line_idx))
    return ordered

def build_chapter_ranges(pages_text: List[str]) -> List[Tuple[str, int, int, int, int]]:
    """
    Returns list of (display_title, start_page, start_line, end_page, end_line_exclusive)
    """
    hits = locate_heading_hits(pages_text)
    ranges: List[Tuple[str, int, int, int, int]] = []
    if not hits:
        return ranges

    for i, h in enumerate(hits):
        start_page, start_line = h.page_idx, h.line_idx
        if i + 1 < len(hits):
            nh = hits[i + 1]
            end_page, end_line = nh.page_idx, nh.line_idx
        else:
            end_page, end_line = len(pages_text) - 1, 10**9  # to end
        ranges.append((h.display, start_page, start_line, end_page, end_line))
    return ranges

def extract_range_text(pages_text: List[str], start_page: int, start_line: int, end_page: int, end_line: int) -> str:
    chunks: List[str] = []
    for pidx in range(start_page, end_page + 1):
        lines = normalize_lines(pages_text[pidx])
        s = start_line if pidx == start_page else 0
        e = end_line if pidx == end_page else len(lines)
        e = min(e, len(lines))
        if s < e:
            chunks.append("\n".join(lines[s:e]))
    return "\n".join(chunks).strip()

def extract_chapters(pages_text: List[str]) -> Dict[str, str]:
    ranges = build_chapter_ranges(pages_text)
    chapters: Dict[str, str] = {}
    for title, sp, sl, ep, el in ranges:
        chapters[title] = extract_range_text(pages_text, sp, sl, ep, el)
    return chapters


# -----------------------------
# Structured parsing: objectives & graduation requirements
# -----------------------------
def parse_objectives(text: str) -> List[str]:
    lines = normalize_lines(text)
    items: List[str] = []
    buf: List[str] = []

    def flush():
        nonlocal buf
        if buf:
            s = clean_text(" ".join(buf))
            if s:
                items.append(s)
        buf = []

    for ln in lines:
        m = re.match(r"^\s*(?:培养目标)?\s*([1-9]\d?)\s*[\.、]\s*(.*)$", ln)
        if m:
            flush()
            buf = [m.group(2)]
            continue
        # also accept "（1）" "1)" etc
        m2 = re.match(r"^\s*[\(（]\s*([1-9]\d?)\s*[\)）]\s*(.*)$", ln)
        if m2:
            flush()
            buf = [m2.group(2)]
            continue
        # continuation line
        if buf:
            buf.append(ln)
    flush()

    # fallback: if no enumerated, try bullet-like lines
    if not items:
        bullets = []
        for ln in lines:
            if re.match(r"^[•●■◆◇\-]\s*", ln):
                bullets.append(re.sub(r"^[•●■◆◇\-]\s*", "", ln).strip())
        if bullets:
            items = bullets

    return items

def _parse_main_item_header(ln: str) -> Optional[Tuple[int, str]]:
    # "1. 工程知识：" / "1 工程知识：" / "1、工程知识：" etc
    m = re.match(r"^\s*([1-9]\d?)\s*[\.、]?\s*([^\d].*?)\s*[：:]\s*(.*)$", ln)
    if m:
        return int(m.group(1)), clean_text(m.group(2) + "：" + m.group(3))
    m2 = re.match(r"^\s*([1-9]\d?)\s*[\.、]\s*(.*)$", ln)
    if m2:
        return int(m2.group(1)), clean_text(m2.group(2))
    return None

def _parse_sub_item(ln: str) -> Optional[Tuple[str, str]]:
    # "1.1 ..." "12.3 ..." etc
    m = re.match(r"^\s*([1-9]\d?\.\d+)\s*(.*)$", ln)
    if m:
        return m.group(1), clean_text(m.group(2))
    return None

def parse_graduation_requirements(text: str) -> Dict[str, Any]:
    """
    Output schema:
    {
      "1": {"title": "...", "subs": {"1.1":"...", ...}},
      ...
      "12": {...}
    }
    """
    lines = normalize_lines(text)
    req: Dict[str, Any] = {}
    cur_main: Optional[int] = None
    cur_buf: List[str] = []

    def flush_main_text():
        nonlocal cur_buf, cur_main
        if cur_main is not None and cur_main in range(1, 13):
            if "title" not in req[str(cur_main)] or not req[str(cur_main)]["title"]:
                s = clean_text(" ".join(cur_buf))
                if s:
                    req[str(cur_main)]["title"] = s
        cur_buf = []

    for ln in lines:
        # detect main item header
        mh = _parse_main_item_header(ln)
        if mh:
            flush_main_text()
            n, title = mh
            cur_main = n
            req.setdefault(str(n), {"title": title, "subs": {}})
            # if title already parsed from this line, reset buffer
            req[str(n)]["title"] = title
            continue

        sh = _parse_sub_item(ln)
        if sh and cur_main is not None:
            code, content = sh
            req.setdefault(str(cur_main), {"title": "", "subs": {}})
            req[str(cur_main)]["subs"][code] = content
            continue

        # continuation for title if no sub-items yet or line seems part of main description
        if cur_main is not None:
            # ignore obvious page headers/footers
            if re.fullmatch(r"\d+", ln):
                continue
            cur_buf.append(ln)

    flush_main_text()

    # Ensure 1..12 keys exist if partially parsed (keeps UI stable)
    for i in range(1, 13):
        req.setdefault(str(i), {"title": "", "subs": {}})

    return req


# -----------------------------
# Appendix title map from body text (附表1..)
# -----------------------------
def extract_appendix_title_map(pages_text: List[str]) -> Dict[str, str]:
    """
    Returns {"附表1": "七、专业教学计划表（附表1）", ...}
    """
    full = "\n".join(pages_text)
    lines = normalize_lines(full)
    mp: Dict[str, str] = {}
    for ln in lines:
        nrm = normalize_for_match(ln)
        m = re.search(r"附表\s*([1-9]\d?)", ln)
        if not m:
            continue
        no = m.group(1)
        key = f"附表{no}"
        # Prefer lines that also contain Chinese chapter numerals or keywords
        if key not in mp:
            mp[key] = ln
        else:
            # choose "richer" line (longer)
            if len(ln) > len(mp[key]):
                mp[key] = ln

    # Normalize display titles for known ones
    defaults = {
        "附表1": "七、专业教学计划表（附表1）",
        "附表2": "八、学分统计表（附表2）",
        "附表3": "九、教学进程表（附表3）",
        "附表4": "十、课程设置对毕业要求支撑关系表（附表4）",
        "附表5": "十一、课程设置逻辑思维导图（附表5）",
    }
    for k, v in defaults.items():
        mp.setdefault(k, v)
    return mp


# -----------------------------
# Table extraction + cleanup
# -----------------------------
@dataclass
class ExtractedTable:
    title: str
    page: int  # 1-based
    df: pd.DataFrame
    raw: List[List[str]]

def _rectangularize(rows: List[List[Any]]) -> List[List[str]]:
    max_len = max((len(r) for r in rows), default=0)
    rect: List[List[str]] = []
    for r in rows:
        rr = [clean_text(x) for x in r]
        if len(rr) < max_len:
            rr += [""] * (max_len - len(rr))
        rect.append(rr)
    # trim trailing fully-empty columns
    if max_len > 0:
        keep = max_len
        for j in range(max_len-1, -1, -1):
            col = [rect[i][j] for i in range(len(rect))]
            if all(clean_text(x)=="" for x in col):
                keep -= 1
            else:
                break
        rect = [r[:keep] for r in rect]
    return rect

def _combine_multirow_headers(rect: List[List[str]]) -> Tuple[List[str], List[List[str]]]:
    """
    Heuristic: if first 2 rows look like header parts, merge them.
    Returns (columns, data_rows).
    """
    if not rect:
        return [], []
    if len(rect) == 1:
        cols = [c or f"列{idx+1}" for idx, c in enumerate(rect[0])]
        return cols, []

    r0, r1 = rect[0], rect[1]
    non0 = sum(1 for x in r0 if x)
    non1 = sum(1 for x in r1 if x)
    # header-like: first row sparse and second row provides detail
    multi = (non0 <= max(2, int(0.45*len(r0)))) and (non1 >= non0)
    if not multi:
        cols = [c or f"列{idx+1}" for idx, c in enumerate(r0)]
        return cols, rect[1:]

    cols = []
    for j in range(len(r0)):
        a = clean_text(r0[j])
        b = clean_text(r1[j])
        if a and b and a != b:
            cols.append(f"{a} / {b}")
        else:
            cols.append(a or b or f"列{j+1}")
    data_rows = rect[2:]
    return cols, data_rows

def _forward_fill_merged_cells(df: pd.DataFrame) -> pd.DataFrame:
    """
    Forward-fill columns that look like 'category' columns (many blanks, few unique non-blanks).
    Helps with merged-cell columns like 课程体系/课程类别.
    """
    out = df.copy()
    nrows = len(out)
    if nrows == 0:
        return out

    for col in out.columns:
        s = out[col].astype(str).fillna("").map(clean_text)
        blank_ratio = (s == "").mean()
        non = s[s != ""]
        uniq = non.nunique(dropna=True)
        # Heuristic thresholds
        if blank_ratio >= 0.20 and uniq <= max(12, int(0.25*nrows)):
            out[col] = s.replace("", pd.NA).ffill().fillna("")
        else:
            out[col] = s

    return out

def _safe_df(df: pd.DataFrame) -> pd.DataFrame:
    # Ensure arrow-safe: no lists/dicts, all strings
    out = df.copy()
    out.columns = [clean_text(c) or f"列{idx+1}" for idx, c in enumerate(out.columns)]
    # de-duplicate columns
    seen: Dict[str, int] = {}
    cols = []
    for c in out.columns:
        if c not in seen:
            seen[c] = 1
            cols.append(c)
        else:
            seen[c] += 1
            cols.append(f"{c}_{seen[c]}")
    out.columns = cols

    def cell_to_str(x: Any) -> str:
        if x is None:
            return ""
        if isinstance(x, (dict, list, tuple, set)):
            try:
                return json.dumps(x, ensure_ascii=False)
            except Exception:
                return str(x)
        if pd.isna(x):
            return ""
        return clean_text(x)

    for c in out.columns:
        out[c] = out[c].map(cell_to_str)
    return out

def extract_tables_pdfplumber(pdf_bytes: bytes, pages_text: List[str]) -> List[ExtractedTable]:
    if pdfplumber is None:
        return []
    appendix_map = extract_appendix_title_map(pages_text)

    # Map page -> best title line mentioning "附表"
    page_title_hint: Dict[int, str] = {}
    for i, t in enumerate(pages_text):
        lines = normalize_lines(t)
        for ln in lines[:12] + lines[-12:]:
            m = re.search(r"(附表\s*[1-9]\d?)", ln)
            if m:
                key = re.sub(r"\s+", "", m.group(1))
                key = key.replace("附表", "附表")
                title = appendix_map.get(key, ln)
                page_title_hint[i+1] = title

    tables: List[ExtractedTable] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            # Use two settings passes to increase recall
            settings_list = [
                dict(
                    vertical_strategy="lines",
                    horizontal_strategy="lines",
                    snap_tolerance=3,
                    join_tolerance=3,
                    edge_min_length=3,
                    intersection_tolerance=5,
                    min_words_vertical=1,
                    min_words_horizontal=1,
                    keep_blank_chars=True,
                ),
                dict(
                    vertical_strategy="text",
                    horizontal_strategy="text",
                    snap_tolerance=3,
                    join_tolerance=3,
                    edge_min_length=3,
                    intersection_tolerance=5,
                    min_words_vertical=1,
                    min_words_horizontal=1,
                    keep_blank_chars=True,
                ),
            ]

            raw_tables: List[List[List[Any]]] = []
            for ts in settings_list:
                try:
                    raw_tables.extend(page.extract_tables(table_settings=ts) or [])
                except Exception:
                    continue

            # De-dup by shape+first row string
            seen = set()
            uniq_raw = []
            for tb in raw_tables:
                if not tb or len(tb) < 2:
                    continue
                sig = (len(tb), max(len(r) for r in tb), clean_text(" ".join(tb[0]))[:80])
                if sig in seen:
                    continue
                seen.add(sig)
                uniq_raw.append(tb)

            for tb in uniq_raw:
                rect = _rectangularize(tb)
                cols, data_rows = _combine_multirow_headers(rect)
                if not cols:
                    continue
                df = pd.DataFrame(data_rows, columns=cols)
                df = _forward_fill_merged_cells(df)
                df = _safe_df(df)

                # title
                page_no = page_idx + 1
                title = page_title_hint.get(page_no, f"表格（第{page_no}页）")
                # If page text contains a more specific line above table
                hint_lines = normalize_lines(pages_text[page_idx])
                # pick the longest line containing "表" or "附表"
                best_line = ""
                for ln in hint_lines:
                    if ("附表" in ln) or re.search(r"表\s*[1-9]\d?", ln):
                        if len(ln) > len(best_line):
                            best_line = ln
                if best_line:
                    # If includes "附表x", map
                    m = re.search(r"(附表\s*[1-9]\d?)", best_line)
                    if m:
                        key = re.sub(r"\s+", "", m.group(1))
                        title = appendix_map.get(key, best_line)
                    else:
                        title = best_line

                tables.append(ExtractedTable(title=title, page=page_no, df=df, raw=rect))

    # Sort by page then title
    tables.sort(key=lambda x: (x.page, x.title))
    return tables


# -----------------------------
# Optional LLM refine (OpenAI-compatible)
# -----------------------------
def call_openai_compatible_chat(base_url: str, api_key: str, model: str, messages: List[Dict[str, str]], temperature: float = 0.2, timeout: int = 120) -> str:
    if requests is None:
        raise RuntimeError("requests 未安装，无法调用 LLM。")
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
    }
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

def llm_refine(parsed: Dict[str, Any], raw_sections: Dict[str, str], appendix_title_lines: List[str], llm_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ask LLM to:
    - fix missing 培养目标 items
    - ensure 毕业要求 has 1..12 with subitems
    - improve chapter title presence (no hallucination; only from provided raw text)
    - refine appendix table titles if missing, and optionally add "方向" classification hints
    """
    base_url = llm_cfg.get("base_url", "")
    api_key = llm_cfg.get("api_key", "")
    model = llm_cfg.get("model", "")
    temperature = llm_cfg.get("temperature", 0.2)

    # Keep raw text bounded
    raw_obj = raw_sections.get("一、培养目标", "")[:6000]
    raw_req = raw_sections.get("二、毕业要求", "")[:9000]

    prompt = {
        "task": "你是高校培养方案解析校对助手。请在不编造信息的前提下，用下面提供的原文片段纠正并补全结构化解析结果。",
        "rules": [
            "只能依据提供的原文片段修正/补全；如果原文缺失则保持为空并说明。",
            "毕业要求必须输出 1-12 共12条，每条可包含若干子条(如1.1,1.2...)。",
            "培养目标输出为列表，每条为完整句子。",
            "不要输出多余解释；只输出 JSON。"
        ],
        "inputs": {
            "parsed": parsed,
            "raw_text": {
                "培养目标_section": raw_obj,
                "毕业要求_section": raw_req,
                "appendix_title_lines": appendix_title_lines[:80],
            }
        },
        "output_schema": {
            "objectives": ["..."],
            "graduation_requirements": {"1":{"title":"...","subs":{"1.1":"..."}}, "...": {}},
            "chapter_titles_found": ["..."],
            "table_title_hints": {"附表1":"...", "附表2":"...", "附表3":"...", "附表4":"...", "附表5":"..."},
            "notes": ["..."]
        }
    }

    messages = [
        {"role": "system", "content": "You return strictly valid JSON (no markdown)."},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}
    ]
    out = call_openai_compatible_chat(base_url, api_key, model, messages, temperature=temperature)
    # Parse JSON safely
    out = out.strip()
    # Remove accidental code fences
    out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out).strip()
    try:
        refined = json.loads(out)
        return refined if isinstance(refined, dict) else {}
    except Exception:
        return {}


# -----------------------------
# Export helpers
# -----------------------------
def make_tables_zip(tables: List[ExtractedTable]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        meta = []
        for i, t in enumerate(tables, start=1):
            safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", t.title)
            safe_title = safe_title[:80] if safe_title else f"table_{i}"
            xlsx_name = f"{i:02d}_{safe_title}_p{t.page}.xlsx"
            xbio = io.BytesIO()
            # prefer xlsxwriter if present
            engine = "xlsxwriter"
            try:
                with pd.ExcelWriter(xbio, engine=engine) as writer:
                    t.df.to_excel(writer, sheet_name="table", index=False)
            except Exception:
                with pd.ExcelWriter(xbio, engine="openpyxl") as writer:
                    t.df.to_excel(writer, sheet_name="table", index=False)

            zf.writestr(xlsx_name, xbio.getvalue())
            meta.append({"title": t.title, "page": t.page, "rows": len(t.df), "cols": len(t.df.columns), "file": xlsx_name})

        zf.writestr("tables_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return bio.getvalue()


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="培养方案全量抽取", layout="wide")

st.title("培养方案 PDF 全量抽取与结构化展示")

with st.sidebar:
    st.header("输入")
    pdf_file = st.file_uploader("上传培养方案 PDF", type=["pdf"])
    run_btn = st.button("开始全量抽取", type="primary")

    st.divider()
    st.header("LLM 校对与修正")
    enable_llm = st.checkbox("启用 LLM 校对与修正", value=False)
    llm_cfg = {}
    if enable_llm:
        st.caption("使用 OpenAI 兼容接口（例如你自建网关 / 兼容服务）。")
        llm_cfg["base_url"] = st.text_input("Base URL", value="https://api.openai.com")
        llm_cfg["api_key"] = st.text_input("API Key", type="password", value="")
        llm_cfg["model"] = st.text_input("Model", value="gpt-4o-mini")
        llm_cfg["temperature"] = st.slider("Temperature", 0.0, 1.0, 0.2, 0.05)

    st.divider()
    st.header("显示")
    show_pages = st.checkbox("显示分页原文", value=False)
    show_raw_tables = st.checkbox("显示表格原始矩阵（调试）", value=False)

if not pdf_file:
    st.info("请先在左侧上传培养方案 PDF。")
    st.stop()

pdf_bytes = pdf_file.read()

if "extract_result" not in st.session_state:
    st.session_state["extract_result"] = None

if run_btn:
    with st.spinner("正在解析 PDF 文本..."):
        pages_text = extract_pages_text(pdf_bytes)
        chapters = extract_chapters(pages_text)

    with st.spinner("正在解析毕业要求/培养目标..."):
        obj_text = chapters.get("一、培养目标", "")
        req_text = chapters.get("二、毕业要求", "")
        objectives = parse_objectives(obj_text)
        gradreq = parse_graduation_requirements(req_text)

    with st.spinner("正在抽取 PDF 表格..."):
        tables = extract_tables_pdfplumber(pdf_bytes, pages_text)

    # Gather appendix title lines for LLM
    appendix_title_lines = []
    for ln in normalize_lines("\n".join(pages_text)):
        if "附表" in ln:
            appendix_title_lines.append(ln)
    appendix_title_lines = appendix_title_lines[:200]

    parsed = {
        "objectives": objectives,
        "graduation_requirements": gradreq,
        "chapter_titles_found": list(chapters.keys()),
        "table_count": len(tables),
    }

    refined = None
    if enable_llm:
        with st.spinner("LLM 正在校对与修正（若接口不可用会自动跳过）..."):
            try:
                refined = llm_refine(parsed, chapters, appendix_title_lines, llm_cfg)
            except Exception as e:
                refined = {"notes": [f"LLM 调用失败：{e}"]}

        # Apply refined results carefully
        if isinstance(refined, dict):
            if isinstance(refined.get("objectives"), list) and refined["objectives"]:
                objectives = [clean_text(x) for x in refined["objectives"] if clean_text(x)]
            if isinstance(refined.get("graduation_requirements"), dict) and refined["graduation_requirements"]:
                # merge 1..12
                for i in range(1, 13):
                    k = str(i)
                    if k in refined["graduation_requirements"]:
                        gradreq[k] = refined["graduation_requirements"][k]
            # Update known appendix titles hint (doesn't change extracted df)
            if isinstance(refined.get("table_title_hints"), dict):
                hints = refined["table_title_hints"]
                for t in tables:
                    for key, title in hints.items():
                        if key in t.title and clean_text(title):
                            t.title = clean_text(title)

    st.session_state["extract_result"] = {
        "pages_text": pages_text,
        "chapters": chapters,
        "objectives": objectives,
        "graduation_requirements": gradreq,
        "tables": tables,
        "llm_refined": refined,
    }

res = st.session_state["extract_result"]
if res is None:
    st.warning("请点击左侧“开始全量抽取”。")
    st.stop()

pages_text = res["pages_text"]
chapters = res["chapters"]
objectives = res["objectives"]
gradreq = res["graduation_requirements"]
tables: List[ExtractedTable] = res["tables"]
refined = res.get("llm_refined")

# ---- Overview ----
st.subheader("章节定位概览")
ranges = build_chapter_ranges(pages_text)
if ranges:
    df_rng = pd.DataFrame(
        [{"章节": t, "起始页": sp+1, "起始行": sl+1, "结束页": ep+1} for (t, sp, sl, ep, el) in ranges]
    )
    st.dataframe(_safe_df(df_rng), use_container_width=True, hide_index=True)
else:
    st.warning("未能定位章节标题（但仍可查看分页原文与表格）。")

tabs = st.tabs(["培养目标", "毕业要求", "章节原文", "附表表格", "分页原文"])

with tabs[0]:
    st.subheader("培养目标（可编辑/校对）")
    if objectives:
        for i, it in enumerate(objectives, start=1):
            st.markdown(f"**{i}.** {it}")
    else:
        st.warning("未解析到培养目标条目。可在“分页原文”里确认 PDF 文本是否可提取。")

    if enable_llm and refined:
        notes = refined.get("notes") if isinstance(refined, dict) else None
        if notes:
            st.info("LLM 校对备注：\n- " + "\n- ".join([clean_text(x) for x in notes if clean_text(x)]))

with tabs[1]:
    st.subheader("毕业要求（1–12）")
    for i in range(1, 13):
        k = str(i)
        item = gradreq.get(k, {"title": "", "subs": {}})
        title = clean_text(item.get("title", ""))
        st.markdown(f"### {i}. {title if title else '（标题缺失）'}")
        subs = item.get("subs", {}) if isinstance(item.get("subs", {}), dict) else {}
        if subs:
            # sort by numeric
            def _key(x):
                try:
                    a,b = x.split(".")
                    return (int(a), float("0."+b))
                except Exception:
                    return (999, 999)
            for code, cont in sorted(subs.items(), key=lambda kv: _key(kv[0])):
                st.markdown(f"- **{code}** {clean_text(cont)}")
        else:
            st.caption("（未解析到子条）")

with tabs[2]:
    st.subheader("章节原文（抽取结果）")
    for title, content in chapters.items():
        with st.expander(title, expanded=False):
            st.text_area("内容", value=content, height=260, label_visibility="collapsed")

with tabs[3]:
    st.subheader("附表/表格（PDF 抽取）")
    if not tables:
        st.warning("未抽取到表格。若 PDF 为图片扫描件，建议改用 OCR（此版本未内置 OCR）。")
    else:
        c1, c2 = st.columns([1, 1])
        with c1:
            st.metric("表格数量", len(tables))
        with c2:
            zip_bytes = make_tables_zip(tables)
            st.download_button("下载全部表格（ZIP+Excel）", data=zip_bytes, file_name="培养方案_表格导出.zip", mime="application/zip")

        for idx, t in enumerate(tables, start=1):
            with st.expander(f"{idx:02d}. {t.title}（第{t.page}页）", expanded=False):
                st.dataframe(_safe_df(t.df), use_container_width=True, hide_index=True)
                if show_raw_tables:
                    st.caption("原始矩阵（调试）：")
                    st.write(t.raw[:10])

with tabs[4]:
    st.subheader("分页原文")
    if show_pages:
        for i, t in enumerate(pages_text, start=1):
            with st.expander(f"第 {i} 页", expanded=False):
                st.text_area("page_text", value=t, height=320, label_visibility="collapsed")
    else:
        st.info("如需查看分页原文，请在左侧勾选“显示分页原文”。")
