# app.py
# Teaching Agent Suite (single-file demo)
# - Base plan 1-11 extraction
# - Appendix tables (7-10) auto extraction + classification
# - Streamlit keys fixed (no DuplicateElementKey / ValueAssignmentNotAllowedError)
# - Sidebar logo fixed (components.html or upload image)

from __future__ import annotations

import io
import re
import json
import time
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any

import pandas as pd
import streamlit as st
import pdfplumber
import streamlit.components.v1 as components

def payload_to_jsonable(payload: dict) -> dict:
    """把 payload 里的 DataFrame / numpy 类型转成 JSON 可序列化对象。"""
    if payload is None:
        return {}

    out = {}
    for k, v in payload.items():
        if isinstance(v, pd.DataFrame):
            df = v.copy()
            df = df.fillna("")
            out[k] = {
                "__type__": "dataframe",
                "columns": [str(c) for c in df.columns.tolist()],
                "data": df.astype(str).values.tolist(),
            }
        elif isinstance(v, dict):
            out[k] = payload_to_jsonable(v)
        elif isinstance(v, list):
            out[k] = [payload_to_jsonable(x) if isinstance(x, dict) else x for x in v]
        else:
            # 兜底：把 pandas/numpy 的标量转成 Python 标量
            try:
                if hasattr(v, "item") and callable(v.item):
                    out[k] = v.item()
                else:
                    out[k] = v
            except Exception:
                out[k] = str(v)
    return out

# -----------------------------
# Helpers
# -----------------------------
def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _short_id(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:10]


