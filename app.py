import io, json, time, random, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any
from openai import OpenAI
from google.api_core import exceptions  # ✅ 解决 NameError: exceptions

# ============================================================
# 1. 模型供应商配置 (增加 max_out 限制提示)
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
            # 强制冷却：Gemini 免费版 5s，其他 2s
            time.sleep(5 if config["is_gemini"] else 2)
            
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
                        {"role": "system", "content": "你是一个严谨的教务专家，只输出 JSON 列表。严禁输出任何额外描述。"},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=config["limit"]
                )
                return json.loads(response.choices[0].message.content)
        except exceptions.ResourceExhausted:
            wait = (i + 1) * 20
            st.warning(f"触发配额限制，正在第 {i+1} 次重试，需等待 {wait} 秒...")
            time.sleep(wait)
        except Exception as e:
            if i == max_retries - 1: st.error(f"调用失败: {str(e)}")
            continue
    return None

# ============================================================
# 3. 智能解析核心 (增加极致切片逻辑)
# ============================================================
def ultra_parse(api_key, pdf_bytes, provider_name):
    # 1. 初始化结果集
    results = {"sections": {}, "table1": [], "table2": [], "table4": []}
    
    # 2. 提取文本与原始表格行
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
        raw_rows_t1 = []
        raw_rows_t4 = []
        for page in pdf.pages:
            tbls = page.extract_tables()
            txt = page.extract_text() or ""
            if "附表1" in txt or "教学计划表" in txt:
                for t in tbls: raw_rows_t1.extend(t)
            if "附表4" in txt or "支撑矩阵" in txt:
                for t in tbls: raw_rows_t4.extend(t)

    # --- 任务 A: 提取 1-6 项正文 (单次请求文字量可控) ---
    st.info("步骤 1: 正在提取 1-6 项正文内容...")
    p_sec = f"从文本中提取 1-6 项正文 JSON。要求分条列出。键名：1培养目标, 2毕业要求, 3专业定位与特色, 4主干学科, 5标准学制, 6毕业条件。文本：{all_text[:12000]}"
    res_sec = call_llm_engine(provider_name, api_key, p_sec)
    if res_sec: results["sections"] = res_sec

    # --- 任务 B: 附表 1 (极致切片：每 30 行请求一次，彻底根除 JSON 截断) ---
    if raw_rows_t1:
        st.info(f"步骤 2: 正在解析教学计划表 (共 {len(raw_rows_t1)} 行，分块处理中)...")
        # 过滤掉明显的空行
        clean_rows_t1 = [r for r in raw_rows_t1 if any(r)]
        for i in range(0, len(clean_rows_t1), 30):
            chunk = clean_rows_t1[i:i+30]
            st.write(f"  > 正在处理第 {i} 至 {i+len(chunk)} 行...")
            p_chunk = f"将数据行转换为 JSON 列表。字段：课程名称, 学分, 学位课, 上课学期。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res_chunk = call_llm_engine(provider_name, api_key, p_chunk)
            if res_chunk and isinstance(res_chunk.get("table1"), list):
                results["table1"].extend(res_chunk["table1"])
            elif isinstance(res_chunk, list): # 兼容不同模型的返回习惯
                results["table1"].extend(res_chunk)

    # --- 任务 C: 附表 2 (学分统计) ---
    st.info("步骤 3: 正在分析学分统计表...")
    p_t2 = f"提取学分统计 JSON 列表。必须区分焊接/无损检测。内容：{all_text}"
    res_t2 = call_llm_engine(provider_name, api_key, p_t2)
    if res_t2: results["table2"] = res_t2.get("table2", [])

    # --- 任务 D: 附表 4 (支撑矩阵切片) ---
    if raw_rows_t4:
        st.info(f"步骤 4: 正在解析支撑关系矩阵...")
        clean_rows_t4 = [r for r in raw_rows_t4 if any(r)]
        for i in range(0, len(clean_rows_t4), 40):
            chunk = clean_rows_t4[i:i+40]
            p_chunk_t4 = f"提取支撑矩阵 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
            res_chunk_t4 = call_llm_engine(provider_name, api_key, p_chunk_t4)
            if res_chunk_t4:
                results["table4"].extend(res_chunk_t4.get("table4", []))

    return results

# ============================================================
# 4. Streamlit UI (修复所有显示逻辑)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="培养方案智能工作台 v5.0")
    
    if "final_data" not in st.session_state:
        st.session_state.final_data = None

    with st.sidebar:
        st.title("⚙️ 模型配置")
        prov = st.selectbox("选择模型供应商", list(PROVIDERS.keys()), key="v5_prov")
        key = st.text_input(f"输入 {prov} API Key", type="password", key="v5_key")
        st.divider()
        if st.button("清理数据缓存"):
            st.session_state.final_data = None
            st.rerun()

    st.header("🧠 培养方案全量智能提取 (终极稳定版)")
    file = st.file_uploader("上传 2024培养方案.pdf", type="pdf")

    if file and key and st.button("🚀 执行一键全量抽取", type="primary"):
        with st.spinner("正在执行超长文档分块校对，请稍候（约 1-2 分钟）..."):
            res = ultra_parse(key, file.getvalue(), prov)
            if res:
                st.session_state.final_data = res
                st.success("🎉 数据抽取完毕！")

    if st.session_state.final_data:
        d = st.session_state.final_data
        t1, t2, t3, t4 = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with t1:
            sections = d.get("sections", {})
            if isinstance(sections, dict) and sections:
                # 兼容不同模型可能返回的嵌套结构
                if "sections" in sections: sections = sections["sections"]
                sec_pick = st.selectbox("选择栏目", list(sections.keys()), key="v5_sec_sel")
                st.text_area("内容", value=sections.get(sec_pick, ""), height=450, key=f"v5_ta_{sec_pick}")
            else:
                st.warning("正文部分提取失败，请检查 API Key 或尝试 Gemini。")

        with t2:
            st.dataframe(pd.DataFrame(d.get("table1", [])), use_container_width=True)
        with t3:
            st.dataframe(pd.DataFrame(d.get("table2", [])), use_container_width=True)
        with t4:
            st.dataframe(pd.DataFrame(d.get("table4", [])), use_container_width=True)

if __name__ == "__main__":
    main()