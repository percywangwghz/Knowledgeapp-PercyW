# -*- coding: utf-8 -*-
"""
文件归档模块（可独立 CLI 运行）：拖入文件 → 读取内容 → AI 判断分类 → 人工确认
→ 按确认框架整理成知识库文档。

支持 .pdf / .docx / .txt / .md。分析阶段只做轻量分类（只看开头）；确认入库时按
config.FRAMEWORK_MAP 勾稽 03_frameworks 的研究框架整理正文（无映射走通用整理）。
界面流程为两阶段：AI 提议分类 → 人工可改判 → 确认入库（整理在此执行）。
落盘时自动补来源行（> 来源文件：… · 归档于 …）供索引器提取元数据。

CLI:
    python ingest.py 文件1.pdf 文件2.docx [--dry-run]

数据：写入 config.KNOWLEDGE_DIR 下对应分类目录，刷新索引后出现在应用中。
"""
import argparse
import concurrent.futures
import html
import io
import json
import os
import re
import shutil
import threading
import time
from datetime import date, datetime

import config
from llm import (chat, get_api_key, get_base_url, get_model,
                 set_thread_api_key, set_thread_base_url, set_thread_model)

SUPPORTED_EXT = {".pdf", ".docx", ".txt", ".md"}
# 分段整理的块大小（env KB_TEXT_CHUNK 可配）：原文按此切块逐块整理后合并，不再硬截断
TEXT_LIMIT = int(os.environ.get("KB_TEXT_CHUNK", "15000") or "15000")
# 分类只看前段是刻意设计（轻量调用，标题/摘要在开头即可判断）；env KB_CLASSIFY_LIMIT 可配
CLASSIFY_TEXT_LIMIT = int(os.environ.get("KB_CLASSIFY_LIMIT", "3000") or "3000")
FRAMEWORK_LIMIT = 4000      # 研究框架文档送入模型的截断长度
MERGE_INPUT_LIMIT = 100000  # reduce 合并工序送入模型的草稿截断长度（长报告草稿可达 6 万+，过紧会静默截尾）
MAX_FILENAME = 60           # 生成文件名长度上限（不含扩展名）

DATA_DIR = config.DATA_DIR
JOB_FILE = "ingest_job.json"        # 后台分析任务的进度/结果落盘文件
SAVE_JOB_FILE = "ingest_save_job.json"  # 确认入库（含改判重整）后台任务落盘文件
UPLOAD_CACHE_DIR = "tmp_upload"         # 上传文件字节缓存目录：重启后「重新发起分析」用
SAVE_PLAN_FILE = "ingest_save_plan.json"  # 入库计划落盘：重启后「重新发起入库」用
IMG_STAGE_DIR = "tmp_imgs"              # 图表截取暂存：分析时落盘，入库时搬进 knowledge/assets/img/
PARTIAL_THROTTLE = 0.8  # 打字机 partial 落盘节流（秒）：距上次落盘不足此间隔只更新内存
PARTIAL_TAIL = 500      # 前端打字机渲染的 partial 末尾截取长度（字符）
# 分段整理的并行度（env KB_ORGANIZE_WORKERS 可配）：map 阶段各块相互独立，
# 并行后整理耗时 ≈ 单块耗时 × 块数/并行度；配 429 退避，默认 3 路较稳
ORGANIZE_WORKERS = int(os.environ.get("KB_ORGANIZE_WORKERS", "3") or "3")

_job_thread = None   # 后台分析线程（模块级变量，跨 rerun 存活）
_save_thread = None  # 后台入库线程（同上）

CLASSIFY_PROMPT = """你是一级市场投资研究团队的知识库管理员。给你一份文件的文件名和开头内容，
请判断它在知识库中的分类并给出编目信息。

知识库分类（key：名称 — 说明）：
{categories}
如果哪个分类都不合适，category_key 填 ""。

输出严格 JSON 对象（不要输出任何其他文字）：
{{"category_key": "分类key", "title": "文档标题", "filename": "文件名（不含扩展名，用中文或英文短横线命名）",
  "summary": "一句话说明这份文档是什么"}}"""

ORGANIZE_PROMPT = """你是一级市场投资研究团队的知识库管理员。给你一份文件的原文，
请把它整理成一篇结构化的知识库文档。

整理要求：
- 保留原文的关键事实、数据和结论，去除页眉页脚/导航等噪声；不改变原意，不得编造原文没有的信息。
- 第一行为 "# 标题"，第二行为 "> 一句话摘要"。
{framework_block}
{image_block}直接输出整理后的 markdown 正文，不要输出任何其他文字，不要用 markdown 代码块包裹。{truncated_note}"""

MERGE_PROMPT = """你是一级市场投资研究团队的知识库管理员。给你一份由「分段整理」拼接而成的知识库文档草稿，
它有两个典型毛病：相邻段的拼接处存在重复小节/重复表述；混入了研究框架之外的章节（自行发明的附录、总结等）。

{whitelist_block}

请输出最终文档，要求：
- 同一内容只保留信息最全的一份，删除重复小节与重复表述；
- 白名单之外的章节：其中有价值的事实并入最相关的框架章节，其余整节删除；
- 文中的图片占位符 [[图:文件名]] 原样保留，不要删除、改写或挪动到不相关的小节；
- 不编造、不改变任何事实与数据；保留 "# 标题" 首行与 "> 一句话摘要" 次行（缺失则据正文补写）。
直接输出最终 markdown 正文，不要输出任何其他文字，不要用 markdown 代码块包裹。"""


def _framework_headings(framework):
    """从研究框架文档中提取章节标题行（# 级），作为整理产出的章节白名单。"""
    return [ln.strip() for ln in (framework or "").split("\n")
            if re.match(r"^#{1,6}\s", ln.strip())]


def _norm_heading(line):
    """章节标题归一化（重复小节归并键）：去 # 与所有空白、全角冒号转半角、小写。"""
    h = re.sub(r"^#{1,6}\s*", "", line.strip())
    return h.replace("：", ":").replace(" ", "").lower()


_ENUM_RE = re.compile(r"^(?:\d+[.、]|[一二三四五六七八九十]+[、.]|模块\d+[:：]|附录[a-z][:：])")


def _dedupe_sections(content, whitelist=None):
    """分段拼接草稿的确定性去重：按 ## 小节切分，同标题小节只保留信息最全
    （最长）的一份，顺序按首次出现——LLM 合并对长草稿不可靠（照抄拼接 +
    输出截断），机械去重必须确定性做，LLM merge 只负责粘合与措辞。
    whitelist（框架章节白名单）非空时做框架硬对齐：标题去编号后与任一白名单
    标题互为子串才保留，其余一律丢弃，并按白名单顺序重排；零匹配（框架选错）
    时放弃过滤只去重，避免整篇清空。"""
    lines = content.split("\n")
    preamble, sections, cur = [], [], None
    for ln in lines:
        if re.match(r"^##\s", ln):
            cur = (ln, [])
            sections.append(cur)
        elif cur is None:
            preamble.append(ln)
        else:
            cur[1].append(ln)
    if not sections:
        return content
    best, order = {}, []
    for head, body in sections:
        key = _norm_heading(head)
        size = len("\n".join(body))
        if key not in best:
            best[key] = (head, body, size)
            order.append(key)
        elif size > best[key][2]:
            best[key] = (head, body, size)
    secs = [(key, *best[key][:2]) for key in order]
    if whitelist:
        wl = [(i, _ENUM_RE.sub("", _norm_heading(h))) for i, h in enumerate(whitelist)]

        def _wl_pos(key):
            """白名单位置：精确匹配（输出模板级标题）优先；否则取首个子串匹配
            （模块说明级标题措辞常与产出不同，如「Winning Pattern Mining」
            vs 产出「Winning Investment Pattern」，混排会打乱模板顺序）。"""
            k = _ENUM_RE.sub("", key)
            sub_pos = None
            for i, w in wl:
                if not k or not w:
                    continue
                if k == w:
                    return i
                if sub_pos is None and (k in w or w in k):
                    sub_pos = i
            return sub_pos

        matched = [(p, h, b) for k, h, b in secs for p in [_wl_pos(k)] if p is not None]
        if matched:  # 有框架即硬约束：白名单外章节一律丢弃，只留零匹配（框架完全
            matched.sort(key=lambda x: x[0])   # 选错）时放弃过滤保底，避免整篇清空
            secs = [(h, b) for _, h, b in matched]
        else:
            secs = [(h, b) for _, h, b in secs]
    else:
        secs = [(h, b) for _, h, b in secs]
    out = ["\n".join(preamble).strip("\n")]
    out += [h + "\n" + "\n".join(b).strip("\n") for h, b in secs]
    return "\n\n".join(x for x in out if x.strip())


