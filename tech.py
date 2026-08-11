# -*- coding: utf-8 -*-
"""
技术沉淀（09_tech）模块：文章 → 技术提取 → 合并进技术档案（可独立 CLI 运行）。

流程：extract_and_route（提取技术要点 + 路由到既有文档/新建）
     → merge_doc（按档案规范全文档重写合并）→ save_tech_doc（写盘）。
档案结构与提取/合并规则外置在 `03_frameworks/技术提取框架.md`（可直接改，
缺文件走内置兜底）；每个技术主题一份"活文档"，同主题材料持续往上填。

CLI:
    python tech.py articles.txt [--dry-run]
    articles.txt 每行一个 mp.weixin.qq.com 链接（# 开头为注释）。

数据：文档落在知识库 09_tech/ 目录，索引由调用方刷新（app 内 on_saved）。
"""
import argparse
import json
import os
import re
from datetime import date

import config
import radar_auto
import radar_wechat
from llm import chat

TECH_DIR_KEY = "09_tech"
TECH_FRAMEWORK_FILE = os.path.join(config.KNOWLEDGE_DIR, "03_frameworks", "技术提取框架.md")
ARTICLE_TEXT_LIMIT = 6000  # 单篇正文送入模型的截断长度
DOC_LIMIT = 12000          # 既有文档送入合并的截断长度
MAX_KEY_POINTS = 6

# 内置兜底规范：与 03_frameworks/技术提取框架.md 同要点，文件缺失时使用
FALLBACK_FRAMEWORK = """技术档案规范（09_tech）：每个技术主题一份"活文档"，同主题材料合并更新。
档案结构：
# {技术名}
> 一句话定位 | 所属行业：xx | 更新时间：YYYY-MM-DD
> 🆕 本次新增：3-5 个要点（最近一次合并写入，下次合并整段覆盖）
## 技术图谱（ASCII 树：路线/子技术/关键环节，合并时扩展）
## 技术原理（详细解释：是什么、怎么实现、为什么、解决什么问题，可分小节）
## 关键指标与路线对比（表格）
## 关联技术与应用场景（上下游技术环节、关联技术、用在哪——不列公司）
## 进展动态（倒序：YYYY-MM-DD 事件（来源））
## 来源文章（倒序：YYYY-MM-DD 公众号《标题》）
提取：原理细节优先（机制/参数/设计原因，保留文章解释细节，不只提炼结论）；技术要点（数字优先）；
带日期进展事件；不提取公司格局信息；材料没有的不硬凑，观点标注为观点。
合并：全文档重写保持连贯；原理章节补充深化（不是替换旧解释）；图谱/指标表补新；
进展与来源只增不改；原有内容不轻易删除，被证伪的保留并标注。"""


def load_framework():
    """加载技术档案规范：优先知识库框架文档（用户可改），缺失时用内置兜底。"""
    try:
        with open(TECH_FRAMEWORK_FILE, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return FALLBACK_FRAMEWORK


def tech_dir():
    return os.path.join(config.KNOWLEDGE_DIR, TECH_DIR_KEY)


def list_tech_docs():
    """扫 09_tech 返回 [{"name","path","title","tagline","gist"}]，供路由选择。"""
    d = tech_dir()
    docs = []
    if not os.path.isdir(d):
        return docs
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(5000)
        except OSError:
            continue
        m = re.search(r"^#\s+(.+)$", head, re.M)
        title = m.group(1).strip() if m else os.path.splitext(fn)[0]
        m = re.search(r"^>\s*(.+)$", head, re.M)
        tagline = m.group(1).strip() if m else ""
        m = re.search(r"^##\s*技术原理\s*\n(.+?)(?=^##\s|\Z)", head, re.M | re.S)
        gist = re.sub(r"\s+", " ", m.group(1)).strip()[:150] if m else ""
        docs.append({"name": fn, "path": path, "title": title,
                     "tagline": tagline, "gist": gist})
    return docs


def _parse_json_obj(text):
    """容错解析 JSON 对象：去 fence / 截取首尾花括号。"""
    text = radar_auto._strip_fence(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _sanitize_filename(name, fallback="未命名技术"):
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip()).strip("_")
    return (name or fallback)[:60]


EXTRACT_PROMPT = """你是技术资料整理员，为一级市场投资研究服务。
给你一篇文章（用户精选公众号，偏技术性与市场判断），做两件事：
1. 按下面的档案规范提取技术内容（结构化 JSON）。重点是**把技术原理讲细**——机制、参数、
   为什么这样设计、解决了什么问题，尽量保留文章中的解释细节，不要只提炼结论。
2. 路由：从既有技术文档清单中选最贴切的落入文档；没有贴切的就给一个新文档名。

== 档案规范 ==
{framework}
== 规范结束 ==

既有技术文档清单（文件名 | 标题 — 定位 | 内容摘要）：
{doc_list}

路由规则：只有文章主题与既有文档确实一致（同一技术、同一细分方向）才落入既有文档；
只是沾边、相关但不同主题、或拿不准时，一律填 "NEW"。

输出严格 JSON 对象（不要输出任何其他文字），字段：
- target: 落入既有文档时照抄清单中的文件名（含 .md）；新建时填 "NEW"
- new_title: target 为 NEW 时填技术名（简洁，中文为主、可含英文术语，如 "1.6T光模块"），否则空字符串
- industry: 所属行业（简短）
- one_liner: 一句话定位
- principle: 技术原理的详细解释（是什么、怎么实现、为什么这样做、解决了什么问题，2-5 句，保留文章细节）
- key_points: 数组，3-6 条技术要点（路线/架构/指标/对比/瓶颈，数字优先；观点标注为观点）
- metrics: 数组，关键指标或路线对比条目（每条 "路线/方案：指标=数值，优劣/成熟度"），没有则空数组
- related: 数组，关联技术 / 上下游技术环节 / 应用场景（技术生态信息，不列公司），没有则空数组
- events: 数组，带日期的进展事件（每条 "YYYY-MM-DD 事件"），没有则空数组"""


def extract_and_route(article, docs=None):
    """单篇文章：提取技术要点 + 路由到既有文档/新建。article = {"title","account","date","text"}。"""
    if docs is None:
        docs = list_tech_docs()
    doc_list = "\n".join(
        f"- {d['name']} | {d['title']} — {d['tagline'][:60]} | 内容：{d.get('gist', '')[:100]}"
        for d in docs) or "（空，暂无技术文档）"
    payload = {"title": str(article.get("title", "")).strip(),
               "account": str(article.get("account", "")).strip(),
               "date": str(article.get("date", ""))[:10],
               "text": str(article.get("text", ""))[:ARTICLE_TEXT_LIMIT]}
    out = ""
    ext = {}
    for _ in range(2):  # 输出被截断/为空/解析失败时自动重试一次
        out = chat([
            {"role": "system", "content": EXTRACT_PROMPT.format(framework=load_framework(),
                                                                doc_list=doc_list)},
            {"role": "user", "content": "请提取并路由以下文章：\n" + json.dumps(payload, ensure_ascii=False)},
        ], max_tokens=6000, feature="tech-extract")
        ext = _parse_json_obj(out)
        if ext:
            break
    if not ext:
        raise RuntimeError("模型输出为空或 JSON 解析失败（可能被截断），请重试")
    points = ext.get("key_points")
    related = ext.get("related")
    return {
        "target": str(ext.get("target", "")).strip(),
        "new_title": str(ext.get("new_title", "")).strip(),
        "industry": str(ext.get("industry", "")).strip(),
        "one_liner": str(ext.get("one_liner", "")).strip(),
        "principle": str(ext.get("principle", "")).strip(),
        "key_points": [str(p).strip() for p in points if str(p).strip()][:MAX_KEY_POINTS]
                      if isinstance(points, list) else [],
        "metrics": [str(m).strip() for m in ext.get("metrics", []) if str(m).strip()]
                   if isinstance(ext.get("metrics"), list) else [],
        "related": [str(x).strip() for x in related if str(x).strip()]
                   if isinstance(related, list) else [],
        "events": [str(e).strip() for e in ext.get("events", []) if str(e).strip()]
                  if isinstance(ext.get("events"), list) else [],
        "source_line": "{} {}《{}》".format(str(article.get("date", ""))[:10] or date.today().isoformat(),
                                          str(article.get("account", "")).strip() or "公众号",
                                          str(article.get("title", "")).strip() or "未命名文章"),
    }


MERGE_PROMPT = """你是技术资料整理员。给你一份技术档案的现有全文（可能为空=新建）和一批新提取的要点，
按档案规范把要点合并进文档，输出更新后的完整文档（全文档重写融入，不是末尾堆砌）。

== 档案规范 ==
{framework}
== 规范结束 ==

合并要点：
- 新要点融入对应章节，保持全文连贯；技术图谱补全新路线/子技术/环节
- 指标表补行或更新单元格；不同来源数据冲突时并列保留并注明来源
- 进展动态、来源文章只增不改，保持倒序
- 文档头"更新时间"改为 {today}；"🆕 本次新增"整段重写为本次的 3-5 个要点
- 原有内容不轻易删除；确被新信息证伪的，保留并标注（已被 xx 取代/证伪）
{create_note}
直接输出完整 markdown 文档（不要输出任何其他文字、不要包代码块）。"""


def merge_doc(existing_content, extraction):
    """把提取结果合并进既有文档（空串=新建），返回更新后的完整文档全文。"""
    create_note = "- 本文档为新建：请按档案结构的章节从零生成，标题为技术名。\n" if not existing_content.strip() else ""
    out = chat([
        {"role": "system", "content": MERGE_PROMPT.format(framework=load_framework(),
                                                          today=date.today().isoformat(),
                                                          create_note=create_note)},
        {"role": "user", "content":
            "== 现有文档全文 ==\n" + (existing_content[:DOC_LIMIT] or "（空，新建文档）")
            + "\n\n== 新提取要点 ==\n" + json.dumps(extraction, ensure_ascii=False)},
    ], max_tokens=16000, feature="tech-merge")
    merged = radar_auto._strip_fence(out).strip()
    if not merged:
        raise RuntimeError("模型返回为空（可能被截断），请重试")
    return merged


def resolve_target_name(extraction):
    """根据路由结果给出目标文件名（不含目录）：既有照抄，新建用 new_title 清洗。"""
    target = extraction.get("target", "")
    if target and target != "NEW" and target.endswith(".md"):
        return target
    return _sanitize_filename(extraction.get("new_title") or extraction.get("one_liner")) + ".md"


def save_tech_doc(filename, content):
    """写入 09_tech（已存在则覆盖——合并模式）。返回绝对路径。"""
    d = tech_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _sanitize_filename(os.path.splitext(filename)[0]) + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")
    return path


def process_article(article, docs=None):
    """单篇全流程（CLI 用）：提取路由 → 读既有文档 → 合并 → 写盘。返回 {"path","is_new","ext"}。"""
    ext = extract_and_route(article, docs)
    name = resolve_target_name(ext)
    path = os.path.join(tech_dir(), name)
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    merged = merge_doc(existing, ext)
    if not merged:
        raise RuntimeError("合并失败（模型返回为空）")
    return {"path": save_tech_doc(name, merged), "is_new": not existing, "ext": ext}


def main():
    ap = argparse.ArgumentParser(description="公众号文章 → 技术沉淀（09_tech）")
    ap.add_argument("file", help="文章清单：每行一个 mp.weixin.qq.com 链接（# 开头为注释）")
    ap.add_argument("--dry-run", action="store_true", help="只提取打印，不合并写盘")
    args = ap.parse_args()
    with open(args.file, "r", encoding="utf-8") as f:
        urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    docs = list_tech_docs()
    for u in urls:
        art = radar_wechat.fetch_article(u)
        if art["error"]:
            print(f"[抓取失败] {u}：{art['error']}")
            continue
        ext = extract_and_route(art, docs)
        if args.dry_run:
            print(json.dumps(ext, ensure_ascii=False, indent=2))
            continue
        name = resolve_target_name(ext)
        path = os.path.join(tech_dir(), name)
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        merged = merge_doc(existing, ext)
        out = save_tech_doc(name, merged)
        print(f"[{'新建' if not existing else '合并'}] {name} -> {out}")
        docs = list_tech_docs()  # 后续文章可路由到刚建的文档


if __name__ == "__main__":
    main()
