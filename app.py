import io, json, time, random, hashlib, base64
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from google.api_core import exceptions
from typing import Dict, List, Any

# ============================================================
# 1. 字段与常量定义
# ============================================================
TABLE_1_FULL_COLS = [
    "课程体系", "课程编码", "课程名称", "开课模式", "考核方式", 
    "学分", "总学时", "内_讲课", "内_实验", "内_上机", "内_实践", 
    "外_学分", "外_学时", "上课学期", "专业方向", "学位课", "备注"
]

# ============================================================
# 2. 工具函数 (JSON、文本、PDF处理)
# ============================================================
def _compact_lines(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def payload_to_jsonable(obj):
    if isinstance(obj, pd.DataFrame):
        return obj.fillna("").to_dict(orient="records")
    if isinstance(obj, dict):
        return {str(k): payload_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [payload_to_jsonable(x) for x in obj]
    return str(obj) if isinstance(obj, (io.BytesIO, bytes)) else obj

# ============================================================
# 3. AI 调用核心 (带节流与重试机制)
# ============================================================
def ai_safe_call(model, prompt: str, max_retries=5):
    """确保在 15 RPM 限制内稳定运行"""
    retries = 0
    while retries < max_retries:
        try:
            # 强制冷却，确保每分钟请求不超过 12 次
            time.sleep(5) 
            response = model.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            )
            return json.loads(response.text)
        except exceptions.ResourceExhausted:
            wait_time = (2 ** retries) * 5 + random.uniform(0, 1)
            st.warning(f"触发 API 配额限制，正在等待 {int(wait_time)} 秒后重试...")
            time.sleep(wait_time)
            retries += 1
        except Exception as e:
            st.error(f"AI 调用异常: {e}")
            return None
    return None

def ai_process_large_table(model, raw_rows, prompt_prefix, chunk_size=80):
    """将长表格分块，防止 AI 截断"""
    results = []
    total = len(raw_rows)
    for i in range(0, total, chunk_size):
        chunk = raw_rows[i : i + chunk_size]
        st.write(f"正在处理数据块：{i+1} 至 {min(i+chunk_size, total)} 行...")
        prompt = f"{prompt_prefix}\n数据片段：{json.dumps(chunk, ensure_ascii=False)}"
        res = ai_safe_call(model, prompt)
        if isinstance(res, list):
            results.extend(res)
    return results

# ============================================================
# 4. 文档深度解析引擎
# ============================================================
def deep_parse_document(api_key, pdf_bytes):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # 获取全文文本用于正文分析
        all_text = [p.extract_text() or "" for p in pdf.pages]
        
        # 提取 1-6 项正文 [cite: 10, 21, 80, 88, 91]
        st.info("正在提取培养方案 1-6 项正文...")
        sec_context = "\n".join(all_text[:6])
        results["sections"] = ai_safe_call(model, f"提取 1-6 项正文 JSON。内容：{sec_context}")

        # 扫描附表
        raw_t1, raw_t4, text_t2 = [], [], ""
        for i, page in enumerate(pdf.pages):
            txt = all_text[i]
            # 附表1 所在页 [cite: 105, 107, 109, 111]
            if "附表1" in txt or "教学计划表" in txt:
                tbl = page.extract_table()
                if tbl: raw_t1.extend(tbl[1:])
            # 附表2 所在页 
            elif "附表2" in txt or "学分统计" in txt:
                text_t2 += f"\n{txt}"
            # 附表4 所在页 [cite: 124, 127, 130, 131, 133]
            elif "附表4" in txt or "支撑关系" in txt:
                tbl = page.extract_table()
                if tbl: raw_t4.extend(tbl[1:])

        # 执行分块抽取
        if raw_t1:
            st.info("正在全量校对附表 1...")
            results["tables"]["1"] = ai_process_large_table(model, raw_t1, f"转换教学计划表。列：{TABLE_1_FULL_COLS}", chunk_size=80)
        
        if text_t2:
            st.info("正在重构附表 2 学分统计...")
            results["tables"]["2"] = ai_safe_call(model, f"提取学分统计 JSON。数据：{text_t2}")

        if raw_t4:
            st.info("正在全量映射附表 4 支撑矩阵...")
            results["tables"]["4"] = ai_process_large_table(model, raw_t4, "提取支撑关系 JSON [课程名称, 指标点, 强度]", chunk_size=100)

    return results

# ============================================================
# 5. 主界面逻辑 (修复空页面问题)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件智能工作台 v1.0", page_icon="🧠")
    
    # 初始化 session state
    if "final_data" not in st.session_state:
        st.session_state.final_data = None

    # 侧边栏
    with st.sidebar:
        st.title("⚙️ 设置")
        api_key = st.text_input("Gemini API Key", type="password", key="final_v1_key")
        st.divider()
        st.caption("版本: v1.0 稳定全量抽取版")

    # 主体界面
    st.markdown("## 📑 培养方案全量智能抽取工作台")
    st.info("请确保已在侧边栏配置 API Key。本版本支持 2.5 Flash 免费版配额自动管理。")
    
    file = st.file_uploader("上传 2024培养方案.pdf", type="pdf", key="final_v1_uploader")

    if file and api_key:
        if st.button("🚀 执行一键全量抽取", type="primary", use_container_width=True, key="final_v1_btn"):
            data = deep_parse_document(api_key, file.getvalue())
            if data:
                st.session_state.final_data = data
                st.success("🎉 全量数据抽取成功！")

    # 渲染结果
    if st.session_state.final_data:
        d = st.session_state.final_data
        tab1, tab2, tab3, tab4 = st.tabs(["1-6 正文内容", "附表1: 教学计划表", "附表2: 学分统计表", "附表4: 支撑矩阵表"])
        
        with tab1:
            sec_pick = st.selectbox("选择栏目查看", ["1","2","3","4","5","6"], key="sec_v1_select")
            content = d["sections"].get(sec_pick, "未提取到相关正文")
            st.text_area("提取结果", value=content, height=450, key="sec_v1_ta")

        with tab2:
            df1 = pd.DataFrame(d["tables"].get("1", []))
            if not df1.empty:
                st.markdown(f"**已识别课程总数：{len(df1)} 门**")
                st.data_editor(df1.reindex(columns=TABLE_1_FULL_COLS), use_container_width=True, key="tbl1_v1_editor")
            else:
                st.warning("附表 1 暂无数据。")

        with tab3:
            df2 = pd.DataFrame(d["tables"].get("2", []))
            st.table(df2)

        with tab4:
            df4 = pd.DataFrame(d["tables"].get("4", []))
            st.dataframe(df4, use_container_width=True, key="tbl4_v1_df")

if __name__ == "__main__":
    main()