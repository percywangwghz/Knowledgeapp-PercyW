"""
知识库前端主应用 - Streamlit
面向结构化知识库
单文件版本，无外部依赖
"""
import os
import sys
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import llm
from battle import render_battle
from ingest import render_ingest
from mdblocks import wrap_ascii_tables
from review import render_review
from radar import render_radar
from tracks import STATUS_BUCKETS, get_track, normalize_status

# 前端注入：侧边栏填入的 API Key 存于 session_state，llm 调用时优先取用。
# 填一次即持久化到本机（LOCAL_KEY_FILE），启动时自动载入，之后无需再填。
def _frontend_api_key():
    """返回当前应使用的 API Key（空串 = 未配置，AI 功能不可用）。"""
    return st.session_state.get("user_api_key", "").strip()


llm.register_key_provider(_frontend_api_key)

# ==================== 配置区域 ====================

# 知识库根目录：优先环境变量，其次仓库内置 knowledge/，最后 ~/.kimi/knowledge（与 config.py 一致）
_REPO_KB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
_USER_KB = os.path.join(os.path.expanduser("~"), ".kimi", "knowledge")
KNOWLEDGE_DIR = os.environ.get(
    "KB_KNOWLEDGE_DIR",
    _REPO_KB if os.path.isdir(_REPO_KB)
    else (_USER_KB if os.path.isdir(_USER_KB) else _REPO_KB))

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
INDEX_FILE = os.path.join(DATA_DIR, "kb_index.json")

# 本机持久化的 API Key 文件（侧边栏填一次即写入，启动自动载入；
# ⚠️ 外发打包前必须删除此文件，里面是本机填过的 key）
LOCAL_KEY_FILE = os.environ.get("KB_LOCAL_KEY_FILE",
                                os.path.join(DATA_DIR, "local_api_key.txt"))


