# -*- coding: utf-8 -*-
"""
Moonshot API 轻量客户端（供 Thesis Battle 等模块调用）。

API Key 解析顺序：
0. 当前线程经 set_thread_api_key 注入的 key（后台任务线程专用：
   线程内没有 Streamlit ScriptRunContext，取不到下面第 1 条的会话 key，
   需主线程启动任务前先取好再注入）
1. 若已调用 register_key_provider 注册提供者（前端 Streamlit 模式）：
   优先用提供者返回的 key（按会话注入）；
   返回 None → 回退到下面的默认来源（本机 localhost 自用场景）；
   返回空串/异常 → 视为无 key，功能不可用（外部访问场景，
   防止外发后访客走站主的 key）
2. CLI / 无提供者时：环境变量 MOONSHOT_API_KEY
3. ~/.kimi/config.toml 中 [providers."managed:moonshot-cn"] 的 api_key
   （即本机 Kimi CLI 已配置的 key，复用不落地）
"""
import json
import os
import re
import threading
import time
from datetime import datetime

import requests


class NoApiKeyError(RuntimeError):
    """未配置 API Key 时抛出。区别于普通查询失败：调用方不应把它吞掉
    伪装成「成功但无结果」，否则任务进度会勾稽不上（抓取 ✅+0 实为失败）。"""

DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
# 模型名可用环境变量 MOONSHOT_MODEL 覆盖（平台改名/换模型时不用改代码）
DEFAULT_MODEL = os.environ.get("MOONSHOT_MODEL", "kimi-k2.6")

# ==================== 花费记录 ====================
# Moonshot CN 平台计费（元 / 百万 tokens，输入/输出），官方调价时改这里
PRICING = {"kimi-k2.6": (4.0, 21.0)}
DEFAULT_PRICE = (4.0, 21.0)
COST_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ai_costs.jsonl")


