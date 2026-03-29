"""
MCP 工具 —— save_skill（知识沉淀）

将有价值的开发经验、架构方案、避坑记录、最佳实践等知识沉淀到飞书知识库。
飞书文档是唯一事实源，向量数据库只是派生索引。
"""

import logging
from typing import Any, List

from mcp.types import TextContent

from knowledge.card import (
    SkillCard,
    SYNC_STATUS_CREATED_FEISHU,
    SYNC_STATUS_INDEXED,
    SYNC_STATUS_PENDING_INDEX,
    parse_tags,
)

logger = logging.getLogger(__name__)


def _trim_error(message: Exception | str, limit: int = 200) -> str:
    return str(message)[:limit]


def _build_partial_success_text(card: SkillCard, error: Exception | str) -> str:
    return (
        f"⚠️ 知识已写入飞书，但索引写入失败，后续可通过同步补偿。\n\n"
        f"🆔 **技能ID**：{card.skill_id}\n"
        f"📌 **标题**：{card.title}\n"
        f"📂 **分类**：{card.category}\n"
        f"🔗 **飞书链接**：{card.feishu_doc_url}\n"
        f"📡 **当前状态**：{card.sync_status}\n"
        f"❗ **错误信息**：{_trim_error(error, 300)}"
    )


def _build_success_text(card: SkillCard) -> str:
    result_text = (
        f"✅ 知识已成功沉淀！\n\n"
        f"🆔 **技能ID**：{card.skill_id}\n"
        f"📌 **标题**：{card.title}\n"
        f"📂 **分类**：{card.category}\n"
    )
    if card.project:
        result_text += f"🏷️ **项目**：{card.project}\n"
    if card.tags:
        result_text += f"🔖 **标签**：{', '.join(card.tags)}\n"
    result_text += f"🔗 **飞书链接**：{card.feishu_doc_url}\n"
    result_text += f"📡 **同步状态**：{card.sync_status}"
    return result_text


async def persist_skill_card(
    *,
    title: str,
    content: str,
    category: str,
    project: str = "",
    tags: List[str] | str | None = None,
    source: str = "ai_conversation",
    config: dict,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store,
    dashboard_logger=None,
) -> dict[str, Any]:
    if registry_store is None:
        raise RuntimeError("知识注册表未初始化，已禁止保存操作，请先启用 Dashboard 数据库。")

    tag_list: List[str] = parse_tags(tags)
    card = SkillCard(
        title=title,
        content=content,
        category=category,
        project=project,
        tags=tag_list,
        source=source,
    )
    logger.info("知识卡片构建完成: %s | %s | source=%s", card.skill_id, card.full_title, source)

    parent_node = config["feishu"].get("category_nodes", {}).get(category, "")

    create_result = await feishu_doc_manager.create_document(
        space_id=config["feishu"].get("wiki_space_id", ""),
        parent_node=parent_node,
        title=card.full_title,
        content=card.to_markdown(),
    )
    card.feishu_doc_url = create_result.get("doc_url") or ""
    card.feishu_doc_token = create_result.get("feishu_doc_token") or ""
    card.wiki_node_token = create_result.get("wiki_node_token") or ""
    card.sync_status = SYNC_STATUS_CREATED_FEISHU

    await registry_store.upsert(card.to_registry_dict())
    logger.info("知识注册表写入成功: %s | 状态: %s", card.skill_id, card.sync_status)

    try:
        embedding = embedder.encode(card.searchable_text)
        await vector_store.upsert(
            id=card.skill_id,
            embedding=embedding,
            metadata=card.to_metadata(),
            document=card.content,
        )
    except Exception as e:
        card.sync_status = SYNC_STATUS_PENDING_INDEX
        card.last_error = _trim_error(e)
        await registry_store.upsert(card.to_registry_dict())
        logger.error("向量库写入失败，知识待补偿: %s | 错误: %s", card.skill_id, e)

        if dashboard_logger:
            try:
                await dashboard_logger.log_save(
                    skill_id=card.skill_id,
                    title=card.title,
                    category=card.category,
                    project=card.project,
                    tags=card.tags,
                    content=card.content,
                    feishu_folder=card.category,
                    feishu_url=card.feishu_doc_url or "",
                    feishu_doc_token=card.feishu_doc_token or "",
                    wiki_node_token=card.wiki_node_token or "",
                    sync_status=card.sync_status,
                    status="partial",
                    error=str(e),
                )
            except Exception as log_error:
                logger.warning("操作日志记录失败（不影响主流程）: %s", log_error)

        return {
            "status": "partial",
            "card": card,
            "error": str(e),
            "text": _build_partial_success_text(card, e),
        }

    card.sync_status = SYNC_STATUS_INDEXED
    card.last_error = None
    await registry_store.upsert(card.to_registry_dict())
    logger.info("知识已完成索引: %s", card.skill_id)

    if dashboard_logger:
        try:
            await dashboard_logger.log_save(
                skill_id=card.skill_id,
                title=card.title,
                category=card.category,
                project=card.project,
                tags=card.tags,
                content=card.content,
                feishu_folder=card.category,
                feishu_url=card.feishu_doc_url or "",
                feishu_doc_token=card.feishu_doc_token or "",
                wiki_node_token=card.wiki_node_token or "",
                sync_status=card.sync_status,
            )
        except Exception as e:
            logger.warning("操作日志记录失败（不影响主流程）: %s", e)

    return {
        "status": "success",
        "card": card,
        "error": None,
        "text": _build_success_text(card),
    }


