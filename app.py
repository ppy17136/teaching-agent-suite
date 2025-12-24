import io, json, time
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from google.api_core import exceptions

# ============================================================
# 1. 核心逻辑：减少请求次数，增加单次容量
# ============================================================
def ai_call_with_throttle(model, prompt, generation_config):
    """强制节流调用：确保每分钟请求不超过 12 次 (留余量)"""
    try:
        # 每次调用前强制冷却，确保符合 15 RPM 限制
        time.sleep(5) 
        return model.generate_content(prompt, generation_config=generation_config)
    except exceptions.ResourceExhausted:
        st.error("API 额度已耗尽。请等待 60 秒后再次点击，或更换 API Key。")
        return None

def ai_process_large_chunks(model, data_list, prompt_template, chunk_size=100):
    """大幅增加 chunk_size（从 30 增加到 100），减少请求总数"""
    results = []
    # 附表 1 约 150 行，100 行一组只需 2 次请求，原来需要 5-6 次
    for i in range(0, len(data_list), chunk_size):
        chunk = data_list[i : i + chunk_size]
        full_prompt = f"{prompt_template}\n数据：{json.dumps(chunk, ensure_ascii=False)}"
        
        st.write(f"正在处理第 {i+1} 至 {i+len(chunk)} 行... (安全节流中)")
        response = ai_call_with_throttle(
            model, 
            full_prompt, 
            {"response_mime_type": "application/json"}
        )
        
        if response:
            try:
                res = json.loads(response.text)
                if isinstance(res, list): results.extend(res)
            except: pass
    return results

# ============================================================
# 2. 增强型解析引擎
# ============================================================
def final_stable_processor(api_key, pdf_bytes):
    genai.configure(api_key=api_key)
    # 必须使用 flash 才能获得 15 RPM，Pro 只有 2 RPM 会直接瘫痪
    model = genai.GenerativeModel('gemini-2.5-flash')
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = [p.extract_text() or "" for p in pdf.pages]
        
        # 1. 正文抽取 (合并 1-6 项，仅 1 次请求)
        st.info("步骤 1/4: 正在提取 1-6 项正文...")
        sec_context = "\n".join(all_text[:6])
        res_sec = ai_call_with_throttle(model, f"提取 1-6 项正文 JSON。内容：{sec_context}", {"response_mime_type": "application/json"})
        if res_sec: results["sections"] = json.loads(res_sec.text)

        # 2. 搜集原始行 (附表 1-4)
        raw_t1, raw_t4, text_t2 = [], [], ""
        for i, page in enumerate(pdf.pages):
            txt = all_text[i]
            if "附表1" in txt or "教学计划表" in txt:
                tbl = page.extract_table()
                if tbl: raw_t1.extend(tbl[1:]) # 
            elif "附表2" in txt or "学分统计" in txt:
                text_t2 += f"\n{txt}" # [cite: 113, 119]
            elif "附表4" in txt or "支撑关系" in txt:
                tbl = page.extract_table()
                if tbl: raw_t4.extend(tbl[1:]) # 

        # 3. 附表处理 (通过增加 chunk_size 极大减少请求次数)
        if raw_t1:
            st.info("步骤 2/4: 正在校对附表 1 (教学计划)...")
            results["tables"]["1"] = ai_process_large_chunks(model, raw_t1, "转换教学计划表。字段：[课程体系, 课程编码, 课程名称, 开课模式, 考核方式, 学分, 总学时, 内_讲课, 内_实验, 内_上机, 内_实践, 外_学分, 外_学时, 上课学期, 专业方向, 学位课, 备注]", chunk_size=80)
        
        if text_t2:
            st.info("步骤 3/4: 正在处理附表 2 (学分统计)...")
            res_t2 = ai_call_with_throttle(model, f"提取学分统计 JSON。文本：{text_t2}", {"response_mime_type": "application/json"})
            if res_t2: results["tables"]["2"] = json.loads(res_t2.text)

        if raw_t4:
            st.info("步骤 4/4: 正在校对附表 4 (支撑矩阵)...")
            # 附表 4 内容极多，增加 chunk_size 到 100 减少请求
            results["tables"]["4"] = ai_process_large_chunks(model, raw_t4, "提取支撑矩阵。字段：[课程名称, 指标点, 强度]", chunk_size=100)

    return results

def main():
    st.set_page_config(layout="wide")
    with st.sidebar:
        api_key = st.text_input("Gemini API Key", type="password", key="safe_key")
    
    file = st.file_uploader("上传 PDF", type="pdf")
    if file and api_key:
        if st.button("🚀 执行全量抽取", type="primary"):
            data = final_stable_processor(api_key, file.getvalue())
            st.session_state.final_v98 = data
            st.success("抽取完成！")

    # 结果渲染逻辑... (同前)