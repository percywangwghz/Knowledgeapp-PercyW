"""
知识库前端主应用 - Streamlit
面向结构化知识库
单文件版本，无外部依赖
"""
import os
import sys
import json
import re
import html
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote as _urlquote

import streamlit as st
import streamlit.components.v1 as components

import jobs
import llm
from battle import render_battle
from config import PROVIDERS
from ingest import render_ingest
from mdblocks import wrap_ascii_tables, inline_local_images, split_details_blocks
from review import render_review
from radar import render_radar
from tracks import STATUS_BUCKETS, get_track, normalize_status

# 前端注入：侧边栏填入的 API Key 存于 session_state，llm 调用时优先取用。
# 填一次即持久化到本机（LOCAL_KEY_FILE），启动时自动载入，之后无需再填。
def _frontend_api_key():
    """返回当前应使用的 API Key（空串 = 未配置，AI 功能不可用）。"""
    return st.session_state.get("user_api_key", "").strip()


def _frontend_model():
    """返回当前会话配置的模型名（空串 = 未配置，llm 侧落厂家预设默认模型）。"""
    return st.session_state.get("user_model", "").strip()


def _frontend_base_url():
    """返回当前会话厂家对应的 API 端点；自定义/无官方兼容端点的厂家取手填值。"""
    pid = st.session_state.get("user_provider", "moonshot")
    if pid in ("custom", "anthropic"):
        return st.session_state.get("user_base_url", "").strip()
    return (PROVIDERS.get(pid) or {}).get("base_url", "")


llm.register_key_provider(_frontend_api_key)
llm.register_model_provider(_frontend_model)
llm.register_base_url_provider(_frontend_base_url)

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
# 厂家 / 模型 / 自定义端点同样持久化到本机（同 local_api_key.txt 模式，env 可覆盖路径）
LOCAL_PROVIDER_FILE = os.environ.get("KB_LOCAL_PROVIDER_FILE",
                                     os.path.join(DATA_DIR, "local_provider.txt"))
LOCAL_MODEL_FILE = os.environ.get("KB_LOCAL_MODEL_FILE",
                                  os.path.join(DATA_DIR, "local_model.txt"))
LOCAL_BASE_URL_FILE = os.environ.get("KB_LOCAL_BASE_URL_FILE",
                                     os.path.join(DATA_DIR, "local_base_url.txt"))