def register_save_skill(
    app,
    config,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store=None,
    dashboard_logger=None,
):
    """
    注册 save_skill MCP 工具

    Args:
        app: MCP Server 实例
        config: 完整配置字典
        embedder: Embedding 模型实例
        vector_store: 向量数据库实例
        feishu_doc_manager: 飞书文档管理器实例
        registry_store: 知识注册表实例
        dashboard_logger: Dashboard 日志记录器（可选）
    """

    @app.tool()
    async def save_skill(
        title: str,
        content: str,
        category: str,
        project: str = "",
        tags: str = "",
    ) -> list[TextContent]:
        """将有价值的知识沉淀到飞书知识库。

        当你在编程过程中产生了值得记录的知识时，请使用此工具。包括但不限于：
        - 完成了一个架构设计方案后，沉淀架构决策
        - 解决了一个复杂 Bug 后，沉淀排查过程和解决方案
        - 发现了一个最佳实践后，记录下来供未来复用
        - 性能优化方案、技术选型决策、业务逻辑梳理等

        Args:
            title: 知识标题（简洁明了，如"Spark作业OOM优化方案"）
            content: 知识内容（Markdown格式，包含背景、方案、代码示例等）
            category: 分类，可选值：架构方案 / 产品迭代 / 优化沉淀 / 避坑记录 / 最佳实践 / 工具使用 / 业务知识
            project: 关联项目名（可选，如"mmbizwxecspark"）
            tags: 标签，多个标签用逗号分隔（可选，如"Spark,OOM,内存优化"）

        Returns:
            沉淀结果，包含飞书文档链接
        """
        tag_list: List[str] = parse_tags(tags)
        card = None

        try:
            result = await persist_skill_card(
                title=title,
                content=content,
                category=category,
                project=project,
                tags=tag_list,
                source="ai_conversation",
                config=config,
                embedder=embedder,
                vector_store=vector_store,
                feishu_doc_manager=feishu_doc_manager,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
            )
            card = result.get("card")
            return [TextContent(type="text", text=result["text"])]

        except ValueError as e:
            return [TextContent(type="text", text=f"❌ 参数错误：{str(e)}")]
        except Exception as e:
            logger.error("save_skill 执行失败: %s", e, exc_info=True)

            if dashboard_logger:
                try:
                    await dashboard_logger.log_save(
                        skill_id=card.skill_id if card else "",
                        title=title,
                        category=category,
                        project=project,
                        tags=tag_list,
                        content=content,
                        feishu_folder=category,
                        feishu_url=card.feishu_doc_url if card and card.feishu_doc_url else "",
                        feishu_doc_token=card.feishu_doc_token if card and card.feishu_doc_token else "",
                        wiki_node_token=card.wiki_node_token if card and card.wiki_node_token else "",
                        sync_status=card.sync_status if card else "",
                        status="failed",
                        error=str(e),
                    )
                except Exception:
                    pass

            return [TextContent(
                type="text",
                text=f"❌ 知识沉淀失败：{_trim_error(e, 300)}",
            )]
