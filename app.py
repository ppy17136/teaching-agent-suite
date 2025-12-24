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
# 2. 深度节流调用引擎
# ============================================================
def call_llm_engine(provider_name, api_key, prompt, max_retries=3):
    config = PROVIDERS[provider_name]
    for i in range(max_retries):
        try:
            time.sleep(6 if config["is_gemini"] else 3) # 留足余量的节流
            
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
                        {"role": "system", "content": "你是一个只输出 JSON 数据的教务专家。请直接返回 JSON 结果，不要包含任何 Markdown 代码块标签。"},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=config["limit"]
                )
                return json.loads(response.choices[0].message.content)
        except exceptions.ResourceExhausted:
            wait = (i + 1) * 20
            st.warning(f"触发配额限制，需等待 {wait} 秒...")
            time.sleep(wait)
        except Exception:
            continue
    return None

# ============================================================
# 3. 增强型分块解析逻辑 (解决 AttributeError)
# ============================================================
def ultra_parse_v51(api_key, pdf_bytes, provider_name):
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

    # 任务 1: 正文提取
    st.info("步骤 1/4: 正在提取 1-6 项正文...")
    p_sec = f"提取 1-6 项正文 JSON。键名：1培养目标, 2毕业要求, 3专业定位与特色, 4主干学科, 5标准学制, 6毕业条件。内容：{all_text[:12000]}"
    res_sec = call_llm_engine(provider_name, api_key, p_sec)
    if isinstance(res_sec, dict):
        results["sections"] = res_sec.get("sections", res_sec)

    # 任务 2: 附表 1 极致切片 (修复 AttributeError)
    if raw_rows_t1:
        clean_t1 = [r for r in raw_rows_t1 if any(r)]
        st.info(f"步骤 2/4: 解析计划表 (共 {len(clean_t1)} 行)...")
        for i in range(0, len(clean_t1), 25): # 缩小切片提高稳定性
            chunk = clean_t1[i:i+25]
            st.write(f"  > 正在校对第 {i+1} 至 {i+len(chunk)} 行...")
            p_chunk = f"将以下表格行转为 JSON 列表，对象字段为：[课程名称, 学分, 学位课, 上课学期]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p_chunk)
            
            # ✅ 核心修复：兼容 Dict 和 List 返回格式
            if isinstance(res, list):
                results["table1"].extend(res)
            elif isinstance(res, dict):
                # 尝试获取常见的键名，如果都没有则取字典中第一个列表值
                data = res.get("table1") or res.get("data") or res.get("items")
                if isinstance(data, list):
                    results["table1"].extend(data)
                else:
                    # 最后的兜底：如果字典里的值本身就是我们要的对象
                    for v in res.values():
                        if isinstance(v, list): results["table1"].extend(v); break

    # 任务 3: 附表 2
    st.info("步骤 3/4: 分析学分统计表...")
    res_t2 = call_llm_engine(provider_name, api_key, f"提取学分统计 JSON 列表。需区分焊接/无损。内容：{all_text}")
    if res_t2: 
        if isinstance(res_t2, list): results["table2"] = res_t2
        else: results["table2"] = res_t2.get("table2", [])

    # 任务 4: 附表 4 极致切片
    if raw_rows_t4:
        clean_t4 = [r for r in raw_rows_t4 if any(r)]
        st.info(f"步骤 4/4: 解析支撑矩阵 (共 {len(clean_t4)} 行)...")
        for i in range(0, len(clean_rows_t4), 35):
            chunk = clean_rows_t4[i:i+35]
            p_t4 = f"提取支撑矩阵 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p_t4)
            if isinstance(res, list): results["table4"].extend(res)
            elif isinstance(res, dict):
                data = res.get("table4") or res.get("data")
                if isinstance(data, list): results["table4"].extend(data)

    return results

# ============================================================
# 4. UI 渲染
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="培养方案智能提取 v5.1")
    if "final_data" not in st.session_state: st.session_state.final_data = None

    with st.sidebar:
        st.title("⚙️ 配置")
        prov = st.selectbox("模型供应商", list(PROVIDERS.keys()))
        key = st.text_input("API Key", type="password")
        if st.button("清理缓存"):
            st.session_state.final_data = None
            st.rerun()

    st.header("🧠 培养方案智能提取工作台")
    file = st.file_uploader("上传 PDF", type="pdf")

    if file and key and st.button("🚀 开始全量抽取", type="primary"):
        res = ultra_parse_v51(key, file.getvalue(), prov)
        if res:
            st.session_state.final_data = res
            st.success("抽取成功！")

    if st.session_state.final_data:
        d = st.session_state.final_data
        tabs = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        with tabs[0]:
            sections = d.get("sections", {})
            if sections:
                pick = st.selectbox("选择栏目", list(sections.keys()))
                st.text_area("内容", value=sections.get(pick, ""), height=400, key=f"v51_ta_{pick}")
        with tabs[1]: st.dataframe(pd.DataFrame(d.get("table1", [])), use_container_width=True)
        with tabs[2]: st.dataframe(pd.DataFrame(d.get("table2", [])), use_container_width=True)
        with tabs[3]: st.dataframe(pd.DataFrame(d.get("table4", [])), use_container_width=True)

if __name__ == "__main__":
    main()