# -*- coding: utf-8 -*-
"""
OpenAI 兼容 API 轻量客户端（供 Thesis Battle 等模块调用，厂家可配）。

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
（除以上两条外无其他来源：不读任何本机 CLI 配置，key 的唯一来源
  就是用户显式填写或显式配置的环境变量）

模型名（get_model）与端点（get_base_url）走同样的注入链：
线程注入（set_thread_model）→ 前端 provider（register_model_provider /
register_base_url_provider）→ env（KB_MODEL/KB_BASE_URL，兼容 MOONSHOT_* 旧名）
→ 当前厂家预设（config.PROVIDERS，按 base_url 反查）→ 内置 Moonshot 默认兜底。

联网搜索（chat_with_search）：参数片段直读 config.PROVIDERS 的 search 字段
（get_search_payload 按 base_url/模型名反查厂家），厂家无搜索能力时抛
SearchNotSupportedError，雷达页捕获后明确提示，不静默降级。
"""
import copy
import json
import os
import threading
import time
from datetime import datetime

import requests


class NoApiKeyError(RuntimeError):
    """未配置 API Key 时抛出。区别于普通查询失败：调用方不应把它吞掉
    伪装成「成功但无结果」，否则任务进度会勾稽不上（抓取 ✅+0 实为失败）。"""


class SearchNotSupportedError(RuntimeError):
    """当前 API 厂家无服务端联网搜索能力时抛出（雷达专用）。
    与 NoApiKeyError 同理：全局性配置失败，每个子查询都会同样失败，
    调用方不应吞掉伪装成「+0 成功」。"""

DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
# 模型名可用环境变量 MOONSHOT_MODEL 覆盖（平台改名/换模型时不用改代码）
DEFAULT_MODEL = os.environ.get("MOONSHOT_MODEL", "kimi-k2.6")

# ==================== 花费记录 ====================
# 计费价目（元 / 百万 tokens，输入/输出）。kimi-k2.6 为官方定价；
# 其余为 2026-08 官方页约值，只用于花费估算，调价以官方为准
PRICING = {
    "kimi-k2.6": (4.0, 21.0),
    "deepseek-chat": (2.0, 8.0),
    "glm-4-plus": (50.0, 50.0),
    "qwen-plus": (0.8, 2.0),
    "gpt-4o": (18.0, 72.0),
    "gemini-2.5-flash": (2.2, 18.0),
}
COST_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ai_costs.jsonl")


def _log_cost(feature, model, usage, estimated):
    """把一次调用的 token 用量与估算费用追加到 data/ai_costs.jsonl。"""
    pin = int(usage.get("prompt_tokens") or 0)
    pout = int(usage.get("completion_tokens") or 0)
    price = PRICING.get(model)
    if price is None:
        # 未收录的模型成本记 0，不崩；需要计费时在 PRICING 补充价目
        print(f"[WARN] PRICING 未收录模型 {model} 的价格，本次成本记 0")
        price = (0.0, 0.0)
    pi, po = price
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


_key_provider = None
_model_provider = None
_base_url_provider = None
_thread_key = threading.local()    # 线程级 key：后台任务线程用（见 set_thread_api_key）
_thread_model = threading.local()  # 线程级模型：同上（见 set_thread_model）
_thread_base_url = threading.local()  # 线程级端点：同上（见 set_thread_base_url）
_thread_session = threading.local()   # 线程级 HTTP 连接池（见 _http_session）


def register_key_provider(fn):
    """注册回调 fn() -> str，返回当前上下文应使用的 API Key。
    供前端按会话注入用户自己的 key（如 Streamlit session_state）；
    返回空串或抛异常时回退到默认解析。传 None 可注销。"""
    global _key_provider
    _key_provider = fn


def register_model_provider(fn):
    """注册回调 fn() -> str，返回当前上下文应使用的模型名，语义镜像 register_key_provider：
    返回 None → 放行回退默认解析；返回空串/异常 → 明确未配置，跳过 env
    直接用厂家预设默认模型（防止外发访客走站主 env 里的模型）。传 None 可注销。"""
    global _model_provider
    _model_provider = fn


def register_base_url_provider(fn):
    """注册回调 fn() -> str，返回当前上下文应使用的 API 端点。
    供前端按厂家选择注入预设/自定义 base_url；返回空 → 回退默认解析。
    传 None 可注销。"""
    global _base_url_provider
    _base_url_provider = fn


