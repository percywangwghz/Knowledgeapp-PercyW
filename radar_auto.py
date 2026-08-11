# -*- coding: utf-8 -*-
"""
Investment Radar 自动抓取 + 认知更新模块（可独立 CLI 运行）。

阶段一（抓取）：用 Moonshot $web_search 按 watchlist 抓近 7 天信息，
  进信号池 radar_signals.json（带 "auto": true，含 sub_track/companies 字段）。
阶段二（认知）：基于近 7 天信号池自动更新主题叙事（radar_themes.json）
  并提炼边际变量（radar_variables.json，带 "auto": true）。

CLI:
    python radar_auto.py [--primary-only] [--dry-run]
                         [--signals-only] [--cognition-only]

数据：
- data/radar_watchlist.json  抓取主题清单（primary=true 的参与周中短跑）
- data/radar_auto.log        jsonl 日志（时间/模式/每主题新增数/认知更新/错误）
"""
import argparse
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from llm import NoApiKeyError, chat, chat_with_search, get_api_key, set_thread_api_key

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")

WATCHLIST_FILE = "radar_watchlist.json"
SIGNALS_FILE = "radar_signals.json"
THEMES_FILE = "radar_themes.json"
VARIABLES_FILE = "radar_variables.json"
LOG_FILE = "radar_auto.log"

THEMES = ["AI Infrastructure", "Foundation Model", "AI Agent", "Robotics", "AI4S",
          "半导体", "光通信", "量子计算", "核聚变", "新能源", "其他"]
SOURCE_TYPES = ["产业信息", "资本信息", "投资人观点"]
EVENT_TYPES = ["Technology", "Market", "Capital", "Competitive", "Policy"]
VAR_TYPES = ["Technology", "Demand", "Capital", "Competitive", "Regulatory"]
MAX_PER_THEME = 10  # 单主题单次运行入库上限（各子查询合并后）
MAX_PER_QUERY = 4   # 单个子查询返回的信号上限
COGNITION_DAYS = 7  # 认知更新回看窗口
MAX_AGE_DAYS = 10   # 信号最大年龄，超龄条目丢弃（模型可能收旧闻）
MAX_WORKERS = 3     # 主题间并行度，避免触发 API 限流

_FILE_LOCK = threading.Lock()  # 并行认知更新时串行化 JSON 文件的读-改-写

DEFAULT_WATCHLIST = {
    "themes": [
        {"theme": "光通信", "primary": True, "enabled": True,
         "query": "光模块 CPO 光互联 硅光 NPO 订单 扩产 云厂商",
         "sub_queries": ["光模块 CPO 光互联 硅光 NPO 订单 扩产 云厂商",
                         "光模块 1.6T 800G 硅光 CPO 技术突破 论文 OFC ECOC 学术会议",
                         "光通信 光芯片 光器件 硅光 融资 并购 IPO 投资",
                         "光模块 CPO 云厂商资本开支 市场预期 投资人观点 订单"]},
        {"theme": "AI Infrastructure", "primary": True, "enabled": True,
         "query": "AI 算力 数据中心 GPU 集群 云厂商资本开支 超节点 建设",
         "sub_queries": ["AI 算力 数据中心 GPU 集群 云厂商资本开支 超节点 建设",
                         "AI 基础设施 超节点 液冷 光互联 供配电 技术发布 OCP Hot Chips",
                         "智算中心 数据中心 算力租赁 融资 订单 中标",
                         "云厂商 资本开支 指引 算力 市场预期 投资人观点"]},
        {"theme": "AI4S", "primary": True, "enabled": True,
         "query": "AI for Science 材料发现 蛋白质 药物研发 科学大模型 产业化",
         "sub_queries": ["AI for Science 材料发现 蛋白质 药物研发 科学大模型 产业化",
                         "AI4S 科学智能 论文 Nature Science NeurIPS 学术会议 突破",
                         "AI4S AI 制药 AI 材料 融资 并购 合作 订单",
                         "AI for Science 产业化 商业化 市场预期 投资人观点"]},
        {"theme": "半导体", "primary": False, "enabled": True,
         "query": "半导体 先进制程 存储 国产替代 涨价 扩产",
         "sub_queries": ["半导体 先进制程 存储 国产替代 涨价 扩产",
                         "半导体 芯片 技术突破 论文 ISSCC IEDM 学术会议 发布",
                         "半导体 设备 材料 EDA 融资 并购 IPO",
                         "半导体 存储 周期 涨价 市场预期 投资人观点"]},
        {"theme": "核聚变", "primary": False, "enabled": True,
         "query": "核聚变 托卡马克 BEST 聚变堆 融资 招标 商业化",
         "sub_queries": ["核聚变 托卡马克 BEST 聚变堆 融资 招标 商业化",
                         "核聚变 点火 能量增益 Q值 技术突破 论文 IAEA 聚变能会议",
                         "核聚变 高温超导 融资 投资 商业化 进展",
                         "核聚变 商业化 政策 市场预期 投资人观点"]},
        {"theme": "量子计算", "primary": False, "enabled": True,
         "query": "量子计算 超导 离子阱 光量子 量子芯片 融资 订单 商业化",
         "sub_queries": ["量子计算 超导 离子阱 光量子 量子芯片 融资 订单 商业化",
                         "量子计算 量子纠错 量子比特 技术突破 论文 Nature APS 学术会议",
                         "量子计算 量子芯片 融资 并购 IPO 订单 采购",
                         "量子计算 商业化 落地 市场预期 投资人观点"]},
    ]
}

