import io, json, time, random
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from google.api_core import exceptions  # 捕获配额异常
from typing import Dict, List, Any

# ============================================================
# 1. 核心配置
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
# 2. 健壮的 AI 调用装饰器 (解决 ResourceExhausted)
# ============================================================
def retry_with_backoff(func, *args, max_retries=5, initial_sleep=2, **kwargs):
    """当遇到配额限制时自动重试"""
    retries = 0
    while retries < max_retries:
        try:
            return func(*args, **kwargs)
        except exceptions.ResourceExhausted:
            # 关键：捕获资源耗尽异常并进入等待
            sleep_time = initial_sleep * (2 ** retries) + random.uniform(0, 1)
            st.warning(f"触发 API 配额限制，正在等待 {int(sleep_time)} 秒后重试...")
            time.sleep(sleep_time)
            retries += 1
        except Exception as e:
            st.error(f"发生未知错误: {e}")
            return None
    st.error("已达到最大重试次数，请检查 API 配额或稍后再试。")
    return None

# ============================================================
# 3. 增强型抽取逻辑
# ============================================================
def ai_process_chunks_robust(model, data_list: List[Any], prompt_template: str, chunk_size: int = 25):
    results = []
    progress_bar = st.progress(0, text="AI 正在分块校验数据（带自动重试）...")
    
    for i in range(0, len(data_list), chunk_size):
        chunk = data_list[i : i + chunk_size]
        full_prompt = f"{prompt_template}\n原始数据：{json.dumps(chunk, ensure_ascii=False)}"
        
        # 使用重试机制调用生成内容
        response = retry_with_backoff(
            model.generate_content,
            full_prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        if response and response.text:
            try:
                chunk_res = json.loads(response.text)
                if isinstance(chunk_res, list): results.extend(chunk_res)
            except: pass
            
        progress_bar.progress(min((i + chunk_size) / len(data_list), 1.0))
        # 强制暂停 1 秒，降低触发概率
        time.sleep(1)
    
    return results

# ============================================================
# 4. 解析引擎
# ============================================================
def full_document_intelligence_suite(api_key, pdf_bytes):
    model = configure_ai(api_key)
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_pages_text = [p.extract_text() or "" for p in pdf.pages]
        
        # 提取正文 1-6 项 (带重试)
        sec_context = "\n".join(all_pages_text[:6])
        res_sec = retry_with_backoff(
            model.generate_content,
            f"提取 1-6 项正文（键1-6）。内容：{sec_context}",
            generation_config={"response_mime_type": "application/json"}
        )
        if res_sec: results["sections"] = json.loads(res_sec.text)

        # 全量搜集原始行
        raw_rows_t1, raw_rows_t4, text_t2 = [], [], ""
        for i, page in enumerate(pdf.pages):
            txt = all_pages_text[i]
            if "附表1" in txt or "教学计划表" in txt:
                tbl = page.extract_table()
                if tbl: raw_rows_t1.extend(tbl[1:])
            elif "附表2" in txt or "学分统计" in txt:
                text_t2 += f"\n{txt}"
            elif "附表4" in txt or "支撑关系" in txt:
                tbl = page.extract_table()
                if tbl: raw_rows_t4.extend(tbl[1:])

        # 分块校对 (带自动重试)
        if raw_rows_t1:
            results["tables"]["1"] = ai_process_chunks_robust(model, raw_rows_t1, f"转换教学计划表。列：{TABLE_1_FULL_COLS}")
        
        if text_t2:
            res_t2 = retry_with_backoff(model.generate_content, f"提取学分统计。文本：{text_t2}", generation_config={"response_mime_type": "application/json"})
            if res_t2: results["tables"]["2"] = json.loads(res_t2.text)

        if raw_rows_t4:
            results["tables"]["4"] = ai_process_chunks_robust(model, raw_rows_t4, "提取支撑强度矩阵(H/M/L)。字段：[课程名称, 指标点, 强度]", chunk_size=40)

    return results

# ============================================================
# UI (保持 v0.9 的 key 修复)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件工作台 v0.9.5")
    
    with st.sidebar:
        api_key = st.text_input("Gemini API Key", type="password", key="api_key_retry")
    
    file = st.file_uploader("上传 2024培养方案.pdf", type="pdf")

    if file and api_key:
        if st.button("🚀 执行全量抽取", type="primary", use_container_width=True):
            data = full_document_intelligence_suite(api_key, file.getvalue())
            st.session_state.all_data_final = data
            st.success("抽取任务已完成（已自动处理配额限制）")

    if "all_data_final" in st.session_state:
        d = st.session_state.all_data_final
        tabs = st.tabs(["1-6正文", "附表1:计划表", "附表2:学分统计", "附表4:支撑矩阵"])
        with tabs[1]:
            df1 = pd.DataFrame(d["tables"]["1"])
            if not df1.empty: st.data_editor(df1.reindex(columns=TABLE_1_FULL_COLS), use_container_width=True)
        # 其余 Tab 渲染逻辑与之前相同...

if __name__ == "__main__":
    main()