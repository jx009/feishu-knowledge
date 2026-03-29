"""
配置管理模块
- 加载 config.yaml 配置文件
- 支持环境变量覆盖（优先级：环境变量 > config.yaml）
- 必填项校验
- 支持 dashboard 数据库连接配置规范化（兼容旧 db_path 并展开 SQLite 路径）
"""

import json
import os
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import load_dotenv


def _deep_set(d: dict, keys: list, value):
    """在嵌套字典中设置值"""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _parse_bool(value: str) -> bool:

    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_category_nodes(value: str) -> dict[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return {
            str(key).strip(): str(node_token).strip()
            for key, node_token in parsed.items()
            if str(key).strip() and str(node_token).strip()
        }

    category_nodes: dict[str, str] = {}
    for item in raw.split(","):
        category, separator, node_token = item.partition(":")
        if separator and category.strip() and node_token.strip():
            category_nodes[category.strip()] = node_token.strip()
    return category_nodes


def _parse_csv_list(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]

    return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]


def _resolve_path(path_value: str, data_dir: Path | None = None) -> str:
    raw_value = str(path_value or "").strip()
    if not raw_value:
        return ""

    path = Path(raw_value).expanduser()
    if not path.is_absolute() and data_dir is not None:
        path = data_dir / path
    return str(path.resolve())


def _build_base_config() -> dict:
    return {
        "runtime": {
            "environment": "development",
            "data_dir": ".feishu-knowledge",
        },
        "feishu": {
            "app_id": "",
            "app_secret": "",
            "wiki_space_id": "",
            "category_nodes": {},
        },
        "vector": {
            "provider": "qdrant_self_hosted",
            "qdrant": {
                "url": "http://localhost:6333",
                "api_key": "",
                "collection_name": "knowledge_skills",
            },
        },
        "embedding": {
            "api_key": "",
            "api_base": "https://api.openai.com/v1",
            "model": "text-embedding-3-small",
            "dimensions": 1536,
        },
        "knowledge": {
            "default_category": "最佳实践",
            "chunk_size": 500,
            "chunk_overlap": 50,
            "search_top_k": 5,
        },
        "dashboard": {
            "enabled": False,
            "host": "0.0.0.0",
            "port": 8080,
            "database_url": "",
        },
        "mcp": {
            "transport": "stdio",
            "host": "0.0.0.0",
            "port": 8001,
            "http_path": "/mcp",
            "sse_path": "/mcp/sse",
            "message_path": "/mcp/messages",
            "public_base_url": "",
        },
        "logging": {
            "level": "INFO",
            "file_enabled": False,
            "directory": "logs",
            "filename": "server.log",
        },
        "extraction": {
            "enabled": True,
            "max_candidates": 5,
            "min_score": 3,
            "min_segment_length": 120,
            "max_excerpt_length": 320,
            "include_full_text_fallback": True,
        },
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
        "governance": {
            "enabled": True,
            "exact_title_merge_enabled": True,
            "semantic_merge_enabled": True,
            "semantic_merge_score_threshold": 0.9,
            "max_related_skills": 3,
        },
        "remote_service": {
            "auth_enabled": False,
            "auth_tokens": [],
            "rate_limit_per_minute": 120,
            "request_timeout_seconds": 30.0,
            "max_concurrency": 20,
            "trust_forwarded_ip": False,
        },
    }


def _extract_sqlite_db_path(database_url: str | None) -> str | None:

    if not database_url:
        return None

    sqlite_prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    for prefix in sqlite_prefixes:
        if database_url.startswith(prefix):
            return str(Path(database_url[len(prefix):]).expanduser())
    return None


def _normalize_dashboard_database_config(config: dict, data_dir: Path | None = None):
    dashboard = config.get("dashboard")
    if not isinstance(dashboard, dict):
        return

    database_url = str(dashboard.get("database_url") or "").strip()
    legacy_db_path = str(dashboard.get("db_path") or "").strip()

    if not database_url and legacy_db_path:
        expanded_path = _resolve_path(legacy_db_path, data_dir)
        dashboard["database_url"] = f"sqlite+aiosqlite:///{expanded_path}"
        return

    sqlite_db_path = _extract_sqlite_db_path(database_url)
    if sqlite_db_path:
        normalized_path = _resolve_path(sqlite_db_path, data_dir)
        dashboard["database_url"] = f"sqlite+aiosqlite:///{normalized_path}"