SYSTEM_PROMPT = """你是 Investment Radar 的信息采集员，为一级市场投资研究服务。

方法论约束（必须遵守）：
- 只捕捉"可能改变市场认知"的信息，不是收集最多的信息。宁缺毋滥。
- 只收最近 7 天内发生/披露的信息，旧闻不要。
- 收录范围覆盖四类：产业链信息（上下游供需、订单、产能、格局变化）、科研进展（技术突破、论文、重大学术会议发布的成果）、融资热点（融资、并购、IPO、大额资本开支）、市场态度（投资人观点、预期与叙事变化）。
- 区分事实与解释：summary 写发生了什么（事实），why 写为什么可能改变市场认知（解释）。
- 每条信息必须能回答"它可能改变市场对什么的理解"，答不上来的不要收。

请用 $web_search 搜索后，输出严格 JSON 数组（不要输出任何其他文字），最多 {max_items} 条，每条字段：
- date: 事件发生日期，"YYYY-MM-DD"
- source_type: 三选一 ["产业信息", "资本信息", "投资人观点"]
- theme: 固定填 "{theme}"
- event_type: 五选一 ["Technology", "Market", "Capital", "Competitive", "Policy"]
- title: 一句话标题
- source: 来源（媒体/机构/公司名）
- summary: 事实摘要，1-2 句，含关键数据
- why: 为什么可能改变市场认知，1 句
- sub_track: 该机会所属子赛道（如"稀释制冷机"、"1.6T 光模块"）
- companies: 原文直接涉及的公司（事实层，照实填写，没有则空字符串）
- targets: 投资线索推导（关键字段，不是从原文摘公司）：
  1) 先想这条信号的影响会传导到哪些关联赛道/环节（谁受益、谁的格局被改变）；
  2) 在每个关联环节里选 1-2 家优质"水下企业"——一级市场视角：未上市或市场关注度低、但在该环节有真实卡位的公司，不要人尽皆知的上市龙头；
  3) 格式"公司名（一句话理由：卡位什么环节/为什么受益）"，多家用顿号分隔，共 2-4 家；
  4) 用搜索核实公司真实存在且业务匹配，不得编造。

如果没有够格的信息，输出空数组 []。主题：{theme}；搜索方向：{query}{focus_line}"""