_DIGEST_CAP = 3  # 「背景速览」类小节最多保留的段/条数（用户要求：提炼两三段即可）


def _cap_digest_sections(content, cap=_DIGEST_CAP):
    """背景速览小节的确定性长度上限：prompt 里的提炼要求只是软约束，模型仍会
    罗列十几条流水账——这里硬截断兜底，速览不许比正文长。段落/列表项按
    非空行计，保留前 cap 条，其余丢弃。"""
    lines = content.split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        i += 1
        if re.match(r"^##\s*背景速览\s*$", ln.strip()):
            kept = 0
            while i < len(lines) and not re.match(r"^##\s", lines[i]):
                if lines[i].strip():
                    kept += 1
                    if kept <= cap:
                        out.append(lines[i])
                i += 1
    return "\n".join(out)


# ==================== 文本提取 ====================
_convert_mod = None  # skills/pdf-preprocess/convert.py 的惰性加载缓存


def _load_convert():
    """按文件路径加载 PDF 转换编排层（目录名含连字符，不能走常规 import）。"""
    global _convert_mod
    if _convert_mod is None:
        import importlib.util
        path = os.path.join(config.APP_DIR, "skills", "pdf-preprocess", "convert.py")
        spec = importlib.util.spec_from_file_location("kb_pdf_convert", path)
        _convert_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_convert_mod)
    return _convert_mod


