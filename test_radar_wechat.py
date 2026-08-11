# -*- coding: utf-8 -*-
"""radar_wechat 单测：文章页提取 + 信号容错解析 + 分析/入库流程（不触网）"""
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import radar_auto as ra
import radar_wechat as rw

orig_dir = ra.DATA_DIR
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK]   {name}")
    else:
        failures.append(name)
        print(f"[FAIL] {name} {detail}")


# ---------- 文章页 HTML 提取 ----------
PAGE = """<html><head>
<meta property="og:title" content="CPO 产业趋势深度：谁在受益" />
<meta content="半导体行业观察" property="og:article:author" />
<script>var nickname = "半导体行业观察"; var ct = "1754000000";</script>
</head><body>
<div class="rich_media_content" id="js_content" >
<p>第一段：某厂商发布 1.6T 光模块。</p>
<p>第二段：订单 &amp; 扩产。</p>
</div>
<div class="rich_media_tool">footer</div>
</body></html>"""

art = rw._extract_article(PAGE)
check("标题提取 og:title", art["title"] == "CPO 产业趋势深度：谁在受益", art["title"])
check("公众号提取 nickname", art["account"] == "半导体行业观察", art["account"])
check("日期提取 ct", bool(__import__("re").match(r"\d{4}-\d{2}-\d{2}", art["date"])), art["date"])
check("正文提取且去标签", "第一段" in art["text"] and "第二段" in art["text"]
      and "<p>" not in art["text"] and "footer" not in art["text"])
check("实体转义", "&amp;" not in art["text"] and "&" in art["text"])

# nickname 缺失时回退 og:article:author
page2 = PAGE.replace('var nickname = "半导体行业观察"; ', "")
check("公众号回退 author meta", rw._extract_article(page2)["account"] == "半导体行业观察")


# ---------- parse_wechat_items 容错 ----------
GOOD = {"date": "2026-08-01", "source_type": "产业信息", "theme": "光通信",
        "event_type": "Market", "title": "某厂商 1.6T 光模块获云厂商大单",
        "source": "半导体行业观察", "summary": "……", "why": "……",
        "importance": "高", "importance_reason": "可能重估光模块环节",
        "sub_track": "1.6T 光模块", "companies": "某厂商",
        "targets": "某光引擎初创（NPO 卡位）"}

items = rw.parse_wechat_items(f"```json\n[{json.dumps(GOOD, ensure_ascii=False)}]\n```")
check("fence 解析", len(items) == 1 and items[0]["title"] == GOOD["title"])
check("importance/理由保留", items and items[0]["importance"] == "高"
      and items[0]["importance_reason"] == "可能重估光模块环节")
check("origin/auto 标记", items and items[0]["origin"] == "wechat" and items[0]["auto"] is False)

bad = dict(GOOD, source_type="小道消息", event_type="八卦", theme="火星产业", importance="爆表")
items = rw.parse_wechat_items(json.dumps([bad], ensure_ascii=False))
check("非法枚举兜底", items and items[0]["source_type"] == "产业信息"
      and items[0]["event_type"] == "Market" and items[0]["theme"] == "其他"
      and items[0]["importance"] == "中")

check("非 JSON → []", rw.parse_wechat_items("这些文章没有有价值的信息。") == [])

many = [dict(GOOD, title=f"标题{i}") for i in range(3)] + [dict(GOOD, title="")]
items = rw.parse_wechat_items(json.dumps(many, ensure_ascii=False), limit=2)
check("缺标题丢弃 + limit 截断", len(items) == 2)


# ---------- analyze_articles（mock LLM） ----------
captured = {}


def fake_chat(messages, feature="unknown"):
    captured["messages"] = messages
    captured["feature"] = feature
    return json.dumps([GOOD], ensure_ascii=False)


with mock.patch.object(rw, "chat_with_search", side_effect=fake_chat):
    sigs = rw.analyze_articles([{"title": "t", "account": "a", "date": "2026-08-01",
                                 "text": "x" * 5000}], theme_hint="光通信")
check("analyze 返回解析信号", len(sigs) == 1 and sigs[0]["origin"] == "wechat")
check("正文截断进 payload", "x" * rw.ARTICLE_TEXT_LIMIT in captured["messages"][1]["content"]
      and "x" * (rw.ARTICLE_TEXT_LIMIT + 1) not in captured["messages"][1]["content"])
check("主题提示进 prompt", "光通信" in captured["messages"][0]["content"]
      and "主题提示" in captured["messages"][0]["content"])
check("feature 记账标记", captured.get("feature") == "radar-wechat")

with mock.patch.object(rw, "chat_with_search", side_effect=fake_chat) as cws:
    check("空文章不调 LLM", rw.analyze_articles([]) == [] and cws.call_count == 0)


# ---------- save_signals 判重入库 ----------
ra.DATA_DIR = tempfile.mkdtemp()
existing = [dict(GOOD, title="已有信号")]  # 池里已有
ra._save(ra.SIGNALS_FILE, existing)
added = rw.save_signals([dict(GOOD, title="已有信号"), dict(GOOD, title="新信号")])
pool = ra._load(ra.SIGNALS_FILE, [])
check("判重只入新增", len(added) == 1 and added[0]["title"] == "新信号" and len(pool) == 2)
check("入池保留 importance", pool[-1]["importance"] == "高" and pool[-1]["origin"] == "wechat")
ra.DATA_DIR = orig_dir

print()
if failures:
    print(f"{len(failures)} test(s) failed")
    sys.exit(1)
print("ALL RADAR_WECHAT TESTS OK")
