# -*- coding: utf-8 -*-
"""
赛道归类权威数据源 + Status 归一化

TRACK_KEYWORD_MAP：关键词（匹配文件名/标题）→ 主赛道（每个项目只归 1 个）。
调整归类 = 改对应行即可，重建索引后生效（app 点刷新 或 运行 indexer.py）。

仓库只内置通用示例；你自己的项目名单写在 data/tracks_local.json
（本机文件，已被 .gitignore 排除，不随仓库分发），格式 {"关键词": "赛道"}，
启动时自动合并进 TRACK_KEYWORD_MAP。

STATUS_BUCKETS：归一化状态桶。文档头部 `**Status**: xxx` 原文保留在
status_detail 字段，筛选/展示用归一化后的 status；无 Status 的文档默认"跟踪中"。
"""
import json
import os

# 关键词 → 主赛道（按文件名+标题包含匹配，先命中先生效）
TRACK_KEYWORD_MAP = {
    "示例项目A": "示例赛道",
    "示例项目B": "示例赛道",
}


def _load_local_tracks():
    """合并本机私有项目→赛道映射 data/tracks_local.json（若存在）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "tracks_local.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            local = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(local, dict):
        TRACK_KEYWORD_MAP.update({str(k): str(v) for k, v in local.items()})


_load_local_tracks()

STATUS_BUCKETS = ["跟踪中", "推进中", "已投资", "已放弃", "已退出"]


def get_track(text):
    """从文件名+标题文本推断主赛道，未命中返回 '未分类'。"""
    for keyword, track in TRACK_KEYWORD_MAP.items():
        if keyword in text:
            return track
    return "未分类"


def normalize_status(raw):
    """把文档里的 Status 原文归一化到五个桶，空值默认'跟踪中'。"""
    raw = (raw or "").strip()
    if not raw:
        return "跟踪中"
    if "退出" in raw:
        return "已退出"
    if "已投" in raw:
        return "已投资"
    if "放弃" in raw or "否决" in raw or "Pass" in raw:
        return "已放弃"
    if "推进" in raw or "CDD" in raw or "DD" in raw or "尽调" in raw:
        return "推进中"
    return "跟踪中"
