# -*- coding: utf-8 -*-
"""
PDF → Markdown 自研编排层（多级回滚链，逐块粒度）。

回滚链（按可用性自动探测，逐块独立生效）：
1. VLM 主力档：当前注入链解析出的模型在 config.PROVIDERS 中 vision=True 且有 api_key 时启用。
   pymupdf 把每块页面渲染成 PNG（150dpi，纯本地），组装 OpenAI 兼容多模态 messages
   （image_url base64 data URL）POST {base_url}/chat/completions，逐块转 Markdown。
   各厂家 vision 消息格式差异的适配集中在 _build_vlm_payload() 一处。
2. pymupdf4llm（本地）：to_markdown() 保留标题层级 / Markdown 表格；
3. pdfplumber（本地）：文字层 + extract_tables() 转 Markdown 表格；
4. pypdf 兜底（纯文字流，裸环境保证可用）。

大 PDF 切分：先 pypdf 廉价抽文字层，按章/节标题正则找语义切点，按目标引擎定块大小：
VLM 可用时按 KB_VLM_CHUNK_PAGES（默认 5）切（请求体小、超时风险低），
本地档按 KB_PDF_CHUNK_PAGES（默认 10）切；找不到切点均匀切。
速度优化（简历解析软件同款逻辑：文本层优先，视觉模型只兜底扫描页）：
抽出的文字层同时用于分流——块内平均字符/页 ≥ KB_TEXT_LAYER_MIN（默认 100）判有文本层，
直接本地解析（毫秒级），不请求 VLM；仅扫描/图片块走 VLM，
且 VLM 块 ThreadPoolExecutor 并发（KB_VLM_WORKERS 默认 3，各块内仍 429/5xx 退避重试）。
每块独立走回滚链；单块全链失败记 degraded 并用 pypdf 文字层保底（哪怕是文字流也保留，不丢内容）。
合并时块首注入页码锚点注释 <!-- p.41-50 · engine:vlm -->。
另：转换同时纯本地截取重要图表（嵌入位图 bbox + 矢量图区截图，面积阈值滤 logo，
md5 去重，KB_IMG_MAX 上限），随结果 "images" 返回，由调用方落盘并在文档里引用。

入口：
    from convert import convert_pdf
    out = convert_pdf("研报.pdf", data)        # -> {"md", "blocks", "degraded_pages"}
CLI：
    python convert.py <file> > out.md          # 进度走 stderr，md 走 stdout
"""
import argparse
import base64
import io
import json
import os
import pickle
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 允许直接运行本文件（CLI）时从仓库根 import llm/config
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

CHUNK_PAGES = int(os.environ.get("KB_PDF_CHUNK_PAGES", "10") or "10")  # 本地档每块页数上限
VLM_CHUNK_PAGES = int(os.environ.get("KB_VLM_CHUNK_PAGES", "5") or "5")  # VLM 档每块页数上限
VLM_DPI = 150          # VLM 档页面渲染分辨率（字节流 PNG，不落盘）
VLM_MAX_TOKENS = 8000  # 单块转换输出上限
VLM_RETRIES = 3        # VLM 单块 429/5xx/超时 指数退避重试次数
# requests 超时元组（连接 15s、读取 180s）：替代旧的单值 600s——单块最坏耗时
# ≈ 180s×VLM_RETRIES + 退避（2s+4s）≈ 10 分钟，超限落下一档，不再出现 30 分钟级卡死
VLM_TIMEOUT = (15, 180)
# 文本层分流阈值（块内平均字符/页，env KB_TEXT_LAYER_MIN 可配）：达到即判「有文本层」
# 走本地解析（毫秒级）；低于才走 VLM——文本层优先，视觉模型只兜底扫描/图片页
TEXT_LAYER_MIN = int(os.environ.get("KB_TEXT_LAYER_MIN", "100") or "100")
VLM_WORKERS = int(os.environ.get("KB_VLM_WORKERS", "3") or "3")  # VLM 块并发数（本地块不占）
# 重要图表自动截取（纯本地 pymupdf，不调模型）：图区面积占页面比例下限（滤 logo/图标）、
# 单文档截图上限（0 = 关闭）、截图分辨率
IMG_MIN_FRAC = float(os.environ.get("KB_IMG_MIN_FRAC", "0.06") or "0.06")
IMG_MAX = int(os.environ.get("KB_IMG_MAX", "20") or "20")
IMG_DPI = int(os.environ.get("KB_IMG_DPI", "150") or "150")

