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
# 2. AI 处理引擎 (增强鲁棒性)
# ============================================================
def ai_safe_call(model, prompt: str, max_retries=3):
    """带冷却和重试的 AI 调用，确保 RPM 限制"""
    for i in range(max_retries):
        try:
            time.sleep(5)  # 强制冷却，适配免费版 RPM 限制
            response = model.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            )
            clean_text = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(clean_text)
        except exceptions.ResourceExhausted:
            st.warning(f"触发配额限制，正在第 {i+1} 次重试...")
            time.sleep(10 * (i + 1))
        except Exception as e:
            continue
    return None

def extract_sections_precise(model, full_text):
    """强化 1-6 项定位，确保 4/5/6 不被遗漏 [cite: 10-91]"""
    prompt = f"""
    提取培养方案正文 1-6 项。确保提取内容完整：
    1: 培养目标 (一、培养目标 之后) [cite: 10]
    2: 毕业要求 (二、毕业要求 之后) [cite: 21]
    3: 专业定位与特色 (三、专业定位与特色 之后) [cite: 80]
    4: 主干学科/核心课程/实践环节 (四、主干学科 之后) [cite: 84]
    5: 标准学制与授予学位 (五、标准学制 之后) [cite: 88]
    6: 毕业条件 (六、毕业条件 之后) [cite: 91]
    
    返回 JSON: {{"1培养目标": "...", "2毕业要求": "...", "3专业定位与特色": "...", "4主干学科/核心课程/实践环节": "...", "5标准学制与授予学位": "...", "6毕业条件": "..."}}
    文本：{full_text[:18000]}
    """
    return ai_safe_call(model, prompt)

def process_table_2_flat(model, raw_text):
    """深度扁平化处理学分表，识别不同专业方向 """
    prompt = f"""
    将学分统计文本转换为扁平的 JSON 列表。
    必须识别“焊接”和“无损检测”两个专业方向的差异。
    字段：["专业方向", "课程体系", "学分统计", "学分比例", "备注"]
    文本：{raw_text}
    """
    return ai_safe_call(model, prompt)

# ============================================================
# 3. 文档解析引擎 (修正 NameError)
# ============================================================
def parse_document_v12_1(api_key, pdf_bytes):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # 正确获取全文文本
        all_text = [p.extract_text() or "" for p in pdf.pages]
        
        # 1. 提取正文 (修正 all_pages 为 all_text)
        st.write("正在智能分析培养方案正文 (1-6项)...")
        results["sections"] = extract_sections_precise(model, "\n".join(all_text[:6]))

        # 2. 全量扫描附表页
        raw_t1, raw_t4, text_t2 = [], [], ""
        for i, page in enumerate(pdf.pages):
            txt = all_text[i]
            # 定位附表1
            if "附表1" in txt or "教学计划表" in txt:
                tbl = page.extract_table()
                if tbl: raw_t1.extend(tbl[1:])
            # 定位附表2 [cite: 113, 119]
            elif "附表2" in txt or "学分统计" in txt:
                text_t2 += f"\n{txt}"
            # 定位附表4 [cite: 124, 128]
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
            st.write("正在重构附表2: 学分统计表...")
            results["tables"]["2"] = process_table_2_flat(model, text_t2)
            
        if raw_t4:
            st.write("正在处理支撑矩阵表...")
            for i in range(0, len(raw_t4), 100):
                chunk = raw_t4[i : i+100]
                prompt = f"提取支撑关系 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
                res = ai_safe_call(model, prompt)
                if isinstance(res, list): results["tables"]["4"].extend(res)

    return results

# ============================================================
# 4. Streamlit UI 逻辑
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件工作台 v1.2.1")
    
    if "data_v121" not in st.session_state:
        st.session_state.data_v121 = None

    with st.sidebar:
        st.title("⚙️ 设置")
        api_key = st.text_input("Gemini API Key", type="password", key="v121_api_key")
        st.caption("版本: v1.2.1 (修复 NameError)")
        
    st.markdown("## 🧠 培养方案全量智能提取 (修复版)")
    
    file = st.file_uploader("上传培养方案 PDF", type="pdf", key="v121_uploader")

    if file and api_key:
        if st.button("🚀 执行一键全量抽取", type="primary", key="v121_run"):
            with st.spinner("AI 正在深度解析文档..."):
                data = parse_document_v12_1(api_key, file.getvalue())
                if data:
                    st.session_state.data_v121 = data
                    st.success("抽取任务已完成！")

    if st.session_state.data_v121:
        d = st.session_state.data_v121
        tab1, tab2, tab3, tab4 = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with tab1:
            # 解决切换问题的关键：使用带有 sec_pick 的 key
            sec_pick = st.selectbox("查看栏目内容", ["1培养目标", "2毕业要求", "3专业定位与特色", "4主干学科/核心课程/实践环节", "5标准学制与授予学位", "6毕业条件"], key="v121_sec_pick")
            content = d["sections"].get(sec_pick, "未提取到相关正文。")
            st.text_area("提取结果", value=content, height=450, key=f"v121_ta_{sec_pick}")

        with tab2:
            df1 = pd.DataFrame(d["tables"]["1"])
            if not df1.empty:
                st.data_editor(df1.reindex(columns=TABLE_1_FULL_COLS), use_container_width=True, key="v121_ed1")

        with tab3:
            st.markdown("### 学分统计明细 ")
            df2 = pd.DataFrame(d["tables"]["2"])
            if not df2.empty:
                st.dataframe(df2, use_container_width=True, key="v121_df2")
            else:
                st.info("暂无学分统计数据。")

        with tab4:
            st.markdown("### 课程设置对毕业要求达成支撑关系表 [cite: 124, 128]")
            st.dataframe(pd.DataFrame(d["tables"]["4"]), use_container_width=True, key="v121_df4")

if __name__ == "__main__":
    main()