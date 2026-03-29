"""
Dashboard Web 服务入口

基于 FastAPI 的 Web 服务，提供监控面板页面和 REST API。
与 MCP Server 共用同一个进程（通过后台线程启动）。
"""

import logging
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .api import create_api_router

logger = logging.getLogger(__name__)

# 静态文件目录
STATIC_DIR = Path(__file__).parent / "static"


def create_dashboard_app(
    dashboard_logger,
    vector_store,
    registry_store=None,
    service_info_provider: Callable[[], dict] | None = None,
    config: dict | None = None,
    embedder=None,
    feishu_doc_manager=None,
) -> FastAPI:
    """
    创建 Dashboard FastAPI 应用

    Args:
        dashboard_logger: Dashboard 日志记录器实例
        vector_store: 向量数据库实例
        registry_store: 知识注册表实例（可选）
        service_info_provider: 运行时信息提供器（可选）

    Returns:
        FastAPI 应用实例
    """

    dashboard_app = FastAPI(
        title="Knowledge MCP Dashboard",
        description="飞书知识库 MCP Server 监控面板",
        version="1.0.0",
    )

    if config is not None and service_info_provider is not None:
        config["_service_info_provider"] = service_info_provider

    # 注册 API 路由
    api_router = create_api_router(
        dashboard_logger,
        vector_store,
        registry_store,
        config=config,
        embedder=embedder,
        feishu_doc_manager=feishu_doc_manager,
    )
    dashboard_app.include_router(api_router)

    # 挂载静态文件
    if STATIC_DIR.exists():
        dashboard_app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    @dashboard_app.get("/")
    async def index():
        """Dashboard 首页"""
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return {"message": "Knowledge MCP Dashboard API is running. Static files not found."}

    @dashboard_app.get("/health")
    async def health():
        """轻量活性检查。"""
        return {
            "status": "healthy",
            "service": "feishu-knowledge-mcp-dashboard",
        }

    @dashboard_app.get("/ready")
    async def readiness():
        """依赖就绪检查。"""
        dependencies = {
            "dashboard_database": {"status": "unknown"},
            "registry_store": {"status": "unknown"},
            "vector_store": {"status": "unknown"},
        }
        errors = []

        try:
            await dashboard_logger.healthcheck()
            dependencies["dashboard_database"] = {"status": "healthy"}
        except Exception as exc:
            dependencies["dashboard_database"] = {
                "status": "unhealthy",
                "error": str(exc),
            }
            errors.append(f"dashboard_database: {exc}")

        if registry_store is not None:
            try:
                await registry_store.count_active()
                dependencies["registry_store"] = {"status": "healthy"}
            except Exception as exc:
                dependencies["registry_store"] = {
                    "status": "unhealthy",
                    "error": str(exc),
                }
                errors.append(f"registry_store: {exc}")
        else:
            dependencies["registry_store"] = {
                "status": "unhealthy",
                "error": "registry_store 未初始化",
            }
            errors.append("registry_store: registry_store 未初始化")

        try:
            collection_info = vector_store.get_collection_info()
            dependencies["vector_store"] = {
                "status": "healthy",
                "details": collection_info,
            }
        except Exception as exc:
            dependencies["vector_store"] = {
                "status": "unhealthy",
                "error": str(exc),
            }
            errors.append(f"vector_store: {exc}")

        if errors:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "unready",
                    "service": "feishu-knowledge-mcp-dashboard",
                    "dependencies": dependencies,
                    "errors": errors,
                },
            )

        return {
            "status": "ready",
            "service": "feishu-knowledge-mcp-dashboard",
            "dependencies": dependencies,
        }

    @dashboard_app.get("/runtime")
    async def runtime_info():
        """输出服务运行时接入信息，便于部署后排查连接配置。"""
        if service_info_provider is None:
            return {
                "service": "feishu-knowledge-mcp",
                "dashboard": {
                    "status": "available",
                },
                "mcp": {
                    "status": "unknown",
                },
            }

        return service_info_provider()

    logger.info("Dashboard FastAPI 应用创建完成")
    return dashboard_app
