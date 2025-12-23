# app.py
# Teaching Agent Suite (single-file demo)
# - Base plan 1-11 extraction
# - Appendix tables (7-10) auto extraction + classification + page-anchored search
# - Streamlit keys fixed (no DuplicateElementKey / ValueAssignmentNotAllowedError)
# - Sidebar logo fixed (HTML render or upload image)
# - Download JSON fixed (no TypeError / non-serializable)

from __future__ import annotations

import io
import re
import json
import time
import hashlib
import base64
import datetime as _dt
from pathlib import Path
from decimal import Decimal
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any

import pandas as pd
import streamlit as st
import pdfplumber


# ============================================================
# JSON serialization helper
# ============================================================
def payload_to_jsonable(obj):
    """递归把各种常见不可 JSON 序列化对象转成可序列化结构。"""
    # pandas
    try:
        import pandas as pd

        if isinstance(obj, pd.DataFrame):
            df = obj.copy().fillna("")
            return {
                "__type__": "dataframe",
                "columns": [str(c) for c in df.columns.tolist()],
                "data": df.astype(str).values.tolist(),
            }
        if hasattr(pd, "Timestamp") and isinstance(obj, pd.Timestamp):
            return obj.isoformat()
    except Exception:
        pass

    # numpy
    try:
        import numpy as np

        if isinstance(obj, (np.integer, np.floating, np.bool_)):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:
        pass

    # bytes（比如 pdf_bytes）
    if isinstance(obj, (bytes, bytearray)):
        return {
            "__type__": "bytes_base64",
            "data": base64.b64encode(bytes(obj)).decode("ascii"),
        }

    # datetime / date
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()

    # Path / Decimal
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)

    # set/tuple
    if isinstance(obj, (set, tuple)):
        return [payload_to_jsonable(x) for x in obj]

    # dict / list
    if isinstance(obj, dict):
        return {str(k): payload_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [payload_to_jsonable(x) for x in obj]

    # 其它：尽量原样返回，必要时转字符串
    try:
        json.dumps(obj)  # probe
        return obj
    except Exception:
        return str(obj)


# ============================================================
# Helpers
# ============================================================
def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _short_id(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:10]


def _safe_text(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def _compact_lines(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
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


# ============================================================
# Base plan (sections 1-11) text extraction (regex best-effort)
# 关键改进：避免目录(Toc)干扰 -> 每个标题取“最后一次出现”的位置
# ============================================================
_SECTION_PATTERNS: List[Tuple[str, List[str]]] = [
    ("1", [r"一[、\.\s]*培养目标", r"1[、\.\s]*培养目标"]),
    ("2", [r"二[、\.\s]*毕业要求", r"2[、\.\s]*毕业要求"]),
    ("3", [r"三[、\.\s]*专业定位与特色", r"3[、\.\s]*专业定位与特色"]),
    ("4", [r"四[、\.\s]*主干学科", r"4[、\.\s]*主干学科"]),
    ("5", [r"五[、\.\s]*标准学制与授予学位", r"5[、\.\s]*标准学制"]),
    ("6", [r"六[、\.\s]*毕业条件", r"6[、\.\s]*毕业条件"]),
    ("7", [r"七[、\.\s]*专业教学计划表", r"7[、\.\s]*专业教学计划表"]),
    ("8", [r"八[、\.\s]*学分统计表", r"8[、\.\s]*学分统计表"]),
    ("9", [r"九[、\.\s]*教学进程表", r"9[、\.\s]*教学进程表"]),
    ("10", [r"十[、\.\s]*课程设置对毕业要求支撑关系表", r"10[、\.\s]*课程设置对毕业要求支撑关系表"]),
    ("11", [r"十一[、\.\s]*课程设置逻辑思维导图", r"11[、\.\s]*课程设置逻辑思维导图"]),
]


def _find_last_heading_pos(full_text: str, patterns: List[str]) -> Optional[int]:
    """返回该标题在全文中最后一次出现的位置，尽量绕开前面的目录。"""
    last_pos = None
    for pat in patterns:
        for m in re.finditer(pat, full_text):
            last_pos = m.start()
    return last_pos


def _build_section_spans(full_text: str) -> Dict[str, Tuple[int, int]]:
    """
    Find each section heading position (prefer last occurrence); return char spans [start,end) for each section.
    """
    hits: List[Tuple[str, int]] = []
    for sec_id, pats in _SECTION_PATTERNS:
        pos = _find_last_heading_pos(full_text, pats)
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
    chunk = re.sub(
        r"^\s*(一|二|三|四|五|六|七|八|九|十|十一|\d+)[、\.\s]*[^\n]{0,30}\n",
        "",
        chunk,
    )
    return _compact_lines(chunk)


# ============================================================
# Appendix table extraction (pdfplumber) + classification
# 关键改进：
# 1) 先用 pages_text 锚定“附表1/2/3/4”所在页，再在附近页抽表
# 2) 抽表返回带 page_idx，避免不同附表互相串
# 3) 每个附表取“最匹配(高分)+更大(面积)”的那张
# ============================================================
def _valid_table_settings_lines() -> dict:
    """Safe pdfplumber settings (avoid TableSettings.resolve TypeError)."""
    return dict(
        vertical_strategy="lines",
        horizontal_strategy="lines",
        snap_tolerance=3,
        join_tolerance=3,
        edge_min_length=3,
        intersection_tolerance=3,
        text_tolerance=3,
    )


def _extract_tables_from_pages(
    pdf_bytes: bytes, page_idx_list: List[int]
) -> List[Tuple[int, List[List[str]]]]:
    """
    Return: list of (page_idx, table_rows)
      table_rows: list of rows; row: list of cell strings
    """
    out: List[Tuple[int, List[List[str]]]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for idx in page_idx_list:
            if idx < 0 or idx >= len(pdf.pages):
                continue
            page = pdf.pages[idx]

            tables = []
            try:
                tables = page.extract_tables(table_settings=_valid_table_settings_lines()) or []
            except TypeError:
                tables = page.extract_tables() or []
            except Exception:
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []

            for t in tables:
                norm = []
                for row in t:
                    norm.append([_safe_text(c) for c in row])
                out.append((idx, norm))
    return out


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

    df = df.copy()
    df.replace({None: ""}, inplace=True)

    # 把 "nan" 文本也清掉
    df = df.applymap(lambda x: "" if str(x).strip().lower() == "nan" else str(x).strip())

    # drop all-empty rows/cols
    df = df.loc[~df.apply(lambda r: all((str(x).strip() == "") for x in r), axis=1)]
    df = df.loc[:, ~df.apply(lambda c: all((str(x).strip() == "") for x in c), axis=0)]
    df = df.reset_index(drop=True)

    # 删除“学期中文数字行”噪声（四 五 六 七 八…）
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


def _table_to_df(table_rows: List[List[str]]) -> pd.DataFrame:
    rows = [r for r in table_rows if any(_safe_text(x) for x in r)]
    if not rows:
        return pd.DataFrame()

    max_cols = max(len(r) for r in rows)
    rows = [r + [""] * (max_cols - len(r)) for r in rows]

    header = rows[0]
    header_join = " ".join(header)
    header_like = any(
        k in header_join
        for k in ["课程", "学分", "周次", "指标", "支撑", "合计", "课程编码", "课程名称", "毕业要求"]
    )
    if header_like:
        cols = [c if c else f"列{i+1}" for i, c in enumerate(header)]
        df = pd.DataFrame(rows[1:], columns=_dedup_cols(cols))
    else:
        cols = [f"列{i+1}" for i in range(max_cols)]
        df = pd.DataFrame(rows, columns=cols)

    return _clean_df(df)


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

    score7 = 0
    for k in ["课程编码", "课程代码", "课程名称", "学分", "总学时", "考核", "开课"]:
        if k in s:
            score7 += 3

    score8 = 0
    for k in ["学分统计", "必修", "选修", "通识", "专业", "实践", "合计", "小计"]:
        if k in s:
            score8 += 3

    score9 = 0
    for k in ["周次", "教学内容", "进度", "章节", "学时", "实验"]:
        if k in s:
            score9 += 3

    score10 = 0
    for k in ["毕业要求", "指标点", "支撑", "达成", "对应", "课程设置对毕业要求"]:
        if k in s:
            score10 += 3

    scores = [("7", score7), ("8", score8), ("9", score9), ("10", score10)]
    best = max(scores, key=lambda x: x[1])
    if best[1] >= 6:
        return best
    return ("", 0)


def _find_appendix_anchor_pages(pages_text: List[str]) -> Dict[str, List[int]]:
    """
    在 pages_text 中寻找附表1~4 的锚点页（可能写成“附表 1”“附表1”“（附表1）”等）。
    返回: {"7":[...], "8":[...], "9":[...], "10":[...]} 的页号列表(0-based)
    """
    pats = {
        "7": [r"附表\s*1", r"专业教学计划表", r"七[、\.\s]*专业教学计划表"],
        "8": [r"附表\s*2", r"学分统计表", r"八[、\.\s]*学分统计表"],
        "9": [r"附表\s*3", r"教学进程表", r"九[、\.\s]*教学进程表"],
        "10": [r"附表\s*4", r"支撑关系表", r"课程设置对毕业要求支撑关系表", r"十[、\.\s]*课程设置对毕业要求支撑关系表"],
    }
    anchors: Dict[str, List[int]] = {k: [] for k in pats.keys()}
    for i, t in enumerate(pages_text):
        tt = t or ""
        for sec, ps in pats.items():
            for p in ps:
                if re.search(p, tt):
                    anchors[sec].append(i)
                    break
    # 去重、排序
    for k in anchors:
        anchors[k] = sorted(list(set(anchors[k])))
    return anchors


def extract_appendix_tables_best_effort(
    pdf_bytes: bytes, pages_text: List[str]
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any]]:
    """
    从 PDF 末尾页面 + 附表锚点页面抽取表格，自动分类分配到 7-10。
    Return:
      tables_map: {"7":df, "8":df, "9":df, "10":df}
      debug_meta: helpful debug info
    """
    n = len(pages_text)

    # 1) 先找锚点页
    anchors = _find_appendix_anchor_pages(pages_text)

    # 2) 生成候选抽取页：锚点附近 + 末尾兜底
    page_set = set()
    for sec, idxs in anchors.items():
        for idx in idxs[-3:]:  # 只取最后几个锚点（避免目录页）
            for d in [-2, -1, 0, 1, 2, 3]:
                j = idx + d
                if 0 <= j < n:
                    page_set.add(j)

    # 兜底：最后 12 页
    for j in range(max(0, n - 12), n):
        page_set.add(j)

    page_idx_list = sorted(page_set)

    # 3) 抽表（带页号）
    raw_tables = _extract_tables_from_pages(pdf_bytes, page_idx_list)

    # 4) 变成 df，并记录来源页
    dfs: List[Tuple[int, pd.DataFrame]] = []
    for page_idx, t in raw_tables:
        df = _table_to_df(t)
        if df is None or df.empty:
            continue
        # 过滤极小噪声表
        if df.shape[0] < 2 and df.shape[1] < 3:
            continue
        dfs.append((page_idx, df))

    # 5) 评分+分类
    candidates: List[Dict[str, Any]] = []
    for i, (pidx, df) in enumerate(dfs):
        sec, score = _classify_table(df)
        area = int(df.shape[0] * df.shape[1])
        candidates.append(
            dict(
                i=i,
                page=pidx,
                sec=sec,
                score=score,
                area=area,
                shape=list(df.shape),
            )
        )

    # 6) 分配：优先 “score高”，再看“同分更大面积”，再偏向“靠近该附表锚点页”
    assigned: Dict[str, pd.DataFrame] = {}
    chosen_idx = set()

    def _anchor_distance(sec: str, page: int) -> int:
        idxs = anchors.get(sec, [])
        if not idxs:
            return 9999
        # 距离最近的锚点
        return min(abs(page - a) for a in idxs)

    # 为每个候选生成一个全局排序键
    ranked = []
    for c in candidates:
        sec = c["sec"]
        if not sec:
            continue
        dist = _anchor_distance(sec, c["page"])
        # 排序：score desc, area desc, dist asc
        ranked.append((c["sec"], c["i"], c["score"], c["area"], dist, c["page"]))
    ranked.sort(key=lambda x: (x[2], x[3], -x[4] * 0), reverse=True)  # 先粗排
    # 更严格：我们自己再按规则挑（每个 sec 选最优）
    for sec in ["7", "8", "9", "10"]:
        # sec 专属候选
        sec_cands = []
        for c in candidates:
            if c["sec"] != sec:
                continue
            dist = _anchor_distance(sec, c["page"])
            sec_cands.append((c["score"], c["area"], -dist, c["page"], c["i"]))
        # score高、面积大、距离近优先
        sec_cands.sort(reverse=True)
        for score, area, neg_dist, page, i in sec_cands:
            if i in chosen_idx:
                continue
            assigned[sec] = dfs[i][1].copy(deep=True)
            chosen_idx.add(i)
            break

    # 7) 如果某些 sec 没分到，但有锚点页：就从锚点页附近的所有表里挑“最大”的那张作为兜底
    for sec in ["7", "8", "9", "10"]:
        if sec in assigned:
            continue
        idxs = anchors.get(sec, [])
        if not idxs:
            continue
        near_pages = set()
        for idx in idxs[-2:]:
            for d in [-2, -1, 0, 1, 2, 3]:
                j = idx + d
                if 0 <= j < n:
                    near_pages.add(j)
        best_i = None
        best_area = -1
        for i, (pidx, df) in enumerate(dfs):
            if i in chosen_idx:
                continue
            if pidx not in near_pages:
                continue
            area = int(df.shape[0] * df.shape[1])
            if area > best_area:
                best_area = area
                best_i = i
        if best_i is not None:
            assigned[sec] = dfs[best_i][1].copy(deep=True)
            chosen_idx.add(best_i)

    debug = {
        "candidate_pages": page_idx_list,
        "anchors": anchors,
        "raw_tables_count": len(raw_tables),
        "dfs_count": len(dfs),
        "candidates_top": sorted(candidates, key=lambda x: (x["score"], x["area"]), reverse=True)[:30],
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

    # 7-11 正文可能只有标题：提示
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


# ============================================================
# UI
# ============================================================
@dataclass
class Project:
    project_id: str
    name: str
    updated_at: str


def _init_state():
    if "projects" not in st.session_state:
        pid = _short_id(_now_str())
        st.session_state.projects = [
            Project(project_id=pid, name=f"默认项目-{time.strftime('%Y%m%d-%H%M')}", updated_at=_now_str())
        ]
        st.session_state.active_project_id = pid

    if "project_data" not in st.session_state:
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
                # ✅ 不再用 components.html（有时会显示成文本/或触发 sidebar.components 相关问题）
                # ✅ 用 markdown + unsafe_allow_html 100%稳
                svg = """
                <div style="width:44px;height:44px;border-radius:50%;
                            background:#2f6fed;display:flex;align-items:center;justify-content:center;
                            color:white;font-weight:800;font-family:Arial;">
                  TA
                </div>
                """
                st.markdown(svg, unsafe_allow_html=True)

        with col2:
            st.markdown("**Teaching Agent Suite**")
            st.caption("v0.6 (base 1–11 + appendix tables + logo fixed)")

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
    # ✅ 必须 unsafe_allow_html=True，否则会把 HTML 当纯文本显示
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
                            updated_at=_now_str(),
                        )
                        break

                st.success("已抽取并写入基座。右侧已联动填充。")

        # 下载 JSON（✅ 修复：不能在 download_button 参数里乱写赋值；同时先做 jsonable）
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
        title_map = dict(toc)

        sec_pick = st.radio(
            "栏目",
            options=[x[0] for x in toc],
            format_func=lambda x: title_map[x],
            horizontal=True,
            key=f"sec_radio_{project.project_id}",
        )

        st.markdown(f"##### {sec_pick}、{title_map[sec_pick]}")

        # 6 内容过长兜底截断：遇到 “七、专业教学计划表” 就截断
        def _truncate_at_next_heading(txt: str, next_sec_id: str) -> str:
            if not txt:
                return ""
            next_title = title_map.get(next_sec_id, "")
            if not next_title:
                return txt
            # 兼容 “七、专业教学计划表” 或 “7 专业教学计划表”
            pat = rf"(\n\s*七[、\.\s]*专业教学计划表|\n\s*7[、\.\s]*专业教学计划表)"
            m = re.search(pat, "\n" + txt)
            if m:
                return _compact_lines(txt[: m.start()])
            return txt

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
            if df0 is None or (isinstance(df0, pd.DataFrame) and df0.empty):
                st.info("未自动抽取到该附表（可能 PDF 表格是图片/线条不规则/或附表布局特殊）。你可以手工补全。")
                df0 = pd.DataFrame()

            editor_key = f"tbl_editor_{project.project_id}_{sec_pick}"
            edited = st.data_editor(
                df0,
                num_rows="dynamic",
                use_container_width=True,
                key=editor_key,
            )
            # ✅ 不覆盖 widget key，另存一份
            st.session_state[f"{editor_key}__value"] = edited

        if sec_pick == "11":
            st.info("逻辑思维导图（附表5）通常是图片/流程图，pdfplumber 的表格抽取不一定有效。可后续加“末页图片抽取”。")


def main():
    st.set_page_config(page_title="Teaching Agent Suite", page_icon="🧠", layout="wide")
    _init_state()

    prj = ui_project_sidebar()
    _render_top_header(prj)

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
