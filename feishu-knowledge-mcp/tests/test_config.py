from pathlib import Path

import pytest

from config import load_config


VALID_CONFIG_TEMPLATE = """
runtime:
  environment: "development"
  data_dir: "{data_dir}"
feishu:
  app_id: "app_id"
  app_secret: "app_secret"
  wiki_space_id: "space_id"
  category_nodes:
    最佳实践: "node_1"
vector:
  provider: "qdrant_self_hosted"
  qdrant:
    url: "http://localhost:6333"
embedding:
  api_key: "openai_key"
  api_base: "https://api.openai.com/v1"
  model: "text-embedding-3-small"
  dimensions: 1536
dashboard:
  enabled: true
  host: "0.0.0.0"
  port: 8080
  database_url: "{database_url}"
logging:
  file_enabled: true
  directory: "logs"
"""


def _write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def _build_valid_config(tmp_path: Path) -> str:
    return VALID_CONFIG_TEMPLATE.format(
        data_dir=tmp_path / "runtime-data",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test-dashboard.db'}"
    )


def test_load_config_accepts_valid_required_fields(tmp_path):
    config_path = _write_config(tmp_path, _build_valid_config(tmp_path))

    config = load_config(str(config_path))

    assert config["runtime"]["environment"] == "development"
    assert config["runtime"]["data_dir"].endswith("runtime-data")
    assert Path(config["runtime"]["data_dir"]).exists()
    assert config["feishu"]["wiki_space_id"] == "space_id"
    assert config["feishu"]["category_nodes"]["最佳实践"] == "node_1"
    assert config["embedding"]["dimensions"] == 1536
    assert config["dashboard"]["host"] == "0.0.0.0"
    assert config["dashboard"]["database_url"].endswith("test-dashboard.db")
    assert config["mcp"]["transport"] == "stdio"
    assert config["mcp"]["host"] == "0.0.0.0"
    assert config["mcp"]["port"] == 8001
    assert config["mcp"]["sse_path"] == "/mcp/sse"
    assert config["mcp"]["message_path"] == "/mcp/messages"
    assert config["logging"]["level"] == "INFO"
    assert config["logging"]["file_enabled"] is True
    assert config["logging"]["directory"].endswith("runtime-data/logs")
    assert config["logging"]["filename"] == "server.log"


