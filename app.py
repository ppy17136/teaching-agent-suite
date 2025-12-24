import io, json, time, random, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from google.api_core import exceptions
from typing import Dict, List, Any

# ============================================================
# 1. 常量与字段定义
# ============================================================
TABLE_1_FULL_COLS = [
    "课程体系", "课程编码", "课程名称", "开课模式", "考核方式", 
    "学分", "总学时", "内_讲课", "内_实验", "内_上机", "内_实践", 
    "外_学分", "外_学时", "上课学期", "专业方向", "学位课", "备注"
]

# ============================================================
# 2. 核心 AI 处理引擎 (带节流与数据清洗)
# ============================================================
def ai_safe_call(model, prompt: str, max_retries=3):
    """确保在 15 RPM 限制内稳定运行，并处理异常"""
    for i in range(max_retries):
        try:
            time.sleep(5)  # 强制冷却，适配免费版 RPM
            response = model.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            )
            # 预处理：去除可能的 Markdown 代码块包裹
            clean_text = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(clean_text)
        except exceptions.ResourceExhausted:
            wait = (i + 1) * 10
            st.warning(f"配额限制，等待 {wait} 秒...")
            time.sleep(wait)
        except Exception as e:
            continue
    return None

def extract_sections_robust(model, full_text):
    """专门针对 1-6 项正文的强化提取 [cite: 10-91]"""
    prompt = f"""
    你是一个教务专家。请从文本中提取以下 6 个章节的内容。
    注意：内容必须完整，不要只提取标题。
    1: 培养目标 (通常以'一、培养目标'开始)
    2: 毕业要求 (通常以'二、毕业要求'开始)
    3: 专业定位与特色 (通常以'三、专业定位与特色'开始)
    4: 主干学科/核心课程/实践环节 (通常以'四、主干学科'开始)
    5: 标准学制与授予学位 (通常以'五、标准学制'开始)
    6: 毕业条件 (通常以'六、毕业条件'开始)

    返回 JSON 字典，格式：{{"1": "...", "2": "...", ...}}
    文本：{full_text[:15000]}
    """
    return ai_safe_call(model, prompt)

def process_appendix_2_flat(model, raw_text):
    """解决截图中的 JSON 字符串问题：强制返回扁平化列表 """
    prompt = f"""
    将学分统计表转换为扁平的 JSON 列表。
    每个对象必须是简单的“键-值”对，严禁在值中使用嵌套的字典或列表。
    字段：["项目分类", "具体项", "学分要求", "学分占比", "备注"]
    数据：{raw_text}
    """
    return ai_safe_call(model, prompt)

# ============================================================
# 3. PDF 解析与流程控制
# ============================================================
def parse_document_v11(api_key, pdf_bytes):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = [p.extract_text() or "" for p in pdf.pages]
        
        # 1. 提取 1-6 正文
        st.write("正在智能提取正文内容...")
        sec_context = "\n".join(all_text[:6])
        results["sections"] = extract_sections_robust(model, sec_context)

        # 2. 扫描附表
        raw_t1, raw_t4, text_t2 = [], [], ""
        for i, page in enumerate(pdf.pages):
            txt = all_text[i]
            if "附表1" in txt or "教学计划表" in txt:
                tbl = page.extract_table()
                if tbl: raw_t1.extend(tbl[1:])
            elif "附表2" in txt or "学分统计" in txt:
                text_t2 += f"\n{txt}"
            elif "附表4" in txt or "支撑关系" in txt:
                tbl = page.extract_table()
                if tbl: raw_t4.extend(tbl[1:])

        # 3. 分块处理附表
        if raw_t1:
            st.write("正在校对教学计划表...")
            # 分块逻辑同前，chunk_size 设为 80 以减少请求数
            for i in range(0, len(raw_t1), 80):
                chunk = raw_t1[i : i+80]
                prompt = f"转换教学计划表片段为 JSON 列表。字段：{TABLE_1_FULL_COLS}。数据：{json.dumps(chunk, ensure_ascii=False)}"
                res = ai_safe_call(model, prompt)
                if isinstance(res, list): results["tables"]["1"].extend(res)
        
        if text_t2:
            st.write("正在格式化学分统计表...")
            results["tables"]["2"] = process_appendix_2_flat(model, text_t2)

        if raw_t4:
            st.write("正在处理支撑矩阵表...")
            for i in range(0, len(raw_t4), 100):
                chunk = raw_t4[i : i+100]
                prompt = f"提取支撑关系 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
                res = ai_safe_call(model, prompt)
                if isinstance(res, list): results["tables"]["4"].extend(res)

    return results

# ============================================================
# 4. 界面渲染 (带唯一 Key 修复)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件工作台 v1.1")
    
    if "data_v11" not in st.session_state:
        st.session_state.data_v11 = None

    with st.sidebar:
        st.title("⚙️ 配置")
        api_key = st.text_input("Gemini API Key", type="password", key="v11_key")
    
    st.markdown("## 🧠 培养方案全量智能提取 (修复版)")
    file = st.file_uploader("上传 PDF", type="pdf", key="v11_uploader")

    if file and api_key:
        if st.button("🚀 执行全量抽取", type="primary", key="v11_run"):
            data = parse_document_v11(api_key, file.getvalue())
            if data:
                st.session_state.data_v11 = data
                st.success("抽取完成！")

    if st.session_state.data_v11:
        d = st.session_state.data_v11
        tab1, tab2, tab3, tab4 = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with tab1:
            sec_pick = st.selectbox("选择栏目", ["1","2","3","4","5","6"], key="v11_sec_sel")
            # 解决截图 2/3 中的显示问题
            content = d["sections"].get(sec_pick, "未提取到相关正文。请检查 PDF 前 5 页是否存在对应标题。")
            st.text_area("提取结果", value=content, height=450, key="v11_text_area")

        with tab2:
            df1 = pd.DataFrame(d["tables"]["1"])
            if not df1.empty:
                st.data_editor(df1.reindex(columns=TABLE_1_FULL_COLS), use_container_width=True, key="v11_ed1")
            
        with tab3:
            # 解决截图 4 中的 JSON 显示问题
            df2 = pd.DataFrame(d["tables"]["2"])
            if not df2.empty:
                st.markdown("### 学分统计明细")
                st.table(df2) # 使用 table 或 dataframe 展示扁平化数据
            else:
                st.info("学分表抽取失败，可能是 PDF 该页文本解析异常。")
            
        with tab4:
            st.dataframe(pd.DataFrame(d["tables"]["4"]), use_container_width=True, key="v11_ed4")

if __name__ == "__main__":
    main()