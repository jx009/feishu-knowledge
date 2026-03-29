"""
向量库同步机制

实现两类同步能力：
- 飞书扫描同步：扫描知识空间中的文档，导入未知文档并刷新已变更文档的向量索引
- 注册表修复同步：遍历已登记知识，修复飞书、注册表、向量库三者的一致性
- 增量同步：基于最近一次同步游标，仅处理飞书最近发生变化的文档

使用方式：
    python -m vector.sync --full         # 扫描飞书并执行注册表驱动的全量修复
    python -m vector.sync --incremental  # 按最近一次同步游标执行增量同步
    python -m vector.sync --check        # 检查注册表与向量索引状态
"""

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from knowledge.card import (
    CATEGORIES,
    SkillCard,
    SYNC_STATUS_CREATED_FEISHU,
    SYNC_STATUS_DELETED,
    SYNC_STATUS_FAILED,
    SYNC_STATUS_INDEXED,
    SYNC_STATUS_PENDING_DELETE,
    SYNC_STATUS_PENDING_INDEX,
    SYNC_STATUS_PENDING_REINDEX,
    calculate_content_hash,
    extract_content_from_markdown,
    parse_tags,
)

logger = logging.getLogger(__name__)

REPAIRABLE_STATUSES = {
    SYNC_STATUS_CREATED_FEISHU,
    SYNC_STATUS_PENDING_INDEX,
    SYNC_STATUS_PENDING_REINDEX,
    SYNC_STATUS_FAILED,
}
DELETE_STATUSES = {
    SYNC_STATUS_PENDING_DELETE,
    SYNC_STATUS_DELETED,
}
SYNC_CURSOR_STATE_KEY = "feishu_incremental_last_sync_time"


