# -*- coding: utf-8 -*-
"""
Investment Radar（投资机会雷达）模块
构建"市场认知追踪层"：信息采集 → 结构化 → 叙事提取 → 演变追踪 → 边际变量 → 周度认知报告。
不输出投资决策，产出供 Belief Tracker / Thesis Battle 使用。

信息采集、主题叙事、边际变量均由 radar_auto 自动维护（抓取 + 认知更新），
本模块只提供看板视图与周报生成。

数据存储（knowledge_app/data/）：
- radar_signals.json    信息信号池
- radar_themes.json     主题叙事（当前 + 演变历史）
- radar_variables.json  边际变量
周报写回知识库 05_tracking/。
"""
import html
import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote as _urlquote

import streamlit as st

import llm
import radar_auto
import radar_wechat
import tech
from config import KNOWLEDGE_DIR

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")

THEMES = radar_auto.THEMES
SOURCE_TYPES = ["产业信息", "资本信息", "投资人观点"]
EVENT_TYPES = ["Technology", "Market", "Capital", "Competitive", "Policy"]
VAR_TYPES = ["Technology", "Demand", "Capital", "Competitive", "Regulatory"]

# 数据词典（AI 写入 JSON 的枚举值，勿改动）→ UI 显示中文
_EVENT_TYPE_CN = {"Technology": "技术", "Market": "市场", "Capital": "资本",
                  "Competitive": "竞争", "Policy": "政策"}
_VAR_TYPE_CN = {"Technology": "技术", "Demand": "需求", "Capital": "资本",
                "Competitive": "竞争", "Regulatory": "监管"}

REPORT_CATEGORY = "05_tracking"

JOB_FILE = "radar_job.json"  # 后台抓取任务的进度/结果落盘文件

_job_thread = None  # 后台抓取线程（模块级变量，跨 rerun 存活）


# ==================== 后台抓取任务 ====================

def _job_running():
    return _job_thread is not None and _job_thread.is_alive()


def _read_job():
    return _load(JOB_FILE, None)


