# app.py - Teaching Agent Suite (AI Optimized Version)
from __future__ import annotations

import io
import re
import json
import time
import hashlib
import base64
import datetime as _dt
from pathlib import Path
from decimal import Decimal
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any

import pandas as pd
import streamlit as st
import pdfplumber
import google.generativeai as genai

# ============================================================
# 1. 配置与常量定义
# ============================================================
TABLE_1_FULL_COLS = [
    "课程体系", "课程编码", "课程名称", "开课模式", "考核方式", 
    "学分", "总学时", "内_讲课", "内_实验", "内_上机", "内_实践", 
    "外_学分", "外_学时", "上课学期", "专业方向", "学位课", "备注"
]

@dataclass
class Project:
    project_id: str
    name: str
    updated_at: str

# ============================================================
# 2. 通用工具函数
# ============================================================
def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def _short_id(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:10]

def _compact_lines(s: str) -> str:
    s = (s or "").replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _read_pdf_pages_text(pdf_bytes: bytes) -> List[str]:
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            pages.append(_compact_lines(p.extract_text() or ""))
    return pages

def payload_to_jsonable(obj):
    """递归处理不可序列化对象，用于 JSON 下载 [cite: 1]"""
    if isinstance(obj, pd.DataFrame):
        return obj.fillna("").to_dict(orient="records")
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): payload_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [payload_to_jsonable(x) for x in obj]
    if isinstance(obj, (Decimal, Path)):
        return str(obj)
    return obj

# ============================================================
# 3. AI 核心处理模块 (Gemini)
# ============================================================
def configure_ai(api_key: str):
    genai.configure(api_key=api_key)
    # 使用最新的稳定模型 [cite: 106]
    return genai.GenerativeModel('gemini-2.5-flash')

def ai_query_json(model, prompt: str) -> Any:
    """强制要求 AI 返回结构化 JSON [cite: 108, 120]"""
    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"AI 解析出错: {e}")
        return {}

def process_complex_table(model, raw_rows: List[List[str]], table_type: str):
    """专门处理附表 1-4 的复杂逻辑 [cite: 105, 129]"""
    if table_type == "1":
        prompt = f"提取教学计划表。必须映射到列：{TABLE_1_FULL_COLS}。识别学位课√并拆分课内/课外学时。"
    elif table_type == "2":
        prompt = "提取学分统计表。字段：[体系, 必修学分, 选修学分, 合计, 比例]。"
    else:
        prompt = "提取课程对毕业要求的支撑强度(H/M/L)。字段：[课程名称, 指标点, 强度]。"
    
    return ai_query_json(model, f"{prompt}\n数据：{json.dumps(raw_rows, ensure_ascii=False)}")

def parse_full_document(api_key, pdf_bytes):
    """主解析流程：分段正文抽取 + 自动附表路由 """
    model = configure_ai(api_key)
    results = {"sections": {}, "tables": {"1": [], "2": [], "4": []}}
    
    pages_text = _read_pdf_pages_text(pdf_bytes)
    
    # 1. 正文抽取 (前 6 页)
    header_text = "\n".join(pages_text[:6])
    sec_prompt = "提取 1-6 项正文：1.培养目标, 2.毕业要求, 3.专业定位, 4.主干学科, 5.学制, 6.毕业条件。返回 JSON。"
    results["sections"] = ai_query_json(model, f"{sec_prompt}\n内容：{header_text}")

    # 2. 附表动态扫描
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").lower()
            table = page.extract_table()
            if not table: continue

            target_type = None
            if "附表1" in text or "计划表" in text: target_type = "1"
            elif "附表2" in text or "学分统计" in text: target_type = "2"
            elif "附表4" in text or "支撑关系" in text: target_type = "4"

            if target_type:
                st.write(f"正在深度解析附表 {target_type} (第 {i+1} 页)...")
                # 过滤空行并处理
                clean_rows = [r for r in table if any(r)]
                res = process_complex_table(model, clean_rows, target_type)
                if isinstance(res, list): results["tables"][target_type].extend(res)

    return results

# ============================================================
# 4. Streamlit UI 渲染
# ============================================================
def ui_init_state():
    if "projects" not in st.session_state:
        pid = _short_id(_now_str())
        st.session_state.projects = [Project(pid, f"默认项目-{time.strftime('%Y%m%d')}", _now_str())]
        st.session_state.active_project_id = pid
    if "all_data" not in st.session_state:
        st.session_state.all_data = None

def main():
    st.set_page_config(layout="wide", page_title="Teaching Agent Suite AI", page_icon="🧠")
    ui_init_state()

    # --- 侧边栏 ---
    with st.sidebar:
        st.title("⚙️ 配置中心")
        api_key = st.text_input("Gemini API Key", type="password", key="gemini_key_input")
        
        st.divider()
        st.markdown("### 项目管理")
        labels = {p.project_id: p.name for p in st.session_state.projects}
        st.selectbox("切换项目", options=list(labels.keys()), format_func=lambda x: labels[x], key="prj_select")
        
        st.caption("v0.8.2 - AI 全量结构化抽取")

    # --- 主界面 ---
    st.markdown("""
    <div style="background:#f0f4ff; padding:20px; border-radius:15px; border-left:5px solid #2f6fed;">
        <h2 style="margin:0;">教学文件智能工作台</h2>
        <p style="color:#666;">利用 Gemini 1.5 Flash 深度理解培养方案，自动填充 1-11 项及各附表 [cite: 135, 210]。</p>
    </div>
    """, unsafe_allow_html=True)

    file = st.file_uploader("上传培养方案 PDF", type="pdf", key="main_uploader")

    if file and api_key:
        if st.button("🚀 开始全量智能抽取", type="primary", use_container_width=True):
            with st.spinner("AI 正在扫描文档并解析复杂表格..."):
                data = parse_full_document(api_key, file.getvalue())
                st.session_state.all_data = data
                st.success("抽取完成！")

    # --- 结果展示 ---
    if st.session_state.all_data:
        d = st.session_state.all_data
        tabs = st.tabs(["1-11正文", "附表1:计划表", "附表2:学分统计", "附表4:支撑关系", "调试/导出"])
        
        with tabs[0]:
            sec = st.radio("栏目选择", ["1","2","3","4","5","6"], horizontal=True, key="sec_nav")
            st.text_area("提取结果", value=d["sections"].get(sec, ""), height=300, key=f"text_{sec}")
            
        with tabs[1]:
            df1 = pd.DataFrame(d["tables"]["1"])
            if not df1.empty:
                df1 = df1.reindex(columns=TABLE_1_FULL_COLS)
                st.data_editor(df1, use_container_width=True, key="editor_t1")
            else:
                st.info("未发现附表 1 数据 [cite: 105, 107]。")
                
        with tabs[2]:
            st.table(pd.DataFrame(d["tables"]["2"]))
            
        with tabs[3]:
            st.dataframe(pd.DataFrame(d["tables"]["4"]), use_container_width=True)

        with tabs[4]:
            col1, col2 = st.columns(2)
            with col1:
                st.download_button("下载基座 JSON", 
                                 data=json.dumps(payload_to_jsonable(d), ensure_ascii=False),
                                 file_name="base_plan.json", mime="application/json")
            with col2:
                if st.button("清理当前缓存"):
                    st.session_state.all_data = None
                    st.rerun()

if __name__ == "__main__":
    main()