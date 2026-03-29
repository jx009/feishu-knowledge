"""
MCP 工具 —— search_skill（知识检索）

语义检索知识库中的相关知识。基于向量相似度匹配最相关的 Top-K 条知识卡片。
"""

import logging
from typing import Optional

from mcp.types import TextContent

logger = logging.getLogger(__name__)


def register_search_skill(app, config, embedder, vector_store, dashboard_logger=None):
    """
    注册 search_skill MCP 工具

    Args:
        app: MCP Server 实例
        config: 完整配置字典
        embedder: Embedding 模型实例
        vector_store: 向量数据库实例
        dashboard_logger: Dashboard 日志记录器（可选）
    """

    default_top_k = config.get("knowledge", {}).get("search_top_k", 5)

    @app.tool()
    async def search_skill(
        query: str,
        category: str = "",
        project: str = "",
        top_k: int = 0,
    ) -> list[TextContent]:
        """语义检索知识库中的相关知识。

        当你需要参考历史经验时，请使用此工具。包括但不限于：
        - 开始新任务前，查看是否有相关的历史经验
        - 遇到问题时，检索是否有类似的踩坑记录
        - 设计方案时，参考历史架构决策
        - 当用户任务涉及架构设计、Bug修复、性能优化时，应主动调用此工具

        Args:
            query: 检索问题（自然语言描述，如"Spark作业内存优化方案"）
            category: 限定分类过滤（可选，如"避坑记录"）。可选值：架构方案 / 产品迭代 / 优化沉淀 / 避坑记录 / 最佳实践 / 工具使用 / 业务知识
            project: 限定项目过滤（可选，如"mmbizwxecspark"）
            top_k: 返回条数（可选，默认5，最大20）

        Returns:
            匹配的知识卡片列表（含内容摘要和飞书链接）
        """
        try:
            # 参数校验
            if not query or not query.strip():
                return [TextContent(
                    type="text",
                    text="❌ 请输入检索关键词"
                )]

            actual_top_k = top_k if top_k > 0 else default_top_k
            actual_top_k = min(actual_top_k, 20)  # 最大 20 条

            # 1. 将查询语句向量化
            query_embedding = embedder.encode(query.strip())

            # 2. 构建过滤条件
            filters = {}
            if category:
                filters["category"] = category
            if project:
                filters["project"] = project

            # 3. 向量相似度检索
            results = await vector_store.search(
                query_vector=query_embedding,
                filter_conditions=filters if filters else None,
                top_k=actual_top_k,
                active_only=True,
            )

            # 4. 记录操作日志
            top_score = results[0]["score"] if results else 0.0
            if dashboard_logger:
                try:
                    await dashboard_logger.log_search(
                        query=query,
                        results_count=len(results),
                        top_score=top_score,
                    )
                    await dashboard_logger.log_search_hits(
                        query=query,
                        results=results,
                    )
                except Exception as e:
                    logger.warning(f"操作日志记录失败: {e}")

            # 5. 格式化返回结果
            if not results:
                filter_info = ""
                if category:
                    filter_info += f"（分类: {category}）"
                if project:
                    filter_info += f"（项目: {project}）"
                return [TextContent(
                    type="text",
                    text=f"未找到与「{query}」相关的知识{filter_info}。\n\n"
                         f"💡 建议：\n"
                         f"- 尝试使用不同的关键词描述\n"
                         f"- 移除分类/项目过滤条件，扩大检索范围\n"
                         f"- 如果确实没有相关知识，可以使用 save_skill 工具沉淀"
                )]

            output = f"🔍 找到 **{len(results)}** 条与「{query}」相关的知识：\n\n"

            for i, r in enumerate(results, 1):
                metadata = r.get("metadata", {})
                title = metadata.get("title", "未命名")
                category_name = metadata.get("category", "未分类")
                project_name = metadata.get("project", "")
                tags = metadata.get("tags", []) or []
                feishu_doc_url = metadata.get("feishu_doc_url", "")
                sync_status = metadata.get("sync_status", "")
                skill_id = r.get("skill_id") or metadata.get("skill_id") or r.get("id", "")
                content_text = r.get("document", "")

                output += f"### {i}. {title}\n"
                if skill_id:
                    output += f"- 🆔 **skill_id**：{skill_id}\n"
                output += f"- 📂 **分类**：{category_name}\n"
                if project_name:
                    output += f"- 🏷️ **项目**：{project_name}\n"
                if tags:
                    output += f"- 🔖 **标签**：{', '.join(tags)}\n"
                if sync_status:
                    output += f"- 📡 **同步状态**：{sync_status}\n"
                output += f"- 📊 **相似度**：{r['score']:.3f}\n"
                if feishu_doc_url:
                    output += f"- 🔗 **飞书链接**：{feishu_doc_url}\n"

                # 内容摘要（最多 500 字符）
                content_preview = content_text[:500]
                if len(content_text) > 500:
                    content_preview += "..."
                output += f"\n{content_preview}\n\n---\n\n"

            return [TextContent(type="text", text=output)]

        except Exception as e:
            logger.error(f"search_skill 执行失败: {e}", exc_info=True)

            if dashboard_logger:
                try:
                    await dashboard_logger.log_search(
                        query=query,
                        results_count=0,
                        top_score=0.0,
                        status="failed",
                        error=str(e),
                    )
                except Exception:
                    pass

            return [TextContent(
                type="text",
                text=f"❌ 知识检索失败：{str(e)[:300]}"
            )]
