# -*- coding: utf-8 -*-
"""
Thesis Battle 2.0 —— AI 红队对话模式

AI 在限定方法论下扮演投委会反对派，与用户辩论已沉淀的投资观点：
- 选定观点文档后，AI 读入文档作为战场背景，主动发起假设攻击；
- 对话按项目持久化（data/battle_sessions/），跨天可继续；
- 用户说「今天到这里」或点击写回按钮，AI 将整场讨论提取为
  「当前状态表 + 当日 Battle Log」预览，经用户审批确认后才
  覆盖更新到项目文档的 ## ⚔️ Battle 记录 节。
"""
import hashlib
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

SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "battle_sessions")

CLOSING_RE = re.compile(r"(今天|今日).{0,4}(到|先).{0,2}这|到此为止|收工|先聊到这|就到这")

STATUS_DESC = {
    "Confirmed": "证据增强",
    "Monitoring": "观点仍成立，但关键变量未确定",
    "Challenged": "出现重要反证，需要降低信心",
    "Invalidated": "核心假设失效",
}

SYSTEM_TEMPLATE = """你是投资委员会中的「红队」成员，专门对用户（投资人）已沉淀的投资观点做压力测试（Thesis Battle）。

【角色立场】
- 你是用户的对立面。任务不是生成 Bull/Bear Case，不是讨好用户，而是持续攻击其观点最脆弱之处。
- 不替投资者做决定，只帮助其提高判断质量。

【方法论——每次发言必须落在以下四步之一，开头用【Step N · 名称】标注当前步骤】
Step 1 · Assumption Attack：识别哪些假设决定观点成立、哪些未经充分验证、哪些最可能失败。按「关键假设 / 为什么重要 / 当前验证程度 / 风险等级」输出。
Step 2 · Falsification Check：逐条检查证伪条件，判定 未触发 / 接近触发 / 已触发，给出证据与对观点的冲击。
Step 3 · Counter World：构造「如果观点错误，真实世界应该是什么样」，并比较当前现实更接近哪个世界。
Step 4 · Thesis Evaluation：给出观点状态（Confirmed / Monitoring / Challenged / Invalidated）与置信度调整及原因。

【铁律】
1. 不追求证明观点正确，而是寻找观点何时可能错误。
2. 不输出泛泛风险，只输出可观察、可验证、能改变投资判断的条件。区分「风险」与「证伪条件」。
3. 用户反驳时，必须回应其论据本身：被说服就承认并收敛攻击点，未被说服就说明为什么。
4. 每次发言聚焦一到两个攻击点，像真实投委会辩论一样逐步推进，不要一次铺开全部。
5. 用户说「今天到这里」「收工」等结束语时，输出当日辩论小结（3-5 条要点 + 你认为的当前观点状态与置信度），并提示用户结果将写回文档。
6. 全程使用中文。

【战场背景——用户当前的投资观点文档】
{doc_content}
"""

EXTRACT_PROMPT = """基于以上整场 Battle 对话，提取当前观点的最新状态。只输出一个 JSON 对象，不要任何其他文字：
{
  "core_belief": "核心判断，一句话",
  "assumptions": [{"assumption": "关键假设", "verification": "已验证/部分验证/未验证", "risk": "高/中/低"}],
  "falsifications": [{"condition": "证伪条件", "status": "未触发/接近触发/已触发", "evidence": "当前证据"}],
  "counter_world": "反向世界一句话描述",
  "closer_world": "原观点世界/反向世界/未分胜负",
  "status": "Confirmed/Monitoring/Challenged/Invalidated",
  "confidence_original": 60,
  "confidence_current": 55,
  "confidence_reason": "置信度变化原因",
  "summary": ["当日辩论要点1", "要点2", "要点3"],
  "next_actions": ["下一步研究动作1", "动作2"]
}
若对话中未涉及某字段，基于文档背景给出最合理推断；置信度为 0-100 的整数。"""


# ==================== 对话持久化 ====================

def _session_file(doc_path):
    h = hashlib.md5(doc_path.encode("utf-8")).hexdigest()[:12]
    return os.path.join(SESSION_DIR, f"{h}.json")


