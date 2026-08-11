# -*- coding: utf-8 -*-
"""
文件归档模块（可独立 CLI 运行）：拖入文件 → 读取内容 → AI 判断分类并按分类研究框架整理成知识库文档。

支持 .pdf / .docx / .txt / .md。两次 LLM 调用：先分类（轻量，只看开头），
再按 config.FRAMEWORK_MAP 勾稽 03_frameworks 的研究框架整理正文（无映射走通用整理）。
界面流程为两阶段：AI 提议分类 → 人工可改判（改判后按新框架重新整理）→ 确认入库。
落盘时自动补来源行（> 来源文件：… · 归档于 …）供索引器提取元数据。

CLI:
    python ingest.py 文件1.pdf 文件2.docx [--dry-run]

数据：写入 config.KNOWLEDGE_DIR 下对应分类目录，刷新索引后出现在应用中。
"""
import argparse
import io
import json
import os
import re
import threading
import time
from datetime import date, datetime

import config
from llm import chat, get_api_key, set_thread_api_key

SUPPORTED_EXT = {".pdf", ".docx", ".txt", ".md"}
TEXT_LIMIT = 15000          # 整理时送入模型的原文截断长度
CLASSIFY_TEXT_LIMIT = 3000  # 分类时送入模型的原文开头长度
FRAMEWORK_LIMIT = 4000      # 研究框架文档送入模型的截断长度
MAX_FILENAME = 60           # 生成文件名长度上限（不含扩展名）

DATA_DIR = config.DATA_DIR
JOB_FILE = "ingest_job.json"  # 后台分析任务的进度/结果落盘文件

_job_thread = None  # 后台分析线程（模块级变量，跨 rerun 存活）

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
直接输出整理后的 markdown 正文，不要输出任何其他文字，不要用 markdown 代码块包裹。{truncated_note}"""


# ==================== 文本提取 ====================

def extract_text(filename, data):
    """从文件字节中提取纯文本。不支持的扩展名抛 ValueError。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError(f"不支持的文件类型：{ext or '（无扩展名）'}，支持 {'/'.join(sorted(SUPPORTED_EXT))}")
    if ext in (".txt", ".md"):
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    # .docx
    import docx
    doc = docx.Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


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


def classify_document(filename, text):
    """轻量调用（只看开头）：判断分类 + 编目信息。
    返回 {category_key, title, filename, summary}。"""
    categories = "\n".join(f"- {k}：{v[0]} — {v[2]}" for k, v in config.CATEGORY_MAP.items())
    out = chat([
        {"role": "system", "content": CLASSIFY_PROMPT.format(categories=categories)},
        {"role": "user", "content": f"文件名：{filename}\n\n原文开头：\n{text[:CLASSIFY_TEXT_LIMIT]}"},
    ], max_tokens=1000, feature="ingest")
    meta = _parse_json_obj(out)
    stem = os.path.splitext(os.path.basename(filename))[0]
    cat_key = meta.get("category_key", "")
    if cat_key not in config.CATEGORY_MAP:
        cat_key = ""
    return {"category_key": cat_key,
            "title": str(meta.get("title", "")).strip() or stem,
            "filename": _sanitize_filename(meta.get("filename"), stem),
            "summary": str(meta.get("summary", "")).strip()}


def organize_document(filename, text, category_key=""):
    """按分类研究框架整理正文（勾稽 03_frameworks 的方法论）；无框架时走通用结构化整理。
    返回 {"content": ...}。"""
    framework = _load_framework(category_key)
    if framework:
        cat_name = config.CATEGORY_MAP.get(category_key, ("", "", ""))[0]
        framework_block = (
            f"- 本文档属于「{cat_name}」。本所对此类资料的研究框架如下，整理时按其章节结构组织内容；"
            "框架章节若原文未涉及，标注「（原文未涉及）」，不要硬凑编造。\n"
            f"== 研究框架 ==\n{framework}\n== 框架结束 ==")
    else:
        framework_block = ("- 按知识库文档风格划分章节（## 小节），必要时用列表/表格；"
                           "原文本身是优质结构化文档时，可以基本保留原文，只做清理和小节化。")
    truncated = text[:TEXT_LIMIT]
    note = ""
    if len(text) > TEXT_LIMIT:
        note = (f"\n\n注意：原文较长（{len(text)} 字），以上只是前 {TEXT_LIMIT} 字，"
                "请基于这部分整理，并在正文末尾注明「基于原文前段整理」。")
    content = _strip_outer_fence(chat([
        {"role": "system", "content": ORGANIZE_PROMPT.format(framework_block=framework_block,
                                                             truncated_note=note)},
        {"role": "user", "content": f"文件名：{filename}\n\n原文：\n{truncated}"},
    ], max_tokens=8000, feature="ingest"))
    if not content:
        stem = os.path.splitext(os.path.basename(filename))[0]
        content = f"# {stem}\n\n> AI 整理失败，以下为原文摘录。\n\n{truncated}"
    return {"content": content}


