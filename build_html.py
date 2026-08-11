# -*- coding: utf-8 -*-
"""
静态 HTML 站点生成器
把知识库生成一套纯 HTML/CSS/JS 静态站，复刻 Streamlit 版的页面与交互。
用法：venv/Scripts/python.exe knowledge_app/build_html.py
产物：knowledge_app/dist/ （可用任意静态服务器托管）
"""
import html
import json
import os
import re
import shutil
import sys
from datetime import datetime

import markdown

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CATEGORY_COLORS, CATEGORY_MAP, KNOWLEDGE_DIR
from indexer import get_related_documents, scan_knowledge_base
from mdblocks import wrap_ascii_tables

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(APP_DIR, "dist")

CATEGORY_ORDER = ["01_industry", "02_deals", "03_frameworks", "04_comparables",
                  "05_tracking", "06_strategy", "07_learnings", "08_funds",
                  "09_tech"]

QUICK_LINKS = [
    ("AI4S项目矩阵", "04_comparables/AI4S项目矩阵.md"),
    ("项目解剖模板", "03_frameworks/项目解剖模板.md"),
    ("科学家创业评估", "03_frameworks/科学家创业评估.md"),
    ("产业链投资图谱", "06_strategy/产业链投资图谱.md"),
]

FILE_ICONS = {"markdown": "📝", "text": "📄", "unknown": "📎"}

STATUS_BUCKETS = ["跟踪中", "推进中", "已投资", "已放弃", "已退出"]

# ==================== 工具 ====================


def esc(s):
    return html.escape(str(s or ""), quote=True)


def doc_title(doc):
    return doc.get("title") or doc["name"].replace(".md", "")


def doc_url(doc, depth=0):
    """文档页 URL：docs/<分类>__<文件名>.html"""
    slug = doc["path"].replace("/", "__").replace(".md", "") + ".html"
    return ("../" * depth) + "docs/" + slug


def url_of_doc_path(path, depth=0):
    slug = path.replace("/", "__").replace(".md", "") + ".html"
    return ("../" * depth) + "docs/" + slug


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.0f}KB"
    return f"{size_bytes/(1024*1024):.1f}MB"


