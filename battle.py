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
    """写回第二阶段：用户点「确认写回」后才真正落盘。"""
    preview = st.session_state.pop("battle_preview", None)
    if not preview or preview.get("doc_path") != doc["path"]:
        return
    try:
        path = _write_back(doc, preview["data"])
    except Exception as e:
        st.error(f"写回失败：{e}")
        return
    on_saved()
    st.session_state.battle_flash = (
        f"已更新 `{os.path.basename(path)}` 的「⚔️ Battle 记录」："
        f"状态表已刷新，当日日志已追加。"
    )
    st.rerun()


# ==================== 主视图 ====================

def render_battle(index, on_saved):
    st.markdown("<div class='doc-title'>⚔️ Thesis Battle</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='meta-line'>AI 扮演投委会红队，在限定方法论下攻击你的观点。"
        "不追求证明观点正确，而是寻找观点何时可能错误。</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<div style='margin-bottom:1rem'></div>", unsafe_allow_html=True)

    if not get_api_key():
        st.warning("未填入 API Key，AI 功能不可用："
                   "请先在左侧边栏「API 设置」处填入你自己的 key（sk-...）。")
        return

    # ---- 选择观点来源文档 ----
    docs = sorted(index.get("documents", []),
                  key=lambda d: (d.get("category_key") != "02_deals", d.get("category", ""), d["name"]))
    options = {f"{d.get('category_icon', '📁')} [{d.get('category', '其他')}] "
               f"{d.get('title') or d['name'].replace('.md', '')}": d for d in docs}
    labels = list(options.keys())
    if not labels:
        st.info("知识库还没有可 Battle 的文档：先到「📥 文件归档」归档一篇项目/行业文档，再回来。")
        return
    default_idx = 0
    for i, label in enumerate(labels):
        if options[label]["path"] == st.session_state.get("battle_doc_path"):
            default_idx = i
            break
    chosen = st.selectbox("观点来源文档", labels, index=default_idx, key="battle_doc")
    doc = options[chosen]

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

    # ---- 当前沉淀状态 ----
    cur = re.search(r"^## ⚔️ Battle 记录\n.*?(?=^## |\Z)", doc.get("content", ""),
                    re.DOTALL | re.MULTILINE)
    with st.expander("📊 当前沉淀状态与历史日志", expanded=False):
        if cur:
            st.markdown(cur.group(0))
        else:
            st.info("该文档尚无 Battle 记录，本场结束后将自动创建。")

    # ---- 写回预览（待审批）：确认前文档不会被修改 ----
    preview = st.session_state.get("battle_preview")
    if preview and preview.get("doc_path") == doc["path"]:
        today = datetime.now().strftime("%Y-%m-%d")
        with st.container(border=True):
            st.markdown("**📋 写回预览（待审批）—— 以下内容经你确认后才会写入文档**")
            st.markdown(_render_state_md(preview["data"], today)
                        + _render_log_md(preview["data"], today))
            c_ok, c_no, _ = st.columns([1, 1, 3])
            with c_ok:
                if st.button("✅ 确认写回", type="primary", use_container_width=True):
                    _confirm_writeback(doc, on_saved)
            with c_no:
                if st.button("❌ 取消", use_container_width=True,
                             help="丢弃预览，对话保留，可继续聊或重新生成"):
                    st.session_state.pop("battle_preview", None)
                    st.rerun()
    elif preview:
        st.session_state.pop("battle_preview", None)  # 预览属于别的文档，丢弃
        preview = None

    # ---- 操作行 ----
    col_end, col_break, col_clear, col_note = st.columns([2, 1, 1, 2])
    with col_end:
        if st.button("💾 结束今日 Battle 并写回文档", type="primary",
                     use_container_width=True,
                     disabled=not msgs or busy or bool(preview),
                     help="生成写回预览，经你确认后才写入文档"):
            st.session_state.battle_finalize = True
            st.rerun()
    with col_break:
        if st.button("🛑 中断", type="secondary", use_container_width=True,
                     disabled=not msgs and not busy,
                     help="放弃本场对话：取消在跑的回复、清空对话，绝不写回文档"):
            _break_reply(doc["path"])
            st.rerun()
    with col_clear:
        if st.button("🗑️ 清空对话", use_container_width=True, disabled=not msgs):
            st.session_state.battle_msgs = []
            _save_messages(doc["path"], [])
            _remove_pending(doc["path"])
            st.rerun()
    with col_note:
        st.markdown(
            "<div class='meta-line' style='margin-top:0.6rem'>说「今天到这里」会生成写回预览，"
            "经你确认后才落盘；「中断」放弃整场对话且绝不写回。</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    # ---- 对话区 ----
    if not msgs and not busy:
        st.info("红队已就位。点击下方按钮让 AI 先开火，或直接发言陈述你的观点。")
        if st.button("🔴 让 AI 先开火", type="primary"):
            st.session_state.battle_pending = (
                "请通读我的观点文档，然后从 Step 1 · Assumption Attack 开始，"
                "攻击其中最脆弱的一到两个假设。"
            )
            st.rerun()

    for m in msgs:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

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

    queued = st.session_state.pop("battle_pending", None)
    prompt = st.chat_input("为你的观点辩护、抛出新证据，或说「今天到这里」收工……",
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