def _load_local_key():
    try:
        with open(LOCAL_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_local_key(key):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if key:
            with open(LOCAL_KEY_FILE, "w", encoding="utf-8") as f:
                f.write(key)
        elif os.path.exists(LOCAL_KEY_FILE):
            os.remove(LOCAL_KEY_FILE)
    except OSError:
        pass

CATEGORY_MAP = {
    "01_industry": ("行业认知", "🏭", "二级框架：赛道全景、产业链、投资逻辑"),
    "02_deals": ("项目解剖", "🔬", "一级框架：公司深度、投资假设、风险分析"),
    "03_frameworks": ("方法论", "🛠️", "可复用工具：评估框架、分析模板"),
    "04_comparables": ("横向比较", "📊", "项目矩阵、竞品对照"),
    "05_tracking": ("动态追踪", "📈", "追踪表、里程碑监控"),
    "06_strategy": ("投资策略", "🎯", "产业链图谱、主题策略"),
    "07_learnings": ("经验沉淀", "💡", "方法论总结、案例复盘"),
    "08_funds": ("被投基金", "💼", "机构投资认知：GP 方法论、投资逻辑拆解、回报归因"),
    "09_tech": ("技术沉淀", "🧪", "各行业技术档案：技术图谱/原理详解/路线对比，公众号技术提取直接落入"),
}

FILE_TYPES = {
    ".md": "markdown",
    ".txt": "text",
}

FILE_ICONS = {
    "markdown": "📝",
    "text": "📄",
    "unknown": "📎",
}

CATEGORY_COLORS = {
    "行业认知": "#1f77b4",
    "项目解剖": "#ff7f0e",
    "方法论": "#2ca02c",
    "横向比较": "#d62728",
    "动态追踪": "#9467bd",
    "投资策略": "#8c564b",
    "经验沉淀": "#e377c2",
    "被投基金": "#17becf",
    "技术沉淀": "#0aa1dd",
    "其他": "#7f7f7f",
}

# ==================== 索引器区域 ====================

def get_file_type(filename):
    ext = Path(filename).suffix.lower()
    return FILE_TYPES.get(ext, "unknown")


def extract_metadata(content):
    meta = {
        "title": "",
        "subtitle": "",
        "project": "",
        "industry": "",
        "status": "",
        "last_updated": "",
    }
    
    lines = content.split('\n')[:30]
    text = '\n'.join(lines)
    
    title_match = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
    if title_match:
        meta["title"] = title_match.group(1).strip()
    
    subtitle_lines = []
    for line in lines:
        if line.startswith('> ') and not line.startswith('> **'):
            subtitle_lines.append(line[2:].strip())
    if subtitle_lines:
        meta["subtitle"] = ' '.join(subtitle_lines[:2])
    
    project_patterns = [
        r'\*\*Deal Type\*\*.*?\|\s*\*\*([^|*]+)\*\*',
        r'项目[:：]\s*(\S+)',
    ]
    for pattern in project_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            meta["project"] = match.group(1).strip()
            break
    
    status_patterns = [
        r'Status\*{0,2}[:：]\s*([^\n|]+)',
        r'状态\*{0,2}[:：]\s*([^\n|]+)',
    ]
    for pattern in status_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            meta["status"] = match.group(1).strip()
            break
    
    date_patterns = [
        r'Last Updated[:：]\s*(\d{4}-\d{2}-\d{2})',
        r'更新时间[:：]\s*(\d{4}-\d{2}-\d{2})',
        r'更新日期[:：]\s*(\d{4}-\d{2}-\d{2})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            meta["last_updated"] = match.group(1)
            break
    
    return meta


def scan_knowledge_base():
    documents = []
    categories = {}
    
    if not os.path.exists(KNOWLEDGE_DIR):
        print(f"[WARNING] Knowledge dir not found: {KNOWLEDGE_DIR}")
        return documents, categories
    
    for item in sorted(os.listdir(KNOWLEDGE_DIR)):
        item_path = os.path.join(KNOWLEDGE_DIR, item)
        if os.path.isfile(item_path) and item.endswith('.md'):
            try:
                with open(item_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                meta = extract_metadata(content)
                doc = {
                    "name": item,
                    "path": item,
                    "category": "其他",
                    "category_key": "",
                    "type": get_file_type(item),
                    "size": os.path.getsize(item_path),
                    "modified": datetime.fromtimestamp(os.path.getmtime(item_path)).isoformat(),
                    "content": content,
                    **meta,
                }
                doc["status_detail"] = doc["status"]
                doc["status"] = normalize_status(doc["status"])
                doc["track"] = get_track(f"{doc['name']} {doc.get('title', '')}")
                documents.append(doc)
            except Exception:
                continue
    
    for category_key in sorted(os.listdir(KNOWLEDGE_DIR)):
        category_path = os.path.join(KNOWLEDGE_DIR, category_key)
        if not os.path.isdir(category_path):
            continue
        
        category_name, category_icon, category_desc = CATEGORY_MAP.get(
            category_key, ("其他", "📁", "")
        )
        
        category_docs = []
        for filename in sorted(os.listdir(category_path)):
            if not filename.endswith('.md'):
                continue
            
            file_path = os.path.join(category_path, filename)
            rel_path = f"{category_key}/{filename}"
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                meta = extract_metadata(content)
                
                doc = {
                    "name": filename,
                    "path": rel_path,
                    "category": category_name,
                    "category_key": category_key,
                    "category_icon": category_icon,
                    "type": get_file_type(filename),
                    "size": os.path.getsize(file_path),
                    "modified": datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat(),
                    "content": content,
                    **meta,
                }
                doc["status_detail"] = doc["status"]
                doc["status"] = normalize_status(doc["status"])
                doc["track"] = get_track(f"{doc['name']} {doc.get('title', '')}")
                documents.append(doc)
                category_docs.append(doc)
            except Exception:
                continue
        
        if category_docs:
            categories[category_key] = {
                "key": category_key,
                "name": category_name,
                "icon": category_icon,
                "description": category_desc,
                "count": len(category_docs),
                "documents": category_docs,
            }
    
    return documents, categories


def build_index(force=False):
    if not force and os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            print(f"[WARN] 索引文件损坏，重建：{INDEX_FILE}")
    
    print(f"[SCAN] Scanning knowledge base: {KNOWLEDGE_DIR}")
    documents, categories = scan_knowledge_base()
    
    index = {
        "indexed_at": datetime.now().isoformat(),
        "total_documents": len(documents),
        "documents": documents,
        "categories": categories,
    }
    
    os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    
    print(f"[DONE] Index complete: {len(documents)} documents, {len(categories)} categories")
    return index


def load_index():
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            print(f"[WARN] 索引文件损坏，重建：{INDEX_FILE}")
    return build_index(force=True)


def search_documents(index, query):
    if not query:
        return index.get("documents", [])
    
    query = query.lower()
    results = []
    
    for doc in index.get("documents", []):
        score = 0
        
        if query in doc.get("title", "").lower():
            score += 10
        
        if query in doc["name"].lower():
            score += 5
        
        if query in doc.get("content", "").lower():
            score += 3
        
        if query in doc.get("project", "").lower():
            score += 6
        
        if query in doc.get("category", "").lower():
            score += 4
        
        if score > 0:
            results.append((score, doc))
    
    results.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in results]


def get_documents_by_category(index, category_key):
    cat = index.get("categories", {}).get(category_key)
    if cat:
        return cat.get("documents", [])
    return []


def get_document_by_path(index, path):
    for doc in index.get("documents", []):
        if doc["path"] == path:
            return doc
    return None


def get_related_documents(index, doc, limit=5):
    related = []
    
    for other in index.get("documents", []):
        if other["path"] == doc["path"]:
            continue
        
        score = 0
        
        if other.get("category") == doc.get("category"):
            score += 2
        
        if other.get("project") and doc.get("project"):
            if other["project"] == doc["project"]:
                score += 5
        
        doc_words = set(doc.get("content", "").lower().split())
        other_words = set(other.get("content", "").lower().split())
        common_words = doc_words & other_words
        meaningful = {w for w in common_words if len(w) > 4}
        score += len(meaningful) * 0.1
        
        if score > 0:
            related.append((score, other))
    
    related.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in related[:limit]]


# ==================== Streamlit UI ====================

st.set_page_config(
    page_title="一级投研知识库",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": "一级投研知识库 v1.0",
    },
)

if not os.path.isdir(KNOWLEDGE_DIR):
    st.error(
        f"未找到知识库目录：`{KNOWLEDGE_DIR}`\n\n"
        "把知识库文件夹命名为 `knowledge` 放到应用目录下，"
        "或设置环境变量 `KB_KNOWLEDGE_DIR` 指向知识库路径后重启。"
    )
    st.stop()

