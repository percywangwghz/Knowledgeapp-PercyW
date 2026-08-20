# -*- coding: utf-8 -*-
"""线程注入回归：并行池线程内的 API key/模型/端点必须与外层线程一致（不触网）

覆盖的缺陷修复：
- ingest.organize_document 分段并行（ThreadPoolExecutor）：池线程拿不到外层
  线程的 threading.local 注入，长文档（>TEXT_LIMIT 触发分段）池内 chat 一律
  NoApiKeyError——前端明明填了 key 却报「未找到 API Key」；
- radar_auto._with_key 只注入 key/model、漏了 base_url：自定义/非默认厂家的
  主题并行抓取会错落到默认 Moonshot 端点（接口 404 / 鉴权失败）。
"""
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ingest
import llm
import radar_auto

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK]   {name}")
    else:
        failures.append(name)
        print(f"[FAIL] {name} {detail}")


# 隔离环境影响：env 里有 key/端口时池线程无注入也能解析出值，测试会失真
_SAVED_ENV = {}
for _v in ("MOONSHOT_API_KEY", "KB_BASE_URL", "MOONSHOT_BASE_URL",
           "KB_MODEL", "MOONSHOT_MODEL", "KB_READ_KIMI_CONFIG"):
    _SAVED_ENV[_v] = os.environ.pop(_v, None)


def _restore_env():
    for k, v in _SAVED_ENV.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


EXPECTED = ("pool-key", "m-x", "https://custom.example/v1")


def _set_main_thread():
    llm.set_thread_api_key(EXPECTED[0])
    llm.set_thread_model(EXPECTED[1])
    llm.set_thread_base_url(EXPECTED[2])


def _clear_main_thread():
    llm.set_thread_api_key(None)
    llm.set_thread_model(None)
    llm.set_thread_base_url(None)


try:
    # ---------- ingest.organize_document：分段并行池线程注入 ----------
    _set_main_thread()
    seen = []

    def fake_chat(messages, **kw):
        # chat 在池线程内被调：记录调用当下解析到的 key/模型/端点
        seen.append((llm.get_api_key(), llm.get_model(), llm.get_base_url()))
        return "整理后内容"

    long_text = "\n".join(f"## 第{n}节\n" + "正文" * 80 for n in range(3))  # 触发多段
    orig_limit = ingest.TEXT_LIMIT
    ingest.TEXT_LIMIT = 100  # 小 limit 强制分段并行
    try:
        with mock.patch.object(ingest, "chat", side_effect=fake_chat):
            out = ingest.organize_document("t.md", long_text, "")
    finally:
        ingest.TEXT_LIMIT = orig_limit
    check("ingest 分段并行实际走池（≥2 次 chat）", len(seen) >= 2, f"calls={len(seen)}")
    check("ingest 池线程 key/模型/端点与外层一致",
          bool(seen) and all(s == EXPECTED for s in seen),
          f"seen={seen[:3]}")
    check("ingest 分段整理仍产出内容", "整理后内容" in out.get("content", ""))

    # ---------- radar_auto.run：主题并行池注入端点 ----------
    radar_auto.DATA_DIR = tempfile.mkdtemp()  # 日志/信号池读写引向临时目录
    seen2 = []

    def fake_fetch(theme, query, focus="", thinking=True):
        seen2.append((llm.get_api_key(), llm.get_base_url()))
        return []

    watchlist = [{"theme": "T1", "query": "q", "focus": "", "enabled": True, "primary": True}]
    with mock.patch.object(radar_auto, "load_watchlist", return_value=watchlist), \
         mock.patch.object(radar_auto, "fetch_theme", side_effect=fake_fetch):
        radar_auto.run(primary_only=True, dry_run=True, do_cognition=False)
    check("radar_auto 池线程 key/端点与外层一致",
          bool(seen2) and all(s == (EXPECTED[0], EXPECTED[2]) for s in seen2),
          f"seen={seen2[:3]}")
finally:
    _clear_main_thread()
    _restore_env()

print()
if failures:
    print(f"{len(failures)} test(s) failed")
    sys.exit(1)
print("ALL THREAD-INJECT TESTS OK")