def _write_job(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path(JOB_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    for attempt in range(5):  # Windows 上读者持有句柄时 os.replace 会被拒，短暂重试
        try:
            os.replace(tmp, _path(JOB_FILE))
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, _path(JOB_FILE))


def _run_job(primary_only, api_key, model=None, base_url=None):
    """后台线程体：执行 radar_auto.run，进度实时落盘 radar_job.json。
    注意：线程内没有 ScriptRunContext，禁止调用任何 st.*；
    前端注入的 API Key、模型与端点同样取不到，由 api_key/model/base_url 参数显式带入。"""
    llm.set_thread_api_key(api_key)
    llm.set_thread_model(model)
    llm.set_thread_base_url(base_url)
    state = {"status": "running", "mode": "primary" if primary_only else "full",
             "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "", "steps": [], "summary": None}
    _write_job(state)
    lock = threading.Lock()

    def progress(stage, theme, status, detail=""):
        with lock:
            state["steps"] = [s for s in state["steps"]
                              if not (s["stage"] == stage and s["theme"] == theme)]
            state["steps"].append({"stage": stage, "theme": theme,
                                   "status": status, "detail": detail})
            _write_job(state)

    try:
        state["summary"] = radar_auto.run(primary_only=primary_only, progress=progress)
        state["status"] = "done"
    except llm.SearchNotSupportedError as e:
        # 厂家无联网搜索能力：全局性配置失败，打标记由 UI 显著提示（不显示堆栈）
        state["status"] = "error"
        state["no_search"] = True
        state["summary"] = {"results": {}, "cognition": {}, "errors": [str(e)]}
    except Exception as e:
        state["status"] = "error"
        state["summary"] = {"results": {}, "cognition": {}, "errors": [str(e)]}
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_job(state)


def _start_job(primary_only):
    global _job_thread
    if _job_running():
        return
    # 主线程取好当前会话的 API Key、模型与端点传给工作线程（线程内访问不了 session_state）
    _job_thread = threading.Thread(target=_run_job,
                                   args=(primary_only, llm.get_api_key(),
                                         llm.get_model(), llm.get_base_url()), daemon=True)
    _job_thread.start()


_STAGE_LABEL = {"fetch": "抓取", "cognition": "认知"}
_STATUS_ICON = {"running": "⏳", "done": "✅", "error": "❌"}


def _steps_html(steps):
    """任务步骤列表 → 一段 HTML 字符串（供单元素渲染与中断提示复用）。"""
    if not steps:
        return "<div class='caption'>正在启动任务…</div>"
    lines = []
    for s in steps:
        icon = _STATUS_ICON.get(s.get("status"), "")
        stage = _STAGE_LABEL.get(s.get("stage"), s.get("stage", ""))
        theme = html.escape(str(s.get("theme", "")))
        detail = ""
        if s.get("detail") and s.get("status") != "running":
            detail = f"（{html.escape(str(s['detail'])[:200])}）"
        lines.append(f"{icon} {stage} · {theme} {detail}")
    return "<div class='caption'>" + "<br>".join(lines) + "</div>"


def _render_job_steps(steps):
    st.markdown(_steps_html(steps), unsafe_allow_html=True)


def _steps_text(steps):
    """任务步骤列表 → 纯文本（fragment 轮询用：st.text 单文本节点原地更新，
    无 HTML 解析、无子节点增删，规避 React removeChild 崩溃）。"""
    if not steps:
        return "正在启动任务…"
    lines = []
    for s in steps:
        icon = _STATUS_ICON.get(s.get("status"), "")
        stage = _STAGE_LABEL.get(s.get("stage"), s.get("stage", ""))
        detail = ""
        if s.get("detail") and s.get("status") != "running":
            detail = f"（{str(s['detail'])[:200]}）"
        lines.append(f"{icon} {stage} · {s.get('theme', '')} {detail}")
    return "\n".join(lines)


@st.fragment(run_every=3)
def _render_job_live():
    """任务运行中每 3 秒轮询 radar_job.json 刷新进度。
    固定挂载 + 纯文本单元素渲染（st.text 原地更新，无 HTML 重解析），
    结束边沿按 started 时间戳去重、只触发一次整页 rerun。"""
    job = _read_job()
    if not job:
        return
    if job.get("status") == "running" and not _job_running():
        return  # 线程不在（中断/重启）：交给主流程的中断提示分支
    if job.get("status") != "running":
        token = job.get("started", "")
        if st.session_state.get("_live_fired_radar") != token:
            st.session_state["_live_fired_radar"] = token
            st.rerun()  # 整页 rerun，由主流程展示最终结果
        return
    banner = (f"⏳ 自动任务进行中（{job.get('mode', '')}，开始于 {job.get('started', '—')}）。"
              "进度实时落盘，可自由切换页面/刷新。")
    st.text(f"{banner}\n\n{_steps_text(job.get('steps', []))}")


def _render_job_result(job):
    summary = job.get("summary") or {}
    errors = summary.get("errors") or []
    if job.get("no_search"):
        # 厂家无联网搜索能力：雷达页显著报错，并指向设置页带 📡 标记的厂家
        st.error(f"🚫 {errors[0] if errors else '当前 API 厂家未提供联网搜索功能，雷达不可用。'}"
                 "支持搜索的厂家见设置页厂家下拉中带 📡 标记的选项。")
        _render_job_steps(job.get("steps", []))
        return
    results = summary.get("results", {})
    total = sum(results.values())
    n_vars = sum(c.get("variables", 0) for c in summary.get("cognition", {}).values())
    if job.get("status") == "done":
        st.success(f"上次自动任务完成（{job.get('finished', '—')}）："
                   f"新增信号 {total} 条，新增边际变量 {n_vars} 个")
    else:
        st.error(f"上次自动任务失败（{job.get('finished', '—')}）")
    _render_job_steps(job.get("steps", []))
    if errors:
        st.warning("部分主题失败：" + "；".join(errors))


# ==================== 公众号技术提取：后台任务 ====================

TECH_JOB_FILE = "tech_job.json"  # 技术提取/合并任务的进度与结果落盘文件

_tech_thread = None  # 后台线程（模块级变量，跨 rerun 存活）


def _tech_job_running():
    return _tech_thread is not None and _tech_thread.is_alive()


def _read_tech_job():
    return _load(TECH_JOB_FILE, None)


def _write_tech_job(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path(TECH_JOB_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    for attempt in range(5):  # Windows 上读者持有句柄时 os.replace 会被拒，短暂重试
        try:
            os.replace(tmp, _path(TECH_JOB_FILE))
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, _path(TECH_JOB_FILE))


def _clear_tech_job():
    try:
        os.remove(_path(TECH_JOB_FILE))
    except OSError:
        pass


def _run_tech_extract(articles):
    """后台线程体：逐篇提取+路由，进度实时落盘 tech_job.json。
    注意：线程内没有 ScriptRunContext，禁止调用任何 st.*。"""
    state = {"status": "running", "phase": "extract",
             "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "", "steps": [], "results": []}
    docs = tech.list_tech_docs()
    _write_tech_job(state)
    try:
        for a in articles:
            title = a.get("title") or "粘贴正文"
            state["steps"].append({"title": title, "status": "running", "detail": ""})
            _write_tech_job(state)
            try:
                ext = tech.extract_and_route(a, docs)
                state["results"].append({"article": a, "ext": ext, "error": ""})
                state["steps"][-1].update({"status": "done",
                                           "detail": (ext.get("one_liner") or "")[:80]})
            except Exception as e:
                state["results"].append({"article": a, "ext": None, "error": str(e)})
                state["steps"][-1].update({"status": "error", "detail": str(e)[:200]})
            _write_tech_job(state)
        state["status"] = "done"
    except Exception as e:
        state["status"] = "error"
        state["steps"].append({"title": "任务", "status": "error", "detail": str(e)[:200]})
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_tech_job(state)


def _run_tech_merge(good):
    """后台线程体：逐篇 AI 合并重写并落盘 09_tech。线程内禁止调用任何 st.*。"""
    state = {"status": "running", "phase": "merge",
             "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "", "steps": [],
             "merged": {"done": 0, "names": [], "errors": []}}
    _write_tech_job(state)
    try:
        for r in good:
            ext = r["ext"]
            name = tech.resolve_target_name(ext)
            state["steps"].append({"title": name[:-3], "status": "running", "detail": ""})
            _write_tech_job(state)
            path = tech.resolve_target_path(name)
            existing = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    existing = f.read()
            try:
                tech.save_tech_doc(name, tech.merge_doc(existing, ext))
                state["merged"]["done"] += 1
                state["merged"]["names"].append(("🆕" if not existing else "🔀") + name[:-3])
                state["steps"][-1]["status"] = "done"
            except Exception as e:
                state["merged"]["errors"].append(f"{name}（{e}）")
                state["steps"][-1].update({"status": "error", "detail": str(e)[:200]})
            _write_tech_job(state)
        state["status"] = "done"
    except Exception as e:
        state["status"] = "error"
        state["merged"]["errors"].append(str(e)[:200])
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_tech_job(state)


def _start_tech_job(target, args):
    global _tech_thread
    if _tech_job_running():
        return
    # 主线程取好当前会话的 API Key、模型与端点传给工作线程（线程内访问不了 session_state）
    api_key = llm.get_api_key()
    model = llm.get_model()
    base_url = llm.get_base_url()

    def _wrapped():
        llm.set_thread_api_key(api_key)
        llm.set_thread_model(model)
        llm.set_thread_base_url(base_url)
        target(*args)

    _tech_thread = threading.Thread(target=_wrapped, daemon=True)
    _tech_thread.start()


@st.fragment(run_every=3)
def _render_tech_job_live():
    """提取/合并进行中每 3 秒轮询 tech_job.json 刷新进度。
    固定挂载 + 单元素渲染（整段状态合成一段 HTML，只更新不增删节点），
    结束边沿按 started 时间戳去重、只触发一次整页 rerun。"""
    job = _read_tech_job()
    if not job:
        return
    if job.get("status") == "running" and not _tech_job_running():
        return  # 线程不在（中断/重启）：交给主流程的中断提示分支
    if job.get("status") != "running":
        token = job.get("started", "")
        if st.session_state.get("_live_fired_radar_tech") != token:
            st.session_state["_live_fired_radar_tech"] = token
            st.rerun()  # 整页 rerun，由主流程消费结果
        return
    label = "合并入库" if job.get("phase") == "merge" else "提取"
    banner = (f"⏳ 技术{label}进行中（开始于 {job.get('started', '—')}）。"
              "进度实时落盘，可自由切换页面/刷新。")
    lines = []
    for s in job.get("steps", []):
        icon = _STATUS_ICON.get(s.get("status"), "")
        detail = ""
        if s.get("detail") and s.get("status") != "running":
            detail = f"（{str(s['detail'])[:120]}）"
        lines.append(f"{icon} {s.get('title', '')} {detail}")
    st.text(f"{banner}\n\n" + "\n".join(lines))


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
    with open(_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==================== 周报生成 ====================

def _in_range(item_date, start, end):
    try:
        d = datetime.strptime(str(item_date)[:10], "%Y-%m-%d").date()
        return start <= d <= end
    except (ValueError, TypeError):
        return False


def build_weekly_report(start, end, key_questions):
    signals = [s for s in _load("radar_signals.json", []) if _in_range(s.get("date"), start, end)]
    variables = [v for v in _load("radar_variables.json", []) if _in_range(v.get("date"), start, end)]
    themes = _load("radar_themes.json", {})
    theme_updates = {t: d for t, d in themes.items()
                     if any(_in_range(h.get("date"), start, end) for h in d.get("history", []))
                     or _in_range(d.get("updated", ""), start, end)}

    lines = [
        f"# 市场认知周报（{start.isoformat()} ~ {end.isoformat()}）",
        "",
        "> Investment Radar 周度输出：市场正在关注什么、为什么开始这样理解、什么变量推动了叙事变化。",
        "> 本报告不做投资决策，供 Belief Tracker / Thesis Battle 使用。",
        "",
        "## 1. Executive Summary",
        "",
    ]
    hot = {}
    for s in signals:
        theme = s.get("theme", "其他")  # pool JSON 可手改，缺键条目归入“其他”而不是崩掉
        hot[theme] = hot.get(theme, 0) + 1
    if hot:
        lines.append("本周信号分布：" + "；".join(f"**{t}** {n} 条" for t, n in sorted(hot.items(), key=lambda x: -x[1])) + "。")
    if theme_updates:
        lines.append(f"叙事有更新的主题：{'、'.join(theme_updates)}。")
    if variables:
        lines.append(f"新增边际变量 {len(variables)} 个：{'、'.join(v.get('title', '—') for v in variables[:5])}。")
    if not (signals or variables or theme_updates):
        lines.append("本周无新录入信息。")

    lines += ["", "## 2. Theme Review（主题分析）", ""]
    if theme_updates:
        for t, d in theme_updates.items():
            cur = d.get("current", {})
            lines += [
                f"### {t}",
                "",
                f"- **当前市场叙事**：{cur.get('narrative', '—')}",
                f"- **支持证据**：{cur.get('evidence', '—')}",
                f"- **代表性观点**：{cur.get('views', '—')}",
                f"- **市场共识**：{cur.get('consensus', '—')}",
                f"- **核心分歧**：{cur.get('divergence', '—')}",
                "",
            ]
    else:
        lines += ["本周无主题叙事更新。", ""]

    lines += ["## 3. Narrative Evolution（叙事演变）", ""]
    transitions = []
    for t, d in themes.items():
        for h in d.get("history", []):
            if _in_range(h.get("date"), start, end) and h.get("is_transition"):
                transitions.append((t, h))
    if transitions:
        for t, h in transitions:
            lines += [
                f"### {t}",
                "",
                f"- **过去叙事**：{h.get('previous', '—')}",
                f"- **当前叙事**：{h.get('new', '—')}",
                f"- **关键节点**：{h.get('date', '—')}",
                f"- **触发事件**：{h.get('trigger', '—')}",
                f"- **市场含义**：{h.get('meaning', '—')}",
                "",
            ]
    else:
        lines += ["本周未记录叙事转变。", ""]

    lines += ["## 4. Marginal Variable Analysis（边际变量分析）", ""]
    if variables:
        for v in variables:
            lines += [
                f"### {v.get('title', '—')}（{v.get('var_type', '—')} / {v.get('theme', '—')}）",
                "",
                f"- **新增信息**：{v.get('new_info', '—')}",
                f"- **原有预期**：{v.get('prev_expect', '—')}",
                f"- **预期变化**：{v.get('expect_change', '—')}",
                f"- **边际变量**：{v.get('marginal_var', '—')}",
                f"- **市场影响**：{v.get('market_impact', '—')}",
                "",
            ]
    else:
        lines += ["本周未记录边际变量。", ""]

    lines += ["## 5. Key Questions to Monitor（未来关注变量）", ""]
    questions = [q.strip() for q in key_questions.split("\n") if q.strip()]
    lines += [f"- {q}" for q in questions] or ["（未填写）"]
    lines.append("")
    return "\n".join(lines)


def _write_report(report_md, end):
    filename = f"市场认知周报_{end.strftime('%Y%m%d')}.md"
    abs_path = os.path.join(KNOWLEDGE_DIR, REPORT_CATEGORY, filename)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)  # 知识库没有 05_tracking/ 时自动建
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    return abs_path


# ==================== 页面（demo §11：radar-nav 子导航 + 六个子页） ====================

_RADAR_TABS = [("overview", "总览"), ("signals", "信号"), ("themes", "主题"),
               ("variables", "变量"), ("reports", "报告"), ("sources", "来源")]


def _sig_row_html(s):
    """demo .signal-row：日期 / 主题 / 类型 / 内容 / Why it matters（行式，不是卡片）。"""
    d = str(s.get("date", "—"))[5:10]
    what = html.escape(str(s.get("title") or "（无标题）"))
    if s.get("summary"):
        what += (f" <span style='color:var(--text-tertiary)'>"
                 f"{html.escape(str(s['summary'])[:90])}</span>")
    why = html.escape(str(s.get("why") or s.get("importance_reason") or "")[:120])
    return (f"<div class='signal-row'><span class='d'>{d}</span>"
            f"<span class='theme'>{html.escape(str(s.get('theme', '—')))}</span>"
            f"<span class='type'>{html.escape(_EVENT_TYPE_CN.get(str(s.get('event_type')), str(s.get('event_type', '—'))))}</span>"
            f"<span class='what'>{what}</span>"
            f"<span class='why'>{why}</span></div>")


def _evidence_html(text):
    """主题字段（可能多行）→ theme-sec 内容：多行渲染为 <ul>，单行 <p>。"""
    text = str(text or "").strip()
    if not text:
        return ""
    lines = [l.strip().lstrip("-·• ") for l in text.split("\n") if l.strip()]
    if len(lines) > 1:
        return "<ul>" + "".join(f"<li>{html.escape(l)}</li>" for l in lines) + "</ul>"
    return f"<p>{html.escape(text)}</p>"


# ---------- Overview ----------

def _render_overview():
    # 抓取操作（quiet 按钮）+ 上次抓取时间
    running = _job_running()
    c_f, c_p, c_t = st.columns([1.6, 1.6, 6])
    with c_f:
        btn_full = st.button("全量抓取", disabled=running,
                             help="抓取 watchlist 全部启用主题，并更新认知库（后台执行）")
    with c_p:
        btn_primary = st.button("重点主题短跑", disabled=running,
                                help="只跑 primary 主题（周中使用，后台执行）")
    with c_t:
        last = radar_auto.last_run_time()
        st.markdown(f"<div class='caption' style='margin-top:0.6rem'>上次自动抓取：{last or '从未'}</div>",
                    unsafe_allow_html=True)
    if (btn_full or btn_primary) and not running:
        _start_job(primary_only=btn_primary)
        st.rerun()

    job = _read_job()
    _render_job_live()  # 固定挂载，内部按状态决定是否渲染
    if running:
        pass
    elif job and job.get("status") == "running":
        # 任务文件仍是 running 但线程已不在：应用曾被重启/进程被杀，任务中断
        st.warning("上次自动任务被中断（应用重启或进程退出），结果可能不完整，可重新发起。")
        _render_job_steps(job.get("steps", []))
    elif job and job.get("status") in ("done", "error"):
        _render_job_result(job)

    signals = _load("radar_signals.json", [])
    variables = _load("radar_variables.json", [])
    themes = _load("radar_themes.json", {})
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    # ---- TODAY：三个 mono 大数字（近 7 天） ----
    n_sig = sum(1 for s in signals if str(s.get("date", ""))[:10] >= week_ago)
    n_theme = sum(1 for d in themes.values()
                  if str(d.get("updated", ""))[:10] >= week_ago
                  or any(str(h.get("date", ""))[:10] >= week_ago
                         for h in d.get("history", [])))
    n_var = sum(1 for v in variables if str(v.get("date", ""))[:10] >= week_ago)
    st.markdown("<div class='section-label' style='margin-top:1.6rem'>今日 · 近 7 天</div>"
                "<div class='today-strip'>"
                f"<div class='today-num'><div class='n'>{n_sig:02d}</div>"
                "<div class='l'>新增信号</div></div>"
                f"<div class='today-num'><div class='n'>{n_theme:02d}</div>"
                "<div class='l'>主题变化</div></div>"
                f"<div class='today-num'><div class='n'>{n_var:02d}</div>"
                "<div class='l'>变量更新</div></div>"
                "</div>", unsafe_allow_html=True)

    # ---- SIGNAL STREAM：最新 8 条行式列表 ----
    st.markdown("<div class='section-label'>信号流</div>", unsafe_allow_html=True)
    if signals:
        st.markdown("".join(_sig_row_html(s) for s in signals[-8:][::-1]),
                    unsafe_allow_html=True)
    else:
        st.markdown("<div class='meta-line' style='padding:0.6rem 0.2rem'>"
                    "信号池为空，点上方「全量抓取」开始首次自动抓取。</div>",
                    unsafe_allow_html=True)

    # ---- NARRATIVE CHANGES：近 30 天的叙事历史条目 ----
    month_ago = (date.today() - timedelta(days=30)).isoformat()
    changes = []
    for t, d in themes.items():
        for h in d.get("history", []):
            if str(h.get("date", ""))[:10] >= month_ago:
                changes.append((str(h.get("date", "")), t, h))
    changes.sort(key=lambda x: x[0], reverse=True)
    if changes:
        blocks = []
        for _d, t, h in changes[:4]:
            text = f"「{h.get('previous', '—')}」→「{h.get('new', '—')}」"
            if h.get("trigger"):
                text += f"。触发：{h['trigger']}"
            # demo：转变用 accent 左边线，微调用 warning 色
            warn = " style='border-left-color:var(--warning)'" \
                if not h.get("is_transition") else ""
            blocks.append(f"<div class='narrative-block'{warn}>"
                          f"<div class='nt'>{html.escape(str(t))}</div>"
                          f"<p>{html.escape(text[:220])}</p></div>")
        st.markdown("<div class='home-section'><div class='section-label'>"
                    "叙事变化</div>" + "".join(blocks) + "</div>",
                    unsafe_allow_html=True)

    # ---- MARGINAL VARIABLES：最新 8 个 chips ----
    if variables:
        chips = "".join(
            f"<span class='var-chip'>"
            f"{html.escape(str(v.get('marginal_var') or v.get('title', '—'))[:30])}</span>"
            for v in variables[-8:][::-1])
        st.markdown("<div class='home-section'><div class='section-label'>"
                    f"边际变量</div><div class='var-chips'>{chips}</div></div>",
                    unsafe_allow_html=True)


# ---------- Signals：信号池完整列表（筛选 + 删除） ----------

def _render_signals():
    st.markdown("<div class='section-label'>全部信号 · 信号池（🤖 自动抓取 / 📱 公众号）</div>",
                unsafe_allow_html=True)
    signals = _load("radar_signals.json", [])
    if not signals:
        st.markdown("<div class='empty-state'><div class='e-title'>信号池为空</div>"
                    "<div class='e-sub'>到「总览」页跑一次「全量抓取」，AI 会自动填充这里。"
                    "</div></div>", unsafe_allow_html=True)
        return
    filter_theme = st.selectbox("按主题筛选", ["全部"] + THEMES, key="sig_filter")
    shown = 0
    for real_idx in range(len(signals) - 1, -1, -1):
        s = signals[real_idx]
        if filter_theme != "全部" and s.get("theme") != filter_theme:
            continue
        if shown >= 50:
            break
        shown += 1
        col_a, col_b = st.columns([23, 1])
        with col_a:
            st.markdown(_sig_row_html(s), unsafe_allow_html=True)
        with col_b:
            if st.button("×", key=f"del_sig_{real_idx}", help="删除该条", type="tertiary"):
                signals.pop(real_idx)
                _save("radar_signals.json", signals)
                st.rerun()


# ---------- Themes：叙事长文（demo .theme-article） ----------

def _render_themes():
    themes = _load("radar_themes.json", {})
    c_sel, c_btn = st.columns([4, 1])
    with c_sel:
        theme = st.selectbox("选择主题", THEMES, key="radar_theme",
                             label_visibility="collapsed")
    with c_btn:
        refresh = st.button("更新认知", key="radar_refresh_cog",
                            help="基于该主题近 7 天信号重新生成叙事与边际变量")
    if refresh:
        if not llm.get_api_key():
            st.warning("未填入 API Key，无法更新认知："
                       "请先在左侧边栏「API 设置」处填入你自己的 key（sk-...）。")
        else:
            with st.spinner(f"正在更新「{theme}」认知…"):
                res = radar_auto.update_cognition(theme)
            if res.get("updated") or res.get("variables"):
                st.toast(f"已更新：叙事 {'✓' if res.get('updated') else '不变'}，"
                         f"新增边际变量 {res.get('variables', 0)} 个")
                st.rerun()
            else:
                st.info(f"「{theme}」近 7 天信号不足，暂无更新（先跑一次自动抓取）。")

    entry = themes.get(theme, {"current": {}, "history": []})
    cur = entry.get("current", {})
    if not cur.get("narrative"):
        st.info("该主题还没有认知记录——跑一次自动抓取，或点上方「更新认知」基于现有信号生成。")
        return

    history = entry.get("history", [])
    parts = ["<div class='theme-article'>",
             "<div class='section-label'>主题</div>",
             f"<h2 class='tt'>{html.escape(str(theme))}</h2>",
             f"<div class='t-sub'>最近叙事更新 "
             f"{html.escape(str(entry.get('updated', '—')))} · "
             f"历史 {len(history):02d} 条</div>"]

    def sec(label, inner):
        if inner:
            parts.append(f"<div class='theme-sec'><div class='section-label'>{label}</div>"
                         f"{inner}</div>")

    sec("当前叙事",
        f"<p>{html.escape(str(cur['narrative']))}</p>" if cur.get("narrative") else "")
    sec("支撑证据", _evidence_html(cur.get("evidence")))
    sec("代表性观点", _evidence_html(cur.get("views")))
    sec("市场共识", _evidence_html(cur.get("consensus")))
    sec("核心分歧", _evidence_html(cur.get("divergence")))

    theme_vars = [v for v in _load("radar_variables.json", []) if v.get("theme") == theme]
    if theme_vars:
        chips = "".join(
            f"<span class='var-chip'>"
            f"{html.escape(str(v.get('marginal_var') or v.get('title', '—'))[:30])}</span>"
            for v in theme_vars[-6:][::-1])
        parts.append("<div class='theme-sec'><div class='section-label'>边际变量</div>"
                     f"<div class='var-chips'>{chips}</div></div>")

    if history:
        rows = []
        for h in reversed(history[-8:]):
            hd = str(h.get("date", "—"))[:7].replace("-", ".")
            hc = f"「{h.get('previous', '—')}」→「{h.get('new', '—')}」"
            if h.get("trigger"):
                hc += f"。触发：{h['trigger']}"
            rows.append(f"<div class='history-row'><span class='hd'>{hd}</span>"
                        f"<span class='hc'>{html.escape(hc[:180])}</span></div>")
        parts.append("<div class='theme-sec'><div class='section-label'>叙事历史</div>"
                     + "".join(rows) + "</div>")
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


# ---------- Variables：边际变量完整列表 ----------

def _render_variables():
    st.markdown("<div class='section-label'>跟踪变量 · AI 从信号中自动提炼</div>",
                unsafe_allow_html=True)
    variables = _load("radar_variables.json", [])
    if not variables:
        st.markdown("<div class='empty-state'><div class='e-title'>尚无边际变量</div>"
                    "<div class='e-sub'>自动抓取后由 AI 从信号中提炼，"
                    "或到 Themes 手动触发「更新认知」。</div></div>",
                    unsafe_allow_html=True)
        return
    rows = []
    for v in variables[::-1]:
        date_s = str(v.get("date", ""))[:10]
        mark = "🤖 " if v.get("auto") else ""
        l2 = f"{_VAR_TYPE_CN.get(str(v.get('var_type')), v.get('var_type', '—'))} · {v.get('theme', '—')} · {v.get('marginal_var', '—')}"
        l3 = (f"新增：{v.get('new_info', '—')} ｜ 预期：{v.get('prev_expect', '—')} → "
              f"{v.get('expect_change', '—')} ｜ 影响：{v.get('market_impact', '—')}")
        rows.append(
            f"<div class='lib-row'><span class='l1'>"
            f"<span class='title'>{mark}{html.escape(str(v.get('title') or '（无标题）'))}</span>"
            f"<span class='date'>{date_s}</span></span>"
            f"<span class='l2' style='font-family:var(--font-mono)'>{html.escape(l2)}</span>"
            f"<span class='l3'>{html.escape(l3[:160])}</span></div>")
    st.markdown("".join(rows), unsafe_allow_html=True)


# ---------- Reports：历史周报 + 生成新周报 ----------

def _render_reports(index, on_saved):
    st.markdown("<div class='section-label'>雷达周报 · 每周市场情报</div>",
                unsafe_allow_html=True)
    reports = [d for d in index.get("documents", [])
               if d.get("category_key") == "05_tracking" and "周报" in d.get("name", "")]
    if reports:
        rows = []
        for d in sorted(reports, key=lambda x: str(x.get("modified", "")),
                        reverse=True)[:10]:
            title = d.get("title") or d["name"].replace(".md", "")
            rows.append(
                f"<a class='doc-row' href='?doc={_urlquote(d['path'])}' target='_self'>"
                f"<span class='title'>{html.escape(title)}</span>"
                f"<span class='meta'>{html.escape(str(d.get('track') or ''))}</span>"
                f"<span class='date'>{str(d.get('modified', ''))[:10]}</span></a>")
        st.markdown("".join(rows), unsafe_allow_html=True)

    st.markdown("<div class='home-section'><div class='section-label'>生成新周报</div></div>",
                unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        start = st.date_input("起始日期", value=date.today() - timedelta(days=7), key="rep_start")
    with c2:
        end = st.date_input("截止日期", value=date.today(), key="rep_end")
    key_questions = st.text_area(
        "Key Questions to Monitor（未来关注变量，每行一条）", height=90,
        placeholder="云厂商下一代集群架构是否明确采用CPO\n模型层价格战是否传导至应用层定价")
    if st.button("生成周报", type="primary"):
        st.session_state["radar_report"] = build_weekly_report(start, end, key_questions)

    report = st.session_state.get("radar_report")
    if report:
        with st.expander("📋 预览周报", expanded=True):
            st.markdown(report)
        if st.button("💾 写入知识库（05_tracking）"):
            try:
                path = _write_report(report, end)
            except OSError as e:
                st.error(f"写入失败：{e}。请检查知识库目录存在且可写。")
            else:
                on_saved()
                st.success(f"已写入：{os.path.basename(path)}")
                st.rerun()


# ---------- Sources：公众号文章 → 技术提取 ----------

def _render_sources(on_saved):
    st.markdown("<div class='section-label'>来源 · 公众号文章 → 技术提取（沉淀到 09_tech 技术档案）</div>",
                unsafe_allow_html=True)
    urls_text = st.text_area(
        "文章链接（每行一个，mp.weixin.qq.com；被反爬拦截时改用下方粘贴正文）",
        height=100, key="wc_urls", placeholder="https://mp.weixin.qq.com/s/...")
    with st.expander("或直接粘贴正文（单篇）"):
        raw_title = st.text_input("标题", key="wc_raw_title")
        raw_account = st.text_input("公众号名", key="wc_raw_account")
        raw_content = st.text_area("正文", height=150, key="wc_raw_content")
    tech_running = _tech_job_running()
    if st.button("🧪 技术提取", type="primary", key="wc_analyze", disabled=tech_running):
        articles = []
        urls = [u.strip() for u in urls_text.split("\n") if u.strip()]
        if urls:
            with st.spinner(f"抓取 {len(urls)} 篇文章…"):
                for u in urls:
                    articles.append(radar_wechat.fetch_article(u))
        if raw_content.strip():
            articles.append({"url": "", "title": raw_title.strip(),
                             "account": raw_account.strip(), "date": "",
                             "text": raw_content.strip()})
        ok = [a for a in articles if a.get("text")]
        for a in articles:
            if not a.get("text"):
                st.warning(f"抓取失败：{a.get('url', '粘贴正文')}（{a.get('error', '无正文')}）")
        if ok:
            _start_tech_job(_run_tech_extract, (ok,))  # 后台提取，进度落盘 tech_job.json
            st.rerun()

    tjob = _read_tech_job()
    _render_tech_job_live()  # 固定挂载，内部按状态决定是否渲染
    if tech_running:
        pass
    elif tjob and tjob.get("status") == "running":
        # 任务文件仍是 running 但线程已不在：应用曾被重启/进程被杀，任务中断
        st.warning("上次技术任务被中断（应用重启或进程退出），结果可能不完整，可重新发起。")
    elif tjob and tjob.get("status") in ("done", "error"):
        if tjob.get("phase") == "merge":
            st.session_state["wc_tech_merged"] = tjob.get("merged") or {}
        else:
            st.session_state["wc_tech"] = tjob.get("results", [])
        _clear_tech_job()
        st.rerun()

    # 合并完成通知（消费一次即弃）
    merged = st.session_state.get("wc_tech_merged")
    if merged is not None:
        del st.session_state["wc_tech_merged"]
        if merged.get("done"):
            st.success(f"已入库 {merged['done']} 篇：{'、'.join(merged['names'])}，见「技术沉淀」分类")
            if "wc_tech" in st.session_state:
                del st.session_state["wc_tech"]
            on_saved()
        for err in merged.get("errors", []):
            st.warning(f"合并失败：{err}")

    results = st.session_state.get("wc_tech") if not tech_running else None
    if results is not None:
        good = [r for r in results if r["ext"]]
        for r in results:
            if not r["ext"]:
                st.warning(f"提取失败：{r['article'].get('title') or '粘贴正文'}（{r['error']}）")
        if not good:
            st.info("没有可入库的技术内容。")
        else:
            st.markdown(f"<div class='caption'>提取 {len(good)} 篇，确认后合并进技术档案"
                        "（同主题往上填，没有对应文档则新建）</div>", unsafe_allow_html=True)
            for r in good:
                a, ext = r["article"], r["ext"]
                name = tech.resolve_target_name(ext)
                is_new = not os.path.exists(tech.resolve_target_path(name))
                tag = "🆕 新建" if is_new else "🔀 合并"
                with st.expander(f"{tag}《{a.get('title') or '粘贴正文'}》→ {name[:-3]}"):
                    if ext.get("one_liner"):
                        st.markdown(f"**定位**：{ext['one_liner']}")
                    if ext.get("principle"):
                        st.markdown(f"**原理**：{ext['principle'][:150]}"
                                    + ("…" if len(ext["principle"]) > 150 else ""))
                    for p in ext.get("key_points", []):
                        st.markdown(f"- {p}")
            if st.button("💾 合并入库（09_tech）", key="wc_save"):
                _start_tech_job(_run_tech_merge, (good,))  # 后台合并，进度落盘 tech_job.json
                st.rerun()


# ---------- 主入口 ----------

def render_radar(index, on_saved):
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)
    st.markdown("<div class='section-label'>投资雷达</div>"
                "<div class='page-title' style='font-size:24px'>市场信号与认知跟踪</div>",
                unsafe_allow_html=True)

    # 子导航：st.button 文字 tab，session_state 切换 + websocket rerun，不再整页刷新
    cur = st.session_state.get("radar_tab", "overview")
    if cur not in dict(_RADAR_TABS):
        cur = "overview"
    st.markdown("<div class='radar-nav-marker'></div>", unsafe_allow_html=True)
    tab_cols = st.columns([1.3, 1.05, 1.2, 1.3, 1.1, 1.15, 12])
    for col, (k, lbl) in zip(tab_cols, _RADAR_TABS):
        with col:
            if st.button(lbl, key=f"radar_tab_{k}",
                         type="primary" if k == cur else "secondary"):
                st.session_state.radar_tab = k
                st.rerun()

    if cur == "overview":
        _render_overview()
    elif cur == "signals":
        _render_signals()
    elif cur == "themes":
        _render_themes()
    elif cur == "variables":
        _render_variables()
    elif cur == "reports":
        _render_reports(index, on_saved)
    elif cur == "sources":
        _render_sources(on_saved)