st.markdown("""
<style>
    /* ---------- 设计 token ---------- */
    :root {
        --kb-bg: #fafaf9;
        --kb-card: #ffffff;
        --kb-text: #1c1c1e;
        --kb-text-2: #6b7280;
        --kb-text-3: #9ca3af;
        --kb-accent: #3b6ea5;
        --kb-accent-soft: #eaf1f8;
        --kb-accent-border: #d7e3f0;
        --kb-border: #e5e3df;
        --kb-radius: 8px;
        --kb-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }

    /* ---------- 全局 ---------- */
    .stApp {
        background-color: var(--kb-bg);
        font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    .main .block-container { padding-top: 2rem; max-width: 1200px; }
    h1, h2, h3, h4, h5, h6 { color: var(--kb-text); font-weight: 650; }
    hr { border-color: var(--kb-border) !important; margin: 1.2rem 0; }

    /* ---------- 顶栏 ---------- */
    .main-header { font-size: 1.6rem; font-weight: 700; color: var(--kb-text); letter-spacing: -0.01em; }
    .sub-header { font-size: 0.85rem; color: var(--kb-text-3); margin-bottom: 1.2rem; }

    /* ---------- 节标题 ---------- */
    .section-header { font-size: 1.1rem; font-weight: 700; color: var(--kb-text); margin: 1.6rem 0 0.8rem 0; }

    /* ---------- 搜索框 ---------- */
    div[data-testid="stTextInput"] input {
        border-radius: var(--kb-radius);
        border: 1px solid var(--kb-border);
        background-color: var(--kb-card);
    }
    div[data-testid="stTextInput"] input:focus {
        border-color: var(--kb-accent);
        box-shadow: 0 0 0 3px rgba(59,110,165,0.15);
    }

    /* ---------- 卡片容器（bordered container 统一卡片化） ---------- */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background-color: var(--kb-card);
        border: 1px solid var(--kb-border) !important;
        border-radius: var(--kb-radius);
        box-shadow: var(--kb-shadow);
        transition: border-color .15s ease, box-shadow .15s ease;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: var(--kb-accent-border) !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    }

    /* ---------- 统计指标 ---------- */
    div[data-testid="stMetricLabel"] { color: var(--kb-text-2); font-size: 0.82rem; }
    div[data-testid="stMetricValue"] { font-size: 1.6rem; font-weight: 700; color: var(--kb-text); }

    /* ---------- 三级按钮（文档标题 / 链接式按钮） ---------- */
    button[kind="tertiary"] {
        justify-content: flex-start;
        text-align: left;
        color: var(--kb-text);
        font-weight: 600;
        padding: 0.15rem 0.25rem;
        white-space: nowrap;
    }
    button[kind="tertiary"] [data-testid="stMarkdownContainer"] { flex: 1 1 auto; text-align: left; }
    button[kind="tertiary"] > div { justify-content: flex-start; width: 100%; }
    button[kind="tertiary"]:hover { color: var(--kb-accent); background: transparent; }
    button[kind="tertiary"]:focus:not(:active) { color: var(--kb-accent); background: transparent; }

    /* ---------- 分类卡片 ---------- */
    .cat-strip { height: 4px; border-radius: var(--kb-radius) var(--kb-radius) 0 0; margin: -1rem -1rem 0.8rem -1rem; }
    .cat-head { font-size: 1.02rem; font-weight: 700; color: var(--kb-text); }
    .cat-count { color: var(--kb-text-3); font-weight: 500; font-size: 0.82rem; margin-left: 0.4rem; }
    .cat-desc { color: var(--kb-text-2); font-size: 0.8rem; margin: 0.15rem 0 0.4rem 0; }

    /* ---------- 首页统计块（单容器 flex 三栏，天然等高对齐） ---------- */
    .stat-flex { display: flex; gap: 2rem; align-items: stretch; }
    .stat-flex > .stat-col { flex: 1 1 0; min-width: 0; }
    .stat-flex > .stat-col.wide { flex: 2 1 0; }
    .stat-flex > .stat-col + .stat-col { border-left: 1px solid var(--kb-border); padding-left: 2rem; }
    .stat-title { font-size: 0.82rem; font-weight: 700; color: var(--kb-text-2); margin-bottom: 0.35rem; }
    .stat-row {
        display: flex; justify-content: space-between; align-items: baseline;
        padding: 0.12rem 0; font-size: 0.85rem; color: var(--kb-text-3);
    }
    .stat-row b { color: var(--kb-text); font-weight: 650; font-size: 0.95rem; font-variant-numeric: tabular-nums; }
    .stat-empty { font-size: 0.8rem; color: var(--kb-text-3); line-height: 1.6; }

    /* ---------- 文档详情 ---------- */
    .doc-title { font-size: 1.7rem; font-weight: 750; color: var(--kb-text); line-height: 1.3; margin-bottom: 0.3rem; }

    /* ---------- 元信息 ---------- */
    .meta-line { color: var(--kb-text-3); font-size: 0.78rem; margin-top: 0.1rem; }

    /* ---------- 侧边栏 ---------- */
    section[data-testid="stSidebar"] { background-color: #f7f6f4; }
    section[data-testid="stSidebar"] button[kind="secondary"] {
        border: none; background: transparent;
        justify-content: flex-start; text-align: left;
        color: var(--kb-text); font-weight: 500;
    }
    section[data-testid="stSidebar"] button[kind="secondary"]:hover { background: #ececea; color: var(--kb-text); }
    section[data-testid="stSidebar"] button[kind="primary"] {
        background: var(--kb-accent-soft); color: var(--kb-accent);
        border: none; justify-content: flex-start; text-align: left; font-weight: 600;
    }
    .sb-section {
        font-size: 0.72rem; font-weight: 700; color: var(--kb-text-3);
        letter-spacing: 0.08em; margin: 0.4rem 0;
    }

    /* ---------- 面包屑 ---------- */
    .crumb-sep { color: var(--kb-text-3); font-size: 0.85rem; padding-top: 0.25rem; text-align: center; }
    .crumb-current {
        color: var(--kb-text-2); font-size: 0.9rem; padding-top: 0.25rem;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }

    /* ---------- 文档目录 TOC ---------- */
    .toc-link {
        display: block; color: var(--kb-text-2); font-size: 0.82rem;
        padding: 0.15rem 0; text-decoration: none;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .toc-link:hover { color: var(--kb-accent); }
    .toc-h3 { padding-left: 1rem; font-size: 0.78rem; color: var(--kb-text-3); }

    /* ---------- 上一篇/下一篇 & 回到顶部 ---------- */
    .pn-label { color: var(--kb-text-3); font-size: 0.78rem; margin-bottom: 0.1rem; }
    .back-top { color: var(--kb-text-3); font-size: 0.82rem; text-decoration: none; }
    .back-top:hover { color: var(--kb-accent); }

    /* ---------- Markdown 正文排版 ---------- */
    .stMarkdown h2 { font-size: 1.35rem; margin-top: 1.6rem; padding-bottom: 0.3rem; border-bottom: 1px solid var(--kb-border); }
    .stMarkdown h3 { font-size: 1.15rem; margin-top: 1.3rem; }
    .stMarkdown p, .stMarkdown li { line-height: 1.75; }
    /* Markdown 表格：表头底色 + 斑马纹 + 圆角边框；display:block 让宽表格横向滚动不挤压页面 */
    .stMarkdown table {
        display: block; max-width: 100%; overflow-x: auto;
        border-collapse: separate; border-spacing: 0;
        font-size: 0.88rem; margin: 0.8rem 0;
        border: 1px solid var(--kb-border); border-radius: var(--kb-radius);
    }
    .stMarkdown thead th { background: #f5f4f2; font-weight: 600; white-space: nowrap; }
    .stMarkdown th, .stMarkdown td {
        padding: 0.5rem 0.8rem;
        border-bottom: 1px solid var(--kb-border); border-right: 1px solid var(--kb-border);
    }
    .stMarkdown tr > th:last-child, .stMarkdown tr > td:last-child { border-right: none; }
    .stMarkdown tbody tr:last-child > td { border-bottom: none; }
    .stMarkdown tbody tr:nth-child(even) td { background: #fafaf9; }
    .stMarkdown tbody tr:hover td { background: #f0f4f9; }
    /* 首页「按功能细分」表：撑满容器、数字右对齐（覆盖上面的全局表格样式） */
    .stMarkdown table.stat-table {
        display: table; width: 100%; border: none; margin: 0.2rem 0;
        border-collapse: collapse; font-size: 0.85rem;
    }
    .stMarkdown table.stat-table thead th {
        background: none; color: var(--kb-text-3);
        font-size: 0.78rem; font-weight: 600; white-space: nowrap;
    }
    .stMarkdown table.stat-table th, .stMarkdown table.stat-table td {
        border: none; border-bottom: 1px solid var(--kb-border);
        padding: 0.28rem 0.4rem;
    }
    .stMarkdown table.stat-table tbody td { background: transparent; color: var(--kb-text-2); }
    .stMarkdown table.stat-table tbody tr:last-child > td { border-bottom: none; }
    .stMarkdown table.stat-table .num { text-align: right; font-variant-numeric: tabular-nums; }
    .stMarkdown blockquote {
        border-left: 3px solid var(--kb-accent); background: var(--kb-accent-soft);
        padding: 0.5rem 1rem; border-radius: 0 6px 6px 0; color: var(--kb-text-2);
    }
    .stMarkdown code { background: #f3f2f0; padding: 0.1rem 0.35rem; border-radius: 4px; font-size: 0.85em; }
    /* pre（含 ASCII 表格围栏）：中西文等宽字体栈、字号略缩、横向滚动不换行 */
    .stMarkdown pre {
        background: #f5f4f2; border: 1px solid var(--kb-border); border-radius: var(--kb-radius);
        white-space: pre; overflow-x: auto;
    }
    .stMarkdown pre code {
        background: none; padding: 0; font-size: 0.82rem; line-height: 1.5;
        font-family: "Sarasa Mono SC", "Cascadia Mono", "Noto Sans Mono CJK SC", Consolas, "Microsoft YaHei", monospace;
    }
</style>
""", unsafe_allow_html=True)