# ==================== 归档 ====================

def save_document(result, source_name):
    """写入知识库对应分类目录（category_key 为空时写入根目录 → 索引为"其他"）。
    文件名冲突自动加序号。在标题行后插入来源行。返回绝对路径。"""
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
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _analyze_one(filename, data):
    """单个文件的分析阶段：提取 → 分类（轻量调用）→ 按分类框架整理，不落盘。
    结果 dict 含 content 与 text（留存原文截断，供改判分类后按新框架重整）。"""
    res = {"file": filename, "ok": False, "category_key": "", "title": "",
           "filename": "", "summary": "", "content": "", "text": "", "error": ""}
    try:
        text = extract_text(filename, data)
        if not text.strip():
            res["error"] = "提取不到文本（扫描件 PDF 需先 OCR）"
            return res
        res.update(classify_document(filename, text))
        res.update(organize_document(filename, text, res["category_key"]))
        res["text"] = text[:TEXT_LIMIT]
        res["ok"] = True
    except Exception as e:
        res["error"] = str(e)
    return res


def ingest_one(filename, data):
    """单个文件完整流程（CLI 用）：分析 → 直接按 AI 提议分类归档。返回结果 dict。"""
    res = _analyze_one(filename, data)
    if res["ok"]:
        try:
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


def _read_job():
    if os.path.exists(_path(JOB_FILE)):
        try:
            with open(_path(JOB_FILE), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None  # 任务文件损坏时按无任务处理，不崩页面
    return None


def _write_job(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path(JOB_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    for attempt in range(5):  # Windows 上读者持有句柄时 os.replace 会被拒，短暂重试
        try:
            os.replace(tmp, _path(JOB_FILE))
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, _path(JOB_FILE))


def _run_job(files_data, api_key):
    """后台线程体：逐个分析文件，进度与结果实时落盘 ingest_job.json。
    注意：线程内没有 ScriptRunContext，禁止调用任何 st.*；
    前端注入的 API Key 同样取不到，由 api_key 参数显式带入。"""
    set_thread_api_key(api_key)
    state = {"status": "running", "started": datetime.now().isoformat(timespec="seconds"),
             "finished": "",
             "steps": [{"file": n, "status": "waiting", "detail": ""} for n, _ in files_data],
             "results": []}
    _write_job(state)
    lock = threading.Lock()

    def _update(i, status, detail=""):
        with lock:
            state["steps"][i]["status"] = status
            state["steps"][i]["detail"] = detail
            _write_job(state)

    try:
        for i, (name, data) in enumerate(files_data):
            _update(i, "running")
            res = _analyze_one(name, data)
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
    global _job_thread
    if _job_running():
        return
    # 主线程取好当前会话的 API Key 传给工作线程（线程内访问不了 session_state）
    _job_thread = threading.Thread(target=_run_job, args=(files_data, get_api_key()),
                                   daemon=True)
    _job_thread.start()


# ==================== 页面 ====================

def _category_options():
    """分类下拉选项：[(label, category_key)]，空 key = 根目录（其他）。"""
    opts = [(f"{v[1]} {v[0]}", k) for k, v in config.CATEGORY_MAP.items()]
    opts.append(("📁 其他（根目录）", ""))
    return opts


def render_ingest(index, on_saved):
    import streamlit as st
    st.markdown("<div class='doc-title'>📥 文件归档</div>", unsafe_allow_html=True)
    st.markdown("<div class='meta-line'>拖入 PDF / DOCX / TXT / MD，AI 读取内容、提议分类并整理成结构化文档；"
                "分类可人工改判，确认后写入知识库。</div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom:0.8rem'></div>", unsafe_allow_html=True)

    _STATUS_ICON = {"waiting": "⏸️", "running": "⏳", "done": "✅", "error": "❌"}

    def _render_job_steps(steps):
        if not steps:
            st.markdown("<div class='caption'>正在启动任务…</div>", unsafe_allow_html=True)
            return
        lines = []
        for s in steps:
            icon = _STATUS_ICON.get(s.get("status"), "")
            detail = (f"（{s['detail']}）"
                      if s.get("detail") and s.get("status") != "running" else "")
            lines.append(f"{icon} {s.get('file', '')} {detail}")
        st.markdown("<div class='caption'>" + "<br>".join(lines) + "</div>",
                    unsafe_allow_html=True)

    @st.fragment(run_every=3)
    def _render_job_live():
        """任务运行中每 3 秒轮询 ingest_job.json 刷新进度；检测到结束后触发整页 rerun。"""
        j = _read_job() or {}
        st.info(f"⏳ 后台分析进行中（开始于 {j.get('started', '—')}）。"
                "进度实时落盘，可自由切换页面/刷新。")
        _render_job_steps(j.get("steps", []))
        if j.get("status") != "running":
            st.rerun()  # 整页 rerun，由主流程导入结果

    running = _job_running()
    files = st.file_uploader("拖入文件（可多选）",
                             type=[e.lstrip(".") for e in sorted(SUPPORTED_EXT)],
                             accept_multiple_files=True)
    if files and st.button("🔍 开始分析", type="primary", disabled=running,
                           help="后台执行：进度实时落盘，可自由切换页面/刷新"):
        files_data = [(f.name, f.read()) for f in files]
        st.session_state.pop("ingest_analysis", None)
        st.session_state.pop("ingest_results", None)
        _start_job(files_data)
        st.rerun()

    # ---- 后台任务状态：进行中轮询 / 中断提示 / 完成后导入分析结果 ----
    job = _read_job()
    if running:
        _render_job_live()
        return
    if job and job.get("status") == "running":
        # 任务文件仍是 running 但线程已不在：应用曾被重启/进程被杀，任务中断
        st.warning("上次后台分析被中断（应用重启或进程退出），结果可能不完整，可重新发起。")
        _render_job_steps(job.get("steps", []))
    elif job and job.get("status") in ("done", "error"):
        results = job.get("results") or []
        try:
            os.remove(_path(JOB_FILE))
        except OSError:
            pass
        if results:
            st.session_state["ingest_analysis"] = results
            st.rerun()
        else:
            st.error("后台分析失败，未产出结果。")

    # ---- 阶段一结果：AI 提议分类，人工可改判 ----
    analysis = st.session_state.get("ingest_analysis") or []
    if analysis:
        opts = _category_options()
        labels = [l for l, _ in opts]
        n_ok = sum(1 for r in analysis if r["ok"])
        st.markdown(f"<div class='section-header'>分析完成：{n_ok} 可归档 / {len(analysis)} 总计"
                    "——确认或改判分类后入库</div>", unsafe_allow_html=True)
        for i, r in enumerate(analysis):
            if not r["ok"]:
                st.warning(f"❌ {r['file']}：{r['error']}")
                continue
            default_label = next((l for l, k in opts if k == r["category_key"]), labels[-1])
            c1, c2 = st.columns([3, 2])
            with c1:
                st.markdown(f"<div class='card'><div class='meta-line'>📄 {r['file']}</div>"
                            f"<div style='font-weight:600; margin-top:0.2rem'>{r['title']}</div>"
                            f"<div class='caption'>{r['summary']}</div></div>",
                            unsafe_allow_html=True)
            with c2:
                st.selectbox("分类（可改判）", labels, index=labels.index(default_label),
                             key=f"ingest_cat_{i}")
        if st.button("💾 确认入库", type="primary", key="ingest_confirm"):
            label_to_key = dict(opts)
            final = []
            for i, r in enumerate(analysis):
                if not r["ok"]:
                    final.append(r)
                    continue
                chosen = label_to_key.get(st.session_state.get(f"ingest_cat_{i}"),
                                          r["category_key"])
                if chosen != r["category_key"] and r.get("text"):
                    cat_name = config.CATEGORY_MAP.get(chosen, ("其他", "", ""))[0]
                    with st.spinner(f"分类改判，按「{cat_name}」框架重新整理「{r['file']}」…"):
                        try:
                            r.update(organize_document(r["file"], r["text"], chosen))
                            r["category_key"] = chosen
                        except Exception as e:
                            r["ok"], r["error"] = False, f"重新整理失败：{e}"
                            final.append(r)
                            continue
                else:
                    r = dict(r, category_key=chosen)
                try:
                    r["path"] = save_document(r, r["file"])
                except Exception as e:
                    r["ok"], r["error"] = False, str(e)
                r.pop("text", None)
                r.pop("content", None)
                final.append(r)
            st.session_state["ingest_results"] = final
            st.session_state.pop("ingest_analysis", None)
            if any(r["ok"] for r in final):
                on_saved()  # 刷新索引，侧栏分类计数与搜索即时可见
            st.rerun()

    # ---- 阶段二结果：已入库，02_deals 可一键发起评审 ----
    results = st.session_state.get("ingest_results") or []
    if results:
        n_ok = sum(1 for r in results if r["ok"])
        st.markdown(f"<div class='section-header'>本次归档：{n_ok} 成功 / {len(results)} 总计</div>",
                    unsafe_allow_html=True)
        for j, r in enumerate(results):
            if r["ok"]:
                name, icon, _ = config.CATEGORY_MAP.get(r["category_key"], ("其他", "📁", ""))
                rel = os.path.relpath(r["path"], config.KNOWLEDGE_DIR).replace(os.sep, "/")
                st.markdown(
                    f"<div class='card'><div class='meta-line'>{icon} {name} · {rel}</div>"
                    f"<div style='font-weight:600; margin-top:0.2rem'>{r['title']}</div>"
                    f"<div class='caption'>{r['summary']}</div></div>",
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
                org = organize_document(os.path.basename(path), text, meta["category_key"])
                out.append({"file": path, **meta, "content_chars": len(org["content"])})
            except Exception as e:
                out.append({"file": path, "error": str(e)})
        else:
            out.append(ingest_one(os.path.basename(path), data))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