def _load_local(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_local(path, value):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if value:
            with open(path, "w", encoding="utf-8") as f:
                f.write(value)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _load_local_key():
    return _load_local(LOCAL_KEY_FILE)


def _save_local_key(key):
    _save_local(LOCAL_KEY_FILE, key)

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

# v2：分类一律不用彩色，原 CATEGORY_COLORS（tab10 色板）已移除（设计说明书 §3）

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
    doc_words = set(doc.get("content", "").lower().split())  # 只算一次，别放循环里

    for other in index.get("documents", []):
        if other["path"] == doc["path"]:
            continue

        score = 0

        if other.get("category") == doc.get("category"):
            score += 2

        if other.get("project") and doc.get("project"):
            if other["project"] == doc["project"]:
                score += 5

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
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Noto+Sans+SC:wght@400;500;600&family=Noto+Serif+SC:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

    /* ---------- Design Tokens（v2，取自设计说明书 §3） ---------- */
    :root {
        --bg: #F7F7F5;
        --surface: #FFFFFF;
        --surface-subtle: #F1F1EE;

        --text-primary: #202326;
        --text-secondary: #62686D;
        --text-tertiary: #959A9E;

        --border: #E1E1DC;
        --border-strong: #D0D0CA;

        --accent: #354A5F;
        --accent-soft: #EDF1F4;

        --success: #657568;
        --warning: #85765D;
        --danger: #8A5D5D;

        --radius-sm: 4px;
        --radius-md: 6px;
        --radius-lg: 8px;

        --shadow-soft: 0 1px 3px rgba(20, 24, 28, 0.04);

        --font-ui: "Inter", "Noto Sans SC", -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
        --font-serif: "Noto Serif SC", "Source Serif 4", Georgia, serif;
        --font-mono: "IBM Plex Mono", "JetBrains Mono", "SF Mono", Consolas, monospace;

        /* 旧 token 别名：功能模块（radar/ingest 等）内联 HTML 里引用了 --kb-* 变量 */
        --kb-bg: var(--bg);
        --kb-card: var(--surface);
        --kb-content: var(--bg);
        --kb-surface-2: var(--surface-subtle);
        --kb-hover: var(--surface-subtle);
        --kb-text: var(--text-primary);
        --kb-text-2: var(--text-secondary);
        --kb-text-3: var(--text-tertiary);
        --kb-accent: var(--accent);
        --kb-accent-soft: var(--accent-soft);
        --kb-border: var(--border);
        --kb-border-light: var(--border);
        --kb-success: var(--success);
        --kb-warning: var(--warning);
        --kb-danger: var(--danger);
        --kb-radius: var(--radius-md);
    }

    /* ---------- 全局 ---------- */
    .stApp {
        background-color: var(--bg);
        font-family: var(--font-ui);
        color: var(--text-primary);
    }
    /* Streamlit 1.5x+ 主容器是 stMainBlockContainer（.main 前缀已失效），两个选择器都写上 */
    .main .block-container,
    div[data-testid="stMainBlockContainer"] { padding-top: 3.3rem; max-width: 1080px; }
    h1, h2, h3, h4, h5, h6 { color: var(--text-primary); font-weight: 600; letter-spacing: -0.01em; }
    hr { border-color: var(--border) !important; margin: 1.2rem 0; }
    ::selection { background: var(--accent-soft); }

    /* 细浅色滚动条：不框住内容 */
    *::-webkit-scrollbar { width: 9px; height: 9px; }
    *::-webkit-scrollbar-track { background: transparent; }
    *::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; border: 2px solid var(--bg); }
    *::-webkit-scrollbar-thumb:hover { background: #C5C5BF; }

    /* ---------- 顶栏 Header（v2 §2.2）：fixed 浮层，品牌 + 全局搜索 + 状态 ---------- */
    header[data-testid="stHeader"] { background-color: var(--bg); }
    /* 侧栏加宽（1.5 倍诉求）：实际基线 14rem → 21rem（336px），长标题不再挤压换行。
       仅展开时锁定宽度；收起（aria-expanded=false）时交还 Streamlit 归零，主区才能跟着拉宽 */
    section[data-testid="stSidebar"][aria-expanded="true"] { min-width: 21rem !important; width: 21rem !important; }
    section[data-testid="stSidebar"][aria-expanded="true"] > div { min-width: 21rem !important; width: 21rem !important; }
    /* 侧栏与主功能区之间的边界线（侧栏底色与主区一致，需竖线分隔） */
    section[data-testid="stSidebar"] { border-right: 1px solid var(--border); }
    /* 「收起/展开侧栏」按钮常驻：Streamlit 默认悬停才显示，常驻后随时可收起/展开 */
    div[data-testid="stSidebarCollapseButton"] { visibility: visible !important; opacity: 1 !important; }
    div[data-testid="stSidebarCollapsedControl"] { visibility: visible !important; opacity: 1 !important; }
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) {
        position: fixed;
        top: 0;
        left: 21rem;          /* 侧栏展开宽度（336px），从主区左缘起 */
        right: 4rem;            /* 给右上角菜单按钮留位 */
        width: auto !important; /* Streamlit 默认 width:100%，fixed 下会溢出右缘 */
        z-index: 1000000;       /* stToolbar z-index 999990 铺满顶栏，必须盖过它才能点到 */
        background: var(--bg);
        border-bottom: 1px solid var(--border);
        box-shadow: none;
        padding: 0;
    }
    /* 侧栏收起时左移，但给「展开侧栏」按钮留位 */
    div[data-testid="stApp"]:has(section[data-testid="stSidebar"][aria-expanded="false"])
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) {
        left: 3rem;
    }
    /* 顶栏容器 fixed 后，原位的带边框包装器只剩 padding/border 撑出的 ~32px 空壳，抹掉 */
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.topnav-marker) {
        border: none;
        padding: 0;
        min-height: 0;
        margin: 0;
    }
    /* 顶部注入的 <style> markdown 与 0 高 components.html iframe 虽然自身高 0，
       但会白吃 stVerticalBlock 的 16px flex gap 把正文顶下去，一律不参与布局 */
    div[data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"]
    > div[data-testid="stElementContainer"]:has(style),
    div[data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"]
    > div[data-testid="stElementContainer"]:has(iframe[height="0"]) {
        display: none;
    }
    /* 带边框容器的边框/圆角/白底在内层 stVerticalBlock 上，一并抹掉 */
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) > div[data-testid="stVerticalBlock"] {
        background: transparent;
        border: none;
        box-shadow: none;
        padding: 0.55rem 1rem 0.55rem 1.5rem;  /* 左侧 1.5rem：logo 不贴侧栏也不离太远 */
        gap: 0;
    }
    /* 顶栏整行垂直居中：品牌名 / 导航 / 搜索 / 状态同一水平线 */
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) div[data-testid="stHorizontalBlock"] {
        align-items: center;
    }
    /* 顶栏内 markdown 容器默认带 -1rem 负下边距，会把列的布局高度算小 16px，
       导致 logo / Indexed 状态比按钮低 ~8px；顶栏内一律归零 */
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) div[data-testid="stMarkdownContainer"] {
        margin-bottom: 0;
    }
    /* marker 占位元素本身不显示，避免撑高顶栏 */
    div[data-testid="stElementContainer"]:has(.topnav-marker) { display: none; }
    /* 导航条左侧品牌名 */
    .topnav-brand {
        font-size: 0.95rem;
        font-weight: 600;
        color: var(--text-primary);
        letter-spacing: 0.02em;
        display: flex;
        align-items: center;
        min-height: 2.2rem;
        white-space: nowrap;
    }
    /* Header 全局搜索：胶囊形浅底（v2 Header search pill） */
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) div[data-testid="stTextInput"] input {
        border-radius: 999px;
        border: 1px solid var(--border);
        background-color: var(--surface-subtle);
        font-size: 0.85rem;
        color: var(--text-primary);
        padding-top: 0.35rem;
        padding-bottom: 0.35rem;
        transition: background .15s ease, border-color .15s ease;
    }
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) div[data-testid="stTextInput"] input:hover {
        border-color: var(--border-strong); background-color: var(--bg);
    }
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) div[data-testid="stTextInput"] input:focus {
        background-color: var(--surface);
        border-color: var(--border-strong);
        box-shadow: none;
    }
    /* Header 按钮默认 = 文字导航（v2 §2.2 顶栏 nav）：无框、灰字、accent 下划线表当前页 */
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) .stButton button {
        border: none !important;
        border-bottom: 2px solid transparent !important;
        background: transparent !important;
        box-shadow: none !important;
        color: var(--text-tertiary) !important;
        font-size: 0.85rem;
        font-weight: 500;
        padding: 0.3rem 0.1rem;
        min-height: 0;
        border-radius: 0 !important;
        white-space: nowrap;        /* 「新项目评审」等长label 不折行 */
        transition: color .15s ease, border-color .15s ease;
    }
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) .stButton button:hover {
        color: var(--text-primary) !important;
        border-bottom-color: transparent !important;
        background: transparent !important;
    }
    /* 当前页导航：.hdr-nav-on marker 的后一个兄弟元素容器里的按钮 */
    div[data-testid="stElementContainer"]:has(.hdr-nav-on),
    div[data-testid="stElementContainer"]:has(.hdr-pill-marker) { display: none; }
    div[data-testid="stElementContainer"]:has(.hdr-nav-on)
    + div[data-testid="stElementContainer"] .stButton button {
        color: var(--text-primary) !important;
        border-bottom-color: var(--accent) !important;
        font-weight: 600;
    }
    /* Tasks 抽屉开关恢复 quiet 胶囊（.hdr-pill-marker 兄弟模式） */
    div[data-testid="stElementContainer"]:has(.hdr-pill-marker)
    + div[data-testid="stElementContainer"] .stButton button {
        border: 1px solid var(--border) !important;
        background: var(--surface) !important;
        border-radius: 999px !important;
        color: var(--text-secondary) !important;
        font-size: 0.78rem;
        padding: 0.25rem 0.7rem;
    }
    div[data-testid="stElementContainer"]:has(.hdr-pill-marker)
    + div[data-testid="stElementContainer"] .stButton button:hover {
        color: var(--accent) !important;
        border-color: var(--accent) !important;
        background: var(--accent-soft) !important;
    }
    /* Header 搜索：收窄成紧凑胶囊（demo 顶栏搜索尺寸） */
    div[data-testid="stLayoutWrapper"]:has(.topnav-marker) div[data-testid="stTextInput"] {
        max-width: 300px; margin-left: auto;
    }
    .hdr-status {
        display: flex; align-items: center; gap: 0.45rem;
        font-size: 0.78rem; color: var(--text-secondary);
        line-height: 2.2rem; white-space: nowrap;
        font-family: var(--font-mono);
    }
    .hdr-status i {
        width: 7px; height: 7px; border-radius: 50%;
        background: var(--success); display: inline-block;
    }
    .hdr-status.accent i { background: var(--accent); }
    .hdr-status.accent { color: var(--accent); }

    /* ---------- 节标题：Editorial 小编辑标 ---------- */
    .section-header {
        font-size: 0.72rem; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase;
        color: var(--text-tertiary); margin: 1.8rem 0 0.9rem 0;
        padding-bottom: 0.45rem; border-bottom: 1px solid var(--border);
    }

    /* ---------- 搜索框（主区兜底样式）：轻底细边 ---------- */
    div[data-testid="stTextInput"] input {
        border-radius: var(--radius-md);
        border: 1px solid var(--border);
        background-color: var(--surface-subtle);
        font-size: 0.9rem;
        color: var(--text-primary);
        transition: background .15s ease, border-color .15s ease;
    }
    div[data-testid="stTextInput"] input:hover { background-color: var(--surface); }
    div[data-testid="stTextInput"] input:focus {
        background-color: var(--surface);
        border-color: var(--border-strong);
        box-shadow: none;
    }
    /* Baseweb 输入框外壳 focus 描边：覆盖 Streamlit 默认主题色（红色） */
    div[data-testid="stTextInput"] div[data-baseweb="input"]:focus-within,
    div[data-testid="stTextArea"] div[data-baseweb="textarea"]:focus-within {
        border-color: var(--border-strong) !important;
        box-shadow: none !important;
    }

    /* ---------- 文件归档页：上传白框包住整个 uploader ---------- */
    div[data-testid="stFileUploader"] {
        background-color: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        padding: 1rem 1.2rem 0.9rem;
        box-shadow: none;
    }
    div[data-testid="stFileUploader"] section { background-color: transparent; }
    div[data-testid="stFileUploader"] small { color: var(--text-tertiary); }

    /* ---------- 卡片容器（bordered container：白底细边、无阴影、小圆角） ---------- */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background-color: var(--surface);
        border: 1px solid var(--border) !important;
        border-radius: var(--radius-md);
        box-shadow: none;
        transition: border-color .15s ease, background .15s ease;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: var(--border-strong) !important;
        box-shadow: none;
    }

    /* ---------- 按钮：quiet 化（v2 §16 次级动作） ---------- */
    .stButton button {
        border-radius: var(--radius-md);
        font-size: 0.85rem;
        transition: background .15s ease, color .15s ease, border-color .15s ease;
    }
    .stButton button[kind="primary"] {
        background: var(--accent); border-color: var(--accent);
    }
    .stButton button[kind="primary"]:hover {
        background: #2A3D50; border-color: #2A3D50;
    }

    /* ---------- 统计指标：数字走 Mono ---------- */
    div[data-testid="stMetricLabel"] { color: var(--text-tertiary); font-size: 0.78rem; }
    div[data-testid="stMetricValue"] {
        font-family: var(--font-mono);
        font-size: 1.35rem; font-weight: 500; color: var(--text-primary);
    }

    /* ---------- 三级按钮（文档标题 / 链接式按钮） ---------- */
    button[kind="tertiary"] {
        justify-content: flex-start;
        text-align: left;
        color: var(--text-primary);
        font-weight: 500;
        font-size: 0.92rem;
        padding: 0.15rem 0.25rem;
        white-space: nowrap;
    }
    button[kind="tertiary"] [data-testid="stMarkdownContainer"] { flex: 1 1 auto; text-align: left; }
    button[kind="tertiary"] > div { justify-content: flex-start; width: 100%; }
    button[kind="tertiary"]:hover { color: var(--accent); background: transparent; }
    button[kind="tertiary"]:focus:not(:active) { color: var(--accent); background: transparent; }

    /* ---------- 文档详情 ---------- */
    .doc-title { font-size: 1.55rem; font-weight: 600; color: var(--text-primary); letter-spacing: -0.01em; line-height: 1.3; margin-bottom: 0.3rem; }

    /* ---------- 元信息 / 辅助文字 ---------- */
    .meta-line { color: var(--text-tertiary); font-size: 0.78rem; margin-top: 0.1rem; }
    .caption { color: var(--text-tertiary); font-size: 0.78rem; line-height: 1.7; }
    .card {
        background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--radius-md); padding: 0.7rem 0.9rem;
        box-shadow: var(--shadow-soft);
    }

    /* ---------- 侧边栏：纸底，active = 2px accent 内线 + 浅底 ---------- */
    section[data-testid="stSidebar"] { background-color: var(--bg); }
    section[data-testid="stSidebar"] button[kind="secondary"] {
        border: none; background: transparent;
        justify-content: flex-start; text-align: left;
        color: var(--text-secondary); font-weight: 500; font-size: 0.85rem;
        border-radius: var(--radius-sm);
        transition: background .15s ease, color .15s ease;
    }
    section[data-testid="stSidebar"] button[kind="secondary"]:hover { background: var(--surface-subtle); color: var(--text-primary); }
    section[data-testid="stSidebar"] button[kind="primary"] {
        background: var(--surface-subtle); color: var(--text-primary);
        border: none; justify-content: flex-start; text-align: left;
        font-weight: 600; font-size: 0.85rem;
        border-radius: var(--radius-sm);
        box-shadow: inset 2px 0 0 var(--accent);
    }
    /* active 项在 hover/focus 下也保持浅底（否则点击后 focus 态落回 Streamlit 默认深色填充） */
    section[data-testid="stSidebar"] button[kind="primary"]:hover,
    section[data-testid="stSidebar"] button[kind="primary"]:focus,
    section[data-testid="stSidebar"] button[kind="primary"]:focus-visible,
    section[data-testid="stSidebar"] button[kind="primary"]:focus:not(:active),
    section[data-testid="stSidebar"] button[kind="primary"]:active {
        background: var(--surface-subtle) !important;
        color: var(--text-primary) !important;
        border: none !important;
        box-shadow: inset 2px 0 0 var(--accent) !important;
    }
    .sb-section {
        font-size: 0.66rem; font-weight: 600; color: var(--text-tertiary);
        letter-spacing: 0.12em; text-transform: uppercase; margin: 0.4rem 0;
    }
    /* 侧栏按钮：文字左对齐 + 行高压到导航密度 */
    section[data-testid="stSidebar"] .stButton button > div { justify-content: flex-start; }
    section[data-testid="stSidebar"] .stButton button { min-height: 1.85rem; padding-top: 0.05rem; padding-bottom: 0.05rem; }

    /* ---------- 文档目录 TOC（阅读页右栏；v2 §6.2 当前章节 = 深字 + accent 左线） ---------- */
    .toc-link {
        display: block; color: var(--text-tertiary); font-size: 0.8rem;
        padding: 0.28rem 0 0.28rem 0.75rem; text-decoration: none;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        border-left: 1px solid var(--border);
        transition: color .15s ease, border-color .15s ease;
    }
    .toc-link:hover { color: var(--text-primary); }
    .toc-h3 { padding-left: 1.6rem; font-size: 0.76rem; }
    .toc-link.active {
        color: var(--text-primary); font-weight: 500;
        border-left-color: var(--accent);
    }

    /* ---------- 上一篇/下一篇 & 回到顶部 ---------- */
    .pn-label { color: var(--text-tertiary); font-size: 0.78rem; margin-bottom: 0.1rem; }
    .back-top { color: var(--text-tertiary); font-size: 0.82rem; text-decoration: none; }
    .back-top:hover { color: var(--accent); }

    /* ---------- 面包屑（阅读页/分类页顶部回跳索引） ---------- */
    .crumb {
        font-size: 0.8rem; color: var(--text-tertiary); margin-bottom: 0.4rem;
        display: flex; flex-wrap: wrap; gap: 0.35rem; align-items: baseline;
    }
    .crumb a { color: var(--text-tertiary); text-decoration: none; }
    .crumb a:hover { color: var(--accent); }
    .crumb-sep { opacity: 0.6; }
    .crumb-cur { color: var(--text-secondary); }

    /* ---------- Context Rail（阅读页右栏，v2 §6.3） ---------- */
    /* rail-marker 所在的 stColumn：sticky + 自身滚动。
       TOC spy 的 keepInView 只滚这个栏，不会再抢主页面滚动条。 */
    div[data-testid="stColumn"]:has(.rail-marker) {
        position: sticky; top: 72px; align-self: flex-start;
        max-height: calc(100vh - 96px); overflow-y: auto;
    }
    .rail-label {
        font-size: 10.5px; letter-spacing: 0.12em; color: var(--text-tertiary);
        text-transform: uppercase; font-weight: 500; margin: 1.1rem 0 0.5rem;
    }
    .cr-block { margin-bottom: 0.85rem; }
    .cr-k {
        font-size: 10.5px; letter-spacing: 0.1em; color: var(--text-tertiary);
        text-transform: uppercase; margin-bottom: 2px;
    }
    .cr-v { font-size: 0.84rem; color: var(--text-primary); }

    /* ---------- StatusPill（任务抽屉 / 任务状态，v2 §14） ---------- */
    .pill {
        display: inline-block; font-size: 11px; padding: 1px 8px;
        border-radius: var(--radius-sm); border: 1px solid var(--border);
        color: var(--text-secondary); background: var(--surface);
        white-space: nowrap;
    }
    .pill.running { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
    .pill.done { color: var(--success); border-color: var(--success); }
    .pill.error { color: var(--danger); border-color: var(--danger); }
    .pill.interrupted { color: var(--warning); border-color: var(--warning); }

    /* ---------- Markdown 正文排版 ---------- */
    .stMarkdown h2 { font-size: 1.22rem; margin-top: 1.7rem; padding-bottom: 0.3rem; border-bottom: 1px solid var(--border); }
    .stMarkdown h3 { font-size: 1.02rem; margin-top: 1.3rem; }
    .stMarkdown p, .stMarkdown li { line-height: 1.8; }
    /* 正文插图：限宽限高居中——自动截取的图表原图很大，撑满全页会淹没正文 */
    .stMarkdown img {
        max-width: 70%; max-height: 420px; object-fit: contain;
        display: block; margin: 0.6rem auto;
    }
    /* Markdown 表格：表头底色 + 斑马纹 + 细边框；display:block 让宽表格横向滚动不挤压页面 */
    .stMarkdown table {
        display: block; max-width: 100%; overflow-x: auto;
        border-collapse: separate; border-spacing: 0;
        font-size: 0.86rem; margin: 0.8rem 0;
        border: 1px solid var(--border); border-radius: var(--radius-md);
    }
    .stMarkdown thead th { background: var(--surface-subtle); font-weight: 600; white-space: nowrap; }
    .stMarkdown th, .stMarkdown td {
        padding: 0.5rem 0.8rem;
        border-bottom: 1px solid var(--border); border-right: 1px solid var(--border);
    }
    .stMarkdown tr > th:last-child, .stMarkdown tr > td:last-child { border-right: none; }
    .stMarkdown tbody tr:last-child > td { border-bottom: none; }
    .stMarkdown tbody tr:nth-child(even) td { background: var(--bg); }
    .stMarkdown tbody tr:hover td { background: var(--surface-subtle); }
    /* 「按功能细分」表：撑满容器、数字右对齐走 Mono（覆盖上面的全局表格样式） */
    .stMarkdown table.stat-table {
        display: table; width: 100%; border: none; margin: 0.2rem 0;
        border-collapse: collapse; font-size: 0.84rem;
    }
    .stMarkdown table.stat-table thead th {
        background: none; color: var(--text-tertiary);
        font-size: 0.68rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase;
        white-space: nowrap;
    }
    .stMarkdown table.stat-table th, .stMarkdown table.stat-table td {
        border: none; border-bottom: 1px solid var(--border);
        padding: 0.3rem 0.4rem;
    }
    .stMarkdown table.stat-table tbody td { background: transparent; color: var(--text-secondary); }
    .stMarkdown table.stat-table tbody tr:last-child > td { border-bottom: none; }
    .stMarkdown table.stat-table .num {
        text-align: right; font-family: var(--font-mono); font-size: 0.78rem;
    }
    /* 引用：2px accent 左边线 + Serif，Research Memo 感 */
    .stMarkdown blockquote {
        border-left: 2px solid var(--accent); background: transparent;
        padding: 0.3rem 0 0.3rem 1rem; border-radius: 0; color: #30343A;
        font-family: var(--font-serif); font-size: 0.92rem;
    }
    .stMarkdown code { background: var(--surface-subtle); padding: 0.1rem 0.35rem; border-radius: var(--radius-sm); font-size: 0.85em; }
    /* pre（含 ASCII 表格围栏）：中西文等宽字体栈、字号略缩、横向滚动不换行 */
    .stMarkdown pre {
        background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-md);
        white-space: pre; overflow-x: auto;
    }
    .stMarkdown pre code {
        background: none; padding: 0; font-size: 0.82rem; line-height: 1.5;
        font-family: "Sarasa Mono SC", "Cascadia Mono", "Noto Sans Mono CJK SC", Consolas, "Microsoft YaHei", monospace;
    }

    /* ---------- 文档正文：Editorial 阅读排版（doc-body-marker 之后的正文列） ----------
       长文走 Serif、1.85 行高、限宽 46rem 阅读列；marker 由 render_document_detail 注入 */
    div[data-testid="stElementContainer"]:has(.doc-body-marker) { display: none; }
    div[data-testid="stVerticalBlock"]:has(.doc-body-marker) .stMarkdown p,
    div[data-testid="stVerticalBlock"]:has(.doc-body-marker) .stMarkdown li {
        font-family: var(--font-serif);
        font-size: 0.96rem; line-height: 1.85; color: #30343A;
    }
    div[data-testid="stVerticalBlock"]:has(.doc-body-marker) .stMarkdown p,
    div[data-testid="stVerticalBlock"]:has(.doc-body-marker) .stMarkdown li,
    div[data-testid="stVerticalBlock"]:has(.doc-body-marker) .stMarkdown h2,
    div[data-testid="stVerticalBlock"]:has(.doc-body-marker) .stMarkdown h3,
    div[data-testid="stVerticalBlock"]:has(.doc-body-marker) .stMarkdown blockquote {
        max-width: 46rem;
    }

    /* ==================== Demo 组件体系（ui_demo_v2 原样移植） ==================== */
    /* 小编辑标 / 页标题 */
    .section-label {
        font-size: 11px; letter-spacing: 0.12em; color: var(--text-tertiary);
        text-transform: uppercase; font-weight: 500; margin-bottom: 10px;
    }
    .page-title { font-size: 28px; font-weight: 600; letter-spacing: -0.01em; color: var(--text-primary); }
    .page-sub { color: var(--text-secondary); font-size: 13px; margin-top: 6px; }
    .home-section { margin-top: 44px; }

    /* Hero（首页 §4） */
    .kb-hero { margin-bottom: 24px; }
    .kb-hero h1 { font-size: 30px; font-weight: 600; margin: 0; color: var(--text-primary); }
    .kb-hero .index-line { font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); margin-top: 8px; }
    a.big-search {
        display: flex; align-items: center; gap: 12px;
        background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
        padding: 13px 22px; color: var(--text-tertiary); font-size: 14px;
        margin: 4px 0 40px; box-shadow: var(--shadow-soft);
        text-decoration: none; transition: border-color .15s ease;
    }
    a.big-search:hover { border-color: var(--border-strong); }
    a.big-search .kbd {
        margin-left: auto; font-family: var(--font-mono); font-size: 11px;
        border: 1px solid var(--border); border-radius: var(--radius-sm);
        padding: 1px 7px; background: var(--surface-subtle); color: var(--text-tertiary);
    }

    /* Editorial 文档行：整行 <a> 可点，细线分隔，hover 浅底（demo .doc-row） */
    a.doc-row {
        display: flex; align-items: baseline; gap: 16px;
        padding: 11px 12px; margin: 0 -12px;
        border-bottom: 1px solid var(--border); border-radius: var(--radius-sm);
        text-decoration: none; transition: background .15s ease;
    }
    a.doc-row:hover { background: var(--surface); }
    a.doc-row .title { font-size: 14px; font-weight: 500; color: var(--text-primary); }
    a.doc-row .meta {
        font-size: 12px; color: var(--text-tertiary);
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
    }
    a.doc-row .date {
        margin-left: auto; font-family: var(--font-mono); font-size: 12px;
        color: var(--text-tertiary); white-space: nowrap;
    }

    /* Collections 编号索引（demo .collections-grid / .collection-row） */
    .collections-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 48px; }
    a.collection-row {
        display: flex; align-items: baseline; gap: 14px;
        padding: 12px; margin: 0 -12px;
        border-bottom: 1px solid var(--border); border-radius: var(--radius-sm);
        text-decoration: none; transition: background .15s ease;
    }
    a.collection-row:hover { background: var(--surface); }
    a.collection-row .num { font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); width: 20px; flex: 0 0 20px; }
    a.collection-row .cname { font-size: 14px; font-weight: 500; color: var(--text-primary); white-space: nowrap; }
    a.collection-row .cdesc {
        font-size: 12px; color: var(--text-tertiary); min-width: 0;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    a.collection-row .ccount { margin-left: auto; font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }

    /* Recent Changes 行（demo .change-row） */
    a.change-row {
        display: flex; gap: 16px; align-items: baseline;
        padding: 8px 12px; margin: 0 -12px;
        border-bottom: 1px solid var(--border); border-radius: var(--radius-sm);
        font-size: 13px; text-decoration: none; transition: background .15s ease;
    }
    a.change-row:hover { background: var(--surface); }
    a.change-row .d { font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); width: 46px; flex: 0 0 46px; }
    a.change-row .t { font-weight: 500; color: var(--text-primary); width: 240px; flex: 0 0 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    a.change-row .w { color: var(--text-secondary); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    /* Library 行（demo .lib-row：两行+摘要） */
    a.lib-row {
        display: block; padding: 14px 12px; margin: 0 -12px;
        border-bottom: 1px solid var(--border); border-radius: var(--radius-sm);
        text-decoration: none; transition: background .15s ease;
    }
    a.lib-row:hover { background: var(--surface); }
    /* 静态变体（Radar Variables 列表等不可点的行）：与 a.lib-row 同壳 */
    div.lib-row {
        display: block; padding: 14px 12px; margin: 0 -12px;
        border-bottom: 1px solid var(--border); border-radius: var(--radius-sm);
    }
    .lib-row .l1 { display: flex; align-items: baseline; gap: 14px; }
    .lib-row .l1 .title { font-size: 14.5px; font-weight: 500; color: var(--text-primary); }
    .lib-row .l1 .date { margin-left: auto; font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); white-space: nowrap; }
    .lib-row .l2 { display: block; font-size: 12px; color: var(--text-tertiary); margin-top: 3px; }
    .lib-row .l3 { display: block; font-size: 13px; color: var(--text-secondary); margin-top: 5px; }

    /* 搜索结果行（demo .result-row） */
    a.result-row {
        display: block; padding: 14px 12px; margin: 0 -12px;
        border-bottom: 1px solid var(--border); border-radius: var(--radius-sm);
        text-decoration: none; transition: background .15s ease;
    }
    a.result-row:hover { background: var(--surface); }
    .result-row .r-title { display: block; font-size: 14.5px; font-weight: 500; color: var(--text-primary); }
    .result-row .r-meta { display: block; font-size: 12px; color: var(--text-tertiary); margin-top: 3px; }
    .result-row .r-why { display: flex; gap: 6px; margin-top: 7px; flex-wrap: wrap; }
    .why-tag { font-size: 11px; color: var(--accent); background: var(--accent-soft); border-radius: var(--radius-sm); padding: 1px 7px; }
    .result-row .r-snippet { display: block; font-size: 13px; color: var(--text-secondary); margin-top: 6px; }
    .result-row mark, a.doc-row mark { background: var(--accent-soft); color: var(--accent); padding: 0 1px; border-radius: 2px; }
    .result-count { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
    .search-scope { font-size: 12px; color: var(--text-tertiary); margin: 12px 0 24px; }

    /* Empty State（demo § empty-state） */
    .empty-state { text-align: center; padding: 72px 20px; color: var(--text-secondary); }
    .empty-state .e-title { font-size: 16px; font-weight: 500; color: var(--text-primary); margin-bottom: 6px; }
    .empty-state .e-sub { font-size: 13px; margin-bottom: 22px; }

    /* Status Pill（demo 变体；running/done/error/interrupted 为任务状态保留） */
    .pill.progress { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
    .pill.watch { color: var(--warning); border-color: var(--warning); }

    /* 阅读页标题（demo §6：Serif 大标题 + meta 行） */
    .reader-title {
        font-family: var(--font-serif); font-size: 30px; font-weight: 600;
        line-height: 1.4; letter-spacing: 0.01em; color: var(--text-primary);
    }
    .doc-meta-line {
        font-size: 12.5px; color: var(--text-tertiary);
        margin: 14px 0 0; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    }
    .doc-meta-line .sep { color: var(--border-strong); }
    .doc-meta-line .mono { font-family: var(--font-mono); }

    /* ==================== 右侧抽屉（任务面板 / 写回预览共用壳，demo .drawer） ==================== */
    div[data-testid="stElementContainer"]:has(.kb-drawer-marker) { display: none; }
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-marker) {
        position: fixed; top: 0; right: 0; bottom: 0; left: auto;
        width: 400px; max-width: 92vw; height: auto;
        z-index: 1000002;
        background: var(--surface);
        border-left: 1px solid var(--border);
        border-radius: 0;
        box-shadow: -4px 0 18px rgba(20, 24, 28, 0.06);
        padding: 0;
    }
    /* 写回预览抽屉盖在任务抽屉之上 */
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-preview) { z-index: 1000003; }
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-marker) > div[data-testid="stVerticalBlock"] {
        background: transparent; border: none; box-shadow: none; border-radius: 0;
        height: 100vh; overflow-y: auto; padding: 0 22px 22px; gap: 0.35rem;
    }
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-marker):hover {
        border-left: 1px solid var(--border); box-shadow: -4px 0 18px rgba(20, 24, 28, 0.06);
    }
    .drawer-title { font-size: 14px; font-weight: 600; color: var(--text-primary); padding-top: 16px; }
    .drawer-sub { font-size: 12px; color: var(--text-tertiary); margin-top: 2px; }
    .drawer-rule { border-bottom: 1px solid var(--border); margin: 10px -22px 14px; }
    /* 抽屉内按钮：quiet 小按钮 */
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-marker) .stButton button {
        border: 1px solid var(--border-strong); background: transparent;
        color: var(--text-secondary); border-radius: var(--radius-md);
        font-size: 12px; padding: 0.2rem 0.7rem; min-height: 0; box-shadow: none;
    }
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-marker) .stButton button:hover {
        background: var(--surface-subtle); color: var(--text-primary);
    }
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-marker) .stButton button[kind="primary"] {
        background: var(--accent); border-color: var(--accent); color: #fff;
    }
    div[data-testid="stLayoutWrapper"]:has(.kb-drawer-marker) .stButton button[kind="primary"]:hover {
        background: #2A3D50; border-color: #2A3D50; color: #fff;
    }

    /* 任务卡（抽屉内）：feature + pill + 进度条 + 当前步骤 + 用时 */
    .task-card { padding: 12px 0 14px; border-bottom: 1px solid var(--border); }
    .task-card .tc-head { display: flex; align-items: baseline; gap: 10px; }
    .task-card .tc-feature { font-size: 13px; font-weight: 500; color: var(--text-primary); }
    .task-card .tc-bar { height: 3px; background: var(--border); border-radius: 2px; margin: 10px 0 8px; }
    .task-card .tc-bar i { display: block; height: 100%; background: var(--accent); border-radius: 2px; transition: width .3s ease; }
    .task-card .tc-step { font-size: 12px; color: var(--text-secondary); }
    .task-card .tc-time { font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); margin-top: 4px; }
    .task-card .tc-summary { font-size: 12px; color: var(--text-tertiary); margin-top: 4px; }

    /* ==================== Battle（demo §9 移植） ==================== */
    .battle-kv { margin-bottom: 20px; }
    .battle-kv .k { font-size: 10.5px; letter-spacing: 0.1em; color: var(--text-tertiary); text-transform: uppercase; margin-bottom: 4px; }
    .battle-kv .v { font-size: 13px; line-height: 1.7; color: var(--text-primary); }
    .battle-kv .v ul { margin: 0 0 0 16px; }
    .battle-kv .v li { margin-bottom: 5px; font-size: 12.5px; color: var(--text-secondary); }
    .round-banner {
        display: flex; align-items: baseline; gap: 14px;
        border-bottom: 1px solid var(--border); padding-bottom: 14px; margin-bottom: 24px;
    }
    .round-banner .round { font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); letter-spacing: 0.08em; }
    .round-banner .phase { font-size: 15px; font-weight: 600; color: var(--text-primary); }
    .round-banner .phase-flow { margin-left: auto; font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); }
    .battle-entry { margin-bottom: 26px; }
    .battle-entry .who {
        font-size: 11px; letter-spacing: 0.12em; font-weight: 600;
        color: var(--text-tertiary); margin-bottom: 6px;
        display: flex; align-items: center; gap: 10px;
    }
    .battle-entry .who .time { font-family: var(--font-mono); font-weight: 400; letter-spacing: 0; }
    .battle-entry.ai .who { color: var(--accent); }
    .battle-entry .body {
        font-size: 13.5px; line-height: 1.85; color: var(--text-primary);
        border-left: 1px solid var(--border); padding-left: 16px;
    }
    .battle-entry.ai .body { border-left-color: var(--accent); }
    .battle-entry .body p { margin: 0 0 10px; }
    .assumption-row {
        display: flex; align-items: baseline; gap: 10px;
        padding: 8px 0; border-bottom: 1px solid var(--border);
        font-size: 12.5px; color: var(--text-primary);
    }
    .assumption-row .an { font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); width: 18px; flex: 0 0 18px; }
    .assumption-row .st { margin-left: auto; font-size: 11px; white-space: nowrap; }
    .assumption-row .st.ok { color: var(--success); }
    .assumption-row .st.attacked { color: var(--warning); }
    .assumption-row .st.broken { color: var(--danger); }
    .confidence-box { padding: 14px 0; }
    .confidence-box .level { font-size: 20px; font-weight: 600; font-family: var(--font-mono); color: var(--text-primary); }
    .confidence-box .bar { height: 3px; background: var(--border); border-radius: 2px; margin-top: 10px; }
    .confidence-box .bar i { display: block; height: 100%; background: var(--warning); border-radius: 2px; }
    /* 左右栏分隔细线（marker + :has 打在 stColumn 上） */
    div[data-testid="stColumn"]:has(.battle-left-marker) { border-right: 1px solid var(--border); padding-right: 2.2rem; }
    div[data-testid="stColumn"]:has(.battle-right-marker) { border-left: 1px solid var(--border); padding-left: 2.2rem; }
    /* 三栏整页布局：主容器 flex 纵向钉满视口，三栏行吃剩余高度（自适应页头高度），
       左右栏各自框内滚动——整页不滚动（左栏超高会把吸底的操作区顶出视口） */
    div[data-testid="stMainBlockContainer"]:has(.battle-dock-marker) {
        display: flex; flex-direction: column; height: 100vh;
    }
    /* flex 链必须穿过 block-container 与三栏行之间的 stVerticalBlock + stLayoutWrapper，
       否则行吃不到剩余高度（:has 内不能再嵌套 :has，故用直接子选择器锁定外壳） */
    div[data-testid="stMainBlockContainer"]:has(.battle-dock-marker) > div[data-testid="stVerticalBlock"] {
        flex: 1; min-height: 0;
    }
    div[data-testid="stMainBlockContainer"]:has(.battle-dock-marker)
        > div[data-testid="stVerticalBlock"]
        > div[data-testid="stLayoutWrapper"]:has(> div[data-testid="stHorizontalBlock"]) {
        flex: 1; min-height: 0;
    }
    div[data-testid="stHorizontalBlock"]:has(.battle-left-marker) {
        align-items: stretch; flex: 1; min-height: 0;
    }
    div[data-testid="stColumn"]:has(.battle-left-marker),
    div[data-testid="stColumn"]:has(.battle-right-marker) { overflow-y: auto; }
    /* 中栏操作区（按钮 + 输入框）停靠栏底；页底留白收紧避免整页滚动；
       本页横向 padding 收窄，腾出的宽度分给左右两栏（中栏绝对宽度不变） */
    /* 注意：Streamlit 1.58 主容器类名是 .stMain / [data-testid="stMainBlockContainer"]，
       旧的 .main 前缀选择器在此版本不匹配 */
    div[data-testid="stMainBlockContainer"]:has(.battle-dock-marker) { padding-bottom: 1rem !important; padding-left: 2.2rem !important; padding-right: 2.2rem !important; }
    div[data-testid="stColumn"]:has(.battle-dock-marker) > div { display: flex; flex-direction: column; height: 100%; }
    div[data-testid="stColumn"]:has(.battle-dock-marker) > div > div[data-testid="stVerticalBlock"] { flex: 1; min-height: 0; }
    /* 对话区滚动框：标记行隐藏，紧邻其后的容器（1.58 里 st.container 外壳是
       stLayoutWrapper）写死 52vh 高——内容再多也只框内滚动，框本身绝不长高 */
    div[data-testid="stElementContainer"]:has(.battle-msgs-marker) { display: none; }
    div[data-testid="stElementContainer"]:has(.battle-msgs-marker) + div[data-testid="stLayoutWrapper"] {
        flex: none; height: 58vh; min-height: 280px; overflow-y: auto; padding-right: 8px;
    }
    /* 空态（红队已就位 + 先开火）在对话框内垂直居中；有消息时此元素不渲染，不影响消息流 */
    div[data-testid="stElementContainer"]:has(.battle-msgs-marker) + div[data-testid="stLayoutWrapper"]
        div[data-testid="stElementContainer"]:has(.empty-state) { margin: auto 0; }
    div[data-testid="stElementContainer"]:has(.battle-dock-marker) { margin-top: auto; }
    .battle-dock-marker { display: none; }
    /* 写回预览 diff 行（demo .diff-block） */
    .diff-block { border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; margin-top: 10px; }
    .diff-row { display: flex; gap: 12px; align-items: baseline; padding: 9px 14px; font-size: 13px; border-bottom: 1px solid var(--border); background: var(--surface); }
    .diff-row:last-child { border-bottom: none; }
    .diff-row .sign { font-family: var(--font-mono); width: 14px; flex: 0 0 14px; }
    .diff-row.add .sign { color: var(--success); }
    .diff-row.mod .sign { color: var(--warning); }
    .diff-row.del .sign { color: var(--danger); }
    .diff-row .dt { color: var(--text-tertiary); font-size: 12px; margin-left: auto; font-family: var(--font-mono); }
    /* Battle 侧栏长文本：允许任意断行，不挤压中栏 */
    .battle-kv .v { overflow-wrap: anywhere; }
    .assumption-row > span:not(.an):not(.st) { min-width: 0; overflow-wrap: anywhere; }

    /* ==================== Radar（demo §11 移植） ==================== */
    /* 子导航：st.button 文字 tab（session_state 切换，不整页刷新）。
       marker 元素隐藏；紧邻的 columns 行做成 demo .radar-nav 的底线条 + 文字 tab */
    .radar-nav { display: flex; gap: 2px; border-bottom: 1px solid var(--border); margin: 22px 0 34px; }
    a.radar-tab {
        background: none; border: none; text-decoration: none; cursor: pointer;
        font-size: 13px; color: var(--text-tertiary);
        padding: 9px 16px;
        border-bottom: 2px solid transparent; margin-bottom: -1px;
        transition: color .15s ease, border-color .15s ease;
    }
    a.radar-tab:hover { color: var(--text-primary); }
    a.radar-tab.active { color: var(--text-primary); border-bottom-color: var(--accent); font-weight: 500; }
    .stMarkdown a.radar-tab { color: var(--text-tertiary); text-decoration: none; }
    .stMarkdown a.radar-tab:hover { color: var(--text-primary); }
    .stMarkdown a.radar-tab.active { color: var(--text-primary); }
    div[data-testid="stElementContainer"]:has(.radar-nav-marker) { display: none; }
    div[data-testid="stElementContainer"]:has(.radar-nav-marker)
    + div[data-testid="stLayoutWrapper"] div[data-testid="stHorizontalBlock"] {
        gap: 2px; border-bottom: 1px solid var(--border); margin: 22px 0 34px; padding: 0;
    }
    div[data-testid="stElementContainer"]:has(.radar-nav-marker)
    + div[data-testid="stLayoutWrapper"] .stButton button {
        background: none !important; border: none !important; box-shadow: none !important;
        border-radius: 0 !important; min-height: 0; width: auto;
        font-size: 13px; font-weight: 400; color: var(--text-tertiary) !important;
        padding: 9px 2px; border-bottom: 2px solid transparent !important; margin-bottom: -1px;
        transition: color .15s ease, border-color .15s ease;
    }
    div[data-testid="stElementContainer"]:has(.radar-nav-marker)
    + div[data-testid="stLayoutWrapper"] .stButton button:hover {
        color: var(--text-primary) !important; background: none !important;
    }
    div[data-testid="stElementContainer"]:has(.radar-nav-marker)
    + div[data-testid="stLayoutWrapper"] .stButton button[kind="primary"] {
        color: var(--text-primary) !important; font-weight: 500;
        border-bottom-color: var(--accent) !important; background: none !important;
    }
    /* Overview：TODAY 三个 mono 大数字 */
    .today-strip { display: flex; gap: 64px; margin-bottom: 44px; }
    .today-num .n { font-family: var(--font-mono); font-size: 34px; font-weight: 500; color: var(--text-primary); }
    .today-num .l { font-size: 12px; color: var(--text-tertiary); margin-top: 4px; }
    /* 信号行（demo .signal-row）：日期/主题/类型/内容/Why */
    .signal-row {
        display: flex; gap: 14px; align-items: baseline;
        padding: 12px 12px; margin: 0 -12px;
        border-bottom: 1px solid var(--border);
        border-radius: var(--radius-sm);
        transition: background .15s ease;
    }
    .signal-row:hover { background: var(--surface); }
    .signal-row .d { font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); width: 46px; flex: 0 0 46px; }
    .signal-row .theme { font-size: 12.5px; font-weight: 500; color: var(--text-primary); width: 86px; flex: 0 0 86px; }
    .signal-row .type { font-size: 11px; color: var(--text-secondary); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0 7px; flex: 0 0 auto; white-space: nowrap; }
    .signal-row .what { font-size: 13px; color: var(--text-primary); flex: 1; min-width: 0; overflow-wrap: anywhere; }
    .signal-row .why { font-size: 12px; color: var(--text-tertiary); width: 240px; flex: 0 0 240px; overflow-wrap: anywhere; }
    /* 叙事变化块（demo .narrative-block） */
    .narrative-block { border-left: 2px solid var(--accent); padding: 4px 0 4px 18px; margin: 16px 0 8px; }
    .narrative-block .nt { font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 6px; }
    .narrative-block p { font-size: 13px; color: var(--text-secondary); line-height: 1.8; margin: 0; }
    /* 边际变量 chips（demo .var-chips） */
    .var-chips { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
    .var-chip {
        font-size: 12.5px; color: var(--text-secondary);
        border: 1px solid var(--border); border-radius: 999px; padding: 5px 14px;
        background: var(--surface);
    }
    /* Themes 子页：叙事长文（demo .theme-article） */
    .theme-article { max-width: 720px; }
    .theme-article h2.tt { font-family: var(--font-serif); font-size: 26px; font-weight: 600; margin: 0 0 6px; color: var(--text-primary); }
    .theme-article .t-sub { font-family: var(--font-mono); font-size: 12.5px; color: var(--text-tertiary); margin-bottom: 36px; }
    .theme-sec { margin-bottom: 36px; }
    .theme-sec .section-label { margin-bottom: 8px; }
    .theme-sec p { font-family: var(--font-serif); font-size: 15px; line-height: 1.9; color: var(--text-primary); margin: 0 0 12px; }
    .theme-sec li { font-family: var(--font-serif); font-size: 15px; line-height: 1.9; color: var(--text-primary); margin-bottom: 8px; }
    .theme-sec ul { margin-left: 20px; }
    .history-row { display: flex; gap: 18px; padding: 10px 0; border-bottom: 1px solid var(--border); }
    .history-row .hd { font-family: var(--font-mono); font-size: 12px; color: var(--text-tertiary); width: 74px; flex: 0 0 74px; }
    .history-row .hc { font-size: 13px; color: var(--text-secondary); line-height: 1.75; min-width: 0; overflow-wrap: anywhere; }
    /* Signals 子页：删除小按钮列（Streamlit 按钮压成行尾 ×） */
    .sig-del button { min-height: 0 !important; padding: 0 0.4rem !important; font-size: 0.8rem !important; }

    /* ==================== Review（demo §10 移植） ==================== */
    /* 五步进度条 01 PROJECT → 05 COMMIT */
    .steps-flow { display: flex; align-items: center; gap: 0; margin: 30px 0 40px; font-family: var(--font-mono); font-size: 11.5px; }
    .step-node { display: flex; align-items: center; gap: 8px; color: var(--text-tertiary); }
    .step-node .sn { letter-spacing: 0.06em; white-space: nowrap; }
    .step-node.done, .step-node.current { color: var(--text-primary); }
    .step-node.current { font-weight: 600; }
    .step-node .dot { width: 7px; height: 7px; border-radius: 50%; border: 1px solid var(--border-strong); background: var(--surface); }
    .step-node.done .dot { background: var(--accent); border-color: var(--accent); }
    .step-node.current .dot { border-color: var(--accent); background: var(--accent-soft); }
    .step-line { flex: 1; height: 1px; background: var(--border); margin: 0 12px; min-width: 24px; }
    /* 选择字段（demo .review-field） */
    .review-field .fl { font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
    .generate-row { margin-top: 10px; }
    .progress-note { font-size: 12.5px; color: var(--text-secondary); }
    /* 生成后两栏：EXISTING KNOWLEDGE / AI ASSESSMENT（demo .assessment-cols） */
    .assess-block { border-top: 1px solid var(--border-strong); padding: 18px 0; }
    .assess-block h4 { font-size: 13px; font-weight: 600; margin: 0 0 8px; color: var(--text-primary); }
    .assess-block p, .assess-block li { font-size: 13px; color: var(--text-secondary); line-height: 1.8; }
    .assess-block ul { margin-left: 18px; }

    /* ==================== Streamlit 原生残留清理 ==================== */
    /* selectbox：轻底细边小圆角，去掉 baseweb 默认填充 */
    div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
        background-color: var(--surface); border-color: var(--border);
        border-radius: var(--radius-sm); font-size: 13px; min-height: 2.1rem;
    }
    div[data-testid="stSelectbox"] div[data-baseweb="select"] > div:focus-within {
        border-color: var(--border-strong) !important; box-shadow: none !important;
    }
    div[data-testid="stSelectbox"] div[data-baseweb="select"] > div:hover { border-color: var(--border-strong); }
    div[data-testid="stSelectbox"] label { font-size: 12px; color: var(--text-tertiary); }
    /* expander：细线框、无阴影 */
    div[data-testid="stExpander"] details {
        border: 1px solid var(--border); border-radius: var(--radius-md);
        background: var(--surface); box-shadow: none;
    }
    div[data-testid="stExpander"] summary { font-size: 13px; color: var(--text-secondary); }
    div[data-testid="stExpander"] summary:hover { color: var(--text-primary); }
    /* chat_input：细边圆角 */
    div[data-testid="stChatInput"] textarea {
        background-color: var(--surface); border-color: var(--border-strong);
        border-radius: var(--radius-md); font-size: 13.5px;
    }
    div[data-testid="stChatInput"] textarea:focus { border-color: var(--accent); box-shadow: none; }
    /* alert（st.info/success/error）：去厚重底色；内层 stAlertContainer 才是真正的
       彩色背景/文字载体，按 kind 换成设计 token 的低饱和色调 */
    div[data-testid="stAlert"] { background: var(--surface-subtle); border: 1px solid var(--border); border-radius: var(--radius-md); color: var(--text-secondary); }
    div[data-testid="stAlertContainer"] { background: var(--surface-subtle) !important; border-radius: var(--radius-md); }
    div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentSuccess"]) { background: rgba(101, 117, 104, 0.08) !important; }
    div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentError"]) { background: rgba(138, 93, 93, 0.08) !important; }
    div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentWarning"]) { background: rgba(133, 118, 93, 0.08) !important; }
    div[data-testid="stAlertContentSuccess"] { color: var(--success) !important; }
    div[data-testid="stAlertContentError"] { color: var(--danger) !important; }
    div[data-testid="stAlertContentWarning"] { color: var(--warning) !important; }
    div[data-testid="stAlertContentInfo"] { color: var(--text-secondary) !important; }
    /* primary 按钮 disabled 态：避免深色实心块（demo quiet 语义） */
    .stButton button[kind="primary"]:disabled {
        background: var(--surface-subtle) !important;
        border-color: var(--border) !important;
        color: var(--text-tertiary) !important;
    }
    /* TOC 链接：压过 stMarkdown 默认链接色/下划线 */
    .stMarkdown a.toc-link { color: var(--text-tertiary); text-decoration: none; }
    .stMarkdown a.toc-link:hover { color: var(--text-primary); }
    .stMarkdown a.toc-link.active { color: var(--text-primary); }
    /* 隐藏原生 Deploy 按钮（右上角菜单保留） */
    div[data-testid="stAppDeployButton"], .stAppDeployButton { display: none !important; }
    /* round-banner 防折行 */
    .round-banner .round, .round-banner .phase { white-space: nowrap; }
    .round-banner .phase-flow { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    /* 全宽页面（reader/battle/radar 等）：page-wide-marker 撑开 block-container */
    div[data-testid="stElementContainer"]:has(.page-wide-marker) { display: none; }
    div[data-testid="stMainBlockContainer"]:has(.page-wide-marker) { max-width: none; }
</style>
""", unsafe_allow_html=True)


def section_header(text):
    st.markdown(f"<div class='section-header'>{text}</div>", unsafe_allow_html=True)


def sidebar_section(text):
    st.markdown(f"<div class='sb-section'>{text}</div>", unsafe_allow_html=True)


# ==================== 最近访问记录（data/view_history.json） ====================

HISTORY_FILE = os.path.join(DATA_DIR, "view_history.json")
HISTORY_LIMIT = 50


def _load_history():
    """读取最近访问记录 [{path, title, category, ts}]；损坏/缺失返回 []。"""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _record_view(doc):
    """记录一次文档访问：同 path 去重提到最前，上限 HISTORY_LIMIT 条。"""
    if not doc or not doc.get("path"):
        return
    entry = {
        "path": doc["path"],
        "title": doc.get("title") or doc["name"].replace(".md", ""),
        "category": doc.get("category", ""),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    history = [e for e in _load_history() if e.get("path") != doc["path"]]
    history.insert(0, entry)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[:HISTORY_LIMIT], f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def open_doc(doc, rerun=True):
    """打开文档的唯一入口：设置 selected_doc + view_mode + 记录最近访问。"""
    st.session_state.selected_doc = doc
    st.session_state.view_mode = "doc"
    _record_view(doc)
    if rerun:
        st.rerun()


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


# Session state
if "index" not in st.session_state:
    with st.spinner("正在加载知识库索引..."):
        st.session_state.index = load_index()

if "selected_doc" not in st.session_state:
    st.session_state.selected_doc = None

if "view_mode" not in st.session_state:
    st.session_state.view_mode = "home"

if "zen" not in st.session_state:
    st.session_state.zen = False

if "search_query" not in st.session_state:
    st.session_state.search_query = ""

if "selected_category" not in st.session_state:
    st.session_state.selected_category = None

# 启动时载入本机持久化的 API Key（填一次，之后自动生效）
if "user_api_key" not in st.session_state:
    st.session_state.user_api_key = _load_local_key()

# 启动时载入本机持久化的厂家/模型/自定义端点（同上，填一次自动生效）
if "user_provider" not in st.session_state:
    _p = _load_local(LOCAL_PROVIDER_FILE)
    st.session_state.user_provider = _p if _p in PROVIDERS else "moonshot"

if "user_model" not in st.session_state:
    st.session_state.user_model = _load_local(LOCAL_MODEL_FILE)

if "user_base_url" not in st.session_state:
    st.session_state.user_base_url = _load_local(LOCAL_BASE_URL_FILE)

# ---- URL 路由：展示页的 HTML 行输出 <a href="?doc=...">/<a href="?cat=...">（demo 同款
# 整行可点），点击后整页刷新带 query params 重新进脚本，在这里消费并落到 session_state ----
_qp_doc = st.query_params.get("doc", "")
_qp_cat = st.query_params.get("cat", "")
_qp_nav = st.query_params.get("nav", "")
_qp_rtab = st.query_params.get("rtab", "")
if _qp_doc or _qp_cat or _qp_nav:
    st.query_params.clear()
    if _qp_doc:
        _d = get_document_by_path(st.session_state.index, _qp_doc)
        if _d:
            st.session_state.selected_doc = _d
            st.session_state.view_mode = "doc"
            _record_view(_d)
    elif _qp_cat and _qp_cat in st.session_state.index.get("categories", {}):
        st.session_state.selected_category = _qp_cat
        st.session_state.view_mode = "category"
        st.session_state.selected_doc = None
    elif _qp_nav in ("home", "battle", "radar", "compare", "ingest", "search"):
        st.session_state.view_mode = _qp_nav
        st.session_state.selected_doc = None
        st.session_state.selected_category = None
        # Radar 子导航（radar.py 的 a.radar-tab 链接）：?nav=radar&rtab=themes
        if _qp_nav == "radar" and _qp_rtab in (
                "overview", "signals", "themes", "variables", "reports", "sources"):
            st.session_state.radar_tab = _qp_rtab


def doc_href(doc):
    """文档行的 demo 式跳转链接（整页刷新 + 上方 query_params 路由消费）。"""
    return "?doc=" + _urlquote(doc["path"])


def cat_href(cat_key):
    return "?cat=" + _urlquote(cat_key)


def _crumb_html(items):
    """面包屑回跳索引：items = [(label, href|None), ...]，href=None 即当前页。
    例：知识 › 09 被投基金 › 投资思维模型：鼎晖创新与成长基金（VGC）"""
    parts = []
    for label, href in items:
        if href:
            parts.append(f"<a href='{href}' target='_self'>{_esc(label)}</a>")
        else:
            parts.append(f"<span class='crumb-cur'>{_esc(label)}</span>")
    return ("<div class='crumb'>" + "<span class='crumb-sep'>›</span>".join(parts)
            + "</div>")


_STATUS_PILL = {"推进中": "progress", "跟踪中": "watch", "已归档": "done"}


def status_pill(status):
    """demo 式状态 pill：推进中=progress(accent) / 跟踪中=watch / 已归档=done。"""
    cls = _STATUS_PILL.get(status or "", "")
    return (f"<span class='pill {cls}'>{html.escape(status)}</span>"
            if status else "")


def _esc(s):
    return html.escape(str(s or ""))


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


def doc_row_html(doc, show_category=True):
    """demo .doc-row：标题 + meta + 日期，整行 <a> 可点（query_params 路由）。"""
    title = doc.get("title") or doc["name"].replace(".md", "")
    meta_parts = []
    if show_category and doc.get("category") and doc["category"] != "其他":
        meta_parts.append(doc["category"])
    if doc.get("track") and doc["track"] != "未分类":
        meta_parts.append(doc["track"])
    if doc.get("status"):
        meta_parts.append(doc["status"])
    meta = " · ".join(meta_parts)
    date = doc.get("last_updated") or str(doc.get("modified", ""))[:10]
    return (f"<a class='doc-row' href='{doc_href(doc)}' target='_self'>"
            f"<span class='title'>{_esc(title)}</span>"
            + (f"<span class='meta'>{_esc(meta)}</span>" if meta else "")
            + f"<span class='date'>{_esc(date)}</span></a>")


def lib_row_html(doc, show_category=False):
    """demo .lib-row：标题+日期 / meta 行 / 摘要行，整行 <a> 可点。"""
    title = doc.get("title") or doc["name"].replace(".md", "")
    date = str(doc.get("modified", ""))[:10]
    meta = []
    if show_category and doc.get("category"):
        meta.append(doc["category"])
    if doc.get("track") and doc["track"] != "未分类":
        meta.append(doc["track"])
    if doc.get("status"):
        meta.append(doc["status"])
    if doc.get("last_updated"):
        meta.append(f"Updated {doc['last_updated']}")
    l2 = f"<span class='l2'>{_esc(' · '.join(meta))}</span>" if meta else ""
    sub = (doc.get("subtitle") or "")[:120]
    l3 = f"<span class='l3'>{_esc(sub)}</span>" if sub else ""
    # 注意：<a> 内只能放 inline 元素（span）——st.markdown 的 markdown-it 会把
    # 块级 <div> 从行内 HTML 段落里拆出来，行结构会散架（demo 对照实测踩过）
    return (f"<a class='lib-row' href='{doc_href(doc)}' target='_self'>"
            f"<span class='l1'><span class='title'>{_esc(title)}</span>"
            f"<span class='date'>{_esc(date)}</span></span>{l2}{l3}</a>")


def change_row_html(doc):
    """demo .change-row：mono 日期 + 标题 + 归属（分类/赛道）。"""
    title = doc.get("title") or doc["name"].replace(".md", "")
    d = str(doc.get("modified", ""))[5:10]
    w = doc.get("category", "")
    if doc.get("track") and doc["track"] != "未分类":
        w += f" · {doc['track']}"
    return (f"<a class='change-row' href='{doc_href(doc)}' target='_self'>"
            f"<span class='d'>{_esc(d)}</span>"
            f"<span class='t'>{_esc(title)}</span>"
            f"<span class='w'>{_esc(w)}</span></a>")


def _snippet(content, query, width=140):
    """正文命中片段：关键词前后各取 width/2，<mark> 高亮（demo .r-snippet）。"""
    idx = content.lower().find(query.lower())
    if idx < 0:
        return ""
    start = max(0, idx - width // 2)
    text = _esc(content[start: idx + len(query) + width // 2].replace("\n", " "))
    q = _esc(query)
    if q:
        text = re.sub(re.escape(q), lambda m: f"<mark>{m.group(0)}</mark>",
                      text, flags=re.IGNORECASE)
    return ("…" if start > 0 else "") + text + "…"


def result_row_html(doc, query):
    """demo .result-row：标题 / meta / 命中位置 why-tags / 正文片段高亮。"""
    title = doc.get("title") or doc["name"].replace(".md", "")
    q = query.lower()
    tags = []
    if q in title.lower():
        tags.append("标题")
    if q in doc.get("project", "").lower():
        tags.append("项目")
    if q in doc.get("category", "").lower():
        tags.append("分类")
    if q in doc.get("content", "").lower():
        tags.append("正文")
    why = "".join(f"<span class='why-tag'>{t}</span>" for t in tags)
    meta = " · ".join(p for p in (doc.get("category", ""),
                                  doc.get("track", "") if doc.get("track") != "未分类" else "",
                                  doc.get("last_updated") or str(doc.get("modified", ""))[:10]) if p)
    snippet = _snippet(doc.get("content", ""), query)
    return (f"<a class='result-row' href='{doc_href(doc)}' target='_self'>"
            f"<span class='r-title'>{_esc(title)}</span>"
            f"<span class='r-meta'>{_esc(meta)}</span>"
            f"<span class='r-why'>{why}</span>"
            + (f"<span class='r-snippet'>{snippet}</span>" if snippet else "")
            + "</a>")


FEATURE_LABELS = {
    "battle": "论文之战",
    "review": "新项目评审",
    "radar": "投资雷达",
    "ingest": "文件归档",
    "tech": "技术提取",
    "unknown": "其他",
}


def _read_costs():
    """读取 data/ai_costs.jsonl 全部记录；损坏/缺失返回 []。"""
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
    return recs


def _today_costs():
    """今日 AI 花费聚合：(调用次数, 总花费)。"""
    today = datetime.now().date().isoformat()
    recs = [r for r in _read_costs() if str(r.get("ts", ""))[:10] == today]
    return len(recs), sum(r.get("cost", 0) for r in recs)


def _week_costs():
    """近 7 天 AI 花费：(recs, 总花费, 总 tokens, 按功能聚合)。控制台页展示用。"""
    recs = _read_costs()
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
    """v2 Research Home（§4）：Hero → 大搜索 → RECENT → COLLECTIONS → RECENT CHANGES。
    展示层全部为 demo 同款 HTML（st.markdown 直出），行点击走 query_params 路由；
    st.button 只留给真正的动作（刷新索引）。"""
    index = st.session_state.index
    categories = index.get("categories", {})
    indexed_at = str(index.get("indexed_at", ""))
    indexed_short = indexed_at[5:16].replace("T", " ") if len(indexed_at) >= 16 else "—"

    # 与 reader/battle/radar 一致：撑开主区，消除两侧留白
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)

    # ---- Hero：小编辑标 + 大标题 + mono 索引信息行（右侧刷新索引小按钮） ----
    _hero, _act = st.columns([8, 1], vertical_alignment="bottom")
    with _hero:
        st.markdown(
            "<div class='kb-hero'>"
            "<div class='section-label'>研究主页</div>"
            "<h1>一级投研知识库</h1>"
            f"<div class='index-line'>{index['total_documents']} 篇文档 · "
            f"{len(categories)} 个合集 · 索引于 {_esc(indexed_short)}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with _act:
        if st.button("刷新索引", key="home_refresh"):
            with st.spinner("重新索引中..."):
                st.session_state.index = build_index(force=True)
            st.rerun()

    # ---- 大搜索胶囊（demo .big-search）：跳到搜索页，实际输入在 Header 搜索框 ----
    st.markdown(
        "<a class='big-search' href='?nav=search' target='_self'>"
        "<span>搜索文档、项目、赛道、概念……</span>"
        "<span class='kbd'>顶栏 ↑</span></a>",
        unsafe_allow_html=True,
    )

    # ---- RECENT：最近访问（view_history，demo .doc-row 整行可点） ----
    rows = []
    for entry in _load_history():
        if len(rows) >= 5:
            break
        doc = get_document_by_path(index, entry.get("path", ""))
        if not doc:
            continue  # 文档已被移动/删除：跳过死记录
        rows.append(doc_row_html(doc))
    st.markdown("<div class='section-label'>近期阅读</div>", unsafe_allow_html=True)
    if rows:
        st.markdown("".join(rows), unsafe_allow_html=True)
    else:
        st.markdown("<div class='meta-line' style='padding:0.6rem 0.2rem'>"
                    "还没有阅读记录，打开任意文档后会出现在这里。"
                    "</div>", unsafe_allow_html=True)

    # ---- COLLECTIONS：编号 + 名称 + 一句话描述 + 数量，两列 editorial 索引 ----
    col_rows = []
    for cat_key in sorted(categories.keys()):
        cat = categories[cat_key]
        num = cat_key.split("_")[0]
        col_rows.append(
            f"<a class='collection-row' href='{cat_href(cat_key)}' target='_self'>"
            f"<span class='num'>{_esc(num)}</span>"
            f"<span class='cname'>{_esc(cat['name'])}</span>"
            f"<span class='cdesc'>{_esc(cat['description'])}</span>"
            f"<span class='ccount'>{cat['count']}</span></a>")
    st.markdown(
        "<div class='home-section'><div class='section-label'>知识合集</div>"
        f"<div class='collections-grid'>{''.join(col_rows)}</div></div>",
        unsafe_allow_html=True)

    # ---- RECENT CHANGES：按修改时间取 10 条，demo .change-row ----
    recent_docs = sorted(
        index.get("documents", []),
        key=lambda x: x.get("modified", ""),
        reverse=True,
    )[:10]
    ch_rows = "".join(change_row_html(d) for d in recent_docs)
    st.markdown(
        "<div class='home-section'><div class='section-label'>最近更新</div>"
        f"{ch_rows}</div>",
        unsafe_allow_html=True)

def render_category_view(cat_key):
    """分类列表页（demo §5 Library）：页头 + control-bar 过滤 + .lib-row 列表。"""
    index = st.session_state.index
    cat = index.get("categories", {}).get(cat_key)
    if not cat:
        st.error("分类不存在")
        return

    num = cat_key.split("_")[0]
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)
    st.markdown(_crumb_html([("知识", "?nav=home"), (cat["name"], None)]),
                unsafe_allow_html=True)
    st.markdown(
        f"<div class='section-label' style='margin-top:0.4rem'>Library · {_esc(num)}</div>"
        f"<div class='page-title'>{_esc(cat['name'])}</div>"
        f"<div class='page-sub'>{_esc(cat['description'])} · 共 {cat['count']} 篇</div>",
        unsafe_allow_html=True)

    docs = cat["documents"]
    if cat_key == "02_deals":
        # control-bar：上下细线夹住的过滤条（demo §5）
        st.markdown("<div style='border-top:1px solid var(--border);margin:24px 0 0'></div>",
                    unsafe_allow_html=True)
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
            st.markdown(f"<div class='result-count' style='margin-top:1.9rem'>"
                        f"{len(docs)} / {cat['count']} 篇</div>",
                        unsafe_allow_html=True)
        st.markdown("<div style='border-bottom:1px solid var(--border);margin-bottom:8px'></div>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<div style='margin-top:24px'></div>", unsafe_allow_html=True)

    st.markdown("".join(lib_row_html(d) for d in docs), unsafe_allow_html=True)


def render_industry_cognition():
    """行业认知（01_industry）嵌入区块：迁移到新项目评审页、评审功能下方。"""
    index = st.session_state.index
    cat = index.get("categories", {}).get("01_industry")
    if not cat:
        return

    st.divider()
    st.markdown("<div class='section-header'>② 行业认知</div>", unsafe_allow_html=True)
    st.markdown(f"<div class='meta-line'>{cat['description']} · 共 {cat['count']} 篇</div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom: 0.8rem'></div>", unsafe_allow_html=True)
    st.markdown("".join(doc_row_html(d, show_category=False) for d in cat["documents"]),
                unsafe_allow_html=True)


def render_search_results(query):
    """搜索页（demo §7）：页头 + scope 行 + result-count + .result-row 列表。"""
    index = st.session_state.index
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)
    st.markdown(
        "<div class='section-label' style='margin-top:0.4rem'>Search</div>"
        "<div class='page-title'>搜索</div>",
        unsafe_allow_html=True)

    if not query:
        st.markdown(
            "<div class='empty-state'><div class='e-title'>输入关键词开始搜索</div>"
            "<div class='e-sub'>在页面顶部 Header 的搜索框输入 · "
            "Search scope: Title · Project · Category · Content</div></div>",
            unsafe_allow_html=True)
        return

    results = search_documents(index, query)
    st.markdown(
        "<div class='search-scope' style='margin-top:26px'>"
        "Search scope: Title · Project · Category · Content</div>"
        f"<div class='result-count'>{len(results)} results for &ldquo;{_esc(query)}&rdquo;</div>",
        unsafe_allow_html=True)

    if not results:
        st.markdown(
            "<div class='empty-state'><div class='e-title'>未找到匹配结果</div>"
            "<div class='e-sub'>尝试其他关键词，或检查拼写</div></div>",
            unsafe_allow_html=True)
        return

    st.markdown("".join(result_row_html(d, query) for d in results[:40]),
                unsafe_allow_html=True)


# ==================== 文档阅读页（v2 §6：三栏 Reading Workspace） ====================

# 目录滚动跟随：高亮当前阅读到的章节，并让目录自身滚动把高亮项保持在可见区。
# JS 查的是 parent document 的 a.toc-link，与目录所在位置无关（原 sidebar 逻辑原样搬到右栏）。
_TOC_SPY_JS = """<script>
(function () {
  var P = window.parent, D = P.document;
  if (P.__tocSpyCleanup) { try { P.__tocSpyCleanup(); } catch (e) {} }
  var links = [], anchors = [], current = -1;

  function collect() {
    links = Array.prototype.slice.call(D.querySelectorAll('a.toc-link'));
    anchors = links.map(function (l) {
      var href = l.getAttribute('href') || '';
      return href.charAt(0) === '#' ? D.getElementById(href.slice(1)) : null;
    });
    return links.length > 0 && anchors.some(Boolean);
  }

  function keepInView(link) {
    if (!link) return;
    var el = link.parentElement;
    while (el && el !== D.body) {
      var ov = P.getComputedStyle(el).overflowY;
      if ((ov === 'auto' || ov === 'scroll') && el.scrollHeight > el.clientHeight + 4) {
        var c = el.getBoundingClientRect(), r = link.getBoundingClientRect();
        if (r.top < c.top + 48) el.scrollTop += (r.top - c.top) - 48;
        else if (r.bottom > c.bottom - 48) el.scrollTop += (r.bottom - c.bottom) + 48;
        return;
      }
      el = el.parentElement;
    }
  }

  function update() {
    // Streamlit 任意交互都重渲染主区 DOM（本组件 iframe 不变、脚本存活）：
    // 旧节点引用随之失效——发现引用失效即重新收集，不依赖一次性就绪
    if (!anchors.length || !D.contains(anchors[0]) || !D.contains(links[0])) {
      current = -1;
      if (!collect()) return;
    }
    // 阅读线定在视口顶部往下 140px（避开顶部固定导航）：最后一条越过阅读线的
    // 标题即当前章节；还没翻到任何标题时高亮第一项
    var idx = -1;
    for (var i = 0; i < anchors.length; i++) {
      var a = anchors[i];
      if (a && a.getBoundingClientRect().top <= 140) idx = i;
    }
    if (idx < 0) idx = 0;
    if (idx === current) return;
    current = idx;
    for (var j = 0; j < links.length; j++) links[j].classList.toggle('active', j === idx);
    keepInView(links[idx]);
  }

  var scheduled = false;
  function onScroll() {
    if (scheduled) return;
    scheduled = true;
    P.requestAnimationFrame(function () { scheduled = false; update(); });
  }

  // 双通道：scroll 事件即时响应（rAF 节流）+ 常驻轮询自愈。
  // 为什么必须有轮询：大文档 base64 图片渲染慢、布局随图片加载持续变化，
  // 「等 DOM 就绪再挂监听」的模式在慢渲染下会超时放弃，高亮从此冻住；
  // 轮询每次只读几个 getBoundingClientRect，开销可忽略，且任何重渲染后自动恢复。
  P.addEventListener('scroll', onScroll, true);
  P.addEventListener('resize', onScroll);
  var timer = P.setInterval(update, 300);
  update();

  P.__tocSpyCleanup = function () {
    P.clearInterval(timer);
    P.removeEventListener('scroll', onScroll, true);
    P.removeEventListener('resize', onScroll);
  };
})();
</script>"""

# 2px 阅读进度条：fixed 顶部、accent 色，监听 stMain 滚动更新宽度。
# 离开阅读页时由主流程注入的清理脚本移除（见主界面 router）。
_READING_PROGRESS_JS = """<script>
(function () {
  var P = window.parent, D = P.document;
  if (P.__kbProgressCleanup) { try { P.__kbProgressCleanup(); } catch (e) {} }
  var bar = D.getElementById('kb-reading-progress');
  if (!bar) {
    bar = D.createElement('div');
    bar.id = 'kb-reading-progress';
    bar.style.cssText = 'position:fixed;top:0;left:0;height:2px;width:0;background:#354A5F;'
      + 'z-index:1000002;transition:width 100ms linear;pointer-events:none;';
    D.body.appendChild(bar);
  }
  function update() {
    var el = D.querySelector('[data-testid="stMain"]') || D.documentElement;
    var max = el.scrollHeight - el.clientHeight;
    var pct = max > 0 ? Math.min(100, Math.max(0, (el.scrollTop / max) * 100)) : 0;
    bar.style.width = pct + '%';
  }
  P.addEventListener('scroll', update, true);
  P.addEventListener('resize', update);
  var timer = P.setInterval(update, 400);
  update();
  P.__kbProgressCleanup = function () {
    P.clearInterval(timer);
    P.removeEventListener('scroll', update, true);
    P.removeEventListener('resize', update);
  };
})();
</script>"""

# Zen Reading Mode（§6.1）：隐藏原生 sidebar，正文列收窄居中
_ZEN_CSS = """<style>
section[data-testid="stSidebar"] { display: none !important; }
.main .block-container { max-width: 800px; }
</style>"""


def _doc_meta_html(doc):
    """demo §6 .doc-meta-line：分类 · 赛道 · 状态 pill · Updated（sep 点分隔）。"""
    parts = []
    cat = doc.get("category", "其他")
    num = doc.get("category_key", "").split("_")[0]
    if cat:
        parts.append(f"<span>{_esc((num + ' ' + cat) if num else cat)}</span>")
    if doc.get("track") and doc["track"] != "未分类":
        parts.append(f"<span>{_esc(doc['track'])}</span>")
    if doc.get("status"):
        parts.append(status_pill(doc["status"]))
    date = doc.get("last_updated") or str(doc.get("modified", ""))[:10]
    if date:
        parts.append(f"<span class='mono'>Updated {_esc(date)}</span>")
    if doc.get("project"):
        parts.append(f"<span>{_esc(doc['project'])}</span>")
    return ("<div class='doc-meta-line'>"
            + "<span class='sep'>·</span>".join(parts) + "</div>")


def _render_doc_content(doc):
    """正文渲染（三栏 / Zen 共用）：Serif 阅读排版 + 章节锚点 + details 折叠块。"""
    # Serif 阅读排版标记：CSS 据此把正文列切成 Editorial 阅读样式
    st.markdown('<div class="doc-body-marker"></div>', unsafe_allow_html=True)
    content = doc.get("content", "")
    # 去掉正文开头与页首重复的 # 标题行
    lines = content.split("\n")
    if lines and lines[0].strip().startswith("# "):
        content = "\n".join(lines[1:]).lstrip("\n")
    if content:
        sections, details_blocks = _processed_sections(
            doc["path"], str(doc.get("modified", "")), content)
        for sec in sections:
            if sec.startswith('<a id="'):
                cut = sec.index("</a>") + 4
                st.markdown(sec[:cut], unsafe_allow_html=True)
                st.markdown(sec[cut:])
            else:
                st.markdown(sec)
        for blk in details_blocks:
            st.markdown(blk, unsafe_allow_html=True)
    else:
        st.warning("文档内容为空")


@st.cache_data(show_spinner=False, max_entries=32)
def _processed_sections(path, modified, content):
    """正文加工管线（图片内联 base64 + ASCII 表格包裹 + details 抽出 + 分节）。
    每次 rerun 重复执行很贵（图多的文档单篇 ~150ms 且重新生成数 MB base64），
    按 (path, modified) 缓存：文件变更后 modified 变化，缓存自动失效。"""
    # 本地图集引用（../assets/img/…）内联成 base64：浏览器解析不到应用外的
    # 相对路径，直接渲染会破图；含括号的目录名也会截断 markdown 图片语法
    content = inline_local_images(
        content, os.path.dirname(os.path.join(KNOWLEDGE_DIR, path)))
    content = wrap_ascii_tables(content)  # 纯文字/ASCII 表格包成围栏，等宽渲染
    # <details> 折叠块（原文全文）抽出：其标题不进目录，正文后单独渲染
    content_main, details_blocks = split_details_blocks(content)
    sections, _ = parse_sections(content_main)
    return sections, details_blocks


def _render_doc_footer(doc, index):
    """正文之后：上一篇/下一篇（同分类内）+ 文件信息 + 回到顶部。"""
    st.divider()
    cat_key = doc.get("category_key")
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
                            open_doc(prev_doc)
                with nc:
                    if next_doc:
                        st.markdown("<div class='pn-label'>下一篇 →</div>", unsafe_allow_html=True)
                        ntitle = next_doc.get("title") or next_doc["name"].replace(".md", "")
                        if st.button(ntitle, key="next_doc", type="tertiary"):
                            open_doc(next_doc)
                st.divider()

    with st.expander("文件信息"):
        st.markdown(f"- **文件名**: `{doc['name']}`")
        st.markdown(f"- **路径**: `{doc['path']}`")
        st.markdown(f"- **大小**: {format_size(doc['size'])}")
        st.markdown(f"- **修改时间**: {doc['modified'][:19]}")

    st.markdown("<div style='text-align:center; margin-top:1.2rem'>"
                "<a class='back-top' href='#page-top' target='_self'>↑ 回到顶部</a></div>",
                unsafe_allow_html=True)


def _render_context_rail(doc, index):
    """右栏第一段：文档信息（回答「我现在在哪里」）。Related/Actions 拆到 _render_related_rail，
    好把目录（Contents）插在文档信息之后、相关文档之前。"""
    st.markdown("<div class='rail-label'>文档信息</div>", unsafe_allow_html=True)
    fields = [
        ("分类", doc.get("category", "其他")),
        ("赛道", doc.get("track") if doc.get("track") != "未分类" else ""),
        ("状态", doc.get("status_detail") or doc.get("status")),
        ("更新", doc.get("last_updated") or str(doc.get("modified", ""))[:10]),
        ("项目", doc.get("project")),
    ]
    rows = "".join(
        f"<div class='cr-block'><div class='cr-k'>{k}</div>"
        f"<div class='cr-v'>{html.escape(str(v))}</div></div>"
        for k, v in fields if v)
    st.markdown(rows or "<div class='meta-line'>无元信息</div>", unsafe_allow_html=True)


def _render_related_rail(doc, index):
    """右栏第三段（目录之后）：相关文档 + 操作。"""
    related = get_related_documents(index, doc)
    if related:
        st.markdown("<div class='rail-label'>相关文档</div>", unsafe_allow_html=True)
        for rdoc in related:
            rtitle = rdoc.get("title") or rdoc["name"].replace(".md", "")
            if st.button(rtitle, key=f"rail_rel_{rdoc['path']}", type="tertiary",
                         use_container_width=True):
                open_doc(rdoc)

    st.markdown("<div class='rail-label'>操作</div>", unsafe_allow_html=True)
    if st.button("✎ 在 VSCode 中打开", key="rail_vscode", type="tertiary",
                 use_container_width=True):
        open_in_vscode(doc["path"])
    if doc.get("category_key") == "02_deals":
        if st.button("发起新项目评审 →", key="rail_review", type="tertiary",
                     use_container_width=True):
            st.session_state.review_preselect_path = doc["path"]
            st.session_state.view_mode = "compare"
            st.session_state.selected_doc = None
            st.rerun()
    if st.button("发起论文之战 →", key="rail_battle", type="tertiary",
                 use_container_width=True):
        # battle 页 selectbox widget 状态粘性会让预选失效，需一并清掉（对齐 review 串联跳转）
        st.session_state.battle_doc_path = doc["path"]
        st.session_state.pop("battle_doc", None)
        st.session_state.pop("battle_msgs", None)
        st.session_state.view_mode = "battle"
        st.session_state.selected_doc = None
        st.rerun()


def _render_toc_rail(doc):
    """右栏 TOC（§6.2）：h2/h3 目录 + 滚动跟随高亮（原 sidebar 目录逻辑整体搬入）。"""
    # 剔除 <details> 折叠块（原文全文）：其标题是原始文档的目录，不是整理产物的章节
    _, toc = parse_sections(split_details_blocks(doc.get("content", ""))[0])
    if not toc:
        return
    st.markdown("<div class='rail-label'>目录</div>", unsafe_allow_html=True)
    toc_html = "".join(
        f"<a class='toc-link {'toc-h3' if level == 3 else 'toc-h2'}' href='#{anchor}' target='_self'>{text}</a>"
        for level, text, anchor in toc[:30]
    )
    st.markdown(toc_html, unsafe_allow_html=True)
    components.html(_TOC_SPY_JS, height=0)


def render_document_detail(doc):
    if not doc:
        st.info("请选择一篇文档")
        return

    title = doc.get("title") or doc["name"].replace(".md", "")
    index = st.session_state.index
    zen = bool(st.session_state.get("zen", False))

    # 打开新文档时滚动回顶部
    if st.session_state.get("_last_doc_path") != doc["path"]:
        st.session_state["_last_doc_path"] = doc["path"]
        components.html("<script>window.parent.document.querySelector('[data-testid=\"stMain\"]').scrollTo(0, 0);</script>", height=0)

    st.markdown('<a id="page-top"></a>', unsafe_allow_html=True)
    components.html(_READING_PROGRESS_JS, height=0)

    if zen:
        # Zen Reading Mode：只渲染居中正文列（720-800px），不渲染三栏
        st.markdown(_ZEN_CSS, unsafe_allow_html=True)
        _t, _z = st.columns([12, 1.6], vertical_alignment="bottom")
        with _t:
            st.markdown(f"<div class='reader-title'>{_esc(title)}</div>", unsafe_allow_html=True)
        with _z:
            if st.button("退出禅读", key="zen_off"):
                st.session_state.zen = False
                st.rerun()
        st.markdown(_doc_meta_html(doc), unsafe_allow_html=True)
        st.divider()
        _render_doc_content(doc)
        _render_doc_footer(doc, index)
        return

    # 两栏 Reading Workspace：宽正文列 + 右侧合并 Rail（Contents / Context / Related / Actions）
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)
    body_col, rail_col = st.columns([7.4, 2.6], gap="large")

    # 右栏先渲染：它很轻量（毫秒级），先出现；正文含 base64 图片等大 payload，
    # 若先渲染正文，右栏要等正文的 delta 全部到达后才开始显示，看起来就是「右栏很慢」
    with rail_col:
        # rail-marker：CSS 据此让右栏 sticky + 自身滚动（TOC spy 的 keepInView 只滚右栏，
        # 不再抢主页面滚动条）；文档背景（Context）排在目录（Contents）上方
        st.markdown("<div class='rail-marker'></div>", unsafe_allow_html=True)
        _render_context_rail(doc, index)
        _render_toc_rail(doc)
        _render_related_rail(doc, index)

    with body_col:
        # 面包屑回跳索引：知识 › 分类 › 当前文档
        _ck = doc.get("category_key", "")
        _cat = index.get("categories", {}).get(_ck) if _ck else None
        _crumbs = [("知识", "?nav=home")]
        if _cat:
            _crumbs.append((_cat["name"], cat_href(_ck)))
        _crumbs.append((title, None))
        st.markdown(_crumb_html(_crumbs), unsafe_allow_html=True)

        _t, _z = st.columns([12, 1], vertical_alignment="bottom")
        with _t:
            st.markdown(f"<div class='reader-title'>{_esc(title)}</div>", unsafe_allow_html=True)
        with _z:
            if st.button("禅读", key="zen_on", type="tertiary",
                         help="禅模式：只保留标题与正文"):
                st.session_state.zen = True
                st.rerun()
        st.markdown(_doc_meta_html(doc), unsafe_allow_html=True)
        st.divider()
        _render_doc_content(doc)
        _render_doc_footer(doc, index)


# ==================== 任务抽屉（右侧 fixed 滑出面板，demo .drawer 形态） ====================
# 原独立「控制台」页已移除：任务状态改为 Header「● N tasks」开关的右侧抽屉，
# 只放实时任务卡（进度条 + 当前步骤 + 已用时/ETA）；AI 成本挪到 sidebar Settings。

_HAS_FRAGMENT = hasattr(st, "fragment")

_STATUS_LABEL = {"running": "运行中", "done": "已完成",
                 "error": "失败", "interrupted": "已中断"}

# 抽屉里「刚结束」任务的展示窗口（秒）：超出后只留 running / interrupted
_DRAWER_RECENT_SECS = 6 * 3600


def _pill(status):
    return (f"<span class='pill {html.escape(status)}'>"
            f"{_STATUS_LABEL.get(status, html.escape(status))}</span>")


def _fmt_secs(secs):
    """秒数 → mm:ss / h:mm:ss。"""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _elapsed_secs(started, finished=""):
    try:
        t0 = datetime.fromisoformat(str(started))
    except ValueError:
        return None
    try:
        t1 = datetime.fromisoformat(str(finished)) if finished else datetime.now()
    except ValueError:
        t1 = datetime.now()
    return max(0, (t1 - t0).total_seconds())


def _job_visible_in_drawer(j):
    """抽屉只放实时任务：running / interrupted 常驻；done/error 只留刚结束的。"""
    if j["status"] in ("running", "interrupted"):
        return True
    age = _elapsed_secs(j.get("finished") or j.get("started"))
    return age is None or age <= _DRAWER_RECENT_SECS


def _task_card_html(j):
    """一张任务小卡：feature + 状态 pill + 进度条 + 当前步骤 + 已用时/ETA。"""
    steps = j.get("steps") or []
    total = len(steps)
    done = sum(1 for s in steps if s["status"] in ("done", "error"))
    cur = next((s for s in steps if s["status"] == "running"), None)

    bar = ""
    if total:
        pct = int(done / total * 100)
        bar = f"<div class='tc-bar'><i style='width:{pct}%'></i></div>"

    step_line = ""
    if j["status"] == "running" and cur:
        step_line = f"<div class='tc-step'>当前：{_esc(cur['label'])}</div>"
    elif total:
        step_line = f"<div class='tc-step'>进度 {done}/{total}</div>"
    elif j.get("summary") and j["status"] == "running":
        step_line = f"<div class='tc-step'>{_esc(j['summary'])}</div>"

    summary = ""
    if j.get("summary") and j["status"] != "running":
        summary = f"<div class='tc-summary'>{_esc(j['summary'])}</div>"

    time_parts = []
    # 中断任务没有 finished：用 job 文件最后写入时间作为结束点（进程死前最后一次落盘）
    _end = j.get("finished") or ""
    if j["status"] == "interrupted" and not _end and j.get("path"):
        try:
            _end = datetime.fromtimestamp(os.path.getmtime(j["path"])).isoformat()
        except OSError:
            _end = ""
    elapsed = _elapsed_secs(j.get("started"), _end)
    if elapsed is not None:
        time_parts.append(f"已用时 {_fmt_secs(elapsed)}")
    # ETA：对齐 ingest._eta_seconds 算法——elapsed × 剩余/已完成（样本不足不显示）
    if j["status"] == "running" and total and done and elapsed:
        eta = elapsed * (total - done) / done
        time_parts.append(f"ETA ~{_fmt_secs(eta)}")
    time_html = (f"<div class='tc-time'>{' · '.join(time_parts)}</div>"
                 if time_parts else "")

    return (f"<div class='task-card'><div class='tc-head'>"
            f"<span class='tc-feature'>{_esc(j['feature'])}</span>{_pill(j['status'])}"
            f"</div>{bar}{step_line}{summary}{time_html}</div>")


def _render_drawer_tasks():
    """任务卡列表：running / interrupted / 刚结束的任务各一张小卡；
    中断任务保留并给 [清除]；无任务时 Empty State。"""
    job_list = [j for j in jobs.list_jobs() if _job_visible_in_drawer(j)]
    if not job_list:
        st.markdown("<div class='empty-state' style='padding:48px 12px'>"
                    "<div class='e-title'>暂无运行中的任务</div>"
                    "<div class='e-sub'>各功能页启动的后台任务会实时出现在这里，"
                    "切换页面/刷新不影响执行。</div></div>",
                    unsafe_allow_html=True)
        return
    for j in job_list:
        st.markdown(_task_card_html(j), unsafe_allow_html=True)
        if j["status"] in ("interrupted", "done", "error"):
            label = "清除失联记录" if j["status"] == "interrupted" else "清除记录"
            if st.button(label, key=f"drawer_clear_{j['key']}", type="tertiary"):
                jobs.clear_job(j["key"])
                st.rerun()


# 任务卡 3 秒自刷（streamlit >= 1.33 支持 fragment；不支持时降级为手动刷新按钮）
_drawer_tasks_fragment = (st.fragment(run_every=3)(_render_drawer_tasks)
                          if _HAS_FRAGMENT else None)


def render_task_drawer():
    """右侧任务抽屉（fixed 滑出面板）：Header「● N tasks」开关，每次 rerun 按状态挂载。"""
    if not st.session_state.get("task_drawer_open"):
        return
    with st.container(border=True):
        st.markdown('<div class="kb-drawer-marker"></div>', unsafe_allow_html=True)
        _t, _x = st.columns([6, 1], vertical_alignment="center")
        with _t:
            st.markdown("<div class='drawer-title'>任务</div>"
                        "<div class='drawer-sub'>后台任务实时进展 · 3s 自动刷新</div>",
                        unsafe_allow_html=True)
        with _x:
            if st.button("×", key="task_drawer_close", help="关闭任务面板"):
                st.session_state.task_drawer_open = False
                st.rerun()
        st.markdown("<div class='drawer-rule'></div>", unsafe_allow_html=True)
        if _drawer_tasks_fragment is not None:
            _drawer_tasks_fragment()
        else:
            if st.button("刷新", key="drawer_refresh"):
                st.rerun()
            _render_drawer_tasks()

# ==================== 主界面 ====================

# ---- Header（v2 §2.2）：品牌名 + 文字导航 5 项 + 窄搜索 + 任务/索引状态，纯 CSS fixed 浮到顶栏 ----
_hdr_running = sum(1 for j in jobs.list_jobs() if j["status"] == "running")

_HDR_NAV = [("home", "首页"), ("battle", "论文之战"), ("radar", "投资雷达"),
            ("compare", "新项目评审"), ("ingest", "文件归档")]
_HDR_BROWSE = {"home", "search", "doc", "category"}  # 浏览态都算「首页」导航高亮

with st.container(border=True):
    st.markdown('<div class="topnav-marker"></div>', unsafe_allow_html=True)
    _hdr_cols = st.columns([2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 2.8, 1.05, 0.75],
                           vertical_alignment="center")
    with _hdr_cols[0]:
        st.markdown('<div class="topnav-brand">一级投研知识库</div>', unsafe_allow_html=True)
    for _i, (_mode, _label) in enumerate(_HDR_NAV):
        with _hdr_cols[1 + _i]:
            _active = (st.session_state.view_mode == _mode
                       or (_mode == "home" and st.session_state.view_mode in _HDR_BROWSE))
            if _active:  # active 下划线：marker 兄弟选择器（见 CSS .hdr-nav-on）
                st.markdown('<div class="hdr-nav-on"></div>', unsafe_allow_html=True)
            if st.button(_label, key=f"hdr_nav_{_mode}", use_container_width=True):
                st.session_state.view_mode = _mode
                st.session_state.selected_doc = None
                st.session_state.selected_category = None
                st.rerun()
    with _hdr_cols[6]:
        search_query = st.text_input(
            "搜索",
            value=st.session_state.search_query,
            placeholder="搜索文档、项目、赛道、概念……",
            label_visibility="collapsed",
        )
    with _hdr_cols[7]:
        # 任务抽屉开关：quiet 胶囊（.hdr-pill-marker 兄弟模式）；有运行中任务时 accent 圆点计数
        st.markdown('<div class="hdr-pill-marker"></div>', unsafe_allow_html=True)
        _task_label = f"● {_hdr_running} 任务" if _hdr_running else "任务"
        if st.button(_task_label, key="hdr_tasks", use_container_width=True):
            st.session_state.task_drawer_open = not st.session_state.get(
                "task_drawer_open", False)
            st.rerun()
    with _hdr_cols[8]:
        st.markdown("<div class='hdr-status'><i></i>已索引</div>",
                    unsafe_allow_html=True)

# 全局搜索行为：输入即跳搜索视图（与原首页搜索一致）；在搜索页清空才回首页
if search_query != st.session_state.search_query:
    st.session_state.search_query = search_query
    if search_query:
        st.session_state.view_mode = "search"
        st.session_state.selected_doc = None
    elif st.session_state.view_mode == "search":
        st.session_state.view_mode = "home"
    st.rerun()

# ---- Sidebar（v2 §2.1）：KNOWLEDGE / RECENT + 底部 Settings（WORKSPACE 导航已上移 Header）----
with st.sidebar:
    sidebar_section("知识")
    index = st.session_state.index
    categories = index.get("categories", {})
    for cat_key in sorted(categories.keys()):
        cat = categories[cat_key]
        is_active = (st.session_state.view_mode == "category"
                     and st.session_state.selected_category == cat_key)
        num = cat_key.split("_")[0]
        if st.button(f"{num} {cat['name']} · {cat['count']}",
                     key=f"nav_cat_{cat_key}",
                     type="primary" if is_active else "secondary",
                     use_container_width=True):
            st.session_state.selected_category = cat_key
            st.session_state.view_mode = "category"
            st.session_state.selected_doc = None
            st.rerun()

    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

    sidebar_section("近期")
    _shown = 0
    for _e in _load_history():
        if _shown >= 5:
            break
        _doc = get_document_by_path(index, _e.get("path", ""))
        if not _doc:
            continue  # 文档已被移动/删除：跳过死记录
        _t = _e.get("title") or _doc["name"].replace(".md", "")
        if st.button(_t, key=f"nav_recent_{_e['path']}", use_container_width=True):
            open_doc(_doc)
        _shown += 1
    if not _shown:
        st.markdown("<div class='meta-line'>还没有阅读记录</div>",
                    unsafe_allow_html=True)

    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

    # Settings：API 设置整段收进底部 expander，不再默认占据侧栏顶部。
    # 本机持久化逻辑（local_api_key.txt 等）与原实现完全一致，只是挪了位置。
    with st.expander("设置 / API", expanded=False):
        st.selectbox(
            "API 厂家",
            list(PROVIDERS.keys()),
            format_func=lambda pid: (PROVIDERS[pid]["label"]
                                     + ("（可联网搜索）" if PROVIDERS[pid].get("search") is not None else "")),
            key="user_provider",
            help="选择你的 key 所属厂家，端点与推荐模型自动带出；"
                 "代理/中转站或未收录厂家选「自定义」。"
                 "标注「可联网搜索」的厂家提供联网搜索，雷达功能可用。",
        )
        _preset = PROVIDERS.get(st.session_state.user_provider, {})
        if st.session_state.user_provider in ("custom", "anthropic"):
            # 自定义厂家 / 官方无 OpenAI 兼容端点（Anthropic）：Base URL 需手填
            st.text_input(
                "Base URL",
                key="user_base_url",
                placeholder="https://...（OpenAI 兼容端点）",
            )
        else:
            st.caption(f"端点：`{_preset.get('base_url', '')}`")
        if _preset.get("note"):
            st.caption(_preset["note"])
        st.text_input(
            "模型",
            key="user_model",
            placeholder=_preset.get("default_model") or "模型名",
            help="留空则使用该厂家的预设默认模型。",
        )
        if not _preset.get("vision", True):
            st.caption("该模型不含视觉能力，PDF 高保真解析将使用本地引擎")
        st.text_input(
            "API Key",
            type="password",
            key="user_api_key",
            placeholder="sk-...（必填，否则 AI 功能不可用）",
            help="填入你自己的 API Key 后，Thesis Battle / Radar / 评审等 AI 功能"
                 "自动走你的 key 计费。只需填一次：key 会保存在本机 data/local_api_key.txt，"
                 "之后每次打开自动生效；清空输入框即删除本机保存的 key。",
            label_visibility="collapsed",
        )
        # 填入/修改/清空 → 同步本机持久化
        for _key, _file in (("user_provider", LOCAL_PROVIDER_FILE),
                            ("user_model", LOCAL_MODEL_FILE),
                            ("user_base_url", LOCAL_BASE_URL_FILE)):
            _v = st.session_state.get(_key, "")
            if isinstance(_v, str):
                _v = _v.strip()
            if _v != _load_local(_file):
                _save_local(_file, _v)
        _cur_key = st.session_state.get("user_api_key", "").strip()
        if _cur_key != _load_local_key():
            _save_local_key(_cur_key)  # 填入/修改/清空 → 同步本机持久化
        if _cur_key:
            st.caption("Key 已保存在本机，AI 功能走你的额度，无需重复输入")
        else:
            st.caption("未填入 Key，AI 功能（Battle / Radar / 评审 / 归档）不可用")

        # AI 成本（原控制台页「近 7 天」区，移入 Settings 底部保持可见）
        st.divider()
        st.markdown("<div class='sb-section'>AI Cost · 近 7 天</div>",
                    unsafe_allow_html=True)
        _recs, _total, _tokens, _by_feat = _week_costs()
        if not _recs:
            st.caption("近 7 天无 AI 调用记录。")
        else:
            st.markdown(
                f"<div class='meta-line' style='margin-bottom:0.4rem'>"
                f"花费 <b style='font-family:var(--font-mono)'>¥{_total:.2f}</b> · "
                f"{len(_recs)} 次调用 · {_tokens / 10000:.1f} 万 tokens</div>",
                unsafe_allow_html=True)
            _lines = ["<table class='stat-table'><thead><tr><th>功能</th>"
                      "<th class='num'>调用</th><th class='num'>花费</th></tr></thead><tbody>"]
            for _feat, _agg in sorted(_by_feat.items(),
                                      key=lambda kv: kv[1]["cost"], reverse=True):
                _lines.append(f"<tr><td>{html.escape(FEATURE_LABELS.get(_feat, _feat))}</td>"
                              f"<td class='num'>{_agg['calls']}</td>"
                              f"<td class='num'>¥{_agg['cost']:.3f}</td></tr>")
            _lines.append("</tbody></table>")
            st.markdown("".join(_lines), unsafe_allow_html=True)


# 主内容区
def _refresh_index():
    st.session_state.index = build_index(force=True)


_vm = st.session_state.view_mode
if _vm != "doc":
    # 离开阅读页：移除 2px 阅读进度条（它由阅读页注入、挂在 parent document 上）
    components.html(
        "<script>(function(){var P=window.parent,D=P.document;"
        "var b=D.getElementById('kb-reading-progress');if(b)b.remove();"
        "if(P.__kbProgressCleanup){try{P.__kbProgressCleanup();}catch(e){}}})();</script>",
        height=0)

if _vm == "home":
    render_home()
elif _vm == "category":
    render_category_view(st.session_state.selected_category)
elif _vm == "search":
    render_search_results(st.session_state.search_query)
elif _vm == "doc":
    render_document_detail(st.session_state.selected_doc)
elif _vm == "battle":
    render_battle(st.session_state.index, _refresh_index)
elif _vm == "radar":
    render_radar(st.session_state.index, _refresh_index)
elif _vm == "compare":
    render_review(st.session_state.index, _refresh_index)
    render_industry_cognition()
elif _vm == "ingest":
    render_ingest(st.session_state.index, _refresh_index)

# 任务抽屉：fixed 右侧面板，挂在所有页面之上（由 Header「● N tasks」开关）
render_task_drawer()
