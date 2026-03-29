import pytest
from fastapi import HTTPException

from dashboard.api import BatchReviewPayload, RejectReviewPayload, create_api_router
from dashboard.app import create_dashboard_app
from dashboard.logger import DashboardLogger
from dashboard.registry import SkillRegistryStore
from knowledge.extractor import ExtractedSkillCandidate, RuleBasedSkillExtractor
from tools.automation_review import register_automation_review
from tools.automation_workflow import register_automation_workflow
from tools.manage_skill import register_manage_skill
from tools.save_skill import register_save_skill
from tools.search_skill import register_search_skill
from vector.sync import SYNC_CURSOR_STATE_KEY, SyncManager


class _FakeMCPApp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


class _LifecycleFeishuDocManager:

    def __init__(self):
        self.documents = {}
        self.deleted_docs = []
        self.created_count = 0

    async def create_document(self, space_id, parent_node, title, content):
        self.created_count += 1
        doc_token = f"doc-{self.created_count}"
        wiki_node_token = f"wiki-{self.created_count}"
        doc_url = f"https://feishu.cn/docx/{doc_token}"
        self.documents[doc_token] = {
            "title": title,
            "content": content,
            "space_id": space_id,
            "parent_node": parent_node,
            "wiki_node_token": wiki_node_token,
            "doc_url": doc_url,
            "deleted": False,
        }
        return {
            "feishu_doc_token": doc_token,
            "wiki_node_token": wiki_node_token,
            "doc_url": doc_url,
        }

    async def get_document_content(self, doc_id):
        return self.documents[doc_id]["content"]

    async def update_document(self, doc_id, title, content):
        self.documents[doc_id]["title"] = title
        self.documents[doc_id]["content"] = content

    async def soft_delete_document(self, doc_id, title, skill_id, wiki_node_token=""):
        self.documents[doc_id]["deleted"] = True
        self.deleted_docs.append(doc_id)
        return {
            "status": "archived",
            "wiki_node_token": f"archived-{wiki_node_token}" if wiki_node_token else "archived-node",
        }


class _HealthyRegistryStore:
    async def count_active(self):
        return 1


class _BrokenRegistryStore:
    async def count_active(self):
        raise RuntimeError("registry unavailable")


class _HealthyVectorStoreProbe:
    def get_collection_info(self):
        return {"status": "green", "points_count": 3}


class _BrokenVectorStoreProbe:
    def get_collection_info(self):
        raise RuntimeError("qdrant unavailable")


def _create_toolset(config, embedder, vector_store, feishu_doc_manager, registry_store, dashboard_logger=None):
    app = _FakeMCPApp()
    register_save_skill(
        app=app,
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )
    register_search_skill(
        app=app,
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        dashboard_logger=dashboard_logger,
    )
    register_manage_skill(
        app=app,
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )
    return app.tools


def _create_automation_toolset(config, embedder, vector_store, feishu_doc_manager, registry_store, dashboard_logger=None):
    app = _FakeMCPApp()
    register_automation_workflow(
        app=app,
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )
    register_automation_review(
        app=app,
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )
    return app.tools


def _create_dashboard_logger(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'dashboard.db'}"
    return DashboardLogger(database_url)


