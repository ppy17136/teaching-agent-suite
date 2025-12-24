import io, json, time, random, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any
from openai import OpenAI
from google.api_core import exceptions

# ============================================================
# 1. 模型供应商配置
# ============================================================
PROVIDERS = {
    "Gemini (Google)": {"base_url": None, "model": "gemini-1.5-flash", "is_gemini": True, "limit": 8192},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "is_gemini": False, "limit": 4096},
    "Kimi (Moonshot)": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k", "is_gemini": False, "limit": 4096},
    "通义千问 (Qwen)": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "is_gemini": False, "limit": 4096},
}

# ============================================================
# 2. 深度流控调用引擎
# ============================================================
def call_llm_engine(provider_name, api_key, prompt, max_retries=3):
    config = PROVIDERS[provider_name]
    for i in range(max_retries):
        try:
            # 基础节流延迟，确保不触碰 RPM 限制
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
                        {"role": "system", "content": "你是一个只输出 JSON 数据的教务专家。严禁输出任何解释性文字或 Markdown 标签。"},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=config["limit"]
                )
                return json.loads(response.choices[0].message.content)
        except exceptions.ResourceExhausted:
            wait = (i + 1) * 20
            st.warning(f"触发 API 配额限制，需等待 {wait} 秒后重试...")
            time.sleep(wait)
        except Exception:
            continue
    return None

# ============================================================
# 3. 稳健型分块解析逻辑 (已修复变量命名错误)
# ============================================================
def ultra_parse_v53(api_key, pdf_bytes, provider_name):
    results = {"sections": {}, "table1": [], "table2": [], "table4": []}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
        raw_rows_t1, raw_rows_t4 = [], []
        for page in pdf.pages:
            txt = page.extract_text() or ""
            tbls = page.extract_tables()
            if any(x in txt for x in ["附表1", "教学计划表"]):
                for t in tbls: raw_rows_t1.extend(t)
            if any(x in txt for x in ["附表4", "支撑矩阵"]):
                for t in tbls: raw_rows_t4.extend(t)

    # --- 任务 1: 提取正文 ---
    st.info("步骤 1/4: 正在解析 1-6 项正文内容...")
    p_sec = f"提取正文 JSON。要求分条列出。键名：1培养目标, 2毕业要求, 3专业定位与特色, 4主干学科, 5标准学制, 6毕业条件。内容：{all_text[:12000]}"
    res_sec = call_llm_engine(provider_name, api_key, p_sec)
    if isinstance(res_sec, dict):
        # 兼容不同模型可能返回的嵌套结构
        data = res_sec.get("sections", res_sec)
        results["sections"] = data

    # --- 任务 2: 附表 1 极致分块 (解决截断问题) ---
    if raw_rows_t1:
        clean_t1 = [r for r in raw_rows_t1 if any(r)]
        st.info(f"步骤 2/4: 解析计划表 (共 {len(clean_t1)} 行)...")
        for i in range(0, len(clean_t1), 25): # 每 25 行发一次请求
            chunk = clean_t1[i : i+25]
            st.write(f"  > 正在处理第 {i+1} 至 {i+len(chunk)} 行课程...")
            p_chunk = f"将表格行转为 JSON 列表。字段：[课程名称, 学分, 学位课, 上课学期]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p_chunk)
            
            # 兼容处理 Dict 和 List 返回格式
            if isinstance(res, list):
                results["table1"].extend(res)
            elif isinstance(res, dict):
                data = res.get("table1") or res.get("data") or list(res.values())[0]
                if isinstance(data, list): results["table1"].extend(data)

    # --- 任务 3: 附表 2 学分统计 ---
    st.info("步骤 3/4: 分析学分统计表...")
    p_t2 = f"提取学分统计 JSON 列表。必须区分焊接/无损检测。内容：{all_text}"
    res_t2 = call_llm_engine(provider_name, api_key, p_t2)
    if res_t2:
        results["table2"] = res_t2 if isinstance(res_t2, list) else res_t2.get("table2", [])

    # --- 任务 4: 附表 4 支撑矩阵 (已修复变量名 clean_rows_t4) ---
    if raw_rows_t4:
        # ✅ 正确定义变量名
        clean_t4 = [r for r in raw_rows_t4 if any(r)] 
        st.info(f"步骤 4/4: 解析支撑矩阵 (共 {len(clean_t4)} 行)...")
        for i in range(0, len(clean_t4), 35): # ✅ 统一使用 clean_t4
            chunk = clean_t4[i : i+35]
            st.write(f"  > 正在映射第 {i+1} 至 {i+len(chunk)} 条支撑关系...")
            p_t4 = f"提取支撑矩阵 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p_t4)
            if isinstance(res, list):
                results["table4"].extend(res)
            elif isinstance(res, dict):
                data = res.get("table4") or res.get("data") or list(res.values())[0]
                if isinstance(data, list): results["table4"].extend(data)

    return results

# ============================================================
# 4. Streamlit UI 渲染
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="培养方案智能助手 v5.3")
    
    if "final_data" not in st.session_state:
        st.session_state.final_data = None

    with st.sidebar:
        st.title("⚙️ 配置")
        prov = st.selectbox("选择模型供应商", list(PROVIDERS.keys()), key="v53_prov")
        key = st.text_input("API Key", type="password", key="v53_key")
        st.divider()
        if st.button("清理数据缓存"):
            st.session_state.final_data = None
            st.rerun()

    st.header("🧠 培养方案智能提取工作台 (v5.3 稳定修正版)")
    file = st.file_uploader("上传 PDF 培养方案", type="pdf")

    if file and key and st.button("🚀 开始全量抽取", type="primary"):
        # 执行修正后的解析逻辑
        res = ultra_parse_v53(key, file.getvalue(), prov)
        if res:
            st.session_state.final_data = res
            st.success("🎉 数据抽取已全部完成！")

    if st.session_state.final_data:
        d = st.session_state.final_data
        tabs = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with tabs[0]:
            sections = d.get("sections", {})
            if sections:
                pick = st.selectbox("选择查看栏目", list(sections.keys()), key="v53_sec_pick")
                # 使用动态 Key 确保下拉刷新
                st.text_area("内容", value=sections.get(pick, ""), height=400, key=f"ta_v53_{pick}")
        
        with tabs[1]:
            st.dataframe(pd.DataFrame(d.get("table1", [])), use_container_width=True)
        with tabs[2]:
            st.dataframe(pd.DataFrame(d.get("table2", [])), use_container_width=True)
        with tabs[3]:
            st.dataframe(pd.DataFrame(d.get("table4", [])), use_container_width=True)

if __name__ == "__main__":
    main()