"""
Dashboard 数据模型

使用 SQLAlchemy ORM 定义操作日志表和知识注册表结构。
每次 MCP 工具调用都会记录一条操作日志；每条受管知识都会在注册表中保留唯一记录。
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class SkillRegistry(Base):
    """
    知识运行元数据注册表

    只存储运行元数据、映射关系和生命周期状态，不存储知识正文。
    """

    __tablename__ = "skill_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(String(200), unique=True, index=True, nullable=False)
    title = Column(String(500), nullable=False)
    category = Column(String(100), index=True, nullable=False)
    project = Column(String(200), index=True, default="")
    tags = Column(Text, default="[]")

    feishu_doc_url = Column(String(500), default="")
    feishu_doc_token = Column(String(200), index=True, default="")
    wiki_node_token = Column(String(200), index=True, default="")

    content_hash = Column(String(128), index=True, default="")
    version = Column(Integer, default=1)
    sync_status = Column(String(50), index=True, default="PENDING_INDEX")
    deleted = Column(Boolean, default=False, index=True)
    source = Column(String(100), default="ai_conversation")
    last_error = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "category": self.category,
            "project": self.project,
            "tags": self.tags,
            "feishu_doc_url": self.feishu_doc_url,
            "feishu_doc_token": self.feishu_doc_token,
            "wiki_node_token": self.wiki_node_token,
            "content_hash": self.content_hash,
            "version": self.version,
            "sync_status": self.sync_status,
            "deleted": self.deleted,
            "source": self.source,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class OperationLog(Base):
    """
    操作日志表

    每次 MCP 工具调用（save/search/update/delete/sync）都记录一条日志，
    用于 Dashboard 的统计展示和操作追溯。
    """

    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    operation = Column(String(50), index=True)
    tool_name = Column(String(100))

    skill_id = Column(String(200), index=True)
    skill_title = Column(String(500))
    skill_category = Column(String(100), index=True)
    skill_project = Column(String(200), index=True)
    skill_tags = Column(Text)
    content_preview = Column(Text)

    feishu_folder = Column(String(200))
    feishu_doc_url = Column(String(500))
    feishu_doc_token = Column(String(200), index=True)
    wiki_node_token = Column(String(200), index=True)
    sync_status = Column(String(50), index=True)

    search_query = Column(Text)
    search_results_count = Column(Integer)
    search_top_score = Column(Float)

    status = Column(String(20), default="success", index=True)
    error_message = Column(Text)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "operation": self.operation,
            "tool_name": self.tool_name,
            "skill_id": self.skill_id,
            "skill_title": self.skill_title,
            "skill_category": self.skill_category,
            "skill_project": self.skill_project,
            "skill_tags": self.skill_tags,
            "content_preview": self.content_preview,
            "feishu_folder": self.feishu_folder,
            "feishu_doc_url": self.feishu_doc_url,
            "feishu_doc_token": self.feishu_doc_token,
            "wiki_node_token": self.wiki_node_token,
            "sync_status": self.sync_status,
            "search_query": self.search_query,
            "search_results_count": self.search_results_count,
            "search_top_score": self.search_top_score,
            "status": self.status,
            "error_message": self.error_message,
        }


class SearchHit(Base):
    """
    搜索命中明细表

    每次 search_skill 返回结果时，会为每个命中的知识记录一条明细，
    用于热门知识统计和后续行为分析。
    """

    __tablename__ = "search_hits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    search_query = Column(Text, nullable=False)

    skill_id = Column(String(200), index=True, nullable=False)
    skill_title = Column(String(500))
    skill_category = Column(String(100), index=True)
    skill_project = Column(String(200), index=True)
    feishu_doc_url = Column(String(500))

    rank = Column(Integer, default=0)
    score = Column(Float, default=0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "search_query": self.search_query,
            "skill_id": self.skill_id,
            "skill_title": self.skill_title,
            "skill_category": self.skill_category,
            "skill_project": self.skill_project,
            "feishu_doc_url": self.feishu_doc_url,
            "rank": self.rank,
            "score": self.score,
        }


class SyncState(Base):
    """
    同步状态表

    用于保存增量同步游标、最近一次扫描时间等轻量级状态。
    """

    __tablename__ = "sync_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    state_key = Column(String(100), unique=True, index=True, nullable=False)
    state_value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "state_key": self.state_key,
            "state_value": self.state_value,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AutomationSession(Base):
    """
    自动化会话表

    记录一次自动检索 / 自动沉淀流程的整体状态，便于定位失败环节。
    """

    __tablename__ = "automation_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(200), unique=True, index=True, nullable=False)
    project = Column(String(200), index=True, default="")
    user_goal = Column(Text, default="")
    normalized_query = Column(Text, default="")
    raw_query = Column(Text, default="")
    keywords = Column(Text, default="[]")
    retrieval_status = Column(String(20), default="pending", index=True)
    extraction_status = Column(String(20), default="pending", index=True)
    save_status = Column(String(20), default="pending", index=True)
    auto_retrieval_count = Column(Integer, default=0)
    extracted_candidates = Column(Integer, default=0)
    auto_saved_count = Column(Integer, default=0)
    review_queued_count = Column(Integer, default=0)
    discarded_count = Column(Integer, default=0)
    saved_skill_ids = Column(Text, default="[]")
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "user_goal": self.user_goal,
            "normalized_query": self.normalized_query,
            "raw_query": self.raw_query,
            "keywords": self.keywords,
            "retrieval_status": self.retrieval_status,
            "extraction_status": self.extraction_status,
            "save_status": self.save_status,
            "auto_retrieval_count": self.auto_retrieval_count,
            "extracted_candidates": self.extracted_candidates,
            "auto_saved_count": self.auto_saved_count,
            "review_queued_count": self.review_queued_count,
            "discarded_count": self.discarded_count,
            "saved_skill_ids": self.saved_skill_ids,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AutomationReviewItem(Base):
    """
    自动沉淀审核队列表

    用于保存中置信度候选知识，支持后续审核或人工转存。
    """

    __tablename__ = "automation_review_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(String(200), unique=True, index=True, nullable=False)
    session_id = Column(String(200), index=True, nullable=False)
    title = Column(String(500), nullable=False)
    category = Column(String(100), index=True, nullable=False)
    project = Column(String(200), index=True, default="")
    tags = Column(Text, default="[]")
    excerpt = Column(Text, default="")
    draft_content = Column(Text, default="")
    reasons = Column(Text, default="[]")
    source_text = Column(Text, default="")
    score = Column(Integer, default=0, index=True)
    confidence = Column(String(20), default="low", index=True)
    status = Column(String(20), default="pending", index=True)
    related_skill_id = Column(String(200), index=True, default="")
    auto_decision = Column(String(20), default="review", index=True)
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "session_id": self.session_id,
            "title": self.title,
            "category": self.category,
            "project": self.project,
            "tags": self.tags,
            "excerpt": self.excerpt,
            "draft_content": self.draft_content,
            "reasons": self.reasons,
            "source_text": self.source_text,
            "score": self.score,
            "confidence": self.confidence,
            "status": self.status,
            "related_skill_id": self.related_skill_id,
            "auto_decision": self.auto_decision,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class OperationLog(Base):
    """
    操作日志表

    每次 MCP 工具调用（save/search/update/delete/sync）都记录一条日志，
    用于 Dashboard 的统计展示和操作追溯。
    """

    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    operation = Column(String(50), index=True)
    tool_name = Column(String(100))

    skill_id = Column(String(200), index=True)
    skill_title = Column(String(500))
    skill_category = Column(String(100), index=True)
    skill_project = Column(String(200), index=True)
    skill_tags = Column(Text)
    content_preview = Column(Text)

    feishu_folder = Column(String(200))
    feishu_doc_url = Column(String(500))
    feishu_doc_token = Column(String(200), index=True)
    wiki_node_token = Column(String(200), index=True)
    sync_status = Column(String(50), index=True)

    search_query = Column(Text)
    search_results_count = Column(Integer)
    search_top_score = Column(Float)

    status = Column(String(20), default="success", index=True)
    error_message = Column(Text)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "operation": self.operation,
            "tool_name": self.tool_name,
            "skill_id": self.skill_id,
            "skill_title": self.skill_title,
            "skill_category": self.skill_category,
            "skill_project": self.skill_project,
            "skill_tags": self.skill_tags,
            "content_preview": self.content_preview,
            "feishu_folder": self.feishu_folder,
            "feishu_doc_url": self.feishu_doc_url,
            "feishu_doc_token": self.feishu_doc_token,
            "wiki_node_token": self.wiki_node_token,
            "sync_status": self.sync_status,
            "search_query": self.search_query,
            "search_results_count": self.search_results_count,
            "search_top_score": self.search_top_score,
            "status": self.status,
            "error_message": self.error_message,
        }


class SearchHit(Base):
    """
    搜索命中明细表

    每次 search_skill 返回结果时，会为每个命中的知识记录一条明细，
    用于热门知识统计和后续行为分析。
    """

    __tablename__ = "search_hits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    search_query = Column(Text, nullable=False)

    skill_id = Column(String(200), index=True, nullable=False)
    skill_title = Column(String(500))
    skill_category = Column(String(100), index=True)
    skill_project = Column(String(200), index=True)
    feishu_doc_url = Column(String(500))

    rank = Column(Integer, default=0)
    score = Column(Float, default=0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "search_query": self.search_query,
            "skill_id": self.skill_id,
            "skill_title": self.skill_title,
            "skill_category": self.skill_category,
            "skill_project": self.skill_project,
            "feishu_doc_url": self.feishu_doc_url,
            "rank": self.rank,
            "score": self.score,
        }


class SyncState(Base):
    """
    同步状态表

    用于保存增量同步游标、最近一次扫描时间等轻量级状态。
    """

    __tablename__ = "sync_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    state_key = Column(String(100), unique=True, index=True, nullable=False)
    state_value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "state_key": self.state_key,
            "state_value": self.state_value,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }