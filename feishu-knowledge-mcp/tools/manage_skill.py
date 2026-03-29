"""
MCP 工具 —— manage_skill（知识管理：更新、删除）

提供已有知识卡片的更新和删除能力。
更新和删除都会优先操作飞书文档，再维护注册表和向量数据库的一致性。
"""

import logging
from datetime import datetime
from typing import List

from mcp.types import TextContent

from knowledge.card import (
    SkillCard,
    SYNC_STATUS_DELETED,
    SYNC_STATUS_FAILED,
    SYNC_STATUS_INDEXED,
    SYNC_STATUS_PENDING_DELETE,
    SYNC_STATUS_PENDING_REINDEX,
    extract_content_from_markdown,
    parse_tags,
)

logger = logging.getLogger(__name__)


def _trim_error(message: Exception | str, limit: int = 200) -> str:
    return str(message)[:limit]


def _build_delete_summary(delete_result: dict) -> str:
    status = delete_result.get("status", "")
    if status == "hard_deleted":
        return "飞书文档已物理删除。"
    if status == "archived":
        archived_node_token = delete_result.get("archived_node_token", "")
        suffix = f"（新节点: {archived_node_token}）" if archived_node_token else ""
        return f"飞书文档已软删除并移动到归档目录{suffix}。"
    if status == "unmounted":
        return "飞书文档已软删除，并已从知识空间取消挂载。"
    return "飞书文档已执行软删除标记，不再参与检索。"


