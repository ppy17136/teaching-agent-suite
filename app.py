import io, json, time, random, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any
from openai import OpenAI
from google.api_core import exceptions

# ============================================================
# 1. 配置中心
# ============================================================
PROVIDERS = {
    "Gemini (Google)": {"base_url": None, "model": "gemini-1.5-flash", "is_gemini": True, "limit": 8192},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "is_gemini": False, "limit": 4096},
    "Kimi (Moonshot)": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k", "is_gemini": False, "limit": 4096},
    "通义千问 (Qwen)": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "is_gemini": False, "limit": 4096},
}

# ============================================================
# 2. 安全渲染工具 (防止 UI 崩溃)
# ============================================================
def safe_to_df(data: Any, default_cols: List[str]) -> pd.DataFrame:
    """清洗 AI 数据，确保 Pandas 能够正常加载"""
    if not data or not isinstance(data, list):
        return pd.DataFrame(columns=default_cols)
    
    clean_list = []
    for item in data:
        if isinstance(item, dict):
            clean_list.append(item)
        elif isinstance(item, list) and len(item) <= len(default_cols):
            clean_list.append(dict(zip(default_cols, item)))
    
    return pd.DataFrame(clean_list) if clean_list else pd.DataFrame(columns=default_cols)

# ============================================================
# 3. 核心调用引擎 (带重试与流控)
# ============================================================
def call_llm_engine(provider_name, api_key, prompt, max_retries=3):
    config = PROVIDERS.get(provider_name, PROVIDERS["Gemini (Google)"])
    for i in range(max_retries):
        try:
            # 基础节流延迟
            time.sleep(6 if config["is_gemini"] else 3) 
            if config["is_gemini"]:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(config["model"])
                response = model.generate_content(
                    prompt, 
                    generation_config={"response_mime_type": "application/json", "max_output_tokens": config["limit"]}
                )
                return json.loads(response.text)
            else:
                client = OpenAI(api_key=api_key, base_url=config["base_url"])
                response = client.chat.completions.create(
                    model=config["model"],
                    messages=[
                        {"role": "system", "content": "你是一个严谨的教务专家，只输出 JSON。"},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=config["limit"]
                )
                return json.loads(response.choices[0].message.content)
        except exceptions.ResourceExhausted:
            time.sleep((i + 1) * 20)
        except Exception:
            continue
    return None

# ============================================================
# 4. 稳健型分块解析引擎 (彻底修复 AttributeError)
# ============================================================
def ultra_parse_v55(api_key, pdf_bytes, provider_name):
    results = {"sections": {}, "table1": [], "table2": [], "table4": []}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
        raw_rows_t1, raw_rows_t4 = [], []
        for page in pdf.pages:
            txt, tbls = page.extract_text() or "", page.extract_tables()
            if any(x in txt for x in ["附表1", "教学计划表"]):
                for t in tbls: raw_rows_t1.extend(t)
            if any(x in txt for x in ["附表4", "支撑矩阵"]):
                for t in tbls: raw_rows_t4.extend(t)

    # 1. 正文
    st.info("步骤 1/4: 提取正文...")
    p_sec = f"提取正文 JSON。键名：1培养目标, 2毕业要求, 3专业定位与特色, 4主干学科, 5标准学制, 6毕业条件。内容：{all_text[:12000]}"
    res_sec = call_llm_engine(provider_name, api_key, p_sec)
    if res_sec:
        # 兼容处理正文嵌套
        results["sections"] = res_sec if isinstance(res_sec, dict) else {}

    # 2. 附表 1 (关键修复点)
    if raw_rows_t1:
        clean_t1 = [r for r in raw_rows_t1 if any(r)]
        st.info(f"步骤 2/4: 解析计划表 (共 {len(clean_t1)} 行)...")
        for i in range(0, len(clean_t1), 25):
            chunk = clean_t1[i : i+25]
            p = f"表格行转 JSON 列表。字段：[课程名称, 学分, 学位课, 上课学期]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p)
            if res:
                # ✅ 修复逻辑：先判断类型，再调用方法
                if isinstance(res, list):
                    results["table1"].extend(res)
                elif isinstance(res, dict):
                    data = res.get("table1") or res.get("data") or list(res.values())[0]
                    if isinstance(data, list): results["table1"].extend(data)

    # 3. 附表 2
    st.info("步骤 3/4: 分析学分统计...")
    res_t2 = call_llm_engine(provider_name, api_key, f"提取学分统计 JSON 列表。区分焊接/无损。内容：{all_text}")
    if res_t2:
        results["table2"] = res_t2 if isinstance(res_t2, list) else res_t2.get("table2", [])

    # 4. 附表 4 (关键修复点)
    if raw_rows_t4:
        clean_t4 = [r for r in raw_rows_t4 if any(r)]
        st.info(f"步骤 4/4: 解析支撑矩阵 (共 {len(clean_t4)} 行)...")
        for i in range(0, len(clean_t4), 35):
            chunk = clean_t4[i : i+35]
            p = f"提取支撑矩阵 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p)
            if res:
                # ✅ 修复逻辑：先判断类型，再调用方法
                if isinstance(res, list):
                    results["table4"].extend(res)
                elif isinstance(res, dict):
                    data = res.get("table4") or res.get("data") or list(res.values())[0]
                    if isinstance(data, list): results["table4"].extend(data)

    return results

# ============================================================
# 5. UI 渲染
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="培养方案智能助手 v5.5")
    if "data" not in st.session_state: st.session_state.data = None

    with st.sidebar:
        st.title("⚙️ 设置")
        prov = st.selectbox("模型供应商", list(PROVIDERS.keys()), key="prov_v55")
        key = st.text_input("API Key", type="password", key="key_v55")
        if st.button("清理缓存"):
            st.session_state.data = None
            st.rerun()

    st.header("🧠 培养方案智能工作台 (修复版)")
    file = st.file_uploader("上传 PDF", type="pdf")

    if file and key and st.button("🚀 开始执行抽取", type="primary"):
        res = ultra_parse_v55(key, file.getvalue(), prov)
        if res:
            st.session_state.data = res
            st.success("抽取任务已完成！")

    if st.session_state.data:
        d = st.session_state.data
        tabs = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with tabs[0]:
            sec = d.get("sections", {})
            if isinstance(sec, dict) and sec:
                pick = st.selectbox("选择查看栏目", list(sec.keys()))
                st.text_area("内容", value=str(sec.get(pick, "")), height=400, key=f"ta_{pick}")
        
        with tabs[1]:
            st.dataframe(safe_to_df(d.get("table1"), ["课程名称", "学分", "学位课", "上课学期"]), use_container_width=True)
        with tabs[2]:
            st.dataframe(safe_to_df(d.get("table2"), ["专业方向", "项目", "学分要求"]), use_container_width=True)
        with tabs[3]:
            st.dataframe(safe_to_df(d.get("table4"), ["课程名称", "指标点", "强度"]), use_container_width=True)

if __name__ == "__main__":
    main()