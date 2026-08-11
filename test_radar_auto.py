# -*- coding: utf-8 -*-
"""radar_auto 单测：dedup 判重 + AI 输出容错解析（不触网）"""
import datetime as _dt
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import radar_auto as ra

orig_dir = ra.DATA_DIR
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK]   {name}")
    else:
        failures.append(name)
        print(f"[FAIL] {name} {detail}")


# ---------- parse_items 容错 ----------
THEME = "光通信"
GOOD = {"date": _dt.date.today().isoformat(), "source_type": "产业信息", "theme": "忽略我",
        "event_type": "Market", "title": "某厂商 1.6T 光模块获云厂商订单",
        "source": "财联社", "summary": "……", "why": "……"}

# 1. 带 markdown fence 的合法输出
items = ra.parse_items(f"```json\n[{__import__('json').dumps(GOOD, ensure_ascii=False)}]\n```", THEME)
check("fence 解析", len(items) == 1 and items[0]["title"] == GOOD["title"])
check("theme 强制覆盖", items and items[0]["theme"] == THEME)

# 2. 带废话前缀后缀
import json
payload = json.dumps([GOOD], ensure_ascii=False)
items = ra.parse_items(f"好的，以下是结果：\n{payload}\n希望对你有帮助！", THEME)
check("废话包裹解析", len(items) == 1)

# 3. 完全非 JSON 输出 → 空表
check("非 JSON → []", ra.parse_items("抱歉，我没有找到相关信息。", THEME) == [])

# 4. 非法 source_type / event_type 兜底
bad = dict(GOOD, source_type="小道消息", event_type="八卦")
items = ra.parse_items(json.dumps([bad], ensure_ascii=False), THEME)
check("非法枚举兜底", items and items[0]["source_type"] == "产业信息"
      and items[0]["event_type"] == "Market")

# 4b. targets 字段保留
with_t = dict(GOOD, targets="某公司（卡位稀释制冷机）", companies="原文公司A")
items = ra.parse_items(json.dumps([with_t], ensure_ascii=False), THEME)
check("targets/companies 保留", items and items[0]["targets"] == "某公司（卡位稀释制冷机）"
      and items[0]["companies"] == "原文公司A")

# 5. 缺标题条目被丢弃；超长截断到 MAX_PER_THEME
no_title = dict(GOOD, title="")
many = [dict(GOOD, title=f"标题{i}") for i in range(ra.MAX_PER_THEME + 3)] + [no_title]
items = ra.parse_items(json.dumps(many, ensure_ascii=False), THEME)
check("截断到上限且丢无标题", len(items) == ra.MAX_PER_THEME, f"got {len(items)}")

# ---------- dedup ----------
existing = [{"title": "某厂商 1.6T 光模块获云厂商订单"}, {"title": "另一条旧闻"}]
new = [
    {"title": "某厂商1.6T光模块获云厂商订单"},      # 空白差异 → 重复
    {"title": "某厂商 1.6T 光模块获云厂商订单！"},   # 标点差异 → 重复
    {"title": "某厂商 1.6T 光模块获云厂商订单"},     # 与上两条互重 → 重复
    {"title": "全新的一条新闻"},                      # 新增
    {"title": ""},                                    # 空标题 → 丢弃
]
added = ra.dedup(existing, new)
check("dedup 归一化判重", [a["title"] for a in added] == ["全新的一条新闻"],
      f"got {[a['title'] for a in added]}")

# ---------- _fresh 超龄过滤 ----------
import datetime as _dt
_today = _dt.date.today().isoformat()
_old = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
fresh_in = [{"title": "新", "date": _today}, {"title": "旧", "date": _old},
            {"title": "无日期", "date": ""}, {"title": "坏日期", "date": "本周"}]
check("_fresh 滤旧留新", [i["title"] for i in ra._fresh(fresh_in)] == ["新", "无日期", "坏日期"])

# ---------- 空结果重试一次 ----------
with tempfile.TemporaryDirectory() as tmp:
    ra.DATA_DIR = tmp
    calls = {"n": 0}
    def flaky(theme, query, focus="", thinking=True):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [dict(GOOD, title="重试得来", date=_today)]
    with mock.patch.object(ra, "fetch_theme", side_effect=flaky), \
         mock.patch.object(ra, "update_cognition", return_value={"updated": False, "variables": 0}), \
         mock.patch.object(ra, "load_watchlist",
                          return_value=[{"theme": "T", "query": "q", "primary": True, "enabled": True}]):
        summary = ra.run()
    check("空结果重试后入库", summary["results"].get("T") == 1 and calls["n"] == 2)
ra.DATA_DIR = orig_dir