VLM_PROMPT = """请把这份 PDF 页面图片忠实转录为 Markdown：
- 保留标题层级（# / ## / ###）；
- 表格转 Markdown 表格，单元格不要丢列；
- 图表给出简要文字解读（标题、坐标轴、关键数值与趋势）；
- 公式转 LaTeX（$...$ / $$...$$）；
- 页眉页脚、页码、免责声明等噪声直接丢弃；
- 逐页顺序输出，不要总结、不要评论，不要用 ``` 代码块包裹。"""

# 语义切点：研报常见章/节标题模式（页首若干行内匹配即记为切点）
_BREAK_RES = [
    re.compile(r"^第[一二三四五六七八九十百零\d]+[章节篇部分]"),
    re.compile(r"^#{1,6}\s+\S"),
    re.compile(r"^[一二三四五六七八九十]+、\S"),
    re.compile(r"^\d+(\.\d+){0,2}[、.．\s]\s*\S"),
]
_DOT_LEADER_RE = re.compile(r"(\.{4,}|…{2,}|·{4,})\s*\d+\s*$")  # 目录点线行不算标题


class _EngineUnavailable(Exception):
    """该档依赖的库/条件不满足，回滚链直接跳过（不算失败）。"""


# ==================== 原生调用子进程隔离 ====================
# pymupdf 的 mupdfcpp64.dll 在部分 PDF 上会段错误（0xc0000005），直接把
# Streamlit 主进程带崩（前端「连接错误」）。所有 pymupdf 触点统一走
# _run_native() 放进独立子进程：子进程崩了只损失这一档，回滚链照常走下一档。

import types as _types

_NATIVE_TIMEOUT = int(os.environ.get("KB_PDF_NATIVE_TIMEOUT", "600") or "600")


def _render_pages_png(data, start, end, dpi):
    """pymupdf 渲染页区间为 PNG 字节列表（在子进程里执行）。"""
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return [doc[p].get_pixmap(dpi=dpi).tobytes("png") for p in range(start, end)]
    finally:
        doc.close()


def _native_job_child(fn_name):
    """子进程入口：stdin 读 pickle 参数 → 执行原生函数 → 结果/异常 pickle 写 stdout。"""
    args = pickle.load(sys.stdin.buffer)
    try:
        payload = ("ok", globals()[fn_name](*args))
    except BaseException as e:
        payload = ("err", f"{type(e).__name__}: {e}")
    sys.stdout.buffer.write(pickle.dumps(payload))
    sys.stdout.buffer.flush()


def _run_native(fn_name, *args):
    """原生库调用放进独立子进程执行，返回其结果。
    子进程段错误/超时/异常都转成 RuntimeError 或 _EngineUnavailable，由回滚链处理。
    实现用 subprocess 而非 multiprocessing：spawn 在无控制台句柄的环境（Git Bash /
    部分服务场景）DuplicateHandle 必炸 WinError 6，subprocess  stdin/stdout 管道稳定。
    测试用 mock 替换本模块函数后不是纯函数对象，不过子进程、直接进程内调用，
    保证 mock 语义不变。"""
    fn = globals()[fn_name]
    if not isinstance(fn, _types.FunctionType):  # 测试 mock 注入：进程内直接跑
        return fn(*args)
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--native-job", fn_name],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)  # stderr 继承：原生库告警可见
    try:
        out, _ = proc.communicate(pickle.dumps(args), timeout=_NATIVE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"{fn_name} 子进程超时（{_NATIVE_TIMEOUT}s），已跳过该档")
    if proc.returncode != 0 or not out:
        raise RuntimeError(
            f"{fn_name} 子进程崩溃（rc={proc.returncode}），已跳过该档")
    status, payload = pickle.loads(out)
    if status == "err":
        msg = str(payload)
        if msg.startswith("_EngineUnavailable:"):  # 保留「依赖缺失」语义，回滚链跳过而非记失败
            raise _EngineUnavailable(msg.split(":", 1)[1].strip())
        raise RuntimeError(f"{fn_name} 子进程报错：{msg}")
    return payload