def test_load_config_supports_env_only(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join([
            "APP_ENV=production",
            f"APP_DATA_DIR={tmp_path / 'env-runtime'}",
            "FEISHU_APP_ID=app_id",
            "FEISHU_APP_SECRET=app_secret",
            "FEISHU_WIKI_SPACE_ID=space_id",
            'FEISHU_CATEGORY_NODES={"最佳实践":"node_1"}',
            "OPENAI_API_KEY=openai_key",
            "QDRANT_URL=http://qdrant:6333",
            "DASHBOARD_ENABLED=true",
            "DASHBOARD_HOST=127.0.0.1",
            "DASHBOARD_DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/feishu_knowledge",
            "MCP_TRANSPORT=sse",
            "MCP_HOST=0.0.0.0",
            "MCP_PORT=9001",
            "MCP_SSE_PATH=/remote/sse",
            "MCP_MESSAGE_PATH=/remote/messages",
            "MCP_PUBLIC_BASE_URL=https://mcp.example.com",
            "GOVERNANCE_ENABLED=true",
            "GOVERNANCE_EXACT_TITLE_MERGE_ENABLED=true",
            "GOVERNANCE_SEMANTIC_MERGE_ENABLED=true",
            "GOVERNANCE_SEMANTIC_MERGE_SCORE_THRESHOLD=0.93",
            "GOVERNANCE_MAX_RELATED_SKILLS=5",
            "MCP_AUTH_ENABLED=true",
            "MCP_AUTH_TOKENS=token-a,token-b",
            "MCP_RATE_LIMIT_PER_MINUTE=240",
            "MCP_REQUEST_TIMEOUT_SECONDS=45",
            "MCP_MAX_CONCURRENCY=32",
            "MCP_TRUST_FORWARDED_IP=true",
            "LOG_LEVEL=DEBUG",
            "LOG_FILE_ENABLED=true",
            "LOG_DIR=runtime-logs",
            "LOG_FILENAME=runtime.log",
        ]),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FEISHU_MCP_CONFIG", raising=False)

    config = load_config()

    assert config["runtime"]["environment"] == "production"
    assert config["runtime"]["data_dir"].endswith("env-runtime")
    assert Path(config["runtime"]["data_dir"]).exists()
    assert config["feishu"]["app_id"] == "app_id"
    assert config["feishu"]["category_nodes"]["最佳实践"] == "node_1"
    assert config["vector"]["qdrant"]["url"] == "http://qdrant:6333"
    assert config["dashboard"]["enabled"] is True
    assert config["dashboard"]["host"] == "127.0.0.1"
    assert config["dashboard"]["database_url"].startswith("postgresql+asyncpg://")
    assert config["mcp"]["transport"] == "sse"
    assert config["mcp"]["host"] == "0.0.0.0"
    assert config["mcp"]["port"] == 9001
    assert config["mcp"]["sse_path"] == "/remote/sse"
    assert config["mcp"]["message_path"] == "/remote/messages"
    assert config["mcp"]["public_base_url"] == "https://mcp.example.com"
    assert config["governance"]["enabled"] is True
    assert config["governance"]["exact_title_merge_enabled"] is True
    assert config["governance"]["semantic_merge_enabled"] is True
    assert config["governance"]["semantic_merge_score_threshold"] == 0.93
    assert config["governance"]["max_related_skills"] == 5
    assert config["remote_service"]["auth_enabled"] is True
    assert config["remote_service"]["auth_tokens"] == ["token-a", "token-b"]
    assert config["remote_service"]["rate_limit_per_minute"] == 240
    assert config["remote_service"]["request_timeout_seconds"] == 45.0
    assert config["remote_service"]["max_concurrency"] == 32
    assert config["remote_service"]["trust_forwarded_ip"] is True
    assert config["logging"]["level"] == "DEBUG"
    assert config["logging"]["file_enabled"] is True
    assert config["logging"]["directory"].endswith("env-runtime/runtime-logs")
    assert config["logging"]["filename"] == "runtime.log"
    assert (tmp_path / "env-runtime" / "runtime-logs").exists()


def test_load_config_requires_dashboard_database_url(tmp_path):
    config_content = _build_valid_config(tmp_path).replace(
        f'sqlite+aiosqlite:///{tmp_path / "test-dashboard.db"}',
        "",
    )
    config_path = _write_config(tmp_path, config_content)

    with pytest.raises(ValueError) as exc_info:
        load_config(str(config_path))

    assert "dashboard.database_url 未配置" in str(exc_info.value)


@pytest.mark.parametrize(
    ("old_text", "new_text", "expected_message"),
    [
        ('wiki_space_id: "space_id"', 'wiki_space_id: "your_wiki_space_id"', "feishu.wiki_space_id 未配置"),
        ('最佳实践: "node_1"', '最佳实践: ""', "feishu.category_nodes 未配置"),
        ('dimensions: 1536', 'dimensions: 0', "embedding.dimensions 必须是大于 0 的整数"),
    ],
)
def test_load_config_rejects_invalid_required_fields(tmp_path, old_text, new_text, expected_message):
    config_content = _build_valid_config(tmp_path).replace(old_text, new_text)
    config_path = _write_config(tmp_path, config_content)

    with pytest.raises(ValueError) as exc_info:
        load_config(str(config_path))

    assert expected_message in str(exc_info.value)


def test_load_config_resolves_relative_sqlite_path_under_runtime_data_dir(tmp_path):
    config_content = _build_valid_config(tmp_path).replace(
        f'sqlite+aiosqlite:///{tmp_path / "test-dashboard.db"}',
        'sqlite+aiosqlite:///dashboard/dashboard.db',
    )
    config_path = _write_config(tmp_path, config_content)

    config = load_config(str(config_path))

    assert config["dashboard"]["database_url"].endswith("runtime-data/dashboard/dashboard.db")
    assert (tmp_path / "runtime-data" / "dashboard").exists()


@pytest.mark.parametrize(
    ("old_text", "new_text", "expected_message"),
    [
        ('file_enabled: true', 'file_enabled: true\nmcp:\n  transport: "http"', "mcp.transport 仅支持 stdio 或 sse"),
        ('file_enabled: true', 'file_enabled: true\nmcp:\n  sse_path: "mcp-sse"', "mcp.sse_path 必须以 / 开头"),
        ('file_enabled: true', 'file_enabled: true\nmcp:\n  public_base_url: "mcp.example.com"', "mcp.public_base_url 必须以 http:// 或 https:// 开头"),
    ],
)
def test_load_config_rejects_invalid_mcp_fields(tmp_path, old_text, new_text, expected_message):
    config_content = _build_valid_config(tmp_path).replace(old_text, new_text)
    config_path = _write_config(tmp_path, config_content)

    with pytest.raises(ValueError) as exc_info:
        load_config(str(config_path))

    assert expected_message in str(exc_info.value)


@pytest.mark.parametrize(
    ("suffix", "expected_message"),
    [
        ('\ngovernance:\n  semantic_merge_score_threshold: 1.2\n', "governance.semantic_merge_score_threshold 必须是 0 到 1 之间的数字"),
        ('\nremote_service:\n  auth_enabled: true\n', "remote_service.auth_enabled 为 true 时，必须至少提供一个 remote_service.auth_tokens"),
        ('\nremote_service:\n  max_concurrency: 0\n', "remote_service.max_concurrency 必须是大于 0 的整数"),
    ],
)
def test_load_config_rejects_invalid_governance_or_remote_service_fields(tmp_path, suffix, expected_message):
    config_content = _build_valid_config(tmp_path) + suffix
    config_path = _write_config(tmp_path, config_content)

    with pytest.raises(ValueError) as exc_info:
        load_config(str(config_path))

    assert expected_message in str(exc_info.value)