def section_header(text):
    st.markdown(f"<div class='section-header'>{text}</div>", unsafe_allow_html=True)


def sidebar_section(text):
    st.markdown(f"<div class='sb-section'>{text}</div>", unsafe_allow_html=True)


def parse_sections(content):
    """把 markdown 按 ##/### 标题切分，返回 (sections, toc)。
    toc 为 (level, text, anchor_id) 列表，anchor 标签已注入对应 section 开头。"""
    lines = content.split("\n")
    toc = []
    sections = []
    buf = []
    in_code = False
    idx = 0
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r'^(#{2,3})\s+(.+?)\s*$', line)
        if m:
            if buf:
                sections.append("\n".join(buf))
                buf = []
            anchor = f"toc-{idx}"
            idx += 1
            text = re.sub(r'[*`]', '', m.group(2))
            toc.append((len(m.group(1)), text, anchor))
            buf.append(f'<a id="{anchor}"></a>')
            buf.append(line)
        else:
            buf.append(line)
    if buf:
        sections.append("\n".join(buf))
    return sections, toc


def render_breadcrumb(parents, current_label):
    """面包屑：parents 为 (label, state_dict) 可点击项，current_label 为当前页文本。"""
    widths = []
    for label, _ in parents:
        widths.append(max(len(label) * 1.2, 2))
        widths.append(0.4)
    widths.append(max(len(current_label) * 1.2, 6))
    widths.append(20)  # 填充列，让面包屑靠左聚拢
    cols = st.columns(widths)
    i = 0
    for label, state in parents:
        with cols[i]:
            if st.button(label, key=f"crumb_{i}_{label}", type="tertiary"):
                st.session_state.update(state)
                st.rerun()
        with cols[i + 1]:
            st.markdown("<div class='crumb-sep'>/</div>", unsafe_allow_html=True)
        i += 2
    with cols[i]:
        st.markdown(f"<div class='crumb-current'>{current_label}</div>", unsafe_allow_html=True)


