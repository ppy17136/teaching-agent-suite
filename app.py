import io, json, time, base64
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from typing import Dict, List, Any

# ============================================================
# 1. 核心配置与字段定义
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
# 2. 增强型 AI 抽取逻辑
# ============================================================
def ai_query_json(model, prompt: str) -> Any:
    """强制要求 AI 返回结构化 JSON"""
    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        return {}

def process_sections_1_6(model, full_text: str):
    """专门针对辽宁石油化工大学格式的正文抽取"""
    prompt = f"""
    你是一个教务管理专家。请从以下文本中提取 1-6 项内容。
    注意识别这些标题：
    1: “一、培养目标” 之后的内容 [cite: 10]
    2: “二、毕业要求” 之后的内容 [cite: 21]
    3: “三、专业定位与特色” 之后的内容 [cite: 80]
    4: “四、主干学科、专业核心课程...” 之后的内容 [cite: 84]
    5: “五、标准学制与授予学位” 之后的内容 [cite: 88]
    6: “六、毕业条件” 之后的内容 [cite: 91]
    
    返回 JSON 字典，键为 "1", "2", "3", "4", "5", "6"。
    文本：{full_text[:15000]}
    """
    return ai_query_json(model, prompt)

def process_credit_table(model, raw_rows: List[List[str]]):
    """针对复杂的附表 2 嵌套表头进行语义重构 [cite: 114, 120]"""
    prompt = f"""
    以下是附表 2（学分统计表）的原始行数据。由于单元格合并，数据可能错位。
    请提取各课程体系的学分分配情况。
    目标字段：["课程体系", "学分统计", "学分比例", "备注"]
    数据：{json.dumps(raw_rows, ensure_ascii=False)}
    """
    return ai_query_json(model, prompt)

# ============================================================
# 3. PDF 解析引擎升级
# ============================================================
def parse_full_document(api_key, pdf_bytes):
    model = configure_ai(api_key)
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # A. 提取正文（前 6 页包含所有 1-6 项） [cite: 10-91]
        pages_text = [p.extract_text() or "" for p in pdf.pages[:6]]
        full_context = "\n".join(pages_text)
        results["sections"] = process_sections_1_6(model, full_context)

        # B. 全文扫描附表
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").lower()
            # 使用更宽松的表格提取设置以应对附表 2 的线条
            table = page.extract_table(table_settings={
                "vertical_strategy": "text", 
                "horizontal_strategy": "lines"
            })
            if not table: continue

            # 附表 1 (教学计划) [cite: 105]
            if "附表1" in text or "教学计划表" in text:
                st.write(f"正在深度解析：附表1 (第 {i+1} 页)...")
                prompt = f"提取教学计划表 JSON。列：{TABLE_1_FULL_COLS}。"
                res = ai_query_json(model, f"{prompt}\n数据：{json.dumps(table[1:])}")
                if isinstance(res, list): results["tables"]["1"].extend(res)

            # 附表 2 (学分统计 - 修复重点) 
            elif "附表2" in text or "学分统计" in text:
                st.write(f"正在重构数据：附表2 (第 {i+1} 页)...")
                res = process_credit_table(model, table)
                if isinstance(res, list): results["tables"]["2"].extend(res)

            # 附表 4 (支撑关系) [cite: 124]
            elif "附表4" in text or "支撑关系" in text:
                st.write(f"正在映射矩阵：附表4 (第 {i+1} 页)...")
                prompt = "提取课程支撑矩阵。字段：[课程名称, 指标点, 强度]。"
                res = ai_query_json(model, f"{prompt}\n数据：{json.dumps(table)}")
                if isinstance(res, list): results["tables"]["4"].extend(res)

    return results

# ============================================================
# 4. Streamlit UI
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件智能工作台")
    
    with st.sidebar:
        st.title("⚙️ 配置中心")
        api_key = st.text_input("Gemini API Key", type="password", key="main_key")
    
    st.header("🧠 培养方案全量智能工作台")
    file = st.file_uploader("上传培养方案 PDF", type="pdf")

    if file and api_key:
        if st.button("🚀 开始智能全量抽取", type="primary", use_container_width=True):
            with st.spinner("AI 正在解析正文及所有附表..."):
                data = parse_full_document(api_key, file.getvalue())
                st.session_state.all_data = data
                st.success("抽取完成！")

    if "all_data" in st.session_state:
        d = st.session_state.all_data
        tabs = st.tabs(["1-11 正文", "附表1:计划表", "附表2:学分统计", "附表4:支撑矩阵"])
        
        with tabs[0]:
            sec = st.selectbox("查看栏目内容", ["1","2","3","4","5","6"], key="sec_nav")
            st.text_area("内容文本", value=d["sections"].get(sec, "未提取到内容"), height=400)
            
        with tabs[1]:
            st.dataframe(pd.DataFrame(d["tables"]["1"]), use_container_width=True)
            
        with tabs[2]:
            st.markdown("### 学分统计总结 (基于附表 2A/2B)")
            st.table(pd.DataFrame(d["tables"]["2"]))
            
        with tabs[3]:
            st.dataframe(pd.DataFrame(d["tables"]["4"]), use_container_width=True)

if __name__ == "__main__":
    main()