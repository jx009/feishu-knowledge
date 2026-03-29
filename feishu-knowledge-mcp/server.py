"""
飞书知识库 MCP Server —— 主入口

启动 MCP Server，注册所有工具，集成 Dashboard 监控面板。
支持两种运行模式：
    1. stdio：本地开发 / MCP 本地进程接入
    2. sse：服务器部署 / 多设备统一远程接入

使用方式：
    python server.py

环境变量（可选，覆盖 config.yaml 中的配置）：
    FEISHU_APP_ID          - 飞书 App ID
    FEISHU_APP_SECRET      - 飞书 App Secret
    OPENAI_API_KEY         - OpenAI API Key
    OPENAI_API_BASE        - OpenAI API 地址（代理/中转）
    QDRANT_URL             - Qdrant 地址
    DASHBOARD_DATABASE_URL - Dashboard PostgreSQL 连接串
    MCP_TRANSPORT          - MCP 传输模式（stdio / sse）
    MCP_HOST               - 远程 MCP 服务监听地址
    MCP_PORT               - 远程 MCP 服务监听端口
"""

import asyncio
import logging
import socket
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),  # MCP 通信使用 stdout，日志输出到 stderr
    ],
)
logger = logging.getLogger("feishu-knowledge-mcp")

# 创建 MCP Server 实例
app = Server("feishu-knowledge-mcp")


def _normalize_http_path(path_value: str, *, trailing_slash: bool = False) -> str:
    raw_path = str(path_value or "").strip()
    if not raw_path:
        return "/"

    normalized = "/" + raw_path.strip("/")
    if trailing_slash:
        if not normalized.endswith("/"):
            normalized += "/"
    elif normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def _resolve_display_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _host_bindings_conflict(host_a: str, host_b: str) -> bool:
    if host_a == host_b:
        return True
    wildcard_hosts = {"0.0.0.0", "::"}
    return host_a in wildcard_hosts or host_b in wildcard_hosts


def _build_local_http_base_url(host: str, port: int) -> str:
    return f"http://{_resolve_display_host(host)}:{port}"


def _build_service_info(config: dict) -> dict:
    runtime_config = config.get("runtime", {}) or {}
    dashboard_config = config.get("dashboard", {}) or {}
    mcp_config = config.get("mcp", {}) or {}
    remote_service_config = config.get("remote_service", {}) or {}

    dashboard_host = str(dashboard_config.get("host") or "0.0.0.0")
    dashboard_port = int(dashboard_config.get("port") or 8080)
    dashboard_enabled = bool(dashboard_config.get("enabled", False))

    mcp_transport = str(mcp_config.get("transport") or "stdio").strip().lower()
    mcp_host = str(mcp_config.get("host") or "0.0.0.0")
    mcp_port = int(mcp_config.get("port") or 8001)
    mcp_sse_path = _normalize_http_path(mcp_config.get("sse_path") or "/mcp/sse")
    mcp_message_path = _normalize_http_path(mcp_config.get("message_path") or "/mcp/messages", trailing_slash=True)
    public_base_url = str(mcp_config.get("public_base_url") or "").strip().rstrip("/")
    mcp_base_url = public_base_url or _build_local_http_base_url(mcp_host, mcp_port)

    service_info = {
        "service": "feishu-knowledge-mcp",
        "runtime": {
            "environment": runtime_config.get("environment", "development"),
            "data_dir": runtime_config.get("data_dir", ""),
        },
        "dashboard": {
            "enabled": dashboard_enabled,
            "host": dashboard_host,
            "port": dashboard_port,
            "url": _build_local_http_base_url(dashboard_host, dashboard_port) if dashboard_enabled else "",
            "health_url": f"{_build_local_http_base_url(dashboard_host, dashboard_port)}/health" if dashboard_enabled else "",
            "ready_url": f"{_build_local_http_base_url(dashboard_host, dashboard_port)}/ready" if dashboard_enabled else "",
            "runtime_url": f"{_build_local_http_base_url(dashboard_host, dashboard_port)}/runtime" if dashboard_enabled else "",
        },
        "mcp": {
            "transport": mcp_transport,
            "host": mcp_host,
            "port": mcp_port,
            "public_base_url": public_base_url,
        },
        "remote_service": {
            "auth_enabled": bool(remote_service_config.get("auth_enabled", False)),
            "rate_limit_per_minute": int(remote_service_config.get("rate_limit_per_minute", 120) or 120),
            "request_timeout_seconds": float(remote_service_config.get("request_timeout_seconds", 30.0) or 30.0),
            "max_concurrency": int(remote_service_config.get("max_concurrency", 20) or 20),
            "trust_forwarded_ip": bool(remote_service_config.get("trust_forwarded_ip", False)),
        },
    }

    if mcp_transport == "sse":
        service_info["mcp"].update(
            {
                "enabled": True,
                "sse_path": mcp_sse_path,
                "message_path": mcp_message_path,
                "base_url": mcp_base_url,
                "sse_url": f"{mcp_base_url}{mcp_sse_path}",
                "message_url": f"{mcp_base_url}{mcp_message_path.rstrip('/')}"
            }
        )
    else:
        service_info["mcp"].update(
            {
                "enabled": False,
                "mode": "local_stdio",
                "description": "当前为本地 stdio 模式，仅适用于本机进程接入。",
            }
        )

    return service_info