def _safe_text(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def _compact_lines(s: str) -> str:
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _join_pages(pages_text: List[str]) -> str:
    return _compact_lines("\n\n".join([t or "" for t in pages_text]))


def _read_pdf_pages_text(pdf_bytes: bytes) -> List[str]:
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            txt = p.extract_text() or ""
            pages.append(_compact_lines(txt))
    return pages


# -----------------------------
# Base plan (sections 1-11) text extraction (regex best-effort)
# -----------------------------
_SECTION_PATTERNS: List[Tuple[str, List[str]]] = [
    ("1", [r"一[、\.\s]*培养目标", r"1[、\.\s]*培养目标"]),
    ("2", [r"二[、\.\s]*毕业要求", r"2[、\.\s]*毕业要求"]),
    ("3", [r"三[、\.\s]*专业定位与特色", r"3[、\.\s]*专业定位与特色"]),
    ("4", [r"四[、\.\s]*主干学科.*?实践.*?环节", r"4[、\.\s]*主干学科"]),
    ("5", [r"五[、\.\s]*标准学制与授予学位", r"5[、\.\s]*标准学制"]),
    ("6", [r"六[、\.\s]*毕业条件", r"6[、\.\s]*毕业条件"]),
    ("7", [r"七[、\.\s]*专业教学计划表", r"7[、\.\s]*专业教学计划表"]),
    ("8", [r"八[、\.\s]*学分统计表", r"8[、\.\s]*学分统计表"]),
    ("9", [r"九[、\.\s]*教学进程表", r"9[、\.\s]*教学进程表"]),
    ("10", [r"十[、\.\s]*课程设置对毕业要求支撑关系表", r"10[、\.\s]*课程设置对毕业要求支撑关系表"]),
    ("11", [r"十一[、\.\s]*课程设置逻辑思维导图", r"11[、\.\s]*课程设置逻辑思维导图"]),
]


def _build_section_spans(full_text: str) -> Dict[str, Tuple[int, int]]:
    """
    Find each section heading position; return char spans [start,end) for each section.
    """
    hits: List[Tuple[str, int]] = []
    for sec_id, pats in _SECTION_PATTERNS:
        pos = None
        for pat in pats:
            m = re.search(pat, full_text)
            if m:
                pos = m.start()
                break
        if pos is not None:
            hits.append((sec_id, pos))

    hits.sort(key=lambda x: x[1])
    spans: Dict[str, Tuple[int, int]] = {}
    for i, (sec_id, start) in enumerate(hits):
        end = hits[i + 1][1] if i + 1 < len(hits) else len(full_text)
        spans[sec_id] = (start, end)
    return spans


def _extract_section_text(full_text: str, spans: Dict[str, Tuple[int, int]], sec_id: str) -> str:
    if sec_id not in spans:
        return ""
    s, e = spans[sec_id]
    chunk = full_text[s:e].strip()

    # 去掉标题行自身（尽量）
    chunk = re.sub(r"^\s*(一|二|三|四|五|六|七|八|九|十|十一|\d+)[、\.\s]*[^\n]{0,30}\n", "", chunk)
    return _compact_lines(chunk)


# -----------------------------
# Appendix table extraction (pdfplumber) + classification
# -----------------------------
def _valid_table_settings_lines() -> dict:
    """
    Safe pdfplumber settings (avoid TableSettings.resolve TypeError).
    """
    # 这些 key 是 pdfplumber 常用且相对稳定的
    return dict(
        vertical_strategy="lines",
        horizontal_strategy="lines",
        snap_tolerance=3,
        join_tolerance=3,
        edge_min_length=3,
        intersection_tolerance=3,
        text_tolerance=3,
    )


def _extract_tables_from_pages(pdf_bytes: bytes, page_idx_list: List[int]) -> List[List[List[str]]]:
    """
    Return: list of tables; table is list of rows; row is list of cell strings.
    """
    out: List[List[List[str]]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for idx in page_idx_list:
            if idx < 0 or idx >= len(pdf.pages):
                continue
            page = pdf.pages[idx]

            # try "lines" settings first, fallback to default
            tables = []
            try:
                tables = page.extract_tables(table_settings=_valid_table_settings_lines()) or []
            except TypeError:
                tables = page.extract_tables() or []
            except Exception:
                # 再兜底一次：有些 PDF 会在 extract_tables 抛未知异常
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []

            for t in tables:
                # t: list[list[cell]]
                norm = []
                for row in t:
                    norm.append([_safe_text(c) for c in row])
                out.append(norm)
    return out


def _table_to_df(table_rows: List[List[str]]) -> pd.DataFrame:
    # 过滤全空行
    rows = [r for r in table_rows if any(_safe_text(x) for x in r)]
    if not rows:
        return pd.DataFrame()

    # 统一列数
    max_cols = max(len(r) for r in rows)
    rows = [r + [""] * (max_cols - len(r)) for r in rows]

    # 尝试识别表头：第一行如果“像表头”
    header = rows[0]
    header_join = " ".join(header)
    header_like = any(k in header_join for k in ["课程", "学分", "周次", "指标", "支撑", "合计", "课程编码", "课程名称"])
    if header_like:
        cols = [c if c else f"列{i+1}" for i, c in enumerate(header)]
        df = pd.DataFrame(rows[1:], columns=_dedup_cols(cols))
    else:
        cols = [f"列{i+1}" for i in range(max_cols)]
        df = pd.DataFrame(rows, columns=cols)

    return _clean_df(df)


def _dedup_cols(cols: List[str]) -> List[str]:
    seen = {}
    out = []
    for c in cols:
        c0 = c.strip() or "列"
        if c0 not in seen:
            seen[c0] = 1
            out.append(c0)
        else:
            seen[c0] += 1
            out.append(f"{c0}_{seen[c0]}")
    return out


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    # 去掉全空列
    df = df.copy()
    df.replace({None: ""}, inplace=True)

    # 把 "nan" 文本也清掉
    df = df.applymap(lambda x: "" if str(x).strip().lower() == "nan" else str(x).strip())

    # drop all-empty rows/cols
    df = df.loc[~df.apply(lambda r: all((str(x).strip() == "") for x in r), axis=1)]
    df = df.loc[:, ~df.apply(lambda c: all((str(x).strip() == "") for x in c), axis=0)]

    # 再次 reset index
    df = df.reset_index(drop=True)

    # 有些 PDF 表会把 “四 五 六 七 八” 这种学期行抽成一行混在数据里：若该行多数字段是中文数字，去掉
    def _looks_like_semester_row(row: pd.Series) -> bool:
        tokens = [str(x).strip() for x in row.tolist() if str(x).strip()]
        if len(tokens) < 3:
            return False
        cn_nums = set(list("一二三四五六七八九十"))
        hits = sum(1 for t in tokens if (len(t) == 1 and t in cn_nums))
        return hits >= 3

    if not df.empty:
        df = df.loc[~df.apply(_looks_like_semester_row, axis=1)].reset_index(drop=True)

    return df


def _table_signature_text(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""
    head = " ".join([str(c) for c in df.columns.tolist()])
    top_rows = []
    for i in range(min(3, len(df))):
        top_rows.append(" ".join([str(x) for x in df.iloc[i].tolist()]))
    return (head + " " + " ".join(top_rows)).lower()


def _classify_table(df: pd.DataFrame) -> Tuple[str, int]:
    """
    Return (section_id, score). section_id in {"7","8","9","10"} or ("",0)
    """
    s = _table_signature_text(df)

    # 专业教学计划表：典型字段：课程编码/课程名称/学分/总学时/考核/开课
    score7 = 0
    for k in ["课程编码", "课程代码", "课程名称", "学分", "总学时", "考核", "开课"]:
        if k in s:
            score7 += 3

    # 学分统计表：典型字段：通识/专业/实践/必修/选修/小计/合计
    score8 = 0
    for k in ["学分统计", "必修", "选修", "通识", "专业", "实践", "合计", "小计"]:
        if k in s:
            score8 += 3

    # 教学进程表：典型字段：周次/教学内容/学时/实验/章节/进度
    score9 = 0
    for k in ["周次", "教学内容", "进度", "章节", "学时", "实验"]:
        if k in s:
            score9 += 3

    # 支撑关系表：典型字段：毕业要求/指标点/支撑/课程对…/达成
    score10 = 0
    for k in ["毕业要求", "指标点", "支撑", "达成", "对应", "课程设置对毕业要求"]:
        if k in s:
            score10 += 3

    scores = [("7", score7), ("8", score8), ("9", score9), ("10", score10)]
    best = max(scores, key=lambda x: x[1])
    if best[1] >= 6:
        return best
    return ("", 0)


def extract_appendix_tables_best_effort(pdf_bytes: bytes, pages_text: List[str]) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any]]:
    """
    从 PDF 末尾页面抽取表格，自动分类分配到 7-10。
    Return:
      tables_map: {"7":df, "8":df, "9":df, "10":df}
      debug_meta: helpful debug info
    """
    n = len(pages_text)
    tail_pages = list(range(max(0, n - 12), n))  # 默认抽最后 12 页
    raw_tables = _extract_tables_from_pages(pdf_bytes, tail_pages)

    dfs: List[pd.DataFrame] = []
    for t in raw_tables:
        df = _table_to_df(t)
        if df is None or df.empty:
            continue
        # 太小的表不要（防止噪声）
        if df.shape[0] < 2 and df.shape[1] < 3:
            continue
        dfs.append(df)

    assigned: Dict[str, pd.DataFrame] = {}
    used_idx = set()
    scored: List[Tuple[int, str, int]] = []  # (idx, sec, score)
    for i, df in enumerate(dfs):
        sec, score = _classify_table(df)
        if sec:
            scored.append((i, sec, score))

    # 贪心：按 score 从大到小分配，确保每个 sec 只取一张表且不重复
    scored.sort(key=lambda x: x[2], reverse=True)
    for i, sec, score in scored:
        if sec in assigned:
            continue
        if i in used_idx:
            continue
        assigned[sec] = dfs[i].copy(deep=True)
        used_idx.add(i)

    debug = {
        "tail_pages": tail_pages,
        "raw_tables_count": len(raw_tables),
        "dfs_count": len(dfs),
        "scored": scored[:20],
        "assigned": {k: list(v.shape) for k, v in assigned.items()},
    }
    return assigned, debug


def base_plan_from_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    pages = _read_pdf_pages_text(pdf_bytes)
    full = _join_pages(pages)
    spans = _build_section_spans(full)

    base = {}
    for sec_id, _ in _SECTION_PATTERNS:
        base[sec_id] = _extract_section_text(full, spans, sec_id)

    # 再补：7-11 可能正文只有标题，这里给提示
    for sec_id in ["7", "8", "9", "10", "11"]:
        if not base.get(sec_id, "").strip():
            base[sec_id] = f"{sec_id}：正文可能仅有标题；请尝试从 PDF 末尾附表自动抽取。"

    auto_tables, debug_meta = extract_appendix_tables_best_effort(pdf_bytes, pages)

    return dict(
        pages=pages,
        full_text=full,
        sections=base,              # 1-11 text
        tables=auto_tables,         # 7-10 tables
        debug=debug_meta,
    )


# -----------------------------
# UI
# -----------------------------
@dataclass
class Project:
    project_id: str
    name: str
    updated_at: str


def _init_state():
    if "projects" not in st.session_state:
        # 默认项目
        pid = _short_id(_now_str())
        st.session_state.projects = [
            Project(project_id=pid, name=f"默认项目-{time.strftime('%Y%m%d-%H%M')}", updated_at=_now_str())
        ]
        st.session_state.active_project_id = pid

    if "project_data" not in st.session_state:
        # project_id -> payload
        st.session_state.project_data = {}

    if "logo_bytes" not in st.session_state:
        st.session_state.logo_bytes = None


def ui_sidebar_brand():
    with st.sidebar:
        col1, col2 = st.columns([1, 4])
        with col1:
            if st.session_state.logo_bytes:
                st.image(st.session_state.logo_bytes, width=44)
            else:
                # 默认一个简单 SVG 圆形 TA
                svg = """
                <div style="width:44px;height:44px;border-radius:50%;
                            background:#2f6fed;display:flex;align-items:center;justify-content:center;
                            color:white;font-weight:800;font-family:Arial;">
                  TA
                </div>
                """
                components.html(svg, height=48)

        with col2:
            st.markdown("**Teaching Agent Suite**")
            st.caption("v0.6 (base 1–11 + appendix tables + logo fixed)")

        # Logo 上传（可选）
        up = st.file_uploader("上传 Logo（可选，png/jpg）", type=["png", "jpg", "jpeg"], key="logo_uploader")
        if up is not None:
            st.session_state.logo_bytes = up.getvalue()


def ui_project_sidebar() -> Project:
    ui_sidebar_brand()

    with st.sidebar:
        st.divider()
        st.markdown("### 项目")
        options = {p.project_id: p for p in st.session_state.projects}
        labels = {p.project_id: f"{p.name} ({p.project_id})" for p in st.session_state.projects}

        pid = st.selectbox(
            "选择项目",
            options=list(labels.keys()),
            format_func=lambda x: labels[x],
            index=list(labels.keys()).index(st.session_state.active_project_id),
            key="project_select",
        )
        st.session_state.active_project_id = pid
        return options[pid]


def _render_top_header(project: Project):
    # 这里一定要 unsafe_allow_html=True，否则会像你截图那样把 html 当文字显示
    html = f"""
    <div style="border:1px solid #e7eefc; background:#f6f9ff; padding:18px 18px; border-radius:14px;">
      <div style="font-weight:900; font-size:28px;">教学文件工作台</div>
      <div style="color:#666; margin-top:4px; font-size:14px;">
        项目： <b>{project.name}</b>（{project.project_id}） · 最后更新： {project.updated_at}
      </div>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


def ui_base_training_plan(project: Project):
    st.subheader("培养方案基座（全量内容库）")
    st.caption("上传培养方案 PDF → 抽取填充 1–11 → 并尝试从末尾附表自动抽表填充 7–10。")

    left, right = st.columns([1, 1.4], gap="large")

    with left:
        pdf = st.file_uploader("上传培养方案 PDF（可选）", type=["pdf"], key=f"pdf_{project.project_id}")

        if st.button("抽取并写入基座", use_container_width=True, type="primary", key=f"extract_btn_{project.project_id}"):
            if not pdf:
                st.warning("请先上传 PDF。")
            else:
                pdf_bytes = pdf.getvalue()
                payload = base_plan_from_pdf(pdf_bytes)
                st.session_state.project_data[project.project_id] = payload
                # 更新时间
                for i, p in enumerate(st.session_state.projects):
                    if p.project_id == project.project_id:
                        st.session_state.projects[i] = Project(
                            project_id=p.project_id,
                            name=p.name,
                            updated_at=_now_str()
                        )
                        break
                st.success("已抽取并写入基座。右侧已联动填充。")

        # 下载 JSON
        payload = st.session_state.project_data.get(project.project_id)
        if payload:
            json_payload = payload_to_jsonable(payload)

            st.download_button(
                "下载基座 JSON",
                data=json.dumps(json_payload, ensure_ascii=False, indent=2).encode("utf-8"),
                file_name=f"base_{project.project_id}.json",
                mime="application/json",
                use_container_width=True,
                key=f"dl_{project.project_id}",
            )


        st.divider()
        if payload:
            missing = [k for k in [str(i) for i in range(1, 12)] if not payload["sections"].get(k, "").strip()]
            if missing:
                st.warning(f"检查：缺少栏目 {missing}")
            else:
                st.success("1–11 栏目均已存在（仍建议人工快速扫读）。")

        with st.expander("调试：分页原文 (raw_pages_text)"):
            if payload:
                st.write(payload["pages"])
            else:
                st.info("先抽取后可见。")

        with st.expander("调试：附表抽取信息 (appendix_debug)"):
            if payload:
                st.json(payload["debug"])
            else:
                st.info("先抽取后可见。")

    with right:
        st.markdown("#### 培养方案内容（按栏目展示，可编辑）")

        payload = st.session_state.project_data.get(project.project_id)
        if not payload:
            st.info("请先在左侧上传 PDF 并点击“抽取并写入基座”。")
            return

        sections = payload["sections"]
        tables = payload.get("tables", {})

        # 顶部目录
        toc = [
            ("1", "培养目标"),
            ("2", "毕业要求"),
            ("3", "专业定位与特色"),
            ("4", "主干学科/核心课程/实践环节"),
            ("5", "标准学制与授予学位"),
            ("6", "毕业条件"),
            ("7", "专业教学计划表（附表1）"),
            ("8", "学分统计表（附表2）"),
            ("9", "教学进程表（附表3）"),
            ("10", "支撑关系表（附表4）"),
            ("11", "逻辑思维导图（附表5）"),
        ]
        # 用 radio 模拟“标签”
        sec_pick = st.radio(
            "栏目",
            options=[x[0] for x in toc],
            format_func=lambda x: dict(toc)[x],
            horizontal=True,
            key=f"sec_radio_{project.project_id}",
        )

        # 展示当前栏目
        title_map = dict(toc)
        st.markdown(f"##### {sec_pick}、{title_map[sec_pick]}")

        # 6：只展示 6 本身，别把 7-11 混进去（通常混进去是因为 span 切分失败）
        # 这里已经靠 spans 切分；再加一道“截断”兜底：遇到下一节标题就截断
        def _truncate_at_next_heading(txt: str, next_sec_id: str) -> str:
            if not txt:
                return ""
            next_title = dict(toc).get(next_sec_id, "")
            if not next_title:
                return txt
            m = re.search(rf"\n\s*{next_sec_id}[、\.\s]*{re.escape(next_title)}", "\n" + txt)
            if m:
                return _compact_lines(txt[: m.start()])
            return txt

        # Text area
        txt = sections.get(sec_pick, "")

        if sec_pick == "6":
            txt = _truncate_at_next_heading(txt, "7")

        st.text_area(
            f"{sec_pick} 文本抽取结果",
            value=txt,
            height=220,
            key=f"sec_text_{project.project_id}_{sec_pick}",
        )

        # 7-10：表格区（自动抽取）
        if sec_pick in ["7", "8", "9", "10"]:
            st.markdown("###### 表格区（可编辑，行可增删）")

            df0 = tables.get(sec_pick)
            if df0 is None or df0 is False or (isinstance(df0, pd.DataFrame) and df0.empty):
                st.info("未自动抽取到该附表（可能 PDF 表格是图片或线条不规则）。你可以手工补全。")
                df0 = pd.DataFrame()

            # 注意：不要把 widget key 的 session_state 自己再赋值，否则会 ValueAssignmentNotAllowed
            editor_key = f"tbl_editor_{project.project_id}_{sec_pick}"
            edited = st.data_editor(
                df0,
                num_rows="dynamic",
                use_container_width=True,
                key=editor_key,
            )
            # 把结果存到另一个 key（不覆盖 widget key）
            st.session_state[f"{editor_key}__value"] = edited

        if sec_pick == "11":
            st.info("逻辑思维导图（附表5）通常是图片/流程图，不一定能从表格抽取。可后续加：末页图片抽取。")


def main():
    st.set_page_config(page_title="Teaching Agent Suite", page_icon="🧠", layout="wide")
    _init_state()

    prj = ui_project_sidebar()

    _render_top_header(prj)

    # tabs
    tab1, tab2, tab3 = st.tabs(["培养方案基座", "模板化教学文件", "项目概览"])
    with tab1:
        ui_base_training_plan(prj)
    with tab2:
        st.info("这里留给你的“模板化教学文件”模块（你原来的生成/校对/导出流程可以放回这里）。")
    with tab3:
        st.write("项目ID：", prj.project_id)
        st.write("最后更新：", prj.updated_at)
        payload = st.session_state.project_data.get(prj.project_id)
        if payload:
            st.write("已写入基座：✅")
            st.write("已抽取附表：", payload.get("debug", {}).get("assigned", {}))
        else:
            st.write("已写入基座：❌")


if __name__ == "__main__":
    main()
