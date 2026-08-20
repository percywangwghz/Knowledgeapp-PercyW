# -*- coding: utf-8 -*-
"""Markdown 文本块工具：识别纯文字/ASCII 表格并包成代码围栏。

供 app.py（Streamlit st.markdown 管线）与 build_html.py（python-markdown
管线）共用：两端在各自的 markdown 转换前调用 wrap_ascii_tables()，
把不在围栏内的 box-drawing 字符块包成 ```text 围栏，使其以等宽 pre 渲染。

另含 inline_local_images()：把本地相对图片引用内联成 base64 data URI，
解决浏览器把相对路径解析到应用 URL 下导致破图的问题。
"""
import base64
import os
import re

# 表格框线字符（竖向/交叉类，横向 ─  alone 不足以判断，故不含）
_BOX_CHARS = "│┼├┤┬┴┃╋╂╞╡╪"

# 图片引用：URL 段用贪婪匹配到行内最后一个 )，容忍目录名里的括号
# （如「鼎晖…_0414vf(1)」——朴素 [^)]+ 会在第一个 ) 处截断导致找不到文件）
_IMG_REF_RE = re.compile(r"!\[([^\]]*)\]\((.+)\)")

# 归档时文末的「原文全文」折叠块
_DETAILS_RE = re.compile(r"\n?<details>.*?</details>\s*", re.DOTALL)


def split_details_blocks(text):
    """把 <details>…</details> 折叠块从正文抽出，返回 (正文, [折叠块…])。
    折叠块是转换后的原始全文：其章节标题不应进入页面目录，渲染时也应在
    正文之后单独追加，而不是混入正文的章节切分。"""
    blocks = []
    main = _DETAILS_RE.sub(lambda m: blocks.append(m.group(0).strip()) or "\n", text)
    return main, blocks


def _is_box_line(line):
    """该行是否为 ASCII 表格行：含 ≥2 个竖线/交叉类制表符。"""
    return sum(1 for c in line if c in _BOX_CHARS) >= 2


def inline_local_images(text, doc_dir, max_bytes=15 * 1048576):
    """把 md 里的本地相对图片引用改写成 base64 data URI，返回新文本。
    图集段引用按 .md 所在目录的相对路径书写（Obsidian/Typora 直接可读），
    但浏览器会把相对路径解析到应用 URL 下 → 404 破图。内联后
    st.markdown / HTML 导出直接可渲染。
    网络图 / data URI / 缺失或超过 max_bytes 的文件原样保留。"""
    def _sub(m):
        alt, url = m.group(1), m.group(2).strip()
        if re.match(r"(?i)^(?:https?:)?//|^data:", url):
            return m.group(0)
        abs_path = os.path.normpath(os.path.join(doc_dir, url))
        try:
            if not os.path.isfile(abs_path) or os.path.getsize(abs_path) > max_bytes:
                return m.group(0)
            with open(abs_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            return m.group(0)
        ext = os.path.splitext(abs_path)[1].lstrip(".").lower() or "png"
        mime = "image/svg+xml" if ext == "svg" else f"image/{ext}"
        return f"![{alt}](data:{mime};base64,{b64})"
    return _IMG_REF_RE.sub(_sub, text)


def wrap_ascii_tables(text):
    """把不在代码围栏内的连续 box-drawing 字符块包成 ```text 围栏。

    判定保守：每行含 ≥2 个 │/┼ 类字符，连续 ≥3 行才算表格块，
    宁可漏判也不误判正常文本（如引用单条竖线符号的句子）。
    已处于 ``` 围栏内的内容原样保留。
    """
    lines = text.split("\n")
    out = []
    run = []
    in_code = False

    def flush():
        if len(run) >= 3:
            out.append("```text")
            out.extend(run)
            out.append("```")
        else:
            out.extend(run)
        run.clear()

    for line in lines:
        if line.strip().startswith("```"):
            flush()
            in_code = not in_code
            out.append(line)
            continue
        if not in_code and _is_box_line(line):
            run.append(line)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)
