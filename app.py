import io, json, time
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any

# ============================================================
# 1. 核心字段定义
# ============================================================
TABLE_1_FULL_COLS = [
    "课程体系", "课程编码", "课程名称", "开课模式", "考核方式", 
    "学分", "总学时", "内_讲课", "内_实验", "内_上机", "内_实践", 
    "外_学分", "外_学时", "上课学期", "专业方向", "学位课", "备注"
]

def configure_ai(api_key: str):
    genai.configure(api_key=api_key)
    return genai.GenerativeModel('gemini-2.5-flash')

# ============================================================
# 2. AI 智能分块处理逻辑 (解决显示不全的关键)
# ============================================================
def ai_process_chunks(model, data_list: List[Any], prompt_template: str, chunk_size: int = 30):
    """将大量行数据分块发送给 AI，防止截断"""
    results = []
    progress_bar = st.progress(0, text="AI 正在分块校验数据...")
    
    for i in range(0, len(data_list), chunk_size):
        chunk = data_list[i : i + chunk_size]
        full_prompt = f"{prompt_template}\n原始数据片段：{json.dumps(chunk, ensure_ascii=False)}"
        
        response = model.generate_content(
            full_prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        try:
            chunk_res = json.loads(response.text)
            if isinstance(chunk_res, list):
                results.extend(chunk_res)
        except:
            pass
        progress_bar.progress(min((i + chunk_size) / len(data_list), 1.0))
    
    return results

# ============================================================
# 3. 增强型解析引擎
# ============================================================
def full_document_intelligence_suite(api_key, pdf_bytes):
    model = configure_ai(api_key)
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_pages_text = [p.extract_text() or "" for p in pdf.pages]
        
        # --- A. 正文 1-6 项抽取 ---
        st.write("正在提取正文 1-6 项...")
        sec_context = "\n".join(all_pages_text[:6])
        sec_prompt = "提取 1-6 项内容，返回 JSON 字典（键为'1'-'6'）。"
        res_sec = model.generate_content(f"{sec_prompt}\n文本：{sec_context}", 
                                       generation_config={"response_mime_type": "application/json"})
        results["sections"] = json.loads(res_sec.text)

        # --- B. 全量表格搜集 ---
        raw_rows_t1 = []  # 附表 1 原始行
        raw_rows_t4 = []  # 附表 4 原始行
        text_t2 = ""      # 附表 2 原始文本（文本重建模式更准）

        for i, page in enumerate(pdf.pages):
            txt = all_pages_text[i]
            # 定位附表 1
            if "附表1" in txt or "教学计划表" in txt:
                tbl = page.extract_table()
                if tbl: raw_rows_t1.extend(tbl[1:])
            # 定位附表 2 (学分统计)
            elif "附表2" in txt or "学分统计" in txt:
                text_t2 += f"\n{txt}"
            # 定位附表 4 (支撑矩阵)
            elif "附表4" in txt or "支撑关系" in txt:
                tbl = page.extract_table()
                if tbl: raw_rows_t4.extend(tbl[1:])

        # --- C. AI 分块校对（核心修复） ---
        if raw_rows_t1:
            st.write(f"正在全量校对附表 1（共 {len(raw_rows_t1)} 行原始数据）...")
            t1_prompt = f"转换教学计划表为 JSON 列表。列：{TABLE_1_FULL_COLS}。严禁遗漏任何课程。"
            results["tables"]["1"] = ai_process_chunks(model, raw_rows_t1, t1_prompt)

        if text_t2:
            st.write("正在从文本重建附表 2（学分统计）...")
            t2_prompt = "提取学分统计。字段：[课程体系, 必修学分, 选修学分, 合计, 比例]。返回 JSON 列表。"
            res_t2 = model.generate_content(f"{t2_prompt}\n文本：{text_t2}", 
                                          generation_config={"response_mime_type": "application/json"})
            results["tables"]["2"] = json.loads(res_t2.text)

        if raw_rows_t4:
            st.write(f"正在全量校对附表 4（共 {len(raw_rows_t4)} 行矩阵数据）...")
            t4_prompt = "提取支撑矩阵 JSON 列表。字段：[课程名称, 指标点, 强度]。"
            results["tables"]["4"] = ai_process_chunks(model, raw_rows_t4, t4_prompt, chunk_size=50)

    return results

# ============================================================
# 4. Streamlit UI
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件全量工作台")
    
    with st.sidebar:
        st.title("⚙️ 配置")
        api_key = st.text_input("Gemini API Key", type="password", key="api_key_v9")
    
    st.header("🧠 培养方案全量智能抽取 (v0.9)")
    file = st.file_uploader("上传 PDF", type="pdf")

    if file and api_key:
        if st.button("🚀 开始全量深度抽取", type="primary", use_container_width=True):
            data = full_document_intelligence_suite(api_key, file.getvalue())
            st.session_state.all_data_v9 = data
            st.success("全量抽取完毕！")

    if "all_data_v9" in st.session_state:
        d = st.session_state.all_data_v9
        t1, t2, t3, t4 = st.tabs(["1-11 正文", "附表1:全量计划表", "附表2:学分统计", "附表4:支撑矩阵"])
        
        with t1:
            sec = st.selectbox("栏目", ["1","2","3","4","5","6"])
            st.text_area("内容", value=d["sections"].get(sec, ""), height=400)
            
        with t2:
            df1 = pd.DataFrame(d["tables"]["1"])
            if not df1.empty:
                df1 = df1.reindex(columns=TABLE_1_FULL_COLS)
                st.write(f"已提取课程总数：{len(df1)} 门")
                st.data_editor(df1, use_container_width=True)
            
        with t3:
            st.table(pd.DataFrame(d["tables"]["2"]))
            
        with t4:
            st.dataframe(pd.DataFrame(d["tables"]["4"]), use_container_width=True)

if __name__ == "__main__":
    main()