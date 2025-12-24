# app.py - 完整集成 LLM 全量解析与 Key 轮换版本
from __future__ import annotations

import io
import os
import re
import json
import uuid
import zipfile
import hashlib
import time
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
import pandas as pd
import requests
import streamlit.components.v1 as components
import pdfplumber
import google.generativeai as genai
from openai import OpenAI

# ---- Word 导出支持 ----
try:
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except Exception:
    Document = None

# ============================================================
# 1. 配置与全局常量
# ============================================================
APP_NAME = "Teaching Agent Suite"
APP_VERSION = "v0.7 (LLM-Mega-Extraction)"
DATA_ROOT = Path("data/projects")

PROVIDERS = {
    "Gemini (Google)": {"base_url": None, "model": "gemini-1.5-flash"},
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "Kimi (Moonshot)": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "智谱 AI (GLM)": {"base_url": "https://open.bigmodel.cn/api/paas/v4/", "model": "glm-4"},
    "零一万物 (Yi)": {"base_url": "https://api.lingyiwanwu.com/v1", "model": "yi-34b-chat-0205"},
    "通义千问 (Qwen)": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "豆包 (字节)": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-pro-32k"}
}

SECTION_TITLES = [
    "一、培养目标", "二、毕业要求", "三、专业定位与特色",
    "四、主干学科、专业核心课程和主要实践性教学环节",
    "五、标准学制与授予学位", "六、毕业条件",
    "七、专业教学计划表", "八、学分统计表", "九、教学进程表",
    "十、课程设置对毕业要求支撑关系表", "十一、课程设置逻辑思维导图",
]

# 映射：大模型 JSON 字段 -> UI 标准标题
LLM_TO_STANDARD_MAP = {
    "1培养目标": "一、培养目标",
    "2毕业要求": "二、毕业要求",
    "3专业定位与特色": "三、专业定位与特色",
    "4主干学科/核心课程/实践环节": "四、主干学科、专业核心课程和主要实践性教学环节",
    "5标准学制与授予学位": "五、标准学制与授予学位",
    "6毕业条件": "六、毕业条件",
}

# ============================================================
# 2. LLM 核心路由与 Key 轮换
# ============================================================

def call_llm_core(provider_name, api_key, prompt):
    """底层的 API 调用"""
    config = PROVIDERS[provider_name]
    if "Gemini" in provider_name:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(config["model"])
        response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
        return json.loads(response.text)
    else:
        client = OpenAI(api_key=api_key, base_url=config["base_url"])
        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": "你是一个只输出 JSON 的教务专家助手。"},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)

def call_llm_with_retry_and_rotation(provider_name, user_api_key, prompt):
    """带轮换和重试逻辑的路由"""
    all_keys = st.secrets.get("GEMINI_KEYS", [])
    if "Gemini" not in provider_name or user_api_key:
        target_key = user_api_key if user_api_key else st.secrets.get("GEMINI_API_KEY", "")
        return call_llm_core(provider_name, target_key, prompt)

    if not all_keys:
        raise Exception("未在 Secrets 中配置 GEMINI_KEYS 列表")

    if "api_key_index" not in st.session_state:
        st.session_state.api_key_index = 0

    start_idx = st.session_state.api_key_index % len(all_keys)
    for i in range(len(all_keys)):
        curr_idx = (start_idx + i) % len(all_keys)
        curr_key = all_keys[curr_idx]
        st.session_state.api_key_index = curr_idx
        try:
            st.write(f"正在尝试使用 Key #{curr_idx + 1}...")
            result = call_llm_core(provider_name, curr_key, prompt)
            st.session_state.api_key_index = (curr_idx + 1) % len(all_keys)
            return result
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["429", "quota", "limit"]):
                st.warning(f"⚠️ Key #{curr_idx + 1} 配额耗尽，尝试切换...")
                continue
            raise e
    raise Exception("❌ 所有配置的 Key 均已失效或超限。")

# ============================================================
# 3. 培养方案全量解析引擎
# ============================================================

MEGA_PROMPT = """你是一个专业的高校教务专家。请深度阅读提供的的培养方案文本，并按照以下要求精确提取信息。

### 提取要求：
1. **分条列出**：毕业要求等子项必须保留原始编号，使用 Markdown 列表。
2. **完整性**：必须包含所有细分条款（如具体的学分数值）。
3. **表格精度**：
   - 附表 1：(教学计划表) 提取所有课程，保留学位课标记。
   - 附表 2：(学分统计) 区分不同专业方向。
   - 附表 4：(支撑矩阵) 提取 H/M/L 强度。

### 输出格式：
必须输出如下 JSON：
{
  "sections": {
    "1培养目标": "...", "2毕业要求": "...", "3专业定位与特色": "...",
    "4主干学科/核心课程/实践环节": "...", "5标准学制与授予学位": "...", "6毕业条件": "..."
  },
  "table1": [{"课程名称": "...", "课内学分": "...", ...}],
  "table2": [...], "table4": [...]
}"""

