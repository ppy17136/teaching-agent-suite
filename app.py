# app.py
# 培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）— 基础底库
# 依赖建议：streamlit, pdfplumber, pandas
# 可选：openpyxl 或 xlsxwriter（用于导出xlsx）；pytesseract（用于OCR）

import io
import re
import json
import hashlib
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st

try:
    import pdfplumber
except Exception as e:
    pdfplumber = None

# ----------------------------
# UI config
# ----------------------------
st.set_page_config(page_title="培养方案PDF全量抽取（基础底库）", layout="wide")

st.markdown(
    """
<style>
/* 让主区域更“铺满”，减少左右空白 */
.block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 98vw; }
</style>
""",
    unsafe_allow_html=True,
)

st.title("培养方案 PDF 全量抽取（文本 + 表格 + 结构化解析）")
st.caption("目标：把培养方案作为“基础底库”尽可能完整抽取，供后续所有教学文件生成/校核使用。")


# ----------------------------
# helpers
# ----------------------------
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


# ----------------------------
# UI
# ----------------------------
with st.sidebar:
    st.header("上传与抽取")
    pdf_file = st.file_uploader("上传培养方案 PDF", type=["pdf"])
    enable_ocr = st.checkbox("对于无文字页启用 OCR（可选）", value=False)
    start_btn = st.button("开始全量抽取", type="primary")

if not pdf_file:
    st.info("请先在左侧上传你的《2024培养方案.pdf》之类的培养方案文件，然后点击“开始全量抽取”。")
    st.stop()

pdf_bytes = pdf_file.read()

if start_btn:
    with st.spinner("正在逐页抽取（文本 + 表格）..."):
        extracted = extract_pdf_all(pdf_bytes, enable_ocr=enable_ocr)

    structured, full_text, course_tables = parse_structured(extracted)

    # 顶部指标
    c1, c2, c3, c4 = st.columns([1, 1, 1, 3])
    c1.metric("总页数", extracted["meta"]["n_pages"])
    c2.metric("表格总数", extracted["meta"]["n_tables"])
    c3.metric("OCR启用", "是" if extracted["meta"]["ocr_enabled"] else "否")
    c4.caption(f"SHA256: {extracted['meta']['sha256']}")

    # 结构化结果区
    st.subheader("结构化识别结果（可先在这里核对）")

    left, right = st.columns([1, 1])

    with left:
        st.markdown("### 培养目标（抽取）")
        obj = structured.get("objectives", [])
        if not obj:
            st.warning("未识别到培养目标（可能标题写法不同/扫描页）。建议勾选OCR或检查PDF是否为图片版。")
        else:
            for i, t in enumerate(obj, start=1):
                st.write(f"{i}. {t}")

    with right:
        st.markdown("### 毕业要求（抽取）")
        gr = structured.get("graduation_requirements", {})
        st.caption(f"当前识别到：{len(gr)} 条（目标通常为 12 条）")
        if not gr:
            st.warning("未识别到毕业要求。建议勾选OCR或检查该部分是否在表格/图片中。")
        else:
            gr_df = pd.DataFrame([{"编号": k, "毕业要求": v} for k, v in gr.items()])
            st.dataframe(gr_df, use_container_width=True, height=260)

    # 课程表
    st.markdown("### 课程相关表格（自动筛选/跨页合并）")
    if not course_tables:
        st.info("未筛选到明显的课程表（可能课程表被识别为图片或表格线不规则）。可尝试启用OCR，或后续增强表格提取策略。")
    else:
        for i, df in enumerate(course_tables, start=1):
            st.markdown(f"**合并表 {i}**（含页码列 __page__）")
            st.dataframe(df, use_container_width=True, height=260)

    # 下载区
    st.divider()
    st.subheader("导出（基础底库）")

    json_bytes = build_json_bytes(extracted, structured, full_text)
    st.download_button(
        "下载抽取结果 JSON（全量底库）",
        data=json_bytes,
        file_name="training_plan_full_extract.json",
        mime="application/json",
        use_container_width=True,
    )

    zip_bytes = build_csv_zip_bytes(extracted, course_tables)
    st.download_button(
        "下载 CSV(zip)（逐页文本 + 逐表CSV + 课程表合并CSV）",
        data=zip_bytes,
        file_name="training_plan_extract_csv.zip",
        mime="application/zip",
        use_container_width=True,
    )

    xlsx_bytes, xlsx_msg = build_excel_bytes(extracted, structured, course_tables)
    if xlsx_bytes is None:
        st.warning(xlsx_msg)
    else:
        st.success(xlsx_msg)
        st.download_button(
            "下载 Excel(xlsx)（meta/培养目标/毕业要求/pages_text/课程表合并）",
            data=xlsx_bytes,
            file_name="training_plan_extract.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    # 原始核查
    with st.expander("查看逐页原始抽取（文本/表格）", expanded=False):
        for p in extracted["pages"]:
            st.markdown(f"#### Page {p['page']}")
            st.text_area("text", value=p.get("text", ""), height=180, key=f"t_{p['page']}")
            if p.get("tables"):
                st.caption(f"tables: {len(p['tables'])}")
                for ti, t in enumerate(p["tables"], start=1):
                    df = table_to_df(t)
                    if df is not None:
                        st.markdown(f"- table {ti}")
                        st.dataframe(df, use_container_width=True, height=200)
            st.divider()

else:
    st.info("已上传 PDF。点击左侧 **开始全量抽取**。")
