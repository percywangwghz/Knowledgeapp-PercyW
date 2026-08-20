"""
知识库应用配置
"""
import os

# 知识库根目录（优先环境变量 KB_KNOWLEDGE_DIR；其次应用目录下的 knowledge/
# （随包分发，解压即用）；最后 ~/.kimi/knowledge（开发机自用免配置）。
# 都不存在时返回应用目录 knowledge/ 路径，由 app 启动检查报错提示）
_REPO_KB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge")
_USER_KB = os.path.join(os.path.expanduser("~"), ".kimi", "knowledge")
KNOWLEDGE_DIR = os.environ.get(
    "KB_KNOWLEDGE_DIR",
    _REPO_KB if os.path.isdir(_REPO_KB)
    else (_USER_KB if os.path.isdir(_USER_KB) else _REPO_KB))

# 应用数据目录
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
INDEX_FILE = os.path.join(DATA_DIR, "kb_index.json")

# 知识库分类定义（文件夹 → 分类名 → 图标）
CATEGORY_MAP = {
    "01_industry": ("行业认知", "🏭", "二级框架：赛道全景、产业链、投资逻辑"),
    "02_deals": ("项目解剖", "🔬", "一级框架：公司深度、投资假设、风险分析"),
    "03_frameworks": ("方法论", "🛠️", "可复用工具：评估框架、分析模板"),
    "04_comparables": ("横向比较", "📊", "项目矩阵、竞品对照"),
    "05_tracking": ("动态追踪", "📈", "追踪表、里程碑监控"),
    "06_strategy": ("投资策略", "🎯", "产业链图谱、主题策略"),
    "07_learnings": ("经验沉淀", "💡", "方法论总结、案例复盘"),
    "08_funds": ("被投基金", "💼", "机构投资认知：GP 方法论、投资逻辑拆解、回报归因"),
    "09_tech": ("技术沉淀", "🧪", "各行业技术档案：技术图谱/原理详解/路线对比，公众号技术提取直接落入"),
}

# 文件类型映射
FILE_TYPES = {
    ".md": "markdown",
    ".txt": "text",
}

# 分类 → 研究框架文档（相对知识库根目录）：
# 归档整理时按对应框架的章节结构组织内容（勾稽已有研究方法）；无映射的分类走通用整理
FRAMEWORK_MAP = {
    "01_industry": "03_frameworks/行业总文档模板.md",
    "02_deals": "03_frameworks/项目解剖模板.md",
    "08_funds": "03_frameworks/机构投资心智模型提取方法论.md",
    "09_tech": "03_frameworks/技术提取框架.md",
}

# 文件类型图标
FILE_ICONS = {
    "markdown": "📝",
    "text": "📄",
    "unknown": "📎",
}