def _load_messages(doc_path):
    path = _session_file(doc_path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("messages", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_messages(doc_path, messages):
    os.makedirs(SESSION_DIR, exist_ok=True)
    with open(_session_file(doc_path), "w", encoding="utf-8") as f:
        json.dump({"doc_path": doc_path,
                   "updated": datetime.now().isoformat(),
                   "messages": messages}, f, ensure_ascii=False, indent=2)


# ==================== 后台 AI 回复（pending 文件 + 线程） ====================

_reply_thread = None    # 后台回复线程（模块级，跨 rerun 存活）
_reply_doc_path = ""    # 当前线程服务的文档
_reply_cancel = None    # 当前线程的取消标记（threading.Event，每轮新建）
_state_lock = threading.Lock()  # 保护 session_state/messages 关键段

PENDING_STALE_SECS = 120  # pending 文件超过该时长未更新且线程已死 → 判定中断
PARTIAL_THROTTLE = 0.8  # 打字机 partial 落盘节流（秒）：距上次落盘不足此间隔只更新内存
                        # （SSE chunk 太密，每片都写盘会抖，Windows 上还会撞句柄重试——对齐 ingest）


def _pending_file(doc_path):
    h = hashlib.md5(doc_path.encode("utf-8")).hexdigest()[:12]
    return os.path.join(SESSION_DIR, f"{h}.pending.json")


def _read_pending(doc_path):
    path = _pending_file(doc_path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _write_pending(doc_path, state):
    os.makedirs(SESSION_DIR, exist_ok=True)
    tmp = _pending_file(doc_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    for attempt in range(5):  # Windows 上读者持有句柄时 os.replace 会被拒，短暂重试
        try:
            os.replace(tmp, _pending_file(doc_path))  # 原子替换
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, _pending_file(doc_path))


def _remove_pending(doc_path):
    try:
        os.remove(_pending_file(doc_path))
    except OSError:
        pass


def _reply_running_for(doc_path):
    return (_reply_doc_path == doc_path
            and _reply_thread is not None and _reply_thread.is_alive())


def _pending_stale(doc_path, pend):
    """pending 显示 running 但线程已死且文件久未更新（进程重启等）→ 判定中断。"""
    if pend.get("status") != "running" or _reply_running_for(doc_path):
        return False
    try:
        age = time.time() - os.path.getmtime(_pending_file(doc_path))
    except OSError:
        return True
    return age > PENDING_STALE_SECS


def _reply_worker(doc, msgs_snapshot, doc_path, cancel, api_key, model=None, base_url=None):
    """后台线程体：调 _ai_reply，部分回复经 on_chunk 实时写 pending 文件。
    cancel 为本轮专属的取消标记：被「中断」置位后，线程不再写任何结果。
    注意：线程内没有 ScriptRunContext，禁止调用任何 st.*；
    前端注入的 API Key、模型与端点同样取不到，由 api_key/model/base_url 参数显式带入。"""
    set_thread_api_key(api_key)
    set_thread_model(model)
    set_thread_base_url(base_url)
    state = {"status": "running", "partial": "", "error": "",
             "started": datetime.now().isoformat(timespec="seconds")}
    last_flush = [0.0]  # 上次 partial 落盘时刻（monotonic）

    def on_chunk(accumulated):
        if cancel.is_set():
            return
        state["partial"] = accumulated
        # 每个 chunk 只更新内存，距上次落盘超过节流间隔才写盘；
        # 最终 done/error 状态在下面无条件落盘，不会丢末尾
        now = time.monotonic()
        if now - last_flush[0] >= PARTIAL_THROTTLE:
            last_flush[0] = now
            _write_pending(doc_path, state)

    _write_pending(doc_path, state)
    try:
        reply = _ai_reply(doc, msgs_snapshot, on_chunk=on_chunk)
        if cancel.is_set():
            return  # 已被中断：不写 done 结果，partial 一并作废
        state["partial"] = reply
        state["status"] = "done"
    except Exception as e:
        if cancel.is_set():
            return
        state["status"] = "error"
        state["error"] = str(e)
    _write_pending(doc_path, state)


def _start_reply(doc, doc_path):
    """为当前对话启动后台回复线程；已有线程在跑时不重复启动。
    每轮新建取消 Event，worker 持有自己的引用，旧轮的取消状态不会误伤新轮。"""
    global _reply_thread, _reply_doc_path, _reply_cancel
    if _reply_thread is not None and _reply_thread.is_alive():
        return False
    snapshot = list(st.session_state.get("battle_msgs", []))
    _reply_doc_path = doc_path
    _reply_cancel = threading.Event()
    # 主线程取好当前会话的 API Key、模型与端点传给工作线程（线程内访问不了 session_state）
    _reply_thread = threading.Thread(
        target=_reply_worker,
        args=(doc, snapshot, doc_path, _reply_cancel, get_api_key(), get_model(),
              get_base_url()),
        daemon=True)
    _reply_thread.start()
    return True


def _break_reply(doc_path):
    """中断：取消在跑的回复（若有）、清空对话并落盘、绝不写回文档。
    线程无法强杀，靠 cancel Event 让 worker 退出前不写结果；
    线程引用立即释放，新回复无需等旧线程结束即可启动。"""
    global _reply_thread, _reply_doc_path
    if _reply_cancel is not None:
        _reply_cancel.set()
    _reply_thread = None
    _reply_doc_path = ""
    _remove_pending(doc_path)
    st.session_state.battle_msgs = []
    _save_messages(doc_path, [])
    st.session_state.pop("battle_finalize", None)  # 核心诉求：中断后绝不触发写回
    st.session_state.pop("battle_pending", None)   # 清掉排队中的「先开火」
    st.session_state.pop("battle_preview", None)   # 清掉待审批的写回预览
    st.session_state.battle_flash = "已中断：回复已取消、对话已清空，不会写回文档。"


@st.fragment(run_every=2)
def _render_reply_live(doc_path):
    """轮询 pending 文件，打字机式显示进行中的 AI 回复。
    固定挂载（内部按状态决定是否渲染），结束/失败边沿按 started 时间戳去重、
    只触发一次整页 rerun——避免条件挂载的卸载与整页 rerun 撞车导致前端 DOM 错位。"""
    pend = _read_pending(doc_path)
    if not pend:
        return
    if pend.get("status") == "running" and not _reply_running_for(doc_path):
        return  # 线程不在（中断/重启）：交给主流程的中断提示分支
    if pend.get("status") != "running":
        token = pend.get("started", "")
        if st.session_state.get("_live_fired_battle") != token:
            st.session_state["_live_fired_battle"] = token
            st.rerun()  # 整页 rerun，由主流程落盘/报错
        return
    partial = pend.get("partial", "")
    # 纯文本渲染：st.text 原地更新单个文本节点，无 markdown/HTML 重解析、
    # 无子节点增删——动态 HTML 重解析导致 React 虚拟 DOM 与真实 DOM 脱节，
    # 是 removeChild 崩溃的根源；完成后的正式排版由主流程一次性渲染
    st.text(partial + " ▌" if partial else "🔴 红队思考中……")


# ==================== AI 调用 ====================

def _system_prompt(doc):
    content = doc.get("content", "")
    if len(content) > 8000:
        content = content[:8000] + "\n……（文档过长，已截断）"
    return SYSTEM_TEMPLATE.format(doc_content=content or "（文档为空）")


def _ai_reply(doc, msgs, on_chunk=None):
    history = [{"role": m["role"], "content": m["content"]} for m in msgs[-40:]]
    return chat([{"role": "system", "content": _system_prompt(doc)}] + history,
                max_tokens=8000, on_chunk=on_chunk, feature="battle")


def _parse_json(raw):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    text = m.group(1) if m else raw[raw.find("{"):raw.rfind("}") + 1]
    if not text:
        raise ValueError("AI 未返回可解析的 JSON")
    return json.loads(text)


# ==================== 写回文档 ====================

SECTION_HEADER = "## ⚔️ Battle 记录"


def _cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _render_state_md(data, today):
    status = data.get("status", "Monitoring")
    desc = STATUS_DESC.get(status, "")
    co, cn = data.get("confidence_original", "—"), data.get("confidence_current", "—")
    lines = [
        SECTION_HEADER,
        "",
        f"### 当前状态（更新于 {today}）",
        "",
        f"**观点状态：{status}**（{desc}） · 置信度 {co}% → {cn}%",
        "",
        f"**Core Belief**：{data.get('core_belief', '—')}",
        "",
        "| 关键假设 | 验证程度 | 风险 |",
        "|---|---|---|",
    ]
    for a in data.get("assumptions", []):
        lines.append(f"| {_cell(a.get('assumption'))} | {_cell(a.get('verification'))} | {_cell(a.get('risk'))} |")
    lines += [
        "",
        "| 证伪条件 | 状态 | 当前证据 |",
        "|---|---|---|",
    ]
    for fc in data.get("falsifications", []):
        lines.append(f"| {_cell(fc.get('condition'))} | {_cell(fc.get('status'))} | {_cell(fc.get('evidence'))} |")
    lines += [
        "",
        f"**反向世界**：{data.get('counter_world', '—')}（当前现实更接近：{data.get('closer_world', '—')}）",
        "",
        f"**置信度变化原因**：{data.get('confidence_reason', '—')}",
        "",
        "**Next Actions**：",
    ]
    actions = data.get("next_actions", [])
    lines += [f"- [ ] {_cell(a)}" for a in actions] or ["- [ ] （待定）"]
    lines += ["", "### Battle Log", ""]
    return "\n".join(lines)


def _render_log_md(data, today):
    lines = [f"#### {today}"]
    for s in data.get("summary", []):
        lines.append(f"- {_cell(s)}")
    lines.append(f"- 观点状态 {data.get('status', '—')}；"
                 f"置信度 {data.get('confidence_original', '—')}% → {data.get('confidence_current', '—')}%")
    return "\n".join(lines)


def _write_back(doc, data):
    """替换文档中的 Battle 记录节（保留历史日志条目），无则追加到文末。"""
    abs_path = os.path.join(KNOWLEDGE_DIR, doc["path"])
    if not os.path.exists(abs_path):
        raise FileNotFoundError(abs_path)
    with open(abs_path, "r", encoding="utf-8") as f:
        content = f.read()

    today = datetime.now().strftime("%Y-%m-%d")
    new_log = _render_log_md(data, today)

    m = re.search(r"^## ⚔️ Battle 记录\n.*?(?=^## |\Z)", content, re.DOTALL | re.MULTILINE)
    if m:
        old_logs = re.findall(r"^#### .+?(?=^#### |\Z)", m.group(0), re.DOTALL | re.MULTILINE)
        section = _render_state_md(data, today) + new_log + "\n\n" + "\n\n".join(l.strip() for l in old_logs)
        content = content[:m.start()] + section.rstrip() + "\n\n" + content[m.end():].lstrip("\n")
    else:
        section = _render_state_md(data, today) + new_log
        content = content.rstrip() + "\n\n---\n\n" + section + "\n"

    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path


def _prepare_preview(doc):
    """写回第一阶段：调 LLM 提取状态表 + 解析，存入 battle_preview 供用户审批。
    本阶段不调 _write_back，文档不会被修改；失败时报错并清除预览状态。"""
    msgs = st.session_state.get("battle_msgs", [])
    if not msgs:
        st.warning("还没有对话内容，先与红队交锋几个回合。")
        return
    with st.spinner("AI 正在汇总本场 Battle，生成写回预览……"):
        try:
            raw = chat([{"role": "system", "content": "你是结构化信息提取助手，只输出 JSON。"}]
                       + [{"role": m["role"], "content": m["content"]} for m in msgs[-40:]]
                       + [{"role": "user", "content": EXTRACT_PROMPT}],
                       max_tokens=10000, feature="battle")
            data = _parse_json(raw)
        except Exception as e:
            st.session_state.pop("battle_preview", None)
            st.error(f"生成写回预览失败：{e}")
            return
    st.session_state.battle_preview = {"doc_path": doc["path"], "data": data}
    st.rerun()


def _confirm_writeback(doc, on_saved):
    """写回第二阶段：用户点「确认写回」后才真正落盘。
    预览先校验后落盘、成功才丢弃——写回失败时预览保留，用户可重试 Approve，
    不必重新生成（重新生成要再花一次 AI 调用）。"""
    preview = st.session_state.get("battle_preview")
    if not preview or preview.get("doc_path") != doc["path"]:
        return
    try:
        path = _write_back(doc, preview["data"])
    except Exception as e:
        st.error(f"写回失败：{e}。预览仍保留，可重试确认写回。")
        return
    st.session_state.pop("battle_preview", None)
    on_saved()
    st.session_state.battle_flash = (
        f"已更新 `{os.path.basename(path)}` 的「⚔️ Battle 记录」："
        f"状态表已刷新，当日日志已追加。"
    )
    st.rerun()


# ==================== 主视图（demo §9 三栏辩论室） ====================

_PHASE_LABELS = {
    1: "假设攻击",
    2: "证伪检验",
    3: "反向世界",
    4: "论点评估",
}
_PHASE_FLOW = "01 假设 → 02 证伪 → 03 反世界 → 04 评估"

# 假设验证程度 → 右栏状态点（demo .assumption-row .st）
_VERIFY_STATE = {"已验证": ("成立", "ok"),
                 "部分验证": ("受质疑", "attacked"),
                 "未验证": ("未验证", "broken")}


def _esc(s):
    return html.escape(str(s or ""))


def _parse_battle_state(content):
    """从文档「## ⚔️ Battle 记录」节解析当前状态面板数据；无此节返回 None。
    节结构见 _render_state_md：状态行 / Core Belief / 关键假设表 / 证伪条件表 / 反向世界。"""
    m = re.search(r"^## ⚔️ Battle 记录\n.*?(?=^## |\Z)", content or "",
                  re.DOTALL | re.MULTILINE)
    if not m:
        return None
    sec = m.group(0)
    out = {"status": "", "status_desc": "", "conf_from": None, "conf_to": None,
           "core_belief": "", "assumptions": [], "falsifications": [],
           "counter_world": "", "closer_world": ""}
    sm = re.search(r"\*\*观点状态：(\w+)\*\*（([^）]*)）\s*·\s*置信度\s*(\d+)%\s*→\s*(\d+)%", sec)
    if sm:
        out["status"], out["status_desc"] = sm.group(1), sm.group(2)
        out["conf_from"], out["conf_to"] = int(sm.group(3)), int(sm.group(4))
    cb = re.search(r"\*\*Core Belief\*\*：(.+)", sec)
    if cb:
        out["core_belief"] = cb.group(1).strip()
    cw = re.search(r"\*\*反向世界\*\*：(.+?)（当前现实更接近：(.+?)）", sec)
    if cw:
        out["counter_world"], out["closer_world"] = cw.group(1).strip(), cw.group(2).strip()

    def _table(header):
        tm = re.search(r"\| *" + header + r" *\|[^\n]*\n\|[\s\-|]+\|\n((?:\|[^\n]*\|\n?)+)",
                       sec)
        if not tm:
            return []
        rows = []
        for line in tm.group(1).strip().split("\n"):
            cells = [c.strip().replace("\\|", "|")
                     for c in line.strip().strip("|").split("|")]
            if any(c and c != "—" for c in cells):
                rows.append(cells)
        return rows

    out["assumptions"] = _table("关键假设")       # [假设, 验证程度, 风险]
    out["falsifications"] = _table("证伪条件")    # [条件, 状态, 当前证据]
    return out


def _current_phase(msgs):
    """当前辩论阶段：取最后一条 AI 发言的【Step N · 名称】标注；没有则默认第一步。"""
    for m in reversed(msgs):
        if m["role"] != "assistant":
            continue
        sm = re.search(r"【Step\s*(\d+)\s*·\s*([^】]+)】", m["content"])
        if sm:
            return _PHASE_LABELS.get(int(sm.group(1)), _esc(sm.group(2)).upper())
        break  # 只看最后一条 AI 发言
    return _PHASE_LABELS[1]


def _entry_html(idx, role, content):
    """demo .battle-entry：左细竖线 + AI/YOU 标签 + mono 序号 + 正文（AI 竖线 accent）。
    消息没有持久化时间戳（{role, content}），用 mono 序号代替 demo 的时间位。"""
    who = "AI" if role == "assistant" else "YOU"
    cls = "battle-entry ai" if role == "assistant" else "battle-entry"
    # 轻量 markdown：先全文 escape 防注入，再恢复 **粗体**；空行分段、单换行 <br>
    body = _esc(content)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    paras = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>"
                    for p in re.split(r"\n\s*\n", body) if p.strip())
    return (f"<div class='{cls}'><div class='who'>{who}"
            f"<span class='time'>#{idx:02d}</span></div>"
            f"<div class='body'>{paras}</div></div>")


def _confidence_html(conf_to):
    """demo .confidence-box：等级 + 3px 细条（宽度=置信度百分比）。"""
    if conf_to is None:
        return ""
    level = "高" if conf_to >= 70 else ("中" if conf_to >= 40 else "低")
    return ("<div class='battle-kv confidence-box'><div class='k'>置信度</div>"
            f"<div class='level'>{level} · {conf_to}%</div>"
            f"<div class='bar'><i style='width:{conf_to}%'></i></div></div>")


def _thesis_context_html(doc, state):
    """左栏 THESIS CONTEXT：项目 / 核心观点 / Evidence / Assumptions。"""
    title = doc.get("title") or doc["name"].replace(".md", "")
    # 核心观点：文档副标题（> 引用行）或首段；写过 Battle 的以 Core Belief 为准
    belief = (state or {}).get("core_belief") or (doc.get("subtitle") or "").strip()
    if not belief:
        m = re.search(r"\n\s*\n([^\n#>|-][^\n]+)", doc.get("content", ""))
        belief = m.group(1).strip() if m else "—"
    parts = ["<div class='section-label'>论文背景</div>",
             f"<div class='battle-kv'><div class='k'>项目</div>"
             f"<div class='v'>{_esc(title)}</div></div>",
             f"<div class='battle-kv'><div class='k'>核心观点</div>"
             f"<div class='v'>{_esc(belief[:200])}</div></div>"]
    if state:
        evidences = [f[2] for f in state["falsifications"] if len(f) > 2 and f[2] != "—"]
        if evidences:
            lis = "".join(f"<li>{_esc(e[:80])}</li>" for e in evidences[:5])
            parts.append(f"<div class='battle-kv'><div class='k'>证据</div>"
                         f"<div class='v'><ul>{lis}</ul></div></div>")
        if state["assumptions"]:
            lis = "".join(f"<li>{_esc(a[0][:80])}</li>" for a in state["assumptions"])
            parts.append(f"<div class='battle-kv'><div class='k'>关键假设</div>"
                         f"<div class='v'><ul>{lis}</ul></div></div>")
    else:
        parts.append("<div class='battle-kv'><div class='k'>证据</div>"
                     "<div class='v' style='color:var(--text-tertiary);font-size:12px'>"
                     "首场论战写回后生成</div></div>")
    return "".join(parts)


def _battle_state_html(state):
    """右栏 BATTLE STATE：Core Assumptions（状态点）/ Unresolved / Falsification / Confidence。"""
    if not state:
        return ("<div class='section-label'>战斗状态</div>"
                "<div class='battle-kv'><div class='v' "
                "style='color:var(--text-tertiary);font-size:12px'>"
                "暂无沉淀状态：结束今日论战并确认写回后，"
                "这里会显示假设状态、证伪条件与置信度。</div></div>")
    parts = ["<div class='section-label'>战斗状态</div>"]
    if state["assumptions"]:
        rows = []
        for i, a in enumerate(state["assumptions"]):
            verification = a[1] if len(a) > 1 else ""
            label, cls = _VERIFY_STATE.get(verification, ("待验证", "attacked"))
            rows.append(f"<div class='assumption-row'><span class='an'>{i + 1:02d}</span>"
                        f"<span>{_esc(a[0][:40])}</span>"
                        f"<span class='st {cls}'>{label}</span></div>")
        parts.append(f"<div class='battle-kv'><div class='k'>关键假设</div>"
                     f"<div class='v'>{''.join(rows)}</div></div>")
    unresolved = sum(1 for a in state["assumptions"]
                     if len(a) > 1 and a[1] in ("未验证", "部分验证"))
    parts.append(f"<div class='battle-kv'><div class='k'>待验证</div>"
                 f"<div class='v'>{unresolved:02d} 项</div></div>")
    if state["falsifications"]:
        f0 = state["falsifications"][0]
        f_text = f0[0] + (f"（{_esc(f0[1])}）" if len(f0) > 1 and f0[1] else "")
        parts.append(f"<div class='battle-kv'><div class='k'>证伪条件</div>"
                     f"<div class='v' style='font-size:12.5px;color:var(--text-secondary)'>"
                     f"{_esc(f_text[:120])}</div></div>")
    parts.append(_confidence_html(state["conf_to"]))
    return "".join(parts)


def render_battle(index, on_saved):
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)
    st.markdown("<div class='section-label' style='margin-top:0.4rem'>论文之战</div>"
                "<div class='page-title' style='font-size:24px'>投资观点压力测试</div>",
                unsafe_allow_html=True)

    if not get_api_key():
        st.warning("未填入 API Key，AI 功能不可用："
                   "请先在左侧边栏「API 设置」处填入你自己的 key（sk-...）。")
        return

    # ---- 观点来源文档：widget 画在左栏顶部，取值先从 session_state 读 ----
    # （widget 状态在上一轮已写入 session_state.battle_doc；后置渲染，值先行——
    #   这样文档解析/会话恢复等逻辑可以保持在三栏渲染之前）
    docs = sorted(index.get("documents", []),
                  key=lambda d: (d.get("category_key") != "02_deals", d.get("category", ""), d["name"]))
    options = {f"{d.get('category_icon', '📁')} [{d.get('category', '其他')}] "
               f"{d.get('title') or d['name'].replace('.md', '')}": d for d in docs}
    labels = list(options.keys())
    if not labels:
        st.info("知识库还没有可论战的文档：先到「📥 文件归档」归档一篇项目/行业文档，再回来。")
        return
    default_idx = 0
    for i, label in enumerate(labels):
        if options[label]["path"] == st.session_state.get("battle_doc_path"):
            default_idx = i
            break
    _prev_label = st.session_state.get("battle_doc")
    if _prev_label in options:
        doc = options[_prev_label]
    else:
        doc = options[labels[default_idx]]
    _cur_idx = labels.index(next(l for l, o in options.items()
                                 if o["path"] == doc["path"]))

    if doc["path"] != st.session_state.get("battle_doc_path"):
        st.session_state.battle_doc_path = doc["path"]
        st.session_state.battle_msgs = _load_messages(doc["path"])
    msgs = st.session_state.setdefault("battle_msgs", _load_messages(doc["path"]))

    # ---- 后台回复恢复：切换页面/刷新回来后，已完成的回复先落进 messages ----
    pend = _read_pending(doc["path"])
    if pend and pend.get("status") == "done":
        reply = pend.get("partial", "")
        with _state_lock:
            msgs = st.session_state.setdefault("battle_msgs", _load_messages(doc["path"]))
            # msgs 为空说明对话已被中断/清空，迟到的 done 结果直接丢弃
            if reply and msgs and not (msgs[-1]["role"] == "assistant"
                                       and msgs[-1]["content"] == reply):
                msgs.append({"role": "assistant", "content": reply})
                _save_messages(doc["path"], msgs)
        _remove_pending(doc["path"])
        pend = None
        # 回复落盘后检查最后一条用户消息是否触发写回预览（仍需用户审批）
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        if last_user and CLOSING_RE.search(last_user):
            st.session_state.battle_finalize = True
            st.rerun()

    busy = bool(pend and pend.get("status") == "running") or _reply_running_for(doc["path"])

    # ---- 一次性提示 & 收尾处理 ----
    flash = st.session_state.pop("battle_flash", None)
    if flash:
        st.success(flash)
    if st.session_state.pop("battle_finalize", False):
        _prepare_preview(doc)  # 只生成预览，需用户审批后才落盘

    # ---- 写回预览：fixed 右侧抽屉（demo REVIEW PENDING），Approve 前文档不会被修改 ----
    preview = st.session_state.get("battle_preview")
    if preview and preview.get("doc_path") == doc["path"]:
        today = datetime.now().strftime("%Y-%m-%d")
        with st.container(border=True):
            st.markdown('<div class="kb-drawer-marker kb-drawer-preview"></div>',
                        unsafe_allow_html=True)
            st.markdown("<div class='drawer-title'>待审批</div>"
                        "<div class='drawer-sub'>写回预览——确认后才会落盘到知识库源文件</div>",
                        unsafe_allow_html=True)
            st.markdown("<div class='drawer-rule'></div>", unsafe_allow_html=True)
            st.markdown(_render_state_md(preview["data"], today)
                        + _render_log_md(preview["data"], today))
            st.markdown("<div class='drawer-rule'></div>"
                        "<div class='drawer-sub'>写回遵循「预览 → 人工确认」流程；"
                        "丢弃预览不影响对话，可继续聊或重新生成。</div>",
                        unsafe_allow_html=True)
            c_ok, c_no, _pad = st.columns([1.2, 1.2, 2])
            with c_ok:
                if st.button("确认写回", type="primary", use_container_width=True,
                             key="battle_wb_approve"):
                    _confirm_writeback(doc, on_saved)
            with c_no:
                if st.button("丢弃预览", use_container_width=True, key="battle_wb_reject",
                             help="丢弃预览，对话保留，可继续聊或重新生成"):
                    st.session_state.pop("battle_preview", None)
                    st.rerun()
    elif preview:
        st.session_state.pop("battle_preview", None)  # 预览属于别的文档，丢弃
        preview = None

    # ---- 三栏辩论室（demo §9）：THESIS CONTEXT / BATTLE ROOM / BATTLE STATE ----
    state = _parse_battle_state(doc.get("content", ""))
    user_rounds = sum(1 for m in msgs if m["role"] == "user")
    # round = 用户消息数+1；用户已发言等 AI 回复时停留在当前轮
    round_no = max(1, user_rounds + (0 if msgs and msgs[-1]["role"] == "user" else 1))

    left, center, right = st.columns([3.5, 6.0, 3.5], gap="medium")

    with left:
        st.markdown('<div class="battle-left-marker"></div>', unsafe_allow_html=True)
        # 选择变化经 widget 状态在下一轮 rerun 顶部生效（见上文「值先行」注释）
        st.selectbox("观点来源文档", labels, index=_cur_idx, key="battle_doc",
                     label_visibility="collapsed")
        st.markdown(_thesis_context_html(doc, state), unsafe_allow_html=True)

    with right:
        st.markdown('<div class="battle-right-marker"></div>', unsafe_allow_html=True)
        st.markdown(_battle_state_html(state), unsafe_allow_html=True)

    with center:
        st.markdown(
            "<div class='round-banner'>"
            f"<span class='round'>第 {round_no:02d} 轮</span>"
            f"<span class='phase'>{_current_phase(msgs)}</span>"
            f"<span class='phase-flow'>{_PHASE_FLOW}</span></div>",
            unsafe_allow_html=True)

        # ---- 对话区：限定高度的滚动框（CSS 见 app.py .battle-msgs-marker）——
        # 空态、消息流、流式打字机全在框内；框下方是吸底的操作区。
        # 标记放在容器【外】紧邻其前：CSS 用相邻兄弟选择器只打容器本身，
        # 若放容器内 :has 会同时命中容器与标记自身两层（标记行会抢 flex 空间） ----
        st.markdown('<div class="battle-msgs-marker"></div>', unsafe_allow_html=True)
        with st.container():

            # ---- 空态（demo）：红队已就位 + [让 AI 先开火] ----
            if not msgs and not busy:
                st.markdown("<div class='empty-state'>"
                            "<div class='e-title'>红队已就位</div>"
                            "<div class='e-sub'>让 AI 先开火，或直接发言陈述你的观点。"
                            "说「今天到这里」会生成写回预览，经你确认后才落盘。</div></div>",
                            unsafe_allow_html=True)
                if st.button("🔴 让 AI 先开火", type="primary", key="battle_first_fire"):
                    st.session_state.battle_pending = (
                        "请通读我的观点文档，然后从 Step 1 · Assumption Attack 开始，"
                        "攻击其中最脆弱的一到两个假设。"
                    )
                    st.rerun()

            # ---- 消息流：demo 研究记录样式（竖线 + AI/YOU 标签），非聊天气泡 ----
            if msgs:
                st.markdown("".join(_entry_html(i + 1, m["role"], m["content"])
                                    for i, m in enumerate(msgs)),
                            unsafe_allow_html=True)

            # ---- 后台回复状态：进行中打字机显示 / 中断 / 失败 ----
            _render_reply_live(doc["path"])  # 固定挂载，内部按状态决定是否渲染
            if pend and pend.get("status") == "running":
                if _pending_stale(doc["path"], pend):
                    st.warning("AI 回复被中断（应用重启或进程退出）。你的发言已保留，可重新发送。")
                    _remove_pending(doc["path"])
                    pend = None
                    busy = False
            elif pend and pend.get("status") == "error":
                # 不丢弃用户已发消息（已落盘），只报错，可重试
                st.error(f"调用 AI 失败：{pend.get('error', '未知错误')}。你的发言已保留，可重新发送或稍后再试。")
                _remove_pending(doc["path"])
                pend = None
                busy = False

        # ---- 操作行（quiet 按钮，demo 操作区样式） ----
        # 停靠标记：把下方操作区（按钮 + 输入框）吸附到中栏底部
        st.markdown('<div class="battle-dock-marker"></div>', unsafe_allow_html=True)
        c_next, c_end, c_break, c_clear = st.columns([2.2, 2.2, 1.4, 1.4])
        with c_next:
            if st.button("开始下一次攻击", type="primary", key="battle_next",
                         use_container_width=True,
                         disabled=busy or not msgs,
                         help="让红队按方法论推进下一轮攻击"):
                st.session_state.battle_pending = (
                    "继续：按方法论推进到下一步，针对我上一条回应发起下一轮攻击。"
                )
                st.rerun()
        with c_end:
            if st.button("结束今日论战并写回", key="battle_finalize_btn",
                         use_container_width=True,
                         disabled=not msgs or busy or bool(preview),
                         help="生成写回预览，经你确认后才写入文档"):
                st.session_state.battle_finalize = True
                st.rerun()
        with c_break:
            if st.button("中断", key="battle_break", use_container_width=True,
                         disabled=not msgs and not busy,
                         help="放弃本场对话：取消在跑的回复、清空对话，绝不写回文档"):
                _break_reply(doc["path"])
                st.rerun()
        with c_clear:
            if st.button("清空", key="battle_clear", use_container_width=True,
                         disabled=not msgs):
                st.session_state.battle_msgs = []
                _save_messages(doc["path"], [])
                _remove_pending(doc["path"])
                # 连同待审批的写回预览一并丢弃：对话已清空，留着旧预览会
                # 把已作废讨论的提取结果写回文档（与「中断」按钮同构）
                st.session_state.pop("battle_preview", None)
                st.session_state.pop("battle_finalize", None)
                st.rerun()

        queued = st.session_state.pop("battle_pending", None)
        prompt = st.chat_input("输入你的回应…（说「今天到这里」收工并生成写回预览）",
                               disabled=busy)
        text = prompt or queued
        if text:
            if busy:
                st.warning("AI 正在回复中，请等当前回复完成后再发言。")
                return
            msgs.append({"role": "user", "content": text})
            _save_messages(doc["path"], msgs)  # 发言立即落盘，刷新不丢
            _start_reply(doc, doc["path"])     # 后台线程生成回复，不在 rerun 内等待
            st.rerun()