# Session state
if "index" not in st.session_state:
    with st.spinner("正在加载知识库索引..."):
        st.session_state.index = load_index()

if "selected_doc" not in st.session_state:
    st.session_state.selected_doc = None

if "view_mode" not in st.session_state:
    st.session_state.view_mode = "home"

if "search_query" not in st.session_state:
    st.session_state.search_query = ""

if "selected_category" not in st.session_state:
    st.session_state.selected_category = None

# 启动时载入本机持久化的 API Key（填一次，之后自动生效）
if "user_api_key" not in st.session_state:
    st.session_state.user_api_key = _load_local_key()


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.0f}KB"
    else:
        return f"{size_bytes/(1024*1024):.1f}MB"


def open_in_vscode(doc_path):
    abs_path = os.path.join(KNOWLEDGE_DIR, doc_path)
    if os.path.exists(abs_path):
        os.system(f'code "{abs_path}"')


def render_document_card(doc, show_category=True):
    icon = FILE_ICONS.get(doc["type"], "📝")
    title = doc.get("title") or doc["name"].replace(".md", "")
    category = doc.get("category", "其他")
    cat_icon = doc.get("category_icon", "📁")

    with st.container(border=True):
        col1, col2 = st.columns([14, 1])

        with col1:
            if st.button(
                f"{icon} {title}",
                key=f"doc_btn_{doc['path']}",
                type="tertiary",
                use_container_width=True,
            ):
                st.session_state.selected_doc = doc
                st.session_state.view_mode = "doc"
                st.rerun()

        with col2:
            if st.button("✏️", key=f"edit_{doc['path']}", type="tertiary", help="在 VS Code 中编辑"):
                open_in_vscode(doc["path"])

        meta_parts = []
        if show_category and category != "其他":
            meta_parts.append(f"{cat_icon} {category}")
        if doc.get("track") and doc["track"] != "未分类":
            meta_parts.append(f"🎯 {doc['track']}")
        if doc.get("last_updated"):
            meta_parts.append(f"📅 {doc['last_updated']}")
        if doc.get("status"):
            meta_parts.append(f"🏷️ {doc['status']}")

        if meta_parts:
            st.markdown(f"<div class='meta-line'>{' · '.join(meta_parts)}</div>", unsafe_allow_html=True)

        if doc.get("subtitle"):
            st.caption(doc["subtitle"][:200])


FEATURE_LABELS = {
    "battle": "⚔️ Thesis Battle",
    "review": "🧭 新项目评审",
    "radar": "📡 Investment Radar",
    "unknown": "其他",
}


def _week_costs():
    """读取 data/ai_costs.jsonl，返回近 7 天 (recs, 总花费, 总 tokens, 按功能聚合)。"""
    path = os.path.join(DATA_DIR, "ai_costs.jsonl")
    recs = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        recs.append(json.loads(line))
        except (OSError, ValueError):
            recs = []
    cutoff = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    recs = [r for r in recs if str(r.get("ts", "")) >= cutoff]
    total = sum(r.get("cost", 0) for r in recs)
    tokens = sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in recs)
    by_feat = {}
    for r in recs:
        f = by_feat.setdefault(r.get("feature", "unknown"),
                               {"calls": 0, "tokens": 0, "cost": 0.0})
        f["calls"] += 1
        f["tokens"] += r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)
        f["cost"] += r.get("cost", 0)
    return recs, total, tokens, by_feat


