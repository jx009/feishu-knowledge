"""
Qdrant 向量数据库封装

负责知识卡片的向量存储、检索和删除。
向量库仅作为派生索引使用，业务主键统一为 skill_id。
"""

import logging
import time
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

logger = logging.getLogger(__name__)


class VectorStore:
    """
    向量数据库封装类

    用法:
        store = VectorStore(config["vector"], dimensions=1536)
        await store.upsert(id, embedding, metadata, document)
        results = await store.search(query_vector, top_k=5)
    """

    def __init__(self, config: dict, dimensions: int = 1536):
        provider = config.get("provider", "qdrant_self_hosted")
        if provider != "qdrant_self_hosted":
            raise ValueError(f"不支持的向量数据库 provider: {provider}")

        qdrant_config = config.get("qdrant", {})
        retry_config = config.get("retry", {}) or {}
        self.collection_name = qdrant_config.get("collection_name", "knowledge_skills")
        self.dimensions = dimensions
        self.retry_max_attempts = int(retry_config.get("max_attempts", 3) or 3)
        self.retry_initial_delay_seconds = float(retry_config.get("initial_delay_seconds", 0.5) or 0.5)
        self.retry_backoff_multiplier = float(retry_config.get("backoff_multiplier", 2.0) or 2.0)
        self.client = QdrantClient(
            url=qdrant_config.get("url", "http://localhost:6333"),
            api_key=qdrant_config.get("api_key") or None,
        )

        self._ensure_collection()
        logger.info(
            "VectorStore 初始化完成 | collection=%s | dimensions=%s",
            self.collection_name,
            self.dimensions,
        )

    def _ensure_collection(self):
        collections = self._with_retry(
            lambda: self.client.get_collections().collections,
            action="获取 Qdrant collections",
        )
        if any(c.name == self.collection_name for c in collections):
            return

        self._with_retry(
            lambda: self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qmodels.VectorParams(
                    size=self.dimensions,
                    distance=qmodels.Distance.COSINE,
                ),
            ),
            action=f"创建 Qdrant collection {self.collection_name}",
        )
        logger.info("已创建 Qdrant collection: %s", self.collection_name)

    def _normalize_payload(
        self,
        skill_id: str,
        metadata: Optional[Dict[str, Any]],
        document: str,
    ) -> Dict[str, Any]:
        metadata = dict(metadata or {})
        payload: Dict[str, Any] = {
            "skill_id": metadata.get("skill_id") or metadata.get("id") or skill_id,
            "id": metadata.get("skill_id") or metadata.get("id") or skill_id,
            "title": metadata.get("title", ""),
            "category": metadata.get("category", ""),
            "project": metadata.get("project", ""),
            "tags": metadata.get("tags", []) or [],
            "created_at": metadata.get("created_at", ""),
            "updated_at": metadata.get("updated_at", ""),
            "source": metadata.get("source", "ai_conversation"),
            "feishu_doc_url": metadata.get("feishu_doc_url", ""),
            "feishu_doc_token": metadata.get("feishu_doc_token", ""),
            "wiki_node_token": metadata.get("wiki_node_token", ""),
            "content_hash": metadata.get("content_hash", ""),
            "version": metadata.get("version", 1),
            "sync_status": metadata.get("sync_status", "INDEXED"),
            "deleted": bool(metadata.get("deleted", False)),
            "document": document,
        }
        return payload

    def _build_filter(self, filter_conditions: Optional[Dict[str, Any]] = None, active_only: bool = False):
        conditions: List[qmodels.FieldCondition] = []
        filter_conditions = filter_conditions or {}

        if active_only:
            conditions.extend([
                qmodels.FieldCondition(
                    key="deleted",
                    match=qmodels.MatchValue(value=False),
                ),
                qmodels.FieldCondition(
                    key="sync_status",
                    match=qmodels.MatchValue(value="INDEXED"),
                ),
            ])

        for key, value in filter_conditions.items():
            if value in (None, ""):
                continue
            if isinstance(value, list):
                conditions.append(
                    qmodels.FieldCondition(
                        key=key,
                        match=qmodels.MatchAny(any=value),
                    )
                )
            else:
                conditions.append(
                    qmodels.FieldCondition(
                        key=key,
                        match=qmodels.MatchValue(value=value),
                    )
                )

        if not conditions:
            return None
        return qmodels.Filter(must=conditions)

    async def upsert(
        self,
        id: str,
        embedding: List[float],
        metadata: Dict[str, Any],
        document: str,
    ):
        payload = self._normalize_payload(skill_id=id, metadata=metadata, document=document)
        point = qmodels.PointStruct(id=id, vector=embedding, payload=payload)
        self._with_retry(
            lambda: self.client.upsert(collection_name=self.collection_name, points=[point]),
            action=f"写入向量点 {id}",
        )
        logger.info("向量点已写入: %s | 状态=%s", id, payload.get("sync_status"))

    async def search(
        self,
        query_vector: List[float],
        top_k: int = 5,
        filter_conditions: Optional[Dict[str, Any]] = None,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        query_filter = self._build_filter(filter_conditions, active_only=active_only)
        hits = self._with_retry(
            lambda: self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                limit=top_k,
                query_filter=query_filter,
                with_payload=True,
            ),
            action="搜索向量库",
        )

        results: List[Dict[str, Any]] = []
        for hit in hits:
            payload = dict(hit.payload or {})
            results.append({
                "id": payload.get("skill_id") or str(hit.id),
                "skill_id": payload.get("skill_id") or str(hit.id),
                "score": hit.score,
                "metadata": payload,
                "document": payload.get("document", ""),
            })
        return results

    async def get(self, skill_id: str) -> Optional[Dict[str, Any]]:
        points = self._with_retry(
            lambda: self.client.retrieve(
                collection_name=self.collection_name,
                ids=[skill_id],
                with_payload=True,
                with_vectors=False,
            ),
            action=f"读取向量点 {skill_id}",
        )
        if not points:
            return None

        payload = dict(points[0].payload or {})
        return {
            "id": payload.get("skill_id") or skill_id,
            "skill_id": payload.get("skill_id") or skill_id,
            "metadata": payload,
            "document": payload.get("document", ""),
        }

    async def delete(self, skill_id: str):
        self._with_retry(
            lambda: self.client.delete(
                collection_name=self.collection_name,
                points_selector=qmodels.PointIdsList(points=[skill_id]),
            ),
            action=f"删除向量点 {skill_id}",
        )
        logger.info("向量点已删除: %s", skill_id)

    async def delete_many(self, skill_ids: List[str]):
        point_ids = [skill_id for skill_id in skill_ids if skill_id]
        if not point_ids:
            return

        self._with_retry(
            lambda: self.client.delete(
                collection_name=self.collection_name,
                points_selector=qmodels.PointIdsList(points=point_ids),
            ),
            action=f"批量删除向量点 count={len(point_ids)}",
        )
        logger.info("向量点已批量删除: count=%s", len(point_ids))

    def list_point_ids(self, limit: Optional[int] = None, batch_size: int = 256) -> List[str]:
        point_ids: List[str] = []
        next_offset = None

        while True:
            current_limit = batch_size
            if limit is not None:
                remaining = limit - len(point_ids)
                if remaining <= 0:
                    break
                current_limit = min(batch_size, remaining)

            points, next_offset = self._with_retry(
                lambda: self.client.scroll(
                    collection_name=self.collection_name,
                    limit=current_limit,
                    offset=next_offset,
                    with_payload=False,
                    with_vectors=False,
                ),
                action="滚动读取向量点 ID",
            )
            if not points:
                break

            point_ids.extend(str(point.id) for point in points)

            if next_offset is None:
                break

        return point_ids

    async def list_all(
        self,
        category: Optional[str] = None,
        project: Optional[str] = None,
        tag: Optional[str] = None,
        sync_status: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        filter_conditions: Dict[str, Any] = {}
        if category:
            filter_conditions["category"] = category
        if project:
            filter_conditions["project"] = project
        if tag:
            filter_conditions["tags"] = [tag]
        if sync_status:
            filter_conditions["sync_status"] = sync_status
        if not include_deleted:
            filter_conditions["deleted"] = False

        points, _ = self._with_retry(
            lambda: self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._build_filter(filter_conditions, active_only=False),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            ),
            action="滚动读取向量明细",
        )

        results: List[Dict[str, Any]] = []
        for point in points:
            payload = dict(point.payload or {})
            results.append({
                "id": payload.get("skill_id") or str(point.id),
                "skill_id": payload.get("skill_id") or str(point.id),
                "metadata": payload,
                "document": payload.get("document", ""),
            })
        return results

    def count(
        self,
        filter_conditions: Optional[Dict[str, Any]] = None,
        active_only: bool = False,
    ) -> int:
        query_filter = self._build_filter(filter_conditions, active_only=active_only)
        result = self._with_retry(
            lambda: self.client.count(
                collection_name=self.collection_name,
                count_filter=query_filter,
                exact=True,
            ),
            action="统计向量点数量",
        )
        return int(getattr(result, "count", 0) or 0)

    def get_collection_info(self) -> Dict[str, Any]:
        info = self._with_retry(
            lambda: self.client.get_collection(self.collection_name),
            action=f"获取 collection 信息 {self.collection_name}",
        )
        return {
            "status": str(info.status),
            "vectors_count": getattr(info, "vectors_count", 0),
            "points_count": getattr(info, "points_count", 0),
        }

    def _with_retry(self, func, action: str):
        attempts = max(1, self.retry_max_attempts)
        delay = max(0.0, self.retry_initial_delay_seconds)
        multiplier = max(1.0, self.retry_backoff_multiplier)
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                return func()
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                logger.warning(
                    "%s 失败，准备重试 (%s/%s): %s",
                    action,
                    attempt,
                    attempts,
                    exc,
                )
                if delay > 0:
                    time.sleep(delay)
                delay = delay * multiplier if delay > 0 else 0.0

        assert last_error is not None
        raise last_error