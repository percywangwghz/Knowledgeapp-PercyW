# -*- coding: utf-8 -*-
"""
新项目评审 —— 行业总文档驱动的评审工作流

以 01_industry 行业总文档为认知底座评审 02_deals 新项目：
- AI 后台线程生成评审草稿（job 文件 + fragment 轮询，切换页面不丢），
  内容为 项目判断 / 坐标落位 / 关键变量更新 / 反哺判断 / 增量思考；
- 确认后同时写回两个文档：项目文档追加/替换「## 🧭 行业总文档评审」节；
  行业总文档更新 项目落位表 / 关键变量演进表 / N.3 新项目评审留档 / 头部更新日期。
"""
import html
import json
import os
import re
import threading
import time
from datetime import datetime

import streamlit as st

from config import KNOWLEDGE_DIR
from llm import (chat, get_api_key, get_base_url, get_model,
                 set_thread_api_key, set_thread_base_url, set_thread_model)

TRUNCATE = 8000

SYSTEM_TEMPLATE = """你是一级市场资深投资合伙人，正在用「行业总文档」作为认知底座评审一个新项目。

【你的任务】
以行业总文档中的行业认知、行业坐标与关键变量为基准，审视新项目文档，输出结构化评审结果：
1. 项目判断：行业认知 + 行业变化 + 已有行业变量，能否决定这个项目的成功？哪些变量是决定性的？
2. 新项目在行业坐标中的落位（两个维度取值 + 一句话落位理由）；
3. 该项目带来的关键变量更新（旧判断 → 新判断，触发来源）；
4. 行业阶段判断是否因此变化；
5. 反哺判断：该项目是否会反哺现有行业认知或已投项目、促成什么原来做不了的事；
6. 建议写入行业总文档的增量思考。

【铁律】
- 判断必须落在行业总文档已有的变量与坐标体系上，不泛泛而谈；
- 没有依据更新的字段就明确写「维持不变」，不要硬编；
- 全程使用中文。

【行业总文档】
{industry_doc}

【新项目文档】
{project_doc}
"""

GENERATE_PROMPT = """基于以上两份文档完成新项目评审。只输出一个 JSON 对象，不要任何其他文字：
{
  "project_judgment": "基于行业总文档的项目判断（markdown 段落）：行业认知+行业变化+已有行业变量能否决定这个项目的成功？哪些变量是决定性的？",
  "coordinate_position": {"dim_a": "维度A 取值", "dim_b": "维度B 取值", "reason": "一句话落位理由"},
  "variable_updates": [{"variable": "关键变量", "old": "旧判断", "new": "新判断", "source": "触发来源"}],
  "industry_stage_update": "行业阶段判断是否因此变化，一句话",
  "feedback_thesis": "反哺判断：该项目是否会反哺现有行业认知或已投项目、促成什么原来做不了的事",
  "industry_doc_additions": "建议写入行业总文档的增量思考（markdown 段落）"
}
若行业总文档的坐标轴仍是（待填），基于文档内容给出你认为最合理的两个维度取值；variable_updates 无更新时返回空数组。"""


# ==================== 小工具 ====================

def _cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _doc_title(d):
    return d.get("title") or d["name"].replace(".md", "")


def _filter_options(options, kw):
    """按关键词过滤 {label: doc} 选项；匹配标签、文件名、一句话定位，大小写不敏感。"""
    k = (kw or "").strip().casefold()
    if not k:
        return options
    return {label: d for label, d in options.items()
            if k in label.casefold()
            or k in d.get("name", "").casefold()
            or k in (d.get("subtitle") or "").casefold()}


def _trunc(content):
    content = content or "（文档为空）"
    if len(content) > TRUNCATE:
        content = content[:TRUNCATE] + "\n……（文档过长，已截断）"
    return content


# ==================== AI 调用 ====================

def _system_prompt(project_doc, industry_doc):
    return SYSTEM_TEMPLATE.format(industry_doc=_trunc(industry_doc.get("content", "")),
                                  project_doc=_trunc(project_doc.get("content", "")))