def register_manage_skill(
    app,
    config,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store=None,
    dashboard_logger=None,
):
    """
    注册 update_skill 和 delete_skill MCP 工具

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
    async def update_skill(
        skill_id: str,
        title: str = "",
        content: str = "",
        category: str = "",
        tags: str = "",
    ) -> list[TextContent]:
        """更新已有的知识卡片。

        更新后会先更新飞书文档，再同步更新注册表和向量数据库中的对应记录。
        """
        try:
            if registry_store is None:
                raise RuntimeError("知识注册表未初始化，已禁止更新操作，请先启用 Dashboard 数据库。")

            record = await registry_store.get(skill_id)
            if not record or record.get("deleted"):
                return [TextContent(
                    type="text",
                    text=f"❌ 未找到知识卡片: {skill_id}\n💡 使用 list_skills 查看已有的知识卡片及其 ID。"
                )]

            feishu_doc_token = record.get("feishu_doc_token")
            if not feishu_doc_token:
                raise RuntimeError(f"知识卡片缺少飞书文档 token，无法更新: {skill_id}")

            raw_markdown = await feishu_doc_manager.get_document_content(feishu_doc_token)
            existing_content = extract_content_from_markdown(raw_markdown)

            updated_title = title.strip() or record.get("title", "")
            updated_content = content if content else existing_content
            updated_category = category.strip() or record.get("category", "最佳实践")
            updated_tags: List[str] = parse_tags(tags) if tags else record.get("tags", [])

            now_iso = datetime.now().isoformat()
            next_version = int(record.get("version") or 1) + 1
            updated_card = SkillCard(
                title=updated_title,
                content=updated_content,
                category=updated_category,
                project=record.get("project", ""),
                tags=updated_tags,
                skill_id=record["skill_id"],
                created_at=record.get("created_at") or now_iso,
                updated_at=now_iso,
                source=record.get("source", "ai_conversation"),
                feishu_doc_url=record.get("feishu_doc_url") or None,
                feishu_doc_token=feishu_doc_token,
                wiki_node_token=record.get("wiki_node_token") or None,
                version=next_version,
                sync_status=SYNC_STATUS_PENDING_REINDEX,
                deleted=False,
                last_error=None,
            )

            await feishu_doc_manager.update_document(
                doc_id=feishu_doc_token,
                title=updated_card.full_title,
                content=updated_card.to_markdown(),
            )

            await registry_store.upsert(updated_card.to_registry_dict())

            try:
                new_embedding = embedder.encode(updated_card.searchable_text)
                await vector_store.upsert(
                    id=updated_card.skill_id,
                    embedding=new_embedding,
                    metadata=updated_card.to_metadata(),
                    document=updated_card.content,
                )
            except Exception as e:
                updated_card.sync_status = SYNC_STATUS_PENDING_REINDEX
                updated_card.last_error = _trim_error(e)
                await registry_store.upsert(updated_card.to_registry_dict())
                logger.error("知识卡片重建索引失败: %s | 错误: %s", skill_id, e)

                if dashboard_logger:
                    try:
                        await dashboard_logger.log_update(
                            skill_id=updated_card.skill_id,
                            title=updated_card.title,
                            category=updated_card.category,
                            project=updated_card.project,
                            tags=updated_card.tags,
                            feishu_url=updated_card.feishu_doc_url or "",
                            feishu_doc_token=updated_card.feishu_doc_token or "",
                            wiki_node_token=updated_card.wiki_node_token or "",
                            sync_status=updated_card.sync_status,
                            status="partial",
                            error=str(e),
                        )
                    except Exception as log_error:
                        logger.warning("操作日志记录失败: %s", log_error)

                return [TextContent(
                    type="text",
                    text=(
                        f"⚠️ 飞书文档已更新，但向量索引刷新失败，后续需要补偿。\n\n"
                        f"🆔 **ID**：{updated_card.skill_id}\n"
                        f"📌 **标题**：{updated_card.title}\n"
                        f"📂 **分类**：{updated_card.category}\n"
                        f"🔗 **飞书链接**：{updated_card.feishu_doc_url}\n"
                        f"📡 **当前状态**：{updated_card.sync_status}\n"
                        f"❗ **错误信息**：{_trim_error(e, 300)}"
                    ),
                )]

            updated_card.sync_status = SYNC_STATUS_INDEXED
            updated_card.last_error = None
            await registry_store.upsert(updated_card.to_registry_dict())
            logger.info("知识卡片更新成功: %s", updated_card.skill_id)

            if dashboard_logger:
                try:
                    await dashboard_logger.log_update(
                        skill_id=updated_card.skill_id,
                        title=updated_card.title,
                        category=updated_card.category,
                        project=updated_card.project,
                        tags=updated_card.tags,
                        feishu_url=updated_card.feishu_doc_url or "",
                        feishu_doc_token=updated_card.feishu_doc_token or "",
                        wiki_node_token=updated_card.wiki_node_token or "",
                        sync_status=updated_card.sync_status,
                    )
                except Exception as e:
                    logger.warning("操作日志记录失败: %s", e)

            result_text = (
                f"✅ 知识卡片已更新！\n\n"
                f"🆔 **ID**：{updated_card.skill_id}\n"
                f"📌 **标题**：{updated_card.title}\n"
                f"📂 **分类**：{updated_card.category}\n"
                f"📡 **同步状态**：{updated_card.sync_status}\n"
            )
            if updated_card.tags:
                result_text += f"🔖 **标签**：{', '.join(updated_card.tags)}\n"
            if updated_card.feishu_doc_url:
                result_text += f"🔗 **飞书链接**：{updated_card.feishu_doc_url}\n"

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            logger.error("update_skill 执行失败: %s", e, exc_info=True)
            if registry_store and skill_id:
                try:
                    await registry_store.update_status(
                        skill_id=skill_id,
                        sync_status=SYNC_STATUS_FAILED,
                        last_error=_trim_error(e),
                    )
                except Exception:
                    pass
            return [TextContent(
                type="text",
                text=f"❌ 更新知识卡片失败：{_trim_error(e, 300)}"
            )]

    @app.tool()
    async def delete_skill(
        skill_id: str,
    ) -> list[TextContent]:
        """删除指定的知识卡片。

        删除会优先对飞书文档执行软删除，再移除向量索引，并在注册表中标记删除。
        """
        try:
            if registry_store is None:
                raise RuntimeError("知识注册表未初始化，已禁止删除操作，请先启用 Dashboard 数据库。")

            record = await registry_store.get(skill_id)
            if not record or record.get("deleted"):
                return [TextContent(
                    type="text",
                    text=f"❌ 未找到知识卡片: {skill_id}\n💡 使用 list_skills 查看已有的知识卡片及其 ID。"
                )]

            skill_title = record.get("title", "未知")
            skill_category = record.get("category", "未知")
            skill_project = record.get("project", "")
            skill_tags = record.get("tags", [])
            feishu_url = record.get("feishu_doc_url", "")
            feishu_doc_token = record.get("feishu_doc_token", "")
            wiki_node_token = record.get("wiki_node_token", "")

            if not feishu_doc_token:
                raise RuntimeError(f"知识卡片缺少飞书文档 token，无法删除: {skill_id}")

            await registry_store.update_status(
                skill_id=skill_id,
                sync_status=SYNC_STATUS_PENDING_DELETE,
                last_error=None,
            )

            delete_result = await feishu_doc_manager.soft_delete_document(
                doc_id=feishu_doc_token,
                title=skill_title,
                skill_id=skill_id,
                wiki_node_token=wiki_node_token,
            )

            updated_wiki_node_token = wiki_node_token
            if delete_result.get("status") == "archived":
                updated_wiki_node_token = delete_result.get("wiki_node_token", wiki_node_token)
            elif delete_result.get("status") in {"unmounted", "hard_deleted"}:
                updated_wiki_node_token = ""

            deleted_record = {
                **record,
                "wiki_node_token": updated_wiki_node_token,
                "sync_status": SYNC_STATUS_PENDING_DELETE,
                "deleted": False,
                "last_error": None,
            }
            await registry_store.upsert(deleted_record)

            try:
                await vector_store.delete(skill_id)
            except Exception as vector_error:
                partial_error = _trim_error(vector_error, 300)
                partial_record = {
                    **deleted_record,
                    "last_error": partial_error,
                }
                await registry_store.upsert(partial_record)
                logger.error("知识卡片向量删除失败，等待补偿: %s | 错误: %s", skill_id, vector_error)

                if dashboard_logger:
                    try:
                        await dashboard_logger.log_delete(
                            skill_id=skill_id,
                            title=skill_title,
                            category=skill_category,
                            project=skill_project,
                            tags=skill_tags,
                            feishu_url=feishu_url,
                            feishu_doc_token=feishu_doc_token,
                            wiki_node_token=updated_wiki_node_token,
                            sync_status=SYNC_STATUS_PENDING_DELETE,
                            status="partial",
                            error=str(vector_error),
                        )
                    except Exception as e:
                        logger.warning("操作日志记录失败: %s", e)

                result_text = (
                    f"⚠️ 飞书侧删除已完成，但向量索引删除失败，后续需要补偿。\n\n"
                    f"🆔 **ID**：{skill_id}\n"
                    f"📌 **标题**：{skill_title}\n"
                    f"📂 **分类**：{skill_category}\n"
                    f"📡 **当前状态**：{SYNC_STATUS_PENDING_DELETE}\n"
                    f"📝 **飞书处理**：{_build_delete_summary(delete_result)}\n"
                    f"❗ **错误信息**：{partial_error}\n"
                )
                if feishu_url:
                    result_text += f"🔗 **飞书链接**：{feishu_url}\n"

                return [TextContent(type="text", text=result_text)]

            deleted_record.update({
                "sync_status": SYNC_STATUS_DELETED,
                "deleted": True,
                "last_error": None,
            })
            await registry_store.upsert(deleted_record)
            logger.info("知识卡片已删除: %s | %s", skill_id, skill_title)

            if dashboard_logger:
                try:
                    await dashboard_logger.log_delete(
                        skill_id=skill_id,
                        title=skill_title,
                        category=skill_category,
                        project=skill_project,
                        tags=skill_tags,
                        feishu_url=feishu_url,
                        feishu_doc_token=feishu_doc_token,
                        wiki_node_token=updated_wiki_node_token,
                        sync_status=SYNC_STATUS_DELETED,
                    )
                except Exception as e:
                    logger.warning("操作日志记录失败: %s", e)

            result_text = (
                f"✅ 知识卡片已删除！\n\n"
                f"🆔 **ID**：{skill_id}\n"
                f"📌 **标题**：{skill_title}\n"
                f"📂 **分类**：{skill_category}\n"
                f"📡 **同步状态**：{SYNC_STATUS_DELETED}\n"
                f"📝 **飞书处理**：{_build_delete_summary(delete_result)}\n"
            )
            if feishu_url:
                result_text += f"🔗 **飞书链接**：{feishu_url}\n"

            return [TextContent(type="text", text=result_text)]

        except Exception as e:
            logger.error("delete_skill 执行失败: %s", e, exc_info=True)
            if registry_store and skill_id:
                try:
                    await registry_store.update_status(
                        skill_id=skill_id,
                        sync_status=SYNC_STATUS_FAILED,
                        last_error=_trim_error(e),
                    )
                except Exception:
                    pass

            if dashboard_logger:
                try:
                    record = await registry_store.get(skill_id) if registry_store else None
                    await dashboard_logger.log_delete(
                        skill_id=skill_id,
                        title=record.get("title", "") if record else "",
                        category=record.get("category", "") if record else "",
                        project=record.get("project", "") if record else "",
                        tags=record.get("tags", []) if record else [],
                        feishu_url=record.get("feishu_doc_url", "") if record else "",
                        feishu_doc_token=record.get("feishu_doc_token", "") if record else "",
                        wiki_node_token=record.get("wiki_node_token", "") if record else "",
                        sync_status=SYNC_STATUS_FAILED,
                        status="failed",
                        error=str(e),
                    )
                except Exception:
                    pass

            return [TextContent(
                type="text",
                text=f"❌ 删除知识卡片失败：{_trim_error(e, 300)}"
            )]