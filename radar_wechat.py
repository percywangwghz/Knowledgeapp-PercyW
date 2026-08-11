# -*- coding: utf-8 -*-
"""
公众号文章 → 雷达信号 分析模块（可独立 CLI 运行）。

输入公众号文章（mp.weixin.qq.com 链接抓取，或手动粘贴正文），AI 提炼
"可能改变市场认知"的信号：重要程度（高/中/低 + 理由）、对行业认知的影响（why）、
水下公司推导（targets）。去重后写入信号池 radar_signals.json
（origin="wechat"，auto=False，与自动抓取的 🤖 条目区分）。

分析逻辑（system prompt）外置在知识库 `03_frameworks/公众号信号分析框架.md`，
可直接编辑调整，无需改代码；文件缺失时用模块内置的兜底 prompt。

CLI:
    python radar_wechat.py articles.txt [--theme 光通信] [--dry-run]
    articles.txt 每行一个 mp.weixin.qq.com 链接（# 开头为注释）。

数据：与 radar_auto 共用 data/radar_signals.json（经 radar_auto.dedup 判重）。
"""
import argparse
import html as html_mod
import json
import os
import re
from datetime import date

import requests

import radar_auto
from config import KNOWLEDGE_DIR
from llm import chat_with_search

IMPORTANCES = ["高", "中", "低"]
MAX_ARTICLES_PER_RUN = 8   # 单次分析的文章数上限（超出截断，避免上下文过长）
ARTICLE_TEXT_LIMIT = 4000  # 单篇正文送入模型的截断长度
MAX_SIGNALS_PER_RUN = 20   # 单次分析产出的信号上限

# 分析逻辑（system prompt）外置到知识库框架文档，改分析思路直接改文件，不用动代码
WECHAT_FRAMEWORK_FILE = os.path.join(KNOWLEDGE_DIR, "03_frameworks", "公众号信号分析框架.md")

# 内置兜底 prompt：与框架文档同内容，文件缺失时使用
WECHAT_PROMPT = """你是 Investment Radar 的信息采集员，为一级市场投资研究服务。
输入是用户亲手挑选的公众号文章，公众号本身已经完成筛选——因此逐篇都要分析，不做二次筛选：
每篇文章至少产出一条信号；仅当全文与投资研究完全无关（广告、招聘、活动通知）时才跳过。

收录范围（这批公众号偏技术性与市场判断，以下四类都要抓）：
- 产业链信息（供需、订单、产能、价格、格局变化）、科研进展（技术突破、论文、重大学术会议成果）、
  融资热点（融资、并购、IPO、大额资本开支）、市场态度（投资人观点、预期与叙事变化）。

分析要求：
- 技术性文章：提炼技术要点（路线/架构、关键指标、与上一代或竞品对比、瓶颈），并判断对产业链格局的影响。
- 市场判断类文章：提炼作者核心判断（方向、依据、预期差），标注是观点而非事实。
- 区分事实与解释：summary 写发生了什么（事实，含关键数据），why 写对行业认知的影响（市场之前如何理解、现在为何要重新判断）。
- 每条必须能回答"它可能改变市场对什么的理解"。

输出严格 JSON 数组（不要输出任何其他文字），最多 {max_items} 条，每条字段：
- date: 事件或文章日期，"YYYY-MM-DD"，无法判断则空字符串
- source_type: 三选一 ["产业信息", "资本信息", "投资人观点"]
- theme: 从主题列表中选最贴切的一个：{themes}
- event_type: 五选一 ["Technology", "Market", "Capital", "Competitive", "Policy"]
- title: 一句话标题
- source: 公众号名（沿用输入，不要编造）
- summary: 事实摘要，2-3 句，含关键数据
- why: 对行业认知的影响，1-2 句
- importance: "高"/"中"/"低"。高=可能引发叙事转变或重估某产业链环节；中=重要增量，强化或修正现有认知；低=边际补充
- importance_reason: 一句话依据
- sub_track: 所属子赛道（如"稀释制冷机"、"1.6T 光模块"）
- companies: 文章直接涉及的公司（照实填写，没有则空字符串）
- targets: 投资线索推导：信号影响会传导到哪些关联赛道/环节，选 2-4 家优质"水下企业"（一级视角：未上市或低关注度、有真实卡位，不要明牌上市龙头），格式"公司名（一句话理由）"，顿号分隔；用搜索核实公司真实存在且业务匹配，不得编造。{hint_line}"""