def _parse_json(raw):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    text = m.group(1) if m else raw[raw.find("{"):raw.rfind("}") + 1]
    if not text:
        raise ValueError("AI 未返回可解析的 JSON")
    return json.loads(text)


# ==================== 后台评审任务（job 文件 + 线程） ====================

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
JOB_FILE = os.path.join(DATA_DIR, "review_job.json")

_job_thread = None  # 后台评审线程（模块级，跨 rerun 存活）
_job_key = ""       # 当前线程服务的「项目|行业」组合

JOB_STALE_SECS = 120  # job 文件超过该时长未更新且线程已死 → 判定中断
PARTIAL_THROTTLE = 0.8  # 打字机 partial 落盘节流（秒）：距上次落盘不足此间隔只更新内存
                        # （SSE chunk 太密，每片都写盘会抖，Windows 上还会撞句柄重试——对齐 ingest）
PARTIAL_TAIL = 800      # 打字机显示的 partial 末尾截取长度（字符）：草稿是 JSON 流，
                        # 全量当 markdown 渲染会被未闭合括号搅乱且越滚越慢


def _job_key_of(project_doc, industry_doc):
    return f"{project_doc['path']}|{industry_doc['path']}"


def _read_job():
    if os.path.exists(JOB_FILE):
        try:
            with open(JOB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _write_job(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = JOB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    for attempt in range(5):  # Windows 上读者持有句柄时 os.replace 会被拒，短暂重试
        try:
            os.replace(tmp, JOB_FILE)  # 原子替换
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, JOB_FILE)


def _remove_job():
    try:
        os.remove(JOB_FILE)
    except OSError:
        pass


def _thread_alive():
    return _job_thread is not None and _job_thread.is_alive()


def _job_stale(job):
    """job 显示 running 但线程已死且文件久未更新（进程重启等）→ 判定中断。"""
    if job.get("status") != "running" or _thread_alive():
        return False
    try:
        age = time.time() - os.path.getmtime(JOB_FILE)
    except OSError:
        return True
    return age > JOB_STALE_SECS


def _job_worker(project_doc, industry_doc, key, api_key, model=None, base_url=None):
    """后台线程体：调 chat 生成评审草稿，partial 经节流实时写 job 文件（打字机显示）。
    注意：线程内没有 ScriptRunContext，禁止调用任何 st.*；
    前端注入的 API Key、模型与端点同样取不到，由 api_key/model/base_url 参数显式带入。"""
    set_thread_api_key(api_key)
    set_thread_model(model)
    set_thread_base_url(base_url)
    state = {"status": "running", "key": key, "partial": "", "data": None, "error": "",
             "started": datetime.now().isoformat(timespec="seconds"),
             "finished": ""}
    last_flush = [0.0]  # 上次 partial 落盘时刻（monotonic）

    def on_chunk(accumulated):
        state["partial"] = accumulated
        # 每个 chunk 只更新内存，距上次落盘超过节流间隔才写盘；
        # 最终 done/error 状态在下面无条件落盘，不会丢末尾
        now = time.monotonic()
        if now - last_flush[0] >= PARTIAL_THROTTLE:
            last_flush[0] = now
            _write_job(state)

    _write_job(state)
    try:
        raw = chat([{"role": "system", "content": _system_prompt(project_doc, industry_doc)},
                    {"role": "user", "content": GENERATE_PROMPT}],
                   max_tokens=8000, on_chunk=on_chunk)
        state["data"] = _parse_json(raw)
        state["status"] = "done"
    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_job(state)


def _start_job(project_doc, industry_doc):
    """启动后台评审线程；已有线程在跑时不重复启动。"""
    global _job_thread, _job_key
    if _thread_alive():
        return False
    _job_key = _job_key_of(project_doc, industry_doc)
    # 主线程取好当前会话的 API Key、模型与端点传给工作线程（线程内访问不了 session_state）
    _job_thread = threading.Thread(
        target=_job_worker, args=(project_doc, industry_doc, _job_key, get_api_key(),
                                  get_model(), get_base_url()),
        daemon=True)
    _job_thread.start()
    return True


@st.fragment(run_every=2)
def _render_job_live(key):
    """轮询 job 文件，打字机式显示生成中的评审草稿。
    固定挂载（内部按状态决定是否渲染），结束/失败边沿按 started 时间戳去重、
    只触发一次整页 rerun——避免条件挂载的卸载与整页 rerun 撞车导致前端 DOM 错位。
    key 为当前选中的「项目|行业」组合：任务属于其他组合时不渲染（主流程已有
    「另一组任务运行中」提示），打字机不串台，也不为无关任务触发整页 rerun。"""
    job = _read_job()
    if not job:
        return
    if job.get("key") != key:
        return  # 属于其他项目/行业组合：done 结果留给切回该组合时的主流程消费
    if job.get("status") == "running" and not _thread_alive():
        return  # 线程不在（中断/重启）：交给主流程的中断提示分支
    if job.get("status") != "running":
        token = job.get("started", "")
        if st.session_state.get("_live_fired_review") != token:
            st.session_state["_live_fired_review"] = token
            st.rerun()  # 整页 rerun，由主流程落草稿/报错
        return
    partial = job.get("partial", "")
    # 纯文本渲染 + 末尾截断：st.text 原地更新单个文本节点，无 markdown/HTML 重解析、
    # 无子节点增删（removeChild 崩溃的根源）；草稿是 JSON 流，截尾避免越绘越慢；
    # 完成后的正式草稿由主流程一次性渲染
    tail = partial[-PARTIAL_TAIL:] if partial else "🔮 AI 正在阅读两份文档……"
    st.text(tail + (" ▌" if partial else ""))


def _default_industry_idx(project_doc, industries):
    """用项目 track 与行业文档 title/name 互相包含匹配，预填行业选择框。"""
    track = (project_doc.get("track") or "").strip().lower()
    if not track:
        return 0
    for i, d in enumerate(industries):
        hay = ((d.get("title") or "") + " " + d.get("name", "")).lower()
        if track in hay:
            return i
    for i, d in enumerate(industries):
        core = re.sub(r"（.*?）|\(.*?\)|\.md", "", d.get("title") or d["name"]).strip().lower()
        if core and core in track:
            return i
    return 0


# ==================== 写回项目文档 ====================

REVIEW_SECTION_RE = re.compile(r"^## 🧭 行业总文档评审\n.*?(?=^## |\Z)",
                               re.DOTALL | re.MULTILINE)


def _render_project_section(today, industry_title, judgment, coord, feedback):
    return "\n".join([
        "## 🧭 行业总文档评审",
        "",
        f"> 评审日期：{today} ｜ 来源行业总文档：{industry_title}",
        "",
        "### 项目判断",
        "",
        judgment.strip() or "（空）",
        "",
        "### 坐标落位",
        "",
        f"- **维度A**：{coord.get('dim_a', '—')}",
        f"- **维度B**：{coord.get('dim_b', '—')}",
        f"- **一句话落位理由**：{coord.get('reason', '—')}",
        "",
        "### 反哺判断",
        "",
        (feedback or "—").strip(),
    ])


def _write_back_project(doc, industry_title, judgment, coord, feedback):
    """替换项目文档中的「## 🧭 行业总文档评审」节，无则文末 --- 后追加。"""
    abs_path = os.path.join(KNOWLEDGE_DIR, doc["path"])
    if not os.path.exists(abs_path):
        raise FileNotFoundError(abs_path)
    with open(abs_path, "r", encoding="utf-8") as f:
        content = f.read()

    today = datetime.now().strftime("%Y-%m-%d")
    section = _render_project_section(today, industry_title, judgment, coord, feedback)

    m = REVIEW_SECTION_RE.search(content)
    if m:
        content = content[:m.start()] + section.rstrip() + "\n\n" + content[m.end():].lstrip("\n")
    else:
        content = content.rstrip() + "\n\n---\n\n" + section + "\n"

    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path


# ==================== 写回行业总文档 ====================

def _find_subsection(content, name_re):
    """定位 ### N.x <名称> 子节，返回 (match_end, section_end)；找不到返回 None。"""
    m = re.search(r"^### \d+\.\d+\s+" + name_re + r".*$", content, re.MULTILINE)
    if not m:
        return None
    nxt = re.search(r"^#{2,3} ", content[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(content)
    return m.end(), end


def _append_table_row(content, name_re, new_row):
    """在指定 ### N.x 子节的表格末尾追加一行；若「（待填）」是唯一数据行则替换之。"""
    loc = _find_subsection(content, name_re)
    if not loc:
        return content, False
    start, end = loc
    # 子节起点回退到标题行首
    line_start = content.rfind("\n", 0, start) + 1
    section = content[line_start:end]
    lines = section.split("\n")
    tbl = [i for i, l in enumerate(lines) if l.strip().startswith("|")]
    if not tbl:
        return content, False
    sep = re.compile(r"^\s*\|[\s\-|]+\|\s*$")
    data_rows = [i for i in tbl if not sep.match(lines[i])]
    body = data_rows[1:]  # 第一行是表头
    if len(body) == 1 and "（待填）" in lines[body[0]]:
        lines[body[0]] = new_row
    else:
        lines.insert(tbl[-1] + 1, new_row)
    return content[:line_start] + "\n".join(lines) + content[end:], True


def _prepend_archive_entry(content, entry):
    """在 ### N.3 新项目评审留档 下按日期倒序插入条目（新条目在最前）。"""
    loc = _find_subsection(content, r"新项目评审留档")
    if not loc:
        return content.rstrip() + "\n\n### 新项目评审留档\n\n" + entry + "\n", False
    start, end = loc
    body = content[start:end].strip()
    if "####" not in body:
        # 只有占位说明文字时整体替换
        return content[:start] + "\n\n" + entry + "\n\n" + content[end:], True
    return content[:start] + "\n\n" + entry + "\n" + content[start:], True


def _write_back_industry(doc, project_title, coord, var_updates,
                         additions, feedback, stage_update):
    """更新行业总文档：落位表 / 关键变量演进表 / N.3 评审留档 / 头部更新日期。"""
    abs_path = os.path.join(KNOWLEDGE_DIR, doc["path"])
    if not os.path.exists(abs_path):
        raise FileNotFoundError(abs_path)
    with open(abs_path, "r", encoding="utf-8") as f:
        content = f.read()

    today = datetime.now().strftime("%Y-%m-%d")

    # d) 更新头部引用块里的更新日期
    content, n = re.subn(r"^>\s*\*{0,2}更新日期\*{0,2}：[^\n]*",
                         f"> **更新日期**：{today}", content,
                         count=1, flags=re.MULTILINE)

    # b) 项目落位表追加一行
    row = (f"| {_cell(project_title)} | {_cell(coord.get('dim_a', ''))} "
           f"| {_cell(coord.get('dim_b', ''))} | {_cell(coord.get('reason', ''))} |")
    content, ok_pos = _append_table_row(content, r"行业坐标", row)

    # a) 关键变量演进表追加行
    ok_var = True
    for v in var_updates:
        vrow = (f"| {today} | {_cell(v.get('variable', ''))} | {_cell(v.get('old', ''))} "
                f"| {_cell(v.get('new', ''))} | {_cell(v.get('source', ''))} |")
        content, ok = _append_table_row(content, r"关键变量演进", vrow)
        ok_var = ok_var and ok

    # c) N.3 新项目评审留档追加条目
    entry = "\n".join([
        f"#### {today} {project_title}",
        "",
        "**增量思考**：",
        "",
        additions.strip() or "（空）",
        "",
        f"**反哺判断**：{(feedback or '—').strip()}",
        "",
        f"**行业阶段更新**：{(stage_update or '维持不变').strip()}",
    ])
    content, ok_arc = _prepend_archive_entry(content, entry)

    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path, {"更新日期": bool(n), "落位表": ok_pos,
                      "关键变量表": ok_var, "评审留档": ok_arc}


# ==================== 主视图（demo §10：五步进度条 → 选择 → 生成 → 两栏审阅 → diff 提交） ====================

_STEP_FLOW = ["01 选项目", "02 选行业", "03 生成", "04 审阅", "05 入库"]


def _steps_html(cur):
    """demo .steps-flow：之前步实心 done，当前步 accent 描点。"""
    parts = []
    for i, name in enumerate(_STEP_FLOW, 1):
        cls = "step-node" + (" done" if i < cur else "") + (" current" if i == cur else "")
        parts.append(f"<div class='{cls}'><span class='dot'></span>"
                     f"<span class='sn'>{name}</span></div>")
        if i < len(_STEP_FLOW):
            parts.append("<div class='step-line'></div>")
    return "<div class='steps-flow'>" + "".join(parts) + "</div>"


def _grab_section(content, kw, n=400):
    """行业总文档里标题含 kw 的章节正文（压平空白、截断），找不到返回 ""。"""
    m = re.search(r"^#{2,4}[^\n]*" + kw + r"[^\n]*\n(.*?)(?=^#{2,4} |\Z)",
                  content, re.DOTALL | re.MULTILINE)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:n]


def _industry_snapshot(content):
    """从行业总文档解析 EXISTING KNOWLEDGE 三件套：行业阶段 / 当前坐标 / 当前核心变量。
    文档结构无强约束，这里是尽力解析，找不到显示「—」。"""
    out = {"stage": "", "coord": "", "variables": []}
    if not content:
        return out
    out["stage"] = _grab_section(content, "行业阶段") or _grab_section(content, "阶段判断")
    out["coord"] = _grab_section(content, "行业坐标")
    m = re.search(r"^#{2,4}[^\n]*(?:核心变量|关键变量)[^\n]*\n(.*?)(?=^#{2,4} |\Z)",
                  content, re.DOTALL | re.MULTILINE)
    if m:
        cells = []
        for line in m.group(1).split("\n"):
            line = line.strip()
            if not line.startswith("|") or re.match(r"^\|[\s\-|]+\|$", line):
                continue
            first = line.strip("|").split("|")[0].strip().replace("**", "")
            if first and first not in ("变量", "关键变量", "核心变量", "名称"):
                cells.append(first)
        out["variables"] = cells[1:6] if len(cells) > 1 else cells[:5]  # 首行多为表头
    return out


def render_review(index, on_saved):
    # 与 reader/battle/radar 一致：撑开主区，消除两侧留白
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)
    st.markdown("<div class='section-label'>新项目评审</div>"
                "<div class='page-title' style='font-size:24px'>行业对照与差距评估</div>",
                unsafe_allow_html=True)

    has_key = bool(get_api_key())
    if not has_key:
        st.warning("未填入 API Key，AI 功能不可用："
                   "请先在左侧边栏「API 设置」处填入你自己的 key（sk-...）。")

    deals = [d for d in index.get("documents", []) if d.get("category_key") == "02_deals"]
    if not deals:
        st.info("02_deals 暂无文档。")
        return
    if not has_key:
        st.info("填入 API Key 后可使用 AI 评审草稿与写回功能。")
        return
    industries = [d for d in index.get("documents", []) if d.get("category_key") == "01_industry"]
    if not industries:
        st.info("01_industry 暂无行业总文档。")
        return

    # ---- 五步进度条：有草稿→04 REVIEW；刚提交→05 COMMIT；否则 01 PROJECT ----
    if st.session_state.pop("_review_just_committed", None):
        cur_step = 5
    elif st.session_state.get("review_draft"):
        cur_step = 4
    else:
        cur_step = 1
    st.markdown(_steps_html(cur_step), unsafe_allow_html=True)

    proj_options = {f"[{d.get('track', '未分类')}] {_doc_title(d)}": d for d in deals}
    preselect = st.session_state.pop("review_preselect_path", None)
    if preselect:  # 归档页「发起评审」跳转：按文档路径预选项目，并清空搜索避免被过滤
        for _label, _d in proj_options.items():
            if _d["path"] == preselect:
                st.session_state.review_project = _label
                st.session_state.review_kw_p = ""
                break
    ind_options = {_doc_title(d): d for d in industries}

    # ---- 01 Project / 02 Industry Context（demo .review-field） ----
    col_p, col_i = st.columns(2)
    with col_p:
        st.markdown("<div class='review-field'><div class='fl'>"
                    "01 项目 —— 搜索或选择项目（02_deals）</div></div>",
                    unsafe_allow_html=True)
        kw_p = st.text_input("搜索项目", key="review_kw_p",
                             placeholder="输入关键词过滤（项目名/赛道/定位）…",
                             label_visibility="collapsed")
        popts = _filter_options(proj_options, kw_p)
        if not popts:
            st.info("无匹配项目，换个关键词试试。")
            return
        chosen_p = st.selectbox("选择评审项目（02_deals）", list(popts.keys()),
                                key="review_project", label_visibility="collapsed")
    pdoc = proj_options[chosen_p]
    with col_i:
        st.markdown("<div class='review-field'><div class='fl'>"
                    "02 行业背景 —— 选择行业总文档（01_industry）</div></div>",
                    unsafe_allow_html=True)
        kw_i = st.text_input("搜索行业总文档", key="review_kw_i",
                             placeholder="输入关键词过滤行业…",
                             label_visibility="collapsed")
        iopts = _filter_options(ind_options, kw_i)
        if not iopts:
            st.info("无匹配行业总文档，换个关键词试试。")
            return
        # 默认行业在过滤后列表中的位置；被过滤掉则退回第一项
        default_label = _doc_title(industries[_default_industry_idx(pdoc, industries)])
        default_i = list(iopts.keys()).index(default_label) if default_label in iopts else 0
        chosen_i = st.selectbox("选择行业总文档（01_industry）", list(iopts.keys()),
                                index=default_i, key="review_industry",
                                label_visibility="collapsed")
    indoc = ind_options[chosen_i]

    # ---- 后台评审任务：恢复 / 状态显示 ----
    key = _job_key_of(pdoc, indoc)
    job = _read_job()
    if job and job.get("status") == "done":
        if job.get("key") == key:
            # 切换页面/刷新回来后，已完成的草稿落进 session_state（与旧同步路径同构）
            data = job.get("data") or {}
            st.session_state.review_draft = {
                "data": data,
                "project_path": pdoc["path"],
                "industry_path": indoc["path"],
            }
            st.session_state.review_pj = data.get("project_judgment", "")
            st.session_state.review_ida = data.get("industry_doc_additions", "")
            st.session_state.review_flash = "评审草稿已生成，请审阅后确认写回。"
            _remove_job()
            st.rerun()
        # key 不匹配的 done 结果不丢弃：留在 job 文件里，切回对应组合时落草稿
        # （生成中切换项目/行业选择是常态，一切就丢草稿等于白跑；下次启动新任务会覆盖）
        job = None

    # ---- 03 ANALYSIS：生成草稿（后台线程，不阻塞 rerun）----
    running = _thread_alive()
    if st.button("生成评审草稿", type="primary", disabled=running,
                 help="后台生成，可随时切换页面；完成后回到本页自动出现草稿"):
        _start_job(pdoc, indoc)
        st.rerun()
    if running:
        st.markdown("<div class='progress-note'>正在生成评审草稿… "
                    "后台进行中，可自由切换页面。</div>", unsafe_allow_html=True)

    _render_job_live(key)  # 固定挂载，内部按状态决定是否渲染
    if job and job.get("status") == "running":
        if _job_stale(job):
            st.warning("评审任务被中断（应用重启或进程退出），请重新生成。")
            _remove_job()
            job = None
        elif job.get("key") != key:
            st.info("另一组项目/行业的评审任务正在后台运行……")
    elif job and job.get("status") == "error":
        # 失败保留现场：不清草稿、只报错，可直接重新生成
        st.error(f"生成评审草稿失败：{job.get('error', '未知错误')}")
        _remove_job()
        job = None

    flash = st.session_state.pop("review_flash", None)
    if flash:
        st.success(flash)
    # 串联入口常驻到被点击为止：不能 pop 后渲染（点击触发的 rerun 中值已消失，按钮会失效）
    next_battle = st.session_state.get("review_next_battle_path")
    if next_battle and st.button("⚔️ 下一步：对该文档发起 Battle", type="primary"):
        st.session_state.pop("review_next_battle_path", None)
        st.session_state.battle_doc_path = next_battle
        # battle 页 selectbox 一旦实例化过，widget 状态粘性会让 index 预选失效，
        # 跳转前清掉旧 widget 值（连同旧文档的对话缓存），预选才能对新文档生效
        st.session_state.pop("battle_doc", None)
        st.session_state.pop("battle_msgs", None)
        st.session_state.view_mode = "battle"
        st.session_state.selected_doc = None
        st.rerun()

    # ---- 04 REVIEW：两栏（EXISTING KNOWLEDGE / AI ASSESSMENT）+ 05 COMMIT diff 预览 ----
    draft = st.session_state.get("review_draft")
    if not draft or draft.get("project_path") != pdoc["path"] \
            or draft.get("industry_path") != indoc["path"]:
        return
    data = draft["data"]
    coord = data.get("coordinate_position") or {}
    var_updates = data.get("variable_updates") or []
    feedback = data.get("feedback_thesis", "")
    stage_update = data.get("industry_stage_update", "")

    snap = _industry_snapshot(indoc.get("content", ""))
    vars_html = ("".join(f"<li>{html.escape(v)}</li>" for v in snap["variables"])
                 if snap["variables"] else "<p>—</p>")
    if snap["variables"]:
        vars_html = f"<ul>{vars_html}</ul>"

    col_l, col_r = st.columns(2, gap="large")
    with col_l:
        st.markdown("<div class='section-label' style='margin-top:1.2rem'>"
                    "现有知识</div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='assess-block'><h4>行业阶段</h4>"
            f"<p>{html.escape(snap['stage'] or '—')}</p></div>"
            "<div class='assess-block'><h4>当前坐标</h4>"
            f"<p>{html.escape(snap['coord'] or '—')}</p></div>"
            f"<div class='assess-block'><h4>当前核心变量</h4>{vars_html}</div>",
            unsafe_allow_html=True)
    with col_r:
        st.markdown("<div class='section-label' style='margin-top:1.2rem'>"
                    "AI 评估</div>", unsafe_allow_html=True)
        st.markdown("<div class='assess-block'><h4>项目判断（可编辑，写入项目文档）</h4></div>",
                    unsafe_allow_html=True)
        st.text_area("项目判断", key="review_pj", height=180,
                     label_visibility="collapsed")
        st.markdown(
            "<div class='assess-block'><h4>坐标落位</h4>"
            f"<p>维度A <b>{html.escape(str(coord.get('dim_a', '—')))}</b> ｜ "
            f"维度B <b>{html.escape(str(coord.get('dim_b', '—')))}</b> ｜ "
            f"{html.escape(str(coord.get('reason', '—')))}</p></div>",
            unsafe_allow_html=True)
        if var_updates:
            lines = ["| 关键变量 | 旧判断 | 新判断 | 触发来源 |", "|---|---|---|---|"]
            for v in var_updates:
                lines.append(f"| {_cell(v.get('variable'))} | {_cell(v.get('old'))} "
                             f"| {_cell(v.get('new'))} | {_cell(v.get('source'))} |")
            st.markdown("<div class='assess-block'><h4>关键变量更新</h4></div>",
                        unsafe_allow_html=True)
            st.markdown("\n".join(lines))
        else:
            st.markdown("<div class='assess-block'><h4>关键变量更新</h4>"
                        "<p>无更新</p></div>", unsafe_allow_html=True)
        st.markdown(
            "<div class='assess-block'><h4>反哺判断</h4>"
            f"<p>{html.escape(feedback or '—')}</p></div>"
            "<div class='assess-block'><h4>行业阶段更新</h4>"
            f"<p>{html.escape(stage_update or '维持不变')}</p></div>",
            unsafe_allow_html=True)
        st.markdown("<div class='assess-block'><h4>增量思考"
                    "（可编辑，写入行业总文档 · N.3 评审留档）</h4></div>",
                    unsafe_allow_html=True)
        st.text_area("增量思考", key="review_ida", height=180,
                     label_visibility="collapsed")

    # ---- Commit Preview（demo .diff-block）----
    has_section = bool(REVIEW_SECTION_RE.search(pdoc.get("content", "")))
    p_sign, p_cls = ("~", "mod") if has_section else ("+", "add")
    rows = [
        (p_cls, p_sign,
         ("替换" if has_section else "新增") + "：「## 🧭 行业总文档评审」节"
         "（项目判断 + 坐标落位 + 反哺判断）", _doc_title(pdoc)),
        ("add", "+", "新增：行业坐标落位表 1 行", _doc_title(indoc)),
    ]
    if var_updates:
        rows.append(("add", "+", f"新增：关键变量演进 {len(var_updates)} 行",
                     _doc_title(indoc)))
    rows += [
        ("add", "+", "新增：N.3 评审留档条目（增量思考 + 反哺判断 + 阶段更新）",
         _doc_title(indoc)),
        ("mod", "~", "更新：头部更新日期", _doc_title(indoc)),
    ]
    st.markdown(
        "<div class='home-section'><div class='section-label'>"
        "写回预览 —— 差异对比</div><div class='diff-block'>"
        + "".join(f"<div class='diff-row {cls}'><span class='sign'>{sign}</span>"
                  f"<span>{html.escape(text)}</span>"
                  f"<span class='dt'>{html.escape(dt)}</span></div>"
                  for cls, sign, text, dt in rows)
        + "</div></div>",
        unsafe_allow_html=True)

    c_back, c_commit, _pad = st.columns([1.2, 1.6, 5])
    with c_back:
        if st.button("返回", key="review_back", use_container_width=True,
                     help="丢弃草稿，返回重新选择/生成"):
            st.session_state.pop("review_draft", None)
            st.rerun()
    with c_commit:
        if st.button("提交入库", type="primary", key="review_commit",
                     use_container_width=True):
            judgment = st.session_state.get("review_pj", data.get("project_judgment", ""))
            additions = st.session_state.get("review_ida",
                                             data.get("industry_doc_additions", ""))
            try:
                p_path = _write_back_project(pdoc, _doc_title(indoc), judgment, coord,
                                             feedback)
                i_path, report = _write_back_industry(indoc, _doc_title(pdoc), coord,
                                                      var_updates, additions, feedback,
                                                      stage_update)
            except Exception as e:
                st.error(f"写入失败：{e}")
                return
            st.session_state.pop("review_draft", None)
            on_saved()
            st.session_state.review_next_battle_path = pdoc["path"]  # 串联：写回后提供「发起 Battle」跳转
            st.session_state._review_just_committed = True  # 进度条点亮 05 COMMIT
            miss = [k for k, v in report.items() if not v]
            note = f"（未定位到：{'、'.join(miss)}）" if miss else ""
            st.session_state.review_flash = (
                f"已写入 `{os.path.basename(p_path)}` 的「🧭 行业总文档评审」与 "
                f"`{os.path.basename(i_path)}` 的落位表/关键变量/评审留档{note}。"
            )
            st.rerun()
