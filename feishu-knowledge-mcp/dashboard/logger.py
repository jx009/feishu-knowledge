"""
Dashboard 操作日志记录器

在每次 MCP 工具调用时自动记录操作日志到 PostgreSQL（或其他 SQLAlchemy 兼容数据库）。
提供 log_save、log_search、log_update、log_delete、log_sync 方法。
"""

import json
import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from .models import Base, OperationLog, SearchHit

logger = logging.getLogger(__name__)


class DashboardLogger:
    """
    操作日志记录器

    与 MCP 工具集成，每次工具调用时记录一条操作日志。
    默认运行配置使用 PostgreSQL，测试中也兼容其他 SQLAlchemy 异步连接串。
    """

    def __init__(self, database_url: str):
        self.database_url = self._normalize_database_url(database_url)
        self.engine = create_async_engine(
            self.database_url,
            echo=False,
            pool_pre_ping=True,
        )
        self.async_session = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info(f"Dashboard 日志记录器初始化: {self.database_url}")

    @staticmethod
    def _normalize_database_url(database_url: str) -> str:
        if "://" in database_url:
            return database_url
        return f"sqlite+aiosqlite:///{database_url}"

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Dashboard 数据库初始化完成")

    async def healthcheck(self):
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self):
        await self.engine.dispose()

    async def log_save(
        self,
        skill_id: str,
        title: str,
        category: str,
        project: str,
        tags: List[str],
        content: str,
        feishu_folder: str,
        feishu_url: str,
        feishu_doc_token: str = "",
        wiki_node_token: str = "",
        sync_status: str = "",
        status: str = "success",
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="save",
                tool_name="save_skill",
                skill_id=skill_id,
                skill_title=title,
                skill_category=category,
                skill_project=project,
                skill_tags=json.dumps(tags, ensure_ascii=False),
                content_preview=content[:200] if content else "",
                feishu_folder=feishu_folder,
                feishu_doc_url=feishu_url,
                feishu_doc_token=feishu_doc_token,
                wiki_node_token=wiki_node_token,
                sync_status=sync_status,
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()

    async def log_search(
        self,
        query: str,
        results_count: int,
        top_score: float,
        status: str = "success",
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="search",
                tool_name="search_skill",
                search_query=query,
                search_results_count=results_count,
                search_top_score=top_score,
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()

    async def log_search_hits(self, query: str, results: List[dict]):
        if not results:
            return

        async with self.async_session() as session:
            for index, result in enumerate(results, 1):
                metadata = result.get("metadata", {}) or {}
                skill_id = result.get("skill_id") or metadata.get("skill_id") or result.get("id", "")
                if not skill_id:
                    continue

                hit = SearchHit(
                    search_query=query,
                    skill_id=skill_id,
                    skill_title=metadata.get("title", ""),
                    skill_category=metadata.get("category", ""),
                    skill_project=metadata.get("project", ""),
                    feishu_doc_url=metadata.get("feishu_doc_url", ""),
                    rank=index,
                    score=float(result.get("score") or 0.0),
                )
                session.add(hit)

            await session.commit()

    async def log_update(
        self,
        skill_id: str,
        title: str,
        category: str,
        project: str = "",
        tags: Optional[List[str]] = None,
        feishu_url: str = "",
        feishu_doc_token: str = "",
        wiki_node_token: str = "",
        sync_status: str = "",
        status: str = "success",
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="update",
                tool_name="update_skill",
                skill_id=skill_id,
                skill_title=title,
                skill_category=category,
                skill_project=project,
                skill_tags=json.dumps(tags or [], ensure_ascii=False),
                feishu_doc_url=feishu_url,
                feishu_doc_token=feishu_doc_token,
                wiki_node_token=wiki_node_token,
                sync_status=sync_status,
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()

    async def log_delete(
        self,
        skill_id: str,
        title: str,
        category: str,
        project: str = "",
        tags: Optional[List[str]] = None,
        feishu_url: str = "",
        feishu_doc_token: str = "",
        wiki_node_token: str = "",
        sync_status: str = "",
        status: str = "success",
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="delete",
                tool_name="delete_skill",
                skill_id=skill_id,
                skill_title=title,
                skill_category=category,
                skill_project=project,
                skill_tags=json.dumps(tags or [], ensure_ascii=False),
                feishu_doc_url=feishu_url,
                feishu_doc_token=feishu_doc_token,
                wiki_node_token=wiki_node_token,
                sync_status=sync_status,
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()

    async def log_sync(
        self,
        skill_id: str,
        title: str,
        category: str,
        sync_status: str,
        status: str = "success",
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="sync",
                tool_name="sync_manager",
                skill_id=skill_id,
                skill_title=title,
                skill_category=category,
                sync_status=sync_status,
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()

    async def log_extract(
        self,
        source_text: str,
        candidate_count: int,
        project: str = "",
        status: str = "success",
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="extract",
                tool_name="extract_skills",
                skill_project=project,
                content_preview=(source_text or "")[:400],
                search_results_count=candidate_count,
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()

    async def log_automation(
        self,
        session_id: str,
        stage: str,
        status: str,
        project: str = "",
        query: str = "",
        content_preview: str = "",
        result_count: int = 0,
        top_score: float = 0.0,
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="automation",
                tool_name=f"automation_{stage}",
                skill_id=session_id,
                skill_project=project,
                search_query=query,
                search_results_count=result_count,
                search_top_score=top_score,
                content_preview=(content_preview or "")[:400],
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()

    async def log_remote_access(
        self,
        *,
        request_id: str,
        client_id: str,
        path: str,
        method: str,
        status: str,
        error: Optional[str] = None,
    ):
        async with self.async_session() as session:
            log = OperationLog(
                operation="remote_access",
                tool_name=f"{method.upper()} {path}",
                skill_id=request_id,
                skill_project=client_id,
                search_query=path,
                content_preview=f"client={client_id} method={method.upper()} path={path}",
                status=status,
                error_message=error,
            )
            session.add(log)
            await session.commit()