def load_wechat_prompt():
    """加载公众号分析 prompt：优先知识库框架文档（用户可改），缺失时用内置兜底。"""
    try:
        with open(WECHAT_FRAMEWORK_FILE, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return WECHAT_PROMPT


# ==================== 文章抓取 ====================

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _meta(page, prop):
    """提取 <meta property/name=prop content=...>，兼容两种属性顺序。"""
    for pat in (r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)' % re.escape(prop),
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop)):
        m = re.search(pat, page)
        if m:
            return html_mod.unescape(m.group(1)).strip()
    return ""


def _strip_tags(fragment):
    fragment = re.sub(r"<script[\s\S]*?</script>", "", fragment)
    fragment = re.sub(r"<style[\s\S]*?</style>", "", fragment)
    text = html_mod.unescape(re.sub(r"<[^>]+>", "\n", fragment))
    return "\n".join(ln for ln in (ln.strip() for ln in text.splitlines()) if ln)


def _extract_article(page):
    """从 mp.weixin.qq.com 文章页 HTML 提取标题/公众号/发布日期/正文。缺啥给空串。"""
    title = _meta(page, "og:title")
    if not title:
        m = re.search(r'<h1[^>]*class="rich_media_title"[^>]*>([\s\S]*?)</h1>', page)
        title = _strip_tags(m.group(1)).replace("\n", " ") if m else ""
    account = ""
    m = re.search(r'var nickname = (?:htmlDecode\()?"([^"]+)"', page)
    if m:
        account = m.group(1).strip()
    if not account:
        account = _meta(page, "og:article:author") or _meta(page, "author")
    pub = ""
    m = re.search(r'var ct = "?(\d{9,11})"?', page)
    if m:
        try:
            pub = date.fromtimestamp(int(m.group(1))).isoformat()
        except (ValueError, OverflowError, OSError):
            pub = ""
    body = ""
    m = re.search(r'id="js_content"[^>]*>([\s\S]*)', page)
    if m:
        frag = m.group(1)
        end = re.search(r'<script[\s>]|<div class="rich_media_tool"', frag)
        if end:
            frag = frag[:end.start()]
        body = _strip_tags(frag)
    return {"title": title, "account": account, "date": pub, "text": body}


def fetch_article(url, timeout=25):
    """抓取单篇公众号文章，返回 {"url","title","account","date","text","error"}。
    微信反爬（环境异常/验证页）时 text 为空、error 给出提示，改手动粘贴正文。"""
    art = {"url": url, "title": "", "account": "", "date": "", "text": "", "error": ""}
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        resp.encoding = resp.apparent_encoding or "utf-8"
        page = resp.text
    except requests.RequestException as e:
        art["error"] = f"网络错误：{e}"
        return art
    if resp.status_code != 200:
        art["error"] = f"HTTP {resp.status_code}"
        return art
    if "js_content" not in page:
        art["error"] = "未取到文章页（可能被反爬拦截），请改用粘贴正文"
        return art
    art.update(_extract_article(page))
    if not art["text"]:
        art["error"] = "未提取到正文（页面结构变化或被拦截），请改用粘贴正文"
    return art


# ==================== AI 分析 ====================

def _articles_payload(articles):
    out = []
    for a in articles[:MAX_ARTICLES_PER_RUN]:
        out.append({
            "title": str(a.get("title", "")).strip(),
            "account": str(a.get("account", "")).strip(),
            "date": str(a.get("date", ""))[:10],
            "text": str(a.get("text", ""))[:ARTICLE_TEXT_LIMIT],
        })
    return out