def _configure_runtime_logging(config: dict):
    logging_config = config.get("logging", {}) or {}
    root_logger = logging.getLogger()

    level_name = str(logging_config.get("level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger.setLevel(level)

    if not logging_config.get("file_enabled"):
        logger.info("📝 文件日志未启用，将仅输出到 stderr")
        return

    log_directory = Path(str(logging_config.get("directory") or "logs")).expanduser()
    log_filename = str(logging_config.get("filename") or "server.log").strip()
    log_path = log_directory / log_filename
    log_directory.mkdir(parents=True, exist_ok=True)

    existing_file_handler = next(
        (
            handler for handler in root_logger.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(getattr(handler, "baseFilename", "")) == log_path
        ),
        None,
    )
    if existing_file_handler is not None:
        logger.info("📝 文件日志已启用: %s", log_path)
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    logger.info("📝 文件日志已启用: %s", log_path)


def _log_startup_summary(config: dict):
    runtime_config = config.get("runtime", {}) or {}
    dashboard_config = config.get("dashboard", {}) or {}
    vector_config = config.get("vector", {}).get("qdrant", {}) or {}
    logging_config = config.get("logging", {}) or {}
    mcp_config = config.get("mcp", {}) or {}

    logger.info(
        "启动配置摘要 | env=%s | data_dir=%s | dashboard=%s | dashboard_host=%s | dashboard_port=%s | mcp_transport=%s | mcp_host=%s | mcp_port=%s | qdrant_url=%s | log_level=%s | file_logging=%s",
        runtime_config.get("environment", "development"),
        runtime_config.get("data_dir", ""),
        dashboard_config.get("enabled", False),
        dashboard_config.get("host", "0.0.0.0"),
        dashboard_config.get("port", 8080),
        str(mcp_config.get("transport") or "stdio").lower(),
        mcp_config.get("host", "0.0.0.0"),
        mcp_config.get("port", 8001),
        vector_config.get("url", "http://localhost:6333"),
        str(logging_config.get("level") or "INFO").upper(),
        logging_config.get("file_enabled", False),
    )


def _ensure_tcp_port_available(host: str, port: int, service_name: str):
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe_host, port))
        except OSError as exc:
            raise RuntimeError(
                f"{service_name} 端口占用检查失败：{probe_host}:{port} 不可用，请检查是否已有进程占用该端口。原始错误: {exc}"
            ) from exc


