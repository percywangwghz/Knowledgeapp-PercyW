# -*- coding: utf-8 -*-
"""tech 单测：框架加载兜底 + 文档清单 + 提取路由解析 + 合并入库全流程（mock LLM，不触网）"""
import json
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import tech

orig_kb = config.KNOWLEDGE_DIR
orig_fw = tech.TECH_FRAMEWORK_FILE
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"[OK]   {name}")
    else:
        failures.append(name)
        print(f"[FAIL] {name} {detail}")


# ---------- 框架加载 ----------
check("框架文档加载（真实知识库）", "档案" in tech.load_framework())
tech.TECH_FRAMEWORK_FILE = os.path.join(tempfile.mkdtemp(), "nonexistent.md")
check("缺文件走内置兜底", tech.load_framework() == tech.FALLBACK_FRAMEWORK)
tech.TECH_FRAMEWORK_FILE = orig_fw


# ---------- 临时知识库 ----------
config.KNOWLEDGE_DIR = tempfile.mkdtemp()
os.makedirs(tech.tech_dir())

with open(os.path.join(tech.tech_dir(), "CPO共封装.md"), "w", encoding="utf-8") as f:
    f.write("# CPO 共封装\n> 光电共封装技术 | 所属行业：光通信 | 更新时间：2026-08-01\n\n## 技术原理\n旧内容\n")

docs = tech.list_tech_docs()
check("文档清单识别", len(docs) == 1 and docs[0]["name"] == "CPO共封装.md")
check("标题提取", docs[0]["title"] == "CPO 共封装")
check("定位行提取", "光电共封装" in docs[0]["tagline"])


# ---------- extract_and_route（mock chat） ----------
EXT = {"target": "CPO共封装.md", "new_title": "", "industry": "光通信",
       "one_liner": "光电共封装，降功耗提带宽", "principle": "把光引擎与交换芯片共封装，缩短电互连距离",
       "key_points": ["要点1", "要点2", "", "要点3"],
       "metrics": ["CPO：功耗=5pJ/bit"], "related": ["交换芯片", "光引擎封装"],
       "events": ["2026-08-01 某厂商发布 CPO 交换机"]}
ART = {"title": "CPO 产业趋势深度", "account": "半导体行业观察", "date": "2026-08-01",
       "text": "正文" * 3000}

with mock.patch.object(tech, "chat", return_value="```json\n" + json.dumps(EXT, ensure_ascii=False) + "\n```") as c:
    ext = tech.extract_and_route(ART, docs)
check("路由到既有文档", ext["target"] == "CPO共封装.md")
check("要点清洗（去空串）", ext["key_points"] == ["要点1", "要点2", "要点3"])
check("原理与关联技术字段", ext["principle"].startswith("把光引擎") and ext["related"] == ["交换芯片", "光引擎封装"])
check("source_line 由文章元数据构建", ext["source_line"] == "2026-08-01 半导体行业观察《CPO 产业趋势深度》")
check("正文截断送入", len(c.call_args[0][0][1]["content"]) < tech.ARTICLE_TEXT_LIMIT + 300
      and c.call_args[1].get("feature") == "tech-extract")

# 模型输出垃圾 → 自动重试一次，仍失败则显式报错（不再静默兜底出空字段）
with mock.patch.object(tech, "chat", return_value="无法解析") as c:
    try:
        tech.extract_and_route(ART, docs)
        check("垃圾输出显式报错", False, "未抛出 RuntimeError")
    except RuntimeError:
        check("垃圾输出显式报错", c.call_count == 2, f"call_count={c.call_count}")

# 第一次垃圾、第二次正常 → 自动重试成功
with mock.patch.object(tech, "chat", side_effect=["无法解析", json.dumps(EXT, ensure_ascii=False)]) as c:
    ext2 = tech.extract_and_route(ART, docs)
check("解析失败自动重试", ext2["target"] == "CPO共封装.md" and c.call_count == 2)


# ---------- resolve_target_name ----------
check("既有文件名照抄", tech.resolve_target_name({"target": "CPO共封装.md"}) == "CPO共封装.md")
check("NEW 用 new_title 清洗", tech.resolve_target_name({"target": "NEW", "new_title": "1.6T 光模块"})
      == "1.6T_光模块.md")
check("无 .md 后缀不走照抄", tech.resolve_target_name({"target": "CPO共封装", "new_title": "X"})
      == "X.md")
check("全空兜底", tech.resolve_target_name({"target": "", "new_title": "", "one_liner": ""})
      == "未命名技术.md")


# ---------- merge_doc / save / 全流程 ----------
MERGED = "# CPO 共封装\n> 定位 | 更新时间：2026-08-05\n> 🆕 本次新增：要点\n\n## 技术原理\n新内容\n"
with mock.patch.object(tech, "chat", return_value="```markdown\n" + MERGED + "\n```") as c:
    merged = tech.merge_doc("旧文档内容", ext)
check("合并输出去 fence", merged == MERGED.strip())
check("合并走 tech-merge", c.call_args[1].get("feature") == "tech-merge")

with mock.patch.object(tech, "chat", return_value=""):
    try:
        tech.merge_doc("旧文档内容", ext)
        check("合并空返回显式报错", False, "未抛出 RuntimeError")
    except RuntimeError:
        check("合并空返回显式报错", True)

path = tech.save_tech_doc("CPO共封装.md", MERGED)
check("落盘到 09_tech", os.path.dirname(path) == tech.tech_dir() and os.path.exists(path))
with open(path, encoding="utf-8") as f:
    check("落盘内容一致", f.read() == MERGED.strip() + "\n")

# process_article 端到端：两次 chat（提取 + 合并）
with mock.patch.object(tech, "chat", side_effect=[json.dumps(EXT, ensure_ascii=False), MERGED]):
    r = tech.process_article(ART, docs)
check("端到端合并既有文档", r["is_new"] is False and r["path"].endswith("CPO共封装.md"))
with open(r["path"], encoding="utf-8") as f:
    check("端到端内容落盘", "新内容" in f.read())

# 新文档路由：第二篇走 NEW → 新建
EXT_NEW = dict(EXT, target="NEW", new_title="LPO 线性直驱")
with mock.patch.object(tech, "chat", side_effect=[json.dumps(EXT_NEW, ensure_ascii=False), MERGED]):
    r2 = tech.process_article(ART, docs)
check("端到端新建文档", r2["is_new"] is True and r2["path"].endswith("LPO_线性直驱.md"))


config.KNOWLEDGE_DIR = orig_kb

print()
if failures:
    print(f"FAILED: {len(failures)} - {failures}")
    sys.exit(1)
print("ALL TECH TESTS OK")