def _log_cost(feature, model, usage, estimated):
    """把一次调用的 token 用量与估算费用追加到 data/ai_costs.jsonl。"""
    pin = int(usage.get("prompt_tokens") or 0)
    pout = int(usage.get("completion_tokens") or 0)
    pi, po = PRICING.get(model, DEFAULT_PRICE)
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "feature": feature,
        "model": model,
        "prompt_tokens": pin,
        "completion_tokens": pout,
        "cost": round(pin * pi / 1e6 + pout * po / 1e6, 4),
        "estimated": bool(estimated),
    }
    try:
        os.makedirs(os.path.dirname(COST_LOG), exist_ok=True)
        with open(COST_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _est_usage(messages, content):
    """API 未返回 usage 时粗估：中英文混合按 ~2 字符/token。"""
    pin = sum(len(str(m.get("content") or "")) for m in messages) // 2
    return {"prompt_tokens": pin, "completion_tokens": len(content or "") // 2}


def _cfg_from_kimi_config():
    """读 ~/.kimi/config.toml 的 moonshot-cn provider，返回 (api_key, base_url)。"""
    cfg = os.path.join(os.path.expanduser("~"), ".kimi", "config.toml")
    if not os.path.exists(cfg):
        return "", ""
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "", ""
    m = re.search(r'\[providers\."managed:moonshot-cn"\](.*?)(?=\n\[)', text, re.DOTALL)
    if not m:
        return "", ""
    km = re.search(r'api_key\s*=\s*"([^"]+)"', m.group(1))
    bm = re.search(r'base_url\s*=\s*"([^"]+)"', m.group(1))
    return (km.group(1) if km else ""), (bm.group(1) if bm else "")


_key_provider = None
_thread_key = threading.local()  # 线程级 key：后台任务线程用（见 set_thread_api_key）


def register_key_provider(fn):
    """注册回调 fn() -> str，返回当前上下文应使用的 API Key。
    供前端按会话注入用户自己的 key（如 Streamlit session_state）；
    返回空串或抛异常时回退到默认解析。传 None 可注销。"""
    global _key_provider
    _key_provider = fn


def set_thread_api_key(key):
    """为当前线程固定 API Key。后台线程（抓取/评审/归档/对话任务）内没有
    Streamlit ScriptRunContext，session_state 注入的 key 取不到——
    需在主线程启动线程前 get_api_key() 取好，工作线程开头调本函数注入。
    传 None 恢复默认解析。"""
    _thread_key.api_key = key


def get_api_key():
    key = getattr(_thread_key, "api_key", None)
    if key is not None:
        return key.strip()
    if _key_provider is not None:
        # 前端模式（Streamlit 已注册提供者）：优先用前端注入的 key。
        # 提供者返回 None → 放行，回退到本机默认来源（本机自用免填 key）；
        # 返回空串 → 明确无 key，功能不可用（外部访问必须显式填 key，
        # 防止外发后访客悄悄走站主机器上的默认 key 计费）
        try:
            key = _key_provider()
        except Exception:
            return ""
        if key is not None:
            return key.strip()
    return os.environ.get("MOONSHOT_API_KEY", "").strip() or _cfg_from_kimi_config()[0]


def get_base_url():
    """API 端点：env MOONSHOT_BASE_URL > ~/.kimi/config.toml provider base_url > 默认。
    sk-kimi- 开头的 key 走 https://api.kimi.com/coding/v1；platform.moonshot.cn 的
    sk- key 走 api.moonshot.cn。在 config.toml 的 provider 里配 base_url 即可切换。"""
    return (os.environ.get("MOONSHOT_BASE_URL", "").strip()
            or _cfg_from_kimi_config()[1] or DEFAULT_BASE_URL)


WEB_SEARCH_TOOLS = [{"type": "builtin_function", "function": {"name": "$web_search"}}]


def _post_stream(payload, key, timeout=300, on_chunk=None):
    """以 stream=True 发起请求并解析 SSE，返回 (message dict, usage dict)。
    message 结构同非流式 choices[0].message：content / tool_calls。
    usage 来自 stream_options=include_usage 的末尾 chunk，可能为 None。
    流式下 timeout 作用于首 chunk 及相邻 chunk 之间，不再整请求挂死。
    on_chunk 为可选回调：每个 content chunk 到达时以截至目前的累积文本调用。"""
    resp = requests.post(
        f"{get_base_url()}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=dict(payload, stream=True, stream_options={"include_usage": True}),
        timeout=timeout,
        stream=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Moonshot API 错误 {resp.status_code}：{resp.text[:300]}")
    resp.encoding = "utf-8"
    content_parts = []
    reasoning_parts = []
    tool_calls = {}  # index -> 累积中的 tool_call 片段
    usage = None
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            if delta.get("content"):
                content_parts.append(delta["content"])
                if on_chunk:
                    on_chunk("".join(content_parts))
            for tc in delta.get("tool_calls") or []:
                slot = tool_calls.setdefault(
                    tc.get("index", 0),
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
    msg = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        # 思考模型的推理链必须原样带回下一轮请求（尤其 $web_search 的服务端结果注入
        # 依赖它），否则模型在工具轮后拿不到搜索结果
        msg["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return msg, usage


def _post(payload, key, timeout=300):
    """非流式请求，返回 (message dict, usage dict)。
    注意：$web_search 的服务端结果注入只在非流式下生效——实测 stream=True 时
    下一轮请求的 prompt 中不含搜索结果（注入丢失），因此带工具的调用必须走这里。"""
    resp = requests.post(
        f"{get_base_url()}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Moonshot API 错误 {resp.status_code}：{resp.text[:300]}")
    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {"role": "assistant", "content": ""}
    return msg, data.get("usage")


def chat_with_search(messages, model=DEFAULT_MODEL, max_tokens=8000, max_rounds=3,
                     feature="unknown", thinking=None):
    """带 $web_search 内置工具的对话：模型发起 tool_call → arguments 原样回传
    （服务端执行搜索），最多 max_rounds 轮，返回最终文本。
    内部走非流式：实测 stream=True 时 Moonshot 服务端不注入 $web_search 结果，
    模型只能编造或返回空。messages 为 openai 格式列表；temperature 不传（kimi-k2.6 只允许默认值 1）。
    thinking=True/False 显式开关 k2.6 思考（None 用模型默认）。注意：实测关思考后
    复杂提示词下模型会在正文里"表演"搜索（幻觉 $web_search）而非发起真实 tool_call，
    生产请保持 True/None；False 仅供调试用。
    feature 用于花费记录（写入 data/ai_costs.jsonl）。"""
    key = get_api_key()
    if not key:
        raise NoApiKeyError(
            "未找到 Moonshot API Key：前端请先在侧边栏「API Key」处填入你自己的 key；"
            "CLI 请设置环境变量 MOONSHOT_API_KEY，"
            "或确保 ~/.kimi/config.toml 已配置 moonshot-cn provider。"
        )
    msgs = list(messages)
    msg = {}
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    got_usage = False
    for _ in range(max_rounds):
        payload = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "tools": WEB_SEARCH_TOOLS,
        }
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        usage = None
        for attempt in range(3):  # 搜索轮服务端较慢，超时/断连退避重试
            try:
                msg, usage = _post(payload, key, timeout=900)
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        if usage:
            got_usage = True
            usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break
        msgs.append(msg)
        for tc in tool_calls:
            msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tc.get("function", {}).get("name", "$web_search"),
                "content": tc.get("function", {}).get("arguments") or "",
            })
    # 兜底：轮次耗尽或空输出时，去掉工具强制要答案；仍空则催一次再要
    for nudge in (None, "请直接输出最终 JSON 数组，不要任何解释。"):
        if (msg.get("content") or "").strip():
            break
        if nudge is not None:
            msgs.append({"role": "user", "content": nudge})
        payload = {"model": model, "messages": msgs, "max_tokens": max_tokens}
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        msg, usage = _post(payload, key, timeout=900)
        if usage:
            got_usage = True
            usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    content = msg.get("content") or ""
    _log_cost(feature, model,
              usage_total if got_usage else _est_usage(msgs, content), not got_usage)
    return content


def chat(messages, model=DEFAULT_MODEL, temperature=None, max_tokens=4096, on_chunk=None,
         feature="unknown"):
    """发送对话，返回 assistant 文本。messages 为 openai 格式列表。
    temperature 为 None 时不传（kimi-k2.6 只允许默认值 1，显式传参会报 400）。
    on_chunk 为可选回调：每个 SSE chunk 到达时以截至目前的累积文本调用
    （用于打字机式部分回复显示）；不传则行为不变。
    feature 用于花费记录（写入 data/ai_costs.jsonl）。"""
    key = get_api_key()
    if not key:
        raise NoApiKeyError(
            "未找到 Moonshot API Key：前端请先在侧边栏「API Key」处填入你自己的 key；"
            "CLI 请设置环境变量 MOONSHOT_API_KEY，"
            "或确保 ~/.kimi/config.toml 已配置 moonshot-cn provider。"
        )
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    msg = {}
    usage = None
    for attempt in range(3):  # 超时/断连退避重试（5s/10s），与 chat_with_search 一致
        try:
            msg, usage = _post_stream(payload, key, timeout=300, on_chunk=on_chunk)
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    content = msg.get("content") or ""
    _log_cost(feature, model, usage or _est_usage(messages, content), usage is None)
    return content
