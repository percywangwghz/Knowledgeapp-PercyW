# 一级投研知识库

面向结构化 Markdown 知识库的交互式浏览 + AI 研究工具（Streamlit）。

功能：分类浏览与全文搜索、Investment Radar（信息采集/主题叙事/边际变量/周报）、
公众号文章技术提取（沉淀到技术档案）、文件拖入自动归档、项目评审、Thesis Battle。

## 快速开始（本地）

**Windows**：双击 `start.bat`（首次运行自动建虚拟环境 + 装依赖，约 1-3 分钟）。

**macOS / Linux**（或手动方式）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

浏览器自动打开 `http://localhost:8501`。详见 [部署说明.md](部署说明.md)。

**AI 功能需要 API Key**：打开页面后在左侧边栏粘贴你自己的
[Moonshot API Key](https://platform.moonshot.cn)。Key 只保存在你自己机器上，
谁使用谁出额度；不填也能浏览文档，只是 AI 功能不可用。

**PDF 高保真解析需配置多模态模型**（如 kimi-k2.6 / qwen-vl-max / glm-4.6v / gpt-4o 等，
在侧边栏「API 设置」选择厂家与模型）：配置后 PDF 逐块渲染成图、走你自己的 API 转录为
Markdown（表格/标题/图表解读/公式齐全，成本随页数线性增长）；模型不支持视觉或未填 Key 时
自动落本地库链（pymupdf4llm → pdfplumber → pypdf），纯文字 PDF 也能入库，但扫描件与
复杂版面会降质。详见 `skills/pdf-preprocess/SKILL.md`。

## 知识库目录

应用启动时自动读取**应用所在目录下的 `knowledge/` 文件夹**，无需任何路径配置——
解压到哪儿都能跑。分类目录说明：

| 目录 | 分类 | 内容 |
|------|------|------|
| `01_industry` | 行业认知 | 赛道全景、产业链、投资逻辑 |
| `02_deals` | 项目解剖 | 公司深度、投资假设、风险分析 |
| `03_frameworks` | 方法论 | 评估框架、分析模板（本仓库已含全部框架文档） |
| `04_comparables` | 横向比较 | 项目矩阵、竞品对照 |
| `05_tracking` | 动态追踪 | 追踪表、周报 |
| `06_strategy` | 投资策略 | 产业链图谱、主题策略 |
| `07_learnings` | 经验沉淀 | 方法论总结、案例复盘 |
| `08_funds` | 被投基金 | GP 方法论、投资逻辑拆解 |
| `09_tech` | 技术沉淀 | 技术档案；公众号技术提取自动落入 |

归档、技术提取、周报等功能产出的文档会自动写入对应分类目录。

路径解析顺序（高级）：环境变量 `KB_KNOWLEDGE_DIR` > 应用目录下 `knowledge/` > `~/.kimi/knowledge`。

## 部署方式

只推荐本地运行（Moonshot API 限制境外 IP，云端部署后 AI 功能不可用）。步骤见 [部署说明.md](部署说明.md)。

## 注意

- `data/` 是运行时自动生成的本地状态（含本机保存的 API key），已在 `.gitignore` 排除，不要提交
- 本仓库只放框架与代码；私人的项目/基金研究内容不要传上来