def _expand_paths(config: dict):
    """规范化运行时目录、dashboard 数据库配置和日志目录。"""
    runtime_config = config.setdefault("runtime", {})
    data_dir = _resolve_path(runtime_config.get("data_dir") or ".feishu-knowledge")
    runtime_config["data_dir"] = data_dir
    data_dir_path = Path(data_dir)

    _normalize_dashboard_database_config(config, data_dir_path)

    logging_config = config.get("logging")
    if isinstance(logging_config, dict):
        directory = str(logging_config.get("directory") or "").strip()
        if directory:
            logging_config["directory"] = _resolve_path(directory, data_dir_path)


def _apply_defaults(config: dict):
    """补齐运行时需要的默认配置，避免业务代码到处分支判断。"""
    runtime = config.setdefault("runtime", {})
    runtime.setdefault("environment", "development")
    runtime.setdefault("data_dir", ".feishu-knowledge")

    feishu = config.setdefault("feishu", {})
    feishu_retry = feishu.setdefault("retry", {})
    feishu_retry.setdefault("max_attempts", 3)
    feishu_retry.setdefault("initial_delay_seconds", 1.0)
    feishu_retry.setdefault("backoff_multiplier", 2.0)

    embedding = config.setdefault("embedding", {})
    embedding.setdefault("dimensions", 1536)
    embedding_retry = embedding.setdefault("retry", {})
    embedding_retry.setdefault("max_attempts", 3)
    embedding_retry.setdefault("initial_delay_seconds", 1.0)
    embedding_retry.setdefault("backoff_multiplier", 2.0)

    vector = config.setdefault("vector", {})
    vector_retry = vector.setdefault("retry", {})
    vector_retry.setdefault("max_attempts", 3)
    vector_retry.setdefault("initial_delay_seconds", 0.5)
    vector_retry.setdefault("backoff_multiplier", 2.0)

    sync = config.setdefault("sync", {})
    sync.setdefault("mode", "repair_and_drift_detection")
    sync.setdefault("cleanup_orphan_vectors", True)
    sync.setdefault("max_records_per_run", 0)

    compensation = config.setdefault("compensation", {})
    compensation.setdefault("include_failed", True)
    compensation.setdefault("include_pending_delete", True)
    compensation.setdefault("batch_size", 100)

    deletion = config.setdefault("deletion", {})
    deletion.setdefault("strategy", "soft_delete_only")
    deletion.setdefault("unmount_from_wiki", False)
    deletion.setdefault("archive_parent_node_token", "")

    dashboard = config.setdefault("dashboard", {})
    dashboard.setdefault("enabled", False)
    dashboard.setdefault("host", "0.0.0.0")
    dashboard.setdefault("port", 8080)

    mcp_config = config.setdefault("mcp", {})
    mcp_config.setdefault("transport", "stdio")
    mcp_config.setdefault("host", "0.0.0.0")
    mcp_config.setdefault("port", 8001)
    mcp_config.setdefault("http_path", "/mcp")
    mcp_config.setdefault("sse_path", "/mcp/sse")
    mcp_config.setdefault("message_path", "/mcp/messages")
    mcp_config.setdefault("public_base_url", "")

    logging_config = config.setdefault("logging", {})
    logging_config.setdefault("level", "INFO")
    logging_config.setdefault("file_enabled", False)
    logging_config.setdefault("directory", "logs")
    logging_config.setdefault("filename", "server.log")

    extraction = config.setdefault("extraction", {})
    extraction.setdefault("enabled", True)
    extraction.setdefault("max_candidates", 5)
    extraction.setdefault("min_score", 3)
    extraction.setdefault("min_segment_length", 120)
    extraction.setdefault("max_excerpt_length", 320)
    extraction.setdefault("include_full_text_fallback", True)

    automation = config.setdefault("automation", {})
    automation.setdefault("enabled", True)
    automation.setdefault("auto_retrieve_enabled", True)
    automation.setdefault("auto_extract_enabled", True)
    automation.setdefault("auto_save_enabled", True)
    automation.setdefault("retrieval_top_k", 5)
    automation.setdefault("high_confidence_score", 10)
    automation.setdefault("medium_confidence_score", 6)
    automation.setdefault("max_auto_save_items", 3)
    automation.setdefault("max_review_queue_items", 5)

    governance = config.setdefault("governance", {})
    governance.setdefault("enabled", True)
    governance.setdefault("exact_title_merge_enabled", True)
    governance.setdefault("semantic_merge_enabled", True)
    governance.setdefault("semantic_merge_score_threshold", 0.9)
    governance.setdefault("max_related_skills", 3)

    remote_service = config.setdefault("remote_service", {})
    remote_service.setdefault("auth_enabled", False)
    remote_service.setdefault("auth_tokens", [])
    remote_service.setdefault("rate_limit_per_minute", 120)
    remote_service.setdefault("request_timeout_seconds", 30.0)
    remote_service.setdefault("max_concurrency", 20)
    remote_service.setdefault("trust_forwarded_ip", False)