def render_markdown(content):
    """Markdown -> (html, toc_tokens)。去掉正文开头重复的 # 标题行。
    纯文字/ASCII 表格先包成围栏；pipe 表格包 .table-wrap 容器以支持横向滚动。"""
    lines = content.split("\n")
    if lines and lines[0].strip().startswith("# "):
        content = "\n".join(lines[1:]).lstrip("\n")
    content = wrap_ascii_tables(content)
    md = markdown.Markdown(
        extensions=["extra", "toc", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-3"}},
    )
    body = md.convert(content)
    body = body.replace("<table>", '<div class="table-wrap"><table>')
    body = body.replace("</table>", "</table></div>")
    return body, md.toc_tokens


def flatten_toc(tokens, out=None):
    if out is None:
        out = []
    for t in tokens:
        out.append((t["level"], t["name"], t["id"]))
        flatten_toc(t.get("children", []), out)
    return out


# ==================== HTML 片段 ====================


def doc_card(doc, depth=0, show_category=True):
    icon = FILE_ICONS.get(doc["type"], "📝")
    title = doc_title(doc)
    track = doc.get("track") or ""
    status = doc.get("status") or ""
    meta_parts = []
    if show_category and doc.get("category", "其他") != "其他":
        meta_parts.append(f"{doc.get('category_icon', '📁')} {doc['category']}")
    if track and track != "未分类":
        meta_parts.append(f"🎯 {track}")
    if doc.get("last_updated"):
        meta_parts.append(f"📅 {doc['last_updated']}")
    if doc.get("status"):
        meta_parts.append(f"🏷️ {doc['status']}")
    meta_html = f'<div class="meta-line">{" · ".join(esc(p) for p in meta_parts)}</div>' if meta_parts else ""
    subtitle = f'<div class="caption">{esc(doc["subtitle"][:200])}</div>' if doc.get("subtitle") else ""
    return f"""
    <div class="card doc-card" data-track="{esc(track)}" data-status="{esc(status)}" data-modified="{esc(doc.get('modified', ''))}" data-name="{esc(title)}">
      <div class="card-head"><a class="doc-link" href="{esc(doc_url(doc, depth))}">{icon} {esc(title)}</a></div>
      {meta_html}
      {subtitle}
    </div>"""


def breadcrumb(items, depth=0):
    """items: [(label, url_or_None)]，最后一项 url=None 为当前页"""
    parts = []
    for label, url in items:
        if url:
            parts.append(f'<a class="crumb-link" href="{esc(url)}">{esc(label)}</a>')
        else:
            parts.append(f'<span class="crumb-current">{esc(label)}</span>')
    return '<div class="breadcrumb">' + '<span class="crumb-sep">/</span>'.join(parts) + "</div>"


# ==================== 页面骨架 ====================

BASE_CSS = """
:root {
  --kb-bg:#fafaf9; --kb-card:#ffffff; --kb-text:#1c1c1e; --kb-text-2:#6b7280;
  --kb-text-3:#9ca3af; --kb-accent:#3b6ea5; --kb-accent-soft:#eaf1f8;
  --kb-accent-border:#d7e3f0; --kb-border:#e5e3df; --kb-radius:8px;
  --kb-shadow:0 1px 3px rgba(0,0,0,0.06); --sidebar-w:250px;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--kb-bg); color:var(--kb-text);
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:15px; line-height:1.7;
}
a { color:var(--kb-accent); text-decoration:none; }
a:hover { text-decoration:underline; }

/* 布局 */
.layout { display:flex; min-height:100vh; }
.sidebar {
  width:var(--sidebar-w); flex-shrink:0; background:#f7f6f4;
  border-right:1px solid var(--kb-border); padding:1.2rem 0.9rem;
  position:sticky; top:0; height:100vh; overflow-y:auto;
}
.main { flex:1; padding:1.6rem 2.2rem 3rem; max-width:1100px; }

/* 侧边栏 */
.sb-section { font-size:0.72rem; font-weight:700; color:var(--kb-text-3); letter-spacing:0.08em; margin:1.1rem 0 0.4rem; }
.sb-section:first-child { margin-top:0; }
.sb-nav { display:block; padding:0.3rem 0.55rem; border-radius:6px; color:var(--kb-text); font-weight:500; font-size:0.9rem; }
.sb-nav:hover { background:#ececea; text-decoration:none; }
.sb-nav.active { background:var(--kb-accent-soft); color:var(--kb-accent); font-weight:600; }
.sb-stat { color:var(--kb-text-2); font-size:0.85rem; }
.sb-divider { border:none; border-top:1px solid var(--kb-border); margin:1rem 0; }

/* 顶栏 */
.topbar { margin-bottom:1.2rem; }
.main-header { font-size:1.6rem; font-weight:700; letter-spacing:-0.01em; }
.sub-header { font-size:0.85rem; color:var(--kb-text-3); margin:0.1rem 0 0.9rem; }
.search-form { display:flex; gap:0.5rem; }
.search-form input {
  flex:1; padding:0.55rem 0.9rem; border-radius:var(--kb-radius);
  border:1px solid var(--kb-border); background:var(--kb-card); font-size:0.9rem; outline:none;
}
.search-form input:focus { border-color:var(--kb-accent); box-shadow:0 0 0 3px rgba(59,110,165,0.15); }
.search-form button {
  padding:0.55rem 1.1rem; border-radius:var(--kb-radius); border:1px solid var(--kb-border);
  background:var(--kb-card); color:var(--kb-text); cursor:pointer; font-size:0.9rem;
}
.search-form button:hover { border-color:var(--kb-accent); color:var(--kb-accent); }

/* 节标题 / 面包屑 */
.section-header { font-size:1.1rem; font-weight:700; margin:1.6rem 0 0.8rem; }
.breadcrumb { font-size:0.88rem; margin-bottom:0.8rem; display:flex; flex-wrap:wrap; gap:0.4rem; align-items:center; }
.crumb-link { color:var(--kb-text-2); font-weight:500; }
.crumb-link:hover { color:var(--kb-accent); }
.crumb-sep { color:var(--kb-text-3); }
.crumb-current { color:var(--kb-text-2); }

/* 卡片 */
.card {
  background:var(--kb-card); border:1px solid var(--kb-border); border-radius:var(--kb-radius);
  box-shadow:var(--kb-shadow); padding:0.9rem 1.1rem; margin-bottom:0.9rem;
  transition:border-color .15s ease, box-shadow .15s ease;
}
.card:hover { border-color:var(--kb-accent-border); box-shadow:0 2px 8px rgba(0,0,0,0.08); }
.doc-card .card-head { display:flex; justify-content:space-between; align-items:center; }
.doc-link { color:var(--kb-text); font-weight:600; font-size:0.98rem; }
.doc-link:hover { color:var(--kb-accent); text-decoration:none; }
.meta-line { color:var(--kb-text-3); font-size:0.78rem; margin-top:0.15rem; }
.caption { color:var(--kb-text-2); font-size:0.82rem; margin-top:0.3rem; }

/* 指标卡 */
.metric-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:0.9rem; }
.metric-card { background:var(--kb-card); border:1px solid var(--kb-border); border-radius:var(--kb-radius); box-shadow:var(--kb-shadow); padding:0.8rem 1.1rem; }
.metric-label { color:var(--kb-text-2); font-size:0.82rem; }
.metric-value { font-size:1.6rem; font-weight:700; }

/* 分类卡 */
.cat-grid { display:grid; grid-template-columns:1fr 1fr; gap:0.9rem; }
.cat-card { padding:0; overflow:hidden; }
.cat-strip { height:4px; }
.cat-body { padding:0.9rem 1.1rem 1rem; }
.cat-head { font-size:1.02rem; font-weight:700; }
.cat-count { color:var(--kb-text-3); font-weight:500; font-size:0.82rem; margin-left:0.4rem; }
.cat-desc { color:var(--kb-text-2); font-size:0.8rem; margin:0.15rem 0 0.5rem; }
.cat-doc { display:block; padding:0.18rem 0.25rem; border-radius:4px; color:var(--kb-text); font-weight:500; font-size:0.9rem; }
.cat-doc:hover { color:var(--kb-accent); text-decoration:none; }
.cat-more { font-size:0.85rem; font-weight:500; }

/* 文档页 */
.doc-title { font-size:1.7rem; font-weight:750; line-height:1.3; margin:0.2rem 0 0.3rem; }
.doc-body { max-width:860px; margin:1.4rem auto 0; }
.pn-grid { display:grid; grid-template-columns:1fr 1fr; gap:0.9rem; margin:1.5rem 0; }
.pn-label { color:var(--kb-text-3); font-size:0.78rem; margin-bottom:0.15rem; }
.back-top-wrap { text-align:center; margin-top:1.5rem; }
.back-top { color:var(--kb-text-3); font-size:0.82rem; }
.back-top:hover { color:var(--kb-accent); }
details { border:1px solid var(--kb-border); border-radius:var(--kb-radius); background:var(--kb-card); padding:0.6rem 1rem; margin-bottom:0.8rem; }
details summary { cursor:pointer; font-weight:600; font-size:0.92rem; outline:none; }
details .detail-body { margin-top:0.6rem; }
.rel-link { display:block; padding:0.18rem 0.25rem; color:var(--kb-text); font-weight:500; font-size:0.9rem; }
.rel-link:hover { color:var(--kb-accent); text-decoration:none; }

/* TOC */
.toc-link {
  display:block; color:var(--kb-text-2); font-size:0.82rem; padding:0.15rem 0;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.toc-link:hover { color:var(--kb-accent); text-decoration:none; }
.toc-h3 { padding-left:1rem; font-size:0.78rem; color:var(--kb-text-3); }

/* Markdown 正文排版 */
.md h1 { font-size:1.45rem; margin:1.5rem 0 0.7rem; }
.md h2 { font-size:1.3rem; margin:1.6rem 0 0.6rem; padding-bottom:0.3rem; border-bottom:1px solid var(--kb-border); }
.md h3 { font-size:1.12rem; margin:1.3rem 0 0.5rem; }
.md h4 { font-size:1rem; margin:1.1rem 0 0.4rem; }
.md p, .md li { line-height:1.75; }
/* Markdown 表格：wrapper 横向滚动；表头底色 + 斑马纹 + 圆角边框，表头不换行 */
.table-wrap { overflow-x:auto; margin:0.8rem 0; }
.md table {
  border-collapse:separate; border-spacing:0; width:100%; font-size:0.88rem;
  border:1px solid var(--kb-border); border-radius:var(--kb-radius);
}
.md th { background:#f5f4f2; font-weight:600; white-space:nowrap; }
.md th, .md td {
  padding:0.5rem 0.8rem;
  border-bottom:1px solid var(--kb-border); border-right:1px solid var(--kb-border);
}
.md tr > th:last-child, .md tr > td:last-child { border-right:none; }
.md tbody tr:last-child > td { border-bottom:none; }
.md tbody tr:nth-child(even) td { background:#fafaf9; }
.md tbody tr:hover td { background:#f0f4f9; }
.md blockquote {
  border-left:3px solid var(--kb-accent); background:var(--kb-accent-soft);
  padding:0.5rem 1rem; border-radius:0 6px 6px 0; color:var(--kb-text-2); margin:0.8rem 0;
}
.md code { background:#f3f2f0; padding:0.1rem 0.35rem; border-radius:4px; font-size:0.85em; }
/* pre（含 ASCII 表格围栏）：中西文等宽字体栈、字号略缩、横向滚动不换行 */
.md pre {
  background:#f5f4f2; border:1px solid var(--kb-border); border-radius:var(--kb-radius);
  padding:0.8rem 1rem; overflow-x:auto; white-space:pre;
}
.md pre code {
  background:none; padding:0; font-size:0.82rem; line-height:1.5;
  font-family:"Sarasa Mono SC","Cascadia Mono","Noto Sans Mono CJK SC",Consolas,"Microsoft YaHei",monospace;
}
.md img { max-width:100%; }
.md hr { border:none; border-top:1px solid var(--kb-border); margin:1.4rem 0; }

/* 对比页表格（复用 .md table 的浅色风格） */
.cmp-table { width:100%; border-collapse:collapse; font-size:0.88rem; margin:0.4rem 0; }
.cmp-table th { background:#f5f4f2; font-weight:600; text-align:left; }
.cmp-table th, .cmp-table td { border:1px solid var(--kb-border); padding:0.45rem 0.7rem; vertical-align:top; }
.cmp-table tbody tr:nth-child(even) td { background:#fafaf9; }

/* 响应式 */
@media (max-width:900px) {
  .layout { flex-direction:column; }
  .sidebar { width:100%; height:auto; position:static; border-right:none; border-bottom:1px solid var(--kb-border); }
  .main { padding:1.2rem 1rem 3rem; }
  .metric-grid { grid-template-columns:repeat(2,1fr); }
  .cat-grid, .pn-grid { grid-template-columns:1fr; }
}
"""

SEARCH_JS = """
(async function () {
  const params = new URLSearchParams(location.search);
  const q = (params.get('q') || '').trim();
  const input = document.getElementById('search-input');
  const list = document.getElementById('results');
  const summary = document.getElementById('summary');
  input.value = q;
  if (!q) { summary.textContent = '输入关键词开始搜索'; return; }

  // KB_DOCS 由 build_html.py 直接内嵌在本文件头部，无需网络请求，file:// 下也能搜索
  const docs = KB_DOCS;
  const query = q.toLowerCase();

  const scored = [];
  for (const d of docs) {
    let score = 0;
    if ((d.title || '').toLowerCase().includes(query)) score += 10;
    if ((d.name || '').toLowerCase().includes(query)) score += 5;
    if ((d.content || '').toLowerCase().includes(query)) score += 3;
    if ((d.project || '').toLowerCase().includes(query)) score += 6;
    if ((d.category || '').toLowerCase().includes(query)) score += 4;
    if (score > 0) scored.push([score, d]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  summary.textContent = `搜索：${q} · 共 ${scored.length} 个结果`;

  const esc = s => String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  list.innerHTML = scored.map(([, d]) => {
    const meta = [d.category_icon && d.category !== '其他' ? d.category_icon + ' ' + d.category : '',
                  d.last_updated ? '📅 ' + d.last_updated : '', d.status ? '🏷️ ' + d.status : '']
                 .filter(Boolean).join(' · ');
    return `<div class="card doc-card">
      <div class="card-head"><a class="doc-link" href="${esc(d.url)}">📝 ${esc(d.title)}</a></div>
      ${meta ? `<div class="meta-line">${esc(meta)}</div>` : ''}
      ${d.subtitle ? `<div class="caption">${esc(d.subtitle.slice(0, 200))}</div>` : ''}
    </div>`;
  }).join('') || '<div class="caption">未找到匹配结果，尝试其他关键词</div>';

  document.getElementById('search-form').addEventListener('submit', e => {
    e.preventDefault();
    const nq = input.value.trim();
    location.search = '?q=' + encodeURIComponent(nq);
  });
})();
"""

DEALS_FILTER_JS = """
(function () {
  var trackSel = document.getElementById('filter-track');
  var statusSel = document.getElementById('filter-status');
  var sortSel = document.getElementById('filter-sort');
  var countEl = document.getElementById('filter-count');
  var wrap = document.getElementById('deal-cards');
  if (!trackSel || !wrap) { return; }
  var cards = Array.prototype.slice.call(wrap.querySelectorAll('.doc-card'));
  var total = cards.length;
  function apply() {
    var t = trackSel.value, s = statusSel.value, mode = sortSel.value;
    var sorted = cards.slice();
    if (mode === 'name') {
      sorted.sort(function (a, b) {
        return (a.getAttribute('data-name') || '').localeCompare(b.getAttribute('data-name') || '', 'zh');
      });
    } else {
      sorted.sort(function (a, b) {
        return (b.getAttribute('data-modified') || '').localeCompare(a.getAttribute('data-modified') || '');
      });
    }
    var hit = 0;
    sorted.forEach(function (c) {
      wrap.appendChild(c);
      var show = (!t || c.getAttribute('data-track') === t) &&
                 (!s || c.getAttribute('data-status') === s);
      c.style.display = show ? '' : 'none';
      if (show) { hit += 1; }
    });
    countEl.textContent = '命中 ' + hit + ' / 共 ' + total + ' 篇';
  }
  trackSel.onchange = apply;
  statusSel.onchange = apply;
  sortSel.onchange = apply;
  apply();
})();
"""

def sidebar_html(index, depth=0, active_cat=None, toc_items=None):
    p = "../" * depth
    categories = index.get("categories", {})

    parts = ['<div class="sb-section">导航</div>',
             f'<a class="sb-nav" href="{p}index.html">🏠 首页</a>',
             f'<a class="sb-nav" href="{p}compare.html">🧭 新项目评审</a>',
             f'<a class="sb-nav" href="{p}battle.html">⚔️ Thesis Battle</a>',
             f'<a class="sb-nav" href="{p}radar.html">📡 Investment Radar</a>',
             '<hr class="sb-divider">', '<div class="sb-section">分类</div>']
    for cat_key in CATEGORY_ORDER:
        if cat_key not in categories:
            continue
        cat = categories[cat_key]
        active = " active" if active_cat == cat_key else ""
        parts.append(f'<a class="sb-nav{active}" href="{p}category/{cat_key}.html">'
                     f'{cat["icon"]} {esc(cat["name"])} ({cat["count"]})</a>')

    if toc_items:
        parts.append('<hr class="sb-divider"><div class="sb-section">本页目录</div>')
        for level, text, anchor in toc_items[:30]:
            cls = "toc-h3" if level == 3 else "toc-h2"
            parts.append(f'<a class="toc-link {cls}" href="#{esc(anchor)}">{esc(text)}</a>')

    parts.append('<hr class="sb-divider"><div class="sb-section">快捷入口</div>')
    for label, path in QUICK_LINKS:
        if any(d["path"] == path for d in index.get("documents", [])):
            parts.append(f'<a class="sb-nav" href="{esc(url_of_doc_path(path, depth))}">⚡ {esc(label)}</a>')

    parts.append('<hr class="sb-divider"><div class="sb-section">统计</div>')
    parts.append(f'<div class="sb-stat">文档 <b>{index["total_documents"]}</b> · '
                 f'分类 <b>{len(categories)}</b></div>')
    return "\n".join(parts)


def topbar_html(depth=0):
    p = "../" * depth
    return f"""
    <div class="topbar">
      <div class="main-header">🧠 一级投研知识库</div>
      <div class="sub-header">沉淀认知 · 关联洞察 · 复用框架</div>
      <form class="search-form" id="search-form" action="{p}search.html" method="get">
        <input id="search-input" name="q" type="text" placeholder="搜索文档、项目、赛道、概念... 例如：AI4S、光掩模">
        <button type="submit">搜索</button>
      </form>
    </div>"""


def page_html(title, index, main_html, depth=0, active_cat=None, toc_items=None, extra_js=""):
    p = "../" * depth
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 一级投研知识库</title>
<link rel="stylesheet" href="{p}assets/style.css">
</head>
<body>
<div class="layout">
  <aside class="sidebar">{sidebar_html(index, depth, active_cat, toc_items)}</aside>
  <main class="main">
    {topbar_html(depth)}
    {main_html}
  </main>
</div>
{extra_js}
</body>
</html>"""


# ==================== 页面生成 ====================


def build_home(index):
    categories = index.get("categories", {})

    metrics = f"""
    <div class="metric-grid">
      <div class="metric-card"><div class="metric-label">总文档</div><div class="metric-value">{index['total_documents']}</div></div>
      <div class="metric-card"><div class="metric-label">分类</div><div class="metric-value">{len(categories)}</div></div>
      <div class="metric-card"><div class="metric-label">生成时间</div><div class="metric-value" style="font-size:1.2rem">{datetime.now():%Y-%m-%d}</div></div>
    </div>"""

    cat_cards = []
    for cat_key in CATEGORY_ORDER:
        if cat_key not in categories:
            continue
        cat = categories[cat_key]
        color = CATEGORY_COLORS.get(cat["name"], "#7f7f7f")
        links = []
        for doc in cat["documents"][:5]:
            icon = FILE_ICONS.get(doc["type"], "📝")
            links.append(f'<a class="cat-doc" href="{esc(doc_url(doc))}">{icon} {esc(doc_title(doc))}</a>')
        more = ""
        if cat["count"] > 5:
            more = f'<a class="cat-more" href="category/{cat_key}.html">查看全部 {cat["count"]} 篇 →</a>'
        cat_cards.append(f"""
        <div class="card cat-card">
          <div class="cat-strip" style="background:{color}"></div>
          <div class="cat-body">
            <div class="cat-head">{cat['icon']} {esc(cat['name'])}<span class="cat-count">{cat['count']} 篇</span></div>
            <div class="cat-desc">{esc(cat['description'])}</div>
            {''.join(links)}
            {more}
          </div>
        </div>""")

    recent = sorted(index.get("documents", []), key=lambda x: x.get("modified", ""), reverse=True)[:10]
    recent_html = "".join(doc_card(d) for d in recent)

    main = f"""
    <div class="section-header">知识库概览</div>
    {metrics}
    <div class="section-header">按分类浏览</div>
    <div class="cat-grid">{''.join(cat_cards)}</div>
    <div class="section-header">最近更新</div>
    {recent_html}"""
    return page_html("首页", index, main)


def build_category(index, cat_key):
    cat = index["categories"][cat_key]
    color = CATEGORY_COLORS.get(cat["name"], "#7f7f7f")
    cards = "".join(doc_card(d, depth=1, show_category=False) for d in cat["documents"])
    filter_html, extra_js = "", ""
    if cat_key == "02_deals":
        tracks = sorted({d.get("track") or "未分类" for d in cat["documents"]})
        sel_style = ("padding:0.35rem 0.6rem; border:1px solid var(--kb-border); "
                     "border-radius:6px; background:#fff; font-size:0.85rem;")
        track_opts = '<option value="">全部赛道</option>' + "".join(
            f'<option value="{esc(t)}">{esc(t)}</option>' for t in tracks)
        status_opts = '<option value="">全部状态</option>' + "".join(
            f'<option value="{esc(s)}">{esc(s)}</option>' for s in STATUS_BUCKETS)
        filter_html = f"""
    <div style="display:flex; flex-wrap:wrap; gap:0.5rem; align-items:center; margin-bottom:1rem">
      <select id="filter-track" style="{sel_style}">{track_opts}</select>
      <select id="filter-status" style="{sel_style}">{status_opts}</select>
      <select id="filter-sort" style="{sel_style}">
        <option value="modified">按更新时间</option>
        <option value="name">按名称</option>
      </select>
      <span class="meta-line" id="filter-count"></span>
    </div>"""
        cards = f'<div id="deal-cards">{cards}</div>'
        extra_js = "<script>" + DEALS_FILTER_JS + "</script>"
    main = f"""
    {breadcrumb([("首页", "../index.html"), (cat["name"], None)], depth=1)}
    <div class="cat-strip" style="background:{color}; border-radius:4px; margin-bottom:0.8rem"></div>
    <div class="doc-title">{cat['icon']} {esc(cat['name'])}</div>
    <div class="meta-line" style="margin-bottom:1rem">{esc(cat['description'])} · 共 {cat['count']} 篇</div>
    {filter_html}
    {cards}"""
    return page_html(cat["name"], index, main, depth=1, active_cat=cat_key, extra_js=extra_js)


def build_doc(index, doc):
    title = doc_title(doc)
    body, toc_tokens = render_markdown(doc.get("content", ""))
    toc_items = flatten_toc(toc_tokens)

    cat_key = doc.get("category_key")
    crumbs = [("首页", "../index.html")]
    if cat_key:
        crumbs.append((doc.get("category", "其他"), f"../category/{cat_key}.html"))
    crumbs.append((title, None))

    meta_parts = [f"{doc.get('category_icon', '📁')} {doc.get('category', '其他')}"]
    if doc.get("last_updated"):
        meta_parts.append(f"📅 {doc['last_updated']}")
    if doc.get("status"):
        meta_parts.append(f"🏷️ {doc['status']}")
    if doc.get("project"):
        meta_parts.append(f"📌 {doc['project']}")

    # 上一篇 / 下一篇
    pn_html = ""
    if cat_key:
        cat_docs = index["categories"][cat_key]["documents"]
        paths = [d["path"] for d in cat_docs]
        if doc["path"] in paths:
            pos = paths.index(doc["path"])
            prev_doc = cat_docs[pos - 1] if pos > 0 else None
            next_doc = cat_docs[pos + 1] if pos < len(cat_docs) - 1 else None
            if prev_doc or next_doc:
                prev_html = (f'<div><div class="pn-label">← 上一篇</div>'
                             f'<a class="doc-link" href="{esc(doc_url(prev_doc, 1))}">📝 {esc(doc_title(prev_doc))}</a></div>') if prev_doc else "<div></div>"
                next_html = (f'<div><div class="pn-label">下一篇 →</div>'
                             f'<a class="doc-link" href="{esc(doc_url(next_doc, 1))}">📝 {esc(doc_title(next_doc))}</a></div>') if next_doc else "<div></div>"
                pn_html = f'<div class="pn-grid">{prev_html}{next_html}</div>'

    # 相关文档
    related = get_related_documents(index, doc)
    related_html = ""
    if related:
        links = "".join(
            f'<a class="rel-link" href="{esc(doc_url(r, 1))}">📝 {esc(doc_title(r))}'
            f'（{esc(r.get("category", ""))}）</a>'
            for r in related)
        related_html = f"""<details><summary>🔗 相关文档推荐（{len(related)} 篇）</summary>
        <div class="detail-body">{links}</div></details>"""

    file_info = f"""<details><summary>📎 文件信息</summary>
    <div class="detail-body"><ul>
      <li><b>文件名</b>：<code>{esc(doc['name'])}</code></li>
      <li><b>路径</b>：<code>{esc(doc['path'])}</code></li>
      <li><b>大小</b>：{format_size(doc['size'])}</li>
      <li><b>修改时间</b>：{esc(doc['modified'][:19])}</li>
    </ul></div></details>"""

    main = f"""
    <a id="page-top"></a>
    {breadcrumb(crumbs, depth=1)}
    <div class="doc-title">{esc(title)}</div>
    <div class="meta-line">{' · '.join(esc(x) for x in meta_parts)}</div>
    <div class="doc-body md">
      {body}
      <hr>
      {pn_html}
      {related_html}
      {file_info}
      <div class="back-top-wrap"><a class="back-top" href="#page-top">↑ 回到顶部</a></div>
    </div>"""
    return page_html(title, index, main, depth=1, active_cat=cat_key, toc_items=toc_items)


def build_search(index):
    main = f"""
    {breadcrumb([("首页", "index.html"), ("搜索", None)])}
    <div class="section-header">搜索</div>
    <div class="meta-line" id="summary" style="margin-bottom:0.8rem"></div>
    <div id="results"></div>"""
    js = '<script src="assets/search.js"></script>'
    return page_html("搜索", index, main, extra_js=js)


# ==================== 新项目评审（行业总文档驱动） ====================

COORD_SECTION_RE = re.compile(r"^## .*行业坐标与关键变量\n.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE)
REVIEW_SECTION_RE = re.compile(r"^## 🧭 行业总文档评审\n.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE)


def _clip(text, n):
    return text if len(text) <= n else text[:n] + "…"


def build_compare(index):
    deals = [d for d in index.get("documents", []) if d.get("category_key") == "02_deals"]
    industries = [d for d in index.get("documents", []) if d.get("category_key") == "01_industry"]
    header = f"""
    {breadcrumb([("首页", "index.html"), ("新项目评审", None)])}
    <div class="doc-title">🧭 新项目评审</div>
    <div class="meta-line" style="margin-bottom:0.3rem">按赛道总览手里的牌；以行业总文档的「行业坐标与关键变量」为基准评审新项目。</div>
    <div class="meta-line" style="margin-bottom:1rem">🔒 评审生成与写回请使用 Streamlit 版（8501）；本页为只读视图。</div>"""
    if not deals:
        return page_html("新项目评审", index, header + '\n    <div class="caption">02_deals 暂无文档。</div>')

    by_track = {}
    for d in deals:
        by_track.setdefault(d.get("track") or "未分类", []).append(d)

    # ① 赛道总览：按赛道分组的可折叠表格
    overview_parts = []
    for track in sorted(by_track):
        tdocs = sorted(by_track[track], key=lambda d: d.get("modified", ""), reverse=True)
        rows = []
        for d in tdocs:
            rows.append(
                f'<tr><td><a href="{esc(doc_url(d))}">{esc(doc_title(d))}</a></td>'
                f'<td>{esc(d.get("status") or "—")}</td>'
                f'<td>{esc(d.get("modified", "")[:10])}</td>'
                f'<td>{esc(_clip(d.get("subtitle") or "—", 40))}</td></tr>')
        open_attr = " open" if len(by_track) <= 3 else ""
        overview_parts.append(
            f"<details{open_attr}><summary>🎯 {esc(track)}（{len(tdocs)}）</summary>"
            '<div class="detail-body"><table class="cmp-table">'
            "<thead><tr><th>项目</th><th>状态</th><th>更新时间</th><th>一句话定位</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div></details>")

    # ② 行业坐标与关键变量：每篇行业总文档一张卡片
    coord_cards = []
    for d in sorted(industries, key=lambda x: doc_title(x)):
        m = COORD_SECTION_RE.search(d.get("content", ""))
        if m:
            body, _ = render_markdown(m.group(0))
            inner = f'<div class="md" style="margin-top:0.6rem">{body}</div>'
        else:
            inner = '<div class="caption" style="margin-top:0.6rem">尚无行业坐标章节。</div>'
        meta = f"{d.get('category_icon', '📁')} {d.get('category', '其他')}"
        if d.get("last_updated"):
            meta += f" · 📅 {d['last_updated']}"
        coord_cards.append(f"""
    <div class="card doc-card">
      <div class="card-head"><a class="doc-link" href="{esc(doc_url(d))}">📝 {esc(doc_title(d))}</a></div>
      <div class="meta-line">{esc(meta)}</div>
      {inner}
    </div>""")
    coord_html = "".join(coord_cards) if coord_cards else _empty_card("01_industry 暂无行业总文档。")

    # ③ 项目评审留档：02_deals 中含「🧭 行业总文档评审」节的文档
    reviewed = [d for d in deals if REVIEW_SECTION_RE.search(d.get("content", ""))]
    reviewed.sort(key=lambda d: d.get("modified", ""), reverse=True)
    review_cards = []
    for d in reviewed:
        section = REVIEW_SECTION_RE.search(d["content"]).group(0)
        body, _ = render_markdown(section)
        meta = f"🎯 {d.get('track') or '未分类'} · {d.get('status') or '—'}"
        if d.get("modified"):
            meta += f" · 📅 {d['modified'][:10]}"
        review_cards.append(f"""
    <div class="card doc-card">
      <div class="card-head"><a class="doc-link" href="{esc(doc_url(d))}">📝 {esc(doc_title(d))}</a></div>
      <div class="meta-line">{esc(meta)}</div>
      <div class="md" style="margin-top:0.6rem">{body}</div>
    </div>""")
    review_html = "".join(review_cards) if review_cards else _empty_card(
        "尚无项目评审——在 Streamlit 版 🧭 页完成新项目评审并写回后，这里会自动出现。")

    main = f"""
    {header}
    <div class="section-header">① 赛道总览</div>
    {''.join(overview_parts)}
    <div class="section-header">② 行业坐标与关键变量</div>
    {coord_html}
    <div class="section-header">③ 项目评审留档</div>
    {review_html}"""
    return page_html("新项目评审", index, main)


# ==================== Thesis Battle / Investment Radar（只读快照） ====================

DATA_DIR = os.path.join(APP_DIR, "data")

BATTLE_SECTION_RE = re.compile(r"^## ⚔️ Battle 记录\n.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE)


def _load_data_json(name, default):
    """读 knowledge_app/data 下的 JSON，文件缺失或损坏时返回 default。"""
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _as_text(v):
    if isinstance(v, (list, tuple)):
        return "；".join(str(x) for x in v)
    return str(v or "")


def _empty_card(text):
    return f'<div class="card"><div class="caption">{esc(text)}</div></div>'


def _last_radar_auto():
    """radar_auto.log（jsonl）最后一行的 ts/mode，没有则返回空串。"""
    try:
        last = ""
        with open(os.path.join(DATA_DIR, "radar_auto.log"), encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line.strip()
        if last:
            rec = json.loads(last)
            ts, mode = str(rec.get("ts", "")), str(rec.get("mode", ""))
            return f"{ts}（{mode}）" if mode else ts
    except (OSError, ValueError):
        pass
    return ""


def build_battle(index):
    docs = [d for d in index.get("documents", []) if BATTLE_SECTION_RE.search(d.get("content", ""))]
    docs.sort(key=lambda d: d.get("modified", ""), reverse=True)
    cards = []
    for d in docs:
        section = BATTLE_SECTION_RE.search(d["content"]).group(0)
        body, _ = render_markdown(section)
        meta = f"{d.get('category_icon', '📁')} {d.get('category', '其他')}"
        if d.get("last_updated"):
            meta += f" · 📅 {d['last_updated']}"
        cards.append(f"""
    <div class="card doc-card">
      <div class="card-head"><a class="doc-link" href="{esc(doc_url(d))}">📝 {esc(doc_title(d))}</a></div>
      <div class="meta-line">{esc(meta)}</div>
      <div class="md" style="margin-top:0.6rem">{body}</div>
    </div>""")
    content = "".join(cards) if cards else _empty_card(
        "尚无 Battle 记录——在 Streamlit 版 ⚔️ 页完成一场辩论并写回后，这里会自动出现。")
    main = f"""
    {breadcrumb([("首页", "index.html"), ("Thesis Battle", None)])}
    <div class="doc-title">⚔️ Thesis Battle</div>
    <div class="meta-line" style="margin-bottom:0.3rem">AI 红队辩论写回各项目文档「⚔️ Battle 记录」节的汇总，共 {len(cards)} 篇。</div>
    <div class="meta-line" style="margin-bottom:1rem">🔒 交互式辩论请使用 Streamlit 版（8501）；本页为只读快照。</div>
    {content}"""
    return page_html("⚔️ Thesis Battle", index, main)


def build_radar(index):
    last_auto = _last_radar_auto()
    auto_line = (f'<div class="meta-line" style="margin-bottom:0.3rem">🤖 上次自动抓取：{esc(last_auto)}</div>'
                 if last_auto else "")

    # ① 信号池
    signals = _load_data_json("radar_signals.json", [])
    if signals:
        rows = []
        for s in sorted(signals, key=lambda x: str(x.get("date", "")), reverse=True)[:50]:
            title = ("🤖 " if s.get("auto") else "") + str(s.get("title", ""))
            rows.append(
                f'<tr><td style="white-space:nowrap">{esc(s.get("date", ""))}</td>'
                f'<td>{esc(s.get("source_type", ""))}</td>'
                f'<td>{esc(s.get("theme", ""))}</td>'
                f'<td>{esc(s.get("event_type", ""))}</td>'
                f'<td>{esc(title)}</td>'
                f'<td>{esc(s.get("why", ""))}</td></tr>')
        signals_html = (
            '<div style="overflow-x:auto"><table class="cmp-table"><thead><tr>'
            "<th>日期</th><th>来源</th><th>主题</th><th>事件类型</th><th>标题</th>"
            "<th>为什么可能改变市场认知</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")
    else:
        signals_html = _empty_card("信号池为空——在 Streamlit 版 📡 页跑一次自动抓取后，这里会自动出现。")

    # ② 主题叙事
    themes = _load_data_json("radar_themes.json", {})
    if themes:
        theme_cards = []
        for theme in sorted(themes):
            entry = themes[theme] or {}
            cur = entry.get("current", {}) or {}
            cur_fields = [("当前叙事", "narrative"), ("支持证据", "evidence"), ("代表性观点", "views"),
                          ("共识", "consensus"), ("分歧", "divergence")]
            body = "".join(
                f'<div style="margin-top:0.45rem"><b>{label}</b>'
                f'<div class="caption">{esc(_as_text(cur.get(key)) or "—")}</div></div>'
                for label, key in cur_fields)
            hist = entry.get("history", []) or []
            hist_html = ""
            if hist:
                items = []
                for h in sorted(hist, key=lambda x: str(x.get("date", "")), reverse=True):
                    mark = "🔀 " if h.get("is_transition") else ""
                    lines = [f"<b>{esc(h.get('date', '—'))}</b> {mark}"]
                    if h.get("previous"):
                        lines.append(f"过去：{esc(_as_text(h['previous']))}")
                    if h.get("new"):
                        lines.append(f"之后：{esc(_as_text(h['new']))}")
                    if h.get("trigger"):
                        lines.append(f"触发：{esc(_as_text(h['trigger']))}")
                    if h.get("meaning"):
                        lines.append(f"含义：{esc(_as_text(h['meaning']))}")
                    items.append('<li style="margin-bottom:0.45rem">' + "<br>".join(lines) + "</li>")
                hist_html = (
                    f'<details style="margin-top:0.6rem"><summary>📜 叙事演变（{len(hist)}）</summary>'
                    '<div class="detail-body"><ul style="margin:0; padding-left:1.2rem">'
                    f'{"".join(items)}</ul></div></details>')
            theme_cards.append(f"""
    <div class="card doc-card">
      <div class="card-head"><span class="doc-link">🧭 {esc(theme)}</span></div>
      <div class="meta-line">更新于 {esc(entry.get('updated', '—'))}</div>
      {body}
      {hist_html}
    </div>""")
        themes_html = "".join(theme_cards)
    else:
        themes_html = _empty_card("尚无主题叙事——自动抓取后由 AI 基于信号生成行业认知。")

    # ③ 边际变量
    variables = _load_data_json("radar_variables.json", [])
    if variables:
        var_fields = [("新增信息", "new_info"), ("原有预期", "prev_expect"), ("预期变化", "expect_change"),
                      ("边际变量", "marginal_var"), ("市场影响", "market_impact")]
        var_cards = []
        for v in sorted(variables, key=lambda x: str(x.get("date", "")), reverse=True):
            mark = "🤖 " if v.get("auto") else ""
            body = "".join(
                f'<div style="margin-top:0.45rem"><b>{label}</b>'
                f'<div class="caption">{esc(_as_text(v.get(key)) or "—")}</div></div>'
                for label, key in var_fields)
            var_cards.append(f"""
    <div class="card doc-card">
      <div class="card-head"><span class="doc-link">{mark}{esc(v.get('title', ''))}</span></div>
      <div class="meta-line">{esc(v.get('var_type', '—'))} · {esc(v.get('theme', '—'))} · 📅 {esc(v.get('date', ''))}</div>
      {body}
    </div>""")
        variables_html = "".join(var_cards)
    else:
        variables_html = _empty_card("尚无边际变量——自动抓取后由 AI 从信号中提炼。")

    main = f"""
    {breadcrumb([("首页", "index.html"), ("Investment Radar", None)])}
    <div class="doc-title">📡 Investment Radar</div>
    <div class="meta-line" style="margin-bottom:0.3rem">信号积累 → 主题叙事演变 → 边际变量定价。</div>
    {auto_line}
    <div class="meta-line" style="margin-bottom:1rem">🔒 交互式录入请使用 Streamlit 版（8501）；本页为只读快照。</div>
    <div class="section-header">① 信号池</div>
    {signals_html}
    <div class="section-header">② 主题叙事</div>
    {themes_html}
    <div class="section-header">③ 边际变量</div>
    {variables_html}"""
    return page_html("📡 Investment Radar", index, main)


# ==================== 主流程 ====================


def main():
    print(f"[SCAN] {KNOWLEDGE_DIR}")
    documents, categories = scan_knowledge_base()
    index = {
        "indexed_at": datetime.now().isoformat(),
        "total_documents": len(documents),
        "documents": documents,
        "categories": categories,
    }

    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)
    for sub in ["assets", "docs", "category"]:
        os.makedirs(os.path.join(DIST_DIR, sub), exist_ok=True)

    # 静态资源
    with open(os.path.join(DIST_DIR, "assets", "style.css"), "w", encoding="utf-8") as f:
        f.write(BASE_CSS)

    # 搜索索引内嵌进 search.js（含全文，前端加权搜索；file:// 双击打开也能搜）
    search_docs = []
    for d in documents:
        search_docs.append({
            "name": d["name"], "title": doc_title(d), "url": doc_url(d),
            "category": d.get("category", ""), "category_icon": d.get("category_icon", ""),
            "project": d.get("project", ""),
            "status": d.get("status", ""), "last_updated": d.get("last_updated", ""),
            "subtitle": d.get("subtitle", ""), "content": d.get("content", ""),
        })
    with open(os.path.join(DIST_DIR, "assets", "search.js"), "w", encoding="utf-8") as f:
        f.write("const KB_DOCS = ")
        json.dump(search_docs, f, ensure_ascii=False)
        f.write(";\n" + SEARCH_JS)

    pages = 0

    def write(rel, content):
        nonlocal pages
        with open(os.path.join(DIST_DIR, rel), "w", encoding="utf-8") as f:
            f.write(content)
        pages += 1

    write("index.html", build_home(index))
    write("search.html", build_search(index))
    write("compare.html", build_compare(index))
    write("battle.html", build_battle(index))
    write("radar.html", build_radar(index))

    for cat_key in CATEGORY_ORDER:
        if cat_key in categories:
            write(f"category/{cat_key}.html", build_category(index, cat_key))

    for d in documents:
        slug = d["path"].replace("/", "__").replace(".md", "") + ".html"
        write(f"docs/{slug}", build_doc(index, d))

    print(f"[DONE] {pages} pages -> {DIST_DIR}")


if __name__ == "__main__":
    main()
