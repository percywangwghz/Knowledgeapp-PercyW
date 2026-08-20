# -*- coding: utf-8 -*-
"""输出侧回归：battle/review 打字机 partial 节流落盘 + review done 结果缓存（不触网）

覆盖的缺陷修复：
- battle._reply_worker / review._job_worker 的 on_chunk 曾每 chunk 全量落盘
  （SSE chunk 太密 → 磁盘狂写、Windows 句柄重试拖累 worker），现按
  PARTIAL_THROTTLE 节流，内存每 chunk 更新、落盘间隔受限、终态无条件落盘；
- review 主流程曾把 key 不匹配的 done 结果直接 _remove_job() 丢弃——生成中
  切换项目/行业选择即丢草稿；现保留到切回对应组合时落草稿。
"""
import json
import os
import sys
import tempfile
import threading
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 持久化 key 文件指向临时路径，避免读写本机真实 data/local_api_key.txt
KEY_FILE = os.path.join(tempfile.mkdtemp(prefix="kb_test_"), "local_api_key.txt")
os.environ["KB_LOCAL_KEY_FILE"] = KEY_FILE

import battle
import review

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK]   {name}")
    else:
        failures.append(name)
        print(f"[FAIL] {name} {detail}")


# ---------- battle._reply_worker：partial 节流落盘 + 终态完整落盘 ----------
battle.SESSION_DIR = tempfile.mkdtemp()
pend_writes = []
orig_pend_write = battle._write_pending


def _spy_pend(doc_path, state):
    pend_writes.append(json.loads(json.dumps(state, ensure_ascii=False)))  # 快照防原地变异
    return orig_pend_write(doc_path, state)


def fake_reply(doc, msgs, on_chunk=None):
    """模拟流式：触发 30 个 chunk 后返回完整回复。"""
    if on_chunk:
        for n in range(30):
            on_chunk(f"部分文本{n}")
    return "完整回复"


with mock.patch.object(battle, "_ai_reply", side_effect=fake_reply), \
     mock.patch.object(battle, "_write_pending", side_effect=_spy_pend):
    battle._reply_worker({"content": "x", "path": "a.md"}, [], "a.md",
                         threading.Event(), "test-key")
check("battle partial 节流落盘", 2 <= len(pend_writes) <= 5,
      f"writes={len(pend_writes)}（共 30 次 on_chunk）")
check("battle done 终态完整落盘",
      pend_writes and pend_writes[-1]["status"] == "done"
      and pend_writes[-1]["partial"] == "完整回复",
      f"last={pend_writes[-1] if pend_writes else None!r}")

# ---------- battle._reply_worker：中断后不写任何结果 ----------
pend_writes.clear()
cancel = threading.Event()


def fake_reply_cancel(doc, msgs, on_chunk=None):
    cancel.set()  # 回复到达前用户已点「中断」
    if on_chunk:
        on_chunk("部分文本")
    return "完整回复"


with mock.patch.object(battle, "_ai_reply", side_effect=fake_reply_cancel), \
     mock.patch.object(battle, "_write_pending", side_effect=_spy_pend):
    battle._reply_worker({"content": "x", "path": "b.md"}, [], "b.md", cancel, "test-key")
check("battle 中断后不写 done/error",
      all(w["status"] == "running" for w in pend_writes),
      f"writes={[w['status'] for w in pend_writes]}")

# ---------- review._job_worker：partial 节流落盘 + done 数据解析落盘 ----------
review.DATA_DIR = tempfile.mkdtemp()
review.JOB_FILE = os.path.join(review.DATA_DIR, "review_job.json")
job_writes = []
orig_job_write = review._write_job


def _spy_job(state):
    job_writes.append(json.loads(json.dumps(state, ensure_ascii=False)))
    return orig_job_write(state)


def fake_chat(messages, **kw):
    """模拟流式：触发 30 个 chunk 后返回可解析 JSON。"""
    cb = kw.get("on_chunk")
    if cb:
        for n in range(30):
            cb(f'{{"project_judgment": "片段{n}')
    return '{"project_judgment": "判断", "industry_doc_additions": "增量"}'


