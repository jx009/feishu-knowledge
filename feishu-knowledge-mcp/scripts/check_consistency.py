#!/usr/bin/env python3
"""
一致性检查脚本

检查注册表、向量索引和异常状态数量，输出可读报告。
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_config
from dashboard.logger import DashboardLogger
from dashboard.registry import SkillRegistryStore
from feishu.document import FeishuDocManager
from vector.embedder import Embedder
from vector.store import VectorStore
from vector.sync import SyncManager


async def main():
    config = load_config()
    dashboard_database_url = config.get("dashboard", {}).get("database_url")
    if not dashboard_database_url:
        raise RuntimeError("未配置 dashboard.database_url，无法执行一致性检查。")

    dashboard_logger = DashboardLogger(dashboard_database_url)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    embedder = Embedder(config["embedding"])
    vector_dimensions = int(config.get("embedding", {}).get("dimensions", 1536))
    vector_store = VectorStore(config["vector"], dimensions=vector_dimensions)
    feishu_doc_manager = FeishuDocManager(config)

    sync_manager = SyncManager(
        config=config,
        embedder=embedder,
        vector_store=vector_store,
        feishu_doc_manager=feishu_doc_manager,
        registry_store=registry_store,
        dashboard_logger=dashboard_logger,
    )
    summary = await sync_manager.check_status()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
