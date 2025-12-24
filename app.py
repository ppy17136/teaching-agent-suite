import io, json, time, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any

# ============================================================
# 1. 核心提示词定义：一次性指令
# ============================================================
MEGA_PROMPT = """
你是一个高校教务专家。请阅读以下完整的培养方案文本，并精确提取以下所有内容。
请严格输出一个 JSON 对象，结构如下：

{
  "sections": {
    "1培养目标": "...",
    "2毕业要求": "...",
    "3专业定位与特色": "...",
    "4主干学科/核心课程/实践环节": "...",
    "5标准学制与授予学位": "...",
    "6毕业条件": "..."
  },
  "table1": [{"课程体系": "...", "课程编码": "...", "课程名称": "...", "学分": "...", "总学时": "...", "上课学期": "...", "备注": "..."}],
  "table2": [{"专业方向": "...", "课程体系": "...", "学分统计": "...", "学分比例": "..."}],
  "table4": [{"课程名称": "...", "指标点": "...", "强度": "..."}]
}

要求：
1. 附表1 (教学计划表) 请提取所有课程，不要遗漏。
2. 附表2 (学分统计) 必须区分“焊接”和“无损检测”方向。
3. 附表4 (支撑矩阵) 提取课程与指标点的对应强度。
"""

# ============================================================
# 2. 简化的解析引擎
# ============================================================
def parse_document_mega(api_key, pdf_bytes):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # 一次性读取全文文本
        all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
        
    st.info("正在发送单次全量抽取请求，请稍候（约 15-30 秒）...")
    
    try:
        # 只发一次请求，解决 ResourceExhausted 问题
        response = model.generate_content(
            f"{MEGA_PROMPT}\n\n以下是培养方案全文：\n{all_text}",
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"抽取失败: {str(e)}")
        return None

# ============================================================
# 3. Streamlit UI
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="省配额全量提取版")
    
    if "mega_data" not in st.session_state:
        st.session_state.mega_data = None

    with st.sidebar:
        api_key = st.text_input("Gemini API Key", type="password")
        st.warning("如果提示配额耗尽且等待无效，请更换一个新的 API Key。")

    st.header("📑 培养方案全量智能提取 (单次请求版)")
    file = st.file_uploader("上传 PDF", type="pdf")

    if file and api_key and st.button("🚀 执行一键全量抽取"):
        result = parse_document_mega(api_key, file.getvalue())
        if result:
            st.session_state.mega_data = result
            st.success("抽取成功！仅消耗 1 次 API 请求配额。")

    if st.session_state.mega_data:
        d = st.session_state.mega_data
        tab1, tab2, tab3, tab4 = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with tab1:
            sections = d.get("sections", {})
            sec_pick = st.selectbox("选择栏目", list(sections.keys()))
            st.text_area("内容", value=sections.get(sec_pick, ""), height=400, key=f"ta_{sec_pick}")

        with tab2:
            st.dataframe(pd.DataFrame(d.get("table1", [])), use_container_width=True)

        with tab3:
            st.dataframe(pd.DataFrame(d.get("table2", [])), use_container_width=True)

        with tab4:
            st.dataframe(pd.DataFrame(d.get("table4", [])), use_container_width=True)

if __name__ == "__main__":
    main()