def _override_from_env(config: dict):
    """
    用环境变量覆盖配置项
    映射关系：
        APP_ENV                      → runtime.environment
        APP_DATA_DIR                 → runtime.data_dir
        FEISHU_APP_ID                → feishu.app_id
        FEISHU_APP_SECRET            → feishu.app_secret
        FEISHU_WIKI_SPACE_ID         → feishu.wiki_space_id
        OPENAI_API_KEY               → embedding.api_key
        OPENAI_API_BASE              → embedding.api_base
        EMBEDDING_DIMENSIONS         → embedding.dimensions
        QDRANT_URL                   → vector.qdrant.url
        DASHBOARD_HOST               → dashboard.host
        DASHBOARD_DATABASE_URL       → dashboard.database_url
        DASHBOARD_PORT               → dashboard.port
        MCP_TRANSPORT                → mcp.transport
        MCP_HOST                     → mcp.host
        MCP_PORT                     → mcp.port
        MCP_HTTP_PATH                → mcp.http_path
        MCP_SSE_PATH                 → mcp.sse_path
        MCP_MESSAGE_PATH             → mcp.message_path
        MCP_PUBLIC_BASE_URL          → mcp.public_base_url
        SYNC_MODE                    → sync.mode
        SYNC_MAX_RECORDS_PER_RUN     → sync.max_records_per_run
        SYNC_CLEANUP_ORPHANS         → sync.cleanup_orphan_vectors
        COMPENSATION_BATCH_SIZE      → compensation.batch_size
        COMPENSATION_INCLUDE_FAILED  → compensation.include_failed
        COMPENSATION_INCLUDE_PENDING_DELETE → compensation.include_pending_delete
        DELETION_STRATEGY            → deletion.strategy
        DELETION_UNMOUNT_FROM_WIKI   → deletion.unmount_from_wiki
        DELETION_ARCHIVE_PARENT_NODE → deletion.archive_parent_node_token
        LOG_LEVEL                    → logging.level
        LOG_FILE_ENABLED             → logging.file_enabled
        LOG_DIR                      → logging.directory
        LOG_FILENAME                 → logging.filename
        EXTRACTION_ENABLED           → extraction.enabled
        EXTRACTION_MAX_CANDIDATES    → extraction.max_candidates
        EXTRACTION_MIN_SCORE         → extraction.min_score
        EXTRACTION_MIN_SEGMENT_LENGTH → extraction.min_segment_length
        EXTRACTION_MAX_EXCERPT_LENGTH → extraction.max_excerpt_length
        EXTRACTION_INCLUDE_FALLBACK  → extraction.include_full_text_fallback
        AUTOMATION_ENABLED           → automation.enabled
        AUTOMATION_AUTO_RETRIEVE_ENABLED → automation.auto_retrieve_enabled
        AUTOMATION_AUTO_EXTRACT_ENABLED  → automation.auto_extract_enabled
        AUTOMATION_AUTO_SAVE_ENABLED     → automation.auto_save_enabled
        AUTOMATION_RETRIEVAL_TOP_K       → automation.retrieval_top_k
        AUTOMATION_HIGH_CONFIDENCE_SCORE → automation.high_confidence_score
        AUTOMATION_MEDIUM_CONFIDENCE_SCORE → automation.medium_confidence_score
        AUTOMATION_MAX_AUTO_SAVE_ITEMS   → automation.max_auto_save_items
        AUTOMATION_MAX_REVIEW_QUEUE_ITEMS → automation.max_review_queue_items
        GOVERNANCE_ENABLED               → governance.enabled
        GOVERNANCE_EXACT_TITLE_MERGE_ENABLED → governance.exact_title_merge_enabled
        GOVERNANCE_SEMANTIC_MERGE_ENABLED → governance.semantic_merge_enabled
        GOVERNANCE_SEMANTIC_MERGE_SCORE_THRESHOLD → governance.semantic_merge_score_threshold
        GOVERNANCE_MAX_RELATED_SKILLS    → governance.max_related_skills
        MCP_AUTH_ENABLED                 → remote_service.auth_enabled
        MCP_AUTH_TOKENS                  → remote_service.auth_tokens
        MCP_RATE_LIMIT_PER_MINUTE        → remote_service.rate_limit_per_minute
        MCP_REQUEST_TIMEOUT_SECONDS      → remote_service.request_timeout_seconds
        MCP_MAX_CONCURRENCY              → remote_service.max_concurrency
        MCP_TRUST_FORWARDED_IP           → remote_service.trust_forwarded_ip

    """
    env_mapping: dict[str, tuple[list[str], Callable[[str], Any]]] = {
        "APP_ENV": (["runtime", "environment"], str),
        "APP_DATA_DIR": (["runtime", "data_dir"], str),
        "FEISHU_APP_ID": (["feishu", "app_id"], str),
        "FEISHU_APP_SECRET": (["feishu", "app_secret"], str),
        "FEISHU_WIKI_SPACE_ID": (["feishu", "wiki_space_id"], str),
        "FEISHU_CATEGORY_NODES": (["feishu", "category_nodes"], _parse_category_nodes),
        "OPENAI_API_KEY": (["embedding", "api_key"], str),
        "OPENAI_API_BASE": (["embedding", "api_base"], str),
        "EMBEDDING_MODEL": (["embedding", "model"], str),
        "EMBEDDING_DIMENSIONS": (["embedding", "dimensions"], int),
        "QDRANT_URL": (["vector", "qdrant", "url"], str),
        "QDRANT_API_KEY": (["vector", "qdrant", "api_key"], str),
        "QDRANT_COLLECTION_NAME": (["vector", "qdrant", "collection_name"], str),
        "DASHBOARD_ENABLED": (["dashboard", "enabled"], _parse_bool),
        "DASHBOARD_HOST": (["dashboard", "host"], str),
        "DASHBOARD_DATABASE_URL": (["dashboard", "database_url"], str),
        "DASHBOARD_PORT": (["dashboard", "port"], int),
        "MCP_TRANSPORT": (["mcp", "transport"], str),
        "MCP_HOST": (["mcp", "host"], str),
        "MCP_PORT": (["mcp", "port"], int),
        "MCP_HTTP_PATH": (["mcp", "http_path"], str),
        "MCP_SSE_PATH": (["mcp", "sse_path"], str),
        "MCP_MESSAGE_PATH": (["mcp", "message_path"], str),
        "MCP_PUBLIC_BASE_URL": (["mcp", "public_base_url"], str),
        "SYNC_MODE": (["sync", "mode"], str),
        "SYNC_MAX_RECORDS_PER_RUN": (["sync", "max_records_per_run"], int),
        "SYNC_CLEANUP_ORPHANS": (["sync", "cleanup_orphan_vectors"], _parse_bool),
        "COMPENSATION_BATCH_SIZE": (["compensation", "batch_size"], int),
        "COMPENSATION_INCLUDE_FAILED": (["compensation", "include_failed"], _parse_bool),
        "COMPENSATION_INCLUDE_PENDING_DELETE": (["compensation", "include_pending_delete"], _parse_bool),
        "DELETION_STRATEGY": (["deletion", "strategy"], str),
        "DELETION_UNMOUNT_FROM_WIKI": (["deletion", "unmount_from_wiki"], _parse_bool),
        "DELETION_ARCHIVE_PARENT_NODE": (["deletion", "archive_parent_node_token"], str),
        "LOG_LEVEL": (["logging", "level"], str),
        "LOG_FILE_ENABLED": (["logging", "file_enabled"], _parse_bool),
        "LOG_DIR": (["logging", "directory"], str),
        "LOG_FILENAME": (["logging", "filename"], str),
        "EXTRACTION_ENABLED": (["extraction", "enabled"], _parse_bool),
        "EXTRACTION_MAX_CANDIDATES": (["extraction", "max_candidates"], int),
        "EXTRACTION_MIN_SCORE": (["extraction", "min_score"], int),
        "EXTRACTION_MIN_SEGMENT_LENGTH": (["extraction", "min_segment_length"], int),
        "EXTRACTION_MAX_EXCERPT_LENGTH": (["extraction", "max_excerpt_length"], int),
        "EXTRACTION_INCLUDE_FALLBACK": (["extraction", "include_full_text_fallback"], _parse_bool),
        "AUTOMATION_ENABLED": (["automation", "enabled"], _parse_bool),
        "AUTOMATION_AUTO_RETRIEVE_ENABLED": (["automation", "auto_retrieve_enabled"], _parse_bool),
        "AUTOMATION_AUTO_EXTRACT_ENABLED": (["automation", "auto_extract_enabled"], _parse_bool),
        "AUTOMATION_AUTO_SAVE_ENABLED": (["automation", "auto_save_enabled"], _parse_bool),
        "AUTOMATION_RETRIEVAL_TOP_K": (["automation", "retrieval_top_k"], int),
        "AUTOMATION_HIGH_CONFIDENCE_SCORE": (["automation", "high_confidence_score"], int),
        "AUTOMATION_MEDIUM_CONFIDENCE_SCORE": (["automation", "medium_confidence_score"], int),
        "AUTOMATION_MAX_AUTO_SAVE_ITEMS": (["automation", "max_auto_save_items"], int),
        "AUTOMATION_MAX_REVIEW_QUEUE_ITEMS": (["automation", "max_review_queue_items"], int),
        "GOVERNANCE_ENABLED": (["governance", "enabled"], _parse_bool),
        "GOVERNANCE_EXACT_TITLE_MERGE_ENABLED": (["governance", "exact_title_merge_enabled"], _parse_bool),
        "GOVERNANCE_SEMANTIC_MERGE_ENABLED": (["governance", "semantic_merge_enabled"], _parse_bool),
        "GOVERNANCE_SEMANTIC_MERGE_SCORE_THRESHOLD": (["governance", "semantic_merge_score_threshold"], float),
        "GOVERNANCE_MAX_RELATED_SKILLS": (["governance", "max_related_skills"], int),
        "MCP_AUTH_ENABLED": (["remote_service", "auth_enabled"], _parse_bool),
        "MCP_AUTH_TOKENS": (["remote_service", "auth_tokens"], _parse_csv_list),
        "MCP_RATE_LIMIT_PER_MINUTE": (["remote_service", "rate_limit_per_minute"], int),
        "MCP_REQUEST_TIMEOUT_SECONDS": (["remote_service", "request_timeout_seconds"], float),
        "MCP_MAX_CONCURRENCY": (["remote_service", "max_concurrency"], int),
        "MCP_TRUST_FORWARDED_IP": (["remote_service", "trust_forwarded_ip"], _parse_bool),
    }

    for env_var, (keys, type_fn) in env_mapping.items():
        value = os.environ.get(env_var)
        if value is not None:
            _deep_set(config, keys, type_fn(value))