NARRATIVE_PROMPT = """你是 Investment Radar 的认知分析师，为一级市场投资研究服务。
基于某主题当前的市场叙事和近期信号池，更新该主题的行业认知库。

== 当前叙事（可能为空）==
__CURRENT__

== 近期信号（JSON 数组）==
__SIGNALS__

输出严格 JSON 对象（不要输出任何其他文字），字段：
{
  "narrative": "市场现在相信什么，1-2 句；信号不足以改变认知时原样保留当前叙事",
  "evidence": "支持证据：引用信号中的事实，含关键数据",
  "views": "代表性观点：来源 + 观点 + 逻辑",
  "consensus": "市场共识",
  "divergence": "核心分歧",
  "is_transition": true 或 false,
  "trigger": "触发事件（仅叙事转变时填，否则空字符串）",
  "meaning": "市场含义（仅叙事转变时填，否则空字符串）",
  "variables": [
    {"title": "", "var_type": "Technology|Demand|Capital|Competitive|Regulatory 五选一",
     "new_info": "发生了什么", "prev_expect": "市场之前如何理解",
     "expect_change": "市场现在如何重新判断",
     "marginal_var": "真正改变判断的因素（不是事件本身）",
     "market_impact": "关注度/估值逻辑/叙事迁移/新参与者",
     "targets": "推导：该变量的影响会传导到哪些关联赛道/环节，选取其中 0-3 家优质水下企业（一级视角：未上市或低关注度、有真实卡位，不要明牌龙头），格式\"公司名（一句话理由）\"，顿号分隔；无把握则空字符串"}
  ]
}
约束：
- is_transition 仅当市场理解方式发生质变时为 true，普通信息增量为 false。
- variables 提炼 0-3 个边际变量，信号不足时返回空数组。
- 一切结论基于信号事实，不得编造信号中没有的信息。"""


# ==================== 存储 ====================

def _path(name):
    return os.path.join(DATA_DIR, name)