class SyncManager:
    """
    向量库同步管理器

    核心职责：
    - 从飞书知识空间扫描文档并导入/刷新知识记录
    - 对注册表中已登记知识执行索引修复与删除补偿
    - 保存增量同步游标，支持下一轮仅处理最近发生变化的文档
    """

    def __init__(
        self,
        config,
        embedder,
        vector_store,
        feishu_doc_manager,
        registry_store,
        dashboard_logger=None,
    ):
        self.config = config or {}
        self.sync_config = self.config.get("sync", {}) if isinstance(self.config, dict) else {}
        self.embedder = embedder
        self.vector_store = vector_store
        self.feishu_doc_manager = feishu_doc_manager
        self.registry_store = registry_store
        self.dashboard_logger = dashboard_logger
        self.wiki_manager = getattr(feishu_doc_manager, "wiki_manager", None)
        self.cleanup_orphan_vectors = bool(self.sync_config.get("cleanup_orphan_vectors", True))
        self.max_records_per_run = int(self.sync_config.get("max_records_per_run", 0) or 0)

    async def full_sync(self) -> Dict[str, int]:
        """
        执行“飞书扫描 + 注册表修复”的全量同步。

        行为说明：
        - 先扫描飞书知识空间，将新增/变更文档导入注册表和向量库
        - 再遍历注册表记录，对待补偿和已删除知识执行一致性修复
        """
        if self.registry_store is None:
            raise RuntimeError("知识注册表未初始化，无法执行全量同步。")

        logger.info("=" * 40)
        logger.info("开始执行飞书扫描 + 注册表驱动全量同步...")
        logger.info("=" * 40)

        scan_summary = await self._sync_feishu_documents(since=None, mode="full")
        latest_update_time = scan_summary.get("latest_update_time", "")
        if latest_update_time:
            await self.registry_store.set_sync_state(SYNC_CURSOR_STATE_KEY, latest_update_time)

        records = await self.registry_store.list_records(limit=None, deleted=None)
        total = len(records)
        logger.info("注册表记录数: %s", total)

        summary = {
            "total": total,
            "feishu_scanned": int(scan_summary.get("scanned", 0)),
            "feishu_imported": int(scan_summary.get("imported", 0)),
            "feishu_updated": int(scan_summary.get("updated", 0)),
            "feishu_unchanged": int(scan_summary.get("unchanged", 0)),
            "feishu_skipped": int(scan_summary.get("skipped", 0)),
            "feishu_failed": int(scan_summary.get("failed", 0)),
            "indexed": 0,
            "unchanged": 0,
            "deleted": 0,
            "failed": 0,
            "orphan_vectors_removed": 0,
        }

        for index, record in enumerate(records, 1):
            skill_id = record.get("skill_id", "")
            title = record.get("title", "未命名")
            logger.info("[%s/%s] 校验知识: %s | %s", index, total, skill_id, title)

            if record.get("deleted") or record.get("sync_status") in DELETE_STATUSES:
                result = await self._reconcile_deleted(record)
            else:
                result = await self._reconcile_active(record)

            if result in summary:
                summary[result] += 1
            else:
                summary["failed"] += 1

        if self.cleanup_orphan_vectors:
            summary["orphan_vectors_removed"] = await self._cleanup_orphan_vectors(records)

        logger.info("=" * 40)
        logger.info(
            "全量同步完成: feishu_scanned=%s | feishu_imported=%s | feishu_updated=%s | "
            "indexed=%s | unchanged=%s | deleted=%s | failed=%s | orphan_vectors_removed=%s | total=%s",
            summary["feishu_scanned"],
            summary["feishu_imported"],
            summary["feishu_updated"],
            summary["indexed"],
            summary["unchanged"],
            summary["deleted"],
            summary["failed"] + summary["feishu_failed"],
            summary["orphan_vectors_removed"],
            summary["total"],
        )
        logger.info("=" * 40)
        return summary

    async def incremental_sync(self) -> Dict[str, Any]:
        """
        按最近一次同步游标执行增量同步。

        首次执行且没有游标时，会退化为一次完整飞书扫描导入，
        并把最新文档更新时间写入游标状态。
        """
        if self.registry_store is None:
            raise RuntimeError("知识注册表未初始化，无法执行增量同步。")

        cursor_before = await self.registry_store.get_sync_state(SYNC_CURSOR_STATE_KEY, "")
        if cursor_before:
            logger.info("开始执行增量同步 | cursor=%s", cursor_before)
        else:
            logger.info("未找到增量同步游标，执行首次全量扫描导入。")

        scan_summary = await self._sync_feishu_documents(
            since=cursor_before or None,
            mode="incremental",
        )
        latest_update_time = scan_summary.get("latest_update_time") or cursor_before
        if latest_update_time:
            await self.registry_store.set_sync_state(SYNC_CURSOR_STATE_KEY, latest_update_time)

        result = {
            "cursor_before": cursor_before,
            "cursor_after": latest_update_time,
            "scanned": int(scan_summary.get("scanned", 0)),
            "imported": int(scan_summary.get("imported", 0)),
            "updated": int(scan_summary.get("updated", 0)),
            "unchanged": int(scan_summary.get("unchanged", 0)),
            "skipped": int(scan_summary.get("skipped", 0)),
            "failed": int(scan_summary.get("failed", 0)),
        }
        logger.info(
            "增量同步完成: scanned=%s | imported=%s | updated=%s | unchanged=%s | skipped=%s | failed=%s | cursor_after=%s",
            result["scanned"],
            result["imported"],
            result["updated"],
            result["unchanged"],
            result["skipped"],
            result["failed"],
            result["cursor_after"] or "<empty>",
        )
        return result

    async def check_status(self) -> Dict[str, int]:
        """检查注册表与向量索引的一致性状态。"""
        if self.registry_store is None:
            raise RuntimeError("知识注册表未初始化，无法检查同步状态。")

        all_records = await self.registry_store.list_records(limit=None, deleted=None)
        active_registry_count = await self.registry_store.count_active()
        active_vector_count = self.vector_store.count(active_only=True)
        pending_records = await self.registry_store.list_pending_records(
            [
                SYNC_STATUS_CREATED_FEISHU,
                SYNC_STATUS_PENDING_INDEX,
                SYNC_STATUS_PENDING_REINDEX,
                SYNC_STATUS_PENDING_DELETE,
                SYNC_STATUS_FAILED,
            ]
        )

        pending_breakdown: Dict[str, int] = {}
        for record in pending_records:
            status = record.get("sync_status", "UNKNOWN")
            pending_breakdown[status] = pending_breakdown.get(status, 0) + 1

        orphan_vector_ids = self._find_orphan_vector_ids(all_records)
        deleted_count = len([record for record in all_records if record.get("deleted")])

        logger.info("注册表总记录数: %s", len(all_records))
        logger.info("注册表活跃知识数: %s", active_registry_count)
        logger.info("向量库活跃索引数: %s", active_vector_count)
        logger.info("注册表已删除知识数: %s", deleted_count)
        logger.info("待补偿/异常记录: %s", sum(pending_breakdown.values()))
        logger.info("注册表外孤儿向量点: %s", len(orphan_vector_ids))
        for status, count in sorted(pending_breakdown.items()):
            logger.info("  - %s: %s", status, count)

        if active_registry_count == active_vector_count and not pending_breakdown and not orphan_vector_ids:
            logger.info("✅ 注册表与向量索引状态一致")
        else:
            logger.warning("⚠️ 检测到状态漂移、孤儿向量点或待补偿记录，请执行 --full 修复")

        return {
            "registry_total": len(all_records),
            "registry_active": active_registry_count,
            "vector_active": active_vector_count,
            "registry_deleted": deleted_count,
            "pending_total": sum(pending_breakdown.values()),
            "vector_orphans": len(orphan_vector_ids),
        }

    async def _sync_feishu_documents(self, since: Optional[str], mode: str) -> Dict[str, Any]:
        if self.registry_store is None:
            raise RuntimeError("知识注册表未初始化，无法执行飞书扫描同步。")
        if self.wiki_manager is None:
            raise RuntimeError("未初始化 WikiManager，无法扫描飞书知识空间。")

        documents = await self.wiki_manager.list_documents_with_categories()
        if self.max_records_per_run > 0:
            documents = documents[: self.max_records_per_run]

        summary: Dict[str, Any] = {
            "scanned": len(documents),
            "imported": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
            "latest_update_time": since or "",
        }

        for index, document in enumerate(documents, 1):
            doc_token = document.get("obj_token", "")
            wiki_node_token = document.get("node_token", "")
            category = self._normalize_category(document.get("category", ""))
            logger.info(
                "[%s/%s] 扫描飞书文档: doc=%s | title=%s | mode=%s",
                index,
                len(documents),
                doc_token or "<empty>",
                document.get("title", "未命名"),
                mode,
            )

            if not doc_token:
                summary["skipped"] += 1
                continue

            try:
                snapshot = await self.feishu_doc_manager.get_document_snapshot(
                    doc_id=doc_token,
                    wiki_node_token=wiki_node_token,
                    category=category,
                )
                normalized_update_time = self._normalize_sync_time(
                    snapshot.get("update_time") or snapshot.get("create_time")
                )
                summary["latest_update_time"] = self._max_sync_time(
                    summary.get("latest_update_time", ""),
                    normalized_update_time,
                )

                if since and normalized_update_time and not self._is_newer_than(normalized_update_time, since):
                    summary["skipped"] += 1
                    continue

                action = await self._upsert_from_snapshot(snapshot)
                if action in summary:
                    summary[action] += 1
                else:
                    summary["failed"] += 1
            except Exception as e:
                logger.error("扫描飞书文档失败: doc=%s | 错误=%s", doc_token, e, exc_info=True)
                summary["failed"] += 1
                await self._log_sync(
                    skill_id=doc_token,
                    title=document.get("title", doc_token),
                    category=category,
                    sync_status=SYNC_STATUS_FAILED,
                    status="failed",
                    error=str(e),
                )

        return summary

    async def _upsert_from_snapshot(self, snapshot: Dict[str, Any]) -> str:
        raw_markdown = snapshot.get("content", "") or ""
        parsed = self._parse_skill_document(raw_markdown)
        existing = await self.registry_store.get_by_doc_token(snapshot.get("doc_id", ""))

        effective_skill_id = (
            (existing or {}).get("skill_id")
            or parsed.get("skill_id")
            or None
        )
        effective_category = self._normalize_category(
            parsed.get("category")
            or snapshot.get("category")
            or (existing or {}).get("category", "")
        )
        effective_project = parsed.get("project") or (existing or {}).get("project", "")
        effective_tags = parsed.get("tags") or (existing or {}).get("tags", [])
        effective_title = (
            parsed.get("title")
            or (existing or {}).get("title")
            or snapshot.get("title")
            or snapshot.get("doc_id")
            or "未命名"
        )
        effective_content = parsed.get("content") or extract_content_from_markdown(raw_markdown) or raw_markdown.strip()
        if not effective_content.strip():
            raise RuntimeError(f"飞书正文为空，无法构建索引: {snapshot.get('doc_id', '')}")

        normalized_created_at = self._normalize_sync_time(
            (existing or {}).get("created_at")
            or snapshot.get("create_time")
            or snapshot.get("update_time")
            or datetime.now().isoformat()
        )
        normalized_updated_at = self._normalize_sync_time(
            snapshot.get("update_time")
            or snapshot.get("create_time")
            or (existing or {}).get("updated_at")
            or datetime.now().isoformat()
        )
        current_hash = calculate_content_hash(effective_content)
        vector_record = await self.vector_store.get(effective_skill_id) if effective_skill_id else None
        vector_metadata = vector_record.get("metadata", {}) if vector_record else {}

        content_changed = bool(existing) and (existing.get("content_hash") or "") != current_hash
        metadata_changed = bool(existing) and any(
            [
                (existing or {}).get("title", "") != effective_title,
                (existing or {}).get("category", "") != effective_category,
                (existing or {}).get("project", "") != effective_project,
                (existing or {}).get("feishu_doc_url", "") != snapshot.get("doc_url", ""),
                (existing or {}).get("wiki_node_token", "") != snapshot.get("wiki_node_token", ""),
                list((existing or {}).get("tags", [])) != list(effective_tags),
                bool((existing or {}).get("deleted", False)),
            ]
        )
        needs_reindex = (
            existing is None
            or (existing or {}).get("sync_status") in REPAIRABLE_STATUSES
            or vector_record is None
            or vector_metadata.get("content_hash") != current_hash
            or vector_metadata.get("sync_status") != SYNC_STATUS_INDEXED
            or bool(vector_metadata.get("deleted", False))
            or content_changed
            or metadata_changed
        )

        if existing is None:
            action = "imported"
        elif needs_reindex:
            action = "updated"
        else:
            action = "unchanged"

        if action == "unchanged" and effective_skill_id:
            await self.registry_store.update_status(
                skill_id=effective_skill_id,
                sync_status=SYNC_STATUS_INDEXED,
                deleted=False,
                last_error=None,
                content_hash=current_hash,
            )
            await self._log_sync(
                skill_id=effective_skill_id,
                title=effective_title,
                category=effective_category,
                sync_status=SYNC_STATUS_INDEXED,
            )
            return action

        next_version = 1
        if existing:
            next_version = int(existing.get("version") or 1)
            if content_changed:
                next_version += 1

        card_kwargs = {
            "title": effective_title,
            "content": effective_content,
            "category": effective_category,
            "project": effective_project,
            "tags": effective_tags,
            "created_at": normalized_created_at,
            "updated_at": normalized_updated_at,
            "source": (existing or {}).get("source") or "feishu_scan",
            "feishu_doc_url": snapshot.get("doc_url") or None,
            "feishu_doc_token": snapshot.get("doc_id") or None,
            "wiki_node_token": snapshot.get("wiki_node_token") or None,
            "version": next_version,
            "sync_status": SYNC_STATUS_INDEXED,
            "deleted": False,
            "last_error": None,
        }
        if effective_skill_id:
            card_kwargs["skill_id"] = effective_skill_id

        card = SkillCard(**card_kwargs)

        embedding = self.embedder.encode(card.searchable_text)
        await self.vector_store.upsert(
            id=card.skill_id,
            embedding=embedding,
            metadata=card.to_metadata(),
            document=card.content,
        )
        await self.registry_store.upsert(card.to_registry_dict())
        await self._log_sync(
            skill_id=card.skill_id,
            title=card.title,
            category=card.category,
            sync_status=card.sync_status,
        )
        return action

    @staticmethod
    def _parse_skill_document(raw_markdown: str) -> Dict[str, Any]:
        if not raw_markdown:
            return {
                "title": "",
                "category": "",
                "project": "",
                "tags": [],
                "skill_id": "",
                "content": "",
            }

        normalized = raw_markdown.replace("\r\n", "\n")
        title_match = re.search(r"^#\s+(.+)$", normalized, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else ""

        category_match = re.search(r"分类[:：]\s*([^|\n]+)", normalized)
        project_match = re.search(r"项目[:：]\s*([^|\n]+)", normalized)
        tags_match = re.search(r"标签[:：]\s*([^|\n]+)", normalized)
        skill_id_match = re.search(r"技能ID[:：]\s*([^|\s\n]+)", normalized)

        category = category_match.group(1).strip() if category_match else ""
        project = project_match.group(1).strip() if project_match else ""
        tags = parse_tags(tags_match.group(1).strip() if tags_match else [])
        skill_id = skill_id_match.group(1).strip() if skill_id_match else ""

        if project and title.startswith(f"[{project}] "):
            title = title[len(project) + 3 :].strip()

        return {
            "title": title,
            "category": category,
            "project": project,
            "tags": tags,
            "skill_id": skill_id,
            "content": extract_content_from_markdown(normalized),
        }

    async def _reconcile_active(self, record: Dict[str, str]) -> str:
        skill_id = record.get("skill_id", "")
        try:
            feishu_doc_token = record.get("feishu_doc_token", "")
            if not skill_id:
                raise RuntimeError("注册表记录缺少 skill_id")
            if not feishu_doc_token:
                raise RuntimeError(f"知识缺少飞书文档 token，无法同步: {skill_id}")

            raw_markdown = await self.feishu_doc_manager.get_document_content(feishu_doc_token)
            content = extract_content_from_markdown(raw_markdown)
            if not content.strip():
                raise RuntimeError(f"飞书正文为空，无法构建索引: {skill_id}")

            current_hash = calculate_content_hash(content)
            stored_hash = record.get("content_hash") or ""
            vector_record = await self.vector_store.get(skill_id)
            vector_metadata = vector_record.get("metadata", {}) if vector_record else {}
            hash_changed = bool(stored_hash) and stored_hash != current_hash

            needs_reindex = (
                record.get("sync_status") in REPAIRABLE_STATUSES
                or not stored_hash
                or vector_record is None
                or vector_metadata.get("content_hash") != current_hash
                or vector_metadata.get("sync_status") != SYNC_STATUS_INDEXED
                or bool(vector_metadata.get("deleted", False))
            )

            safe_category = self._normalize_category(record.get("category", ""))
            now_iso = datetime.now().isoformat()
            next_version = int(record.get("version") or 1) + (1 if hash_changed else 0)
            updated_at = now_iso if needs_reindex or hash_changed else (record.get("updated_at") or now_iso)

            if not needs_reindex:
                await self.registry_store.update_status(
                    skill_id=skill_id,
                    sync_status=SYNC_STATUS_INDEXED,
                    deleted=False,
                    last_error=None,
                    version=next_version,
                    content_hash=current_hash,
                )
                await self._log_sync(
                    skill_id=skill_id,
                    title=record.get("title", skill_id),
                    category=safe_category,
                    sync_status=SYNC_STATUS_INDEXED,
                )
                return "unchanged"

            card = SkillCard(
                title=record.get("title") or skill_id,
                content=content,
                category=safe_category,
                project=record.get("project", ""),
                tags=record.get("tags", []),
                skill_id=skill_id,
                created_at=record.get("created_at") or now_iso,
                updated_at=updated_at,
                source=record.get("source", "ai_conversation"),
                feishu_doc_url=record.get("feishu_doc_url") or None,
                feishu_doc_token=feishu_doc_token or None,
                wiki_node_token=record.get("wiki_node_token") or None,
                version=next_version,
                sync_status=SYNC_STATUS_INDEXED,
                deleted=False,
                last_error=None,
            )

            embedding = self.embedder.encode(card.searchable_text)
            await self.vector_store.upsert(
                id=card.skill_id,
                embedding=embedding,
                metadata=card.to_metadata(),
                document=card.content,
            )
            await self.registry_store.upsert(card.to_registry_dict())

            await self._log_sync(
                skill_id=card.skill_id,
                title=card.title,
                category=card.category,
                sync_status=card.sync_status,
            )
            return "indexed"

        except Exception as e:
            logger.error("同步知识失败: %s | 错误: %s", skill_id or "<unknown>", e, exc_info=True)
            if skill_id:
                try:
                    await self.registry_store.update_status(
                        skill_id=skill_id,
                        sync_status=SYNC_STATUS_FAILED,
                        last_error=self._trim_error(e),
                    )
                except Exception:
                    pass
            await self._log_sync(
                skill_id=skill_id,
                title=record.get("title", ""),
                category=self._normalize_category(record.get("category", "")),
                sync_status=SYNC_STATUS_FAILED,
                status="failed",
                error=str(e),
            )
            return "failed"

    async def _reconcile_deleted(self, record: Dict[str, str]) -> str:
        skill_id = record.get("skill_id", "")
        try:
            if not skill_id:
                raise RuntimeError("注册表记录缺少 skill_id")

            await self.vector_store.delete(skill_id)
            await self.registry_store.update_status(
                skill_id=skill_id,
                sync_status=SYNC_STATUS_DELETED,
                deleted=True,
                last_error=None,
            )
            await self._log_sync(
                skill_id=skill_id,
                title=record.get("title", skill_id),
                category=self._normalize_category(record.get("category", "")),
                sync_status=SYNC_STATUS_DELETED,
            )
            return "deleted"

        except Exception as e:
            logger.error("清理已删除知识失败: %s | 错误: %s", skill_id or "<unknown>", e, exc_info=True)
            if skill_id:
                try:
                    await self.registry_store.update_status(
                        skill_id=skill_id,
                        sync_status=SYNC_STATUS_FAILED,
                        last_error=self._trim_error(e),
                    )
                except Exception:
                    pass
            await self._log_sync(
                skill_id=skill_id,
                title=record.get("title", ""),
                category=self._normalize_category(record.get("category", "")),
                sync_status=SYNC_STATUS_FAILED,
                status="failed",
                error=str(e),
            )
            return "failed"

    def _find_orphan_vector_ids(self, records: List[Dict[str, str]]) -> List[str]:
        registry_skill_ids = {
            str(record.get("skill_id")).strip()
            for record in records
            if str(record.get("skill_id", "")).strip()
        }
        vector_point_ids = self.vector_store.list_point_ids()
        return [point_id for point_id in vector_point_ids if point_id not in registry_skill_ids]

    async def _cleanup_orphan_vectors(self, records: List[Dict[str, str]]) -> int:
        orphan_vector_ids = self._find_orphan_vector_ids(records)
        if not orphan_vector_ids:
            return 0

        logger.warning("检测到注册表外孤儿向量点，开始清理: count=%s", len(orphan_vector_ids))
        await self.vector_store.delete_many(orphan_vector_ids)
        logger.info("孤儿向量点清理完成: count=%s", len(orphan_vector_ids))
        return len(orphan_vector_ids)

    async def _log_sync(
        self,
        skill_id: str,
        title: str,
        category: str,
        sync_status: str,
        status: str = "success",
        error: str | None = None,
    ):
        if not self.dashboard_logger:
            return

        try:
            await self.dashboard_logger.log_sync(
                skill_id=skill_id,
                title=title,
                category=category,
                sync_status=sync_status,
                status=status,
                error=error,
            )
        except Exception as log_error:
            logger.warning("记录同步日志失败: %s", log_error)

    @staticmethod
    def _trim_error(message: Exception | str, limit: int = 200) -> str:
        return str(message)[:limit]

    @staticmethod
    def _normalize_category(category: str) -> str:
        if category in CATEGORIES:
            return category
        return "最佳实践"

    @classmethod
    def _normalize_sync_time(cls, value: Any) -> str:
        parsed = cls._parse_sync_datetime(value)
        if parsed is None:
            return ""
        return parsed.isoformat()

    @classmethod
    def _parse_sync_datetime(cls, value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 1_000_000_000_000:
                timestamp = timestamp / 1000.0
            return datetime.fromtimestamp(timestamp)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.isdigit():
                return cls._parse_sync_datetime(int(text))
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                return None
        return None

    @classmethod
    def _is_newer_than(cls, current: str, cursor: str) -> bool:
        current_dt = cls._parse_sync_datetime(current)
        cursor_dt = cls._parse_sync_datetime(cursor)
        if current_dt is None:
            return False
        if cursor_dt is None:
            return True
        return current_dt > cursor_dt

    @classmethod
    def _max_sync_time(cls, first: str, second: str) -> str:
        if not first:
            return second
        if not second:
            return first
        first_dt = cls._parse_sync_datetime(first)
        second_dt = cls._parse_sync_datetime(second)
        if first_dt is None:
            return second
        if second_dt is None:
            return first
        return second if second_dt > first_dt else first


async def _main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="飞书知识库向量同步工具")
    parser.add_argument("--full", action="store_true", help="执行飞书扫描 + 注册表驱动的全量同步")
    parser.add_argument("--incremental", action="store_true", help="按最近一次同步游标执行增量同步")
    parser.add_argument("--check", action="store_true", help="检查注册表与向量索引状态")
    args = parser.parse_args()

    if not args.full and not args.incremental and not args.check:
        parser.print_help()
        return

    sys.path.insert(0, str(Path(__file__).parent.parent))

    from config import load_config
    from dashboard.logger import DashboardLogger
    from dashboard.registry import SkillRegistryStore
    from feishu.document import FeishuDocManager
    from vector.embedder import Embedder
    from vector.store import VectorStore

    config = load_config()
    dashboard_database_url = config.get("dashboard", {}).get("database_url")
    if not dashboard_database_url:
        raise RuntimeError("未配置 dashboard.database_url，无法执行同步。")

    embedder = Embedder(config["embedding"])
    vector_dimensions = int(config.get("embedding", {}).get("dimensions", 1536))
    vector_store = VectorStore(config["vector"], dimensions=vector_dimensions)
    feishu_doc_manager = FeishuDocManager(config)
    dashboard_logger = DashboardLogger(dashboard_database_url)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    sync_manager = SyncManager(
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )

    if args.full:
        await sync_manager.full_sync()
    elif args.incremental:
        await sync_manager.incremental_sync()
    elif args.check:
        await sync_manager.check_status()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    asyncio.run(_main())