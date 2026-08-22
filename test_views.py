# -*- coding: utf-8 -*-
"""用 Streamlit AppTest 对 app.py 各视图做无头回归测试"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kb_index.json")

# 持久化 key 文件指向临时路径，避免读写本机真实 data/local_api_key.txt
KEY_FILE = os.path.join(tempfile.mkdtemp(prefix="kb_test_"), "local_api_key.txt")
os.environ["KB_LOCAL_KEY_FILE"] = KEY_FILE

# fresh clone 没有索引文件时先自动构建（扫仓库内置 knowledge/）
if not os.path.exists(INDEX):
    import indexer
    indexer.build_index(force=True)

with open(INDEX, "r", encoding="utf-8") as f:
    index = json.load(f)

failures = []


def run_view(name, state=None, keep_key_file=False):
    if not keep_key_file and os.path.exists(KEY_FILE):
        os.remove(KEY_FILE)  # 默认每个用例从无持久化 key 的干净状态启动
    at = AppTest.from_file(APP, default_timeout=60)
    if state:
        for k, v in state.items():
            at.session_state[k] = v
    at.run()
    if at.exception:
        failures.append((name, at.exception[0].value))
        print(f"[FAIL] {name}: {at.exception[0].value}")
    else:
        print(f"[OK]   {name}")
    return at


# 1. 首页
at = run_view("home")
if not at.exception:
    # Hero：研究主页 eyebrow + 大标题 + mono 索引信息行
    md_text = "\n".join(m.value for m in at.markdown)
    for t in ("研究主页", "一级投研知识库", "篇文档", "个合集", "索引于"):
        assert t in md_text, f"home hero missing: {t}"
    assert len(at.metric) == 0, "home should use compact stat blocks, not big metric cards"
    print("       hero rendered")

# 2. 分类页
cat_key = next(iter(index["categories"]))
run_view("category", {"view_mode": "category", "selected_category": cat_key})

# 3. 搜索结果
run_view("search", {"view_mode": "search", "search_query": "AI4S"})

# 4. 文档详情
doc = next(d for d in index["documents"] if d["category_key"] == "02_deals")
at = run_view("doc", {"view_mode": "doc", "selected_doc": doc})
if not at.exception:
    md_text = "\n".join(m.value for m in at.markdown)
    assert doc.get("title", "")[:10] in md_text or doc["name"][:10] in md_text, "doc title not rendered"
    print("       doc rendered:", doc["name"])

# 5. Thesis Battle（前端模式无默认 key，需注入会话 key）
at = run_view("battle", {"view_mode": "battle", "user_api_key": "sk-test"})
if not at.exception:
    assert len(at.selectbox) >= 1, "battle doc selector missing"
    print("       battle selectbox options:", len(at.selectbox[0].options))

# 6. Investment Radar
at = run_view("radar", {"view_mode": "radar"})
if not at.exception:
    # 子导航是 st.button 文字 tab（非 st.tabs），6 个：总览/信号/主题/变量/报告/来源
    radar_tabs = [b for b in at.button if (b.key or "").startswith("radar_tab_")]
    assert len(radar_tabs) == 6, f"radar should have 6 sub-nav tabs, got {len(radar_tabs)}"
    print("       radar sub-nav tabs:", [b.label for b in radar_tabs])

# 7. 新项目评审（含 ② 行业认知嵌入区块）
# 7a. 未填 key：必须出现「功能不可用」警告，且不渲染评审 selectbox
at = run_view("compare", {"view_mode": "compare"})
if not at.exception:
    assert len(at.warning) >= 1, "api-key warning missing"
    assert not any(s.key == "review_project" for s in at.selectbox), \
        "review selectbox should not render without key"
    print("       no-key warning shown")
# 7b. 侧边栏注入 key 后：完整渲染（评审区块 + ② 行业认知区块）
at = run_view("compare+key", {"view_mode": "compare", "user_api_key": "sk-test"})
if not at.exception:
    md_text = "\n".join(m.value for m in at.markdown)
    assert "新项目评审" in md_text, "review section missing"
    assert "行业总文档" in md_text, "industry cognition section missing"
    assert "② 行业认知" in md_text, "industry cognition block missing from compare view"
    assert len(at.selectbox) >= 2, f"review selectboxes missing, got {len(at.selectbox)}"
    print("       review selectboxes:", [s.label for s in at.selectbox])

# 8. 文件归档
at = run_view("ingest", {"view_mode": "ingest"})
if not at.exception:
    md_text = "\n".join(m.value for m in at.markdown)
    assert "文件归档" in md_text, "ingest section missing"
    assert "② 行业认知" not in md_text, "industry cognition block leaked into ingest view"

# 8b. 后台分析完成 → 落盘任务结果导入 session_state 并消费任务文件
import ingest as _ing
_ing._write_job({"status": "done", "started": "2026-08-04T00:00:00",
                 "finished": "2026-08-04T00:01:00",
                 "steps": [{"file": "x.pdf", "status": "done", "detail": ""}],
                 "results": [{"file": "x.pdf", "ok": True, "category_key": "02_deals",
                              "title": "T", "filename": "f", "summary": "S",
                              "content": "# T", "text": "t"}]})
at = run_view("ingest", {"view_mode": "ingest"})
if not at.exception:
    ana = at.session_state["ingest_analysis"]
    assert ana and ana[0]["title"] == "T", "job results not imported"
    assert not os.path.exists(os.path.join(_ing.DATA_DIR, "ingest_job.json")), \
        "job file not consumed"

# 9. 串联链路：归档 → 评审预选 → 评审写回后跳 Battle
import config as _config
KB_DIR = _config.KNOWLEDGE_DIR  # 与 app.py 同一套解析：env > 仓库内置 knowledge/ > 本机默认
deal_doc = next(d for d in index["documents"] if d["category_key"] == "02_deals")

# 9a. 归档结果「发起评审」→ 跳评审页并自动选中该项目（预选路径随即被消费）
expected_label = (f"[{deal_doc.get('track', '未分类')}] "
                  f"{deal_doc.get('title') or deal_doc['name'].replace('.md', '')}")
at = run_view("ingest", {"view_mode": "ingest", "user_api_key": "sk-test", "ingest_results": [
    {"file": "bp.pdf", "ok": True, "category_key": "02_deals", "title": "T", "summary": "S",
     "path": os.path.join(KB_DIR, deal_doc["path"])}]})
if not at.exception:
    btn = next((b for b in at.button if "发起评审" in b.label), None)
    assert btn is not None, "ingest review button missing"
    btn.click().run()
    assert at.session_state.view_mode == "compare", "ingest → compare jump failed"
    got = at.selectbox(key="review_project").value
    assert got == expected_label, f"chained preselect failed: {got!r} != {expected_label!r}"

# 9b. 评审页消费预选 → 项目 selectbox 自动选中对应文档
at = run_view("compare", {"view_mode": "compare", "user_api_key": "sk-test",
                          "review_preselect_path": deal_doc["path"]})
if not at.exception:
    got = at.selectbox(key="review_project").value
    assert got == expected_label, f"preselect failed: {got!r} != {expected_label!r}"

# 9c. 评审写回后「发起 Battle」→ 跳 Battle 页并预选文档
at = run_view("compare", {"view_mode": "compare", "user_api_key": "sk-test",
                          "review_next_battle_path": deal_doc["path"]})
if not at.exception:
    btn = next((b for b in at.button if "发起 Battle" in b.label), None)
    assert btn is not None, "next-battle button missing"
    btn.click().run()
    assert at.session_state.view_mode == "battle", "compare → battle jump failed"
    assert at.session_state.battle_doc_path == deal_doc["path"], "battle preselect mismatch"

# 10. API Key 注入：register_key_provider 覆盖 / 注销；前端模式不回退默认 key
import llm as _llm
_orig_env = os.environ.get("MOONSHOT_API_KEY")
os.environ["MOONSHOT_API_KEY"] = "env-key"
_llm.register_key_provider(lambda: "session-key")
assert _llm.get_api_key() == "session-key", "session key should take priority"
_llm.register_key_provider(lambda: "  ")  # 前端模式：空白即无 key，不回退
assert _llm.get_api_key() == "", "registered blank provider must NOT fall back to env"
_llm.register_key_provider(lambda: 1 / 0)  # 异常同样视为无 key
assert _llm.get_api_key() == "", "raising provider must NOT fall back to env"
_llm.register_key_provider(lambda: None)  # 本机自用：放行回退默认来源
assert _llm.get_api_key() == "env-key", "None provider should fall through to env"
_llm.register_key_provider(None)  # 注销（CLI 模式）才回退到默认来源
assert _llm.get_api_key() == "env-key", "unregistered provider should fall back to env"
if _orig_env is None:
    del os.environ["MOONSHOT_API_KEY"]
else:
    os.environ["MOONSHOT_API_KEY"] = _orig_env
print("[OK]   api-key injection")

# 10b. app.py 侧边栏 API Key 输入框 → 写入 session_state + 本机持久化文件
at = run_view("home")
if not at.exception:
    ti = next((t for t in at.text_input if t.label == "API Key"), None)
    assert ti is not None, "sidebar api-key input missing"
    ti.set_value("sk-friend-demo").run()
    assert at.session_state.user_api_key == "sk-friend-demo", \
        f"api-key input not synced to session_state: {at.session_state.user_api_key!r}"
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        assert f.read().strip() == "sk-friend-demo", "api-key not persisted to local file"
    print("[OK]   app key provider")

# 10c. 填一次一直用：全新会话（无预设 state）启动 → 自动载入持久化 key，功能直接可用
at = run_view("compare(persisted)", {"view_mode": "compare"}, keep_key_file=True)
if not at.exception:
    assert at.session_state.user_api_key == "sk-friend-demo", \
        "persisted key not loaded on fresh session"
    assert any(s.key == "review_project" for s in at.selectbox), \
        "review selectbox should render with persisted key"
    assert not at.warning, "no warning expected when persisted key exists"
    print("[OK]   key persistence across sessions")
if os.path.exists(KEY_FILE):
    os.remove(KEY_FILE)

# 11. 本地图片内联（mdblocks.inline_local_images，纯函数直测）：
# 相对路径图片 → base64 data URI；含括号目录名不截断；网络图/缺失文件原样保留
import mdblocks

_tmp_kb = tempfile.mkdtemp()
_doc_dir = os.path.join(_tmp_kb, "08_funds")
_img_dir = os.path.join(_tmp_kb, "assets", "img", "D(1)")
os.makedirs(_doc_dir)
os.makedirs(_img_dir)
# 1x1 PNG
_png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
with open(os.path.join(_img_dir, "p3_1.png"), "wb") as f:
    f.write(_png)
_md = ("## 图集\n\n![p.3 图表](../assets/img/D(1)/p3_1.png)\n\n"
       "![网图](https://x.com/a.png)\n\n![缺失](../assets/img/D(1)/none.png)")
_out = mdblocks.inline_local_images(_md, _doc_dir)
assert "![p.3 图表](data:image/png;base64," in _out, "local image not inlined"
assert "![网图](https://x.com/a.png)" in _out, "remote image should stay as-is"
assert "![缺失](../assets/img/D(1)/none.png)" in _out, "missing file should stay as-is"
print("[OK]   local image inlining")

# 12. 折叠块拆分（split_details_blocks）：原文全文不进目录、正文后单独渲染
_md2 = "# T\n\n## 一\n正文\n\n<details><summary>原文全文</summary>\n\n## 原始目录\n原文\n\n</details>\n"
_main, _blocks = mdblocks.split_details_blocks(_md2)
assert "## 原始目录" not in _main and len(_blocks) == 1 and "## 原始目录" in _blocks[0], \
    f"main={_main!r} blocks={_blocks!r}"
assert "## 一" in _main, "正文标题应保留"
print("[OK]   details blocks split")

print()
if failures:
    print(f"{len(failures)} view(s) failed")
    sys.exit(1)
print("ALL VIEWS OK")