def render_home():
    index = st.session_state.index
    recs, total, tokens, by_feat = _week_costs()

    # 顶部统计一行搞定：单容器 flex 三栏（概览 / 本周花费 / 按功能细分），天然等高
    if recs:
        cost_rows = (
            f"<div class='stat-row'><span>总花费</span><b>¥{total:.2f}</b></div>"
            f"<div class='stat-row'><span>调用次数</span><b>{len(recs)}</b></div>"
            f"<div class='stat-row'><span>Tokens</span><b>{tokens / 10000:.1f} 万</b></div>")
        feat_rows = "".join(
            f"<tr><td>{FEATURE_LABELS.get(feat, feat)}</td>"
            f"<td class='num'>{f['calls']}</td>"
            f"<td class='num'>{f['tokens'] / 10000:.1f} 万</td>"
            f"<td class='num'>¥{f['cost']:.2f}</td></tr>"
            for feat, f in sorted(by_feat.items(), key=lambda kv: -kv[1]["cost"]))
        feat_html = (
            "<table class='stat-table'><thead><tr><th>功能</th>"
            "<th class='num'>调用</th><th class='num'>Tokens</th>"
            "<th class='num'>花费</th></tr></thead>"
            f"<tbody>{feat_rows}</tbody></table>")
        if any(r.get("estimated") for r in recs):
            feat_html += ("<div class='stat-empty'>部分记录为估算值"
                          "（API 未返回 token 用量，按字符数粗估）。</div>")
    else:
        cost_rows = "<div class='stat-empty'>近 7 天暂无 AI 调用记录。</div>"
        feat_html = ("<div class='stat-empty'>Thesis Battle、新项目评审、Radar "
                     "自动抓取的 token 花费会记录在这里。</div>")

    with st.container(border=True):
        st.markdown(
            "<div class='stat-flex'>"
            "<div class='stat-col'>"
            "<div class='stat-title'>知识库概览</div>"
            f"<div class='stat-row'><span>总文档</span><b>{index['total_documents']}</b></div>"
            f"<div class='stat-row'><span>分类</span><b>{len(index.get('categories', {}))}</b></div>"
            f"<div class='stat-row'><span>索引时间</span><b>{index['indexed_at'][:10]}</b></div>"
            "</div>"
            "<div class='stat-col'>"
            "<div class='stat-title'>本周 AI 花费</div>"
            f"{cost_rows}"
            "</div>"
            "<div class='stat-col wide'>"
            "<div class='stat-title'>按功能细分</div>"
            f"{feat_html}"
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )

    section_header("按分类浏览")

    categories = index.get("categories", {})
    cat_cols = st.columns(2)
    col_idx = 0

    category_order = ["02_deals", "03_frameworks", "04_comparables",
                      "05_tracking", "06_strategy", "07_learnings", "08_funds",
                      "09_tech"]

    for cat_key in category_order:
        if cat_key not in categories:
            continue
        cat = categories[cat_key]
        color = CATEGORY_COLORS.get(cat["name"], "#7f7f7f")

        with cat_cols[col_idx % 2]:
            with st.container(border=True):
                st.markdown(f"<div class='cat-strip' style='background:{color}'></div>", unsafe_allow_html=True)
                st.markdown(
                    f"<div class='cat-head'>{cat['icon']} {cat['name']}"
                    f"<span class='cat-count'>{cat['count']} 篇</span></div>"
                    f"<div class='cat-desc'>{cat['description']}</div>",
                    unsafe_allow_html=True,
                )

                for doc in cat["documents"][:5]:
                    title = doc.get("title") or doc["name"].replace(".md", "")
                    if st.button(f"📝 {title}", key=f"home_cat_{doc['path']}",
                                 type="tertiary", use_container_width=True):
                        st.session_state.selected_doc = doc
                        st.session_state.view_mode = "doc"
                        st.rerun()

                if cat["count"] > 5:
                    if st.button(f"查看全部 {cat['count']} 篇 →", key=f"view_all_{cat_key}", type="tertiary"):
                        st.session_state.selected_category = cat_key
                        st.session_state.view_mode = "category"
                        st.rerun()

        col_idx += 1

    section_header("最近更新")
    recent_docs = sorted(
        index.get("documents", []),
        key=lambda x: x.get("modified", ""),
        reverse=True,
    )[:10]

    for doc in recent_docs:
        render_document_card(doc)


def render_category_view(cat_key):
    index = st.session_state.index
    cat = index.get("categories", {}).get(cat_key)
    
    if not cat:
        st.error("分类不存在")
        return
    
    color = CATEGORY_COLORS.get(cat["name"], "#7f7f7f")

    render_breadcrumb([("首页", {"view_mode": "home", "selected_doc": None, "selected_category": None})],
                      cat["name"])

    st.markdown(f"<div class='cat-strip' style='background:{color}; margin:0 0 0.8rem 0'></div>", unsafe_allow_html=True)
    st.markdown(f"<div class='doc-title'>{cat['icon']} {cat['name']}</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='meta-line'>{cat['description']} · 共 {cat['count']} 篇</div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom: 1rem'></div>", unsafe_allow_html=True)

    docs = cat["documents"]
    if cat_key == "02_deals":
        track_options = sorted({d.get("track", "未分类") for d in cat["documents"]})
        col_t, col_s, col_o, col_n = st.columns([2, 2, 2, 3])
        with col_t:
            track_f = st.selectbox("赛道", ["全部"] + track_options, key="deals_f_track")
        with col_s:
            status_f = st.selectbox("状态", ["全部"] + STATUS_BUCKETS, key="deals_f_status")
        with col_o:
            sort_f = st.selectbox("排序", ["按更新时间", "按名称"], key="deals_f_sort")
        if track_f != "全部":
            docs = [d for d in docs if d.get("track") == track_f]
        if status_f != "全部":
            docs = [d for d in docs if d.get("status") == status_f]
        if sort_f == "按名称":
            docs = sorted(docs, key=lambda d: (d.get("title") or d["name"]))
        else:
            docs = sorted(docs, key=lambda d: d.get("modified", ""), reverse=True)
        with col_n:
            st.markdown(f"<div class='meta-line' style='margin-top:1.9rem'>命中 {len(docs)} / {cat['count']} 篇</div>",
                        unsafe_allow_html=True)

    for doc in docs:
        render_document_card(doc, show_category=False)


def render_industry_cognition():
    """行业认知（01_industry）嵌入区块：迁移到新项目评审页、评审功能下方。"""
    index = st.session_state.index
    cat = index.get("categories", {}).get("01_industry")
    if not cat:
        return

    color = CATEGORY_COLORS.get(cat["name"], "#7f7f7f")
    st.divider()
    st.markdown(f"<div class='cat-strip' style='background:{color}; margin:0 0 0.8rem 0'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-header'>② 行业认知</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='meta-line'>{cat['description']} · 共 {cat['count']} 篇</div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom: 0.8rem'></div>", unsafe_allow_html=True)

    for doc in cat["documents"]:
        render_document_card(doc, show_category=False)


def render_search_results(query):
    index = st.session_state.index
    results = search_documents(index, query)
    
    render_breadcrumb([("首页", {"view_mode": "home", "selected_doc": None, "selected_category": None})],
                      f"搜索：{query}")

    section_header(f"搜索：{query}")
    st.markdown(f"<div class='meta-line'>共 {len(results)} 个结果</div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom: 0.8rem'></div>", unsafe_allow_html=True)

    if not results:
        st.info("未找到匹配结果，尝试其他关键词")
        return
    
    for doc in results:
        render_document_card(doc)


