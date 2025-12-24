import io, os, re, json, uuid, time, hashlib
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from openai import OpenAI

# ============================================================
# 1. 配置与模型定义
# ============================================================
APP_NAME = "Teaching Agent Suite"
APP_VERSION = "v0.8 (LLM-Rotation-Final)"
DATA_ROOT = Path("data/projects")

PROVIDERS = {
    "Gemini (Google)": {"base_url": None, "model": "gemini-2.5-flash"},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "Kimi (Moonshot)": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
}

SECTION_TITLES = [
    "一、培养目标", "二、毕业要求", "三、专业定位与特色",
    "四、主干学科、专业核心课程和主要实践性教学环节",
    "五、标准学制与授予学位", "六、毕业条件",
    "七、专业教学计划表", "八、学分统计表", "九、教学进程表",
    "十、课程设置对毕业要求支撑关系表", "十一、课程设置逻辑思维导图",
]

LLM_TO_STANDARD_MAP = {
    "1培养目标": "一、培养目标", "2毕业要求": "二、毕业要求",
    "3专业定位与特色": "三、专业定位与特色",
    "4主干学科/核心课程/实践环节": "四、主干学科、专业核心课程和主要实践性教学环节",
    "5标准学制与授予学位": "五、标准学制与授予学位", "6毕业条件": "六、毕业条件",
}

# ============================================================
# 2. API Key 轮换与重试核心逻辑
# ============================================================

def call_llm_core(provider_name, api_key, prompt):
    config = PROVIDERS[provider_name]
    if "Gemini" in provider_name:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(config["model"])
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    else:
        client = OpenAI(api_key=api_key, base_url=config["base_url"])
        response = client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "system", "content": "你是一个只输出 JSON 的教务专家助手。"}, {"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)

def get_gemini_keys() -> List[str]:
    keys = st.secrets.get("GEMINI_KEYS", [])
    if isinstance(keys, str): return [k.strip() for k in keys.split(",") if k.strip()]
    return list(keys)

def call_llm_with_retry_and_rotation(provider_name, user_api_key, prompt):
    all_keys = get_gemini_keys() # 获取 Secrets 中的 Key 列表
    
    # 初始化索引
    if "api_key_index" not in st.session_state:
        st.session_state.api_key_index = 0

    # 场景 A：手动输入 Key 时不参与轮换
    if "Gemini" not in provider_name or user_api_key:
        target_key = user_api_key if user_api_key else st.secrets.get("GEMINI_API_KEY", "")
        return call_llm_core(provider_name, target_key, prompt)

    # 场景 B：自动轮换逻辑
    if not all_keys:
        raise Exception("未在 Secrets 中配置 GEMINI_KEYS")

    last_exception = None
    # 记录本次点击开始时的索引
    start_idx = st.session_state.api_key_index % len(all_keys)

    for i in range(len(all_keys)):
        # 计算当前要尝试的 Key 索引
        curr_idx = (start_idx + i) % len(all_keys)
        curr_key = all_keys[curr_idx]
        
        # 实时更新 session_state，让 UI 反馈当前状态
        st.session_state.api_key_index = curr_idx
        
        try:
            st.write(f"正在尝试使用 Key #{curr_idx + 1}...")
            result = call_llm_core(provider_name, curr_key, prompt)
            
            # --- 关键修改：成功后将索引推向下一个，确保下次点击直接用新 Key ---
            st.session_state.api_key_index = (curr_idx + 1) % len(all_keys)
            return result
        except Exception as e:
            err = str(e).lower()
            # 如果是配额错误，继续循环尝试下一个
            if any(x in err for x in ["429", "quota", "limit"]):
                st.warning(f"⚠️ Key #{curr_idx + 1} 配额耗尽，正在自动切换...")
                last_exception = e
                continue 
            raise e
    
    raise Exception(f"❌ 所有 Key 均已尝试，无法完成提取。最后错误: {last_exception}")

# ============================================================
# 3. 培养方案全量解析引擎
# ============================================================

MEGA_PROMPT = """你是一个专业的高校教务专家。请深度阅读提供的文本并输出 JSON...要求保持分条列出、表格精度、H/M/L 支撑强度等。"""

def parse_training_plan_llm(pdf_bytes, provider_name, user_key):
    with st.status(f"🚀 正在通过 {provider_name} 解析培养方案...", expanded=True) as status:
        try:
            st.write("🔍 正在读取 PDF 全文...")
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
            
            st.write(f"📑 发送 AI 抽取请求 (内容长度: {len(all_text)})...")
            res = call_llm_with_retry_and_rotation(provider_name, user_key, f"{MEGA_PROMPT}\n\n原文：\n{all_text}")
            
            # 映射数据到标准栏目
            standard_sections = {v: res.get("sections", {}).get(k, "") for k, v in LLM_TO_STANDARD_MAP.items()}
            append_tables = {
                "七、专业教学计划表": res.get("table1", []),
                "八、学分统计表": res.get("table2", []),
                "九、教学进程表": [],
                "十、课程设置对毕业要求支撑关系表": res.get("table4", [])
            }
            
            status.update(label="✅ 解析成功！", state="complete", expanded=False)
            return {
                "meta": {"sha256": hashlib.sha256(pdf_bytes).hexdigest(), "rev": int(time.time()), "provider": provider_name},
                "sections": standard_sections,
                "appendices": {"tables": append_tables},
                "raw_pages_text": [all_text]
            }
        except Exception as e:
            status.update(label="❌ 解析失败", state="error", expanded=True)
            st.error(str(e))
            return None

# ============================================================
# 4. 数据持久化与 UI
# ============================================================

def save_base_plan(pid, plan):
    p = DATA_ROOT / pid
    p.mkdir(parents=True, exist_ok=True)
    (p / "base_training_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")

def main():
    st.set_page_config(layout="wide", page_title=APP_NAME)
    
    # 侧边栏配置
    with st.sidebar:
        st.title(f"🤖 {APP_NAME}")
        provider = st.selectbox("解析模型", list(PROVIDERS.keys()))
        user_key = st.text_input("手动 API Key (留空则轮换)", type="password")
        
        # 轮换状态显示
        all_keys = get_gemini_keys()
        if "Gemini" in provider and not user_key and all_keys:
            next_idx = st.session_state.get("api_key_index", 0) % len(all_keys)
            st.info(f"💡 自动轮换：下次使用 Key #{next_idx + 1}")
        st.divider()

    st.header("🧠 培养方案全量 AI 提取")
    file = st.file_uploader("上传 PDF 培养方案", type=["pdf"])

    if file and st.button("🚀 执行全量 AI 抽取", type="primary"):
        res = parse_training_plan_llm(file.read(), provider, user_key)
        if res:
            save_base_plan("default_project", res)
            st.session_state.plan_data = res
            st.success("抽取成功！已保存至基座。")

    # 结果展示
    if "plan_data" in st.session_state:
        d = st.session_state.plan_data
        tabs = st.tabs(SECTION_TITLES)
        for i, title in enumerate(SECTION_TITLES[:6]):
            with tabs[i]: st.text_area(title, value=d['sections'].get(title, ""), height=400)
        # 表格展示省略，可参考之前版本

if __name__ == "__main__":
    main()