# ==================== 切分 ====================

def _find_breaks(page_texts):
    """找语义切点：每页页首若干行命中章节标题正则的页码（0-based）集合。"""
    breaks = set()
    for i, text in enumerate(page_texts):
        for line in (text or "").split("\n")[:6]:
            line = line.strip()
            if not line or len(line) > 40 or _DOT_LEADER_RE.search(line):
                continue
            if any(r.match(line) for r in _BREAK_RES):
                breaks.add(i)
                break
    return breaks


def _plan_chunks(n_pages, breaks, max_pages):
    """按语义边界切块：不超过 max_pages 的前提下取窗口内最后一个切点；
    窗口内无切点则均匀切。返回 [(start, end)]，页码 0-based 左闭右开。"""
    if n_pages <= 0:
        return []
    max_pages = max(1, max_pages)
    chunks = []
    start = 0
    while start < n_pages:
        limit = min(start + max_pages, n_pages)
        cands = [b for b in breaks if start < b < limit]
        cut = max(cands) if cands else limit
        chunks.append((start, cut))
        start = cut
    return chunks


def _has_text_layer(page_texts, start, end):
    """块内页面平均字符数 ≥ TEXT_LAYER_MIN 判有文本层：本地解析即可（毫秒级），
    不烧 VLM；否则判扫描/图片页走 VLM。空白页（0 字符）必然进 VLM 档。"""
    n = max(1, end - start)
    return sum(len(t or "") for t in page_texts[start:end]) / n >= TEXT_LAYER_MIN


# ==================== 各档单块转换 ====================

def _chunk_vlm(data, start, end, *, model, api_key, base_url):
    """VLM 档：pymupdf 渲染块内页面为 PNG（子进程隔离）→ OpenAI 兼容多模态请求。
    429/5xx/超时/断连 指数退避最多 VLM_RETRIES 次；其余错误直接抛（落下一档）。"""
    try:
        import pymupdf  # noqa: F401  可用性检查；实际渲染在子进程里做
    except ImportError:
        raise _EngineUnavailable("未安装 pymupdf")
    import requests
    pngs = _run_native("_render_pages_png", data, start, end, VLM_DPI)
    images = [base64.b64encode(p).decode("ascii") for p in pngs]
    payload = _build_vlm_payload(model, images)
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in range(VLM_RETRIES):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=VLM_TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RuntimeError(f"可重试错误 {resp.status_code}：{resp.text[:200]}")
            if resp.status_code != 200:
                # 4xx（非 429）多为参数/权限问题，重试无意义，直接落下一档
                raise _EngineUnavailable(f"VLM 请求被拒 {resp.status_code}：{resp.text[:200]}")
            data_json = resp.json()
            msg = (data_json.get("choices") or [{}])[0].get("message") or {}
            return msg.get("content") or ""
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            err = e
        except RuntimeError as e:
            err = e
        if attempt < VLM_RETRIES - 1:
            time.sleep(2 ** attempt * 2)  # 指数退避 2s/4s（块内串行；并发上限由 VLM_WORKERS 约束）
    raise RuntimeError(f"VLM 块转换失败（重试 {VLM_RETRIES} 次）：{err}")


