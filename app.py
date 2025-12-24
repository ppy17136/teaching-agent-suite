import io, json, time, random, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any
from openai import OpenAI
from google.api_core import exceptions  # ✅ 修正 1：确保导入异常处理模块

# ============================================================
# 1. 模型供应商配置
# ============================================================
PROVIDERS = {
    "Gemini (Google)": {"base_url": None, "model": "gemini-1.5-flash", "is_gemini": True},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "is_gemini": False},
    "Kimi (Moonshot)": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k", "is_gemini": False},
    "智谱 AI (GLM)": {"base_url": "https://open.bigmodel.cn/api/paas/v4/", "model": "glm-4", "is_gemini": False},
    "通义千问 (Qwen)": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "is_gemini": False},
    "豆包 (字节)": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-pro-32k", "is_gemini": False}
}

# ============================================================
# 2. 核心提示词：强调结构化与分条
# ============================================================
MEGA_PROMPT = """
你是一个专业的高校教务专家。请精确提取以下内容并严格输出一个 JSON 对象。
提取要求：
1. **分条列出**：1-6项正文必须保留原始编号，使用 '\\n' 换行。
2. **禁止嵌套**：表格内严禁出现嵌套 JSON，必须全部为扁平字符串。
3. **附表要求**：附表1提取全量课程；附表2区分焊接/无损方向；附表4提取支撑强度。
"""

# ============================================================
# 3. 统一驱动引擎 (带重试、节流与 Token 保护)
# ============================================================
def call_llm_engine(provider_name, api_key, prompt, max_retries=3):
    """统一处理所有模型的调用逻辑，修复 NameError 和 截断问题"""
    if provider_name not in PROVIDERS:
        st.error(f"无效的供应商: {provider_name}")
        return None
        
    config = PROVIDERS[provider_name]
    
    for i in range(max_retries):
        try:
            # 基础流控 (防止过快触发限制)
            time.sleep(5 if config["is_gemini"] else 2)
            
            if config["is_gemini"]:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(config["model"])
                # Gemini 支持 8192 token 输出
                response = model.generate_content(
                    prompt, 
                    generation_config={"response_mime_type": "application/json", "max_output_tokens": 8192}
                )
                return json.loads(response.text)
            else:
                client = OpenAI(api_key=api_key, base_url=config["base_url"])
                # ✅ 修正 2：为 OpenAI 兼容模型增加 max_tokens 设置，防止截断
                response = client.chat.completions.create(
                    model=config["model"],
                    messages=[
                        {"role": "system", "content": "你是一个严谨的教务专家，只输出完整的 JSON，严禁截断。"},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=4096 # 国产模型通常最大支持 4k 输出
                )
                return json.loads(response.choices[0].message.content)
                
        except exceptions.ResourceExhausted:
            wait = (i + 1) * 20
            st.warning(f"触发配额限制，正在第 {i+1} 次重试，需等待 {wait} 秒...")
            time.sleep(wait)
        except json.JSONDecodeError as je:
            # 捕获截断导致的 JSON 错误
            st.error(f"JSON 解析失败 (可能是内容太长被模型强行截断): {str(je)}")
            return None
        except Exception as e:
            if i == max_retries - 1: st.error(f"调用失败: {str(e)}")
            continue
    return None

# ============================================================
# 4. 智能解析引擎 (分段策略决策)
# ============================================================
def intelligent_processor(api_key, pdf_bytes, provider_name):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
    
    char_count = len(all_text)
    is_gemini = PROVIDERS[provider_name]["is_gemini"]
    
    # 策略判断：非 Gemini 且字符 > 12,000，则分段请求以防止截断
    needs_split = (not is_gemini) and (char_count > 12000)
    final_res = {"sections": {}, "table1": [], "table2": [], "table4": []}

    if not needs_split:
        st.info("📊 采用【全量单次】抽取模式...")
        full_p = f"{MEGA_PROMPT}\n\n内容原文：\n{all_text}"
        res = call_llm_engine(provider_name, api_key, full_p)
        if res: final_res = res
    else:
        st.warning(f"📊 文档较长 ({char_count} 字符)，为防止输出截断，自动切换为【分段安全】抽取模式...")
        
        # 任务 1: 正文 + 学分表 (限制输入长度)
        st.write("步骤 1: 正在提取正文与学分统计...")
        p1 = f"{MEGA_PROMPT}\n任务：仅提取 1-6 项正文和附表 2。内容：{all_text[:15000]}"
        r1 = call_llm_engine(provider_name, api_key, p1)
        if r1:
            final_res["sections"] = r1.get("sections", {})
            final_res["table2"] = r1.get("table2", [])

        # 任务 2: 教学计划表 (附表 1)
        st.write("步骤 2: 正在提取教学计划表...")
        p2 = f"请提取附表 1 的所有课程，格式 {{'table1':[...]}}。内容：\n{all_text}"
        r2 = call_llm_engine(provider_name, api_key, p2)
        if r2: final_res["table1"] = r2.get("table1", [])

        # 任务 3: 支撑矩阵 (附表 4)
        st.write("步骤 3: 正在提取支撑矩阵...")
        p3 = f"请提取附表 4 的支撑矩阵，格式 {{'table4':[...]}}。内容：\n{all_text}"
        r3 = call_llm_engine(provider_name, api_key, p3)
        if r3: final_res["table4"] = r3.get("table4", [])

    return final_res

# ============================================================
# 5. UI 逻辑 (修复下拉列表状态问题)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="智能教学工作台 v4.1")
    
    if "mega_data" not in st.session_state:
        st.session_state.mega_data = None

    with st.sidebar:
        st.title("🤖 模型配置")
        selected_provider = st.selectbox("选择模型供应商", list(PROVIDERS.keys()), key="prov_v4")
        api_key = st.text_input(f"输入 {selected_provider} 的 API Key", type="password", key="key_v4")
        st.divider()
        if st.button("清理缓存数据"):
            st.session_state.mega_data = None
            st.rerun()

    st.header("🧠 培养方案全量提取 (策略分流版)")
    file = st.file_uploader("上传 PDF 培养方案", type="pdf")

    if file and api_key and st.button("🚀 执行全量抽取", type="primary"):
        with st.spinner("AI 正在深度解析文档，请稍候..."):
            result = intelligent_processor(api_key, file.getvalue(), selected_provider)
            if result:
                st.session_state.mega_data = result
                st.success("抽取成功！")

    if st.session_state.mega_data:
        d = st.session_state.mega_data
        tab1, tab2, tab3, tab4 = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with tab1:
            sections = d.get("sections", {})
            if sections:
                sec_pick = st.selectbox("选择栏目", list(sections.keys()), key="sec_pick_v4")
                # ✅ 修正 3：使用动态 key 确保切换下拉列表后内容即时刷新
                st.text_area("内容文本", value=sections.get(sec_pick, ""), height=450, key=f"ta_v4_{sec_pick}")
            else:
                st.warning("正文部分提取失败。")

        with tab2:
            st.dataframe(pd.DataFrame(d.get("table1", [])), use_container_width=True)
        with tab3:
            st.dataframe(pd.DataFrame(d.get("table2", [])), use_container_width=True)
        with tab4:
            st.dataframe(pd.DataFrame(d.get("table4", [])), use_container_width=True)

if __name__ == "__main__":
    main()