def _run_startup_preflight(config: dict):
    runtime_config = config.get("runtime", {}) or {}
    dashboard_config = config.get("dashboard", {}) or {}
    logging_config = config.get("logging", {}) or {}
    mcp_config = config.get("mcp", {}) or {}

    data_dir = Path(str(runtime_config.get("data_dir") or "")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    logger.info("运行时目录已确认: %s", data_dir)

    if logging_config.get("file_enabled"):
        log_directory = Path(str(logging_config.get("directory") or "")).expanduser()
        log_directory.mkdir(parents=True, exist_ok=True)
        logger.info("日志目录已确认: %s", log_directory)

    if dashboard_config.get("enabled", False):
        dashboard_host = str(dashboard_config.get("host") or "0.0.0.0")
        dashboard_port = int(dashboard_config.get("port") or 8080)
        _ensure_tcp_port_available(dashboard_host, dashboard_port, "Dashboard Web 服务")
        logger.info("Dashboard 端口预检通过: %s:%s", dashboard_host, dashboard_port)
    else:
        dashboard_host = str(dashboard_config.get("host") or "0.0.0.0")
        dashboard_port = int(dashboard_config.get("port") or 8080)

    mcp_transport = str(mcp_config.get("transport") or "stdio").strip().lower()
    if mcp_transport == "sse":
        mcp_host = str(mcp_config.get("host") or "0.0.0.0")
        mcp_port = int(mcp_config.get("port") or 8001)

        if dashboard_config.get("enabled", False) and dashboard_port == mcp_port and _host_bindings_conflict(dashboard_host, mcp_host):
            raise RuntimeError(
                f"Dashboard 与 MCP 远程服务不能复用同一监听地址：dashboard={dashboard_host}:{dashboard_port}，mcp={mcp_host}:{mcp_port}"
            )

        _ensure_tcp_port_available(mcp_host, mcp_port, "MCP SSE 服务")
        logger.info("MCP 远程端口预检通过: %s:%s", mcp_host, mcp_port)


def _start_dashboard(
    config,
    dashboard_logger,
    vector_store,
    registry_store=None,
    embedder=None,
    feishu_doc_manager=None,
):
    """在后台线程中启动 Dashboard Web 服务"""
    import uvicorn
    from dashboard.app import create_dashboard_app

    dashboard_config = config["dashboard"]
    dashboard_app = create_dashboard_app(
        dashboard_logger,
        vector_store,
        registry_store,
        service_info_provider=lambda: _build_service_info(config),
        config=config,
        embedder=embedder,
        feishu_doc_manager=feishu_doc_manager,
    )

    host = dashboard_config.get("host", "0.0.0.0")
    port = dashboard_config.get("port", 8080)

    def run_dashboard():
        uvicorn.run(
            dashboard_app,
            host=host,
            port=port,
            log_level="warning",  # 减少 uvicorn 的日志输出
        )

    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()

    service_info = _build_service_info(config)
    logger.info("✅ Dashboard Web 面板已启动: %s", service_info["dashboard"]["url"])
    logger.info(
        "✅ Dashboard 健康检查地址: %s | 就绪检查地址: %s | 运行时信息地址: %s",
        service_info["dashboard"]["health_url"],
        service_info["dashboard"]["ready_url"],
        service_info["dashboard"]["runtime_url"],
    )


def _run_stdio_mcp_server():
    logger.info("🚀 MCP Server 已就绪（stdio 模式），等待本地进程连接...")

    async def run():
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("MCP Server 已停止")


def _run_sse_mcp_server(config: dict):
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route
    from starlette.types import Receive, Scope, Send

    try:
        from mcp.server.sse import SseServerTransport
    except Exception as exc:
        raise RuntimeError("当前安装的 mcp SDK 不支持 SSE 远程传输，请升级 mcp 依赖后重试。") from exc

    mcp_config = config.get("mcp", {}) or {}
    host = str(mcp_config.get("host") or "0.0.0.0")
    port = int(mcp_config.get("port") or 8001)
    sse_path = _normalize_http_path(mcp_config.get("sse_path") or "/mcp/sse")
    message_path = _normalize_http_path(mcp_config.get("message_path") or "/mcp/messages", trailing_slash=True)
    remote_service_config = config.get("remote_service", {}) or {}
    auth_enabled = bool(remote_service_config.get("auth_enabled", False))
    auth_tokens = {
        str(token).strip()
        for token in (remote_service_config.get("auth_tokens") or [])
        if str(token).strip()
    }
    rate_limit_per_minute = max(1, int(remote_service_config.get("rate_limit_per_minute", 120) or 120))
    request_timeout_seconds = max(1.0, float(remote_service_config.get("request_timeout_seconds", 30.0) or 30.0))
    max_concurrency = max(1, int(remote_service_config.get("max_concurrency", 20) or 20))
    trust_forwarded_ip = bool(remote_service_config.get("trust_forwarded_ip", False))

    sse_transport = SseServerTransport(message_path)
    remote_semaphore = asyncio.Semaphore(max_concurrency)
    request_counters: dict[str, deque[float]] = defaultdict(deque)

    def _client_ip(request: Request) -> str:
        if trust_forwarded_ip:
            forwarded_for = request.headers.get("x-forwarded-for", "")
            if forwarded_for.strip():
                return forwarded_for.split(",")[0].strip()
        if request.client and request.client.host:
            return request.client.host
        return "unknown"

    def _extract_auth_token(request: Request) -> str:
        auth_header = request.headers.get("authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return request.headers.get("x-api-key", "").strip()

    def _authorize_request(request: Request) -> tuple[bool, str]:
        if not auth_enabled:
            return True, ""
        token = _extract_auth_token(request)
        if token and token in auth_tokens:
            return True, ""
        return False, "未通过远程服务鉴权，请提供有效的 Bearer Token 或 X-API-Key。"

    def _allow_request_rate(client_id: str) -> bool:
        now = time.time()
        window = request_counters[client_id]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= rate_limit_per_minute:
            return False
        window.append(now)
        return True

    @asynccontextmanager
    async def _guard_remote_request(request: Request):
        request_id = request.headers.get("x-request-id", "").strip() or uuid.uuid4().hex[:16]
        client_id = _client_ip(request)
        request.state.request_id = request_id
        request.state.client_id = client_id

        authorized, message = _authorize_request(request)
        if not authorized:
            logger.warning("远程请求鉴权失败 | request_id=%s | client=%s | path=%s", request_id, client_id, request.url.path)
            yield JSONResponse(
                {"error": message, "request_id": request_id},
                status_code=401,
                headers={"x-request-id": request_id},
            )
            return

        if not _allow_request_rate(client_id):
            logger.warning("远程请求限流触发 | request_id=%s | client=%s | path=%s", request_id, client_id, request.url.path)
            yield JSONResponse(
                {"error": "请求过于频繁，请稍后重试。", "request_id": request_id},
                status_code=429,
                headers={"x-request-id": request_id},
            )
            return

        await remote_semaphore.acquire()
        started_at = time.perf_counter()
        try:
            yield request_id
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            remote_semaphore.release()
            logger.info(
                "远程请求完成 | request_id=%s | client=%s | path=%s | elapsed_ms=%.2f",
                request_id,
                client_id,
                request.url.path,
                elapsed_ms,
            )

    async def _audit_remote_access(request_id: str, client_id: str, path: str, method: str, status: str, error: str | None = None):
        if dashboard_logger is None:
            return
        try:
            await dashboard_logger.log_remote_access(
                request_id=request_id,
                client_id=client_id,
                path=path,
                method=method,
                status=status,
                error=error,
            )
        except Exception as audit_error:
            logger.warning("远程访问审计日志记录失败: %s", audit_error)

    async def handle_sse(request):
        async with _guard_remote_request(request) as guard_result:
            if not isinstance(guard_result, str):
                return guard_result

            request_id = guard_result
            try:
                async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
                    await _audit_remote_access(request_id, request.state.client_id, request.url.path, request.method, "running")
                    await app.run(streams[0], streams[1], app.create_initialization_options())
                await _audit_remote_access(request_id, request.state.client_id, request.url.path, request.method, "success")
            except Exception as exc:
                await _audit_remote_access(request_id, request.state.client_id, request.url.path, request.method, "failed", str(exc))
                raise
            return PlainTextResponse("MCP SSE 连接已关闭", headers={"x-request-id": request_id})

    async def handle_message(scope: Scope, receive: Receive, send: Send):
        request = Request(scope, receive)
        async with _guard_remote_request(request) as guard_result:
            if not isinstance(guard_result, str):
                response = guard_result
                await response(scope, receive, send)
                return

            request_id = guard_result
            try:
                await _audit_remote_access(request_id, request.state.client_id, request.url.path, request.method, "running")
                await asyncio.wait_for(
                    sse_transport.handle_post_message(scope, receive, send),
                    timeout=request_timeout_seconds,
                )
                await _audit_remote_access(request_id, request.state.client_id, request.url.path, request.method, "success")
            except asyncio.TimeoutError:
                logger.warning("MCP 消息请求超时 | request_id=%s | path=%s", request_id, request.url.path)
                await _audit_remote_access(request_id, request.state.client_id, request.url.path, request.method, "failed", "timeout")
                response = JSONResponse(
                    {"error": "远程请求处理超时。", "request_id": request_id},
                    status_code=504,
                    headers={"x-request-id": request_id},
                )
                await response(scope, receive, send)
            except Exception as exc:
                await _audit_remote_access(request_id, request.state.client_id, request.url.path, request.method, "failed", str(exc))
                raise

    async def remote_runtime(request: Request):
        service_info = _build_service_info(config)
        service_info["remote_runtime"] = {
            "request_id": request.headers.get("x-request-id", "").strip() or uuid.uuid4().hex[:16],
            "client_id": _client_ip(request),
            "active_requests": max_concurrency - remote_semaphore._value,
            "rate_limit_per_minute": rate_limit_per_minute,
            "request_timeout_seconds": request_timeout_seconds,
            "auth_enabled": auth_enabled,
        }
        return JSONResponse(service_info)

    mcp_http_app = Starlette(
        routes=[
            Route(sse_path, endpoint=handle_sse),
            Route("/runtime", endpoint=remote_runtime),
            Mount(message_path, app=handle_message),
        ]
    )

    service_info = _build_service_info(config)
    logger.info("🚀 MCP Server 已就绪（SSE 模式），等待远程客户端连接...")
    logger.info(
        "✅ 远程 MCP 接入地址: %s | 消息提交地址: %s",
        service_info["mcp"]["sse_url"],
        service_info["mcp"]["message_url"],
    )

    try:
        uvicorn.run(
            mcp_http_app,
            host=host,
            port=port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        logger.info("MCP SSE 服务已停止")


def _run_mcp_server(config: dict):
    mcp_transport = str(config.get("mcp", {}).get("transport") or "stdio").strip().lower()
    if mcp_transport == "sse":
        _run_sse_mcp_server(config)
        return
    _run_stdio_mcp_server()


def main():
    """主入口函数"""

    logger.info("=" * 60)
    logger.info("飞书知识库 MCP Server 启动中...")
    logger.info("=" * 60)

    # 1. 加载配置
    from config import load_config

    try:
        config = load_config()
        _configure_runtime_logging(config)
        _run_startup_preflight(config)
        logger.info("✅ 配置加载成功")
        _log_startup_summary(config)
    except FileNotFoundError as e:
        logger.error("❌ %s", e)
        sys.exit(1)
    except ValueError as e:
        logger.error("❌ %s", e)
        sys.exit(1)
    except RuntimeError as e:
        logger.error("❌ 启动预检失败: %s", e)
        sys.exit(1)

    # 2. 初始化各模块
    from feishu.document import FeishuDocManager
    from vector.embedder import Embedder
    from vector.store import VectorStore

    try:
        embedder = Embedder(config["embedding"])
        logger.info("✅ OpenAI Embedding 初始化成功")

        vector_dimensions = int(config.get("embedding", {}).get("dimensions", 1536))
        vector_store = VectorStore(config["vector"], dimensions=vector_dimensions)
        logger.info("✅ Qdrant 向量数据库初始化成功")

        feishu_doc_manager = FeishuDocManager(config)
        logger.info("✅ 飞书文档管理器初始化成功")
    except ConnectionError as e:
        logger.error("❌ 连接失败: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("❌ 初始化失败: %s", e)
        sys.exit(1)

    # 3. 初始化 Dashboard 日志记录器与知识注册表（必需）
    dashboard_config = config.get("dashboard", {})
    dashboard_database_url = dashboard_config.get("database_url")
    if not dashboard_database_url:
        logger.error("❌ dashboard.database_url 未配置，知识注册表无法初始化，服务已禁止启动")
        sys.exit(1)

    logger.info(
        "Dashboard 依赖检查 | enabled=%s | host=%s | port=%s | database=%s",
        dashboard_config.get("enabled", False),
        dashboard_config.get("host", "0.0.0.0"),
        dashboard_config.get("port", 8080),
        dashboard_database_url,
    )

    try:
        from dashboard.logger import DashboardLogger
        from dashboard.registry import SkillRegistryStore

        dashboard_logger = DashboardLogger(dashboard_database_url)
        asyncio.run(dashboard_logger.init_db())
        registry_store = SkillRegistryStore(dashboard_logger)
        config["_dashboard_logger"] = dashboard_logger
        logger.info("✅ Dashboard 数据库与注册表初始化成功")
    except Exception as e:
        logger.error("❌ Dashboard 数据库或注册表初始化失败，服务已禁止启动: %s", e)
        sys.exit(1)

    # 4. 注册所有 MCP 工具
    from tools.automation_review import register_automation_review
    from tools.automation_workflow import register_automation_workflow
    from tools.extract_skills import register_extract_skills
    from tools.list_skills import register_list_skills
    from tools.manage_skill import register_manage_skill
    from tools.save_skill import register_save_skill
    from tools.search_skill import register_search_skill

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
    register_list_skills(
        app=app,
        config=config,
        registry_store=registry_store,
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
    register_extract_skills(
        app=app,
        config=config,
        dashboard_logger=dashboard_logger,
    )
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

    logger.info(
        "✅ MCP 工具注册完成: save_skill, search_skill, list_skills, update_skill, delete_skill, extract_skills, start_auto_session, finish_auto_session, list_review_queue, approve_review_item, reject_review_item, batch_review_items_tool"
    )

    # 5. 启动 Dashboard Web 服务（后台线程）
    if dashboard_config.get("enabled", False):
        try:
            _start_dashboard(
                config,
                dashboard_logger,
                vector_store,
                registry_store,
                embedder,
                feishu_doc_manager,
            )

        except Exception as e:
            logger.error("❌ Dashboard Web 服务启动失败: %s", e)
            sys.exit(1)

    # 6. 启动 MCP Server（stdio / sse）
    try:
        _run_mcp_server(config)
    except RuntimeError as e:
        logger.error("❌ MCP 服务启动失败: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()