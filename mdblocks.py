# -*- coding: utf-8 -*-
"""Markdown 文本块工具：识别纯文字/ASCII 表格并包成代码围栏。

供 app.py（Streamlit st.markdown 管线）与 build_html.py（python-markdown
管线）共用：两端在各自的 markdown 转换前调用 wrap_ascii_tables()，
把不在围栏内的 box-drawing 字符块包成 ```text 围栏，使其以等宽 pre 渲染。
"""

# 表格框线字符（竖向/交叉类，横向 ─  alone 不足以判断，故不含）
_BOX_CHARS = "│┼├┤┬┴┃╋╂╞╡╪"


def _is_box_line(line):
    """该行是否为 ASCII 表格行：含 ≥2 个竖线/交叉类制表符。"""
    return sum(1 for c in line if c in _BOX_CHARS) >= 2


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