def set_thread_api_key(key):
    """为当前线程固定 API Key。后台线程（抓取/评审/归档/对话任务）内没有
    Streamlit ScriptRunContext，session_state 注入的 key 取不到——
    需在主线程启动线程前 get_api_key() 取好，工作线程开头调本函数注入。
    传 None 恢复默认解析。"""
    _thread_key.api_key = key


def set_thread_model(model):
    """为当前线程固定模型名，用法同 set_thread_api_key：主线程启动线程前
    get_model() 取好，工作线程开头调本函数注入。传 None 恢复默认解析。"""
    _thread_model.model = model


def set_thread_base_url(url):
    """为当前线程固定 API 端点，用法同 set_thread_api_key：主线程启动线程前
    get_base_url() 取好，工作线程开头调本函数注入。
    缺少本注入时，后台线程里的 get_base_url() 会触发 base_url provider
    去读 st.session_state（无 ScriptRunContext，刷警告），且自定义端点/
    非默认厂家会错落到默认端点。传 None 恢复默认解析。"""
    _thread_base_url.base_url = url


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
    key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if key:
        return key
    return ""


def get_base_url():
    """API 端点解析顺序：线程注入（set_thread_base_url，后台任务线程专用）
    > env KB_BASE_URL > env MOONSHOT_BASE_URL（兼容旧名）
    > 前端 register_base_url_provider 注入（厂家预设/自定义）> 默认 Moonshot。
    不读任何本机 CLI 配置；sk-kimi- 开头的 key 走 https://api.kimi.com/coding/v1，
    platform.moonshot.cn 的 sk- key 走 api.moonshot.cn。"""
    thread_url = getattr(_thread_base_url, "base_url", None)
    if thread_url:
        return thread_url.strip()
    url = (os.environ.get("KB_BASE_URL", "").strip()
           or os.environ.get("MOONSHOT_BASE_URL", "").strip())
    if url:
        return url
    if _base_url_provider is not None:
        # 前端模式：厂家下拉选定后由提供者给出端点；返回空 → 继续回退
        try:
            url = _base_url_provider()
        except Exception:
            url = None
        if url:
            return url.strip()
    return DEFAULT_BASE_URL


def _preset_default_model():
    """按当前 base_url 反查 config.PROVIDERS 预设的默认模型；查不到返回空串。
    （厂家信息的唯一事实源在 config.PROVIDERS，此处只做反查）"""
    try:
        from config import PROVIDERS
    except ImportError:
        return ""
    cur = get_base_url().rstrip("/")
    for p in PROVIDERS.values():
        if p.get("default_model") and (p.get("base_url") or "").rstrip("/") == cur:
            return p["default_model"]
    return ""


def get_model():
    """模型名解析顺序（镜像 get_api_key）：
    线程注入（set_thread_model）→ 前端 provider → env KB_MODEL
    → env MOONSHOT_MODEL（兼容旧名）→ 当前厂家预设 default_model → DEFAULT_MODEL 兜底。"""
    model = getattr(_thread_model, "model", None)
    if model:
        return model.strip()
    if _model_provider is not None:
        # 语义同 key 提供者：None → 放行回退；空串/异常 → 明确未配置，
        # 跳过 env（防止外发访客走站主 env 模型），直接落厂家预设默认
        try:
            model = _model_provider()
        except Exception:
            model = ""
        if model:
            return model.strip()
        if model == "":
            return _preset_default_model() or DEFAULT_MODEL
    return (os.environ.get("KB_MODEL", "").strip()
            or os.environ.get("MOONSHOT_MODEL", "").strip()
            or _preset_default_model() or DEFAULT_MODEL)


# 模型名前缀 → PROVIDERS 键：base_url 精确匹配不上时的尽力辅助识别
# （中转/代理端点场景）；匹配不上视为无搜索能力，不乱猜参数格式
_SEARCH_MODEL_PREFIXES = [
    (("kimi-", "moonshot-"), "moonshot"),
    (("glm-", "chatglm"), "zhipu"),
    (("doubao-", "ep-"), "doubao"),
    (("qwen", "qwq-"), "dashscope"),
    (("gpt-", "chatgpt-", "o1", "o3", "o4"), "openai"),
    (("gemini-",), "gemini"),
    (("claude-",), "anthropic"),
    (("grok-",), "xai"),
    (("sonar",), "perplexity"),
    (("deepseek-",), "deepseek"),
]