def _build_vlm_payload(model, images_b64):
    """组装 OpenAI 兼容多模态请求体。各厂家 vision 消息格式差异的适配集中在
    本函数一处（新增厂家时在此按 model/base_url 分支调整）。
    默认格式对 Moonshot/智谱/豆包/通义/OpenAI 等 OpenAI 兼容端点通用：
    content 为 text + image_url(base64 data URL) 列表。"""
    content = [{"type": "text", "text": VLM_PROMPT}]
    content += [{"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}}
                for b64 in images_b64]
    return {"model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": VLM_MAX_TOKENS}


def _chunk_pymupdf4llm(data, start, end):
    """pymupdf4llm 档：保留标题层级与 Markdown 表格。"""
    try:
        import pymupdf
        import pymupdf4llm
    except ImportError:
        raise _EngineUnavailable("未安装 pymupdf4llm")
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return pymupdf4llm.to_markdown(doc, pages=list(range(start, end)))
    finally:
        doc.close()


def _md_table(rows):
    """pdfplumber extract_tables() 的二维表 → Markdown 表格。"""
    rows = [[("" if c is None else str(c)).replace("\n", " ").replace("|", "\\|").strip()
             for c in row] for row in rows if row]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join([" --- "] * width) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _chunk_pdfplumber(data, start, end):
    """pdfplumber 档：文字层 + extract_tables() 转 Markdown 表格。"""
    try:
        import pdfplumber
    except ImportError:
        raise _EngineUnavailable("未安装 pdfplumber")
    parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[start:end]:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
            for tbl in page.extract_tables():
                md = _md_table(tbl)
                if md:
                    parts.append(md)
    return "\n\n".join(parts)


def _chunk_pypdf(page_texts, start, end):
    """pypdf 兜底：纯文字流（复用切分阶段已抽好的文字层，零开销）。"""
    return "\n".join(page_texts[start:end])


# ==================== 重要图表截取 ====================

def _merge_rects(rects):
    """重叠图区并集合并（按面积从大到小一遍扫，小区域被大区域吞并即可，不求最优）。"""
    merged = []
    for r in sorted(rects, key=lambda x: x.get_area(), reverse=True):
        for m in merged:
            if r.intersects(m):
                m |= r
                break
        else:
            merged.append(r)
    return merged


def _overlap_frac(r, tr):
    """r 被 tr 覆盖的面积比例（0-1）。"""
    inter = r & tr
    if inter.is_empty or r.is_empty:
        return 0.0
    return inter.get_area() / r.get_area()


def _table_rects(page, pymupdf):
    """页面内检测到的表格区域：表格在文本层已转成 md 表格（pymupdf4llm 负责），
    再截图就是重复且丢失可拷贝性——截图流程用这份清单跳过表格区域。"""
    try:
        return [pymupdf.Rect(t.bbox) for t in page.find_tables().tables]
    except Exception:
        return []


def _extract_images(data):
    """截取 PDF 中的重要图表为 PNG（纯本地 pymupdf，不调模型）。
    两类图区：
    - 嵌入位图（get_image_info 拿 bbox）：照片/扫描插图，面积占页 ≥ IMG_MIN_FRAC 才算重要；
    - 矢量图表（研报/论文图表多为矢量）：get_drawings 路径数 ≥ 30 且并集面积 ≥ 15% 页，
      对整个并集区域截图——位图提取拿不到矢量图，截图是通用兜底。
    区域重叠先合并；按内容 md5 去重（每页重复的 logo 只留一张）；每页最多 2 张、
    全文档最多 IMG_MAX 张。无 pymupdf（裸环境）返回 []，不影响主链。
    返回 [{"page": 1-based, "name": "p12_1.png", "data": png_bytes}]。"""
    if IMG_MAX <= 0:
        return []
    try:
        import pymupdf
    except ImportError:
        return []
    import hashlib
    out, seen = [], set()
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for pno in range(len(doc)):
            if len(out) >= IMG_MAX:
                break
            page = doc[pno]
            page_area = page.rect.get_area() or 1
            regions = []
            tabs = None  # 表格区域清单（惰性检测，见下方截图循环）
            for info in page.get_image_info():
                r = pymupdf.Rect(info["bbox"])
                if not r.is_empty and r.get_area() / page_area >= IMG_MIN_FRAC:
                    regions.append(r)
            try:
                drawings = page.get_drawings()
            except Exception:
                drawings = []
            if len(drawings) >= 30:  # 路径足够多才像图表，排除零星装饰线/下划线
                u = None
                for d in drawings:
                    r = d.get("rect")
                    if r is not None and not r.is_empty:
                        u = r if u is None else (u | r)
                if u is not None and u.get_area() / page_area >= 0.15:
                    regions.append(u)
            for r in _merge_rects(regions)[:2]:
                if len(out) >= IMG_MAX:
                    break
                r = r & page.rect  # 截到页面范围内（矢量图 bbox 偶尔越界）
                if r.is_empty:
                    continue
                if tabs is None:  # 惰性检测：只对有候选图区的页跑 find_tables
                    tabs = _table_rects(page, pymupdf)
                if any(_overlap_frac(r, tr) >= 0.5 for tr in tabs):
                    continue  # 表格区域：文本层已带 md 表格，不再截图
                png = page.get_pixmap(clip=r, dpi=IMG_DPI).tobytes("png")
                h = hashlib.md5(png).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                out.append({"page": pno + 1, "name": f"p{pno + 1}_{len(out) + 1}.png",
                            "data": png})
    finally:
        doc.close()
    return out


# ==================== 统一入口 ====================

def _vlm_available(api_key, model):
    """VLM 档可用性：有 key 且注入链解析出的模型在 PROVIDERS 中 vision=True。
    返回 (可用?, base_url, 不可用原因)。"""
    if not api_key:
        return False, "", "未配置 API Key"
    try:
        from llm import get_base_url, _match_provider
    except ImportError:
        return False, "", "无法加载 llm 注入链"
    base_url = get_base_url()
    _, preset = _match_provider(model, base_url)
    if not preset:
        return False, base_url, "厂家未收录，无法确认视觉能力"
    if not preset.get("vision"):
        return False, base_url, f"当前厂家（{preset.get('label', '?')}）预设模型不支持视觉"
    return True, base_url, ""


def _convert_chunk(data, page_texts, start, end, chain, *, model, api_key, base_url,
                   progress_cb=None, ci=0, total=0):
    """单块走回滚链，返回 (engine, text, err)；engine=None 表示全链失败（调用方保底）。
    档级失败经 progress_cb 发 fail 事件（done=None，不计入完成数）。"""
    err = None
    for eng in chain:
        try:
            if eng == "vlm":
                text = _chunk_vlm(data, start, end, model=model,
                                  api_key=api_key, base_url=base_url)
            elif eng == "pymupdf4llm":
                text = _run_native("_chunk_pymupdf4llm", data, start, end)
            elif eng == "pdfplumber":
                text = _chunk_pdfplumber(data, start, end)
            else:
                text = _chunk_pypdf(page_texts, start, end)
        except _EngineUnavailable as e:
            err = str(e)  # 依赖缺失/请求被拒：跳过本档
            continue
        except Exception as e:
            err = f"{eng} 档失败：{e}"
            if progress_cb:
                progress_cb({"block": ci + 1, "done": None, "total": total,
                             "pages": f"{start + 1}-{end}", "engine": eng, "status": "fail"})
            continue
        if text and text.strip():
            return eng, text, err
        err = f"{eng} 档输出为空"
    return None, "", err


def convert_pdf(filename, data, *, api_key=None, model=None, progress_cb=None):
    """PDF 字节 → Markdown。逐块走回滚链（文本层分流：有文本层的块本地解析，
    扫描/图片块 VLM 优先 → pymupdf4llm → pdfplumber → pypdf），VLM 块并发。
    api_key/model 未显式传入时走 llm 注入链解析；progress_cb(info dict) 回调：
    开始时一次 {"status": "start", "done": 0, "total"}；每块完成一次
    {"status": "done", "block", "done": 已完成块数, "total", "pages", "engine"}；
    档级失败 {"status": "fail", "done": None, ...}（前端只统计 done）。
    返回 {"md": str, "blocks": [{"pages", "engine", "ok", "error"}],
    "degraded_pages": [1-based], "images": [{"page", "name", "data"}]（重要图表截图）}。"""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    n_pages = len(reader.pages)
    # 两遍走之第一遍：pypdf 廉价抽文字层，既供语义切分/文本层分流，也作 pypdf 档输出
    page_texts = [(p.extract_text() or "") for p in reader.pages]

    if api_key is None or model is None:
        try:
            from llm import get_api_key, get_model
            api_key = get_api_key() if api_key is None else api_key
            model = get_model() if model is None else model
        except ImportError:
            api_key, model = api_key or "", model or ""
    use_vlm, base_url, _ = _vlm_available(api_key, model)
    # 切分在可用性判定之后：块大小按目标引擎决定（VLM 档块小，本地档块大）
    chunks = _plan_chunks(n_pages, _find_breaks(page_texts),
                          VLM_CHUNK_PAGES if use_vlm else CHUNK_PAGES)
    total = len(chunks)
    # 文本层分流：有文本层的块不请求 VLM（本地毫秒级），扫描/图片块才走 VLM
    local_chain = ["pymupdf4llm", "pdfplumber", "pypdf"]
    chains = [(local_chain if _has_text_layer(page_texts, s, e) else ["vlm"] + local_chain)
              if use_vlm else local_chain for s, e in chunks]

    results = [None] * total  # ci -> (engine, text, err)，按块号落位，并发不乱序
    done_n = [0]

    def _run(ci):
        start, end = chunks[ci]
        results[ci] = _convert_chunk(data, page_texts, start, end, chains[ci],
                                     model=model, api_key=api_key, base_url=base_url,
                                     progress_cb=progress_cb, ci=ci, total=total)

    def _emit_done(ci):
        done_n[0] += 1
        if progress_cb:
            start, end = chunks[ci]
            progress_cb({"block": ci + 1, "done": done_n[0], "total": total,
                         "pages": f"{start + 1}-{end}", "engine": results[ci][0],
                         "status": "done"})

    if progress_cb:
        progress_cb({"block": 0, "done": 0, "total": total,
                     "pages": "", "engine": "", "status": "start"})
    vlm_idxs = [i for i, c in enumerate(chains) if c[0] == "vlm"]
    for ci in [i for i, c in enumerate(chains) if c[0] != "vlm"]:
        _run(ci)  # 本地块毫秒级，顺序跑即可
        _emit_done(ci)
    if vlm_idxs:  # VLM 块并发：done 事件只在主循环 as_completed 里发，无需额外锁
        with ThreadPoolExecutor(max_workers=min(VLM_WORKERS, len(vlm_idxs))) as ex:
            futs = {ex.submit(_run, ci): ci for ci in vlm_idxs}
            for fut in as_completed(futs):
                _emit_done(futs[fut])

    blocks, degraded, md_parts = [], [], []
    for ci, (start, end) in enumerate(chunks):
        label = f"{start + 1}-{end}"
        engine, text, err = results[ci]
        if engine is None:
            # 单块全链失败：pypdf 文字层保底，不丢内容，记 degraded
            text = "\n".join(page_texts[start:end])
            engine = "pypdf"
            degraded.extend(range(start + 1, end + 1))
            blocks.append({"pages": label, "engine": engine,
                           "ok": bool(text.strip()), "error": err})
        else:
            blocks.append({"pages": label, "engine": engine, "ok": True, "error": None})
        md_parts.append(f"<!-- p.{label} · engine:{engine} -->\n\n{text.strip()}")
    try:
        images = _run_native("_extract_images", data)   # 图表截取也隔离，崩了不影响正文
    except Exception:
        images = []
    return {"md": "\n\n".join(md_parts).strip() + "\n",
            "blocks": blocks, "degraded_pages": degraded,
            "images": images}


# ==================== 整档子进程隔离 ====================
# 崩溃实锤：pymupdf 的 mupdfcpp64.dll 对个别 PDF 段错误（0xc0000005），同步调用
# 会把服务器进程一起杀死（前端「连接错误」）。_run_native 只护住单档，整链路
# 可能还有漏网触点——convert_pdf_isolated 把 convert_pdf 整体关进独立子进程：
# 子进程崩了只损失这一份文件，主进程降级 pypdf 纯文本提取，服务不中断。
# 进度事件经子进程 stdout 以 JSON 行流回（"PROGRESS {...}"），主进程实时转发
# progress_cb；结果 pickle 落临时文件（大 md 不走管道，避免编码/缓冲坑）。

def _convert_job_child(argfile, resfile):
    """子进程入口：读参数 → convert_pdf → 结果 pickle 落盘；进度写 stdout。"""
    with open(argfile, "rb") as f:
        args = pickle.load(f)

    def _cb(info):
        sys.stdout.write("PROGRESS " + json.dumps(info, ensure_ascii=True) + "\n")
        sys.stdout.flush()

    out = convert_pdf(args["filename"], args["data"], api_key=args.get("api_key"),
                      model=args.get("model"), progress_cb=_cb)
    with open(resfile, "wb") as f:
        pickle.dump(out, f)


def _pypdf_fallback(filename, data, reason=""):
    """子进程崩溃后的纯 pypdf 兜底：每页文字层直接拼接，结构与 convert_pdf 一致。
    不碰任何原生库，保证裸环境可返回；全部页记 degraded 让调用方如实标注。"""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        n = len(reader.pages)
        parts = [f"<!-- p.{i + 1}-{i + 1} · engine:pypdf-fallback -->\n\n"
                 f"{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages)]
    except Exception as e:  # PDF 坏到 pypdf 都读不了：返回空骨架，不丢文件
        n, parts = 0, [f"<!-- engine:pypdf-fallback -->\n\n（解析失败：{e}）"]
    stem = os.path.splitext(os.path.basename(filename))[0]
    md = f"# {stem}\n\n" + "\n\n".join(parts).strip() + "\n"
    err = f"pymupdf 子进程崩溃，降级纯文本提取（{reason}）" if reason else "降级纯文本提取"
    return {"md": md,
            "blocks": [{"pages": f"1-{n}", "engine": "pypdf-fallback", "ok": n > 0,
                        "error": None if n else err}],
            "degraded_pages": list(range(1, n + 1)), "images": []}


def convert_pdf_isolated(filename, data, *, api_key=None, model=None, progress_cb=None):
    """子进程隔离版 convert_pdf，签名与返回值完全一致，可直接替换调用。
    子进程崩溃/超时/结果缺失时自动降级 _pypdf_fallback，绝不把异常抛给服务器。"""
    import subprocess
    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as af:
        pickle.dump({"filename": filename, "data": data,
                     "api_key": api_key, "model": model}, af)
        argfile = af.name
    resfile = argfile + ".out"
    proc = None
    try:
        # stderr 继承：子进程的档级日志直接进服务器终端；stdout 专走进度行
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--convert-job", argfile, resfile],
            stdout=subprocess.PIPE, stderr=None, text=True,
            encoding="utf-8", errors="replace")
        for line in proc.stdout:  # 子进程退出（含段错误）时管道关闭，循环自然结束
            if line.startswith("PROGRESS ") and progress_cb:
                try:
                    progress_cb(json.loads(line[9:]))
                except Exception:
                    pass
        rc = proc.wait(timeout=60)
        if rc == 0 and os.path.exists(resfile):
            with open(resfile, "rb") as f:
                return pickle.load(f)
        print(f"[convert] 转换子进程异常退出(rc={rc})，降级 pypdf 纯文本: {filename}")
        return _pypdf_fallback(filename, data, f"rc={rc}")
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            proc.kill()
        print(f"[convert] 转换子进程失败({exc})，降级 pypdf 纯文本: {filename}")
        return _pypdf_fallback(filename, data, str(exc))
    finally:
        for f in (argfile, resfile):
            try:
                os.unlink(f)
            except OSError:
                pass


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--native-job":
        _native_job_child(sys.argv[2])  # 原生调用子进程：stdin 收参，stdout 回结果
        return
    if len(sys.argv) >= 4 and sys.argv[1] == "--convert-job":
        _convert_job_child(sys.argv[2], sys.argv[3])  # 子进程模式：参数/结果走 pickle 文件
        return
    ap = argparse.ArgumentParser(description="PDF → Markdown（多级回滚链，进度走 stderr）")
    ap.add_argument("file", help="待转换 PDF 路径")
    ap.add_argument("--imgs", help="把自动截取的重要图表 PNG 写入该目录")
    args = ap.parse_args()
    with open(args.file, "rb") as f:
        data = f.read()

    def _cb(info):
        if info["status"] == "start":
            print(f"共 {info['total']} 块", file=sys.stderr)
        else:
            shown = info.get("done") or info["block"]
            print(f"[{shown}/{info['total']}] p.{info['pages']} "
                  f"{info['engine']} {info['status']}", file=sys.stderr)

    out = convert_pdf(os.path.basename(args.file), data, progress_cb=_cb)
    sys.stdout.write(out["md"])
    if out.get("images") and args.imgs:
        os.makedirs(args.imgs, exist_ok=True)
        for im in out["images"]:
            with open(os.path.join(args.imgs, im["name"]), "wb") as f:
                f.write(im["data"])
        print(f"[imgs] 截取图表 {len(out['images'])} 张 → {args.imgs}", file=sys.stderr)
    if out["degraded_pages"]:
        print(f"[warn] 降级页：{out['degraded_pages']}", file=sys.stderr)


if __name__ == "__main__":
    main()
