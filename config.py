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
