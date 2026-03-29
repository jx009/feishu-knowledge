"""
知识卡片数据结构定义

知识卡片（SkillCard）是知识库的基本单元，定义了知识的统一结构。
所有沉淀的知识都会被结构化为 SkillCard 格式，然后写入飞书文档、注册表和向量数据库。
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union


# 支持的知识分类
CATEGORIES = [
    "架构方案",
    "产品迭代",
    "优化沉淀",
    "避坑记录",
    "最佳实践",
    "工具使用",
    "业务知识",
]

# 知识生命周期状态
SYNC_STATUS_CREATED_FEISHU = "CREATED_FEISHU"
SYNC_STATUS_PENDING_INDEX = "PENDING_INDEX"
SYNC_STATUS_INDEXED = "INDEXED"
SYNC_STATUS_PENDING_REINDEX = "PENDING_REINDEX"
SYNC_STATUS_PENDING_DELETE = "PENDING_DELETE"
SYNC_STATUS_DELETED = "DELETED"
SYNC_STATUS_FAILED = "FAILED"


def _now_iso() -> str:
    """返回当前 ISO 时间字符串"""
    return datetime.now().isoformat()


def _generate_skill_id() -> str:
    """生成唯一的知识卡片 ID，格式: skill_YYYYMMDD_xxxx"""
    now = datetime.now()
    short_uuid = uuid.uuid4().hex[:8]
    return f"skill_{now.strftime('%Y%m%d')}_{short_uuid}"


def ensure_category_supported(category: str):
    """校验分类合法性"""
    if category not in CATEGORIES:
        raise ValueError(
            f"不支持的分类: '{category}'。"
            f"支持的分类: {', '.join(CATEGORIES)}"
        )


def parse_tags(tags: Optional[Union[str, List[str]]]) -> List[str]:
    """统一解析标签输入，支持逗号分隔字符串或列表"""
    if not tags:
        return []

    if isinstance(tags, str):
        raw_tags = tags.split(",")
    else:
        raw_tags = tags

    normalized: List[str] = []
    seen = set()
    for tag in raw_tags:
        clean_tag = str(tag).strip()
        if clean_tag and clean_tag not in seen:
            normalized.append(clean_tag)
            seen.add(clean_tag)
    return normalized


def calculate_content_hash(content: str) -> str:
    """计算正文内容哈希，用于检测飞书内容漂移"""
    normalized = (content or "").strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def extract_content_from_markdown(markdown_text: str) -> str:
    """从 SkillCard 写入飞书的 Markdown 中提取原始正文内容。"""
    if not markdown_text:
        return ""

    normalized = markdown_text.replace("\r\n", "\n")
    separator = "\n---\n"
    if separator not in normalized:
        return normalized.strip()

    body = normalized.split(separator, 1)[1]
    return body.lstrip("\n").strip()


@dataclass
class SkillCard:

    """
    知识卡片数据结构

    Attributes:
        title: 知识标题（简洁明了）
        content: 知识内容（Markdown 格式，包含背景、方案、代码示例等）
        category: 分类（架构方案/产品迭代/优化沉淀/避坑记录/最佳实践/工具使用/业务知识）
        project: 关联项目名（可选）
        tags: 标签列表（可选）
        skill_id: 唯一业务标识（自动生成）
        created_at: 创建时间（自动生成）
        updated_at: 更新时间（自动生成）
        source: 来源（默认 ai_conversation）
        feishu_doc_url: 飞书文档链接（创建后回填）
        feishu_doc_token: 飞书文档实体 token（创建后回填）
        wiki_node_token: 飞书知识库挂载节点 token（创建后回填）
        content_hash: 正文内容哈希
        version: 逻辑版本号
        sync_status: 当前同步状态
        deleted: 是否已删除
        last_error: 最近一次失败原因
    """

    title: str
    content: str
    category: str
    project: str = ""
    tags: List[str] = field(default_factory=list)
    skill_id: str = field(default_factory=_generate_skill_id)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    source: str = "ai_conversation"
    feishu_doc_url: Optional[str] = None
    feishu_doc_token: Optional[str] = None
    wiki_node_token: Optional[str] = None
    content_hash: str = ""
    version: int = 1
    sync_status: str = SYNC_STATUS_PENDING_INDEX
    deleted: bool = False
    last_error: Optional[str] = None

    def __post_init__(self):
        ensure_category_supported(self.category)
        self.tags = parse_tags(self.tags)
        if not self.content_hash:
            self.content_hash = calculate_content_hash(self.content)

    @property
    def id(self) -> str:
        """兼容旧字段命名，统一映射到 skill_id"""
        return self.skill_id

    @property
    def full_title(self) -> str:
        """带项目前缀的完整标题，用于飞书文档标题"""
        if self.project:
            return f"[{self.project}] {self.title}"
        return self.title

    @property
    def searchable_text(self) -> str:
        """
        用于向量化的可搜索文本
        拼接：标题 + 项目 + 标签 + 内容，提高检索召回率
        """
        parts = [self.title]
        if self.project:
            parts.append(self.project)
        if self.tags:
            parts.append(" ".join(self.tags))
        parts.append(self.content)
        return "\n".join([part for part in parts if part])

    def to_markdown(self) -> str:
        """
        生成写入飞书文档的 Markdown 格式内容

        格式：
        # 标题
        > 分类 | 项目 | 标签 | 更新时间
        ---
        正文内容
        """
        lines = [f"# {self.full_title}", ""]

        meta_parts = [f"📂 分类：{self.category}"]
        if self.project:
            meta_parts.append(f"🏷️ 项目：{self.project}")
        if self.tags:
            meta_parts.append(f"🔖 标签：{', '.join(self.tags)}")
        meta_parts.append(f"🆔 技能ID：{self.skill_id}")
        meta_parts.append(f"🕒 更新时间：{self.updated_at[:19]}")

        lines.append("> " + " | ".join(meta_parts))
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(self.content)

        return "\n".join(lines)

    def to_metadata(self) -> Dict[str, Any]:
        """
        生成写入向量数据库的元数据（payload）
        不包含 content 本身（content 会单独存储）
        """
        return {
            "id": self.skill_id,
            "skill_id": self.skill_id,
            "title": self.title,
            "full_title": self.full_title,
            "category": self.category,
            "project": self.project,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "feishu_doc_url": self.feishu_doc_url or "",
            "feishu_doc_token": self.feishu_doc_token or "",
            "wiki_node_token": self.wiki_node_token or "",
            "content_hash": self.content_hash,
            "version": self.version,
            "sync_status": self.sync_status,
            "deleted": self.deleted,
            "last_error": self.last_error or "",
        }

    def to_registry_dict(self) -> Dict[str, Any]:
        """转为注册表字典，不包含正文内容"""
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "category": self.category,
            "project": self.project,
            "tags": self.tags,
            "feishu_doc_url": self.feishu_doc_url or "",
            "feishu_doc_token": self.feishu_doc_token or "",
            "wiki_node_token": self.wiki_node_token or "",
            "content_hash": self.content_hash,
            "version": self.version,
            "sync_status": self.sync_status,
            "deleted": self.deleted,
            "source": self.source,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> Dict[str, Any]:
        """转为完整字典，用于序列化或回传"""
        return {
            **self.to_registry_dict(),
            "id": self.skill_id,
            "content": self.content,
        }

    @classmethod
    def from_registry(cls, data: Dict[str, Any], content: str) -> "SkillCard":
        """从注册表记录和正文内容恢复 SkillCard"""
        return cls(
            title=data.get("title", ""),
            content=content,
            category=data.get("category", "最佳实践"),
            project=data.get("project", ""),
            tags=data.get("tags", []),
            skill_id=data.get("skill_id") or data.get("id") or _generate_skill_id(),
            created_at=data.get("created_at") or _now_iso(),
            updated_at=data.get("updated_at") or _now_iso(),
            source=data.get("source", "ai_conversation"),
            feishu_doc_url=data.get("feishu_doc_url") or None,
            feishu_doc_token=data.get("feishu_doc_token") or None,
            wiki_node_token=data.get("wiki_node_token") or None,
            content_hash=data.get("content_hash") or calculate_content_hash(content),
            version=int(data.get("version") or 1),
            sync_status=data.get("sync_status", SYNC_STATUS_PENDING_INDEX),
            deleted=bool(data.get("deleted", False)),
            last_error=data.get("last_error") or None,
        )