def _validate(config: dict):
    """
    校验必填配置项
    如果缺少必填项，抛出 ValueError 并给出明确提示
    """
    errors = []

    feishu = config.get("feishu", {})
    if not feishu.get("app_id") or feishu["app_id"] == "your_feishu_app_id":
        errors.append(
            "feishu.app_id 未配置。请在 config.yaml 中填写飞书 App ID，"
            "或设置环境变量 FEISHU_APP_ID"
        )
    if not feishu.get("app_secret") or feishu["app_secret"] == "your_feishu_app_secret":
        errors.append(
            "feishu.app_secret 未配置。请在 config.yaml 中填写飞书 App Secret，"
            "或设置环境变量 FEISHU_APP_SECRET"
        )

    wiki_space_id = feishu.get("wiki_space_id")
    if not wiki_space_id or wiki_space_id == "your_wiki_space_id":
        errors.append(
            "feishu.wiki_space_id 未配置。请在 config.yaml 中填写飞书知识空间 ID，"
            "或设置环境变量 FEISHU_WIKI_SPACE_ID"
        )

    category_nodes = feishu.get("category_nodes") or {}
    if not isinstance(category_nodes, dict) or not any(str(value).strip() for value in category_nodes.values()):
        errors.append(
            "feishu.category_nodes 未配置。请至少为一个知识分类填写对应的 wiki 节点 token。"
        )

    embedding = config.get("embedding", {})
    if not embedding.get("api_key") or embedding["api_key"] == "your_openai_api_key":
        errors.append(
            "embedding.api_key 未配置。请在 config.yaml 中填写 OpenAI API Key，"
            "或设置环境变量 OPENAI_API_KEY"
        )

    dimensions = embedding.get("dimensions", 1536)
    try:
        dimensions = int(dimensions)
        if dimensions <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("embedding.dimensions 必须是大于 0 的整数。")

    for section_name in ("feishu", "embedding", "vector"):
        retry_config = config.get(section_name, {}).get("retry", {})
        try:
            max_attempts = int(retry_config.get("max_attempts", 3))
            if max_attempts <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{section_name}.retry.max_attempts 必须是大于 0 的整数。")

        try:
            initial_delay = float(retry_config.get("initial_delay_seconds", 1.0))
            if initial_delay < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{section_name}.retry.initial_delay_seconds 必须是大于等于 0 的数字。")

        try:
            backoff_multiplier = float(retry_config.get("backoff_multiplier", 2.0))
            if backoff_multiplier < 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{section_name}.retry.backoff_multiplier 必须是大于等于 1 的数字。")

    sync_config = config.get("sync", {})

    if sync_config.get("mode") not in {"repair_only", "repair_and_drift_detection"}:
        errors.append("sync.mode 仅支持 repair_only 或 repair_and_drift_detection。")

    try:
        max_records_per_run = int(sync_config.get("max_records_per_run", 0))
        if max_records_per_run < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("sync.max_records_per_run 必须是大于等于 0 的整数。")

    compensation = config.get("compensation", {})
    try:
        batch_size = int(compensation.get("batch_size", 100))
        if batch_size <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("compensation.batch_size 必须是大于 0 的整数。")

    deletion = config.get("deletion", {})
    if deletion.get("strategy") not in {"soft_delete_only", "soft_delete_and_unmount", "hard_delete"}:
        errors.append(
            "deletion.strategy 仅支持 soft_delete_only、soft_delete_and_unmount 或 hard_delete。"
        )

    dashboard = config.get("dashboard", {})
    if not str(dashboard.get("database_url") or "").strip():
        errors.append(
            "dashboard.database_url 未配置。注册表已成为运行时必需依赖，请在 config.yaml 中填写数据库连接串，或设置环境变量 DASHBOARD_DATABASE_URL"
        )

    if not str(dashboard.get("host") or "").strip():
        errors.append("dashboard.host 未配置。请填写 Dashboard 监听地址，例如 0.0.0.0。")

    try:
        dashboard_port = int(dashboard.get("port", 8080))
        if dashboard_port <= 0 or dashboard_port > 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("dashboard.port 必须是 1 到 65535 之间的整数。")

    mcp_config = config.get("mcp", {})
    transport = str(mcp_config.get("transport") or "stdio").strip().lower()
    if transport not in {"stdio", "sse", "streamable_http"}:
        errors.append("mcp.transport 仅支持 stdio、sse 或 streamable_http。")

    if not str(mcp_config.get("host") or "").strip():
        errors.append("mcp.host 未配置。请填写远程 MCP 服务监听地址，例如 0.0.0.0。")

    try:
        mcp_port = int(mcp_config.get("port", 8001))
        if mcp_port <= 0 or mcp_port > 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("mcp.port 必须是 1 到 65535 之间的整数。")

    for key, field_name in (
        ("http_path", "mcp.http_path"),
        ("sse_path", "mcp.sse_path"),
        ("message_path", "mcp.message_path"),
    ):
        path_value = str(mcp_config.get(key) or "").strip()
        if not path_value:
            errors.append(f"{field_name} 未配置。")
        elif not path_value.startswith("/"):
            errors.append(f"{field_name} 必须以 / 开头。")

    public_base_url = str(mcp_config.get("public_base_url") or "").strip()
    if public_base_url and not public_base_url.startswith(("http://", "https://")):
        errors.append("mcp.public_base_url 必须以 http:// 或 https:// 开头。")

    runtime = config.get("runtime", {})
    if not str(runtime.get("data_dir") or "").strip():
        errors.append("runtime.data_dir 未配置。请指定统一的数据目录。")

    logging_config = config.get("logging", {})
    if not str(logging_config.get("level") or "").strip():
        errors.append("logging.level 未配置。请填写日志级别，例如 INFO。")

    if logging_config.get("file_enabled"):
        if not str(logging_config.get("directory") or "").strip():
            errors.append("logging.directory 未配置。启用文件日志时必须指定日志目录。")
        if not str(logging_config.get("filename") or "").strip():
            errors.append("logging.filename 未配置。启用文件日志时必须指定日志文件名。")

    extraction = config.get("extraction", {})
    try:
        max_candidates = int(extraction.get("max_candidates", 5))
        if max_candidates <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("extraction.max_candidates 必须是大于 0 的整数。")

    try:
        min_score = int(extraction.get("min_score", 3))
        if min_score <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("extraction.min_score 必须是大于 0 的整数。")

    try:
        min_segment_length = int(extraction.get("min_segment_length", 120))
        if min_segment_length <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("extraction.min_segment_length 必须是大于 0 的整数。")

    try:
        max_excerpt_length = int(extraction.get("max_excerpt_length", 320))
        if max_excerpt_length <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("extraction.max_excerpt_length 必须是大于 0 的整数。")

    if not isinstance(extraction.get("include_full_text_fallback", True), bool):
        errors.append("extraction.include_full_text_fallback 必须是布尔值。")

    automation = config.get("automation", {})
    for key in ("enabled", "auto_retrieve_enabled", "auto_extract_enabled", "auto_save_enabled"):
        if not isinstance(automation.get(key, True), bool):
            errors.append(f"automation.{key} 必须是布尔值。")

    try:
        retrieval_top_k = int(automation.get("retrieval_top_k", 5))
        if retrieval_top_k <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("automation.retrieval_top_k 必须是大于 0 的整数。")

    try:
        high_confidence_score = int(automation.get("high_confidence_score", 10))
        if high_confidence_score <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("automation.high_confidence_score 必须是大于 0 的整数。")
        high_confidence_score = 10

    try:
        medium_confidence_score = int(automation.get("medium_confidence_score", 6))
        if medium_confidence_score <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("automation.medium_confidence_score 必须是大于 0 的整数。")
        medium_confidence_score = 6

    if isinstance(high_confidence_score, int) and isinstance(medium_confidence_score, int):
        if medium_confidence_score > high_confidence_score:
            errors.append("automation.medium_confidence_score 不能大于 automation.high_confidence_score。")

    try:
        max_auto_save_items = int(automation.get("max_auto_save_items", 3))
        if max_auto_save_items <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("automation.max_auto_save_items 必须是大于 0 的整数。")

    try:
        max_review_queue_items = int(automation.get("max_review_queue_items", 5))
        if max_review_queue_items <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("automation.max_review_queue_items 必须是大于 0 的整数。")

    governance = config.get("governance", {})
    for key in ("enabled", "exact_title_merge_enabled", "semantic_merge_enabled"):
        if not isinstance(governance.get(key, True), bool):
            errors.append(f"governance.{key} 必须是布尔值。")

    try:
        semantic_merge_score_threshold = float(governance.get("semantic_merge_score_threshold", 0.9))
        if semantic_merge_score_threshold < 0 or semantic_merge_score_threshold > 1:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("governance.semantic_merge_score_threshold 必须是 0 到 1 之间的数字。")

    try:
        max_related_skills = int(governance.get("max_related_skills", 3))
        if max_related_skills <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("governance.max_related_skills 必须是大于 0 的整数。")

    remote_service = config.get("remote_service", {})
    for key in ("auth_enabled", "trust_forwarded_ip"):
        if not isinstance(remote_service.get(key, False), bool):
            errors.append(f"remote_service.{key} 必须是布尔值。")

    auth_tokens = remote_service.get("auth_tokens", [])
    if not isinstance(auth_tokens, list):
        errors.append("remote_service.auth_tokens 必须是字符串列表。")
        auth_tokens = []
    else:
        normalized_tokens = [str(item).strip() for item in auth_tokens if str(item).strip()]
        remote_service["auth_tokens"] = normalized_tokens
        auth_tokens = normalized_tokens

    if remote_service.get("auth_enabled") and not auth_tokens:
        errors.append("remote_service.auth_enabled 为 true 时，必须至少提供一个 remote_service.auth_tokens。")

    try:
        rate_limit_per_minute = int(remote_service.get("rate_limit_per_minute", 120))
        if rate_limit_per_minute <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("remote_service.rate_limit_per_minute 必须是大于 0 的整数。")

    try:
        request_timeout_seconds = float(remote_service.get("request_timeout_seconds", 30.0))
        if request_timeout_seconds <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("remote_service.request_timeout_seconds 必须是大于 0 的数字。")

    try:
        max_concurrency = int(remote_service.get("max_concurrency", 20))
        if max_concurrency <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("remote_service.max_concurrency 必须是大于 0 的整数。")

    if errors:
        error_msg = "配置校验失败：\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)