def _match_provider(model, base_url):
    """反查 config.PROVIDERS：先按 base_url 精确匹配（厂家信息单一事实源，
    用户在 UI 选厂家时 base_url 即识别结果），匹配不上再用模型名前缀尽力辅助。
    返回 (provider_id, preset)；未收录返回 (None, None)。"""
    try:
        from config import PROVIDERS
    except ImportError:
        return None, None
    cur = (base_url or "").rstrip("/")
    if cur:
        for pid, p in PROVIDERS.items():
            if p.get("base_url") and p["base_url"].rstrip("/") == cur:
                return pid, p
    m = (model or "").lower()
    if m:
        for prefixes, pid in _SEARCH_MODEL_PREFIXES:
            if any(m.startswith(px) for px in prefixes):
                return pid, PROVIDERS.get(pid)
    return None, None


def get_search_payload(model=None, base_url=None):
    """返回当前厂家开启原生联网搜索的最小请求参数片段（调用方原样合并进
    chat/completions 请求体）。model/base_url 未显式指定时走注入链解析；
    厂家未收录（custom）或无搜索能力（search=None）时返回 None。
    片段数据全部来自 config.PROVIDERS（各厂官方文档，来源见 config 注释）。"""
    model = model or get_model()
    base_url = base_url or get_base_url()
    _, preset = _match_provider(model, base_url)
    if not preset:
        return None
    search = preset.get("search")
    return copy.deepcopy(search) if search is not None else None


def _http_session():
    """每线程一个 requests.Session：连接池复用 TCP/TLS 长连接，省去每轮调用的
    握手开销（每次约 0.3-1s，分段整理/多轮搜索这类连续调用收益最大）。
    Session 非线程安全，故按线程隔离（threading.local）：主线程、各后台 worker、
    并行整理线程各自持有一条连接。"""
    s = getattr(_thread_session, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_session.session = s
    return s


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}  # 限流与服务端抖动：可退避重试


def _retry_wait(resp, attempt):
    """退避秒数：优先服务端的 Retry-After（封顶 30s），否则 3s/6s 递增。"""
    ra = (resp.headers.get("Retry-After") or "").strip()
    if ra:
        try:
            return min(float(ra), 30.0)
        except ValueError:
            pass
    return 3.0 * (attempt + 1)