def _load(name, default):
    if os.path.exists(_path(name)):
        try:
            with open(_path(name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default  # 文件损坏/被改坏时按空处理，不崩页面
    return default


def _save(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path(name))  # 原子替换，避免并行/中断时读到半个文件


def load_watchlist():
    wl = _load(WATCHLIST_FILE, None)
    if wl is None:
        wl = DEFAULT_WATCHLIST
        _save(WATCHLIST_FILE, wl)
    return wl.get("themes", [])


def last_run_time():
    """读 log 最后一行的时间戳，供 UI 显示。"""
    if not os.path.exists(_path(LOG_FILE)):
        return ""
    last = ""
    with open(_path(LOG_FILE), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return ""
    try:
        return json.loads(last).get("ts", "")
    except ValueError:
        return ""


# ==================== 解析 ====================

def _strip_fence(text):
    return re.sub(r"```\w*", "", text or "").strip()


def parse_items(text, theme, limit=MAX_PER_THEME):
    """容错解析信号数组：去 fence / 截取 JSON / 字段白名单校验。"""
    text = _strip_fence(text)
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
            "source_type": it.get("source_type") if it.get("source_type") in SOURCE_TYPES else "产业信息",
            "theme": theme,
            "event_type": it.get("event_type") if it.get("event_type") in EVENT_TYPES else "Market",
            "title": str(it.get("title", "")).strip(),
            "source": str(it.get("source", "")).strip(),
            "summary": str(it.get("summary", "")).strip(),
            "why": str(it.get("why", "")).strip(),
            "sub_track": str(it.get("sub_track", "")).strip(),
            "companies": str(it.get("companies", "")).strip(),
            "targets": str(it.get("targets", "")).strip(),
        })
    return items


def parse_obj(text):
    """容错解析单个 JSON 对象：去 fence / 截取花括号范围。"""
    text = _strip_fence(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _fresh(items, days=MAX_AGE_DAYS):
    """丢弃超过 days 天的条目；日期缺失/无法解析的保留（交由人去重判断）。"""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [it for it in items
            if not it.get("date") or not re.match(r"\d{4}-\d{2}-\d{2}", it["date"])
            or it["date"][:10] >= cutoff]


# ==================== 阶段一：抓取 ====================

_FETCH_STATS = {}  # theme -> [{"q": 子查询, "n": 返回条数}]，每次 run() 开始时清空
DEBUG_LOG = "radar_fetch_debug.jsonl"  # 逐子查询实时日志：每条完成即追加，中途被杀不丢


def _debug_write(entry):
    """逐条追加调试日志（实时落盘，用于观察长任务中间状态）。"""
    entry = dict(entry, ts=datetime.now().isoformat(timespec="seconds"))
    try:
        with _FILE_LOCK:
            with open(_path(DEBUG_LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _default_sub_queries(theme, query, focus=""):
    """watchlist 未配置 sub_queries 时的默认多角度子查询：原关键词串 + 产业链/科研会议/资本市场角度。"""
    return [
        query,
        f"{theme} 产业链 订单 供需 产能 格局",
        f"{theme} 科研进展 学术会议 论文 技术突破",
        f"{theme} 融资 并购 IPO 投资人观点 市场预期",
    ]


def _fetch_one_query(theme, query, focus="", thinking=True):
    """按单个子查询搜索一次，返回信号条目（上限 MAX_PER_QUERY）并记录统计。
    thinking 参数保留给 llm 层做实验；生产上必须 True——关思考实测全灭。"""
    focus_line = f"\n额外关注：{focus}" if focus else ""
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(
            max_items=MAX_PER_QUERY, theme=theme, query=query, focus_line=focus_line)},
        {"role": "user", "content": f"今天是 {today}。请搜索并输出「{theme}」主题最近 7 天"
                                   f"（{cutoff} 之后）值得收录的信号；先核对每条的日期，旧闻不要。"},
    ]
    try:
        raw = chat_with_search(messages, max_tokens=32768, feature="radar",
                               thinking=thinking)
    except Exception as e:  # 单个子查询失败不拖垮整个主题，记下错误继续
        stat = {"q": query, "n": 0, "raw": f"ERROR: {e}"[:150]}
        _FETCH_STATS.setdefault(theme, []).append(stat)
        _debug_write({"theme": theme, "titles": [], **stat})
        if isinstance(e, NoApiKeyError):
            raise  # 无 key 是全局性失败，不能吞掉伪装成「+0 成功」
        return []
    items = parse_items(raw, theme, limit=MAX_PER_QUERY)
    stat = {"q": query, "n": len(items), "raw": raw[:150] if not items else ""}
    _FETCH_STATS.setdefault(theme, []).append(stat)
    _debug_write({"theme": theme, "titles": [i["title"] for i in items], **stat})
    return items


def fetch_theme(theme, query, focus="", thinking=True):
    """抓一个主题：query 为列表时按给定子查询逐个搜索，为字符串时拆默认多角度子查询；
    各子查询结果合并、主题内标题判重，上限 MAX_PER_THEME。返回信号条目列表（未与信号池去重）。"""
    queries = [q for q in (query if isinstance(query, list) else
                           _default_sub_queries(theme, query, focus)) if q]
    seen, items = set(), []
    for q in queries:
        for it in _fetch_one_query(theme, q, focus, thinking=thinking):
            key = _norm_title(it["title"])
            if key and key not in seen:
                seen.add(key)
                items.append(it)
    return items[:MAX_PER_THEME]


def _norm_title(title):
    return re.sub(r"[\s\W_]+", "", str(title)).lower()


def dedup(existing, new_items):
    """标题归一化判重：与已有池及本批次内部比对，返回真正新增的条目。"""
    seen = {_norm_title(s.get("title")) for s in existing}
    added = []
    for it in new_items:
        key = _norm_title(it.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        added.append(it)
    return added


# ==================== 阶段二：认知更新 ====================

def _recent_signals(theme, days=COGNITION_DAYS):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [s for s in _load(SIGNALS_FILE, [])
            if s.get("theme") == theme and str(s.get("date", ""))[:10] >= cutoff]


def update_cognition(theme, days=COGNITION_DAYS):
    """基于近 days 天信号更新主题叙事并提炼边际变量。返回更新摘要。"""
    recent = _recent_signals(theme, days)
    result = {"theme": theme, "updated": False, "variables": 0, "signals": len(recent)}
    if not recent:
        return result

    themes = _load(THEMES_FILE, {})
    cur = themes.get(theme, {}).get("current", {})
    prompt = (NARRATIVE_PROMPT
              .replace("__CURRENT__", json.dumps(cur, ensure_ascii=False) if cur else "（空，请建立第一版）")
              .replace("__SIGNALS__", json.dumps(recent[-20:], ensure_ascii=False)))
    obj = parse_obj(chat([
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"请更新「{theme}」主题的行业认知。"},
    ], max_tokens=6000, feature="radar"))
    if not obj:
        return result

    with _FILE_LOCK:  # 主题间并行时串行化读-改-写，重新读入避免覆盖其他线程的更新
        themes = _load(THEMES_FILE, {})
        entry = themes.get(theme, {"current": {}, "history": []})
        cur = entry.get("current", {})
        today = date.today().isoformat()
        new_narr = str(obj.get("narrative", "")).strip()
        if new_narr:
            if cur.get("narrative") and cur["narrative"] != new_narr:
                entry.setdefault("history", []).append({
                    "date": today, "previous": cur["narrative"], "new": new_narr,
                    "trigger": str(obj.get("trigger", "")).strip(),
                    "meaning": str(obj.get("meaning", "")).strip(),
                    "is_transition": bool(obj.get("is_transition")),
                })
            elif not cur.get("narrative"):
                entry.setdefault("history", []).append({
                    "date": today, "previous": "（首次建立）", "new": new_narr,
                    "trigger": "", "meaning": "", "is_transition": True,
                })
            entry["current"] = {k: str(obj.get(k, "")).strip()
                                for k in ("narrative", "evidence", "views", "consensus", "divergence")}
            entry["updated"] = today
            themes[theme] = entry
            _save(THEMES_FILE, themes)
            result["updated"] = True

        raw_vars = obj.get("variables")
        if isinstance(raw_vars, list) and raw_vars:
            cleaned = []
            for v in raw_vars[:3]:
                if not isinstance(v, dict) or not str(v.get("title", "")).strip():
                    continue
                cleaned.append({
                    "date": today,
                    "title": str(v.get("title", "")).strip(),
                    "var_type": v.get("var_type") if v.get("var_type") in VAR_TYPES else "Technology",
                    "theme": theme,
                    "new_info": str(v.get("new_info", "")).strip(),
                    "prev_expect": str(v.get("prev_expect", "")).strip(),
                    "expect_change": str(v.get("expect_change", "")).strip(),
                    "marginal_var": str(v.get("marginal_var", "")).strip(),
                    "market_impact": str(v.get("market_impact", "")).strip(),
                    "targets": str(v.get("targets", "")).strip(),
                    "auto": True,
                })
            if cleaned:
                variables = _load(VARIABLES_FILE, [])
                added = dedup(variables, cleaned)
                variables.extend(added)
                _save(VARIABLES_FILE, variables)
                result["variables"] = len(added)
    return result


# ==================== 主流程 ====================

def run(primary_only=False, dry_run=False, do_fetch=True, do_cognition=True, progress=None,
        only=None):
    """抓取 → 去重入池 → 认知更新 → 写日志。返回摘要 dict。
    主题间并行（ThreadPoolExecutor，max_workers=MAX_WORKERS）。
    progress 为可选回调 progress(stage, theme, status, detail)，
    stage 为 "fetch"/"cognition"，status 为 "running"/"done"/"error"。
    only 为可选主题名，指定后只跑该主题（调试用）。"""
    themes = [t for t in load_watchlist()
              if t.get("enabled", True) and (not primary_only or t.get("primary"))]
    if only:
        themes = [t for t in themes if t.get("theme") == only]
    results, cognition, errors = {}, {}, []
    _FETCH_STATS.clear()
    # 并行 worker 线程内取不到前端会话注入的 key（无 ScriptRunContext），
    # 在本线程取好后逐个注入
    api_key = get_api_key()

    def _with_key(fn, *args):
        if api_key:
            set_thread_api_key(api_key)
        return fn(*args)

    def _report(stage, theme, status, detail=""):
        if progress:
            try:
                progress(stage, theme, status, detail)
            except Exception:
                pass

    if do_fetch:
        signals = _load(SIGNALS_FILE, [])

        def _fetch_one(t):
            theme = t["theme"]
            # 实测：关思考（快模式）下模型会在正文里"表演"搜索（$web_search 幻觉）
            # 而非发起真实 tool_call，复杂提示词下全灭——一律开思考
            thinking = True
            q = t.get("sub_queries") or t.get("query", theme)
            items = _fresh(fetch_theme(theme, q, t.get("focus", ""), thinking=thinking))
            if not items:  # 搜索波动导致空结果时，换关键词重试一次
                items = _fresh(fetch_theme(theme, f"{t.get('query', theme)} 最新进展 本周",
                                           t.get("focus", ""), thinking=thinking))
            return theme, items

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for t in themes:
                futures[pool.submit(_with_key, _fetch_one, t)] = t["theme"]
                _report("fetch", t["theme"], "running")
            for fut in as_completed(futures):
                theme = futures[fut]
                try:
                    _, items = fut.result()
                except Exception as e:  # 单主题失败不中断整体
                    results[theme] = 0
                    errors.append(f"抓取-{theme}: {e}")
                    _report("fetch", theme, "error", str(e))
                    continue
                added = dedup(signals, items)
                results[theme] = len(added)
                if not dry_run:
                    for it in added:
                        it["auto"] = True
                    signals.extend(added)
                _report("fetch", theme, "done", f"+{len(added)}")
        if not dry_run:
            _save(SIGNALS_FILE, signals)

    if do_cognition and not dry_run:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for t in themes:
                futures[pool.submit(_with_key, update_cognition, t["theme"])] = t["theme"]
                _report("cognition", t["theme"], "running")
            for fut in as_completed(futures):
                theme = futures[fut]
                try:
                    cognition[theme] = fut.result()
                    _report("cognition", theme, "done",
                            f"叙事{'✓' if cognition[theme]['updated'] else '不变'} "
                            f"变量+{cognition[theme]['variables']}")
                except Exception as e:
                    errors.append(f"认知-{theme}: {e}")
                    _report("cognition", theme, "error", str(e))

    mode = ("primary" if primary_only else "full") + ("-dry" if dry_run else "")
    if not do_fetch:
        mode += "-cogonly"
    if not do_cognition:
        mode += "-sigonly"
    log_entry = {"ts": datetime.now().isoformat(timespec="seconds"), "mode": mode,
                 "results": results,
                 "fetch_detail": {t: _FETCH_STATS.get(t, []) for t in results},
                 "cognition": {k: {"updated": v["updated"], "variables": v["variables"]}
                               for k, v in cognition.items()},
                 "errors": errors}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_path(LOG_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    return log_entry


def main():
    ap = argparse.ArgumentParser(description="Investment Radar 自动抓取 + 认知更新")
    ap.add_argument("--primary-only", action="store_true", help="只跑 primary 主题（周中短跑）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取打印，不写入信号池（不跑认知更新）")
    ap.add_argument("--signals-only", action="store_true", help="只做抓取，跳过认知更新")
    ap.add_argument("--cognition-only", action="store_true", help="跳过抓取，只基于现有信号池做认知更新")
    ap.add_argument("--only", metavar="主题名", help="只跑指定主题（调试用）")
    args = ap.parse_args()
    summary = run(primary_only=args.primary_only, dry_run=args.dry_run,
                  do_fetch=not args.cognition_only, do_cognition=not args.signals_only,
                  only=args.only)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