with mock.patch.object(review, "chat", side_effect=fake_chat), \
     mock.patch.object(review, "_write_job", side_effect=_spy_job):
    review._job_worker({"content": "p", "path": "p.md"},
                       {"content": "i", "path": "i.md"}, "p.md|i.md", "test-key")
check("review partial 节流落盘", 2 <= len(job_writes) <= 5,
      f"writes={len(job_writes)}（共 30 次 on_chunk）")
check("review done 终态解析落盘",
      job_writes and job_writes[-1]["status"] == "done"
      and (job_writes[-1]["data"] or {}).get("project_judgment") == "判断",
      f"last={job_writes[-1] if job_writes else None!r}")

# ---------- review 视图：done 结果的 key 缓存语义（AppTest 无头） ----------
from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kb_index.json")
if not os.path.exists(INDEX):
    import indexer
    indexer.build_index(force=True)
with open(INDEX, "r", encoding="utf-8") as f:
    index = json.load(f)

# 用真实 data 目录的 job 文件（视图按模块常量读写）；测试前备份、结束后恢复
review.DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
review.JOB_FILE = os.path.join(review.DATA_DIR, "review_job.json")
backup = None
if os.path.exists(review.JOB_FILE):
    with open(review.JOB_FILE, "r", encoding="utf-8") as f:
        backup = f.read()


def run_compare():
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["view_mode"] = "compare"
    at.session_state["user_api_key"] = "sk-test"
    at.run()
    if at.exception:
        failures.append("compare 视图运行异常")
        print(f"[FAIL] compare 视图运行异常: {at.exception[0].value}")
    return at


# 与 review.py 主流程完全同构地重建当前默认选中的「项目|行业」组合
deals = [d for d in index["documents"] if d.get("category_key") == "02_deals"]
industries = [d for d in index["documents"] if d.get("category_key") == "01_industry"]


def _title(d):
    return d.get("title") or d["name"].replace(".md", "")


pdoc = next(iter({f"[{d.get('track', '未分类')}] {_title(d)}": d for d in deals}.values()))
indoc = industries[review._default_industry_idx(pdoc, industries)]
match_key = review._job_key_of(pdoc, indoc)

try:
    # 4a. key 不匹配的 done 结果：不丢弃、不落草稿
    review._write_job({"status": "done", "key": "别的项目|别的行业",
                       "partial": "x", "data": {"project_judgment": "别组"},
                       "error": "", "started": "2026-08-19T00:00:00",
                       "finished": "2026-08-19T00:01:00"})
    at = run_compare()
    if not at.exception:
        check("key 不匹配 done 不丢弃", os.path.exists(review.JOB_FILE))
        check("key 不匹配不落草稿", "review_draft" not in at.session_state)

    # 4b. key 匹配的 done 结果：落草稿进 session_state 并消费 job 文件
    review._write_job({"status": "done", "key": match_key,
                       "partial": "x", "data": {"project_judgment": "判断",
                                                "industry_doc_additions": "增量"},
                       "error": "", "started": "2026-08-19T00:00:00",
                       "finished": "2026-08-19T00:01:00"})
    at = run_compare()
    if not at.exception:
        draft = at.session_state["review_draft"] if "review_draft" in at.session_state else None
        check("key 匹配落草稿",
              bool(draft) and draft["data"].get("project_judgment") == "判断"
              and draft.get("project_path") == pdoc["path"],
              f"draft={draft!r}")
        check("key 匹配消费 job 文件", not os.path.exists(review.JOB_FILE))
finally:
    if os.path.exists(review.JOB_FILE):
        os.remove(review.JOB_FILE)
    if backup is not None:
        with open(review.JOB_FILE, "w", encoding="utf-8") as f:
            f.write(backup)

print()
if failures:
    print(f"{len(failures)} test(s) failed")
    sys.exit(1)
print("ALL LIVE-OUTPUT TESTS OK")