def _post_stream(payload, key, timeout=300, on_chunk=None):
    """以 stream=True 发起请求并解析 SSE，返回 (message dict, usage dict)。
    message 结构同非流式 choices[0].message：content / tool_calls。
    usage 来自 stream_options=include_usage 的末尾 chunk，可能为 None。
    流式下 timeout 作用于首 chunk 及相邻 chunk 之间，不再整请求挂死。
    on_chunk 为可选回调：每个 content chunk 到达时以截至目前的累积文本调用。"""
    is_kimi = str(payload.get("model", "")).startswith("kimi-")
    for attempt in range(3):  # 限流/服务端抖动退避重试（并行整理时 429 常见）
        resp = _http_session().post(
            f"{get_base_url()}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=dict(payload, stream=True, stream_options={"include_usage": True}),
            timeout=timeout,
            stream=True,
        )
        if resp.status_code == 200:
            break
        status, text = resp.status_code, resp.text[:300]
        resp.close()
        if status in _RETRYABLE_STATUS and attempt < 2:
            time.sleep(_retry_wait(resp, attempt))
            continue
        raise RuntimeError(f"API 错误 {status}：{text}")
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
            # reasoning_content 回传仅 kimi 路径需要（k2.6 思考链原样带回下一轮）；
            # 其他厂家不回传，避免非标准字段触发各家参数校验差异
            if is_kimi and delta.get("reasoning_content"):
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
    for attempt in range(3):  # 限流/服务端抖动退避重试，与 _post_stream 一致
        resp = _http_session().post(
            f"{get_base_url()}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            break
        status, text = resp.status_code, resp.text[:300]
        resp.close()
        if status in _RETRYABLE_STATUS and attempt < 2:
            time.sleep(_retry_wait(resp, attempt))
            continue
        raise RuntimeError(f"API 错误 {status}：{text}")
    data = resp.json()
    msg = (data.get("choices") or [{}])[0].get("message") or {"role": "assistant", "content": ""}
    return msg, data.get("usage")


def chat_with_search(messages, model=None, max_tokens=8000, max_rounds=3,
                     feature="unknown", thinking=None):
    """带联网搜索的对话：按当前厂家注入原生搜索参数片段（get_search_payload，
    数据来自 config.PROVIDERS），搜索在厂家服务端执行，返回最终文本。
    厂家无搜索能力时抛 SearchNotSupportedError（调用方负责明确提示，不静默降级）。
    内部走非流式：实测 stream=True 时 Moonshot 服务端不注入 $web_search 结果，
    模型只能编造或返回空；其他服务端自动执行型搜索（enable_search 这类开关）
    单轮即出结果，无需工具回传循环。
    Kimi 路径（model 以 kimi- 开头）保留多轮：模型发起 tool_call → arguments
    原样回传（服务端执行搜索），最多 max_rounds 轮。thinking=True/False 显式开关
    k2.6 思考（None 用模型默认，仅 kimi 路径生效）。注意：实测关思考后复杂提示词下
    模型会在正文里"表演"搜索（幻觉 $web_search）而非发起真实 tool_call，
    生产请保持 True/None；False 仅供调试用。
    messages 为 openai 格式列表；temperature 一律不传（kimi-k2.6 只允许默认值 1，
    其他厂家保持各家默认）。
    feature 用于花费记录（写入 data/ai_costs.jsonl）。"""
    key = get_api_key()
    if not key:
        raise NoApiKeyError(
            "未找到 API Key：前端请先在侧边栏「API 设置」处填入你自己的 key；"
            "CLI 请设置环境变量 MOONSHOT_API_KEY。"
        )
    model = model or get_model()  # 未显式指定时走注入链解析
    search = get_search_payload(model=model)
    if search is None:
        _, preset = _match_provider(model, get_base_url())
        label = (preset or {}).get("label") or "未收录/自定义厂家"
        raise SearchNotSupportedError(
            f"当前 API 厂家（{label}）未提供联网搜索功能，雷达不可用。")
    is_kimi = model.startswith("kimi-")  # Kimi 特有参数（thinking/多轮工具回传）仅 kimi 启用
    msgs = list(messages)
    msg = {}
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    got_usage = False
    for _ in range(max_rounds):
        payload = {"model": model, "messages": msgs, "max_tokens": max_tokens, **search}
        if is_kimi and thinking is not None:
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
        if not tool_calls or not is_kimi:
            break
        # 仅 kimi 路径需要把 $web_search 的 tool_call arguments 原样回传
        # （由 Moonshot 服务端执行搜索）；其余厂家为服务端自动执行，直接出终稿
        msgs.append(msg)
        for tc in tool_calls:
            msgs.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tc.get("function", {}).get("name", "$web_search"),
                "content": tc.get("function", {}).get("arguments") or "",
            })
    # 兜底：轮次耗尽或空输出时，去掉搜索参数强制要答案；仍空则催一次再要
    for nudge in (None, "请直接输出最终 JSON 数组，不要任何解释。"):
        if (msg.get("content") or "").strip():
            break
        if nudge is not None:
            msgs.append({"role": "user", "content": nudge})
        payload = {"model": model, "messages": msgs, "max_tokens": max_tokens}
        if is_kimi and thinking is not None:
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


def chat(messages, model=None, temperature=None, max_tokens=4096, on_chunk=None,
         feature="unknown", thinking=None):
    """发送对话，返回 assistant 文本。messages 为 openai 格式列表。
    temperature 为 None 时不传（kimi-k2.6 只允许默认值 1，显式传参会报 400）。
    on_chunk 为可选回调：每个 SSE chunk 到达时以截至目前的累积文本调用
    （用于打字机式部分回复显示）；不传则行为不变。
    thinking=True/False 显式开关 k2.6 思考（None 用模型默认，仅 kimi 路径生效）：
    整理/过滤这类「改写原文」任务必须关思考——思考链与正文共享 max_tokens，
    开着会把 8000 预算烧在推理上，轻则输出截断、重则正文为空。
    feature 用于花费记录（写入 data/ai_costs.jsonl）。"""
    key = get_api_key()
    if not key:
        raise NoApiKeyError(
            "未找到 API Key：前端请先在侧边栏「API 设置」处填入你自己的 key；"
            "CLI 请设置环境变量 MOONSHOT_API_KEY。"
        )
    model = model or get_model()  # 未显式指定时走注入链解析
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if thinking is not None and str(model).startswith("kimi-"):
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
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
