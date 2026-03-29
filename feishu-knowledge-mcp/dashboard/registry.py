import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, select

from .models import AutomationReviewItem, AutomationSession, SkillRegistry, SyncState

logger = logging.getLogger(__name__)


class SkillRegistryStore:
    """知识注册表数据访问层"""

    def __init__(self, dashboard_logger):
        self.dashboard_logger = dashboard_logger
        self.async_session = dashboard_logger.async_session

    @staticmethod
    def _normalize_tags(tags: Any) -> List[str]:
        if not tags:
            return []
        if isinstance(tags, str):
            try:
                parsed = json.loads(tags)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                return [item.strip() for item in tags.split(",") if item.strip()]
        if isinstance(tags, list):
            return [str(item).strip() for item in tags if str(item).strip()]
        return []

    @staticmethod
    def _normalize_json_list(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                return [item.strip() for item in value.split(",") if item.strip()]
        return []

    @staticmethod
    def _serialize_tags(tags: Any) -> str:
        if isinstance(tags, str):
            try:
                parsed = json.loads(tags)
                if isinstance(parsed, list):
                    return json.dumps(parsed, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        normalized = SkillRegistryStore._normalize_tags(tags)
        return json.dumps(normalized, ensure_ascii=False)

    @staticmethod
    def _serialize_json_list(values: Any) -> str:
        normalized = SkillRegistryStore._normalize_json_list(values)
        return json.dumps(normalized, ensure_ascii=False)

    def _row_to_dict(self, row: SkillRegistry) -> Dict[str, Any]:
        data = row.to_dict()
        data["tags"] = self._normalize_tags(data.get("tags"))
        return data

    def _session_row_to_dict(self, row: AutomationSession) -> Dict[str, Any]:
        data = row.to_dict()
        data["keywords"] = self._normalize_json_list(data.get("keywords"))
        data["saved_skill_ids"] = self._normalize_json_list(data.get("saved_skill_ids"))
        return data

    def _review_row_to_dict(self, row: AutomationReviewItem) -> Dict[str, Any]:
        data = row.to_dict()
        data["tags"] = self._normalize_tags(data.get("tags"))
        data["reasons"] = self._normalize_json_list(data.get("reasons"))
        return data

    async def get(self, skill_id: str) -> Optional[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SkillRegistry).where(SkillRegistry.skill_id == skill_id)
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def get_by_doc_token(self, feishu_doc_token: str) -> Optional[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SkillRegistry).where(
                    SkillRegistry.feishu_doc_token == feishu_doc_token
                )
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def get_by_wiki_node_token(self, wiki_node_token: str) -> Optional[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SkillRegistry).where(
                    SkillRegistry.wiki_node_token == wiki_node_token
                )
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def get_by_content_hash(self, content_hash: str) -> Optional[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SkillRegistry).where(
                    SkillRegistry.content_hash == content_hash,
                    SkillRegistry.deleted.is_(False),
                )
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    async def find_active_by_title(
        self,
        title: str,
        *,
        project: str = "",
        category: str = "",
    ) -> List[Dict[str, Any]]:
        async with self.async_session() as session:
            query = select(SkillRegistry).where(
                SkillRegistry.deleted.is_(False),
                SkillRegistry.title == title,
            )
            if project:
                query = query.where(SkillRegistry.project == project)
            if category:
                query = query.where(SkillRegistry.category == category)
            query = query.order_by(SkillRegistry.updated_at.desc())
            result = await session.execute(query)
            return [self._row_to_dict(row) for row in result.scalars().all()]

    async def list_recent_active_records(
        self,
        *,
        project: str = "",
        category: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        async with self.async_session() as session:
            query = (
                select(SkillRegistry)
                .where(SkillRegistry.deleted.is_(False))
                .order_by(SkillRegistry.updated_at.desc())
                .limit(max(1, limit))
            )
            if project:
                query = query.where(SkillRegistry.project == project)
            if category:
                query = query.where(SkillRegistry.category == category)
            result = await session.execute(query)
            return [self._row_to_dict(row) for row in result.scalars().all()]

    async def get_sync_state(self, state_key: str, default: str = "") -> str:
        async with self.async_session() as session:
            result = await session.execute(
                select(SyncState).where(SyncState.state_key == state_key)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return default
            return row.state_value or default

    async def set_sync_state(self, state_key: str, state_value: str) -> Dict[str, Any]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SyncState).where(SyncState.state_key == state_key)
            )
            row = result.scalar_one_or_none()

            if row is None:
                row = SyncState(state_key=state_key)
                session.add(row)

            row.state_value = state_value or ""
            row.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(row)
            return row.to_dict()

    async def upsert(self, record: Dict[str, Any]) -> Dict[str, Any]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SkillRegistry).where(
                    SkillRegistry.skill_id == record["skill_id"]
                )
            )
            row = result.scalar_one_or_none()

            if row is None:
                row = SkillRegistry(skill_id=record["skill_id"])
                session.add(row)

            row.title = record.get("title", row.title or "")
            row.category = record.get("category", row.category or "最佳实践")
            row.project = record.get("project", row.project or "")
            row.tags = self._serialize_tags(record.get("tags", row.tags or []))
            row.feishu_doc_url = record.get("feishu_doc_url", row.feishu_doc_url or "")
            row.feishu_doc_token = record.get("feishu_doc_token", row.feishu_doc_token or "")
            row.wiki_node_token = record.get("wiki_node_token", row.wiki_node_token or "")
            row.content_hash = record.get("content_hash", row.content_hash or "")
            row.version = int(record.get("version", row.version or 1))
            row.sync_status = record.get("sync_status", row.sync_status or "PENDING_INDEX")
            row.deleted = bool(record.get("deleted", row.deleted))
            row.source = record.get("source", row.source or "ai_conversation")
            row.last_error = record.get("last_error")

            if record.get("created_at") and row.created_at is None:
                row.created_at = self._parse_datetime(record["created_at"])
            if record.get("updated_at"):
                row.updated_at = self._parse_datetime(record["updated_at"])
            else:
                row.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def update_status(
        self,
        skill_id: str,
        sync_status: str,
        deleted: Optional[bool] = None,
        last_error: Optional[str] = None,
        version: Optional[int] = None,
        content_hash: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SkillRegistry).where(SkillRegistry.skill_id == skill_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            row.sync_status = sync_status
            if deleted is not None:
                row.deleted = deleted
            row.last_error = last_error
            if version is not None:
                row.version = version
            if content_hash is not None:
                row.content_hash = content_hash
            row.updated_at = datetime.utcnow()

            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def list_records(
        self,
        category: str = "",
        project: str = "",
        project_is_empty: bool = False,
        tags: Optional[List[str]] = None,
        sync_status: str = "",
        statuses: Optional[List[str]] = None,
        deleted: Optional[bool] = None,
        limit: Optional[int] = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        async with self.async_session() as session:
            query = select(SkillRegistry).order_by(SkillRegistry.updated_at.desc())

            if category:
                query = query.where(SkillRegistry.category == category)
            if project_is_empty:
                query = query.where(
                    or_(SkillRegistry.project.is_(None), SkillRegistry.project == "")
                )
            elif project:
                query = query.where(SkillRegistry.project == project)

            normalized_statuses = [status.strip() for status in (statuses or []) if status and status.strip()]
            if sync_status:
                query = query.where(SkillRegistry.sync_status == sync_status)
            elif normalized_statuses:
                query = query.where(SkillRegistry.sync_status.in_(normalized_statuses))

            if deleted is not None:
                query = query.where(SkillRegistry.deleted == deleted)
            if offset > 0:
                query = query.offset(offset)
            if limit is not None and limit > 0:
                query = query.limit(limit)

            result = await session.execute(query)
            rows = [self._row_to_dict(row) for row in result.scalars().all()]

            if tags:
                required_tags = {tag.strip() for tag in tags if tag.strip()}
                if required_tags:
                    rows = [
                        row for row in rows
                        if required_tags.issubset(set(row.get("tags", [])))
                    ]
            return rows

    async def count_records(
        self,
        category: str = "",
        project: str = "",
        project_is_empty: bool = False,
        tags: Optional[List[str]] = None,
        sync_status: str = "",
        statuses: Optional[List[str]] = None,
        deleted: Optional[bool] = None,
    ) -> int:
        required_tags = {tag.strip() for tag in (tags or []) if tag.strip()}
        if required_tags:
            rows = await self.list_records(
                category=category,
                project=project,
                project_is_empty=project_is_empty,
                tags=list(required_tags),
                sync_status=sync_status,
                statuses=statuses,
                deleted=deleted,
                limit=None,
            )
            return len(rows)

        async with self.async_session() as session:
            query = select(func.count(SkillRegistry.id))

            if category:
                query = query.where(SkillRegistry.category == category)
            if project_is_empty:
                query = query.where(
                    or_(SkillRegistry.project.is_(None), SkillRegistry.project == "")
                )
            elif project:
                query = query.where(SkillRegistry.project == project)

            normalized_statuses = [status.strip() for status in (statuses or []) if status and status.strip()]
            if sync_status:
                query = query.where(SkillRegistry.sync_status == sync_status)
            elif normalized_statuses:
                query = query.where(SkillRegistry.sync_status.in_(normalized_statuses))

            if deleted is not None:
                query = query.where(SkillRegistry.deleted == deleted)

            result = await session.execute(query)
            return int(result.scalar() or 0)

    async def list_pending_records(self, statuses: List[str]) -> List[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SkillRegistry).where(SkillRegistry.sync_status.in_(statuses))
            )
            return [self._row_to_dict(row) for row in result.scalars().all()]

    async def count_active(self) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count(SkillRegistry.id)).where(SkillRegistry.deleted.is_(False))
            )
            return int(result.scalar() or 0)

    async def upsert_automation_session(self, session_record: Dict[str, Any]) -> Dict[str, Any]:
        async with self.async_session() as session:
            result = await session.execute(
                select(AutomationSession).where(AutomationSession.session_id == session_record["session_id"])
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = AutomationSession(session_id=session_record["session_id"])
                session.add(row)

            row.project = session_record.get("project", row.project or "")
            row.user_goal = session_record.get("user_goal", row.user_goal or "")
            row.normalized_query = session_record.get("normalized_query", row.normalized_query or "")
            row.raw_query = session_record.get("raw_query", row.raw_query or "")
            row.keywords = self._serialize_json_list(session_record.get("keywords", row.keywords or []))
            row.retrieval_status = session_record.get("retrieval_status", row.retrieval_status or "pending")
            row.extraction_status = session_record.get("extraction_status", row.extraction_status or "pending")
            row.save_status = session_record.get("save_status", row.save_status or "pending")
            row.auto_retrieval_count = int(session_record.get("auto_retrieval_count", row.auto_retrieval_count or 0))
            row.extracted_candidates = int(session_record.get("extracted_candidates", row.extracted_candidates or 0))
            row.auto_saved_count = int(session_record.get("auto_saved_count", row.auto_saved_count or 0))
            row.review_queued_count = int(session_record.get("review_queued_count", row.review_queued_count or 0))
            row.discarded_count = int(session_record.get("discarded_count", row.discarded_count or 0))
            row.saved_skill_ids = self._serialize_json_list(session_record.get("saved_skill_ids", row.saved_skill_ids or []))
            row.last_error = session_record.get("last_error")
            row.updated_at = datetime.utcnow()
            if session_record.get("created_at") and row.created_at is None:
                row.created_at = self._parse_datetime(session_record["created_at"])

            await session.commit()
            await session.refresh(row)
            return self._session_row_to_dict(row)

    async def get_automation_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(AutomationSession).where(AutomationSession.session_id == session_id)
            )
            row = result.scalar_one_or_none()
            return self._session_row_to_dict(row) if row else None

    async def list_automation_sessions(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        async with self.async_session() as session:
            query = (
                select(AutomationSession)
                .order_by(AutomationSession.updated_at.desc())
                .offset(max(0, offset))
                .limit(max(1, limit))
            )
            result = await session.execute(query)
            return [self._session_row_to_dict(row) for row in result.scalars().all()]

    async def count_automation_sessions(self) -> int:
        async with self.async_session() as session:
            result = await session.execute(select(func.count(AutomationSession.id)))
            return int(result.scalar() or 0)

    async def upsert_review_item(self, review_record: Dict[str, Any]) -> Dict[str, Any]:
        async with self.async_session() as session:
            result = await session.execute(
                select(AutomationReviewItem).where(AutomationReviewItem.review_id == review_record["review_id"])
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = AutomationReviewItem(review_id=review_record["review_id"])
                session.add(row)

            row.session_id = review_record.get("session_id", row.session_id or "")
            row.title = review_record.get("title", row.title or "")
            row.category = review_record.get("category", row.category or "最佳实践")
            row.project = review_record.get("project", row.project or "")
            row.tags = self._serialize_tags(review_record.get("tags", row.tags or []))
            row.excerpt = review_record.get("excerpt", row.excerpt or "")
            row.draft_content = review_record.get("draft_content", row.draft_content or "")
            row.reasons = self._serialize_json_list(review_record.get("reasons", row.reasons or []))
            row.source_text = review_record.get("source_text", row.source_text or "")
            row.score = int(review_record.get("score", row.score or 0))
            row.confidence = review_record.get("confidence", row.confidence or "low")
            row.status = review_record.get("status", row.status or "pending")
            row.related_skill_id = review_record.get("related_skill_id", row.related_skill_id or "")
            row.auto_decision = review_record.get("auto_decision", row.auto_decision or "review")
            row.last_error = review_record.get("last_error")
            row.updated_at = datetime.utcnow()
            if review_record.get("created_at") and row.created_at is None:
                row.created_at = self._parse_datetime(review_record["created_at"])

            await session.commit()
            await session.refresh(row)
            return self._review_row_to_dict(row)

    async def get_review_item(self, review_id: str) -> Optional[Dict[str, Any]]:
        async with self.async_session() as session:
            result = await session.execute(
                select(AutomationReviewItem).where(AutomationReviewItem.review_id == review_id)
            )
            row = result.scalar_one_or_none()
            return self._review_row_to_dict(row) if row else None

    async def list_review_items(
        self,
        status: str = "",
        session_id: str = "",
        project: str = "",
        confidence: str = "",
        limit: Optional[int] = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        async with self.async_session() as session:
            query = select(AutomationReviewItem).order_by(AutomationReviewItem.updated_at.desc())
            if status:
                query = query.where(AutomationReviewItem.status == status)
            if session_id:
                query = query.where(AutomationReviewItem.session_id == session_id)
            if project:
                query = query.where(AutomationReviewItem.project == project)
            if confidence:
                query = query.where(AutomationReviewItem.confidence == confidence)
            if offset > 0:
                query = query.offset(offset)
            if limit is not None and limit > 0:
                query = query.limit(limit)

            result = await session.execute(query)
            return [self._review_row_to_dict(row) for row in result.scalars().all()]

    async def count_review_items(
        self,
        status: str = "",
        session_id: str = "",
        project: str = "",
        confidence: str = "",
    ) -> int:
        async with self.async_session() as session:
            query = select(func.count(AutomationReviewItem.id))
            if status:
                query = query.where(AutomationReviewItem.status == status)
            if session_id:
                query = query.where(AutomationReviewItem.session_id == session_id)
            if project:
                query = query.where(AutomationReviewItem.project == project)
            if confidence:
                query = query.where(AutomationReviewItem.confidence == confidence)
            result = await session.execute(query)
            return int(result.scalar() or 0)

    async def get_automation_overview_stats(self) -> Dict[str, Any]:
        async with self.async_session() as session:
            session_count_result = await session.execute(select(func.count(AutomationSession.id)))
            review_pending_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(AutomationReviewItem.status == "pending")
            )
            review_approved_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(AutomationReviewItem.status == "approved")
            )
            review_rejected_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(AutomationReviewItem.status == "rejected")
            )
            total_auto_saved_result = await session.execute(
                select(func.coalesce(func.sum(AutomationSession.auto_saved_count), 0))
            )
            total_review_queued_result = await session.execute(
                select(func.coalesce(func.sum(AutomationSession.review_queued_count), 0))
            )
            total_discarded_result = await session.execute(
                select(func.coalesce(func.sum(AutomationSession.discarded_count), 0))
            )
            retrieval_failed_result = await session.execute(
                select(func.count(AutomationSession.id)).where(AutomationSession.retrieval_status == "failed")
            )
            extraction_failed_result = await session.execute(
                select(func.count(AutomationSession.id)).where(AutomationSession.extraction_status == "failed")
            )
            save_failed_result = await session.execute(
                select(func.count(AutomationSession.id)).where(AutomationSession.save_status == "failed")
            )

        return {
            "total_sessions": int(session_count_result.scalar() or 0),
            "pending_review_items": int(review_pending_result.scalar() or 0),
            "approved_review_items": int(review_approved_result.scalar() or 0),
            "rejected_review_items": int(review_rejected_result.scalar() or 0),
            "total_auto_saved": int(total_auto_saved_result.scalar() or 0),
            "total_review_queued": int(total_review_queued_result.scalar() or 0),
            "total_discarded": int(total_discarded_result.scalar() or 0),
            "retrieval_failed_sessions": int(retrieval_failed_result.scalar() or 0),
            "extraction_failed_sessions": int(extraction_failed_result.scalar() or 0),
            "save_failed_sessions": int(save_failed_result.scalar() or 0),
        }

    async def get_governance_overview_stats(self) -> Dict[str, Any]:
        async with self.async_session() as session:
            create_new_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(AutomationReviewItem.auto_decision == "create_new")
            )
            merge_existing_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(AutomationReviewItem.auto_decision == "merge_existing")
            )
            reuse_existing_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(AutomationReviewItem.auto_decision == "reuse_existing")
            )
            pending_with_related_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(
                    AutomationReviewItem.status == "pending",
                    AutomationReviewItem.related_skill_id.is_not(None),
                    AutomationReviewItem.related_skill_id != "",
                )
            )
            approved_merge_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(
                    AutomationReviewItem.status == "approved",
                    AutomationReviewItem.auto_decision == "merge_existing",
                )
            )
            approved_reuse_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(
                    AutomationReviewItem.status == "approved",
                    AutomationReviewItem.auto_decision == "reuse_existing",
                )
            )
            approved_create_result = await session.execute(
                select(func.count(AutomationReviewItem.id)).where(
                    AutomationReviewItem.status == "approved",
                    AutomationReviewItem.auto_decision == "create_new",
                )
            )

        return {
            "review_create_new": int(create_new_result.scalar() or 0),
            "review_merge_existing": int(merge_existing_result.scalar() or 0),
            "review_reuse_existing": int(reuse_existing_result.scalar() or 0),
            "pending_with_related_skill": int(pending_with_related_result.scalar() or 0),
            "approved_merge_existing": int(approved_merge_result.scalar() or 0),
            "approved_reuse_existing": int(approved_reuse_result.scalar() or 0),
            "approved_create_new": int(approved_create_result.scalar() or 0),
        }

    async def get_overview_stats(self) -> Dict[str, Any]:
        async with self.async_session() as session:
            active_filter = SkillRegistry.deleted.is_(False)

            total_result = await session.execute(
                select(func.count(SkillRegistry.id)).where(active_filter)
            )
            category_result = await session.execute(
                select(SkillRegistry.category, func.count(SkillRegistry.id))
                .where(active_filter)
                .group_by(SkillRegistry.category)
            )
            project_result = await session.execute(
                select(SkillRegistry.project, func.count(SkillRegistry.id))
                .where(active_filter)
                .group_by(SkillRegistry.project)
            )
            sync_result = await session.execute(
                select(SkillRegistry.sync_status, func.count(SkillRegistry.id))
                .where(active_filter)
                .group_by(SkillRegistry.sync_status)
            )

            total_skills = int(total_result.scalar() or 0)
            category_rows = category_result.all()
            project_rows = project_result.all()
            sync_rows = sync_result.all()

        return {
            "total_skills": total_skills,
            "category_distribution": {
                row[0]: row[1] for row in category_rows if row[0]
            },
            "project_distribution": {
                (row[0] or "未关联项目"): row[1] for row in project_rows
            },
            "sync_status_distribution": {
                (row[0] or "UNKNOWN"): row[1] for row in sync_rows
            },
        }

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value)
        return datetime.utcnow()