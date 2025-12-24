import io, json, time, random, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any
from openai import OpenAI
from google.api_core import exceptions

# ============================================================
# 1. 供应商配置
# ============================================================
PROVIDERS = {
    "通义千问 (Qwen)": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "is_gemini": False, "limit": 2048},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "is_gemini": False, "limit": 4096},
    "Gemini (Google)": {"base_url": None, "model": "gemini-1.5-flash", "is_gemini": True, "limit": 8192},
}

# ============================================================
# 2. 增强型数据清洗工具
# ============================================================
def safe_to_df(data: Any, default_cols: List[str]) -> pd.DataFrame:
    """解决内容不显示的核心：多层搜索与类型转换"""
    if not data: return pd.DataFrame(columns=default_cols)
    
    clean_list = []
    # 智能解包：如果是字典，寻找其中的列表
    rows = data if isinstance(data, list) else []
    if isinstance(data, dict):
        for k in ["table1", "table2", "table4", "data", "items"]:
            if isinstance(data.get(k), list):
                rows = data[k]
                break
        if not rows: # 兜底：取第一个列表值
            for v in data.values():
                if isinstance(v, list): rows = v; break

    for item in rows:
        if isinstance(item, dict): clean_list.append(item)
        elif isinstance(item, list): clean_list.append(dict(zip(default_cols, item)))
    
    return pd.DataFrame(clean_list) if clean_list else pd.DataFrame(columns=default_cols)

# ============================================================
# 3. 统一调用内核 (带 Markdown 剥离)
# ============================================================
def call_llm_engine(provider_name, api_key, prompt, max_retries=3):
    config = PROVIDERS.get(provider_name, PROVIDERS["Gemini (Google)"])
    for i in range(max_retries):
        try:
            time.sleep(6 if config["is_gemini"] else 3)
            if config["is_gemini"]:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(config["model"])
                resp = model.generate_content(prompt, generation_config={"response_mime_type":"application/json"})
                return json.loads(resp.text)
            else:
                client = OpenAI(api_key=api_key, base_url=config["base_url"])
                resp = client.chat.completions.create(
                    model=config["model"],
                    messages=[{"role":"system","content":"你是一个严谨的教务专家，只输出JSON。"},{"role":"user","content":prompt}],
                    response_format={"type": "json_object"},
                    max_tokens=config["limit"]
                )
                raw = resp.choices[0].message.content
                # 剥离 Markdown 标签以防解析失败
                return json.loads(re.sub(r'```json\s*|\s*```', '', raw).strip())
        except exceptions.ResourceExhausted:
            time.sleep(20 * (i + 1))
        except Exception:
            continue
    return None

# ============================================================
# 4. 极致分块解析引擎 (修复缺失与空显示)
# ============================================================
def ultra_parse_v56(api_key, pdf_bytes, provider_name):
    results = {"sections": {}, "table1": [], "table2": [], "table4": []}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
        raw_t1, raw_t4 = [], []
        for p in pdf.pages:
            txt, tbls = p.extract_text() or "", p.extract_tables()
            if any(x in txt for x in ["附表1", "教学计划表"]):
                for t in tbls: raw_t1.extend(t)
            if any(x in txt for x in ["附表4", "支撑矩阵"]):
                for t in tbls: raw_t4.extend(t)

    # 1. 正文独立提取
    st.info("步骤 1/5: 正在提取 1-6 项正文...")
    res_sec = call_llm_engine(provider_name, api_key, f"提取 1-6 项正文 JSON。键名：1培养目标, 2毕业要求, 3专业定位与特色, 4主干学科, 5标准学制, 6毕业条件。内容：{all_text[:12000]}")
    if res_sec: results["sections"] = res_sec

    # 2. 附表 1 (极致切片：Qwen 建议 15 行)
    if raw_t1:
        clean_t1 = [r for r in raw_t1 if any(r)]
        st.info(f"步骤 2/5: 解析计划表 (共 {len(clean_t1)} 行，防止缺失)...")
        for i in range(0, len(clean_t1), 15): # 👈 下调分块大小至 15
            chunk = clean_t1[i : i+15]
            st.write(f"  > 正在处理计划表第 {i+1} 至 {i+len(chunk)} 行...")
            r = call_llm_engine(provider_name, api_key, f"将表格行转为 JSON 列表 [课程名称, 学分, 学位课, 上课学期]。数据：{json.dumps(chunk, ensure_ascii=False)}")
            results["table1"].extend(safe_to_df(r, ["课程名称", "学分", "学位课", "上课学期"]).to_dict('records'))

    # 3. 附表 2 (独立提取，解决空显示)
    st.info("步骤 3/5: 解析学分统计表...")
    res_t2 = call_llm_engine(provider_name, api_key, f"提取附表 2 学分统计 JSON 列表。必须区分'焊接'和'无损检测'方向。内容：{all_text}")
    results["table2"] = safe_to_df(res_t2, ["专业方向", "项目", "学分要求"]).to_dict('records')

    # 4. 附表 4 (支撑矩阵极致切片)
    if raw_t4:
        clean_t4 = [r for r in raw_t4 if any(r)]
        st.info(f"步骤 4/5: 解析支撑矩阵 (共 {len(clean_t4)} 行，防止缺失)...")
        for i in range(0, len(clean_t4), 15): # 👈 下调分块大小至 15
            chunk = clean_t4[i : i+15]
            st.write(f"  > 正在映射矩阵第 {i+1} 至 {i+len(chunk)} 条支撑关系...")
            r = call_llm_engine(provider_name, api_key, f"提取支撑矩阵 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}")
            results["table4"].extend(safe_to_df(r, ["课程名称", "指标点", "强度"]).to_dict('records'))

    return results

# ============================================================
# 5. UI 渲染
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学方案提取 v5.6")
    if "data" not in st.session_state: st.session_state.data = None

    with st.sidebar:
        st.title("⚙️ 配置")
        prov = st.selectbox("模型供应商", list(PROVIDERS.keys()))
        key = st.text_input("API Key", type="password")
        if st.button("清理缓存"):
            st.session_state.data = None
            st.rerun()

    st.header("🧠 培养方案智能工作台 (极致精度版)")
    file = st.file_uploader("上传 PDF", type="pdf")

    if file and key and st.button("🚀 开始执行抽取", type="primary"):
        res = ultra_parse_v56(key, file.getvalue(), prov)
        if res:
            st.session_state.data = res
            st.success("🎉 抽取任务已全部完成！")

    if st.session_state.data:
        d = st.session_state.data
        tabs = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        with tabs[0]:
            sec = d.get("sections", {})
            if isinstance(sec, dict) and sec:
                if "sections" in sec: sec = sec["sections"]
                pick = st.selectbox("查看栏目", list(sec.keys()), key="v56_sel")
                st.text_area("内容", value=str(sec.get(pick, "")), height=400, key=f"v56_ta_{pick}")
        with tabs[1]: st.dataframe(pd.DataFrame(d.get("table1", [])), use_container_width=True)
        with tabs[2]: st.dataframe(pd.DataFrame(d.get("table2", [])), use_container_width=True)
        with tabs[3]: st.dataframe(pd.DataFrame(d.get("table4", [])), use_container_width=True)

if __name__ == "__main__":
    main()