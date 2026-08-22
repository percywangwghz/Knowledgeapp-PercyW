# -*- coding: utf-8 -*-
"""中枢控制台数据层（只读 + 清除）。

扫描 data/ 下各功能模块的后台任务落盘文件（radar / tech / review / ingest /
battle pending），归一为统一的 job 结构供 app.py 控制台页渲染。

设计约束：
- 只读取/删除 job 文件，绝不写业务数据，不参与任务调度（调度仍在各模块内）；
- 文件损坏、不存在、字段缺失一律跳过或降级，不抛异常；
- "失联" 判定对齐各模块 _job_stale 惯例：status=running 但文件 mtime 超过
  STALE_SECS（10 分钟）未更新 → 视为中断（进程重启/崩溃后线程已死）。
"""
import json
import os
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
BATTLE_SESSION_DIR = os.path.join(DATA_DIR, "battle_sessions")

STALE_SECS = 600  # running 任务 10 分钟未更新 → 判定中断/失联

# 步骤状态 → 进度图标（done / running / pending / error）
_STEP_ICON = {"done": "✓", "running": "●", "error": "✕",
              "waiting": "○", "pending": "○", "": "○"}

_STAGE_LABEL = {"fetch": "抓取", "cognition": "认知"}


def _read_json(path):
    """读取 JSON 文件；不存在/损坏返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _is_stale(path, job):
    """status=running 但文件久未更新（进程已死）→ 判定中断。"""
    if not isinstance(job, dict) or job.get("status") != "running":
        return False
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return True
    return age > STALE_SECS


def _norm_status(raw, stale):
    if raw == "running":
        return "interrupted" if stale else "running"
    if raw in ("done", "error"):
        return raw
    return raw or "done"


def _norm_steps(steps, label_key=None, label_fn=None):
    """步骤列表归一：[{icon, label, status}]；label_fn 优先于 label_key。"""
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        st_ = str(s.get("status", ""))
        if label_fn:
            label = label_fn(s)
        else:
            label = str(s.get(label_key or "title", ""))
        out.append({"icon": _STEP_ICON.get(st_, "○"), "label": label,
                    "status": st_})
    return out


def _mkjob(key, feature, raw, path, steps, summary, extra_status=None):
    stale = _is_stale(path, raw)
    status = _norm_status(extra_status or raw.get("status", ""), stale)
    return {
        "key": key,
        "feature": feature,
        "status": status,
        "started": str(raw.get("started", "") or ""),
        "finished": str(raw.get("finished", "") or ""),
        "steps": steps,
        "summary": summary,
        "path": path,
        "stale": stale,
    }


# ==================== 各功能 job 归一 ====================

def _radar_job():
    path = os.path.join(DATA_DIR, "radar_job.json")
    j = _read_json(path)
    if not j:
        return None
    steps = _norm_steps(
        j.get("steps"),
        label_fn=lambda s: f"{_STAGE_LABEL.get(s.get('stage'), s.get('stage', ''))} · {s.get('theme', '')}")
    summary = ""
    sm = j.get("summary") or {}
    if j.get("status") == "done" and sm:
        total = sum((sm.get("results") or {}).values())
        n_vars = sum(c.get("variables", 0) for c in (sm.get("cognition") or {}).values())
        summary = f"新增信号 {total} 条 · 边际变量 {n_vars} 个"
    elif j.get("status") == "error":
        errs = sm.get("errors") or []
        summary = errs[0][:120] if errs else "任务失败"
    elif j.get("status") == "running":
        summary = f"模式 {j.get('mode', '')} · 进度实时落盘"
    return _mkjob("radar", "Radar 抓取", j, path, steps, summary)


def _tech_job():
    path = os.path.join(DATA_DIR, "tech_job.json")
    j = _read_json(path)
    if not j:
        return None
    steps = _norm_steps(j.get("steps"), label_key="title")
    merged = j.get("merged") or {}
    if j.get("status") == "done" and merged:
        summary = f"已合并 {merged.get('done', 0)} 篇进技术档案"
        if merged.get("errors"):
            summary += f" · {len(merged['errors'])} 篇失败"
    elif j.get("status") == "running":
        summary = f"阶段：{j.get('phase', 'extract')}"
    elif j.get("status") == "error":
        summary = str(j.get("error", "任务失败"))[:120]
    else:
        summary = ""
    return _mkjob("tech", "公众号技术提取", j, path, steps, summary)


def _review_job():
    path = os.path.join(DATA_DIR, "review_job.json")
    j = _read_json(path)
    if not j:
        return None
    proj = ""
    if j.get("key"):
        proj = os.path.basename(str(j["key"]).split("|")[0]).replace(".md", "")
    if j.get("status") == "done":
        summary = f"{proj} · 评审草稿已生成，等待人工确认" if proj else "评审草稿已生成"
    elif j.get("status") == "error":
        summary = f"{proj} · {str(j.get('error', '任务失败'))[:100]}".strip(" ·")
    else:
        summary = f"{proj} · AI 生成中…" if proj else "AI 生成中…"
    return _mkjob("review", "新项目评审", j, path, [], summary)


def _ingest_job(key, feature, filename):
    path = os.path.join(DATA_DIR, filename)
    j = _read_json(path)
    if not j:
        return None
    steps = _norm_steps(j.get("steps"), label_key="file")
    total = len(steps)
    done = sum(1 for s in steps if s["status"] in ("done", "error"))
    if j.get("status") == "running":
        summary = f"{done}/{total} 已处理"
    elif j.get("status") == "error":
        summary = str(j.get("error", "任务失败"))[:120]
    else:
        summary = f"{done}/{total} 完成"
    return _mkjob(key, feature, j, path, steps, summary)


def _battle_session_label(h):
    """由 pending 文件哈希反查 session 的文档名；查不到返回哈希前 6 位。"""
    sess = _read_json(os.path.join(BATTLE_SESSION_DIR, f"{h}.json"))
    if sess and sess.get("doc_path"):
        return os.path.basename(str(sess["doc_path"])).replace(".md", "")
    return h[:6]


def _battle_pending_jobs():
    out = []
    if not os.path.isdir(BATTLE_SESSION_DIR):
        return out
    try:
        names = os.listdir(BATTLE_SESSION_DIR)
    except OSError:
        return out
    for name in sorted(names):
        if not name.endswith(".pending.json"):
            continue
        path = os.path.join(BATTLE_SESSION_DIR, name)
        j = _read_json(path)
        if not j:
            continue
        h = name[:-len(".pending.json")]
        label = _battle_session_label(h)
        if j.get("cancel"):
            status = "interrupted"
        else:
            status = j.get("status", "")
        if status == "running":
            partial = str(j.get("partial", ""))
            summary = "AI 回复中… " + (partial[-60:] if partial else "")
        elif status == "error":
            summary = str(j.get("error", "任务失败"))[:120]
        else:
            summary = "回复已完成"
        job = _mkjob(f"battle_{h}", f"Thesis Battle · {label}", j, path, [],
                     summary, extra_status=status)
        out.append(job)
    return out


def list_jobs():
    """扫描全部 job 文件，归一为统一结构列表。
    排序：running → interrupted → error → done，同组按 started 倒序。"""
    jobs = []
    for fn in (_radar_job, _tech_job, _review_job,
               lambda: _ingest_job("ingest", "文件归档 · 分析", "ingest_job.json"),
               lambda: _ingest_job("ingest_save", "文件归档 · 入库", "ingest_save_job.json")):
        try:
            j = fn()
        except Exception:
            j = None
        if j:
            jobs.append(j)
    try:
        jobs.extend(_battle_pending_jobs())
    except Exception:
        pass
    order = {"running": 0, "interrupted": 1, "error": 2, "done": 3}
    jobs.sort(key=lambda j: (order.get(j["status"], 4), j["started"]), reverse=False)
    # 同组内 started 倒序：先按 started 倒序排，再按状态稳定排序
    jobs.sort(key=lambda j: j["started"], reverse=True)
    jobs.sort(key=lambda j: order.get(j["status"], 4))
    return jobs


def list_battle_sessions():
    """各 battle session 概况：[{hash, doc_path, doc_name, messages, updated,
    pending_status, pending_stale}]，按最后活动时间倒序。"""
    out = []
    if not os.path.isdir(BATTLE_SESSION_DIR):
        return out
    try:
        names = os.listdir(BATTLE_SESSION_DIR)
    except OSError:
        return out
    for name in sorted(names):
        if name.endswith(".pending.json") or not name.endswith(".json"):
            continue
        h = name[:-len(".json")]
        sess = _read_json(os.path.join(BATTLE_SESSION_DIR, name))
        if not sess:
            continue
        pend_path = os.path.join(BATTLE_SESSION_DIR, f"{h}.pending.json")
        pend = _read_json(pend_path)
        pend_status = ""
        pend_stale = False
        if pend:
            pend_status = pend.get("status", "")
            if pend.get("cancel"):
                pend_status = "interrupted"
            elif pend_status == "running" and _is_stale(pend_path, pend):
                pend_status = "interrupted"
                pend_stale = True
        doc_path = str(sess.get("doc_path", ""))
        out.append({
            "hash": h,
            "doc_path": doc_path,
            "doc_name": os.path.basename(doc_path).replace(".md", "") or h[:6],
            "messages": len(sess.get("messages") or []),
            "updated": str(sess.get("updated", "") or ""),
            "pending_status": pend_status,
            "pending_stale": pend_stale,
        })
    out.sort(key=lambda s: s["updated"], reverse=True)
    return out


def clear_job(key):
    """删除对应 job 落盘文件（清理中断/完成记录）。返回是否删除成功。
    battle_<hash> 只删 pending 文件，不动对话 session 本身。"""
    paths = {
        "radar": os.path.join(DATA_DIR, "radar_job.json"),
        "tech": os.path.join(DATA_DIR, "tech_job.json"),
        "review": os.path.join(DATA_DIR, "review_job.json"),
        "ingest": os.path.join(DATA_DIR, "ingest_job.json"),
        "ingest_save": os.path.join(DATA_DIR, "ingest_save_job.json"),
    }
    path = paths.get(key)
    if path is None and key.startswith("battle_"):
        h = key[len("battle_"):]
        path = os.path.join(BATTLE_SESSION_DIR, f"{h}.pending.json")
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False
