import io, json, time, random, re
import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai
from google.api_core import exceptions
from typing import Dict, List, Any

# ============================================================
# 1. 常量定义
# ============================================================
TABLE_1_FULL_COLS = [
    "课程体系", "课程编码", "课程名称", "开课模式", "考核方式", 
    "学分", "总学时", "内_讲课", "内_实验", "内_上机", "内_实践", 
    "外_学分", "外_学时", "上课学期", "专业方向", "学位课", "备注"
]

# ============================================================
# 2. AI 处理逻辑
# ============================================================
def ai_safe_call(model, prompt: str, max_retries=3):
    """带冷却的 AI 调用，确保 RPM 限制"""
    for i in range(max_retries):
        try:
            time.sleep(5)  # 强制 5 秒冷却，适配免费版限制
            response = model.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            )
            clean_text = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(clean_text)
        except exceptions.ResourceExhausted:
            time.sleep(10 * (i + 1))
        except Exception:
            continue
    return None

def extract_sections_precise(model, full_text):
    """强化 1-6 项定位，确保 4/5/6 不被遗漏 """
    prompt = f"""
    提取培养方案正文 1-6 项。
    1: 培养目标 (一、培养目标 之后)
    2: 毕业要求 (二、毕业要求 之后)
    3: 专业定位与特色 (三、专业定位与特色 之后)
    4: 主干学科/核心课程/实践环节 (四、主干学科 之后)
    5: 标准学制与授予学位 (五、标准学制 之后)
    6: 毕业条件 (六、毕业条件 之后)
    
    返回 JSON: {{"1": "...", "2": "...", "3": "...", "4": "...", "5": "...", "6": "..."}}
    文本：{full_text[:18000]}
    """
    return ai_safe_call(model, prompt)

def process_table_2_flat(model, raw_text):
    """强制展平学分表，防止出现截图中的嵌套 JSON """
    prompt = f"""
    将学分统计文本转换为 JSON 列表。
    必须识别“焊接”和“无损检测”两个专业方向。
    每行必须是简单的键值对，严禁嵌套。
    字段：["专业方向", "课程体系", "学分合计", "比例", "备注"]
    文本：{raw_text}
    """
    return ai_safe_call(model, prompt)

# ============================================================
# 3. 解析引擎
# ============================================================
def parse_document_v12(api_key, pdf_bytes):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = [p.extract_text() or "" for p in pdf.pages]
        
        # 1. 提取正文
        st.write("正在智能提取正文 1-6 项...")
        results["sections"] = extract_sections_precise(model, "\n".join(all_pages[:6]))

        # 2. 扫描附表页
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

        # 3. 结构化处理
        if raw_t1:
            st.write("校对教学计划表中...")
            # 分块逻辑省略，同前...
        if text_t2:
            st.write("重构学分统计表中...")
            results["tables"]["2"] = process_table_2_flat(model, text_t2)
        if raw_t4:
            st.write("校对支撑矩阵中...")
            # 分块逻辑省略，同前...

    return results

# ============================================================
# 4. UI 渲染 (核心修复点)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件工作台 v1.2")
    
    if "data_v12" not in st.session_state:
        st.session_state.data_v12 = None

    with st.sidebar:
        st.title("⚙️ 配置")
        api_key = st.text_input("Gemini API Key", type="password", key="v12_key")
    
    file = st.file_uploader("上传 2024培养方案.pdf", type="pdf")

    if file and api_key and st.button("🚀 执行全量抽取", key="v12_run"):
        st.session_state.data_v12 = parse_document_v12(api_key, file.getvalue())

    if st.session_state.data_v12:
        d = st.session_state.data_v12
        tab1, tab2, tab3, tab4 = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计矩阵", "附表4: 支撑矩阵"])
        
        with tab1:
            # 修复切换问题的关键：
            sec_pick = st.selectbox("选择栏目", ["1","2","3","4","5","6"], key="v12_sec_select")
            content = d["sections"].get(sec_pick, "未提取到正文")
            
            # 使用带 sec_pick 的 key 强制刷新组件状态
            st.text_area("提取结果", value=content, height=450, key=f"v12_ta_{sec_pick}")

        with tab3:
            st.markdown("### 学分统计明细 (已修复 JSON 嵌套)")
            df2 = pd.DataFrame(d["tables"]["2"])
            if not df2.empty:
                st.dataframe(df2, use_container_width=True)
            else:
                st.info("该表为空，请重新执行抽取")

if __name__ == "__main__":
    main()