def render_document_detail(doc):
    if not doc:
        st.info("请选择一篇文档")
        return

    title = doc.get("title") or doc["name"].replace(".md", "")
    index = st.session_state.index

    # 打开新文档时滚动回顶部
    if st.session_state.get("_last_doc_path") != doc["path"]:
        st.session_state["_last_doc_path"] = doc["path"]
        components.html("<script>window.parent.document.querySelector('[data-testid=\"stMain\"]').scrollTo(0, 0);</script>", height=0)

    st.markdown('<a id="page-top"></a>', unsafe_allow_html=True)

    # 面包屑：首页 / 分类 / 当前文档
    parents = [("首页", {"view_mode": "home", "selected_doc": None, "selected_category": None})]
    cat_key = doc.get("category_key")
    if cat_key:
        parents.append((doc.get("category", "其他"),
                        {"view_mode": "category", "selected_category": cat_key, "selected_doc": None}))
    render_breadcrumb(parents, title)

    col_title, col_edit = st.columns([14, 1])
    with col_title:
        st.markdown(f"<div class='doc-title'>{title}</div>", unsafe_allow_html=True)
    with col_edit:
        if st.button("✏️ 编辑", key="edit_current", type="tertiary"):
            open_in_vscode(doc["path"])

    meta_parts = [f"{doc.get('category_icon', '📁')} {doc.get('category', '其他')}"]
    if doc.get("track") and doc["track"] != "未分类":
        meta_parts.append(f"🎯 {doc['track']}")
    if doc.get("last_updated"):
        meta_parts.append(f"📅 {doc['last_updated']}")
    if doc.get("status"):
        meta_parts.append(f"🏷️ {doc['status']}")
    if doc.get("project"):
        meta_parts.append(f"📌 {doc['project']}")
    st.markdown(f"<div class='meta-line'>{' · '.join(meta_parts)}</div>", unsafe_allow_html=True)

    st.divider()

    _, body_col, _ = st.columns([1, 10, 1])
    with body_col:
        content = doc.get("content", "")
        # 去掉正文开头与页首重复的 # 标题行
        lines = content.split("\n")
        if lines and lines[0].strip().startswith("# "):
            content = "\n".join(lines[1:]).lstrip("\n")
        if content:
            content = wrap_ascii_tables(content)  # 纯文字/ASCII 表格包成围栏，等宽渲染
            sections, _ = parse_sections(content)
            for sec in sections:
                if sec.startswith('<a id="'):
                    cut = sec.index("</a>") + 4
                    st.markdown(sec[:cut], unsafe_allow_html=True)
                    st.markdown(sec[cut:])
                else:
                    st.markdown(sec)
        else:
            st.warning("文档内容为空")

        st.divider()

        # 上一篇 / 下一篇（同分类内）
        if cat_key:
            cat_docs = get_documents_by_category(index, cat_key)
            paths = [d["path"] for d in cat_docs]
            if doc["path"] in paths:
                pos = paths.index(doc["path"])
                prev_doc = cat_docs[pos - 1] if pos > 0 else None
                next_doc = cat_docs[pos + 1] if pos < len(cat_docs) - 1 else None
                if prev_doc or next_doc:
                    pc, nc = st.columns(2)
                    with pc:
                        if prev_doc:
                            st.markdown("<div class='pn-label'>← 上一篇</div>", unsafe_allow_html=True)
                            ptitle = prev_doc.get("title") or prev_doc["name"].replace(".md", "")
                            if st.button(ptitle, key="prev_doc", type="tertiary"):
                                st.session_state.selected_doc = prev_doc
                                st.rerun()
                    with nc:
                        if next_doc:
                            st.markdown("<div class='pn-label'>下一篇 →</div>", unsafe_allow_html=True)
                            ntitle = next_doc.get("title") or next_doc["name"].replace(".md", "")
                            if st.button(ntitle, key="next_doc", type="tertiary"):
                                st.session_state.selected_doc = next_doc
                                st.rerun()
                    st.divider()

        related = get_related_documents(index, doc)
        if related:
            with st.expander(f"🔗 相关文档推荐 ({len(related)} 篇)"):
                for rdoc in related:
                    rtitle = rdoc.get("title") or rdoc["name"].replace(".md", "")
                    rcat = rdoc.get("category", "")
                    label = f"📝 {rtitle}"
                    if rcat:
                        label += f" ({rcat})"
                    if st.button(label, key=f"rel_{rdoc['path']}", type="tertiary", use_container_width=True):
                        st.session_state.selected_doc = rdoc
                        st.rerun()

        with st.expander("📎 文件信息"):
            st.markdown(f"- **文件名**: `{doc['name']}`")
            st.markdown(f"- **路径**: `{doc['path']}`")
            st.markdown(f"- **大小**: {format_size(doc['size'])}")
            st.markdown(f"- **修改时间**: {doc['modified'][:19]}")

        st.markdown("<div style='text-align:center; margin-top:1.2rem'>"
                    "<a class='back-top' href='#page-top'>↑ 回到顶部</a></div>",
                    unsafe_allow_html=True)


# ==================== 主界面 ====================

if st.session_state.view_mode == "home":
    st.markdown('<div class="main-header">🧠 一级投研知识库</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header">沉淀认知 · 关联洞察 · 复用框架</div>', unsafe_allow_html=True)

# 搜索栏（仅首页与搜索结果页显示，便于在搜索页改关键词重新搜）
if st.session_state.view_mode in ("home", "search"):
    col_search, col_refresh = st.columns([6, 1])
    with col_search:
        search_query = st.text_input(
            "🔍 搜索文档、项目、赛道、概念...",
            value=st.session_state.search_query,
            placeholder="例如：AI4S、光掩模、国产替代...",
        )
    with col_refresh:
        if st.button("🔄 刷新", use_container_width=True):
            with st.spinner("重新索引中..."):
                st.session_state.index = build_index(force=True)
            st.success("索引已更新！")
            st.rerun()

    # 处理搜索
    if search_query != st.session_state.search_query:
        st.session_state.search_query = search_query
        if search_query:
            st.session_state.view_mode = "search"
            st.session_state.selected_doc = None
        else:
            st.session_state.view_mode = "home"
        st.rerun()