@pytest.mark.asyncio
async def test_registry_overview_stats_use_active_registry_records(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    await registry_store.upsert(
        {
            "skill_id": "skill-active-1",
            "title": "活跃知识 1",
            "category": "最佳实践",
            "project": "project-a",
            "tags": ["python"],
            "sync_status": "INDEXED",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-active-2",
            "title": "活跃知识 2",
            "category": "最佳实践",
            "project": "",
            "tags": ["asyncio"],
            "sync_status": "PENDING_INDEX",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-deleted",
            "title": "已删除知识",
            "category": "避坑记录",
            "project": "project-b",
            "tags": ["legacy"],
            "sync_status": "FAILED",
            "deleted": True,
        }
    )

    assert await registry_store.count_active() == 2

    overview = await registry_store.get_overview_stats()

    assert overview["total_skills"] == 2
    assert overview["category_distribution"] == {"最佳实践": 2}
    assert overview["project_distribution"] == {"project-a": 1, "未关联项目": 1}
    assert overview["sync_status_distribution"] == {
        "INDEXED": 1,
        "PENDING_INDEX": 1,
    }


class _FakeVectorStore:
    def __init__(self, point_ids):
        self.point_ids = list(point_ids)
        self.deleted_batches = []
        self.upserted = []
        self.documents = {}

    def list_point_ids(self, limit=None, batch_size=256):
        if limit is None:
            return list(self.point_ids)
        return list(self.point_ids)[:limit]

    def count(self, filter_conditions=None, active_only=False):
        return len(self.point_ids)

    async def get(self, skill_id):
        payload = self.documents.get(skill_id)
        if payload is None:
            return None
        return {
            "id": skill_id,
            "skill_id": skill_id,
            "metadata": payload["metadata"],
            "document": payload["document"],
        }

    async def upsert(self, id, embedding, metadata, document):
        if id not in self.point_ids:
            self.point_ids.append(id)
        self.documents[id] = {
            "embedding": list(embedding),
            "metadata": dict(metadata),
            "document": document,
        }
        self.upserted.append(
            {
                "id": id,
                "embedding": list(embedding),
                "metadata": dict(metadata),
                "document": document,
            }
        )

    async def delete(self, skill_id):
        self.documents.pop(skill_id, None)
        self.point_ids = [point_id for point_id in self.point_ids if point_id != skill_id]

    async def delete_many(self, skill_ids):
        self.deleted_batches.append(list(skill_ids))
        deleted_ids = set(skill_ids)
        self.point_ids = [point_id for point_id in self.point_ids if point_id not in deleted_ids]
        for skill_id in deleted_ids:
            self.documents.pop(skill_id, None)

    def get_collection_info(self):
        return {"points_count": len(self.point_ids)}


class _LifecycleVectorStore(_FakeVectorStore):
    async def search(self, query_vector, filter_conditions=None, top_k=5, active_only=True):
        results = []
        for point_id in self.point_ids:
            payload = self.documents.get(point_id)
            if payload is None:
                continue

            metadata = dict(payload["metadata"])
            if active_only:
                if metadata.get("deleted"):
                    continue
                if metadata.get("sync_status") != "INDEXED":
                    continue

            if filter_conditions:
                mismatch = False
                for key, value in filter_conditions.items():
                    if metadata.get(key) != value:
                        mismatch = True
                        break
                if mismatch:
                    continue

            results.append(
                {
                    "id": point_id,
                    "skill_id": point_id,
                    "score": 0.99,
                    "metadata": metadata,
                    "document": payload["document"],
                }
            )

        return results[:top_k]


class _FakeEmbedder:
    def encode(self, text):
        return [float(len(text or "")), 1.0, 0.5]


class _FakeWikiManager:
    def __init__(self, documents):
        self.documents = list(documents)

    async def list_documents_with_categories(self):
        return list(self.documents)


class _FakeFeishuDocManager:
    def __init__(self, snapshots):
        self.snapshots = dict(snapshots)
        self.wiki_manager = _FakeWikiManager(
            [
                {
                    "obj_token": doc_id,
                    "node_token": snapshot.get("wiki_node_token", ""),
                    "title": snapshot.get("title", doc_id),
                    "category": snapshot.get("category", "最佳实践"),
                }
                for doc_id, snapshot in snapshots.items()
            ]
        )

    async def get_document_snapshot(self, doc_id, wiki_node_token="", category=""):
        snapshot = dict(self.snapshots[doc_id])
        snapshot.setdefault("doc_id", doc_id)
        snapshot.setdefault("wiki_node_token", wiki_node_token)
        snapshot.setdefault("category", category)
        snapshot.setdefault("doc_url", f"https://feishu.cn/docx/{doc_id}")
        return snapshot

    async def get_document_content(self, doc_id):
        return self.snapshots[doc_id].get("content", "")


@pytest.mark.asyncio
async def test_sync_manager_cleans_up_orphan_vector_points(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    await registry_store.upsert(
        {
            "skill_id": "skill-1",
            "title": "知识 1",
            "category": "最佳实践",
            "project": "project-a",
            "tags": ["python"],
            "sync_status": "INDEXED",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-2",
            "title": "知识 2",
            "category": "最佳实践",
            "project": "project-b",
            "tags": ["asyncio"],
            "sync_status": "DELETED",
            "deleted": True,
        }
    )

    vector_store = _FakeVectorStore(["skill-1", "skill-2", "legacy-node-token", "orphan-skill"])
    sync_manager = SyncManager(
        config={},
        embedder=None,
        vector_store=vector_store,
        feishu_doc_manager=None,
        registry_store=registry_store,
        dashboard_logger=None,
    )

    status = await sync_manager.check_status()

    assert status["vector_orphans"] == 2
    assert sync_manager._find_orphan_vector_ids(
        await registry_store.list_records(limit=None, deleted=None)
    ) == ["legacy-node-token", "orphan-skill"]

    removed_count = await sync_manager._cleanup_orphan_vectors(
        await registry_store.list_records(limit=None, deleted=None)
    )

    assert removed_count == 2
    assert vector_store.deleted_batches == [["legacy-node-token", "orphan-skill"]]
    assert vector_store.list_point_ids() == ["skill-1", "skill-2"]


@pytest.mark.asyncio
async def test_hot_skills_api_aggregates_search_hits_and_prefers_registry_metadata(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    await registry_store.upsert(
        {
            "skill_id": "skill-1",
            "title": "注册表标题 1",
            "category": "最佳实践",
            "project": "project-a",
            "feishu_doc_url": "https://example.com/doc-1",
            "sync_status": "INDEXED",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-2",
            "title": "注册表标题 2",
            "category": "工具使用",
            "project": "project-b",
            "feishu_doc_url": "https://example.com/doc-2",
            "sync_status": "PENDING_INDEX",
            "deleted": False,
        }
    )

    await dashboard_logger.log_search(query="Python 异步", results_count=2, top_score=0.95)
    await dashboard_logger.log_search_hits(
        query="Python 异步",
        results=[
            {
                "skill_id": "skill-1",
                "score": 0.95,
                "metadata": {
                    "title": "旧标题 1",
                    "category": "旧分类",
                    "project": "legacy-project",
                    "feishu_doc_url": "https://legacy/doc-1",
                },
            },
            {
                "skill_id": "skill-2",
                "score": 0.87,
                "metadata": {
                    "title": "旧标题 2",
                    "category": "旧分类",
                    "project": "legacy-project",
                    "feishu_doc_url": "https://legacy/doc-2",
                },
            },
        ],
    )
    await dashboard_logger.log_search(query="异步 最佳实践", results_count=1, top_score=0.88)
    await dashboard_logger.log_search_hits(
        query="异步 最佳实践",
        results=[
            {
                "skill_id": "skill-1",
                "score": 0.88,
                "metadata": {
                    "title": "旧标题 1",
                    "category": "旧分类",
                    "project": "legacy-project",
                    "feishu_doc_url": "https://legacy/doc-1",
                },
            },
        ],
    )

    router = create_api_router(
        dashboard_logger=dashboard_logger,
        vector_store=_FakeVectorStore([]),
        registry_store=registry_store,
    )

    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/stats/hot-skills")
    response = await endpoint(top_k=10, days=30)

    assert response["days"] == 30
    assert len(response["hot_skills"]) == 2
    assert response["hot_skills"][0]["skill_id"] == "skill-1"
    assert response["hot_skills"][0]["title"] == "注册表标题 1"
    assert response["hot_skills"][0]["category"] == "最佳实践"
    assert response["hot_skills"][0]["project"] == "project-a"
    assert response["hot_skills"][0]["feishu_doc_url"] == "https://example.com/doc-1"
    assert response["hot_skills"][0]["hit_count"] == 2
    assert response["hot_skills"][0]["best_rank"] == 1
    assert response["hot_skills"][0]["sync_status"] == "INDEXED"

    assert response["hot_skills"][1]["skill_id"] == "skill-2"
    assert response["hot_skills"][1]["title"] == "注册表标题 2"
    assert response["hot_skills"][1]["hit_count"] == 1


@pytest.mark.asyncio
async def test_dashboard_overview_uses_successful_search_metrics_and_full_exception_counts(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    await registry_store.upsert(
        {
            "skill_id": "skill-indexed",
            "title": "已索引知识",
            "category": "最佳实践",
            "project": "project-a",
            "sync_status": "INDEXED",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-pending-index",
            "title": "待索引知识",
            "category": "最佳实践",
            "project": "project-a",
            "sync_status": "PENDING_INDEX",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-pending-reindex",
            "title": "待重建知识",
            "category": "工具使用",
            "project": "project-b",
            "sync_status": "PENDING_REINDEX",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-pending-delete",
            "title": "待删除知识",
            "category": "工具使用",
            "project": "project-b",
            "sync_status": "PENDING_DELETE",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-failed",
            "title": "失败知识",
            "category": "避坑记录",
            "project": "project-c",
            "sync_status": "FAILED",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-deleted",
            "title": "已删除知识",
            "category": "避坑记录",
            "project": "project-c",
            "sync_status": "DELETED",
            "deleted": True,
        }
    )

    await dashboard_logger.log_search(query="成功查询", results_count=1, top_score=0.95)
    await dashboard_logger.log_search(query="成功查询", results_count=1, top_score=0.93)
    await dashboard_logger.log_search(query="失败查询", results_count=0, top_score=0.0, status="failed", error="timeout")

    router = create_api_router(
        dashboard_logger=dashboard_logger,
        vector_store=_FakeVectorStore([]),
        registry_store=registry_store,
    )

    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/stats/overview")
    response = await endpoint()

    assert response["total_skills"] == 5
    assert response["total_searches"] == 2
    assert response["total_search_attempts"] == 3
    assert response["failed_searches"] == 1
    assert response["sync_status_distribution"] == {
        "INDEXED": 1,
        "PENDING_INDEX": 1,
        "PENDING_REINDEX": 1,
        "PENDING_DELETE": 1,
        "FAILED": 1,
    }
    assert response["exception_counts"] == {
        "pending_index": 1,
        "pending_reindex": 1,
        "pending_delete": 1,
        "failed": 1,
        "deleted": 1,
        "active_exceptions": 4,
        "total_exceptions": 5,
    }


@pytest.mark.asyncio
async def test_dashboard_hot_queries_only_counts_successful_searches(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()

    await dashboard_logger.log_search(query="Python", results_count=2, top_score=0.98)
    await dashboard_logger.log_search(query="Python", results_count=1, top_score=0.88)
    await dashboard_logger.log_search(query="Python", results_count=0, top_score=0.0, status="failed", error="timeout")
    await dashboard_logger.log_search(query="Rust", results_count=1, top_score=0.8)

    router = create_api_router(
        dashboard_logger=dashboard_logger,
        vector_store=_FakeVectorStore([]),
        registry_store=None,
    )

    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/stats/hot-queries")
    response = await endpoint(top_k=10)

    assert response["hot_queries"] == [
        {"query": "Python", "count": 2},
        {"query": "Rust", "count": 1},
    ]


@pytest.mark.asyncio
async def test_registry_exceptions_api_includes_reindex_and_pending_delete(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    await registry_store.upsert(
        {
            "skill_id": "skill-pending-index",
            "title": "待索引知识",
            "category": "最佳实践",
            "project": "project-a",
            "sync_status": "PENDING_INDEX",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-pending-reindex",
            "title": "待重建知识",
            "category": "工具使用",
            "project": "project-b",
            "sync_status": "PENDING_REINDEX",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-pending-delete",
            "title": "待删除知识",
            "category": "工具使用",
            "project": "project-b",
            "sync_status": "PENDING_DELETE",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-failed",
            "title": "失败知识",
            "category": "避坑记录",
            "project": "project-c",
            "sync_status": "FAILED",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-deleted",
            "title": "已删除知识",
            "category": "避坑记录",
            "project": "project-c",
            "sync_status": "DELETED",
            "deleted": True,
        }
    )

    router = create_api_router(
        dashboard_logger=dashboard_logger,
        vector_store=_FakeVectorStore([]),
        registry_store=registry_store,
    )

    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/registry/exceptions")
    response = await endpoint(include_deleted=True, page=1, page_size=20)

    assert response["statuses"] == ["PENDING_INDEX", "PENDING_REINDEX", "PENDING_DELETE", "FAILED"]
    assert response["total"] == 5
    assert {record["skill_id"] for record in response["records"]} == {
        "skill-pending-index",
        "skill-pending-reindex",
        "skill-pending-delete",
        "skill-failed",
        "skill-deleted",
    }


@pytest.mark.asyncio
async def test_registry_records_api_supports_empty_project_filter_and_returns_filter_options(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    await registry_store.upsert(
        {
            "skill_id": "skill-empty-project",
            "title": "未关联项目知识",
            "category": "最佳实践",
            "project": "",
            "sync_status": "INDEXED",
            "deleted": False,
        }
    )
    await registry_store.upsert(
        {
            "skill_id": "skill-project-a",
            "title": "ProjectA 知识",
            "category": "工具使用",
            "project": "project-a",
            "sync_status": "FAILED",
            "deleted": False,
        }
    )

    router = create_api_router(
        dashboard_logger=dashboard_logger,
        vector_store=_FakeVectorStore([]),
        registry_store=registry_store,
    )

    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/registry/records")
    response = await endpoint(project_is_empty=True, page=1, page_size=20)

    assert response["total"] == 1
    assert [record["skill_id"] for record in response["records"]] == ["skill-empty-project"]
    assert response["filter_options"]["categories"] == ["工具使用", "最佳实践"]
    assert response["filter_options"]["projects"] == ["project-a", "未关联项目"]
    assert response["filter_options"]["sync_statuses"] == ["FAILED", "INDEXED"]


@pytest.mark.asyncio
async def test_dashboard_app_health_and_ready_endpoints_report_dependency_status(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()

    dashboard_app = create_dashboard_app(
        dashboard_logger=dashboard_logger,
        vector_store=_HealthyVectorStoreProbe(),
        registry_store=_HealthyRegistryStore(),
    )

    health_endpoint = next(route.endpoint for route in dashboard_app.routes if route.path == "/health")
    ready_endpoint = next(route.endpoint for route in dashboard_app.routes if route.path == "/ready")

    health_response = await health_endpoint()
    ready_response = await ready_endpoint()

    assert health_response == {
        "status": "healthy",
        "service": "feishu-knowledge-mcp-dashboard",
    }
    assert ready_response["status"] == "ready"
    assert ready_response["dependencies"]["dashboard_database"]["status"] == "healthy"
    assert ready_response["dependencies"]["registry_store"]["status"] == "healthy"
    assert ready_response["dependencies"]["vector_store"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_dashboard_app_runtime_endpoint_returns_remote_mcp_access_info(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()

    dashboard_app = create_dashboard_app(
        dashboard_logger=dashboard_logger,
        vector_store=_HealthyVectorStoreProbe(),
        registry_store=_HealthyRegistryStore(),
        service_info_provider=lambda: {
            "service": "feishu-knowledge-mcp",
            "dashboard": {
                "enabled": True,
                "url": "http://127.0.0.1:8080",
                "runtime_url": "http://127.0.0.1:8080/runtime",
            },
            "mcp": {
                "transport": "sse",
                "enabled": True,
                "host": "0.0.0.0",
                "port": 8001,
                "sse_url": "https://mcp.example.com/mcp/sse",
                "message_url": "https://mcp.example.com/mcp/messages",
            },
        },
    )

    runtime_endpoint = next(route.endpoint for route in dashboard_app.routes if route.path == "/runtime")
    runtime_response = await runtime_endpoint()

    assert runtime_response["service"] == "feishu-knowledge-mcp"
    assert runtime_response["dashboard"]["url"] == "http://127.0.0.1:8080"
    assert runtime_response["mcp"]["transport"] == "sse"
    assert runtime_response["mcp"]["enabled"] is True
    assert runtime_response["mcp"]["sse_url"] == "https://mcp.example.com/mcp/sse"
    assert runtime_response["mcp"]["message_url"] == "https://mcp.example.com/mcp/messages"


@pytest.mark.asyncio
async def test_dashboard_app_ready_endpoint_returns_503_when_dependencies_fail(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()

    dashboard_app = create_dashboard_app(
        dashboard_logger=dashboard_logger,
        vector_store=_BrokenVectorStoreProbe(),
        registry_store=_BrokenRegistryStore(),
    )

    ready_endpoint = next(route.endpoint for route in dashboard_app.routes if route.path == "/ready")

    with pytest.raises(HTTPException) as exc_info:
        await ready_endpoint()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["status"] == "unready"
    assert exc_info.value.detail["dependencies"]["registry_store"]["status"] == "unhealthy"
    assert exc_info.value.detail["dependencies"]["vector_store"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_registry_store_can_persist_sync_cursor_state(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    assert await registry_store.get_sync_state(SYNC_CURSOR_STATE_KEY, "") == ""

    await registry_store.set_sync_state(SYNC_CURSOR_STATE_KEY, "2026-03-17T10:00:00")

    assert await registry_store.get_sync_state(SYNC_CURSOR_STATE_KEY, "") == "2026-03-17T10:00:00"


@pytest.mark.asyncio
async def test_incremental_sync_imports_new_feishu_document_and_updates_cursor(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)
    vector_store = _FakeVectorStore([])
    feishu_doc_manager = _FakeFeishuDocManager(
        {
            "doc-new": {
                "title": "[ProjectX] 新知识",
                "content": "# [ProjectX] 新知识\n\n> 📂 分类：最佳实践 | 🏷️ 项目：ProjectX | 🔖 标签：python,asyncio | 🆔 技能ID：skill_external_001 | 🕒 更新时间：2026-03-17T10:00:00\n\n---\n\n这里是新知识正文",
                "update_time": "2026-03-17T10:00:00",
                "create_time": "2026-03-17T09:00:00",
                "wiki_node_token": "wiki-node-1",
                "category": "最佳实践",
            }
        }
    )

    sync_manager = SyncManager(
        config={},
        embedder=_FakeEmbedder(),
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=None,
    )

    result = await sync_manager.incremental_sync()

    assert result["scanned"] == 1
    assert result["imported"] == 1
    assert result["updated"] == 0
    assert result["cursor_after"] == "2026-03-17T10:00:00"

    record = await registry_store.get("skill_external_001")
    assert record is not None
    assert record["title"] == "新知识"
    assert record["project"] == "ProjectX"
    assert record["tags"] == ["python", "asyncio"]
    assert record["feishu_doc_token"] == "doc-new"
    assert record["wiki_node_token"] == "wiki-node-1"
    assert record["sync_status"] == "INDEXED"

    assert vector_store.upserted[0]["id"] == "skill_external_001"
    assert vector_store.upserted[0]["document"] == "这里是新知识正文"


@pytest.mark.asyncio
async def test_incremental_sync_only_processes_documents_newer_than_cursor(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)
    vector_store = _FakeVectorStore([])
    feishu_doc_manager = _FakeFeishuDocManager(
        {
            "doc-old": {
                "title": "旧知识",
                "content": "# 旧知识\n\n> 📂 分类：最佳实践 | 🆔 技能ID：skill_old_001 | 🕒 更新时间：2026-03-17T09:30:00\n\n---\n\n旧正文",
                "update_time": "2026-03-17T09:30:00",
                "create_time": "2026-03-17T09:00:00",
                "wiki_node_token": "wiki-node-old",
                "category": "最佳实践",
            },
            "doc-new": {
                "title": "新知识",
                "content": "# 新知识\n\n> 📂 分类：工具使用 | 🆔 技能ID：skill_new_001 | 🕒 更新时间：2026-03-17T11:00:00\n\n---\n\n新正文",
                "update_time": "2026-03-17T11:00:00",
                "create_time": "2026-03-17T10:30:00",
                "wiki_node_token": "wiki-node-new",
                "category": "工具使用",
            },
        }
    )

    await registry_store.set_sync_state(SYNC_CURSOR_STATE_KEY, "2026-03-17T10:00:00")

    sync_manager = SyncManager(
        config={},
        embedder=_FakeEmbedder(),
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=None,
    )

    result = await sync_manager.incremental_sync()

    assert result["scanned"] == 2
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert result["cursor_before"] == "2026-03-17T10:00:00"
    assert result["cursor_after"] == "2026-03-17T11:00:00"
    assert await registry_store.get("skill_old_001") is None
    assert await registry_store.get("skill_new_001") is not None


@pytest.mark.asyncio
async def test_skill_tool_lifecycle_save_search_update_delete(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)
    vector_store = _LifecycleVectorStore([])
    feishu_doc_manager = _LifecycleFeishuDocManager()
    embedder = _FakeEmbedder()
    config = {
        "feishu": {
            "wiki_space_id": "space-001",
            "category_nodes": {"最佳实践": "node-best-practice"},
        },
        "knowledge": {"search_top_k": 5},
    }

    tools = _create_toolset(
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )

    save_result = await tools["save_skill"](
        title="异步重试实践",
        content="这里是第一版正文",
        category="最佳实践",
        project="ProjectX",
        tags="python,asyncio",
    )
    save_text = save_result[0].text
    assert "✅ 知识已成功沉淀" in save_text

    records = await registry_store.list_records(limit=None, deleted=False)
    assert len(records) == 1
    saved_record = records[0]
    skill_id = saved_record["skill_id"]
    assert saved_record["title"] == "异步重试实践"
    assert saved_record["project"] == "ProjectX"
    assert saved_record["tags"] == ["python", "asyncio"]
    assert saved_record["sync_status"] == "INDEXED"
    assert skill_id in vector_store.documents

    search_result = await tools["search_skill"](
        query="异步重试",
        project="ProjectX",
    )
    search_text = search_result[0].text
    assert "找到 **1** 条与「异步重试」相关的知识" in search_text
    assert skill_id in search_text
    assert "异步重试实践" in search_text

    update_result = await tools["update_skill"](
        skill_id=skill_id,
        title="异步重试实践（更新版）",
        content="这里是第二版正文",
        category="最佳实践",
        tags="python,asyncio,retry",
    )
    update_text = update_result[0].text
    assert "✅ 知识卡片已更新" in update_text

    updated_record = await registry_store.get(skill_id)
    assert updated_record is not None
    assert updated_record["title"] == "异步重试实践（更新版）"
    assert updated_record["tags"] == ["python", "asyncio", "retry"]
    assert updated_record["sync_status"] == "INDEXED"
    assert updated_record["version"] == 2
    assert vector_store.documents[skill_id]["document"] == "这里是第二版正文"

    post_update_search = await tools["search_skill"](
        query="更新版正文",
        project="ProjectX",
    )
    assert "异步重试实践（更新版）" in post_update_search[0].text

    delete_result = await tools["delete_skill"](skill_id=skill_id)
    delete_text = delete_result[0].text
    assert "✅ 知识卡片已删除" in delete_text
    assert skill_id in delete_text

    deleted_record = await registry_store.get(skill_id)
    assert deleted_record is not None
    assert deleted_record["deleted"] is True
    assert deleted_record["sync_status"] == "DELETED"
    assert skill_id not in vector_store.documents
    assert len(feishu_doc_manager.deleted_docs) == 1

    final_search = await tools["search_skill"](
        query="异步重试",
        project="ProjectX",
    )
    assert "未找到与「异步重试」相关的知识" in final_search[0].text


@pytest.mark.asyncio
async def test_automation_workflow_auto_retrieves_saves_and_queues_review_items(tmp_path, monkeypatch):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)
    vector_store = _LifecycleVectorStore(["seed-skill"])
    vector_store.documents["seed-skill"] = {
        "metadata": {
            "title": "历史自动化闭环经验",
            "category": "最佳实践",
            "project": "ProjectX",
            "feishu_doc_url": "https://example.com/seed-skill",
            "sync_status": "INDEXED",
            "deleted": False,
        },
        "document": "自动检索应该在任务开始时默认参与，并返回最相关的历史经验。",
    }
    feishu_doc_manager = _LifecycleFeishuDocManager()
    embedder = _FakeEmbedder()
    config = {
        "feishu": {
            "wiki_space_id": "space-001",
            "category_nodes": {
                "最佳实践": "node-best-practice",
                "工具使用": "node-tooling",
                "避坑记录": "node-pitfall",
            },
        },
        "knowledge": {"search_top_k": 5},
        "automation": {
            "enabled": True,
            "auto_retrieve_enabled": True,
            "auto_extract_enabled": True,
            "auto_save_enabled": True,
            "retrieval_top_k": 5,
            "high_confidence_score": 10,
            "medium_confidence_score": 6,
            "max_auto_save_items": 3,
            "max_review_queue_items": 5,
        },
        "extraction": {
            "enabled": True,
            "max_candidates": 5,
            "min_score": 3,
            "min_segment_length": 40,
            "max_excerpt_length": 240,
            "include_full_text_fallback": True,
        },
    }

    def fake_extract(self, text, project="", top_k=None):
        return [
            ExtractedSkillCandidate(
                title="自动保存的最佳实践",
                category="最佳实践",
                project=project,
                tags=["automation", "closure"],
                score=11,
                confidence="high",
                reasons=["命中最佳实践信号"],
                excerpt="自动沉淀高置信度候选时应直接入库。",
                draft_content="## 适用场景\n\n自动沉淀闭环\n\n## 推荐做法\n\n高置信度知识直接自动保存。",
            ),
            ExtractedSkillCandidate(
                title="待审核的工具使用说明",
                category="工具使用",
                project=project,
                tags=["dashboard", "review"],
                score=7,
                confidence="medium",
                reasons=["命中工具使用信号"],
                excerpt="中等置信度候选应进入审核队列。",
                draft_content="## 使用场景\n\nDashboard 审核队列\n\n## 操作步骤\n\n人工确认后再入库。",
            ),
            ExtractedSkillCandidate(
                title="低价值候选",
                category="避坑记录",
                project=project,
                tags=["noise"],
                score=5,
                confidence="low",
                reasons=["信息不完整"],
                excerpt="低价值候选应被丢弃。",
                draft_content="信息不足，暂不沉淀。",
            ),
        ]

    monkeypatch.setattr(RuleBasedSkillExtractor, "extract", fake_extract)

    tools = _create_automation_toolset(
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )

    start_result = await tools["start_auto_session"](
        user_goal="补齐自动检索和自动沉淀主闭环",
        project="ProjectX",
    )
    start_text = start_result[0].text
    assert "🧠 自动会话已启动" in start_text
    assert "🔎 **召回数量**：1" in start_text
    assert "历史自动化闭环经验" in start_text

    sessions = await registry_store.list_automation_sessions(limit=10, offset=0)
    assert len(sessions) == 1
    session_id = sessions[0]["session_id"]

    finish_result = await tools["finish_auto_session"](
        session_id=session_id,
        user_goal="补齐自动检索和自动沉淀主闭环",
        conversation_summary="已经完成自动检索、自动提取、自动保存与审核队列串联。",
        project="ProjectX",
        tool_summary="调用自动会话开始与结束工具。",
        code_change_summary="补齐了 Dashboard 审核入口与自动化主流程。",
        decisions="高置信度知识自动保存，中等置信度进入审核队列。",
        errors_and_fixes="无",
        final_conclusion="第二步主闭环已打通。",
    )
    finish_text = finish_result[0].text
    assert "🤖 自动会话已完成" in finish_text
    assert "- 自动提取候选：3 条" in finish_text
    assert "- 自动保存成功：1 条" in finish_text
    assert "- 进入审核队列：1 条" in finish_text
    assert "- 已丢弃低价值候选：1 条" in finish_text

    saved_records = await registry_store.list_records(limit=None, deleted=False)
    assert len(saved_records) == 1
    saved_record = saved_records[0]
    assert saved_record["title"] == "自动保存的最佳实践"
    assert saved_record["project"] == "ProjectX"
    assert saved_record["tags"] == ["automation", "closure"]
    assert saved_record["sync_status"] == "INDEXED"

    pending_reviews = await registry_store.list_review_items(status="pending", limit=20)
    assert len(pending_reviews) == 1
    pending_review = pending_reviews[0]
    assert pending_review["title"] == "待审核的工具使用说明"
    assert pending_review["confidence"] == "medium"
    assert pending_review["project"] == "ProjectX"

    session = await registry_store.get_automation_session(session_id)
    assert session is not None
    assert session["retrieval_status"] == "success"
    assert session["extraction_status"] == "success"
    assert session["save_status"] == "success"
    assert session["auto_retrieval_count"] == 1
    assert session["extracted_candidates"] == 3
    assert session["auto_saved_count"] == 1
    assert session["review_queued_count"] == 1
    assert session["discarded_count"] == 1
    assert len(session["saved_skill_ids"]) == 1
    assert session["saved_skill_ids"][0] == saved_record["skill_id"]


@pytest.mark.asyncio
async def test_automation_review_api_supports_overview_listing_and_review_actions(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)
    vector_store = _LifecycleVectorStore([])
    feishu_doc_manager = _LifecycleFeishuDocManager()
    embedder = _FakeEmbedder()
    config = {
        "feishu": {
            "wiki_space_id": "space-001",
            "category_nodes": {
                "最佳实践": "node-best-practice",
                "工具使用": "node-tooling",
            },
        },
        "automation": {
            "enabled": True,
            "auto_retrieve_enabled": True,
            "auto_extract_enabled": True,
            "auto_save_enabled": True,
            "high_confidence_score": 10,
            "medium_confidence_score": 6,
        },
    }

    await registry_store.upsert_automation_session(
        {
            "session_id": "auto-session-001",
            "project": "ProjectX",
            "user_goal": "验证审核闭环",
            "normalized_query": "验证审核闭环",
            "raw_query": "验证审核闭环",
            "keywords": ["审核", "闭环"],
            "retrieval_status": "success",
            "extraction_status": "success",
            "save_status": "success",
            "auto_retrieval_count": 2,
            "extracted_candidates": 4,
            "auto_saved_count": 1,
            "review_queued_count": 3,
            "discarded_count": 0,
            "saved_skill_ids": ["skill-existing"],
        }
    )

    await registry_store.upsert_review_item(
        {
            "review_id": "review-approve",
            "session_id": "auto-session-001",
            "title": "待通过候选",
            "category": "最佳实践",
            "project": "ProjectX",
            "tags": ["automation"],
            "excerpt": "审批通过后应入库。",
            "draft_content": "## 推荐做法\n\n审批通过后写入飞书与向量库。",
            "reasons": ["高价值候选"],
            "source_text": "审批通过后写入飞书与向量库。",
            "score": 8,
            "confidence": "medium",
            "status": "pending",
            "auto_decision": "review",
        }
    )
    await registry_store.upsert_review_item(
        {
            "review_id": "review-reject",
            "session_id": "auto-session-001",
            "title": "待驳回候选",
            "category": "工具使用",
            "project": "ProjectX",
            "tags": ["duplicate"],
            "excerpt": "这条候选信息重复。",
            "draft_content": "重复内容，无需再次沉淀。",
            "reasons": ["内容重复"],
            "source_text": "重复内容，无需再次沉淀。",
            "score": 6,
            "confidence": "medium",
            "status": "pending",
            "auto_decision": "review",
        }
    )
    await registry_store.upsert_review_item(
        {
            "review_id": "review-batch-1",
            "session_id": "auto-session-001",
            "title": "批量通过候选 1",
            "category": "最佳实践",
            "project": "ProjectX",
            "tags": ["batch"],
            "excerpt": "批量通过候选 1。",
            "draft_content": "## 推荐做法\n\n批量通过候选 1。",
            "reasons": ["需要批量处理"],
            "source_text": "批量通过候选 1。",
            "score": 7,
            "confidence": "medium",
            "status": "pending",
            "auto_decision": "review",
        }
    )
    await registry_store.upsert_review_item(
        {
            "review_id": "review-batch-2",
            "session_id": "auto-session-001",
            "title": "批量通过候选 2",
            "category": "最佳实践",
            "project": "ProjectX",
            "tags": ["batch"],
            "excerpt": "批量通过候选 2。",
            "draft_content": "## 推荐做法\n\n批量通过候选 2。",
            "reasons": ["需要批量处理"],
            "source_text": "批量通过候选 2。",
            "score": 7,
            "confidence": "medium",
            "status": "pending",
            "auto_decision": "review",
        }
    )

    router = create_api_router(
        dashboard_logger=dashboard_logger,
        vector_store=vector_store,
        registry_store=registry_store,
        config=config,
        embedder=embedder,
        feishu_doc_manager=feishu_doc_manager,
    )

    overview_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/automation/overview")
    sessions_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/automation/sessions")
    reviews_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/automation/reviews")
    approve_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/automation/reviews/{review_id}/approve")
    reject_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/automation/reviews/{review_id}/reject")
    batch_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/automation/reviews/batch")

    initial_overview = await overview_endpoint()
    assert initial_overview["total_sessions"] == 1
    assert initial_overview["pending_review_items"] == 4
    assert initial_overview["total_auto_saved"] == 1
    assert initial_overview["total_review_queued"] == 3

    sessions_response = await sessions_endpoint(page=1, page_size=10)
    assert sessions_response["total"] == 1
    assert sessions_response["sessions"][0]["session_id"] == "auto-session-001"
    assert sessions_response["sessions"][0]["keywords"] == ["审核", "闭环"]

    reviews_response = await reviews_endpoint(status="pending", session_id="auto-session-001", project="ProjectX", confidence="medium", page=1, page_size=10)
    assert reviews_response["total"] == 4
    assert {item["review_id"] for item in reviews_response["items"]} == {
        "review-approve",
        "review-reject",
        "review-batch-1",
        "review-batch-2",
    }

    approve_response = await approve_endpoint("review-approve")
    assert approve_response["status"] == "success"
    assert approve_response["card_skill_id"]
    approved_item = await registry_store.get_review_item("review-approve")
    assert approved_item is not None
    assert approved_item["status"] == "approved"
    assert approved_item["related_skill_id"] == approve_response["card_skill_id"]

    reject_response = await reject_endpoint("review-reject", RejectReviewPayload(reason="内容重复，无需沉淀"))
    assert reject_response["status"] == "success"
    rejected_item = await registry_store.get_review_item("review-reject")
    assert rejected_item is not None
    assert rejected_item["status"] == "rejected"
    assert rejected_item["last_error"] == "内容重复，无需沉淀"

    batch_response = await batch_endpoint(
        BatchReviewPayload(
            action="approve",
            review_ids=["review-batch-1", "review-batch-2"],
            reason="",
        )
    )
    assert batch_response["action"] == "approve"
    assert batch_response["success_count"] == 2
    assert batch_response["failed_count"] == 0

    approved_batch_1 = await registry_store.get_review_item("review-batch-1")
    approved_batch_2 = await registry_store.get_review_item("review-batch-2")
    assert approved_batch_1["status"] == "approved"
    assert approved_batch_2["status"] == "approved"
    assert approved_batch_1["related_skill_id"]
    assert approved_batch_2["related_skill_id"]

    saved_records = await registry_store.list_records(limit=None, deleted=False)
    assert len(saved_records) == 3

    final_overview = await overview_endpoint()
    assert final_overview["pending_review_items"] == 0
    assert final_overview["approved_review_items"] == 3
    assert final_overview["rejected_review_items"] == 1

    approved_reviews_response = await reviews_endpoint(status="approved", session_id="", project="", confidence="", page=1, page_size=10)
    assert approved_reviews_response["total"] == 3
    assert {item["review_id"] for item in approved_reviews_response["items"]} == {
        "review-approve",
        "review-batch-1",
        "review-batch-2",
    }


@pytest.mark.asyncio
async def test_governance_review_approval_reuses_or_merges_existing_skills(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)
    vector_store = _LifecycleVectorStore([])
    feishu_doc_manager = _LifecycleFeishuDocManager()
    embedder = _FakeEmbedder()
    config = {
        "feishu": {
            "wiki_space_id": "space-001",
            "category_nodes": {
                "最佳实践": "node-best-practice",
            },
        },
        "governance": {
            "enabled": True,
            "exact_title_merge_enabled": True,
            "semantic_merge_enabled": True,
            "semantic_merge_score_threshold": 0.9,
            "max_related_skills": 3,
        },
    }

    tools = _create_automation_toolset(
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )

    existing_same_title = await registry_store.upsert(
        {
            "skill_id": "skill-existing-merge",
            "title": "已有最佳实践",
            "category": "最佳实践",
            "project": "ProjectX",
            "tags": ["old"],
            "feishu_doc_url": "https://feishu.cn/docx/doc-existing-merge",
            "feishu_doc_token": "doc-existing-merge",
            "wiki_node_token": "wiki-existing-merge",
            "content_hash": "hash-old-1",
            "version": 1,
            "sync_status": "INDEXED",
            "deleted": False,
        }
    )
    feishu_doc_manager.documents["doc-existing-merge"] = {
        "title": "[ProjectX] 已有最佳实践",
        "content": "# [ProjectX] 已有最佳实践\n\n> 📂 分类：最佳实践 | 🏷️ 项目：ProjectX | 🆔 技能ID：skill-existing-merge | 🕒 更新时间：2026-03-17T10:00:00\n\n---\n\n旧的治理实践正文",
        "space_id": "space-001",
        "parent_node": "node-best-practice",
        "wiki_node_token": "wiki-existing-merge",
        "doc_url": "https://feishu.cn/docx/doc-existing-merge",
        "deleted": False,
    }
    vector_store.point_ids.append("skill-existing-merge")
    vector_store.documents["skill-existing-merge"] = {
        "metadata": {
            "title": "已有最佳实践",
            "category": "最佳实践",
            "project": "ProjectX",
            "feishu_doc_url": "https://feishu.cn/docx/doc-existing-merge",
            "sync_status": "INDEXED",
            "deleted": False,
        },
        "document": "旧的治理实践正文",
    }

    existing_same_hash = await registry_store.upsert(
        {
            "skill_id": "skill-existing-reuse",
            "title": "完全重复知识",
            "category": "最佳实践",
            "project": "ProjectX",
            "tags": ["duplicate"],
            "feishu_doc_url": "https://feishu.cn/docx/doc-existing-reuse",
            "feishu_doc_token": "doc-existing-reuse",
            "wiki_node_token": "wiki-existing-reuse",
            "content_hash": "d0c95f0fb4b6dbd55c5a98f49ef6bfd5d86ce15f152c5e7fc7f1711cf09890f2",
            "version": 1,
            "sync_status": "INDEXED",
            "deleted": False,
        }
    )
    feishu_doc_manager.documents["doc-existing-reuse"] = {
        "title": "[ProjectX] 完全重复知识",
        "content": "# [ProjectX] 完全重复知识\n\n> 📂 分类：最佳实践 | 🏷️ 项目：ProjectX | 🆔 技能ID：skill-existing-reuse | 🕒 更新时间：2026-03-17T10:00:00\n\n---\n\n重复正文",
        "space_id": "space-001",
        "parent_node": "node-best-practice",
        "wiki_node_token": "wiki-existing-reuse",
        "doc_url": "https://feishu.cn/docx/doc-existing-reuse",
        "deleted": False,
    }
    vector_store.point_ids.append("skill-existing-reuse")
    vector_store.documents["skill-existing-reuse"] = {
        "metadata": {
            "title": "完全重复知识",
            "category": "最佳实践",
            "project": "ProjectX",
            "feishu_doc_url": "https://feishu.cn/docx/doc-existing-reuse",
            "sync_status": "INDEXED",
            "deleted": False,
        },
        "document": "重复正文",
    }

    await registry_store.upsert_automation_session(
        {
            "session_id": "auto-session-governance",
            "project": "ProjectX",
            "user_goal": "验证治理合并",
            "normalized_query": "验证治理合并",
            "raw_query": "验证治理合并",
            "keywords": ["治理", "合并"],
            "retrieval_status": "success",
            "extraction_status": "success",
            "save_status": "success",
        }
    )

    await registry_store.upsert_review_item(
        {
            "review_id": "review-merge-existing",
            "session_id": "auto-session-governance",
            "title": "已有最佳实践",
            "category": "最佳实践",
            "project": "ProjectX",
            "tags": ["new"],
            "excerpt": "新的补充内容",
            "draft_content": "新的补充内容",
            "reasons": ["标题完全一致"],
            "source_text": "新的补充内容",
            "score": 8,
            "confidence": "medium",
            "status": "pending",
            "auto_decision": "review",
        }
    )
    await registry_store.upsert_review_item(
        {
            "review_id": "review-reuse-existing",
            "session_id": "auto-session-governance",
            "title": "完全重复知识",
            "category": "最佳实践",
            "project": "ProjectX",
            "tags": ["duplicate"],
            "excerpt": "重复正文",
            "draft_content": "重复正文",
            "reasons": ["内容完全重复"],
            "source_text": "重复正文",
            "score": 8,
            "confidence": "medium",
            "status": "pending",
            "auto_decision": "review",
        }
    )

    merge_result = await tools["approve_review_item"]("review-merge-existing")
    reuse_result = await tools["approve_review_item"]("review-reuse-existing")

    assert "治理动作：merge_existing" in merge_result[0].text
    assert "治理动作：reuse_existing" in reuse_result[0].text

    merged_record = await registry_store.get(existing_same_title["skill_id"])
    assert merged_record is not None
    assert merged_record["version"] == 2
    assert "skill-existing-merge" in vector_store.documents
    assert "新的补充内容" in vector_store.documents["skill-existing-merge"]["document"]

    reused_review = await registry_store.get_review_item("review-reuse-existing")
    assert reused_review is not None
    assert reused_review["status"] == "approved"
    assert reused_review["related_skill_id"] == existing_same_hash["skill_id"]
    assert reused_review["auto_decision"] == "reuse_existing"


@pytest.mark.asyncio
async def test_governance_and_remote_runtime_api_expose_step_three_state(tmp_path):
    dashboard_logger = _create_dashboard_logger(tmp_path)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    await registry_store.upsert_review_item(
        {
            "review_id": "review-create",
            "session_id": "session-1",
            "title": "建议新建",
            "category": "最佳实践",
            "project": "ProjectX",
            "score": 7,
            "confidence": "medium",
            "status": "pending",
            "auto_decision": "create_new",
        }
    )
    await registry_store.upsert_review_item(
        {
            "review_id": "review-merge",
            "session_id": "session-1",
            "title": "建议合并",
            "category": "最佳实践",
            "project": "ProjectX",
            "score": 7,
            "confidence": "medium",
            "status": "approved",
            "auto_decision": "merge_existing",
            "related_skill_id": "skill-merge-1",
        }
    )
    await registry_store.upsert_review_item(
        {
            "review_id": "review-reuse",
            "session_id": "session-1",
            "title": "建议复用",
            "category": "最佳实践",
            "project": "ProjectX",
            "score": 7,
            "confidence": "medium",
            "status": "approved",
            "auto_decision": "reuse_existing",
            "related_skill_id": "skill-reuse-1",
        }
    )

    config = {
        "remote_service": {
            "auth_enabled": True,
            "rate_limit_per_minute": 120,
            "request_timeout_seconds": 30.0,
            "max_concurrency": 20,
            "trust_forwarded_ip": False,
        },
        "_service_info_provider": lambda: {
            "service": "feishu-knowledge-mcp",
            "dashboard": {
                "url": "http://127.0.0.1:8080",
                "runtime_url": "http://127.0.0.1:8080/runtime",
            },
            "mcp": {
                "transport": "sse",
                "enabled": True,
                "sse_url": "https://mcp.example.com/mcp/sse",
                "message_url": "https://mcp.example.com/mcp/messages",
            },
            "remote_service": {
                "auth_enabled": True,
                "rate_limit_per_minute": 120,
                "request_timeout_seconds": 30.0,
                "max_concurrency": 20,
                "trust_forwarded_ip": False,
            },
        },
    }

    router = create_api_router(
        dashboard_logger=dashboard_logger,
        vector_store=_FakeVectorStore([]),
        registry_store=registry_store,
        config=config,
    )

    governance_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/governance/overview")
    runtime_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/runtime/remote-service")
    automation_overview_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/automation/overview")

    governance_response = await governance_endpoint()
    runtime_response = await runtime_endpoint()
    automation_response = await automation_overview_endpoint()

    assert governance_response["review_create_new"] == 1
    assert governance_response["review_merge_existing"] == 1
    assert governance_response["review_reuse_existing"] == 1
    assert governance_response["pending_with_related_skill"] == 0
    assert governance_response["approved_merge_existing"] == 1
    assert governance_response["approved_reuse_existing"] == 1

    assert runtime_response["service"] == "feishu-knowledge-mcp"
    assert runtime_response["mcp"]["transport"] == "sse"
    assert runtime_response["remote_service"]["auth_enabled"] is True
    assert runtime_response["remote_service"]["max_concurrency"] == 20

    assert automation_response["governance"]["review_merge_existing"] == 1
    assert automation_response["governance"]["review_reuse_existing"] == 1