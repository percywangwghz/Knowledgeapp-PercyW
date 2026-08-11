"""
知识库索引器 - 扫描知识库目录，建立结构化索引
"""
import os
import json
import re
from datetime import datetime
from pathlib import Path

from config import KNOWLEDGE_DIR, INDEX_FILE, CATEGORY_MAP, FILE_TYPES
from tracks import get_track, normalize_status


def get_file_type(filename):
    """根据扩展名获取文件类型"""
    ext = Path(filename).suffix.lower()
    return FILE_TYPES.get(ext, "unknown")


def extract_metadata(content):
    """从Markdown内容提取元数据"""
    meta = {
        "title": "",
        "subtitle": "",
        "tags": [],
        "project": "",
        "industry": "",
        "status": "",
        "last_updated": "",
    }
    
    lines = content.split('\n')[:30]  # 只看前30行
    text = '\n'.join(lines)
    
    # 提取标题（第一个 # 开头）
    title_match = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
    if title_match:
        meta["title"] = title_match.group(1).strip()
    
    # 提取副标题（> 开头的引用）
    subtitle_lines = []
    for line in lines:
        if line.startswith('> ') and not line.startswith('> **'):
            subtitle_lines.append(line[2:].strip())
    if subtitle_lines:
        meta["subtitle"] = ' '.join(subtitle_lines[:2])
    
    # 提取标签（`#标签` 或 Tags: 格式）
    tag_patterns = [
        r'Tags?[:：]\s*([\w\s,/#]+)',
        r'标签[:：]\s*([\w\s,/#]+)',
    ]
    for pattern in tag_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            tags = [t.strip() for t in match.group(1).split(',')]
            meta["tags"] = [t for t in tags if t]
            break
    
    # 提取项目名（从标题或路径推断）
    project_patterns = [
        r'\*\*Deal Type\*\*.*?\|\s*\*\*([^|*]+)\*\*',
        r'项目[:：]\s*(\S+)',
    ]
    for pattern in project_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            meta["project"] = match.group(1).strip()
            break
    
    # 提取状态（兼容 **Status**: 写法，捕获到 | 或换行）
    status_patterns = [
        r'Status\*{0,2}[:：]\s*([^\n|]+)',
        r'状态\*{0,2}[:：]\s*([^\n|]+)',
    ]
    for pattern in status_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            meta["status"] = match.group(1).strip()
            break
    
    # 提取更新时间
    date_patterns = [
        r'Last Updated[:：]\s*(\d{4}-\d{2}-\d{2})',
        r'更新时间[:：]\s*(\d{4}-\d{2}-\d{2})',
        r'更新日期[:：]\s*(\d{4}-\d{2}-\d{2})',
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            meta["last_updated"] = match.group(1)
            break
    
    return meta


def scan_knowledge_base():
    """扫描知识库目录，返回结构化数据"""
    documents = []
    categories = {}
    
    # 扫描根目录的独立文件
    for item in sorted(os.listdir(KNOWLEDGE_DIR)):
        item_path = os.path.join(KNOWLEDGE_DIR, item)
        if os.path.isfile(item_path) and item.endswith('.md'):
            try:
                with open(item_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                meta = extract_metadata(content)
                doc = {
                    "name": item,
                    "path": item,
                    "category": "其他",
                    "category_key": "",
                    "type": get_file_type(item),
                    "size": os.path.getsize(item_path),
                    "modified": datetime.fromtimestamp(os.path.getmtime(item_path)).isoformat(),
                    "content": content,
                    **meta,
                }
                doc["status_detail"] = doc["status"]
                doc["status"] = normalize_status(doc["status"])
                doc["track"] = get_track(f"{doc['name']} {doc.get('title', '')}")
                documents.append(doc)
            except Exception:
                continue
    
    # 扫描分类目录
    for category_key in sorted(os.listdir(KNOWLEDGE_DIR)):
        category_path = os.path.join(KNOWLEDGE_DIR, category_key)
        if not os.path.isdir(category_path):
            continue
        
        category_name, category_icon, category_desc = CATEGORY_MAP.get(
            category_key, ("其他", "📁", "")
        )
        
        category_docs = []
        for filename in sorted(os.listdir(category_path)):
            if not filename.endswith('.md'):
                continue
            
            file_path = os.path.join(category_path, filename)
            rel_path = f"{category_key}/{filename}"
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                meta = extract_metadata(content)
                
                doc = {
                    "name": filename,
                    "path": rel_path,
                    "category": category_name,
                    "category_key": category_key,
                    "category_icon": category_icon,
                    "type": get_file_type(filename),
                    "size": os.path.getsize(file_path),
                    "modified": datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat(),
                    "content": content,
                    **meta,
                }
                doc["status_detail"] = doc["status"]
                doc["status"] = normalize_status(doc["status"])
                doc["track"] = get_track(f"{doc['name']} {doc.get('title', '')}")
                documents.append(doc)
                category_docs.append(doc)
            except Exception:
                continue
        
        if category_docs:
            categories[category_key] = {
                "key": category_key,
                "name": category_name,
                "icon": category_icon,
                "description": category_desc,
                "count": len(category_docs),
                "documents": category_docs,
            }
    
    return documents, categories


def build_index(force=False):
    """构建或更新索引（索引文件损坏时自动重建）"""
    if not force and os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            print(f"[WARN] 索引文件损坏，重建：{INDEX_FILE}")
    
    print(f"[SCAN] Scanning knowledge base: {KNOWLEDGE_DIR}")
    documents, categories = scan_knowledge_base()
    
    index = {
        "indexed_at": datetime.now().isoformat(),
        "total_documents": len(documents),
        "documents": documents,
        "categories": categories,
    }
    
    os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    
    print(f"[DONE] Index complete: {len(documents)} documents, {len(categories)} categories")
    return index


def load_index():
    """加载索引（文件损坏时自动重建）"""
    if os.path.exists(INDEX_FILE):
        try:
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            print(f"[WARN] 索引文件损坏，重建：{INDEX_FILE}")
    return build_index(force=True)


def search_documents(index, query):
    """搜索文档"""
    if not query:
        return index.get("documents", [])
    
    query = query.lower()
    results = []
    
    for doc in index.get("documents", []):
        score = 0
        
        # 标题匹配（权重最高）
        if query in doc.get("title", "").lower():
            score += 10
        
        # 文件名匹配
        if query in doc["name"].lower():
            score += 5
        
        # 内容匹配
        if query in doc.get("content", "").lower():
            score += 3
        
        # 标签匹配
        if any(query in tag.lower() for tag in doc.get("tags", [])):
            score += 8
        
        # 项目名匹配
        if query in doc.get("project", "").lower():
            score += 6
        
        # 分类匹配
        if query in doc.get("category", "").lower():
            score += 4
        
        if score > 0:
            results.append((score, doc))
    
    results.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in results]


def get_documents_by_category(index, category_key):
    """按分类获取文档"""
    cat = index.get("categories", {}).get(category_key)
    if cat:
        return cat.get("documents", [])
    return []


def get_all_tags(index):
    """获取所有标签"""
    tags = set()
    for doc in index.get("documents", []):
        tags.update(doc.get("tags", []))
    return sorted(list(tags))


def get_documents_by_tag(index, tag):
    """按标签获取文档"""
    return [doc for doc in index.get("documents", []) if tag in doc.get("tags", [])]


def get_document_by_path(index, path):
    """通过路径获取文档"""
    for doc in index.get("documents", []):
        if doc["path"] == path:
            return doc
    return None


def get_related_documents(index, doc, limit=5):
    """获取相关文档"""
    related = []
    
    for other in index.get("documents", []):
        if other["path"] == doc["path"]:
            continue
        
        score = 0
        
        # 同分类
        if other.get("category") == doc.get("category"):
            score += 2
        
        # 标签重叠
        common_tags = set(other.get("tags", [])) & set(doc.get("tags", []))
        score += len(common_tags) * 3
        
        # 项目关联
        if other.get("project") and doc.get("project"):
            if other["project"] == doc["project"]:
                score += 5
        
        # 内容关键词重叠（简单版本）
        doc_words = set(doc.get("content", "").lower().split())
        other_words = set(other.get("content", "").lower().split())
        common_words = doc_words & other_words
        # 只计算有意义的词（过滤常见词）
        meaningful = {w for w in common_words if len(w) > 4}
        score += len(meaningful) * 0.1
        
        if score > 0:
            related.append((score, other))
    
    related.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in related[:limit]]


if __name__ == "__main__":
    build_index(force=True)
