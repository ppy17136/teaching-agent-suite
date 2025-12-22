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

# -----------------------------
# Optional LLM refinement (for post-processing)
# -----------------------------

@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str = "openai_compat"  # "openai_compat" | "dashscope"
    model: str = ""
    api_key: str = ""
    base_url: str = ""  # only for openai_compat
    timeout: int = 60
    temperature: float = 0.0
    max_tokens: int = 2000


def _get_secret(key: str, default: str = "") -> str:
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return str(st.secrets.get(key, default))
    except Exception:
        pass
    return os.environ.get(key, default)


def _openai_compat_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    if not base_url:
        return ""
    if base_url.endswith("/chat/completions") or base_url.endswith("/v1/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return base_url.rstrip("/") + "/chat/completions"
    return base_url.rstrip("/") + "/v1/chat/completions"


def llm_chat(messages: List[Dict[str, str]], cfg: LLMConfig) -> str:
    if not cfg.enabled:
        return ""
    if not cfg.model:
        raise ValueError("LLM 已启用，但未填写 model。")
    if not cfg.api_key:
        raise ValueError("LLM 已启用，但未填写 API Key。")

    provider = (cfg.provider or "openai_compat").lower().strip()

    if provider == "dashscope":
        try:
            import dashscope  # type: ignore
            from dashscope import Generation  # type: ignore
        except Exception as e:
            raise RuntimeError("未安装 dashscope SDK（requirements.txt 需要加入 dashscope）。") from e

        dashscope.api_key = cfg.api_key
        resp = Generation.call(
            model=cfg.model,
            messages=messages,
            result_format="message",
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
        try:
            return resp.output.choices[0].message["content"]
        except Exception:
            return str(resp)

    # openai-compatible
    url = _openai_compat_url(cfg.base_url)
    if not url:
        raise ValueError("OpenAI-compatible 模式需要填写 base_url（例如 .../v1 或完整 chat/completions 端点）。")

    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=cfg.timeout)
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        return json.dumps(data, ensure_ascii=False)


def _extract_context_snippets(pages_text: List[str]) -> Dict[str, str]:
    all_text = "\n".join([t or "" for t in pages_text])

    def clip_around(keyword: str, window: int = 3500) -> str:
        idx = all_text.find(keyword)
        if idx < 0:
            return ""
        s = max(0, idx - window // 3)
        e = min(len(all_text), idx + window)
        return all_text[s:e]

    return {
        "objectives": clip_around("培养目标"),
        "grad_reqs": clip_around("毕业要求"),
        "sections": clip_around("专业定位") + "\n" + clip_around("主干学科") + "\n" + clip_around("标准学制") + "\n" + clip_around("毕业条件"),
        "tables": clip_around("附表"),
    }


def llm_refine_result(result: Dict[str, Any], pages_text: List[str], cfg: LLMConfig) -> Dict[str, Any]:
    snippets = _extract_context_snippets(pages_text)

    system = (
        "你是高校培养方案结构化抽取的校对助手。\n"
        "在不捏造内容的前提下，基于提供的 PDF 片段与已抽取结果，修正/补全：\n"
        "1) 培养目标 objectives；\n"
        "2) 毕业要求 graduate_requirements（必须包含 1-12 大条及子条 1.1..）；\n"
        "3) 大标题 sections（如 三、专业定位与特色 等）；\n"
        "4) 附表标题 appendix_titles（附表1..5 对应的中文表名）。\n"
        "只输出 JSON，不要输出其它文字。"
    )

    compact = {
        "objectives": result.get("objectives", []),
        "graduate_requirements": result.get("graduate_requirements", []),
        "sections": result.get("sections", []),
        "appendix_titles": result.get("appendix_titles", {}),
    }

    user_payload = {
        "pdf_snippets": snippets,
        "extracted": compact,
        "output_schema": {
            "objectives": ["string", "..."],
            "graduate_requirements": [{"id": "1..12", "title": "string", "text": "string", "sub": [{"id": "1.1", "text": "string"}]}],
            "sections": [{"no": "三", "title": "string", "text": "string"}],
            "appendix_titles": {"附表1": "string", "附表2": "string", "附表3": "string", "附表4": "string", "附表5": "string"},
        },
    }

    content = llm_chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        cfg=cfg,
    )

    fixed = json.loads(content)

    for k in ["objectives", "graduate_requirements", "sections", "appendix_titles"]:
        if k in fixed and fixed[k]:
            result[k] = fixed[k]
    result["_llm_refined"] = True
    return result

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

def extract_pages_text(pdf_bytes: bytes) -> List[str]:
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
    """Heuristic fill for merged cells: horizontal then vertical for sparse columns."""
    if not table:
        return table
    rows = [r[:] for r in table]
    n_rows = len(rows)
    n_cols = max(len(r) for r in rows)

    # make rectangular
    for i in range(n_rows):
        if len(rows[i]) < n_cols:
            rows[i] += [""] * (n_cols - len(rows[i]))

    # horizontal fill
    for i in range(n_rows):
        last = ""
        for j in range(n_cols):
            if rows[i][j] != "":
                last = rows[i][j]
            else:
                # only fill if last looks like category-like text (avoid filling numeric columns)
                if last and not re.match(r"^[-+]?\d+(\.\d+)?$", last):
                    rows[i][j] = rows[i][j] or ""  # keep empty by default
        # do not actually fill horizontally aggressively (often wrong). Keep conservative.

    # Decide columns that are likely merged vertically: high empty ratio
    empties = []
    for j in range(n_cols):
        col = [rows[i][j] for i in range(n_rows)]
        empty_ratio = sum(1 for x in col if x == "") / max(1, n_rows)
        empties.append(empty_ratio)

    # vertical fill on columns with empty_ratio high
    for j in range(n_cols):
        if empties[j] < 0.35:
            continue
        last = ""
        for i in range(n_rows):
            if rows[i][j] != "":
                last = rows[i][j]
            else:
                if last:
                    rows[i][j] = last

    return rows


def infer_direction_for_row(row: List[str]) -> str:
    text = " ".join([c for c in row if c])
    if "焊接" in text and "无损" in text:
        return "混合"
    if "焊接" in text:
        return "焊接"
    if "无损" in text or "NDT" in text:
        return "无损检测"
    return ""


def infer_direction_for_table(table: List[List[str]]) -> str:
    cnt = {"焊接": 0, "无损检测": 0}
    for r in table[:50]:
        d = infer_direction_for_row(r)
        if d == "焊接":
            cnt["焊接"] += 1
        elif d == "无损检测":
            cnt["无损检测"] += 1
    if cnt["焊接"] and cnt["无损检测"]:
        return "混合"
    if cnt["焊接"]:
        return "焊接"
    if cnt["无损检测"]:
        return "无损检测"
    return ""


def classify_appendix(table: List[List[str]]) -> str:
    """Return appendix key like '附表1'..'附表5' or ''"""
    head = " ".join(table[0]) if table else ""
    head2 = " ".join(table[1]) if len(table) > 1 else ""
    blob = (head + " " + head2)

    if "课程编码" in blob and "课程体系" in blob:
        return "附表1"
    if "学分" in blob and ("统计" in blob or "合计" in blob):
        return "附表2"
    if "教学进程" in blob or "周" in blob or "学期" in blob and "周" in blob:
        return "附表3"
    if "毕业要求" in blob or "1.1" in blob or "12.3" in blob:
        return "附表4"

    # 逻辑思维导图通常不是表格
    return ""


@dataclass
class ExtractedTable:
    appendix: str
    appendix_title: str
    page: int
    title: str
    columns: List[str]
    rows: List[List[str]]
    direction: str


def extract_tables_pdfplumber(pdf_bytes: bytes, pages_text: List[str]) -> List[ExtractedTable]:
    if pdfplumber is None:
        return []

    appendix_map = extract_appendix_title_map(pages_text)

    out: List[ExtractedTable] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            # Try two strategies: lines-first (bordered) then text (borderless)
            raw_tables: List[List[List[Any]]] = []
            for settings in (PDFPLUMBER_TABLE_SETTINGS_LINES, PDFPLUMBER_TABLE_SETTINGS_TEXT):
                try:
                    raw_tables += page.extract_tables(settings) or []
                except Exception:
                    continue

            # De-duplicate by a light signature (first 3 rows joined)
            seen_sig: set = set()
            for t in raw_tables:
                nt = normalize_table(t)
                if not nt or len(nt) < 2:
                    continue
                sig = "||".join(["|".join(r) for r in nt[: min(3, len(nt))]])
                if sig in seen_sig:
                    continue
                seen_sig.add(sig)
                nt = ffill_merged_cells(nt)

                appendix = classify_appendix(nt)
                appendix_title = appendix_map.get(appendix, appendix) if appendix else ""

                # Determine header
                columns = nt[0]
                rows = nt[1:]

                # Add direction column if useful (appendix1 often needs)
                direction_tbl = infer_direction_for_table(nt)
                # per-row direction for appendix1/4 might be useful
                add_row_direction = appendix in ("附表1", "附表4")
                if add_row_direction:
                    columns = columns + ["专业方向(推断)"]
                    new_rows = []
                    for r in rows:
                        new_rows.append(r + [infer_direction_for_row(r)])
                    rows = new_rows

                title = appendix_title or f"表格-P{i+1}"  # fallback
                if appendix_title:
                    title = f"{appendix_title} - 第{i+1}页"

                out.append(
                    ExtractedTable(
                        appendix=appendix,
                        appendix_title=appendix_title,
                        page=i + 1,
                        title=title,
                        columns=columns,
                        rows=rows,
                        direction=direction_tbl,
                    )
                )

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


# -----------------------------
# Full extraction pipeline
# -----------------------------


def run_full_extract(pdf_bytes: bytes, llm_cfg: Optional[LLMConfig] = None) -> Dict[str, Any]:
    pages_text = extract_pages_text(pdf_bytes)
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
    # Optional LLM refinement
    if llm_cfg is not None and getattr(llm_cfg, 'enabled', False):
        try:
            result = llm_refine_result(result, pages_text, llm_cfg)
        except Exception as e:
            result['_llm_error'] = str(e)

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

    with st.expander("🔎 启用 LLM 校对与修正（可选）", expanded=False):
        enable_llm = st.checkbox("启用 LLM 校对与修正", value=False)
        provider = st.selectbox("LLM 提供方", ["OpenAI-compatible", "DashScope/Qwen"], index=0)
        model = st.text_input("Model 名称", value=_get_secret("LLM_MODEL", ""))
        api_key = st.text_input("API Key", value=_get_secret("LLM_API_KEY", ""), type="password")
        base_url = ""
        if provider == "OpenAI-compatible":
            base_url = st.text_input("Base URL（如 https://xxx/v1 或完整 chat/completions）", value=_get_secret("LLM_BASE_URL", ""))
        st.caption("可在 Streamlit secrets / 环境变量设置：LLM_API_KEY / LLM_MODEL / LLM_BASE_URL")

    llm_cfg = LLMConfig(
        enabled=bool(enable_llm),
        provider="dashscope" if provider == "DashScope/Qwen" else "openai_compat",
        model=model.strip(),
        api_key=api_key.strip(),
        base_url=base_url.strip(),
        temperature=0.0,
        max_tokens=2000,
        timeout=60,
    )

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
        res = run_full_extract(pdf_bytes, llm_cfg=llm_cfg)
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
                        st.dataframe(safe_dataframe_for_streamlit(df), use_container_width=True, hide_index=True)
                    except Exception:
                        st.warning("该表格渲染遇到兼容性问题，已退回为HTML表格显示。")
                        # pandas.DataFrame.to_markdown() needs optional dependency "tabulate".
                        # Streamlit Cloud often doesn't include it, so use HTML instead.
                        html = safe_dataframe_for_streamlit(df).to_html(index=False, escape=False)
                        st.markdown(html, unsafe_allow_html=True)

# 6) 分页原文
with tabs[5]:
    st.subheader("分页原文（用于溯源/调试抽取缺失）")
    pages = res.get("pages_text", [])
    for i, txt in enumerate(pages, start=1):
        with st.expander(f"第{i}页文本", expanded=(i == 1)):
            st.text(txt)