def _extract_with_meta(filename, data, progress_cb=None):
    """提取文本并附解析元信息（PDF 走多级回滚链，返回块/降级页信息）。
    返回 (text, note, images)；note 为人类可读的解析说明（空串表示无特别事项）；
    images 为 PDF 截取的重要图表 [{"page","name","data"}]，非 PDF 恒为 []。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"不支持的文件类型：{ext or '（无扩展名）'}，支持 {'/'.join(sorted(SUPPORTED_EXT))}")
    if ext in (".txt", ".md"):
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc), "", []
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace"), "", []
    if ext == ".pdf":
        # 子进程隔离版：pymupdf 段错误只杀子进程，主进程自动降级 pypdf，服务不中断
        out = _load_convert().convert_pdf_isolated(filename, data, progress_cb=progress_cb)
        engines = {}
        for b in out["blocks"]:
            engines[b["engine"]] = engines.get(b["engine"], 0) + 1
        note = "PDF 解析：" + " / ".join(f"{k} {v} 块" for k, v in engines.items())
        if out["degraded_pages"]:
            note += f"；第 {out['degraded_pages']} 页降级为低保真提取"
        if out.get("images"):
            note += f"；自动截取图表 {len(out['images'])} 张"
        return out["md"], note, out.get("images") or []
    # .docx：按文档顺序交错输出段落与表格（旧逻辑段落全在前、表格堆在后）
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = docx.Document(io.BytesIO(data))
    parts = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            parts.append(Paragraph(child, doc).text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts), "", []


def extract_text(filename, data, progress_cb=None):
    """从文件字节中提取文本（PDF 产出 Markdown）。不支持的扩展名抛 ValueError。"""
    return _extract_with_meta(filename, data, progress_cb)[0]


# ==================== AI 整理 ====================

def _strip_outer_fence(text):
    """只去最外层包裹的 ``` 围栏（保留正文里可能存在的代码块）。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def _parse_json_obj(text):
    """容错解析单个 JSON 对象：去外层 fence / 截取花括号范围。"""
    text = _strip_outer_fence(text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _sanitize_filename(name, fallback):
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip()).strip("_")
    if not name:
        name = fallback
    return name[:MAX_FILENAME]


def _load_framework(category_key):
    """按 config.FRAMEWORK_MAP 读该分类的研究框架文档（截断），无映射或文件缺失返回 ""。"""
    rel = config.FRAMEWORK_MAP.get(category_key, "")
    if not rel:
        return ""
    path = os.path.join(config.KNOWLEDGE_DIR, rel)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()[:FRAMEWORK_LIMIT]


def classify_document(filename, text, on_chunk=None):
    """轻量调用（只看开头）：判断分类 + 编目信息。
    on_chunk 为可选流式回调（透传 chat，供打字机式进度显示）。
    返回 {category_key, title, filename, summary}。"""
    categories = "\n".join(f"- {k}：{v[0]} — {v[2]}" for k, v in config.CATEGORY_MAP.items())
    out = chat([
        {"role": "system", "content": CLASSIFY_PROMPT.format(categories=categories)},
        {"role": "user", "content": f"文件名：{filename}\n\n原文开头：\n{text[:CLASSIFY_TEXT_LIMIT]}"},
    ], max_tokens=1000, on_chunk=on_chunk, feature="ingest", thinking=False)
    meta = _parse_json_obj(out)
    stem = os.path.splitext(os.path.basename(filename))[0]
    cat_key = meta.get("category_key", "")
    if cat_key not in config.CATEGORY_MAP:
        cat_key = ""
    return {"category_key": cat_key,
            "title": str(meta.get("title", "")).strip() or stem,
            "filename": _sanitize_filename(meta.get("filename"), stem),
            "summary": str(meta.get("summary", "")).strip()}


def _hard_split(text, limit):
    """超长无标题文本按段落边界硬切，单段仍超长则按字数切。"""
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > limit:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
        while len(buf) > limit:  # 单段超 limit：先切出 limit 一段
            chunks.append(buf[:limit])
            buf = buf[limit:]
    if buf:
        chunks.append(buf)
    return chunks


def _split_chunks(text, limit):
    """按 Markdown 标题切块（无标题按字数）：小节尽量合并进 limit 内，
    超长小节再按段落硬切。用于长文的分段整理（map-reduce）。"""
    sections, cur = [], []
    for line in text.split("\n"):
        if re.match(r"^#{1,6}\s", line) and cur:
            sections.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        sections.append("\n".join(cur))
    chunks, buf = [], ""
    for sec in sections:
        if len(sec) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(sec, limit))
        elif buf and len(buf) + len(sec) + 1 > limit:
            chunks.append(buf)
            buf = sec
        else:
            buf = f"{buf}\n{sec}" if buf else sec
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


_TRIM_BOILER_RE = re.compile(
    r"^(?:#{1,6}\s*)?[^\n]{0,20}(免责声明|法律声明|分析师声明|版权声明|公司声明)[^\n]{0,20}$",
    re.MULTILINE)


def _trim_boilerplate(text):
    """LLM 调用前的文本瘦身（只作用于喂给模型的副本，归档原文不动）：
    1) 跨页重复行：同一短行（8-80 字符，页眉/页脚/每页重复的声明行）出现 ≥4 次时
       仅保留首次出现——PDF 转换产物里这类行每页一条，纯 token 浪费；
    2) 尾部法律样板：「免责声明/法律声明/分析师声明/版权声明/公司声明」起至文末，
       仅在该标题位于全文后 1/4 时才剥，避免误伤「风险因素」等正文章节。"""
    lines = text.split("\n")
    counts = {}
    for ln in lines:
        s = ln.strip()
        if 8 <= len(s) <= 80:
            counts[s] = counts.get(s, 0) + 1
    seen = set()
    kept = []
    for ln in lines:
        s = ln.strip()
        if counts.get(s, 0) >= 4:
            if s in seen:
                continue
            seen.add(s)
        kept.append(ln)
    text = "\n".join(kept)
    m = _TRIM_BOILER_RE.search(text, int(len(text) * 0.75))
    if m:
        text = text[:m.start()].rstrip() + "\n"
    return text


def organize_document(filename, text, category_key="", on_chunk=None, chunk_cb=None,
                      stage_cb=None, images=None):
    """按分类研究框架整理正文（勾稽 03_frameworks 的方法论）；无框架时走通用结构化整理。
    原文超过 TEXT_LIMIT 时分段整理（map-reduce）：按 Markdown 标题切块 → 每块分别整理
    → 顺序合并为一个文档；分段拼接后先做确定性去重（同标题小节只留最长一份、
    按框架白名单过滤重排），再交 LLM merge 做粘合——机械去重不交给模型。
    images 为该文档截取的图表文件名清单（如 p3_1.png）：整理 prompt 会附上清单，
    模型在相关小节末尾留 [[图:文件名]] 占位符，入库时替换为真实图片引用。
    on_chunk 为可选流式回调（透传每次 chat，分段时逐块生效，供打字机式进度显示）；
    chunk_cb(n, total) 为可选分段进度回调（并行时按完成数上报）；
    stage_cb("merge") 在进入合并工序时回调（供任务层串联阶段进度）。
    返回 {"content": ...}。"""
    framework = _load_framework(category_key)
    if framework:
        cat_name = config.CATEGORY_MAP.get(category_key, ("", "", ""))[0]
        framework_block = (
            f"- 本文档属于「{cat_name}」。本所对此类资料的研究框架如下：严格且仅按框架的章节结构组织内容，"
            "不得新增框架之外的章节（包括自行发明的附录、总结、补充说明）；"
            "原文中框架没有对应章节的内容，并入最相关的框架章节；"
            "框架章节若原文未涉及，标注「（原文未涉及）」，不要硬凑编造；"
            "「背景速览」类信息小节只做提炼，至多 3 段/条，不得罗列堆砌、不得比正文章节还长。\n"
            f"== 研究框架 ==\n{framework}\n== 框架结束 ==")
    else:
        framework_block = ("- 按知识库文档风格划分章节（## 小节），必要时用列表/表格；"
                           "原文本身是优质结构化文档时，可以基本保留原文，只做清理和小节化。")
    if images:
        inv = "、".join(
            f"{n}（第{m.group(1)}页）" if (m := re.match(r"p(\d+)_", n)) else n
            for n in images)
        image_block = (f"- 本文档自动截取了这些图表：{inv}。某小节内容与某张图直接相关时，"
                       "在该小节末尾单独起一行写占位符 [[图:文件名]]（如 [[图:p3_1.png]]），"
                       "只许使用清单内的文件名，无关就不要引用（未被引用的图不会入库）。\n")
    else:
        image_block = ""
    chunks = _split_chunks(text, TEXT_LIMIT)

    def _organize_chunk(i, chunk):
        """整理单个块，返回 (i, content)；并行/串行共用，保证块序可复原。
        空输出（多为 k2.6 思考链烧光 max_tokens 正文没出来）重试一次；仍空则
        回退该块原文——静默丢块会让后续整理面对残篇，满篇「原文未涉及」。"""
        note = ""
        if len(chunks) > 1:
            note = f"\n\n注意：原文较长，已按章节切分为 {len(chunks)} 段，这是第 {i + 1} 段。"
            if i > 0:
                note += "请整理为同一份文档的续篇：直接输出 ## 级小节内容，不要重复输出 # 标题与摘要行。"
        msgs = [
            {"role": "system", "content": ORGANIZE_PROMPT.format(framework_block=framework_block,
                                                                 truncated_note=note,
                                                                 image_block=image_block)},
            {"role": "user", "content": f"文件名：{filename}\n\n原文：\n{chunk}"},
        ]
        for _ in range(2):
            # 关思考：改写类任务不需要推理，思考链只会与正文抢 max_tokens；
            # 预算提到 12000：密集报告的块提取可能逼近 8000，顶格即静默截断正文
            content = _strip_outer_fence(chat(msgs, max_tokens=12000, on_chunk=on_chunk,
                                              feature="ingest", thinking=False))
            if content.strip():
                return i, content
        return i, chunk  # 整理失败不丢内容：该块以原文形态进合并

    if len(chunks) > 1 and ORGANIZE_WORKERS > 1:
        # map 阶段各块独立 → 线程池并行（LLM 等待占绝对大头，近似线性加速）；
        # chunk_cb 按完成数上报（并行下块完成顺序不定，报完成数比报块号更有意义）
        # 池线程是全新线程：无 ScriptRunContext，也拿不到外层线程的 threading.local
        # 注入（threading.local 按线程隔离）——必须在本线程解析好 key/模型/端点
        # 再逐池线程注入，否则池内 chat 一律 NoApiKeyError（长文档分段整理必现）
        api_key, model, base_url = get_api_key(), get_model(), get_base_url()
        done = [0]
        cb_lock = threading.Lock()

        def _run_one(i_chunk):
            set_thread_api_key(api_key)
            set_thread_model(model)
            set_thread_base_url(base_url)
            i, content = _organize_chunk(*i_chunk)
            with cb_lock:
                done[0] += 1
                if chunk_cb:
                    chunk_cb(done[0], len(chunks))
            return i, content

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(ORGANIZE_WORKERS, len(chunks))) as ex:
            results = list(ex.map(_run_one, enumerate(chunks)))
        parts = [c for _, c in sorted(results) if c]
    else:
        parts = []
        for i, chunk in enumerate(chunks):
            _, content = _organize_chunk(i, chunk)
            if chunk_cb:
                chunk_cb(i + 1, len(chunks))
            if content:
                parts.append(content)
    content = "\n\n".join(parts)
    if content and len(parts) > 1:
        headings = _framework_headings(framework)
        # 确定性去重先行：同标题小节只留最长一份 + 按框架白名单过滤重排。
        # 机械去重交给模型不可靠（长草稿照抄拼接 + 输出 token 上限截断），必须确定性做
        content = _dedupe_sections(content, headings or None)
        # reduce 工序：去重后的草稿交 LLM 做粘合（拼接处的半句重复、措辞统一）
        if stage_cb:
            stage_cb("merge")
        whitelist_block = ("研究框架章节（白名单，最终文档只允许出现这些章节）：\n" + "\n".join(headings)
                           if headings else
                           "（本文档无预设研究框架：按知识库通用结构归并重复内容即可，不要自行新增附录。）")
        merged = _strip_outer_fence(chat([
            {"role": "system", "content": MERGE_PROMPT.format(whitelist_block=whitelist_block)},
            {"role": "user", "content": f"文件名：{filename}\n\n草稿：\n{content[:MERGE_INPUT_LIMIT]}"},
        ], max_tokens=16000, on_chunk=on_chunk, feature="ingest", thinking=False))
        if merged.strip():
            content = merged
    if content:
        content = _cap_digest_sections(content)  # 背景速览硬上限，防模型罗列堆砌
    if not content:
        stem = os.path.splitext(os.path.basename(filename))[0]
        content = f"# {stem}\n\n> AI 整理失败，以下为原文摘录。\n\n{text[:TEXT_LIMIT]}"
    return {"content": content}


# ==================== 归档 ====================

_IMG_TOKEN_RE = re.compile(r"\[\[图\s*[:：]\s*([^\]]+?)\s*\]\]")


def _attach_images(result, folder, name, lines):
    """把正文 [[图:文件名]] 占位符实际引用的图表 PNG 搬进 knowledge/assets/img/<文档名>/，
    占位符原地替换为相对路径引用，让图表跟着相关分析走。
    未被引用的图（模型判断与正文无关）不搬运、不附录，随暂存目录一并清掉——
    自动截取是广撒网兜底，无关图表堆进文档只会稀释信息密度（用户明确要求）。
    相对路径引用（Obsidian/Typora/导出 HTML 可直接显示）；暂存缺失跳过不报错。
    恒返回 []（已取消文末图集段）。"""
    images = result.get("images") or []
    if not images:
        return []
    src_dir = os.path.join(_path(IMG_STAGE_DIR), _slug(result.get("file", "")))
    used = []
    for ln in lines:
        for n in _IMG_TOKEN_RE.findall(ln):
            if n in images and n not in used:
                used.append(n)
    refs = {}
    img_dir = os.path.join(config.KNOWLEDGE_DIR, "assets", "img", name)
    for im_name in used:
        src = os.path.join(src_dir, im_name)
        if not os.path.isfile(src):
            continue
        os.makedirs(img_dir, exist_ok=True)
        dst = os.path.join(img_dir, im_name)
        shutil.move(src, dst)
        m = re.match(r"p(\d+)_", im_name)
        cap = f"p.{m.group(1)}" if m else ""
        rel = os.path.relpath(dst, folder).replace(os.sep, "/")
        refs[im_name] = f"![{cap} 图表]({rel})"
    shutil.rmtree(src_dir, ignore_errors=True)
    for i, ln in enumerate(lines):
        def _sub(m):
            return refs.get(m.group(1), m.group(0))  # 清单外/暂存缺失占位符保留原样
        lines[i] = _IMG_TOKEN_RE.sub(_sub, ln)
    return []


def save_document(result, source_name):
    """写入知识库对应分类目录（category_key 为空时写入根目录 → 索引为"其他"）。
    文件名冲突自动加序号。在标题行后插入来源行；正文引用的截取图表就地内联。
    原文全文不落文档（用户明确要求不留），改判重整的原文由入库计划/上传缓存供给。
    返回绝对路径。"""
    folder = (os.path.join(config.KNOWLEDGE_DIR, result["category_key"])
              if result["category_key"] else config.KNOWLEDGE_DIR)
    os.makedirs(folder, exist_ok=True)
    base, n = result["filename"], 1
    while True:
        name = base if n == 1 else f"{base}_{n}"
        path = os.path.join(folder, f"{name}.md")
        if not os.path.exists(path):
            break
        n += 1
    source_line = f"> 来源文件：{source_name} · 归档于 {date.today().isoformat()}"
    lines = result["content"].split("\n")
    if lines and lines[0].startswith("# "):
        lines.insert(1, source_line)
    else:
        lines = [f"# {result['title']}", source_line, ""] + lines
    lines += _attach_images(result, folder, name, lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _analyze_one(filename, data, progress_cb=None, on_chunk=None, stage_cb=None,
                 organize=True):
    """单个文件的分析阶段：提取 → 分类（轻量调用）→ 按分类框架整理，不落盘。
    organize=False 时只做到分类为止（页面后台分析任务走这个模式：整理工序统一
    前移到入库阶段、按人工确认的分类执行，避免 AI 分类跑错时整篇白整理再重跑）。
    on_chunk 为可选流式回调（透传分类/整理两次 chat，供打字机式进度显示）；
    stage_cb 为可选阶段回调，依次收到 classify / organize(category_key) / chunk(n, total)，
    供任务层把「解析 → 分类 → 分段整理」的阶段串联写进进度状态。
    结果 dict 含 text（留存全文不截断，供入库阶段按确认框架分段整理）；
    organize=False 时 content 为空。"""
    res = {"file": filename, "ok": False, "category_key": "", "title": "",
           "filename": "", "summary": "", "content": "", "text": "", "error": ""}
    try:
        text, note, images = _extract_with_meta(filename, data, progress_cb)
        # 扫描件无文字层时 md 只剩锚点注释，判空需去掉注释
        if not re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip():
            res["error"] = "提取不到文本（扫描件 PDF 请配置多模态模型走 VLM 档解析）"
            return res
        if note:
            res["note"] = note
        llm_text = _trim_boilerplate(text)  # 喂模型的副本瘦身；归档留存仍是原文全文
        if stage_cb:
            stage_cb("classify")
        res.update(classify_document(filename, llm_text, on_chunk=on_chunk))
        if organize:
            if stage_cb:
                stage_cb("organize", res["category_key"])
            res.update(organize_document(
                filename, llm_text, res["category_key"], on_chunk=on_chunk,
                chunk_cb=(lambda n, t: stage_cb("chunk", n, t)) if stage_cb else None,
                stage_cb=stage_cb,
                images=[im["name"] for im in images] if images else None))
        res["text"] = text
        if images:
            # 截取的图表暂存 data/tmp_imgs/（字节不随任务 JSON 走），入库时搬进知识库
            res["images"] = _stage_images(filename, images)
        res["ok"] = True
    except Exception as e:
        res["error"] = str(e)
    return res


def ingest_one(filename, data):
    """单个文件完整流程（CLI 用）：分析（解析+分类）→ 按 AI 提议分类过滤+整理 → 归档。
    返回结果 dict。"""
    res = _analyze_one(filename, data, organize=False)
    if res["ok"]:
        try:
            res.update(organize_document(filename, _trim_boilerplate(res["text"]),
                                         res["category_key"]))
            res["path"] = save_document(res, os.path.basename(filename))
        except Exception as e:
            res["ok"] = False
            res["error"] = str(e)
    res.pop("text", None)
    res.pop("content", None)
    return res


# ==================== 后台分析任务 ====================

def _path(name):
    return os.path.join(DATA_DIR, name)


def _job_running():
    return _job_thread is not None and _job_thread.is_alive()


def _read_job(name=JOB_FILE):
    if os.path.exists(_path(name)):
        try:
            with open(_path(name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None  # 任务文件损坏时按无任务处理，不崩页面
    return None


def _write_job(state, name=JOB_FILE):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    for attempt in range(5):  # Windows 上读者持有句柄时 os.replace 会被拒，短暂重试
        try:
            os.replace(tmp, _path(name))
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, _path(name))


def _cache_uploads(files_data):
    """把上传文件字节落到 data/tmp_upload/：应用重启后「重新发起分析」用。
    先清掉上一批缓存再写，避免旧文件混入；缓存失败不应阻塞分析（调用方包 try）。"""
    d = _path(UPLOAD_CACHE_DIR)
    os.makedirs(d, exist_ok=True)
    for old in os.listdir(d):
        os.remove(os.path.join(d, old))
    for name, data in files_data:
        with open(os.path.join(d, os.path.basename(name)), "wb") as f:
            f.write(data)


def _load_cached_uploads():
    """读回缓存的上传文件 [(name, bytes)]；目录不存在/为空返回 []。"""
    d = _path(UPLOAD_CACHE_DIR)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        fp = os.path.join(d, fn)
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                out.append((fn, f.read()))
    return out


def _clear_upload_cache():
    shutil.rmtree(_path(UPLOAD_CACHE_DIR), ignore_errors=True)


def _slug(name):
    """文件名 → 目录安全 slug（去扩展名，非法字符替换下划线，限长）。"""
    return re.sub(r"[^\w.-]", "_", os.path.splitext(os.path.basename(name))[0])[:60] or "doc"


def _stage_images(filename, images):
    """把截取的图表 PNG 落到 data/tmp_imgs/<源文件slug>/，返回文件名列表（入库时搬走）。
    先清掉该文件旧的暂存，避免同一文件两次分析残留混叠。"""
    d = os.path.join(_path(IMG_STAGE_DIR), _slug(filename))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    names = []
    for im in images:
        with open(os.path.join(d, im["name"]), "wb") as f:
            f.write(im["data"])
        names.append(im["name"])
    return names


def _fmt_eta(seconds):
    """ETA 秒数 → 人类可读：不足 60 秒显示「约 1 分钟内」，否则「约 X 分钟」。"""
    if seconds < 60:
        return "约 1 分钟内"
    return f"约 {int(seconds / 60 + 0.5)} 分钟"


def _eta_seconds(started_iso, done, total, now=None):
    """由任务开始时间（ISO 字符串）与 完成量/总量 估剩余秒数：elapsed/done*(total-done)。
    done<=0（样本不足）/ total<=done / 时间戳不可解析时返回 None，不瞎估。"""
    if done <= 0 or total <= done:
        return None
    try:
        start_ts = datetime.fromisoformat(started_iso).timestamp()
    except (TypeError, ValueError):
        return None
    elapsed = max(0.0, (now if now is not None else time.time()) - start_ts)
    return elapsed / done * (total - done)


def _live_eta(job, steps):
    """前端信息条 ETA 后缀：优先块级计数（PDF 细粒度），否则文件粒度；样本不足返回空串。
    返回形如「 · 预计剩余约 3 分钟」的文案（前导空格+中点），直接拼进信息条。"""
    blocks = job.get("blocks") or {}
    eta = _eta_seconds(job.get("started", ""), blocks.get("done", 0), blocks.get("total", 0))
    if eta is None and steps:
        done_files = sum(1 for s in steps if s.get("status") in ("done", "error"))
        eta = _eta_seconds(job.get("started", ""), done_files, len(steps))
    return f" · 预计剩余{_fmt_eta(eta)}" if eta is not None else ""


def _fmt_elapsed(started_iso):
    """由 ISO 开始时间算「 · 已用 X 分 Y 秒」后缀；解析失败返回空串。"""
    try:
        sec = max(0, int(time.time() - datetime.fromisoformat(started_iso).timestamp()))
    except (TypeError, ValueError):
        return ""
    return f" · 已用 {sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f" · 已用 {sec} 秒"


def _throttled_partial(state, lock, i, name=JOB_FILE):
    """生成打字机 partial 回调：把流式累计文本写进 steps[i]["partial"] 并落盘，
    距上次落盘不足 PARTIAL_THROTTLE 秒时只更新内存（chunk 太密，每片都写盘会抖）。"""
    last = [0.0]

    def cb(accumulated):
        now = time.monotonic()
        flush = now - last[0] >= PARTIAL_THROTTLE
        with lock:
            state["steps"][i]["partial"] = accumulated
            if flush:
                last[0] = now
                _write_job(state, name)

    return cb


def _clear_transient(step):
    """文件结束时清掉瞬态字段（打字机 partial；block_started 为旧版任务文件可能的残留）。"""
    step.pop("partial", None)
    step.pop("block_started", None)


def _run_job(files_data, api_key, model=None, base_url=None):
    """后台线程体：逐个分析文件（解析 + AI 分类，轻量），进度与结果实时落盘
    ingest_job.json。整理工序不在此执行——分类结果先交人工确认/改判，
    整理统一由入库任务按确认分类跑，避免框架选错时整篇白整理再重跑。
    PDF 逐块完成数累计全局 done/total 估 ETA（写入 state["blocks"] 供前端信息条
    显示「预计剩余」）；分析中的流式 partial 经节流落盘（打字机显示）。
    注意：线程内没有 ScriptRunContext，禁止调用任何 st.*；
    前端注入的 API Key、模型与端点同样取不到，由 api_key/model/base_url 参数显式带入。"""
    set_thread_api_key(api_key)
    set_thread_model(model)
    set_thread_base_url(base_url)
    state = {"status": "running", "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "",
             "steps": [{"file": n, "status": "waiting", "detail": ""} for n, _ in files_data],
             "results": []}
    _write_job(state)
    lock = threading.Lock()
    file_blocks = {}  # i -> {"total": 块数, "done": 已完成块数}（仅 PDF 有块概念）

    def _update(i, status, detail=""):
        with lock:
            state["steps"][i]["status"] = status
            state["steps"][i]["detail"] = detail
            if status in ("done", "error"):
                state["steps"][i]["frac"] = 1.0
                _clear_transient(state["steps"][i])
            _write_job(state)

    def _progress(i):
        """PDF 逐块转换进度回调：完成块数/总块数 + 引擎 + 全局块 ETA 实时落盘；
        块计数同时写 state["blocks"]，供前端信息条显示「预计剩余」。
        本任务只做到分类为止，解析是绝对大头：占单文件进度的 0-85%（frac）。"""
        def cb(info):
            if info.get("status") not in ("start", "done"):
                return
            with lock:
                pf = file_blocks.setdefault(i, {"total": info["total"], "done": 0})
                pf["total"] = info["total"]
                if info["status"] == "done":
                    pf["done"] = info.get("done") or pf["done"]
                state["steps"][i]["frac"] = round(
                    0.85 * min(pf["done"], pf["total"]) / pf["total"], 3) if pf["total"] else 0.0
                total = sum(f["total"] for f in file_blocks.values())
                done = sum(min(f["done"], f["total"]) for f in file_blocks.values())
                state["blocks"] = {"done": done, "total": total}
                eta = _eta_seconds(state["started"], done, total)
                if info["status"] == "start":
                    detail = f"共 {info['total']} 块，解析中…"
                else:
                    detail = f"块 {pf['done']}/{pf['total']} · {info['engine']}"
                if eta is not None:
                    detail += f" · 预计剩余{_fmt_eta(eta)}"
                state["steps"][i]["detail"] = detail
                _write_job(state)
        return cb

    def _stage(i):
        """阶段串联回调：分类阶段写进步骤 detail 并落盘（解析进度由 _progress 负责）。
        分类占单文件进度的 85-95%；之后由 _update 收尾到 100%。"""
        def cb(stage, *args):
            if stage != "classify":
                return
            with lock:
                state["steps"][i].pop("partial", None)
                state["steps"][i]["detail"] = "AI 分类中…"
                state["steps"][i]["frac"] = 0.9
                _write_job(state)
        return cb

    try:
        for i, (name, data) in enumerate(files_data):
            _update(i, "running")
            res = _analyze_one(name, data, progress_cb=_progress(i),
                               on_chunk=_throttled_partial(state, lock, i),
                               stage_cb=_stage(i), organize=False)
            state["results"].append(res)
            if res["ok"]:
                cat = config.CATEGORY_MAP.get(res["category_key"], ("其他",))[0]
                _update(i, "done", f"→ {cat} · {res['title'][:30]}")
            else:
                _update(i, "error", res["error"][:120])
        state["status"] = "done"
    except Exception:
        state["status"] = "error"
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_job(state)


def _start_job(files_data):
    """启动后台分析任务；已有任务在跑时返回 False（调用方负责显式提示）。"""
    global _job_thread
    if _job_running():
        return False
    try:
        _cache_uploads(files_data)  # 字节落盘缓存：应用重启后中断提示处可「重新发起分析」
    except OSError:
        pass  # 缓存失败不阻塞本次分析
    shutil.rmtree(_path(IMG_STAGE_DIR), ignore_errors=True)  # 新一批分析，旧图表暂存作废
    # 主线程取好当前会话的 API Key、模型与端点传给工作线程（线程内访问不了 session_state）
    _job_thread = threading.Thread(target=_run_job, args=(files_data, get_api_key(),
                                                          get_model(), get_base_url()),
                                   daemon=True)
    _job_thread.start()
    return True


def _save_running():
    return _save_thread is not None and _save_thread.is_alive()


def _run_save_job(plan, api_key, model=None, base_url=None):
    """后台入库线程体：逐文件按人工确认的分类（chosen）跑「按框架整理」工序
    （分段整理 + 合并去重，耗时大头），随后落盘写知识库；进度与结果实时落盘
    ingest_save_job.json。
    整理只在这里执行且只用确认后的框架——分析任务只做到分类为止，
    框架选错的代价止于一次轻量分类调用，不再整篇白整理。
    分析失败的文件（ok=False）不落盘写空壳，error 原样带进结果。
    无块概念，ETA 按文件粒度（已完成文件数/总文件数）；整理阶段的流式 partial
    与分段/合并子阶段进度同样落盘（打字机与进度条显示）。
    plan 各项为分析结果 dict + chosen（人工最终选择的分类）。"""
    set_thread_api_key(api_key)
    set_thread_model(model)
    set_thread_base_url(base_url)
    state = {"status": "running", "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "",
             "steps": [{"file": p["file"], "status": "waiting", "detail": ""} for p in plan],
             "results": []}
    _write_job(state, SAVE_JOB_FILE)
    lock = threading.Lock()

    def _files_eta():
        """按已完成文件数估剩余时间；第一个文件完成前（样本不足）返回 ""。"""
        done = sum(1 for s in state["steps"] if s["status"] in ("done", "error"))
        sec = _eta_seconds(state["started"], done, len(state["steps"]))
        return f"预计剩余{_fmt_eta(sec)}" if sec is not None else ""

    def _update(i, status, detail=""):
        with lock:
            state["steps"][i]["status"] = status
            state["steps"][i]["detail"] = detail
            if status in ("done", "error"):
                state["steps"][i]["frac"] = 1.0
                _clear_transient(state["steps"][i])
            _write_job(state, SAVE_JOB_FILE)

    def _stage(i):
        """入库子阶段串联：按框架整理(5-95%) → 合并去重(95%+)，
        写进 detail 与 frac；阶段切换时清掉上一阶段的打字机 partial。"""
        def cb(stage, *args):
            with lock:
                if stage == "organize":
                    frac = 0.05
                    detail = state["steps"][i]["detail"] or "按框架整理中…"
                    state["steps"][i].pop("partial", None)
                elif stage == "chunk":
                    n, total = args
                    frac = 0.05 + 0.9 * n / total if total else 0.05
                    base = state["steps"][i]["detail"].split(" · 已整理 ")[0]
                    detail = base + (f" · 已整理 {n}/{total} 段" if total > 1 else "")
                elif stage == "merge":
                    frac, detail = 0.97, "合并去重、对齐框架中…"
                    state["steps"][i].pop("partial", None)
                else:
                    return
                state["steps"][i]["detail"] = detail
                state["steps"][i]["frac"] = round(frac, 3)
                _write_job(state, SAVE_JOB_FILE)
        return cb

    try:
        for i, p in enumerate(plan):
            _update(i, "running", _files_eta())
            r = dict(p)
            try:
                chosen = p.get("chosen", p["category_key"])
                if p["ok"]:
                    if p.get("text"):
                        # 分析只做到分类：凡有原文的都在此按确认框架整理
                        # （唯一整理入口；框架选错的代价止于一次轻量分类调用）
                        cat_name = config.CATEGORY_MAP.get(chosen, ("其他", "", ""))[0]
                        rejudge = "分类改判，" if chosen != p["category_key"] else ""
                        eta_s = f" · {_files_eta()}" if _files_eta() else ""
                        stage_cb = _stage(i)
                        on_chunk = _throttled_partial(state, lock, i, SAVE_JOB_FILE)
                        _update(i, "running", f"{rejudge}按「{cat_name}」框架整理中…" + eta_s)
                        r.update(organize_document(p["file"], _trim_boilerplate(p["text"]),
                                                   chosen,
                                                   on_chunk=on_chunk,
                                                   chunk_cb=lambda n, t: stage_cb("chunk", n, t),
                                                   stage_cb=stage_cb,
                                                   images=p.get("images")))
                    r["category_key"] = chosen
                    r["path"] = save_document(r, p["file"])
                # ok=False（分析失败）：不落盘写空壳，error 原样进结果
            except Exception as e:
                r["ok"], r["error"] = False, str(e)
            for k in ("text", "content", "chosen", "note"):
                r.pop(k, None)
            state["results"].append(r)
            if r["ok"]:
                cat = config.CATEGORY_MAP.get(r["category_key"], ("其他",))[0]
                _update(i, "done", f"→ {cat}")
            else:
                _update(i, "error", r["error"][:120])
        state["status"] = "done"
    except Exception:
        state["status"] = "error"
    state["finished"] = datetime.now().isoformat(timespec="seconds")
    _write_job(state, SAVE_JOB_FILE)


def _start_save_job(plan):
    """启动后台入库任务；已有任务在跑时返回 False。"""
    global _save_thread
    if _save_running():
        return False
    try:
        _write_job({"plan": plan}, SAVE_PLAN_FILE)  # 计划落盘：重启后中断提示处可「重新发起入库」
    except OSError:
        pass  # 缓存失败不阻塞本次入库
    _save_thread = threading.Thread(target=_run_save_job, args=(plan, get_api_key(),
                                                                get_model(), get_base_url()),
                                    daemon=True)
    _save_thread.start()
    return True


# ==================== 页面 ====================

def _category_options():
    """分类下拉选项：[(label, category_key)]，空 key = 根目录（其他）。"""
    opts = [(f"{v[1]} {v[0]}", k) for k, v in config.CATEGORY_MAP.items()]
    opts.append(("📁 其他（根目录）", ""))
    return opts


def render_ingest(index, on_saved):
    import streamlit as st
    # 与 reader/battle/radar 一致：撑开主区，消除两侧留白
    st.markdown('<div class="page-wide-marker"></div>', unsafe_allow_html=True)
    st.markdown("<div class='doc-title'>📥 文件归档</div>", unsafe_allow_html=True)
    st.markdown("<div class='meta-line'>拖入 PDF / DOCX / TXT / MD，AI 读取内容、提议分类并整理成结构化文档；"
                "分类可人工改判，确认后写入知识库。</div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom:0.8rem'></div>", unsafe_allow_html=True)

    _STATUS_ICON = {"waiting": "⏸️", "running": "⏳", "done": "✅", "error": "❌"}

    def _render_job_steps(steps):
        """中断/恢复提示处的静态步骤列表（一次性渲染，非轮询热路径）。
        file/detail 为外部文本，拼 HTML 前必须转义。"""
        if not steps:
            st.markdown("<div class='caption'>正在启动任务…</div>", unsafe_allow_html=True)
            return
        for s in steps:
            icon = _STATUS_ICON.get(s.get("status"), "")
            detail = html.escape(str(s.get("detail") or ""))
            suffix = f"（{detail}）" if detail else ""
            st.markdown(f"<div class='caption'>{icon} {html.escape(str(s.get('file', '')))} "
                        f"{suffix}</div>", unsafe_allow_html=True)
            # 打字机：当前 running 文件的流式部分文本（末尾截断），等宽引用样式 + 光标
            partial = s.get("partial") or ""
            if s.get("status") == "running" and partial:
                tail = html.escape(partial[-PARTIAL_TAIL:])
                st.markdown(
                    "<div style='font-family:monospace; font-size:0.78rem; white-space:pre-wrap; "
                    "word-break:break-all; opacity:0.75; border-left:2px solid #ccc; "
                    "padding-left:0.6rem; margin:0.1rem 0 0.5rem 1.2rem'>"
                    + tail + " ▌</div>", unsafe_allow_html=True)

    def _live_status(banner, steps, started_iso):
        """把任务实时状态（横幅 + 进度条 + 步骤）合成纯文本，交给单个 st.text 渲染：
        每次轮询只原地更新一个文本节点，没有任何 HTML 解析和子节点增删——动态 HTML
        重解析导致 React 虚拟 DOM 与真实 DOM 脱节，正是此前 removeChild 崩溃的根源。
        进度条取各步骤 frac（子阶段进度分数：解析 0-30% / 分类 30-40% /
        整理 40-95% / 合并 95-100%）的均值，随子阶段平滑推进，不再按文件粒度跳变；
        已用时间由 started 时间戳实时算出，配合进度条一起走。"""
        lines = [banner, ""]
        if not steps:
            lines.append("正在启动任务…")
            return "\n".join(lines)
        done = sum(1 for s in steps if s.get("status") in ("done", "error"))
        fracs = []
        for s in steps:
            f = s.get("frac")
            if f is None:  # 旧版任务文件没有 frac：退化为文件粒度
                f = 1.0 if s.get("status") in ("done", "error") else 0.0
            fracs.append(min(max(float(f), 0.0), 1.0))
        pct = round(sum(fracs) / len(fracs) * 100)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(f"文件进度 {done}/{len(steps)}  {bar} {pct}%{_fmt_elapsed(started_iso)}")
        for s in steps:
            icon = _STATUS_ICON.get(s.get("status"), "")
            detail = str(s.get("detail") or "")
            suffix = f"（{detail}）" if detail else ""
            lines.append(f"{icon} {s.get('file', '')} {suffix}")
        return "\n".join(lines)

    def _render_typewriter(steps):
        """当前 running 文件的 AI 流式输出，放进固定高度可滚动窗口：
        打字机实时滚动且窗口本身位置固定，后续「分类确认/入库」操作区不会被顶跑。
        容器 + 内部单 st.text 是稳定节点结构（轮询热路径只更新文本、不增删节点）。"""
        live_partial = next((s.get("partial") or "" for s in steps
                             if s.get("status") == "running" and s.get("partial")), "")
        with st.container(height=320, border=True):
            st.text(live_partial[-3000:] if live_partial
                    else "（AI 输出流将实时显示在这里，窗口内可上下滚动）")

    @st.fragment(run_every=3)
    def _render_job_live():
        """任务运行中每 3 秒轮询 ingest_job.json 刷新进度。
        固定挂载 + 稳定节点结构（状态条与打字机均为文本原地更新，不增删节点），
        结束边沿按 started 时间戳去重、只触发一次整页 rerun——
        三者共同规避前端 React removeChild 竞态。"""
        j = _read_job()
        if not j:
            return
        if j.get("status") == "running" and not _job_running():
            return  # 线程不在（中断/重启）：交给主流程的中断提示分支
        if j.get("status") != "running":
            token = j.get("started", "")
            if st.session_state.get("_live_fired_ingest") != token:
                st.session_state["_live_fired_ingest"] = token
                st.rerun()  # 整页 rerun，由主流程导入结果
            return
        steps = j.get("steps", [])
        st.text(_live_status(
            f"⏳ 后台解析与分类进行中（开始于 {j.get('started', '—')}）{_live_eta(j, steps)}。"
            "整理将在你确认分类后执行；进度实时落盘，可自由切换页面/刷新。",
            steps, j.get("started", "")))
        _render_typewriter(steps)

    @st.fragment(run_every=3)
    def _render_save_live():
        """后台入库（含改判重整）轮询 ingest_save_job.json。
        固定挂载 + 稳定节点结构（状态条与打字机均为文本原地更新，不增删节点），
        结束边沿按 started 时间戳去重、只触发一次整页 rerun。"""
        j = _read_job(SAVE_JOB_FILE)
        if not j:
            return
        if j.get("status") == "running" and not _save_running():
            return  # 线程不在（中断/重启）：交给主流程的中断提示分支
        if j.get("status") != "running":
            token = j.get("started", "")
            if st.session_state.get("_live_fired_ingest_save") != token:
                st.session_state["_live_fired_ingest_save"] = token
                st.rerun()  # 整页 rerun，由主流程导入结果
            return
        steps = j.get("steps", [])
        st.text(_live_status(
            "💾 后台入库中（按你确认的框架整理成文档，可能耗时较长）"
            f"{_live_eta(j, steps)}。", steps, j.get("started", "")))
        _render_typewriter(steps)

    running = _job_running()
    save_running = _save_running()

    # 预读输出区内容，决定输出框是否渲染（无内容时不显示空框）
    job = _read_job()
    save_job = _read_job(SAVE_JOB_FILE)
    analysis = st.session_state.get("ingest_analysis") or []
    results_done = st.session_state.get("ingest_results") or []
    has_output = (running or save_running or bool(analysis) or bool(results_done)
                  or bool(job and job.get("status")) or bool(save_job and save_job.get("status")))

    # ---- 大框：包住上部拖入区与下部输出区 ----
    outer = st.container(border=True)
    with outer:
        # ---- 上部：拖入区（白框样式见 app.py CSS，限制说明文字被包进框内）----
        files = st.file_uploader("拖入文件（可多选）",
                                 type=[e.lstrip(".") for e in sorted(SUPPORTED_EXT)],
                                 accept_multiple_files=True)
        if files:
            total_mb = sum(getattr(f, "size", 0) for f in files) / 1048576
            st.markdown(f"<div class='caption'>已选 {len(files)} 个文件 · 共 {total_mb:.1f} MB</div>",
                        unsafe_allow_html=True)
            if st.button("🔍 开始分析", type="primary", disabled=running or save_running,
                         help="后台执行：进度实时落盘，可自由切换页面/刷新"):
                files_data = [(f.name, f.read()) for f in files]
                st.session_state.pop("ingest_analysis", None)
                st.session_state.pop("ingest_results", None)
                if _start_job(files_data):
                    st.rerun()
                else:
                    st.warning("已有入库任务进行中，请等待完成后再发起。")

        # ---- 下部：输出区（固定高度 560px 不随内容伸缩，超出框内滚动；常驻占位）----
        out_box = st.container(border=True, height=560)
        with out_box:
            if not has_output:
                # 空态占位文案（顶部显示；框高由 height=560 定死，不再靠占位撑高）
                st.markdown("<div class='caption' style='color:var(--kb-text-3)'>"
                            "输出区：分析进度与结果将显示在这里。</div>",
                            unsafe_allow_html=True)
            # ---- 后台分析任务状态：进行中轮询 / 中断提示 / 完成后导入分析结果 ----
            _render_job_live()  # 固定挂载，内部按状态决定是否渲染
            if running:
                return
            if job and job.get("status") == "running":
                # 任务文件仍是 running 但线程已不在：应用曾被重启/进程被杀，任务中断
                st.warning("上次后台分析被中断（应用重启或进程退出），结果可能不完整。")
                _render_job_steps(job.get("steps", []))
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("🔄 重新发起分析", key="ingest_restart",
                                 help="用上次的文件缓存重新分析（缓存自点「开始分析」时自动生成）"):
                        cached = _load_cached_uploads()
                        if not cached:
                            st.error("未找到上次上传的文件缓存，请重新拖入文件。")
                        elif _start_job(cached):
                            st.rerun()
                        else:
                            st.warning("已有分析任务进行中，请稍候。")
                with c2:
                    if st.button("🗑 清除中断记录", key="ingest_clear_stale"):
                        try:
                            os.remove(_path(JOB_FILE))
                        except OSError:
                            pass
                        _clear_upload_cache()
                        shutil.rmtree(_path(IMG_STAGE_DIR), ignore_errors=True)
                        st.rerun()
            elif job and job.get("status") in ("done", "error"):
                results = job.get("results") or []
                try:
                    os.remove(_path(JOB_FILE))
                except OSError:
                    pass
                _clear_upload_cache()  # 分析结果已被取走，上传缓存使命完成
                if results:
                    st.session_state["ingest_analysis"] = results
                    st.rerun()
                else:
                    st.error("后台分析失败，未产出结果。")

            # ---- 后台入库任务状态：进行中轮询 / 中断提示 / 完成后导入入库结果 ----
            _render_save_live()  # 固定挂载，内部按状态决定是否渲染
            if save_running:
                return
            if save_job and save_job.get("status") == "running":
                st.warning("上次后台入库被中断（应用重启或进程退出），结果可能不完整。")
                _render_job_steps(save_job.get("steps", []))
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("🔄 重新发起入库", key="save_restart",
                                 help="用上次的入库计划重新执行（计划自点「确认入库」时自动落盘）"):
                        plan = (_read_job(SAVE_PLAN_FILE) or {}).get("plan") or []
                        if not plan:
                            st.error("未找到上次的入库计划缓存，请重新分析文件。")
                        elif _start_save_job(plan):
                            st.rerun()
                        else:
                            st.warning("已有入库任务进行中，请稍候。")
                with c2:
                    if st.button("🗑 清除中断记录", key="save_clear_stale"):
                        for fn in (SAVE_JOB_FILE, SAVE_PLAN_FILE):
                            try:
                                os.remove(_path(fn))
                            except OSError:
                                pass
                        shutil.rmtree(_path(IMG_STAGE_DIR), ignore_errors=True)
                        st.rerun()
            elif save_job and save_job.get("status") in ("done", "error"):
                final = save_job.get("results") or []
                for fn in (SAVE_JOB_FILE, SAVE_PLAN_FILE):  # 入库结果被取走，计划缓存一并清掉
                    try:
                        os.remove(_path(fn))
                    except OSError:
                        pass
                if final:
                    st.session_state["ingest_results"] = final
                    if any(r["ok"] for r in final):
                        on_saved()  # 刷新索引，侧栏分类计数与搜索即时可见
                    st.rerun()
                else:
                    st.error("后台入库失败，未产出结果。")

            # ---- 阶段一结果：AI 提议分类，人工可改判 ----
            if analysis:
                opts = _category_options()
                labels = [l for l, _ in opts]
                n_ok = sum(1 for r in analysis if r["ok"])
                st.markdown(f"<div class='section-header'>分类完成：{n_ok} 可归档 / {len(analysis)} 总计"
                            "——确认或改判框架后，按确认框架整理入库（整理只跑一次）</div>",
                            unsafe_allow_html=True)
                for i, r in enumerate(analysis):
                    if not r["ok"]:
                        st.warning(f"❌ {r['file']}：{r['error']}")
                        continue
                    default_label = next((l for l, k in opts if k == r["category_key"]), labels[-1])
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        # title/summary/note/file 均为 LLM/外部文本：必须 html.escape 后再拼 HTML，
                        # 否则一个 < 或未闭合引号就会让真实 DOM 与 React 预期树脱节（removeChild 崩溃）
                        note_line = (f"<div class='caption'>⚙️ {html.escape(str(r['note']))}</div>"
                                     if r.get("note") else "")
                        st.markdown(f"<div class='card'><div class='meta-line'>📄 "
                                    f"{html.escape(str(r['file']))}</div>"
                                    f"<div style='font-weight:600; margin-top:0.2rem'>"
                                    f"{html.escape(str(r['title']))}</div>"
                                    f"<div class='caption'>{html.escape(str(r['summary']))}</div>"
                                    f"{note_line}</div>",
                                    unsafe_allow_html=True)
                    with c2:
                        st.selectbox("分类（可改判）", labels, index=labels.index(default_label),
                                     key=f"ingest_cat_{i}")
                if st.button("💾 确认分类，整理入库", type="primary", key="ingest_confirm",
                             help="按上面确认/改判后的框架逐篇整理并写入知识库，"
                                  "整理耗时较长，后台执行不阻塞页面"):
                    label_to_key = dict(opts)
                    # 组装入库计划交后台线程：改判分类的重整（可能耗时较长）不再卡主线程
                    plan = []
                    for i, r in enumerate(analysis):
                        if not r["ok"]:
                            plan.append(r)
                            continue
                        chosen = label_to_key.get(st.session_state.get(f"ingest_cat_{i}"),
                                                  r["category_key"])
                        plan.append(dict(r, chosen=chosen))
                    st.session_state.pop("ingest_analysis", None)
                    if _start_save_job(plan):
                        st.rerun()
                    else:
                        st.warning("已有入库任务进行中，请等待完成后再发起。")

            # ---- 阶段二结果：已入库，02_deals 可一键发起评审 ----
            results = results_done
            if results:
                n_ok = sum(1 for r in results if r["ok"])
                st.markdown(f"<div class='section-header'>本次归档：{n_ok} 成功 / {len(results)} 总计</div>",
                            unsafe_allow_html=True)
                for j, r in enumerate(results):
                    if r["ok"]:
                        name, icon, _ = config.CATEGORY_MAP.get(r["category_key"], ("其他", "📁", ""))
                        rel = os.path.relpath(r["path"], config.KNOWLEDGE_DIR).replace(os.sep, "/")
                        st.markdown(
                            f"<div class='card'><div class='meta-line'>{icon} {name} · "
                            f"{html.escape(rel)}</div>"
                            f"<div style='font-weight:600; margin-top:0.2rem'>"
                            f"{html.escape(str(r['title']))}</div>"
                            f"<div class='caption'>{html.escape(str(r['summary']))}</div></div>",
                            unsafe_allow_html=True)
                        if r["category_key"] == "02_deals":
                            if st.button("🧭 发起评审", key=f"ingest_review_{j}",
                                         help="跳到新项目评审页并自动选中该文档"):
                                st.session_state.review_preselect_path = rel
                                st.session_state.view_mode = "compare"
                                st.session_state.selected_doc = None
                                st.rerun()
                    else:
                        st.warning(f"❌ {r['file']}：{r['error']}")
                st.markdown("<div class='caption'>已入库的文档可在左侧分类或搜索中查看。</div>",
                            unsafe_allow_html=True)


# ==================== CLI ====================

def main():
    ap = argparse.ArgumentParser(description="文件归档：读取 → AI 分类整理 → 写入知识库")
    ap.add_argument("files", nargs="+", help="待归档文件（pdf/docx/txt/md）")
    ap.add_argument("--dry-run", action="store_true", help="只提取整理打印，不写入知识库")
    args = ap.parse_args()
    out = []
    for path in args.files:
        with open(path, "rb") as f:
            data = f.read()
        if args.dry_run:
            try:
                text = extract_text(os.path.basename(path), data)
                meta = classify_document(os.path.basename(path), text)
                org = organize_document(os.path.basename(path), _trim_boilerplate(text),
                                        meta["category_key"])
                out.append({"file": path, **meta, "content_chars": len(org["content"])})
            except Exception as e:
                out.append({"file": path, "error": str(e)})
        else:
            out.append(ingest_one(os.path.basename(path), data))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
