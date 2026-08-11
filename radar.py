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


def _run_job(primary_only, api_key):
    """后台线程体：执行 radar_auto.run，进度实时落盘 radar_job.json。
    注意：线程内没有 ScriptRunContext，禁止调用任何 st.*；
    前端注入的 API Key 同样取不到，由 api_key 参数显式带入。"""
    llm.set_thread_api_key(api_key)
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
    except Exception as e:
        state["status"] = "error"
        state["summary"] = {"results": {}, "cognition": {}, "errors": [str(e)]}
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_job(state)


def _start_job(primary_only):
    global _job_thread
    if _job_running():
        return
    # 主线程取好当前会话的 API Key 传给工作线程（线程内访问不了 session_state）
    _job_thread = threading.Thread(target=_run_job,
                                   args=(primary_only, llm.get_api_key()), daemon=True)
    _job_thread.start()


_STAGE_LABEL = {"fetch": "抓取", "cognition": "认知"}
_STATUS_ICON = {"running": "⏳", "done": "✅", "error": "❌"}


def _render_job_steps(steps):
    if not steps:
        st.markdown("<div class='caption'>正在启动任务…</div>", unsafe_allow_html=True)
        return
    lines = []
    for s in steps:
        icon = _STATUS_ICON.get(s.get("status"), "")
        stage = _STAGE_LABEL.get(s.get("stage"), s.get("stage", ""))
        theme = html.escape(str(s.get("theme", "")))
        detail = ""
        if s.get("detail") and s.get("status") != "running":
            detail = f"（{html.escape(str(s['detail'])[:200])}）"
        lines.append(f"{icon} {stage} · {theme} {detail}")
    st.markdown("<div class='caption'>" + "<br>".join(lines) + "</div>",
                unsafe_allow_html=True)


@st.fragment(run_every=3)
def _render_job_live():
    """任务运行中每 3 秒轮询 radar_job.json 刷新进度；检测到结束后触发整页 rerun。"""
    job = _read_job() or {}
    st.info(f"⏳ 自动任务进行中（{job.get('mode', '')}，开始于 {job.get('started', '—')}）。"
            "进度实时落盘，可自由切换页面/刷新。")
    _render_job_steps(job.get("steps", []))
    if job.get("status") != "running":
        st.rerun()  # 整页 rerun，由主流程展示最终结果


def _render_job_result(job):
    summary = job.get("summary") or {}
    results = summary.get("results", {})
    total = sum(results.values())
    n_vars = sum(c.get("variables", 0) for c in summary.get("cognition", {}).values())
    if job.get("status") == "done":
        st.success(f"上次自动任务完成（{job.get('finished', '—')}）："
                   f"新增信号 {total} 条，新增边际变量 {n_vars} 个")
    else:
        st.error(f"上次自动任务失败（{job.get('finished', '—')}）")
    _render_job_steps(job.get("steps", []))
    errors = summary.get("errors") or []
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
            path = os.path.join(tech.tech_dir(), name)
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
    # 主线程取好当前会话的 API Key 传给工作线程（线程内访问不了 session_state）
    api_key = llm.get_api_key()

    def _wrapped():
        llm.set_thread_api_key(api_key)
        target(*args)

    _tech_thread = threading.Thread(target=_wrapped, daemon=True)
    _tech_thread.start()


@st.fragment(run_every=3)
def _render_tech_job_live():
    """提取/合并进行中每 3 秒轮询 tech_job.json 刷新进度；检测到结束后触发整页 rerun。"""
    job = _read_tech_job() or {}
    label = "合并入库" if job.get("phase") == "merge" else "提取"
    st.info(f"⏳ 技术{label}进行中（开始于 {job.get('started', '—')}）。"
            "进度实时落盘，可自由切换页面/刷新。")
    lines = []
    for s in job.get("steps", []):
        icon = _STATUS_ICON.get(s.get("status"), "")
        detail = ""
        if s.get("detail") and s.get("status") != "running":
            detail = f"（{html.escape(str(s['detail'])[:120])}）"
        lines.append(f"{icon} {html.escape(str(s.get('title', '')))} {detail}")
    if lines:
        st.markdown("<div class='caption'>" + "<br>".join(lines) + "</div>",
                    unsafe_allow_html=True)
    if job.get("status") != "running":
        st.rerun()  # 整页 rerun，由主流程消费结果


# ==================== 存储 ====================

def _path(name):
    return os.path.join(DATA_DIR, name)


