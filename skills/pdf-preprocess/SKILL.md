---
name: pdf-preprocess
description: PDF 转 Markdown 的多级回滚工作流——文本层分流（有文本层本地毫秒级解析，扫描/图片页才走 VLM 且并发），本地库链保底，大 PDF 按语义切块，重要图表自动截图为 PNG。
whenToUse: 需要把 PDF（研报、论文、公告、扫描件等）高保真转换为 Markdown，或需要从 PDF 中自动截取重要图表（插图/图表区域截图）时使用；尤其含表格/图表/公式/复杂版面或页数较多（>10 页）的 PDF。
---

# PDF 预处理（pdf-preprocess）

把 PDF 转成结构化 Markdown 的自研编排层，入口为 `convert.py` 的 `convert_pdf()`，
也可 CLI 直接调用。数据只流向用户自己配置的 API 厂家（VLM 档）或哪都不去（纯本地档），
不依赖任何第三方解析服务。

## 文本层分流（速度关键，简历解析软件同款逻辑）

切块后先看每块的文本层密度（块均字符/页，env `KB_TEXT_LAYER_MIN`，默认 100）：

- **有文本层** → 本地链直接解析（毫秒级），不发 VLM 请求。原生电子版研报/论文
  基本全程走这条路，分钟级变秒级。
- **无文本层**（扫描件/图片页）→ VLM 档优先，且多个 VLM 块 `ThreadPoolExecutor`
  并发（env `KB_VLM_WORKERS`，默认 3）。

## 多级回滚链（逐块粒度，按可用性自动降级）

1. **VLM 档**：注入链解析出的模型在 `config.PROVIDERS` 中 `vision=True` 且有 API Key 时，
   对无文本层的块启用。pymupdf 本地渲染页面为 PNG → OpenAI 兼容多模态请求 → 模型转录为
   Markdown。扫描件、图表解读、复杂版面、公式只有这一档能覆盖。单块 429/5xx/超时
   指数退避最多重试 3 次；单块最坏耗时约 10 分钟后落下一档，不会卡死。
2. **pymupdf4llm**（本地保底主力）：保留标题层级与 Markdown 表格。
3. **pdfplumber**（本地补强）：文字层 + `extract_tables()` 转 Markdown 表格。
4. **pypdf 兜底**：纯文字流，裸环境保证可用。

单块全链失败时记 `degraded_pages` 并用 pypdf 文字层保底——哪怕是文字流也保留，不丢内容。

## 多模态 only 提示

VLM 档要求配置的主模型具备视觉能力（如 kimi-k2.6 / qwen-vl-max / glm-4.6v / gpt-4o 等）。
**不提供独立的解析模型选项**：主模型不支持视觉或未配 Key 时自动落本地库链，
不静默降质——调用方应在 UI 说明当前走了哪档。
VLM 成本只花在扫描/图片块上（每块一次调用，花用户自己的 token）。

## 大 PDF 切分与合并

- 先 pypdf 廉价抽文字层，按章/节标题正则找语义切点，在不超过块页数上限的前提下
  按语义边界切；找不到则均匀切。块大小按目标引擎定：VLM 档 `KB_VLM_CHUNK_PAGES`
  （默认 5 页），本地档 `KB_PDF_CHUNK_PAGES`（默认 10 页）。
- 每块独立走回滚链；单块失败/超时只影响该块，整篇不失败。
- 合并时块首注入页码锚点注释 `<!-- p.41-50 · engine:vlm -->`，保留溯源。

## 重要图表自动截取

转换同时纯本地截取图表（不调模型、零额外成本），随结果 `images` 返回：

- **嵌入位图**：`get_image_info` 拿 bbox，面积占页 ≥ `KB_IMG_MIN_FRAC`（默认 6%）
  才算重要——自动滤掉 logo/图标/页眉装饰。
- **矢量图表**：研报论文的图表多为矢量绘制，位图提取拿不到；对路径数 ≥ 30 且并集
  面积 ≥ 15% 页的页面做整区截图兜底。
- **表格不截图**：`find_tables` 检出的表格区域、以及文字覆盖率 ≥ 20% 的密集文字区
  （无边框表格/正文段落）一律跳过——能提取成 Markdown 表格的内容不留图片。
- 按内容 md5 去重（每页重复的 logo 只留一张），每页最多 2 张、单文档最多
  `KB_IMG_MAX`（默认 20，0 关闭）张，分辨率 `KB_IMG_DPI`（默认 150）。
- 入库时只保留正文 `[[图:…]]` 占位符实际引用的图；未被引用的（模型判为无关）
  不搬运、不附录，随暂存目录清掉。

## 用法

```bash
python convert.py 研报.pdf > out.md                    # 进度与降级告警走 stderr
python convert.py 研报.pdf --imgs ./imgs > out.md      # 截取的图表 PNG 写入 ./imgs
```

```python
from convert import convert_pdf
out = convert_pdf("研报.pdf", data, progress_cb=lambda info: print(info))
# -> {"md": str,
#     "blocks": [{"pages", "engine", "ok", "error"}],
#     "degraded_pages": [1-based 页码],
#     "images": [{"page", "name", "data"}]}   # 截取的图表 PNG 字节
```

`api_key` / `model` 不传时走 `llm.py` 注入链解析（线程注入 → 前端 provider → env）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_PDF_CHUNK_PAGES` | 10 | 本地档每块页数上限 |
| `KB_VLM_CHUNK_PAGES` | 5 | VLM 档每块页数上限 |
| `KB_VLM_WORKERS` | 3 | VLM 块并发数 |
| `KB_TEXT_LAYER_MIN` | 100 | 文本层判定阈值（块均字符/页） |
| `KB_IMG_MAX` | 20 | 单文档图表截取上限（0 = 关闭） |
| `KB_IMG_MIN_FRAC` | 0.06 | 图区面积占页面比例下限 |
| `KB_IMG_DPI` | 150 | 截图分辨率 |

## 依赖

`pymupdf4llm`（含 pymupdf，VLM 渲染 + 本地主力 + 图表截取）、`pdfplumber`（表格补强）、
`pypdf`（切分 + 兜底）、`requests`（VLM 请求）。库缺失时对应档自动跳过；
脱离仓库独立使用时注入链不可用，自动降级为纯本地解析（图表截取不受影响）。
