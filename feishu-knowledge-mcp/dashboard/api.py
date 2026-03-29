"""
Dashboard REST API

提供 Dashboard 前端所需的所有 REST API 接口。
包括总览统计、操作记录列表、趋势分析、热门知识等。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select

from tools.automation_review import (
    approve_review_item_record,
    batch_review_items,
    reject_review_item_record,
)

from .models import OperationLog, SearchHit

logger = logging.getLogger(__name__)


DEFAULT_EXCEPTION_STATUSES = ["PENDING_INDEX", "PENDING_REINDEX", "PENDING_DELETE", "FAILED"]
SEARCH_SUCCESS_STATUS = "success"


class RejectReviewPayload(BaseModel):
    reason: str = ""


class BatchReviewPayload(BaseModel):
    action: str = Field(..., description="approve 或 reject")
    review_ids: list[str] = Field(default_factory=list)
    reason: str = ""


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_exception_counts(sync_status_distribution, deleted_count=0):
    pending_index = _safe_int(sync_status_distribution.get("PENDING_INDEX"))
    pending_reindex = _safe_int(sync_status_distribution.get("PENDING_REINDEX"))
    pending_delete = _safe_int(sync_status_distribution.get("PENDING_DELETE"))
    failed = _safe_int(sync_status_distribution.get("FAILED"))
    deleted = _safe_int(deleted_count)
    active_exceptions = pending_index + pending_reindex + pending_delete + failed

    return {
        "pending_index": pending_index,
        "pending_reindex": pending_reindex,
        "pending_delete": pending_delete,
        "failed": failed,
        "deleted": deleted,
        "active_exceptions": active_exceptions,
        "total_exceptions": active_exceptions + deleted,
    }


def create_api_router(
    dashboard_logger,
    vector_store,
    registry_store=None,
    config=None,
    embedder=None,
    feishu_doc_manager=None,
):
    """
    创建 API 路由，注入依赖

    Args:
        dashboard_logger: Dashboard 日志记录器
        vector_store: 向量数据库实例
        registry_store: 知识注册表实例（可选）

    Returns:
        FastAPI APIRouter
    """
    router = APIRouter(prefix="/api")

    def _service_info() -> dict:
        if config is None:
            return {}
        provider = config.get("_service_info_provider")
        if callable(provider):
            try:
                return provider()
            except Exception as exc:
                logger.warning("读取服务运行信息失败: %s", exc)
        return {}

    def _automation_runtime_ready(require_save_runtime: bool = False):
        if registry_store is None:
            return False, "知识注册表未初始化"
        if require_save_runtime and (config is None or embedder is None or feishu_doc_manager is None):
            return False, "自动沉淀审批依赖未完整注入"
        return True, ""

    @router.get("/stats/overview")
    async def get_overview():
        """
        总览统计

        Returns:
            total_skills: 知识总数
            today_saved: 今日新增
            week_saved: 本周新增
            total_searches: 成功检索次数
            total_search_attempts: 检索请求总次数（含失败）
            failed_searches: 检索失败次数
            category_distribution: 分类分布（优先使用注册表实时状态）
            project_distribution: 项目分布（优先使用注册表实时状态）
            sync_status_distribution: 同步状态分布（注册表口径）
        """
        try:
            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            week_ago = today - timedelta(days=7)

            async with dashboard_logger.async_session() as session:
                today_result = await session.execute(
                    select(func.count(OperationLog.id)).where(
                        OperationLog.operation == "save",
                        OperationLog.status == "success",
                        OperationLog.timestamp >= today,
                    )
                )
                today_saved = today_result.scalar() or 0

                week_result = await session.execute(
                    select(func.count(OperationLog.id)).where(
                        OperationLog.operation == "save",
                        OperationLog.status == "success",
                        OperationLog.timestamp >= week_ago,
                    )
                )
                week_saved = week_result.scalar() or 0

                search_result = await session.execute(
                    select(func.count(OperationLog.id)).where(
                        OperationLog.operation == "search",
                        OperationLog.status == SEARCH_SUCCESS_STATUS,
                    )
                )
                total_searches = search_result.scalar() or 0

                search_attempt_result = await session.execute(
                    select(func.count(OperationLog.id)).where(
                        OperationLog.operation == "search",
                    )
                )
                total_search_attempts = search_attempt_result.scalar() or 0

                search_failed_result = await session.execute(
                    select(func.count(OperationLog.id)).where(
                        OperationLog.operation == "search",
                        OperationLog.status == "failed",
                    )
                )
                failed_searches = search_failed_result.scalar() or 0

                category_distribution = {}
                project_distribution = {}
                if registry_store is None:
                    category_result = await session.execute(
                        select(
                            OperationLog.skill_category,
                            func.count(OperationLog.id),
                        )
                        .where(
                            OperationLog.operation == "save",
                            OperationLog.status == "success",
                        )
                        .group_by(OperationLog.skill_category)
                    )
                    category_distribution = {
                        row[0]: row[1] for row in category_result.all() if row[0]
                    }

                    project_result = await session.execute(
                        select(
                            OperationLog.skill_project,
                            func.count(OperationLog.id),
                        )
                        .where(
                            OperationLog.operation == "save",
                            OperationLog.status == "success",
                        )
                        .group_by(OperationLog.skill_project)
                    )
                    project_distribution = {
                        row[0] or "未关联项目": row[1] for row in project_result.all()
                    }

            sync_status_distribution = {}
            exception_counts = {}
            if registry_store is not None:
                overview_stats = await registry_store.get_overview_stats()
                total_skills = overview_stats["total_skills"]
                category_distribution = overview_stats["category_distribution"]
                project_distribution = overview_stats["project_distribution"]
                sync_status_distribution = overview_stats["sync_status_distribution"]
                exception_counts = _build_exception_counts(
                    sync_status_distribution,
                    deleted_count=await registry_store.count_records(deleted=True),
                )
            else:
                collection_info = vector_store.get_collection_info()
                total_skills = collection_info.get("points_count", 0)

            return {
                "total_skills": total_skills,
                "today_saved": today_saved,
                "week_saved": week_saved,
                "total_searches": total_searches,
                "total_search_attempts": total_search_attempts,
                "failed_searches": failed_searches,
                "category_distribution": category_distribution,
                "project_distribution": project_distribution,
                "sync_status_distribution": sync_status_distribution,
                "exception_counts": exception_counts,
            }

        except Exception as e:
            logger.error(f"获取总览统计失败: {e}")
            return {"error": str(e)}

    @router.get("/automation/overview")
    async def get_automation_overview():
        """自动化闭环总览：会话数、自动保存数、待审核数、失败数。"""
        try:
            ready, message = _automation_runtime_ready()
            if not ready:
                return {"error": message}
            automation_stats = await registry_store.get_automation_overview_stats()
            governance_stats = await registry_store.get_governance_overview_stats()
            return {
                **automation_stats,
                "governance": governance_stats,
            }
        except Exception as e:
            logger.error(f"获取自动化总览失败: {e}")
            return {"error": str(e)}

    @router.get("/governance/overview")
    async def get_governance_overview():
        """知识治理总览：创建 / 合并 / 复用建议与审批结果。"""
        try:
            ready, message = _automation_runtime_ready()
            if not ready:
                return {"error": message}
            return await registry_store.get_governance_overview_stats()
        except Exception as e:
            logger.error(f"获取治理总览失败: {e}")
            return {"error": str(e)}

    @router.get("/runtime/remote-service")
    async def get_remote_service_runtime():
        """返回正式远程服务化相关的运行态信息。"""
        try:
            info = _service_info()
            return {
                "service": info.get("service", "feishu-knowledge-mcp"),
                "mcp": info.get("mcp", {}),
                "dashboard": info.get("dashboard", {}),
                "remote_service": info.get("remote_service", {}),
            }
        except Exception as e:
            logger.error(f"获取远程服务运行态失败: {e}")
            return {"error": str(e)}

    @router.get("/automation/sessions")
    async def get_automation_sessions(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        """自动化会话列表。"""
        try:
            ready, message = _automation_runtime_ready()
            if not ready:
                return {"error": message}

            offset = (page - 1) * page_size
            sessions = await registry_store.list_automation_sessions(limit=page_size, offset=offset)
            total = await registry_store.count_automation_sessions()
            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "sessions": sessions,
            }
        except Exception as e:
            logger.error(f"获取自动化会话列表失败: {e}")
            return {"error": str(e)}

    @router.get("/automation/reviews")
    async def get_automation_reviews(
        status: Optional[str] = Query(None, description="审核状态"),
        session_id: Optional[str] = Query(None, description="会话ID"),
        project: Optional[str] = Query(None, description="项目"),
        confidence: Optional[str] = Query(None, description="置信度"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        """自动沉淀审核队列。"""
        try:
            ready, message = _automation_runtime_ready()
            if not ready:
                return {"error": message}

            offset = (page - 1) * page_size
            items = await registry_store.list_review_items(
                status=status or "",
                session_id=session_id or "",
                project=project or "",
                confidence=confidence or "",
                limit=page_size,
                offset=offset,
            )
            total = await registry_store.count_review_items(
                status=status or "",
                session_id=session_id or "",
                project=project or "",
                confidence=confidence or "",
            )
            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "items": items,
            }
        except Exception as e:
            logger.error(f"获取审核队列失败: {e}")
            return {"error": str(e)}

    @router.post("/automation/reviews/{review_id}/approve")
    async def approve_automation_review(review_id: str):
        """审批通过指定审核项，并复用现有保存链路完成入库。"""
        try:
            ready, message = _automation_runtime_ready(require_save_runtime=True)
            if not ready:
                return {"error": message}

            result = await approve_review_item_record(
                review_id,
                config=config,
                embedder=embedder,
                vector_store=vector_store,
                feishu_doc_manager=feishu_doc_manager,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
            )
            return {
                "status": result.get("status") or "success",
                "message": result.get("text") or "✅ 审核项已通过。",
                "review": result.get("review"),
                "card_skill_id": getattr(result.get("card"), "skill_id", "") if result.get("card") is not None else "",
            }
        except Exception as e:
            logger.error(f"审批审核项失败: {e}")
            return {"error": str(e)}

    @router.post("/automation/reviews/{review_id}/reject")
    async def reject_automation_review(review_id: str, payload: RejectReviewPayload = Body(default_factory=RejectReviewPayload)):
        """驳回指定审核项。"""
        try:
            ready, message = _automation_runtime_ready()
            if not ready:
                return {"error": message}

            result = await reject_review_item_record(
                review_id,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
                reason=payload.reason,
            )
            return {
                "status": result.get("status") or "success",
                "message": result.get("text") or "🗑️ 审核项已驳回。",
                "review": result.get("review"),
            }
        except Exception as e:
            logger.error(f"驳回审核项失败: {e}")
            return {"error": str(e)}

    @router.post("/automation/reviews/batch")
    async def batch_handle_automation_reviews(payload: BatchReviewPayload):
        """批量审批或驳回审核队列项。"""
        try:
            ready, message = _automation_runtime_ready(require_save_runtime=payload.action.strip().lower() == "approve")
            if not ready:
                return {"error": message}

            result = await batch_review_items(
                action=payload.action,
                review_ids=payload.review_ids,
                config=config,
                embedder=embedder,
                vector_store=vector_store,
                feishu_doc_manager=feishu_doc_manager,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
                reason=payload.reason,
            )
            return result
        except Exception as e:
            logger.error(f"批量处理审核项失败: {e}")
            return {"error": str(e)}

    @router.get("/registry/records")
    async def get_registry_records(
        status: Optional[str] = Query(None, description="单个同步状态"),
        statuses: Optional[str] = Query(None, description="多个同步状态，逗号分隔"),
        deleted: Optional[bool] = Query(None, description="是否已删除"),
        category: Optional[str] = Query(None, description="筛选分类"),
        project: Optional[str] = Query(None, description="筛选项目"),
        project_is_empty: bool = Query(False, description="仅筛选未关联项目"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        """注册表知识列表，支持按状态和删除标记筛选。"""
        try:
            if registry_store is None:
                return {"error": "知识注册表未初始化"}

            status_list = [item.strip() for item in (statuses or "").split(",") if item.strip()]
            offset = (page - 1) * page_size

            records = await registry_store.list_records(
                category=category or "",
                project=project or "",
                project_is_empty=project_is_empty,
                sync_status=status or "",
                statuses=status_list,
                deleted=deleted,
                limit=page_size,
                offset=offset,
            )
            total = await registry_store.count_records(
                category=category or "",
                project=project or "",
                project_is_empty=project_is_empty,
                sync_status=status or "",
                statuses=status_list,
                deleted=deleted,
            )

            overview_stats = await registry_store.get_overview_stats()
            sync_status_distribution = overview_stats.get("sync_status_distribution", {})
            project_distribution = overview_stats.get("project_distribution", {})
            category_distribution = overview_stats.get("category_distribution", {})

            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "records": records,
                "filter_options": {
                    "categories": sorted(category_distribution.keys()),
                    "projects": sorted(project_distribution.keys()),
                    "sync_statuses": sorted(sync_status_distribution.keys()),
                },
            }
        except Exception as e:
            logger.error(f"获取注册表知识列表失败: {e}")
            return {"error": str(e)}

    @router.get("/registry/exceptions")
    async def get_registry_exceptions(
        include_deleted: bool = Query(True, description="是否包含已删除知识"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        """异常知识视图：待索引、待重建、待删除补偿、失败和已删除知识。"""
        try:
            if registry_store is None:
                return {"error": "知识注册表未初始化"}

            exception_records = await registry_store.list_records(
                statuses=DEFAULT_EXCEPTION_STATUSES,
                limit=None,
            )
            record_map = {record["skill_id"]: record for record in exception_records}

            if include_deleted:
                deleted_records = await registry_store.list_records(
                    deleted=True,
                    limit=None,
                )
                for record in deleted_records:
                    record_map[record["skill_id"]] = record

            merged_records = sorted(
                record_map.values(),
                key=lambda item: item.get("updated_at") or item.get("created_at") or "",
                reverse=True,
            )

            total = len(merged_records)
            offset = (page - 1) * page_size
            page_records = merged_records[offset: offset + page_size]

            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "records": page_records,
                "statuses": DEFAULT_EXCEPTION_STATUSES,
                "include_deleted": include_deleted,
            }
        except Exception as e:
            logger.error(f"获取异常知识视图失败: {e}")
            return {"error": str(e)}

    @router.get("/logs/list")
    async def get_logs(
        operation: Optional[str] = Query(None, description="筛选操作类型"),
        project: Optional[str] = Query(None, description="筛选项目"),
        category: Optional[str] = Query(None, description="筛选分类"),
        date_from: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        """
        操作记录列表

        支持按操作类型、项目、分类、日期范围筛选 + 分页
        """
        try:
            async with dashboard_logger.async_session() as session:
                query = select(OperationLog).order_by(desc(OperationLog.timestamp))

                if operation:
                    query = query.where(OperationLog.operation == operation)
                if project:
                    query = query.where(OperationLog.skill_project == project)
                if category:
                    query = query.where(OperationLog.skill_category == category)
                if date_from:
                    query = query.where(
                        OperationLog.timestamp >= datetime.fromisoformat(date_from)
                    )
                if date_to:
                    dt_to = datetime.fromisoformat(date_to) + timedelta(days=1)
                    query = query.where(OperationLog.timestamp < dt_to)

                count_query = select(func.count()).select_from(query.subquery())
                total_result = await session.execute(count_query)
                total = total_result.scalar() or 0

                offset = (page - 1) * page_size
                query = query.offset(offset).limit(page_size)

                result = await session.execute(query)
                logs = [log.to_dict() for log in result.scalars().all()]

            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "logs": logs,
            }

        except Exception as e:
            logger.error(f"获取操作记录列表失败: {e}")
            return {"error": str(e)}

    @router.get("/stats/trend")
    async def get_trend(days: int = Query(30, ge=1, le=365)):
        """
        沉淀趋势

        返回最近 N 天每天的沉淀数和检索数
        """
        try:
            start_date = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=days - 1)

            async with dashboard_logger.async_session() as session:
                result = await session.execute(
                    select(OperationLog)
                    .where(OperationLog.timestamp >= start_date)
                    .where(OperationLog.status == SEARCH_SUCCESS_STATUS)
                )
                logs = result.scalars().all()

            daily_data = {}
            for i in range(days):
                date = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
                daily_data[date] = {"save": 0, "search": 0}

            for log in logs:
                if log.timestamp:
                    date_str = log.timestamp.strftime("%Y-%m-%d")
                    if date_str in daily_data:
                        if log.operation == "save":
                            daily_data[date_str]["save"] += 1
                        elif log.operation == "search":
                            daily_data[date_str]["search"] += 1

            trend = [
                {"date": date, "saves": data["save"], "searches": data["search"]}
                for date, data in sorted(daily_data.items())
            ]

            return {"days": days, "trend": trend}

        except Exception as e:
            logger.error(f"获取趋势数据失败: {e}")
            return {"error": str(e)}

    @router.get("/stats/hot-queries")
    async def get_hot_queries(top_k: int = Query(10, ge=1, le=50)):
        """
        热门查询词

        统计最常被检索的关键词
        """
        try:
            async with dashboard_logger.async_session() as session:
                result = await session.execute(
                    select(
                        OperationLog.search_query,
                        func.count(OperationLog.id).label("count"),
                    )
                    .where(OperationLog.operation == "search")
                    .where(OperationLog.status == SEARCH_SUCCESS_STATUS)
                    .where(OperationLog.search_query.isnot(None))
                    .group_by(OperationLog.search_query)
                    .order_by(desc("count"))
                    .limit(top_k)
                )
                hot_queries = [
                    {"query": row[0], "count": row[1]}
                    for row in result.all()
                    if row[0]
                ]

            return {"hot_queries": hot_queries}

        except Exception as e:
            logger.error(f"获取热门查询词失败: {e}")
            return {"error": str(e)}

    @router.get("/stats/hot-skills")
    async def get_hot_skills(
        top_k: int = Query(10, ge=1, le=50),
        days: int = Query(30, ge=1, le=365),
    ):
        """
        热门知识

        统计最近 N 天被检索命中次数最多的知识。
        """
        try:
            start_time = datetime.utcnow() - timedelta(days=days)

            async with dashboard_logger.async_session() as session:
                result = await session.execute(
                    select(
                        SearchHit.skill_id,
                        func.count(SearchHit.id).label("hit_count"),
                        func.max(SearchHit.timestamp).label("last_hit_at"),
                        func.min(SearchHit.rank).label("best_rank"),
                        func.max(SearchHit.skill_title).label("skill_title"),
                        func.max(SearchHit.skill_category).label("skill_category"),
                        func.max(SearchHit.skill_project).label("skill_project"),
                        func.max(SearchHit.feishu_doc_url).label("feishu_doc_url"),
                    )
                    .where(SearchHit.timestamp >= start_time)
                    .group_by(SearchHit.skill_id)
                    .order_by(desc("hit_count"), desc("last_hit_at"))
                    .limit(top_k)
                )
                rows = result.all()

            hot_skills = []
            for row in rows:
                record = None
                if registry_store is not None:
                    record = await registry_store.get(row.skill_id)

                hot_skills.append(
                    {
                        "skill_id": row.skill_id,
                        "title": (record or {}).get("title") or row.skill_title or row.skill_id,
                        "category": (record or {}).get("category") or row.skill_category or "",
                        "project": (record or {}).get("project") or row.skill_project or "",
                        "feishu_doc_url": (record or {}).get("feishu_doc_url") or row.feishu_doc_url or "",
                        "hit_count": int(row.hit_count or 0),
                        "last_hit_at": row.last_hit_at.isoformat() if row.last_hit_at else None,
                        "best_rank": int(row.best_rank or 0),
                        "deleted": bool((record or {}).get("deleted", False)),
                        "sync_status": (record or {}).get("sync_status") or "",
                    }
                )

            return {
                "days": days,
                "hot_skills": hot_skills,
            }

        except Exception as e:
            logger.error(f"获取热门知识失败: {e}")
            return {"error": str(e)}

    return router