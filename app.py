# -*- coding: utf-8 -*-
"""
培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）

目标：把培养方案中“所有可用信息”尽量完整、可校对地抽取出来，为后续教学文件生成提供唯一事实源。

特别处理：
1) 毕业要求：解析 1–12 条及 1.1–12.x 子条款（跨页连续）
2) 大标题（三~六等）：按“中文序号 + 顿号/、”识别并保留内容
3) 附表标题：识别“七~十一（附表1~5）”，并为后续表格绑定表名
4) 合并单元格：对空白单元格做纵向/横向填充，最大化还原原表语义
5) 专业方向：基于“专业方向”列/页内提示，清晰分离焊接/无损检测/共同/未知
6) 导出：JSON（全量）、表格CSV ZIP（无需 openpyxl）

说明：
- 本版本优先保证“可用文本+表格”的完整抽取；OCR 建议独立做成可插拔模块，避免把错误写入基础库。
"""
import io
import re
import json
import time
import hashlib
import zipfile
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import pdfplumber

# ---------------------------
# 正则与基础工具
# ---------------------------

CN_SECTION_RE = re.compile(r"^(?P<idx>[一二三四五六七八九十十一十二]+)[、\.]\s*(?P<title>.+?)\s*$")
# 兼容：附表 1 / 附表1，中文/英文括号
APPENDIX_TITLE_RE = re.compile(
    r"^(?P<idx>[七八九十十一]+)[、\.]\s*(?P<title>.+?)\s*[（(]\s*(?P<app>附表\s*[1-5])\s*[）)]\s*$"
)
REQ_MAIN_RE = re.compile(r"^(?P<no>\d{1,2})\.\s*(?P<title>[^：:]+)[:：]\s*(?P<body>.*)\s*$")
REQ_SUB_RE = re.compile(r"^(?P<no>\d{1,2}\.\d{1,2})\s+(?P<body>.*)\s*$")

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def clean_text(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def join_lines(lines: List[str]) -> str:
    out = []
    last_blank = False
    for ln in lines:
        ln = ln.rstrip()
        if not ln:
            if not last_blank:
                out.append("")
            last_blank = True
        else:
            out.append(ln)
            last_blank = False
    return "\n".join(out).strip()

def norm_cn_heading(heading: str) -> str:
    return re.sub(r"\s+", "", heading)

def cn_num_to_int(cn: str) -> int:
    mapping = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,"十一":11,"十二":12}
    return mapping.get(cn, 0)

# ---------------------------
# 表格：清洗/补全（合并单元格）
# ---------------------------

def _ffill_vertical(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        df[c] = df[c].replace({None: "", "None": ""})
        if any(k in str(c) for k in ["体系", "方向", "类别", "模块", "性质", "类型", "学期", "考核", "模式", "课程体系"]):
            df[c] = df[c].replace("", pd.NA).ffill().fillna("")
    return df

def _ffill_horizontal(df: pd.DataFrame) -> pd.DataFrame:
    candidates = [c for c in df.columns if any(k in str(c) for k in ["体系", "类别", "模块"])]
    for idx in df.index:
        for c in candidates:
            if clean_text(df.at[idx, c]) == "":
                left_cols = df.columns.tolist()
                j = left_cols.index(c)
                for k in range(j-1, -1, -1):
                    v = clean_text(df.at[idx, left_cols[k]])
                    if v:
                        df.at[idx, c] = v
                        break
    return df

def fill_merged_like(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df = _ffill_vertical(df)
    df = _ffill_horizontal(df)
    return df

def normalize_two_row_header(table: List[List[Optional[str]]]) -> Tuple[List[str], List[List[str]]]:
    if len(table) < 3:
        cols = [clean_text(x) or f"col{i}" for i, x in enumerate(table[0] if table else [])]
        data = [[clean_text(x) for x in row] for row in table[1:]]
        return cols, data

    h0 = [clean_text(x) for x in table[0]]
    h1 = [clean_text(x) for x in table[1]]

    key_tokens = {"学分","学时","总学时","讲课","实验","上机","实践"}
    if sum(1 for x in h1 if x in key_tokens) < 3:
        cols = [x or f"col{i}" for i, x in enumerate(h0)]
        data = [[clean_text(x) for x in row] for row in table[1:]]
        return cols, data

    cols: List[str] = []
    last_parent = ""
    for j in range(max(len(h0), len(h1))):
        p = h0[j] if j < len(h0) else ""
        c = h1[j] if j < len(h1) else ""
        if p:
            last_parent = p
        parent = p or last_parent

        if c and c != parent:
            if "学分及学时分配" in parent:
                parent = parent.replace("学分及学时分配", "")
            name = f"{parent}{c}".strip()
        else:
            name = parent.strip() or f"col{j}"

        name = re.sub(r"\s+", "", name)
        cols.append(name)

    data_rows = [[clean_text(x) for x in row] for row in table[2:]]
    return cols, data_rows

def table_to_df(table: List[List[Optional[str]]]) -> pd.DataFrame:
    cols, rows = normalize_two_row_header(table)
    ncol = len(cols)
    fixed_rows = []
    for r in rows:
        rr = (r + [""] * ncol)[:ncol]
        fixed_rows.append(rr)
    df = pd.DataFrame(fixed_rows, columns=cols)
    df = df.loc[~(df.apply(lambda x: all(clean_text(v) == "" for v in x), axis=1))].copy()
    df = fill_merged_like(df)
    return df

# ---------------------------
# 文本：结构化（章节/附表标题/毕业要求/培养目标）
# ---------------------------

def extract_pages_text(pdf_bytes: bytes) -> List[str]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(p.extract_text() or "") for p in pdf.pages]

def extract_appendix_titles(all_lines: List[str]) -> Dict[str, str]:
    """
    从全文行中抽取“七~十一（附表X）”的标题。
    返回：{"附表1":"七、专业教学计划表", ...}
    """
    m: Dict[str, str] = {}
    for ln in all_lines:
        ln2 = clean_text(ln)
        mm = APPENDIX_TITLE_RE.match(ln2)
        if mm:
            app_raw = mm.group("app")  # e.g. 附表 1
            app = re.sub(r"\s+", "", app_raw)  # -> 附表1
            idx = mm.group("idx")
            title = mm.group("title")
            m[app] = f"{idx}、{title}"
    return m

def split_sections(pages_text: List[str]) -> Dict[str, str]:
    lines = []
    for i, t in enumerate(pages_text, start=1):
        if t:
            lines.extend(t.splitlines())
        lines.append(f"【PAGE_BREAK_{i}】")

    starts = []
    for idx, ln in enumerate(lines):
        ln2 = clean_text(ln)
        m = CN_SECTION_RE.match(ln2)
        if m:
            cn = m.group("idx")
            title = m.group("title")
            key = f"{cn}、{title}"
            starts.append((idx, key, cn_num_to_int(cn)))

    uniq = []
    seen = set()
    for pos, key, num in starts:
        k = norm_cn_heading(key)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((pos, key, num))
    uniq.sort(key=lambda x: x[0])

    sections: Dict[str, List[str]] = {}
    for i, (pos, key, num) in enumerate(uniq):
        end = uniq[i+1][0] if i+1 < len(uniq) else len(lines)
        body_lines = []
        for ln in lines[pos+1:end]:
            if ln.startswith("【PAGE_BREAK_"):
                body_lines.append("")
            else:
                body_lines.append(clean_text(ln))
        sections[key] = body_lines

    return {k: join_lines(v) for k, v in sections.items()}

def parse_graduation_requirements(section_text: str) -> Dict[str, Any]:
    lines = [clean_text(x) for x in (section_text or "").splitlines()]
    items: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    cur_sub: Optional[Dict[str, Any]] = None

    def flush_sub():
        nonlocal cur_sub, cur
        if cur is None or cur_sub is None:
            return
        cur.setdefault("subitems", []).append(cur_sub)
        cur_sub = None

    def flush_item():
        nonlocal cur
        if cur is None:
            return
        cur["body"] = cur["body"].strip()
        for s in cur.get("subitems", []):
            s["body"] = s["body"].strip()
        items.append(cur)
        cur = None

    for ln in lines:
        if not ln:
            continue

        m1 = REQ_MAIN_RE.match(ln)
        m2 = REQ_SUB_RE.match(ln)

        if m1:
            flush_sub()
            flush_item()
            cur = {
                "no": int(m1.group("no")),
                "title": clean_text(m1.group("title")),
                "body": clean_text(m1.group("body")),
                "subitems": []
            }
            continue

        if m2 and cur is not None:
            flush_sub()
            cur_sub = {"no": m2.group("no"), "body": clean_text(m2.group("body"))}
            continue

        if cur_sub is not None:
            cur_sub["body"] += " " + ln
        elif cur is not None:
            cur["body"] += " " + ln

    flush_sub()
    flush_item()

    return {"count": len(items), "items": items, "raw": section_text.strip()}

def parse_training_objectives(section_text: str) -> Dict[str, Any]:
    raw = section_text.strip()
    lines = [clean_text(x) for x in raw.splitlines() if clean_text(x)]
    bullet_re = re.compile(r"^(\(?\d+\)?[\.、\)]|\-|\u2022)\s*(.+)$")
    bullets = []
    cur = ""
    for ln in lines:
        m = bullet_re.match(ln)
        if m:
            if cur:
                bullets.append(cur.strip())
            cur = m.group(2).strip()
        else:
            cur = (cur + " " + ln).strip() if cur else ln
    if cur:
        bullets.append(cur.strip())
    return {"raw": raw, "bullets": bullets, "count": len(bullets)}

# ---------------------------
# PDF 表格抽取（按页）
# ---------------------------

def extract_tables_by_page(pdf_bytes: bytes) -> Tuple[Dict[int, List[pd.DataFrame]], List[str]]:
    out: Dict[int, List[pd.DataFrame]] = {}
    pages_text: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, p in enumerate(pdf.pages, start=1):
            pages_text.append(p.extract_text() or "")
            tables = p.extract_tables() or []
            dfs: List[pd.DataFrame] = []
            for t in tables:
                try:
                    df = table_to_df(t)
                    if len(df) > 0 and len(df.columns) > 1:
                        dfs.append(df)
                except Exception:
                    continue
            if dfs:
                out[i] = dfs
    return out, pages_text

def guess_table_appendix(page_no: int) -> Optional[str]:
    if 6 <= page_no <= 9:
        return "附表1"
    if page_no == 10:
        return "附表2"
    if page_no == 11:
        return "附表3"
    if 12 <= page_no <= 16:
        return "附表4"
    if page_no == 17:
        return "附表5"
    return None

def split_by_direction(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    dir_col = None
    for c in df.columns:
        if "专业方向" in str(c) or str(c) == "方向":
            dir_col = c
            break

    if not dir_col:
        return {"未知": df}

    tmp = df.copy()
    tmp[dir_col] = tmp[dir_col].replace("", pd.NA).ffill().fillna("")

    def norm_dir(x: str) -> str:
        x = clean_text(x)
        if "焊" in x:
            return "焊接"
        if "无损" in x:
            return "无损检测"
        if x == "":
            return "共同"
        return x

    tmp["_方向归一"] = tmp[dir_col].map(norm_dir)
    parts = {}
    for k, g in tmp.groupby("_方向归一", dropna=False):
        parts[str(k)] = g.drop(columns=["_方向归一"]).reset_index(drop=True)
    return parts

@dataclass
class TablePack:
    appendix: Optional[str]
    title: str
    pages: List[int]
    direction: str
    columns: List[str]
    rows: List[List[str]]

def build_table_packs(
    tables_by_page: Dict[int, List[pd.DataFrame]],
    pages_text: List[str],
    appendix_titles: Dict[str, str]
) -> List[TablePack]:
    packs: List[TablePack] = []

    for page_no, dfs in tables_by_page.items():
        appendix = guess_table_appendix(page_no)
        base_title = appendix_titles.get(appendix, appendix or f"第{page_no}页表格")

        # 页内方向提示（附表2/3 常在页内写“专业方向：焊接/无损检测”，且一页两张表）
        page_txt = pages_text[page_no-1] if 0 <= page_no-1 < len(pages_text) else ""
        page_has_weld = "专业方向" in page_txt and "焊接" in page_txt
        page_has_ndt = "专业方向" in page_txt and "无损检测" in page_txt

        for ti, df in enumerate(dfs):
            # 默认标题
            title = base_title
            direction_hint = "未知"

            # 规则1：如果表内有“专业方向”列，优先按列拆分
            parts = split_by_direction(df)
            if list(parts.keys()) != ["未知"]:
                for direction, ddf in parts.items():
                    packs.append(TablePack(
                        appendix=appendix,
                        title=title,
                        pages=[page_no],
                        direction=direction,
                        columns=[str(c) for c in ddf.columns],
                        rows=ddf.astype(str).fillna("").values.tolist()
                    ))
                continue  # 本页该表已处理

            # 规则2：附表2/3：一页两表，通常第一表=焊接，第二表=无损检测
            if page_no in (10, 11) and len(dfs) >= 2 and page_has_weld and page_has_ndt:
                direction_hint = "焊接" if ti == 0 else "无损检测"
                title = f"{base_title}（{direction_hint}）"

            # 规则3：尝试从表内容判断
            sample = " ".join([clean_text(x) for x in df.head(3).astype(str).values.flatten().tolist()][:40])
            if direction_hint == "未知":
                if "焊接" in sample and "无损检测" not in sample:
                    direction_hint = "焊接"
                    title = f"{base_title}（焊接）"
                elif "无损检测" in sample and "焊接" not in sample:
                    direction_hint = "无损检测"
                    title = f"{base_title}（无损检测）"
                elif appendix == "附表1":
                    # 专业教学计划表里可能既无方向列又含共同内容
                    direction_hint = "共同"

            packs.append(TablePack(
                appendix=appendix,
                title=title,
                pages=[page_no],
                direction=direction_hint,
                columns=[str(c) for c in df.columns],
                rows=df.astype(str).fillna("").values.tolist()
            ))

    # 合并：同标题+方向+附表 的跨页表格
    merged: Dict[Tuple[str, str, Optional[str]], TablePack] = {}
    for p in packs:
        key = (p.title, p.direction, p.appendix)
        if key not in merged:
            merged[key] = p
        else:
            merged[key].pages.extend(p.pages)
            merged[key].rows.extend(p.rows)

    for v in merged.values():
        v.pages = sorted(set(v.pages))

    return list(merged.values())

# ---------------------------
# 全量抽取（缓存）
# ---------------------------

@st.cache_data(show_spinner=False)
def run_full_extract(pdf_bytes: bytes) -> Dict[str, Any]:
    pages_text_for_sections = extract_pages_text(pdf_bytes)
    all_lines = []
    for t in pages_text_for_sections:
        all_lines.extend((t or "").splitlines())

    appendix_titles = extract_appendix_titles(all_lines)
    sections = split_sections(pages_text_for_sections)

    obj_key = next((k for k in sections.keys() if k.startswith("一、") and ("培养目标" in k or "培养方案" in k)), None)
    req_key = next((k for k in sections.keys() if k.startswith("二、") and "毕业要求" in k), None)
    objectives = parse_training_objectives(sections.get(obj_key, "")) if obj_key else {"raw":"","bullets":[],"count":0}
    grad_req = parse_graduation_requirements(sections.get(req_key, "")) if req_key else {"count":0,"items":[],"raw":""}

    tables_by_page, pages_text = extract_tables_by_page(pdf_bytes)
    table_packs = build_table_packs(tables_by_page, pages_text, appendix_titles)

    return {
        "meta": {
            "sha256": sha256_bytes(pdf_bytes),
            "total_pages": len(pages_text_for_sections),
            "tables_pages": sorted(list(tables_by_page.keys())),
            "appendix_titles": appendix_titles,
            "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S")
        },
        "sections": sections,
        "training_objectives": objectives,
        "graduation_requirements": grad_req,
        "tables": [asdict(t) for t in table_packs],
        "raw_pages": [{"page": i+1, "text": pages_text_for_sections[i]} for i in range(len(pages_text_for_sections))]
    }

def make_tables_zip(table_packs: List[Dict[str, Any]]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        index = []
        for i, t in enumerate(table_packs, start=1):
            title = re.sub(r"[\\/:*?\"<>|]+", "_", t.get("title","table"))
            direction = re.sub(r"[\\/:*?\"<>|]+", "_", t.get("direction","未知"))
            appendix = t.get("appendix") or "NA"
            fname = f"{i:02d}_{appendix}_{direction}_{title}.csv"
            df = pd.DataFrame(t["rows"], columns=t["columns"])
            z.writestr(fname, df.to_csv(index=False, encoding="utf-8-sig"))
            index.append({**{k: t.get(k) for k in ["appendix","title","pages","direction"]}, "file": fname})
        z.writestr("index.json", json.dumps(index, ensure_ascii=False, indent=2))
    return bio.getvalue()

# ---------------------------
# Streamlit UI
# ---------------------------

st.set_page_config(page_title="培养方案PDF全量抽取（基础库）", layout="wide")

st.title("培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）")
st.caption("目标：一次性抽取培养方案中的全部可用信息，并允许人工校对，作为后续教学文件生成的唯一事实源。")

with st.sidebar:
    st.header("上传与抽取")
    up = st.file_uploader("上传培养方案 PDF", type=["pdf"])
    st.checkbox("对无文本页启用 OCR（可选）", value=False, disabled=True,
                help="建议把OCR做成单独可插拔模块，避免把OCR误差写入基础库。")
    start = st.button("开始全量抽取", type="primary", use_container_width=True)

if not up:
    st.info("请先在左侧上传你的《2024培养方案.pdf》之类的培养方案文件，然后点击“开始全量抽取”。")
    st.stop()

pdf_bytes = up.getvalue()

if start:
    with st.spinner("正在抽取：文本、章节结构、毕业要求、表格…"):
        result = run_full_extract(pdf_bytes)
    st.success("抽取完成。请在下方逐项校对。")
else:
    result = run_full_extract(pdf_bytes)

meta = result["meta"]
colA, colB, colC, colD = st.columns([1,1,1,3])
colA.metric("总页数", meta["total_pages"])
colB.metric("表格页数", len(meta["tables_pages"]))
colC.metric("OCR启用", "否")
colD.caption(f"SHA256: {meta['sha256']}")

dl1, dl2 = st.columns([1,1])
with dl1:
    st.download_button(
        "下载抽取结果 JSON（全量库）",
        data=json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"{up.name.rsplit('.',1)[0]}_full_extract.json",
        mime="application/json",
        use_container_width=True
    )
with dl2:
    zip_bytes = make_tables_zip(result["tables"])
    st.download_button(
        "下载表格 CSV ZIP（无需Excel依赖）",
        data=zip_bytes,
        file_name=f"{up.name.rsplit('.',1)[0]}_tables.zip",
        mime="application/zip",
        use_container_width=True
    )

tabs = st.tabs(["概览", "培养目标", "毕业要求(12条)", "大标题/章节", "附表/表格(可校对)", "原始页文本"])

with tabs[0]:
    st.subheader("结构化识别结果（可先在这里核对）")
    st.write("附表标题识别：")
    st.json(meta["appendix_titles"])
    st.write("已识别章节（按出现顺序）：")
    st.write(list(result["sections"].keys()))

with tabs[1]:
    st.subheader("培养目标（可编辑/校对）")
    obj = result["training_objectives"]
    st.write(f"识别到条目数：{obj['count']}")
    if obj["bullets"]:
        st.write("条目（自动抽取）：")
        for i, b in enumerate(obj["bullets"], start=1):
            st.markdown(f"- **{i}.** {b}")
    st.write("原文（建议对照PDF核对）：")
    st.text_area("培养目标原文", value=obj["raw"], height=240)

with tabs[2]:
    st.subheader("毕业要求（应为 12 大条 + 子条款）")
    req = result["graduation_requirements"]
    st.write(f"解析到大条数量：{req['count']}（理想值：12）")
    if req["count"] != 12:
        st.warning("当前解析到的大条数量不是 12。请展开“原文”核对：可能存在特殊换行/排版。")
    for item in req["items"]:
        st.markdown(f"### {item['no']}. {item['title']}")
        st.write(item["body"])
        subs = item.get("subitems", [])
        if subs:
            st.markdown("**子条款：**")
            for s in subs:
                st.markdown(f"- {s['no']} {s['body']}")
    with st.expander("原文（用于100%人工核对）", expanded=False):
        st.text_area("毕业要求原文", value=req["raw"], height=360)

with tabs[3]:
    st.subheader("章节（含“三~六”等大标题）")
    st.caption("目标：把培养方案中出现的所有大标题及其正文内容完整保留下来（即使正文是“……”也要保留）。")
    for k, v in result["sections"].items():
        with st.expander(k, expanded=False):
            st.text_area(k, value=v, height=220)

with tabs[4]:
    st.subheader("附表/表格（带表名、方向拆分、合并格补全）")
    st.caption("如果发现某张表“标题绑定不正确”，可按页码增加规则或在导出的JSON里手动改名后再导入。")

    all_dirs = sorted(set(t.get("direction","未知") for t in result["tables"]))
    dir_sel = st.multiselect("筛选专业方向", options=all_dirs, default=all_dirs)
    all_apps = sorted(set((t.get("appendix") or "NA") for t in result["tables"]))
    app_sel = st.multiselect("筛选附表", options=all_apps, default=all_apps)

    for i, t in enumerate(result["tables"], start=1):
        if t.get("direction","未知") not in dir_sel:
            continue
        if (t.get("appendix") or "NA") not in app_sel:
            continue

        title = t.get("title","(无标题)")
        appendix = t.get("appendix") or "NA"
        direction = t.get("direction","未知")
        pages = t.get("pages", [])

        st.markdown(f"### {i:02d}. [{appendix}] {title} — 方向：{direction}（页码：{pages}）")
        df = pd.DataFrame(t["rows"], columns=t["columns"])
        st.dataframe(df, use_container_width=True, hide_index=True)

with tabs[5]:
    st.subheader("原始页文本（用于定位漏抽/错抽）")
    pages = result["raw_pages"]
    pno = st.number_input("页码", min_value=1, max_value=len(pages), value=1, step=1)
    st.text_area(f"第 {pno} 页文本", value=pages[pno-1]["text"], height=520)
