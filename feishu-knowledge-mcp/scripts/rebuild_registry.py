#!/usr/bin/env python3
"""
重建知识注册表脚本

基于现有向量库 payload 补齐缺失的知识注册表记录。
仅用于历史治理或人工迁移后的补登记，不作为常规主流程。
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_config
from dashboard.logger import DashboardLogger
from dashboard.registry import SkillRegistryStore
from knowledge.card import SYNC_STATUS_INDEXED
from vector.store import VectorStore


async def main():
    config = load_config()
    dashboard_database_url = config.get("dashboard", {}).get("database_url")
    if not dashboard_database_url:
        raise RuntimeError("未配置 dashboard.database_url，无法执行注册表重建。")

    dashboard_logger = DashboardLogger(dashboard_database_url)
    await dashboard_logger.init_db()
    registry_store = SkillRegistryStore(dashboard_logger)

    vector_dimensions = int(config.get("embedding", {}).get("dimensions", 1536))
    vector_store = VectorStore(config["vector"], dimensions=vector_dimensions)
    points = await vector_store.list_all(include_deleted=True, limit=1000)

    rebuilt = 0
    skipped = 0
    now_iso = datetime.utcnow().isoformat()

    for point in points:
        metadata = point.get("metadata", {})
        skill_id = metadata.get("skill_id") or point.get("skill_id")
        if not skill_id:
            skipped += 1
            continue

        existing = await registry_store.get(skill_id)
        if existing:
            skipped += 1
            continue

        record = {
            "skill_id": skill_id,
            "title": metadata.get("title", skill_id),
            "category": metadata.get("category", "最佳实践"),
            "project": metadata.get("project", ""),
            "tags": metadata.get("tags", []),
            "feishu_doc_url": metadata.get("feishu_doc_url", ""),
            "feishu_doc_token": metadata.get("feishu_doc_token", ""),
            "wiki_node_token": metadata.get("wiki_node_token", ""),
            "content_hash": metadata.get("content_hash", ""),
            "version": int(metadata.get("version", 1) or 1),
            "sync_status": metadata.get("sync_status", SYNC_STATUS_INDEXED) or SYNC_STATUS_INDEXED,
            "deleted": bool(metadata.get("deleted", False)),
            "source": metadata.get("source", "ai_conversation"),
            "last_error": None,
            "created_at": metadata.get("created_at") or now_iso,
            "updated_at": metadata.get("updated_at") or now_iso,
        }
        await registry_store.upsert(record)
        rebuilt += 1

    print({"rebuilt": rebuilt, "skipped": skipped, "total_points": len(points)})


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    asyncio.run(main())
