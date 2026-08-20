# -*- coding: utf-8 -*-
"""方案二（雷达搜索去 Kimi 化）冒烟测试：get_search_payload 正反用例 +
chat_with_search 的厂家门控（全 mock，不发起真实请求）。直接 python test_search.py 运行。"""
import os
import sys
from unittest import mock

import llm

failures = []


def check(name, cond, extra=""):
    print(("[OK]  " if cond else "[FAIL]") + f" {name}" + (f" | {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(name)


# 固定厂家识别环境：env base_url 优先，逐个厂家切换
P = {}
from config import PROVIDERS
P = PROVIDERS

def payload_for(pid, model=None):
    p = P[pid]
    return llm.get_search_payload(model=model or p["default_model"],
                                  base_url=p["base_url"])

# ---- get_search_payload 正例（按 base_url 精确匹配）----
moon = payload_for("moonshot")
check("moonshot 平移 $web_search",
      moon == {"tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}]})
check("zhipu web_search enable",
      payload_for("zhipu") == {"tools": [{"type": "web_search", "web_search": {"enable": True}}]})
check("doubao web_search 插件", payload_for("doubao") == {"tools": [{"type": "web_search"}]})
check("dashscope enable_search", payload_for("dashscope") == {"enable_search": True})
check("openai web_search_options", payload_for("openai") == {"web_search_options": {}})
check("perplexity 空 dict（内置搜索）", payload_for("perplexity") == {})

# ---- get_search_payload 反例（search=None）----
proxy = "https://proxy.example.com/v1"
for pid in ("deepseek", "gemini", "xai"):
    check(f"{pid} 无搜索 → None", payload_for(pid) is None)
# anthropic 无官方端点（base_url=""）：模拟中转场景，模型前缀识别为 anthropic → None
check("anthropic 无搜索 → None",
      llm.get_search_payload(model="claude-sonnet-4-5", base_url=proxy) is None)
check("custom/未收录 base_url → None",
      llm.get_search_payload(model="some-random-model", base_url=proxy) is None)

# ---- 模型名前缀辅助识别（中转端点，base_url 匹配不上）----
check("前缀 glm- → zhipu", llm.get_search_payload(model="glm-4.6", base_url=proxy) ==
      {"tools": [{"type": "web_search", "web_search": {"enable": True}}]})
check("前缀 kimi- → moonshot",
      llm.get_search_payload(model="kimi-k2.6", base_url=proxy) ==
      {"tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}]})
check("前缀 qwen → dashscope",
      llm.get_search_payload(model="qwen-plus", base_url=proxy) == {"enable_search": True})
check("前缀 deepseek- → None", llm.get_search_payload(model="deepseek-chat", base_url=proxy) is None)

# ---- chat_with_search：search=None 厂家抛 SearchNotSupportedError ----
with mock.patch.object(llm, "get_api_key", return_value="sk-test"), \
     mock.patch.object(llm, "_log_cost", lambda *a, **k: None):
    # 模拟 UI 选了 DeepSeek：env 注入 deepseek 端点（base_url 精确匹配优先于模型前缀）
    os.environ["KB_BASE_URL"] = P["deepseek"]["base_url"]
    try:
        llm.chat_with_search([{"role": "user", "content": "hi"}], model="deepseek-chat")
        check("deepseek 抛 SearchNotSupportedError", False, "未抛异常")
    except llm.SearchNotSupportedError as e:
        check("deepseek 抛 SearchNotSupportedError",
              "DeepSeek" in str(e) and "未提供联网搜索功能，雷达不可用" in str(e), str(e))
    finally:
        del os.environ["KB_BASE_URL"]

    # 未收录/自定义厂家
    os.environ["KB_BASE_URL"] = proxy
    try:
        llm.chat_with_search([{"role": "user", "content": "hi"}], model="mystery-1")
        check("custom 抛 SearchNotSupportedError", False, "未抛异常")
    except llm.SearchNotSupportedError as e:
        check("custom 抛 SearchNotSupportedError", "未收录/自定义厂家" in str(e), str(e))
    finally:
        del os.environ["KB_BASE_URL"]

# ---- kimi 路径：请求体仍含 $web_search，thinking 门控，多轮回传 ----
calls = []

def fake_post_kimi(payload, key, timeout=300):
    calls.append(payload)
    if len(calls) == 1:
        # 第一轮：模型发起 $web_search tool_call
        return ({"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "$web_search",
                                              "arguments": '{"query":"x"}'}}]},
                {"prompt_tokens": 1, "completion_tokens": 1})
    return {"role": "assistant", "content": "[]"}, {"prompt_tokens": 1, "completion_tokens": 1}

with mock.patch.object(llm, "get_api_key", return_value="sk-test"), \
     mock.patch.object(llm, "_log_cost", lambda *a, **k: None), \
     mock.patch.object(llm, "_post", fake_post_kimi):
    out = llm.chat_with_search([{"role": "user", "content": "hi"}], model="kimi-k2.6",
                               thinking=True)
    check("kimi 返回终稿", out == "[]")
    check("kimi 首请求含 $web_search tools",
          calls[0].get("tools") == [{"type": "builtin_function",
                                     "function": {"name": "$web_search"}}])
    check("kimi thinking 注入", calls[0].get("thinking") == {"type": "enabled"})
    check("kimi 多轮回传 tool_call arguments", len(calls) == 2 and
          calls[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1",
                                       "name": "$web_search", "content": '{"query":"x"}'})
    check("kimi 不传 temperature", "temperature" not in calls[0])

# ---- 非 kimi 路径（dashscope）：enable_search 单轮、无 thinking、忽略 tool_calls ----
calls2 = []

def fake_post_ds(payload, key, timeout=300):
    calls2.append(payload)
    # 即便异常返回 tool_calls，也不应触发多轮回传
    return ({"role": "assistant", "content": "[]",
             "tool_calls": [{"id": "c", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]},
            {"prompt_tokens": 1, "completion_tokens": 1})

os.environ["KB_BASE_URL"] = P["dashscope"]["base_url"]
try:
    with mock.patch.object(llm, "get_api_key", return_value="sk-test"), \
         mock.patch.object(llm, "_log_cost", lambda *a, **k: None), \
         mock.patch.object(llm, "_post", fake_post_ds):
        out = llm.chat_with_search([{"role": "user", "content": "hi"}], model="qwen-vl-max",
                                   thinking=True)
        check("dashscope 返回终稿", out == "[]")
        check("dashscope 请求体含 enable_search", calls2[0].get("enable_search") is True)
        check("dashscope 不注入 thinking", "thinking" not in calls2[0])
        check("dashscope 单轮（tool_calls 不回传）", len(calls2) == 1)
        check("dashscope 不传 temperature", "temperature" not in calls2[0])
finally:
    del os.environ["KB_BASE_URL"]

# ---- radar_auto：SearchNotSupportedError 不被吞掉 ----
import radar_auto
with mock.patch.object(radar_auto, "chat_with_search",
                       side_effect=llm.SearchNotSupportedError("当前 API 厂家（DeepSeek）未提供联网搜索功能，雷达不可用。")):
    try:
        radar_auto._fetch_one_query("t", "q")
        check("radar_auto._fetch_one_query 冒泡 SearchNotSupportedError", False, "被吞掉")
    except llm.SearchNotSupportedError:
        check("radar_auto._fetch_one_query 冒泡 SearchNotSupportedError", True)

print()
if failures:
    print(f"{len(failures)} test(s) failed: {failures}")
    sys.exit(1)
print("ALL SEARCH SMOKE TESTS OK")
