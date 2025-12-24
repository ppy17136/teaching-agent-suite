import io, json, time, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any
from openai import OpenAI  # 用于适配 DeepSeek, Kimi, Yi, 智谱等

# ============================================================
# 1. 模型供应商配置
# ============================================================
PROVIDERS = {
    "Gemini (Google)": {"base_url": None, "model": "gemini-2.5-flash"},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "Kimi (Moonshot)": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "智谱 AI (GLM)": {"base_url": "https://open.bigmodel.cn/api/paas/v4/", "model": "glm-4"},
    "零一万物 (Yi)": {"base_url": "https://api.lingyiwanwu.com/v1", "model": "yi-34b-chat-0205"},
    "通义千问 (Qwen)": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "豆包 (字节)": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-pro-32k"}
}

# ============================================================
# 2. 统一大模型调用路由
# ============================================================
def call_llm(provider_name, api_key, prompt):
    config = PROVIDERS[provider_name]
    
    # --- 场景 A: Gemini 专用 SDK ---
    if "Gemini" in provider_name:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(config["model"])
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    
    # --- 场景 B: OpenAI 兼容格式 (DeepSeek, Kimi, GLM, etc.) ---
    else:
        client = OpenAI(api_key=api_key, base_url=config["base_url"])
        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": "你是一个只输出 JSON 的教务专家助手。"},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)


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
  "table1": [{"课程体系": "...", "课程编码": "...", "课程名称": "...", "开课模式": "...", "考核方式": "...", "课内学分": "...", "课内总学时": "...", "课内讲课学时": "...", "课内实验学时": "...", "课内上机学时": "...", "课内实践学时": "...", "课外学分": "...", "课外学时": "...", "上课学期": "...", "专业方向": "...", "是否学位课": "...", "备注": "..."}],
  "table2": [{"专业方向": "...", "课程体系": "...", "开课模式": "...", "学期一学分分配": "...", "学期二学分分配": "...", "学期三学分分配": "...", "学期四学分分配": "...", "学期五学分分配": "...", "学期六学分分配": "...", "学期七学分分配": "...", "学期八学分分配": "...", "学分统计": "...", "学分比例": "..."}],
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
        full_prompt = f"{MEGA_PROMPT}\n\n培养方案原文：\n{all_text}"
        result = call_llm(selected_provider, api_key, full_prompt)
        return result
    except Exception as e:
        st.error(f"抽取失败: {str(e)}")
        return None

# ============================================================
# 3. Streamlit UI
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="多模型智能教学工作台")
    
    if "mega_data" not in st.session_state:
        st.session_state.mega_data = None

    with st.sidebar:
        st.title("🤖 模型配置")
        selected_provider = st.selectbox("选择模型供应商", list(PROVIDERS.keys()))
        api_key = st.text_input(f"输入 {selected_provider} 的 API Key", type="password")
        st.info(f"当前模型: {PROVIDERS[selected_provider]['model']}")
        st.warning("如果提示配额耗尽且等待无效，请更换一个新的 API Key。")        
   

    st.header("🧠 培养方案全量提取 (多模型版)")
    file = st.file_uploader("上传 PDF 培养方案", type="pdf")

    if file and api_key and st.button("🚀 执行一键全量抽取", type="primary"):
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