# API 厂家预设表（全项目唯一事实源，llm/app/雷达搜索适配都从这里取）。
# 每条：label 下拉显示名 / base_url OpenAI 兼容端点 / default_model 推荐默认模型 /
# vision 是否多模态（PDF 高保真解析用）/ search 原生搜索参数片段（方案二：
# 原样合并进 chat/completions 请求体即开启厂家服务端联网搜索；None=无搜索能力，
# 雷达直接报错）。search 片段逐家按官方文档核实（2026-08-12），来源 URL 见各行注释。
PROVIDERS = {
    # platform.kimi.com/docs/guide/use-web-search（2026-08-12 核实；现状平移：
    # $web_search 为 builtin_function，模型发起 tool_call 后 arguments 原样回传，
    # 由 Moonshot 服务端执行搜索）
    "moonshot": {
        "label": "Moonshot Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "kimi-k2.6",
        "vision": True,
        "search": {"tools": [{"type": "builtin_function",
                              "function": {"name": "$web_search"}}]},
    },
    # api-docs.deepseek.com（官方注明 deepseek-chat 无视觉、无服务端搜索，2026-08-12 核实）
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "vision": False,
        "search": None,
    },
    # docs.bigmodel.cn/cn/guide/tools/web-search（2026-08-12 核实：Chat Completions
    # 经 tools 传 web_search 工具、enable=true 开启，服务端执行，无需工具回传）
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4.6",
        "vision": True,
        "search": {"tools": [{"type": "web_search",
                              "web_search": {"enable": True}}]},
    },
    # 火山方舟文档（volcengine.com/docs/82379）；默认模型为占位——
    # 方舟按接入点（endpoint）调用，用户需换成自己创建的 endpoint 模型 id。
    # search 为 Web Search 联网内容插件（volcengine.com/docs/82379/1756990，
    # 2026-08-12 核实：内置工具，服务端执行，无需工具回传）
    "doubao": {
        "label": "豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-seed-1-6",
        "vision": True,
        "search": {"tools": [{"type": "web_search"}]},
        "note": "模型需换成你在火山方舟创建的接入点（endpoint）模型 id",
    },
    # help.aliyun.com/zh/model-studio/web-search（2026-08-12 核实：OpenAI 兼容
    # Chat Completions 顶层传 enable_search=true 开启，服务端执行）；
    # qwen-vl-max 为多模态款（纯文本可换 qwen-plus）
    "dashscope": {
        "label": "通义千问（DashScope）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-vl-max",
        "vision": True,
        "search": {"enable_search": True},
    },
    # developers.openai.com《Web search》指南（2026-08-12 核实：Chat Completions
    # 仅 gpt-5-search-api 等搜索专用模型支持联网，配 web_search_options；
    # Responses API 的 web_search 工具本客户端不支持）
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "vision": True,
        "search": {"web_search_options": {}},
        "note": "联网搜索仅 gpt-5-search-api 等搜索专用模型支持，用雷达请把模型改为搜索款",
    },
    # ai.google.dev/gemini-api/docs/openai（2026-08-12 核实：google_search
    # grounding 仅原生 generateContent API 支持，该 OpenAI 兼容端点不支持，
    # 故 search=None 按无搜索处理）
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "vision": True,
        "search": None,
        "note": "联网搜索（google_search grounding）仅原生 API 支持，雷达暂不可用",
    },
    # docs.anthropic.com（2026-08-12 核实）：官方无 OpenAI 兼容端点，web_search
    # 工具仅 Messages 原生 API 支持，故 search=None；中转场景由「自定义」兜底
    "anthropic": {
        "label": "Anthropic Claude",
        "base_url": "",
        "default_model": "claude-sonnet-4-5",
        "vision": True,
        "search": None,
        "note": "官方无 OpenAI 兼容端点，需经代理/中转，Base URL 手填；雷达搜索不可用",
    },
    # docs.x.ai（2026-08-12 核实：Live Search 的 search_parameters 已在 Chat
    # Completions 弃用——官方返回 410 并引导迁移至 Responses/Agent Tools API 的
    # web_search 工具；本客户端走 Chat Completions，故 search=None）
    "xai": {
        "label": "xAI Grok",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-4",
        "vision": True,
        "search": None,
        "note": "Live Search 已被官方弃用（迁移至 Responses API），雷达暂不可用",
    },
    # docs.perplexity.ai（2026-08-12 核实：sonar 系列全模型内置实时联网搜索，
    # 无需任何额外参数，故 search 为空 dict；无视觉）
    "perplexity": {
        "label": "Perplexity",
        "base_url": "https://api.perplexity.ai",
        "default_model": "sonar",
        "vision": False,
        "search": {},
        "note": "sonar 全模型内置联网搜索，无需额外参数",
    },
    # 自定义：覆盖代理/中转站/新厂家，端点与模型全由用户填；search=None 按无搜索处理
    "custom": {
        "label": "自定义",
        "base_url": "",
        "default_model": "",
        "vision": True,
        "search": None,
        "note": "Base URL 与模型名按你的服务商文档填写；自定义端点按无联网搜索处理（雷达不可用）",
    },
}

# 分类颜色（用于UI）
CATEGORY_COLORS = {
    "行业认知": "#1f77b4",
    "项目解剖": "#ff7f0e",
    "方法论": "#2ca02c",
    "横向比较": "#d62728",
    "动态追踪": "#9467bd",
    "投资策略": "#8c564b",
    "经验沉淀": "#e377c2",
    "被投基金": "#17becf",
    "技术沉淀": "#0aa1dd",
    "其他": "#7f7f7f",
}