# 侧边栏
with st.sidebar:
    sidebar_section("导航")

    if st.button("🏠 首页", use_container_width=True):
        st.session_state.view_mode = "home"
        st.session_state.selected_doc = None
        st.session_state.selected_category = None
        st.rerun()

    if st.button("⚔️ Thesis Battle", use_container_width=True):
        st.session_state.view_mode = "battle"
        st.session_state.selected_doc = None
        st.rerun()

    if st.button("📡 Investment Radar", use_container_width=True):
        st.session_state.view_mode = "radar"
        st.session_state.selected_doc = None
        st.rerun()

    if st.button("🧭 新项目评审", use_container_width=True):
        st.session_state.view_mode = "compare"
        st.session_state.selected_doc = None
        st.rerun()

    if st.button("📥 文件归档", use_container_width=True):
        st.session_state.view_mode = "ingest"
        st.session_state.selected_doc = None
        st.rerun()

    st.divider()

    sidebar_section("API Key")
    st.text_input(
        "Moonshot API Key",
        type="password",
        key="user_api_key",
        placeholder="sk-...（必填，否则 AI 功能不可用）",
        help="填入你自己的 Moonshot API Key 后，Thesis Battle / Radar / 评审等 AI 功能"
             "自动走你的 key 计费。只需填一次：key 会保存在本机 data/local_api_key.txt，"
             "之后每次打开自动生效；清空输入框即删除本机保存的 key。",
        label_visibility="collapsed",
    )
    _cur_key = st.session_state.get("user_api_key", "").strip()
    if _cur_key != _load_local_key():
        _save_local_key(_cur_key)  # 填入/修改/清空 → 同步本机持久化
    if _cur_key:
        st.caption("🔑 Key 已保存在本机，AI 功能走你的额度，无需重复输入")
    else:
        st.caption("⚠️ 未填入 Key，AI 功能（Battle / Radar / 评审 / 归档）不可用")

    st.divider()

    sidebar_section("分类")
    index = st.session_state.index
    categories = index.get("categories", {})

    category_order = ["02_deals", "03_frameworks", "04_comparables",
                      "05_tracking", "06_strategy", "07_learnings", "08_funds",
                      "09_tech"]

    for cat_key in category_order:
        if cat_key not in categories:
            continue
        cat = categories[cat_key]
        is_active = (st.session_state.view_mode == "category"
                     and st.session_state.selected_category == cat_key)
        if st.button(
            f"{cat['icon']} {cat['name']} ({cat['count']})",
            key=f"nav_cat_{cat_key}",
            type="primary" if is_active else "secondary",
            use_container_width=True,
        ):
            st.session_state.selected_category = cat_key
            st.session_state.view_mode = "category"
            st.session_state.selected_doc = None
            st.rerun()

    st.divider()

    # 文档阅读模式下显示本页目录
    if st.session_state.view_mode == "doc" and st.session_state.selected_doc:
        sidebar_section("本页目录")
        _, toc = parse_sections(st.session_state.selected_doc.get("content", ""))
        if toc:
            toc_html = "".join(
                f"<a class='toc-link {'toc-h3' if level == 3 else 'toc-h2'}' href='#{anchor}'>{text}</a>"
                for level, text, anchor in toc[:30]
            )
            st.markdown(toc_html, unsafe_allow_html=True)
        else:
            st.markdown("<div class='meta-line'>本文档无章节标题</div>", unsafe_allow_html=True)
        st.divider()

    sidebar_section("快捷入口")

    quick_links = [
        ("📖 使用手册", "03_frameworks/知识库使用手册.md"),
        ("AI4S项目矩阵", "04_comparables/AI4S项目矩阵.md"),
        ("项目解剖模板", "03_frameworks/项目解剖模板.md"),
        ("科学家创业评估", "03_frameworks/科学家创业评估.md"),
        ("产业链投资图谱", "06_strategy/产业链投资图谱.md"),
    ]

    for label, path in quick_links:
        doc = get_document_by_path(index, path)
        if not doc:
            continue  # 知识库里没有的文档不渲染按钮（避免死按钮）
        if st.button(label, key=f"quick_{path}", use_container_width=True):
            st.session_state.selected_doc = doc
            st.session_state.view_mode = "doc"
            st.rerun()

    st.divider()

    sidebar_section("统计")
    st.markdown(f"- 文档: **{index['total_documents']}**")
    st.markdown(f"- 分类: **{len(categories)}**")


# 主内容区
def _refresh_index():
    st.session_state.index = build_index(force=True)


if st.session_state.view_mode == "home":
    render_home()
elif st.session_state.view_mode == "category":
    render_category_view(st.session_state.selected_category)
elif st.session_state.view_mode == "search":
    render_search_results(st.session_state.search_query)
elif st.session_state.view_mode == "doc":
    render_document_detail(st.session_state.selected_doc)
elif st.session_state.view_mode == "battle":
    render_battle(st.session_state.index, _refresh_index)
elif st.session_state.view_mode == "radar":
    render_radar(st.session_state.index, _refresh_index)
elif st.session_state.view_mode == "compare":
    render_review(st.session_state.index, _refresh_index)
    render_industry_cognition()
elif st.session_state.view_mode == "ingest":
    render_ingest(st.session_state.index, _refresh_index)