def parse_wechat_items(text, limit=MAX_SIGNALS_PER_RUN):
    """容错解析信号数组：去 fence / 截取 JSON / 字段白名单校验。
    比 radar_auto.parse_items 多 importance/importance_reason，theme 校验进 THEMES，
    打 origin="wechat"、auto=False 标记。"""
    text = radar_auto._strip_fence(text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        raw = json.loads(text[start:end + 1])
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    items = []
    for it in raw[:limit]:
        if not isinstance(it, dict) or not str(it.get("title", "")).strip():
            continue
        items.append({
            "date": str(it.get("date", ""))[:10],
            "source_type": (it.get("source_type") if it.get("source_type") in radar_auto.SOURCE_TYPES
                            else "产业信息"),
            "theme": it.get("theme") if it.get("theme") in radar_auto.THEMES else "其他",
            "event_type": (it.get("event_type") if it.get("event_type") in radar_auto.EVENT_TYPES
                           else "Market"),
            "title": str(it.get("title", "")).strip(),
            "source": str(it.get("source", "")).strip(),
            "summary": str(it.get("summary", "")).strip(),
            "why": str(it.get("why", "")).strip(),
            "importance": it.get("importance") if it.get("importance") in IMPORTANCES else "中",
            "importance_reason": str(it.get("importance_reason", "")).strip(),
            "sub_track": str(it.get("sub_track", "")).strip(),
            "companies": str(it.get("companies", "")).strip(),
            "targets": str(it.get("targets", "")).strip(),
            "origin": "wechat",
            "auto": False,
        })
    return items


def analyze_articles(articles, theme_hint="", max_items=MAX_SIGNALS_PER_RUN):
    """AI 分析文章列表（[{"title","account","date","text"}...]），返回信号条目（未入池）。"""
    payload = _articles_payload(articles)
    if not payload:
        return []
    hint_line = (f"\n主题提示：这批文章大概率属于「{theme_hint}」，theme 字段优先填它。"
                 if theme_hint else "")
    template = load_wechat_prompt()
    try:
        system = template.format(max_items=max_items, themes="、".join(radar_auto.THEMES),
                                 hint_line=hint_line)
    except (KeyError, IndexError, ValueError):
        system = template  # 框架文档占位符被改坏时原样使用，不阻断分析
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "请分析以下公众号文章并输出信号 JSON 数组：\n"
         + json.dumps(payload, ensure_ascii=False)},
    ]
    return parse_wechat_items(chat_with_search(messages, feature="radar-wechat"),
                              limit=max_items)


def save_signals(items):
    """与信号池判重后追加入库，返回真正新增的条目。"""
    signals = radar_auto._load(radar_auto.SIGNALS_FILE, [])
    added = radar_auto.dedup(signals, items)
    for it in added:
        it.setdefault("origin", "wechat")
        it.setdefault("auto", False)
    signals.extend(added)
    radar_auto._save(radar_auto.SIGNALS_FILE, signals)
    return added


# ==================== CLI ====================

def main():
    ap = argparse.ArgumentParser(description="公众号文章 → 雷达信号")
    ap.add_argument("file", help="文章清单：每行一个 mp.weixin.qq.com 链接（# 开头为注释）")
    ap.add_argument("--theme", default="", help="主题提示（可选）")
    ap.add_argument("--dry-run", action="store_true", help="只分析打印，不写入信号池")
    args = ap.parse_args()
    with open(args.file, "r", encoding="utf-8") as f:
        urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    articles = []
    for u in urls:
        art = fetch_article(u)
        if art["error"]:
            print(f"[抓取失败] {u}：{art['error']}")
        else:
            articles.append(art)
    signals = analyze_articles(articles, theme_hint=args.theme)
    if args.dry_run:
        print(json.dumps(signals, ensure_ascii=False, indent=2))
        return
    added = save_signals(signals)
    print(json.dumps({"fetched": len(articles), "signals": len(signals),
                      "added": len(added)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