# ---------- watchlist 落盘默认值（用临时目录隔离，不动真实数据）----------
with tempfile.TemporaryDirectory() as tmp:
    ra.DATA_DIR = tmp
    themes = ra.load_watchlist()
    check("watchlist 首次生成", len(themes) == 6 and os.path.exists(ra._path(ra.WATCHLIST_FILE)))
    check("primary 恰为前 3 个", sum(1 for t in themes if t.get("primary")) == 3)
    entry = ra.run.__wrapped__ if hasattr(ra.run, "__wrapped__") else None  # noqa
    # dry-run 不写 signals 但写 log
    with mock.patch.object(ra, "fetch_theme", return_value=[dict(GOOD)]):
        summary = ra.run(dry_run=True)
    check("dry-run 不写 signals", not os.path.exists(ra._path(ra.SIGNALS_FILE)))
    check("dry-run 写 log 且带 -dry", summary["mode"].endswith("-dry")
          and sum(summary["results"].values()) == 6)
    with mock.patch.object(ra, "fetch_theme",
                           side_effect=lambda theme, query, focus="", thinking=True: [dict(GOOD, title=f"{theme}新闻")]), \
         mock.patch.object(ra, "update_cognition",
                           return_value={"theme": "", "updated": True, "variables": 1, "signals": 3}) as cog:
        summary = ra.run(primary_only=True)
    sigs = ra._load(ra.SIGNALS_FILE, [])
    check("primary-only 只跑 3 主题", len(summary["results"]) == 3)
    check("实跑写入且带 auto 标记", len(sigs) == 3 and all(s.get("auto") for s in sigs))
    check("认知阶段跟随实跑", cog.call_count == 3 and len(summary["cognition"]) == 3)
    check("last_run_time 可读", bool(ra.last_run_time()))
ra.DATA_DIR = orig_dir

# ---------- parse_obj 容错 ----------
check("parse_obj 正常", ra.parse_obj('前缀 {"a": 1, "b": [2]} 后缀') == {"a": 1, "b": [2]})
check("parse_obj 非对象 → None", ra.parse_obj("[1,2,3]") is None)
check("parse_obj 废话 → None", ra.parse_obj("没有任何 JSON") is None)

# ---------- update_cognition（chat 打桩）----------
NARR = {"narrative": "市场相信 NPO 是过渡主线", "evidence": "腾讯云 Q4 部署",
        "views": "券商：受益顺序光引擎先行", "consensus": "放量确定",
        "divergence": "CPO 与 NPO 路线之争", "is_transition": True,
        "trigger": "腾讯云给出商用时间表", "meaning": "渗透率上修",
        "variables": [{"title": "NPO 商用时间表提前", "var_type": "Demand",
                       "new_info": "x", "prev_expect": "y", "expect_change": "z",
                       "marginal_var": "m", "market_impact": "i",
                       "targets": "某光引擎初创（NPO 卡位）"},
                      {"var_type": "乱"}]}
with tempfile.TemporaryDirectory() as tmp:
    ra.DATA_DIR = tmp
    today = __import__("datetime").date.today().isoformat()
    ra._save(ra.SIGNALS_FILE, [dict(GOOD, theme=THEME, date=today, title="信号A")])
    with mock.patch.object(ra, "chat", return_value=json.dumps(NARR, ensure_ascii=False)):
        res = ra.update_cognition(THEME)
    themes = ra._load(ra.THEMES_FILE, {})
    variables = ra._load(ra.VARIABLES_FILE, [])
    check("认知更新写叙事", res["updated"] and themes[THEME]["current"]["narrative"] == NARR["narrative"])
    check("首次建立入历史", themes[THEME]["history"][0]["is_transition"] is True)
    check("变量入库且带 auto", len(variables) == 1 and variables[0]["auto"] is True
          and variables[0]["theme"] == THEME and variables[0].get("targets") == "某光引擎初创（NPO 卡位）")
    # 信号不足 → 不更新
    with mock.patch.object(ra, "chat", return_value="{}"):
        res2 = ra.update_cognition("核聚变")
    check("无信号不更新", res2["updated"] is False and res2["variables"] == 0)
ra.DATA_DIR = orig_dir

# ---------- 多角度子查询抓取 ----------
# _default_sub_queries：原关键词串 + 产业/资本/技术政策 4 个角度
qs = ra._default_sub_queries("光通信", "光模块 CPO 订单")
check("默认子查询 4 角度", len(qs) == 4 and qs[0] == "光模块 CPO 订单"
      and any("订单" in q for q in qs) and any("融资" in q for q in qs))

# fetch_theme：多子查询合并 + 主题内判重 + 统计记录
with mock.patch.object(ra, "chat_with_search") as cws, \
        mock.patch.object(ra, "_debug_write"):  # 不污染真实调试日志
    cws.side_effect = [
        json.dumps([dict(GOOD, title="新闻A"), dict(GOOD, title="新闻B")], ensure_ascii=False),
        json.dumps([dict(GOOD, title="新闻A"), dict(GOOD, title="新闻C")], ensure_ascii=False),
    ]
    ra._FETCH_STATS.clear()
    merged = ra.fetch_theme(THEME, ["子查询1", "子查询2"])
check("多子查询合并去重", [i["title"] for i in merged] == ["新闻A", "新闻B", "新闻C"])
check("子查询统计记录", ra._FETCH_STATS.get(THEME) == [{"q": "子查询1", "n": 2, "raw": ""},
                                                    {"q": "子查询2", "n": 2, "raw": ""}])
check("子查询各调一次", cws.call_count == 2)

# 字符串 query 走默认多角度（4 次调用）
with mock.patch.object(ra, "chat_with_search", return_value="[]") as cws, \
        mock.patch.object(ra, "_debug_write"):
    ra.fetch_theme(THEME, "光模块 CPO 订单")
check("字符串 query 拆 4 角度", cws.call_count == 4)

print()
if failures:
    print(f"{len(failures)} test(s) failed")
    sys.exit(1)
print("ALL RADAR_AUTO TESTS OK")