def load_config(path: str = None) -> dict:
    """
    加载配置。

    加载优先级：
    1. `.env` / 系统环境变量
    2. 指定的 path 参数
    3. 环境变量 FEISHU_MCP_CONFIG 指向的路径
    4. 当前目录下的 config.yaml
    5. ~/.feishu-knowledge/config.yaml

    当找不到配置文件时，会仅基于环境变量与内置默认值构建配置。

    Args:
        path: 配置文件路径（可选）

    Returns:
        配置字典

    Raises:
        FileNotFoundError: 显式指定的配置文件不存在
        ValueError: 必填配置项缺失
    """
    project_env_path = Path(__file__).parent / ".env"
    load_dotenv(project_env_path, override=False)
    load_dotenv(override=False)

    if path is None:
        path = os.environ.get("FEISHU_MCP_CONFIG")

    if path is None:
        local_config = Path(__file__).parent / "config.yaml"
        if local_config.exists():
            path = str(local_config)

    if path is None:
        home_config = Path.home() / ".feishu-knowledge" / "config.yaml"
        if home_config.exists():
            path = str(home_config)

    config = _build_base_config()

    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")

        with open(config_path, "r", encoding="utf-8") as f:
            file_config = yaml.safe_load(f) or {}

        _deep_merge(config, file_config)

    _override_from_env(config)
    _apply_defaults(config)
    _expand_paths(config)

    runtime_data_dir = str(config.get("runtime", {}).get("data_dir") or "").strip()
    if runtime_data_dir:
        Path(runtime_data_dir).mkdir(parents=True, exist_ok=True)

    sqlite_db_path = _extract_sqlite_db_path(
        config.get("dashboard", {}).get("database_url")
    )

    if sqlite_db_path:
        Path(sqlite_db_path).parent.mkdir(parents=True, exist_ok=True)

    logging_config = config.get("logging", {})
    if logging_config.get("file_enabled"):
        log_directory = str(logging_config.get("directory") or "").strip()
        if log_directory:
            Path(log_directory).mkdir(parents=True, exist_ok=True)

    _validate(config)
    return config
