"""
MCP 工具 —— list_skills（知识列表）

从知识注册表中列出已沉淀的知识卡片，支持按分类、项目、标签和状态过滤。
管理视图以注册表为准，不再以向量库作为主数据源。
"""

import logging

from mcp.types import TextContent

logger = logging.getLogger(__name__)


def _normalize_tags(tags):
    if not tags:
        return []
    if isinstance(tags, list):
        return tags
    if isinstance(tags, str):
        return [tag.strip() for tag in tags.split(",") if tag.strip()]
    return []


def register_list_skills(app, config, registry_store=None, dashboard_logger=None):
    """
    注册 list_skills MCP 工具

    Args:
        app: MCP Server 实例
        config: 完整配置字典
        registry_store: 知识注册表实例
        dashboard_logger: Dashboard 日志记录器（可选，当前未使用）
    """

    @app.tool()
    async def list_skills(
        category: str = "",
        project: str = "",
        tag: str = "",
        status: str = "",
        include_deleted: bool = False,
        limit: int = 20,
    ) -> list[TextContent]:
        """列出已沉淀的知识卡片。

        支持按分类、项目、标签、状态过滤，默认不显示已删除知识。
        """
        try:
            if registry_store is None:
                raise RuntimeError("知识注册表未初始化，无法列出知识卡片。")

            limit = max(1, min(limit, 100))
            tag_value = tag.strip()
            results = await registry_store.list_records(
                category=category.strip(),
                project=project.strip(),
                tags=[tag_value] if tag_value else None,
                sync_status=status.strip(),
                deleted=None if include_deleted else False,
                limit=limit,
            )

            if not results:
                filter_desc = []
                if category:
                    filter_desc.append(f"分类={category}")
                if project:
                    filter_desc.append(f"项目={project}")
                if tag:
                    filter_desc.append(f"标签={tag}")
                if status:
                    filter_desc.append(f"状态={status}")

                msg = "📭 暂无符合条件的知识卡片"
                if filter_desc:
                    msg += f"（过滤条件：{' / '.join(filter_desc)}）"
                return [TextContent(type="text", text=msg)]

            lines = [f"📚 共找到 **{len(results)}** 条知识卡片：", ""]
            for idx, item in enumerate(results, 1):
                tags = _normalize_tags(item.get("tags"))
                deleted = item.get("deleted", False)
                deleted_text = "（已删除）" if deleted else ""

                lines.append(
                    f"**{idx}. {item.get('title', '未命名')}** {deleted_text}\n"
                    f"- 🆔 ID：{item.get('skill_id', '')}\n"
                    f"- 📂 分类：{item.get('category', '未分类')}\n"
                    f"- 🏷️ 项目：{item.get('project', '未关联')}\n"
                    f"- 🔖 标签：{', '.join(tags) if tags else '无'}\n"
                    f"- 📡 状态：{item.get('sync_status', '未知')}\n"
                    f"- 📝 版本：{item.get('version', 1)}\n"
                    f"- 🕒 更新时间：{item.get('updated_at', item.get('created_at', '未知'))}\n"
                    f"- 🔗 飞书链接：{item.get('feishu_doc_url', '无')}"
                )
                lines.append("")

            return [TextContent(type="text", text="\n".join(lines).rstrip())]

        except Exception as e:
            logger.error("list_skills 执行失败: %s", e, exc_info=True)
            return [TextContent(type="text", text=f"❌ 获取知识列表失败：{str(e)}")]
