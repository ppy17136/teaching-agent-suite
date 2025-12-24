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
# 2. 安全数据转换工具 (解决 AttributeError)
# ============================================================
def safe_to_df(data: Any, default_cols: List[str]) -> pd.DataFrame:
    """
    强制将 AI 返回的杂乱数据清洗为 Pandas 可识别的字典列表
    """
    if not isinstance(data, list):
        return pd.DataFrame(columns=default_cols)
    
    clean_list = []
    for item in data:
        if isinstance(item, dict):
            clean_list.append(item)
        elif isinstance(item, list) and len(item) <= len(default_cols):
            # 如果 AI 错误地返回了列表，尝试将其转回字典
            clean_list.append(dict(zip(default_cols, item)))
    
    df = pd.DataFrame(clean_list)
    if df.empty:
        return pd.DataFrame(columns=default_cols)
    return df

# ============================================================
# 3. 深度流控调用引擎
# ============================================================
def call_llm_engine(provider_name, api_key, prompt, max_retries=3):
    config = PROVIDERS.get(provider_name, PROVIDERS["Gemini (Google)"])
    for i in range(max_retries):
        try:
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
                        {"role": "system", "content": "你是一个只输出 JSON 数据的教务专家。严禁解释文字。"},
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
# 4. 稳健型分块解析逻辑
# ============================================================
def ultra_parse_v54(api_key, pdf_bytes, provider_name):
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
    st.info("步骤 1/4: 提取正文内容...")
    p_sec = f"提取正文 JSON。键名：1培养目标, 2毕业要求, 3专业定位与特色, 4主干学科, 5标准学制, 6毕业条件。内容：{all_text[:12000]}"
    res_sec = call_llm_engine(provider_name, api_key, p_sec)
    if res_sec: results["sections"] = res_sec.get("sections", res_sec)

    # 任务 2: 附表 1 分块
    if raw_rows_t1:
        clean_t1 = [r for r in raw_rows_t1 if any(r)]
        st.info(f"步骤 2/4: 解析计划表 (共 {len(clean_t1)} 行)...")
        for i in range(0, len(clean_t1), 25):
            chunk = clean_t1[i : i+25]
            p_chunk = f"表格行转 JSON 列表。字段：[课程名称, 学分, 学位课, 上课学期]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p_chunk)
            if res:
                data = res.get("table1") or res.get("data") or (res if isinstance(res, list) else [])
                if isinstance(data, list): results["table1"].extend(data)

    # 任务 3: 附表 2
    st.info("步骤 3/4: 分析学分统计...")
    p_t2 = f"提取学分统计 JSON 列表。区分焊接/无损检测。内容：{all_text}"
    res_t2 = call_llm_engine(provider_name, api_key, p_t2)
    if res_t2: results["table2"] = res_t2 if isinstance(res_t2, list) else res_t2.get("table2", [])

    # 任务 4: 附表 4 分块 (修复变量命名)
    if raw_rows_t4:
        clean_t4 = [r for r in raw_rows_t4 if any(r)]
        st.info(f"步骤 4/4: 解析支撑矩阵 (共 {len(clean_t4)} 行)...")
        for i in range(0, len(clean_t4), 35):
            chunk = clean_t4[i : i+35]
            p_t4 = f"提取支撑矩阵 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res = call_llm_engine(provider_name, api_key, p_t4)
            if res:
                data = res.get("table4") or res.get("data") or (res if isinstance(res, list) else [])
                if isinstance(data, list): results["table4"].extend(data)

    return results

# ============================================================
# 5. UI 渲染 (解决 AttributeError 核心区域)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="培养方案智能助手 v5.4")
    if "data" not in st.session_state: st.session_state.data = None

    with st.sidebar:
        st.title("⚙️ 配置")
        prov = st.selectbox("模型供应商", list(PROVIDERS.keys()))
        key = st.text_input("API Key", type="password")
        if st.button("清理缓存"):
            st.session_state.data = None
            st.rerun()

    st.header("🧠 培养方案智能提取工作台 (v5.4 健壮版)")
    file = st.file_uploader("上传 PDF 培养方案", type="pdf")

    if file and key and st.button("🚀 开始全量抽取", type="primary"):
        res = ultra_parse_v54(key, file.getvalue(), prov)
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
                st.text_area("内容", value=sec.get(pick, ""), height=400, key=f"ta_{pick}")
        
        # ✅ 使用 safe_to_df 替代直接创建，防止 AttributeError
        with tabs[1]:
            st.dataframe(safe_to_df(d.get("table1"), ["课程名称", "学分", "学位课", "上课学期"]), use_container_width=True)
        
        with tabs[2]:
            st.dataframe(safe_to_df(d.get("table2"), ["专业方向", "项目", "学分要求"]), use_container_width=True)
        
        with tabs[3]:
            # 处理支撑矩阵渲染
            st.dataframe(safe_to_df(d.get("table4"), ["课程名称", "指标点", "强度"]), use_container_width=True)

if __name__ == "__main__":
    main()