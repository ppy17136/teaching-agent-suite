import io, os, json, time, hashlib
import datetime as dt
from pathlib import Path
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from openai import OpenAI

# =========================
# 1. 基础配置
# =========================
PROVIDERS = {
    "Gemini (Google)": {"base_url": None, "model": "gemini-1.5-flash"},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
}

SECTION_TITLES = ["一、培养目标", "二、毕业要求", "三、专业定位与特色", "四、主干学科、专业核心课程和主要实践性教学环节", "五、标准学制与授予学位", "六、毕业条件"]

# =========================
# 2. 轮换调用逻辑
# =========================

def get_gemini_keys():
    keys = st.secrets.get("GEMINI_KEYS", [])
    return [k.strip() for k in keys] if isinstance(keys, list) else []

def call_llm_core(provider_name, api_key, prompt):
    if "Gemini" in provider_name:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(PROVIDERS[provider_name]["model"])
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    # 其他 OpenAI 兼容模型省略...

def call_llm_with_retry_and_rotation(provider_name, user_api_key, prompt):
    all_keys = get_gemini_keys()
    if "Gemini" not in provider_name or user_api_key:
        return call_llm_core(provider_name, user_api_key or st.secrets.get("GEMINI_API_KEY", ""), prompt)

    # 使用按钮点击后已经确定的索引
    start_idx = st.session_state.get("api_key_index", 0) % len(all_keys)
    for i in range(len(all_keys)):
        curr_idx = (start_idx + i) % len(all_keys)
        st.session_state.api_key_index = curr_idx
        try:
            st.write(f"正在尝试使用 Key #{curr_idx + 1}...")
            return call_llm_core(provider_name, all_keys[curr_idx], prompt)
        except Exception as e:
            if "429" in str(e) or "quota" in str(e).lower():
                continue
            raise e
    raise Exception("所有 Key 均不可用")

# =========================
# 3. UI 与解析逻辑
# =========================

def main():
    st.set_page_config(layout="wide")
    if "api_key_index" not in st.session_state: st.session_state.api_key_index = 0

    with st.sidebar:
        st.title("🤖 模型配置")
        provider = st.selectbox("选择解析模型", list(PROVIDERS.keys()))
        user_key = st.text_input("手动 API Key (留空则轮换)", type="password")
        
        all_keys = get_gemini_keys()
        if "Gemini" in provider and not user_key and all_keys:
            idx = st.session_state.api_key_index % len(all_keys)
            st.info(f"💡 当前/下次使用的 Key: #{idx + 1}")

    st.header("🧠 培养方案全量提取")
    file = st.file_uploader("上传 PDF", type=["pdf"])

    if file and st.button("🚀 执行全量 AI 抽取", type="primary", use_container_width=True):
        # --- 强制点击即轮换 ---
        if "Gemini" in provider and not user_key and all_keys:
            st.session_state.api_key_index = (st.session_state.api_key_index + 1) % len(all_keys)
            st.toast(f"已轮换至新 Key", icon="🔄")
        
        # 执行解析 (内部会使用更新后的 api_key_index)
        # 这里仅作演示，实际请补充 MEGA_PROMPT 定义
        with st.status("正在解析...", expanded=True):
            with pdfplumber.open(io.BytesIO(file.read())) as pdf:
                all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
            res = call_llm_with_retry_and_rotation(provider, user_key, all_text)
            st.session_state.result = res
            st.success("完成！")

if __name__ == "__main__":
    main()