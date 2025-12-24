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
# 2. AI 处理引擎 (大幅减少请求频率)
# ============================================================
def ai_safe_call(model, prompt: str, max_retries=5):
    """带指数退避和更长冷却的 AI 调用"""
    for i in range(max_retries):
        try:
            # 免费版 Gemini 2.5 Flash 限制为 15 RPM
            # 增加基础冷却时间到 6 秒，确保每分钟请求不超过 10 次
            time.sleep(6) 
            response = model.generate_content(
                prompt, 
                generation_config={"response_mime_type": "application/json"}
            )
            clean_text = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(clean_text)
        except exceptions.ResourceExhausted:
            # 如果耗尽配额，等待时间翻倍：15s, 30s, 60s...
            wait_time = (i + 1) * 15 
            st.warning(f"⚠️ 触发 API 配额限制，正在尝试第 {i+1} 次重试，需等待 {wait_time} 秒...")
            time.sleep(wait_time)
        except Exception as e:
            if i == max_retries - 1:
                st.error(f"❌ AI 调用失败: {str(e)}")
            continue
    return None

def extract_sections_precise(model, full_text):
    """提取正文 1-6 项，保持键名与 UI 一致 """
    prompt = f"""
    提取培养方案正文 1-6 项。内容必须包含各标题下的详细文字说明：
    1培养目标: [cite: 10] 之后的正文内容
    2毕业要求: [cite: 21] 之后的正文内容
    3专业定位与特色: [cite: 80] 之后的正文内容
    4主干学科/核心课程/实践环节: [cite: 84] 之后的正文内容
    5标准学制与授予学位: [cite: 88] 之后的正文内容
    6毕业条件: [cite: 91] 之后的正文内容
    
    返回 JSON 键名必须精确为: {{"1培养目标": "...", "2毕业要求": "...", "3专业定位与特色": "...", "4主干学科/核心课程/实践环节": "...", "5标准学制与授予学位": "...", "6毕业条件": "..."}}
    文本：{full_text[:18000]}
    """
    return ai_safe_call(model, prompt)

def process_table_2_flat(model, raw_text):
    """深度扁平化处理学分表 """
    prompt = f"""
    将以下学分统计表内容转换为扁平的 JSON 列表。
    必须识别“焊接”和“无损检测”两个专业方向的行。
    字段：["专业方向", "课程体系", "学分统计", "学分比例", "备注"]
    不要在单元格内嵌套 JSON 对象或字典，必须全部转换为字符串。
    文本内容：{raw_text}
    """
    return ai_safe_call(model, prompt)

# ============================================================
# 3. 文档解析引擎 (优化分块大小)
# ============================================================
def parse_document_stable(api_key, pdf_bytes):
    genai.configure(api_key=api_key)
    # 强制使用 2.5-flash，Pro 的 RPM 限制（2次/分）无法完成此任务
    model = genai.GenerativeModel('gemini-2.5-flash')
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_text = [p.extract_text() or "" for p in pdf.pages]
        
        # 1. 提取正文
        st.status("正在分析正文 (1-6项)...", state="running")
        res_sec = extract_sections_precise(model, "\n".join(all_text[:6]))
        if res_sec: results["sections"] = res_sec

        # 2. 扫描并分流原始数据
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

        # 3. 大块处理数据 (减少请求总数)
        if raw_t1:
            st.status(f"正在校对教学计划表 (共 {len(raw_t1)} 行)...", state="running")
            # Flash 窗口大，单次处理 150 行减少请求次数
            for i in range(0, len(raw_t1), 150):
                chunk = raw_t1[i : i+150]
                prompt = f"将以下教学计划表数据转为 JSON 列表。字段：{TABLE_1_FULL_COLS}。数据：{json.dumps(chunk, ensure_ascii=False)}"
                res = ai_safe_call(model, prompt)
                if isinstance(res, list): results["tables"]["1"].extend(res)
            
        if text_t2:
            st.status("正在重构学分统计表...", state="running")
            res_t2 = process_table_2_flat(model, text_t2)
            if res_t2: results["tables"]["2"] = res_t2
            
        if raw_t4:
            st.status(f"正在处理支撑矩阵表 (共 {len(raw_t4)} 行)...", state="running")
            # 单次处理 200 行，减少请求总数至 1-2 次
            for i in range(0, len(raw_t4), 200):
                chunk = raw_t4[i : i+200]
                prompt = f"提取支撑关系 JSON 列表 [课程名称, 指标点, 强度]。数据：{json.dumps(chunk, ensure_ascii=False)}"
                res = ai_safe_call(model, prompt)
                if isinstance(res, list): results["tables"]["4"].extend(res)

    return results

# ============================================================
# 4. UI 逻辑 (修复组件刷新问题)
# ============================================================
def main():
    st.set_page_config(layout="wide", page_title="教学文件工作台 v1.2.5")
    
    if "data_final" not in st.session_state:
        st.session_state.data_final = None

    with st.sidebar:
        st.title("⚙️ 设置")
        api_key = st.text_input("Gemini API Key", type="password", key="final_api_key")
        st.info("免费版配额有限，程序已开启自动流控，请勿频繁点击。")
    
    st.markdown("## 🧠 培养方案全量智能提取 (稳定修复版)")
    file = st.file_uploader("上传培养方案 PDF", type="pdf", key="final_uploader")

    if file and api_key and st.button("🚀 执行全量抽取", type="primary", key="final_run"):
        data = parse_document_stable(api_key, file.getvalue())
        if data:
            st.session_state.data_final = data
            st.success("🎉 抽取成功！")

    if st.session_state.data_final:
        d = st.session_state.data_final
        tab1, tab2, tab3, tab4 = st.tabs(["1-6 正文", "附表1: 计划表", "附表2: 学分统计", "附表4: 支撑矩阵"])
        
        with tab1:
            sections_list = ["1培养目标", "2毕业要求", "3专业定位与特色", "4主干学科/核心课程/实践环节", "5标准学制与授予学位", "6毕业条件"]
            sec_pick = st.selectbox("查看栏目内容", sections_list, key="final_sec_pick")
            
            # 使用动态 key 强制内容随 selectbox 变化而刷新
            content = d["sections"].get(sec_pick, "⚠️ 未提取到内容，可能受配额限制影响，请尝试重新抽取。")
            st.text_area("提取结果", value=content, height=450, key=f"final_ta_{sec_pick}")

        with tab2:
            df1 = pd.DataFrame(d["tables"]["1"])
            if not df1.empty:
                st.data_editor(df1.reindex(columns=TABLE_1_FULL_COLS), use_container_width=True, key="final_ed1")

        with tab3:
            st.markdown("### 学分统计明细")
            df2 = pd.DataFrame(d["tables"]["2"])
            if not df2.empty:
                st.dataframe(df2, use_container_width=True, key="final_df2")

        with tab4:
            st.markdown("### 课程设置对毕业要求达成支撑关系表")
            st.dataframe(pd.DataFrame(d["tables"]["4"]), use_container_width=True, key="final_df4")

if __name__ == "__main__":
    main()