def _load(name, default):
    if os.path.exists(_path(name)):
        with open(_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
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
        hot[s["theme"]] = hot.get(s["theme"], 0) + 1
    if hot:
        lines.append("本周信号分布：" + "；".join(f"**{t}** {n} 条" for t, n in sorted(hot.items(), key=lambda x: -x[1])) + "。")
    if theme_updates:
        lines.append(f"叙事有更新的主题：{'、'.join(theme_updates)}。")
    if variables:
        lines.append(f"新增边际变量 {len(variables)} 个：{'、'.join(v['title'] for v in variables[:5])}。")
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
                f"### {v['title']}（{v.get('var_type', '—')} / {v.get('theme', '—')}）",
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
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    return abs_path


# ==================== 页面 ====================

def render_radar(index, on_saved):
    st.markdown("<div class='doc-title'>📡 Investment Radar</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='meta-line'>市场认知追踪层：信号由 AI 自动抓取，叙事与边际变量自动维护。"
        "区分 事实 → 市场解释 → 认知变化；产出供 Thesis Battle 使用。</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<div style='margin-bottom:0.8rem'></div>", unsafe_allow_html=True)

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📥 信息采集", "🧭 主题叙事", "⚡ 边际变量",
                                            "📰 周报生成", "📱 公众号"])

    # ---------- Tab 1: 信息采集（信号看板） ----------
    with tab1:
        running = _job_running()
        c_f, c_p, c_t = st.columns([2, 2, 4])
        with c_f:
            btn_full = st.button("🔄 立即抓取（全量）", disabled=running,
                                 help="抓取 watchlist 全部启用主题，并更新认知库（后台执行）")
        with c_p:
            btn_primary = st.button("⚡ 重点主题短跑", disabled=running,
                                    help="只跑 primary 主题（周中使用，后台执行）")
        with c_t:
            last = radar_auto.last_run_time()
            st.markdown(f"<div class='caption' style='margin-top:0.6rem'>上次自动抓取：{last or '从未'}</div>",
                        unsafe_allow_html=True)
        if (btn_full or btn_primary) and not running:
            _start_job(primary_only=btn_primary)
            st.rerun()

        job = _read_job()
        if running:
            _render_job_live()
        elif job and job.get("status") == "running":
            # 任务文件仍是 running 但线程已不在：应用曾被重启/进程被杀，任务中断
            st.warning("上次自动任务被中断（应用重启或进程退出），结果可能不完整，可重新发起。")
            _render_job_steps(job.get("steps", []))
        elif job and job.get("status") in ("done", "error"):
            _render_job_result(job)

        signals = _load("radar_signals.json", [])
        if not signals:
            st.info("信号池为空，点击上方按钮开始首次自动抓取。")
        else:
            week_ago = (date.today() - timedelta(days=7)).isoformat()
            n_week = sum(1 for s in signals if str(s.get("date", ""))[:10] >= week_ago)
            m1, m2, m3 = st.columns(3)
            m1.metric("信号总数", len(signals))
            m2.metric("近 7 天新增", n_week)
            m3.metric("覆盖主题", len({s.get("theme") for s in signals}))
            dist = {}
            for s in signals:
                dist[s.get("theme", "其他")] = dist.get(s.get("theme", "其他"), 0) + 1
            st.markdown(
                "<div class='caption'>主题分布：" +
                " · ".join(f"{t} {n}" for t, n in sorted(dist.items(), key=lambda x: -x[1])) +
                "</div>", unsafe_allow_html=True)

            st.markdown("<div class='section-header'>信号池（🤖 = 自动抓取）</div>", unsafe_allow_html=True)
            filter_theme = st.selectbox("按主题筛选", ["全部"] + THEMES, key="sig_filter")
            shown = 0
            for real_idx in range(len(signals) - 1, -1, -1):
                s = signals[real_idx]
                if filter_theme != "全部" and s.get("theme") != filter_theme:
                    continue
                if shown >= 30:
                    break
                shown += 1
                col_a, col_b = st.columns([12, 1])
                with col_a:
                    auto_mark = "🤖 " if s.get("auto") else ("📱 " if s.get("origin") == "wechat" else "")
                    track = f" · {s['sub_track']}" if s.get("sub_track") else ""
                    imp = f" · ⚑{s['importance']}" if s.get("importance") else ""
                    html = (
                        f"<div class='meta-line'>[{s['date']}] {s['source_type']} · {s['theme']} · {s['event_type']}{track}{imp}</div>"
                        f"<div style='font-weight:600'>{auto_mark}{s['title']}</div>")
                    if s.get("summary"):
                        html += f"<div class='caption'>{s['summary'][:150]}</div>"
                    if s.get("why"):
                        html += f"<div class='caption'>💡 {s['why'][:120]}</div>"
                    if s.get("importance_reason"):
                        html += f"<div class='caption'>⚑ {s['importance_reason'][:120]}</div>"
                    if s.get("companies"):
                        html += f"<div class='caption'>🏢 {s['companies']}</div>"
                    if s.get("targets"):
                        html += f"<div class='caption'>🎯 {s['targets']}</div>"
                    st.markdown(html, unsafe_allow_html=True)
                with col_b:
                    if st.button("🗑️", key=f"del_sig_{real_idx}", help="删除该条"):
                        signals.pop(real_idx)
                        _save("radar_signals.json", signals)
                        st.rerun()

    # ---------- Tab 2: 主题叙事（行业认知库） ----------
    with tab2:
        themes = _load("radar_themes.json", {})
        c_sel, c_btn = st.columns([4, 1])
        with c_sel:
            theme = st.selectbox("选择主题", THEMES, key="radar_theme")
        with c_btn:
            st.markdown("<div style='margin-top:1.6rem'></div>", unsafe_allow_html=True)
            refresh = st.button("🔄 更新认知", help="基于该主题近 7 天信号重新生成叙事与边际变量")
        if refresh:
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
        if cur.get("narrative"):
            st.markdown(
                f"<div class='card'><div class='meta-line'>当前叙事（更新于 {entry.get('updated', '—')}）</div>"
                f"<div style='font-weight:600; margin-top:0.2rem'>{cur['narrative']}</div></div>",
                unsafe_allow_html=True,
            )
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(f"<div class='card'><div class='meta-line'>支持证据</div>"
                            f"<div>{cur.get('evidence', '—')}</div></div>", unsafe_allow_html=True)
                st.markdown(f"<div class='card'><div class='meta-line'>市场共识</div>"
                            f"<div>{cur.get('consensus', '—')}</div></div>", unsafe_allow_html=True)
            with c2:
                st.markdown(f"<div class='card'><div class='meta-line'>代表性观点</div>"
                            f"<div>{cur.get('views', '—')}</div></div>", unsafe_allow_html=True)
                st.markdown(f"<div class='card'><div class='meta-line'>核心分歧</div>"
                            f"<div>{cur.get('divergence', '—')}</div></div>", unsafe_allow_html=True)
        else:
            st.info("该主题还没有认知记录——跑一次自动抓取，或点上方「更新认知」基于现有信号生成。")

        history = entry.get("history", [])
        if history:
            st.markdown("<div class='section-header'>叙事演变历史</div>", unsafe_allow_html=True)
            for h in reversed(history[-8:]):
                flag = "🔀 转变" if h.get("is_transition") else "更新"
                with st.expander(f"[{h.get('date', '—')}] {flag}"):
                    st.markdown(f"**过去**：{h.get('previous', '—')}")
                    st.markdown(f"**之后**：{h.get('new', '—')}")
                    if h.get("trigger"):
                        st.markdown(f"**触发**：{h['trigger']}")
                    if h.get("meaning"):
                        st.markdown(f"**含义**：{h['meaning']}")

    # ---------- Tab 3: 边际变量（自动提炼） ----------
    with tab3:
        st.markdown("<div class='section-header'>边际变量库（AI 从信号中自动提炼，🤖 标记）</div>",
                    unsafe_allow_html=True)
        variables = _load("radar_variables.json", [])
        if not variables:
            st.info("尚无边际变量——自动抓取后由 AI 从信号中提炼，或到「主题叙事」手动触发更新。")
        for v in variables[-15:][::-1]:
            auto_mark = "🤖 " if v.get("auto") else ""
            with st.expander(f"[{v['date']}] {auto_mark}{v['title']}（{v.get('var_type', '—')} / {v.get('theme', '—')}）"):
                st.markdown(f"**边际变量**：{v.get('marginal_var', '—')}")
                st.markdown(f"**新增信息**：{v.get('new_info', '—')}")
                st.markdown(f"**预期变化**：{v.get('prev_expect', '—')} → {v.get('expect_change', '—')}")
                st.markdown(f"**市场影响**：{v.get('market_impact', '—')}")
                if v.get("targets"):
                    st.markdown(f"**🎯 关联标的（AI 推导，需人工核实）**：{v['targets']}")

    # ---------- Tab 4: 周报生成 ----------
    with tab4:
        st.markdown("<div class='section-header'>生成 Weekly Market Intelligence Report</div>",
                    unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            start = st.date_input("起始日期", value=date.today() - timedelta(days=7), key="rep_start")
        with c2:
            end = st.date_input("截止日期", value=date.today(), key="rep_end")
        key_questions = st.text_area(
            "Key Questions to Monitor（未来关注变量，每行一条）", height=90,
            placeholder="云厂商下一代集群架构是否明确采用CPO\n模型层价格战是否传导至应用层定价")
        if st.button("📝 生成周报", type="primary"):
            st.session_state["radar_report"] = build_weekly_report(start, end, key_questions)

        report = st.session_state.get("radar_report")
        if report:
            with st.expander("📋 预览周报", expanded=True):
                st.markdown(report)
            if st.button("💾 写入知识库（05_tracking）"):
                path = _write_report(report, end)
                on_saved()
                st.success(f"已写入：{os.path.basename(path)}")
                st.rerun()

    # ---------- Tab 5: 公众号文章 → 技术提取 ----------
    with tab5:
        st.markdown("<div class='section-header'>公众号文章 → 技术提取（沉淀到 09_tech 技术档案）</div>",
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
        if tech_running:
            _render_tech_job_live()
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
                    is_new = not os.path.exists(os.path.join(tech.tech_dir(), name))
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