def base_plan_llm_mega_parse(pdf_bytes, provider_name, api_key):
    """带进度显示的 AI 全量解析"""
    with st.status(f"🚀 正在通过 {provider_name} 解析培养方案...", expanded=True) as status:
        try:
            st.write("🔍 正在提取 PDF 文本...")
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                all_text = "\n".join([p.extract_text() or "" for p in pdf.pages])
            
            st.write(f"📑 正在发送 AI 抽取请求 (内容长度: {len(all_text)})...")
            prompt = f"{MEGA_PROMPT}\n\n培养方案原文：\n{all_text}"
            
            start_time = time.time()
            raw_result = call_llm_with_retry_and_rotation(provider_name, api_key, prompt)
            
            # 格式转换映射
            standard_sections = {}
            llm_sections = raw_result.get("sections", {})
            for l_key, s_key in LLM_TO_STANDARD_MAP.items():
                standard_sections[s_key] = llm_sections.get(l_key, "")
            
            append_tables = {
                "七、专业教学计划表": raw_result.get("table1", []),
                "八、学分统计表": raw_result.get("table2", []),
                "九、教学进程表": [], 
                "十、课程设置对毕业要求支撑关系表": raw_result.get("table4", [])
            }

            status.update(label="✅ 解析成功！", state="complete", expanded=False)
            return {
                "meta": {"sha256": hashlib.sha256(pdf_bytes).hexdigest(), "rev": int(time.time()), "provider": provider_name},
                "sections": standard_sections,
                "appendices": {"tables": append_tables},
                "course_graph": {"nodes": [], "edges": []},
                "raw_pages_text": [all_text]
            }
        except Exception as e:
            status.update(label="❌ 解析失败", state="error", expanded=True)
            st.error(str(e))
            return None

# ============================================================
# 4. Persistence & Utilities (保持原有逻辑)
# ============================================================

def safe_json_load(s: str, default: Any = None) -> Any:
    try: return json.loads(s)
    except: return default

def ensure_dir(p: Path): p.mkdir(parents=True, exist_ok=True)

@dataclass
class Project:
    project_id: str; name: str; llm: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""; logo_file: str = ""

def load_base_plan(pid: str) -> Dict[str, Any]:
    p = DATA_ROOT / pid / "base_training_plan.json"
    return safe_json_load(p.read_text("utf-8"), {}) if p.exists() else {}

def save_base_plan(pid: str, plan: Dict[str, Any]):
    ensure_dir(DATA_ROOT / pid)
    (DATA_ROOT / pid / "base_training_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")

# ============================================================
# 5. UI 部分
# ============================================================

def ui_base_training_plan(pid: str):
    st.subheader("培养方案基座 (LLM 全量解析版)")
    plan = load_base_plan(pid)
    rev = plan.get("meta", {}).get("rev", 0)

    colL, colR = st.columns([1, 2])
    with colL:
        provider = st.selectbox("选择解析模型", list(PROVIDERS.keys()))
        api_key = st.text_input("手动 API Key (可选)", type="password")
        up = st.file_uploader("上传 PDF 培养方案", type=["pdf"])
        
        if up and st.button("🚀 执行全量 AI 抽取", type="primary", use_container_width=True):
            res = base_plan_llm_mega_parse(up.read(), provider, api_key)
            if res:
                save_base_plan(pid, res)
                st.rerun()

    with colR:
        if not plan: st.info("请先上传并解析培养方案。"); return
        
        tabs = st.tabs(SECTION_TITLES)
        sections = plan.get("sections", {})
        append_tables = plan.get("appendices", {}).get("tables", {})

        for i, title in enumerate(SECTION_TITLES[:6]):
            with tabs[i]:
                st.text_area(title, value=sections.get(title, ""), height=300, key=f"txt_{rev}_{i}")

        for j, title in enumerate(SECTION_TITLES[6:10], start=6):
            with tabs[j]:
                df = pd.DataFrame(append_tables.get(title, []))
                st.data_editor(df, num_rows="dynamic", use_container_width=True, key=f"edt_{rev}_{j}")

def main():
    st.set_page_config(layout="wide", page_title=APP_NAME)
    ensure_dir(DATA_ROOT)
    
    # 简单的项目初始化
    pid = "default_project"
    if not (DATA_ROOT / pid).exists(): ensure_dir(DATA_ROOT / pid)

    st.title(f"🧠 {APP_NAME} {APP_VERSION}")
    
    tab_base, tab_docs = st.tabs(["培养方案基座", "教学文件管理"])
    with tab_base: ui_base_training_plan(pid)
    with tab_docs: st.info("教学文件管理模块已就绪，正在同步基座数据...")

if __name__ == "__main__":
    main()