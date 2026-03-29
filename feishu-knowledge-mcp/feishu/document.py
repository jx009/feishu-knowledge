"""
飞书文档操作封装

负责飞书文档的 CRUD 操作，包括：
- 在知识空间中创建文档
- 读取文档内容
- 更新文档内容
- 删除或软删除文档
"""

import logging
from typing import Any, Dict

from lark_oapi.api.docx.v1 import *
from lark_oapi.api.wiki.v2 import *

from .client import FeishuClient
from .wiki import WikiManager

logger = logging.getLogger(__name__)


class FeishuDocManager:
    """
    飞书文档管理器

    封装飞书文档和知识空间的操作，提供创建、读取、更新、删除文档的能力。

    用法:
        manager = FeishuDocManager(config["feishu"])
        result = await manager.create_document(space_id, parent_node, "标题", "内容")
    """

    def __init__(self, config: dict):
        """
        初始化文档管理器

        Args:
            config: 完整配置字典，或仅 feishu 配置字典
        """
        self.full_config = config
        self.feishu_config = config.get("feishu", config)
        self.deletion_config = config.get("deletion", {}) if "feishu" in config else {}

        self.feishu_client = FeishuClient(self.feishu_config)
        self.client = self.feishu_client.get_client()
        self.wiki_space_id = self.feishu_config.get("wiki_space_id", "")
        self.category_nodes = self.feishu_config.get("category_nodes", {})
        self.wiki_manager = WikiManager(config)

        logger.info(f"飞书文档管理器初始化完成 | Wiki空间: {self.wiki_space_id}")

    def _get_parent_node(self, category: str) -> str:
        """
        根据分类获取飞书知识空间中对应的父节点 token

        Args:
            category: 知识分类（如"架构方案"、"避坑记录"等）

        Returns:
            父节点的 node_token
        """
        node_token = self.category_nodes.get(category)
        if not node_token:
            logger.warning(f"分类 '{category}' 未配置飞书节点映射，使用根节点")
            return ""
        return node_token

    async def create_document(
        self,
        space_id: str,
        parent_node: str,
        title: str,
        content: str,
    ) -> Dict[str, str]:
        """
        创建飞书文档并挂载到知识库。

        注意：飞书文档实体和 wiki 节点是两个不同对象。
        这里会先创建 docx 文档，再挂载到 wiki，最后返回结构化结果。

        Args:
            space_id: 知识空间 ID
            parent_node: 父节点 token（对应 wiki 分类节点）
            title: 文档标题
            content: 文档内容（Markdown 格式）

        Returns:
            包含 doc_url、feishu_doc_token、wiki_node_token 的字典
        """
        try:
            doc_request = (
                CreateDocumentRequest.builder()
                .request_body(
                    CreateDocumentRequestBody.builder()
                    .title(title)
                    .build()
                )
                .build()
            )

            doc_response = self.client.docx.v1.document.create(doc_request)
            if not doc_response.success():
                raise RuntimeError(
                    f"创建飞书文档失败: code={doc_response.code}, msg={doc_response.msg}"
                )

            doc_token = doc_response.data.document.document_id
            logger.info(f"飞书文档创建成功: {doc_token} | 标题: {title}")

            await self._write_content(doc_token, content)

            wiki_node_token = ""
            if space_id:
                wiki_node_token = await self._mount_to_wiki(
                    space_id=space_id,
                    parent_node=parent_node,
                    doc_id=doc_token,
                    title=title,
                )

            doc_url = f"https://feishu.cn/docx/{doc_token}"
            logger.info(
                "文档创建并挂载完成: doc_token=%s | wiki_node_token=%s",
                doc_token,
                wiki_node_token or "<root>",
            )

            return {
                "doc_url": doc_url,
                "feishu_doc_token": doc_token,
                "wiki_node_token": wiki_node_token,
            }

        except Exception as e:
            logger.error(f"创建飞书文档失败: {e}")
            raise

    async def _write_content(self, doc_id: str, content: str):
        """
        向文档中写入初始内容。

        初次创建时仍使用 block 写入，避免在创建阶段引入额外的 raw_content 依赖。
        后续更新流程统一通过 raw_content 全量覆盖。
        """
        try:
            text_elements = [
                TextElement.builder()
                .text_run(TextRun.builder().content(content).build())
                .build()
            ]

            block = (
                Block.builder()
                .block_type(2)
                .text(Text.builder().elements(text_elements).build())
                .build()
            )

            block_request = (
                CreateDocumentBlockChildrenRequest.builder()
                .document_id(doc_id)
                .block_id(doc_id)
                .request_body(
                    CreateDocumentBlockChildrenRequestBody.builder()
                    .children([block])
                    .build()
                )
                .build()
            )

            block_response = self.client.docx.v1.document_block_children.create(block_request)
            if not block_response.success():
                raise RuntimeError(
                    f"写入文档内容失败: code={block_response.code}, msg={block_response.msg}"
                )

            logger.info(f"文档内容写入成功: {doc_id}")

        except Exception as e:
            logger.error(f"写入文档内容失败: {doc_id} | 错误: {e}")
            raise

    async def _mount_to_wiki(
        self,
        space_id: str,
        parent_node: str,
        doc_id: str,
        title: str,
    ) -> str:
        """
        将文档挂载到知识空间的指定节点下。

        Args:
            space_id: 知识空间 ID
            parent_node: 父节点 token
            doc_id: 文档 token
            title: 文档标题

        Returns:
            创建的 wiki 节点 token
        """
        try:
            node = await self.wiki_manager.mount_document(
                doc_token=doc_id,
                title=title,
                parent_node_token=parent_node,
                space_id=space_id,
            )
            return node.get("node_token", "")

        except Exception as e:
            logger.error(f"挂载文档到知识空间失败: {e}")
            raise

    async def update_document(self, doc_id: str, title: str, content: str):
        """
        全量更新文档标题和正文内容。

        正文内容使用 raw_content 接口覆盖，标题更新失败时仅记录告警，
        不影响正文更新结果，因为正文中的一级标题同样会体现最新标题。
        """
        self._replace_raw_content(doc_id, content)

        if title:
            try:
                self._update_title(doc_id, title)
            except Exception as e:
                logger.warning(f"更新文档标题失败（正文已更新）: {doc_id} | 错误: {e}")

        logger.info(f"文档更新成功: {doc_id}")

    def _replace_raw_content(self, doc_id: str, content: str):
        self.feishu_client.request(
            "PUT",
            f"docx/v1/documents/{doc_id}/raw_content",
            body={"content": content},
        )

    def _update_title(self, doc_id: str, title: str):
        self.feishu_client.request(
            "PATCH",
            f"docx/v1/documents/{doc_id}",
            body={"title": title},
        )

    def _resolve_deletion_strategy(self) -> str:
        return self.deletion_config.get("strategy", "soft_delete_only")

    async def soft_delete_document(
        self,
        doc_id: str,
        title: str,
        skill_id: str,
        wiki_node_token: str = "",
    ) -> Dict[str, Any]:
        """
        受控删除文档。

        根据 deletion 配置执行以下策略之一：
        - soft_delete_only：仅标记正文和标题为已删除
        - soft_delete_and_unmount：标记删除后，从 wiki 取消挂载
        - hard_delete：取消挂载后物理删除飞书文档

        如果配置了 archive_parent_node_token，则软删除场景优先将 wiki 节点移动到归档目录。
        返回结构化结果，便于上层提示“已归档”“已取消挂载”或“已彻底删除”。
        """
        strategy = self._resolve_deletion_strategy()
        archive_parent_node_token = self.deletion_config.get("archive_parent_node_token", "")
        should_unmount = bool(self.deletion_config.get("unmount_from_wiki", False)) or strategy == "soft_delete_and_unmount"
        result: Dict[str, Any] = {
            "strategy": strategy,
            "status": "unknown",
            "doc_id": doc_id,
            "wiki_node_token": wiki_node_token,
            "soft_deleted": False,
            "unmounted": False,
            "archived": False,
            "hard_deleted": False,
            "archived_node_token": "",
        }

        if strategy == "hard_delete":
            if wiki_node_token and should_unmount:
                await self.wiki_manager.unmount_node(wiki_node_token)
                result["unmounted"] = True
            await self.delete_document(doc_id)
            result["hard_deleted"] = True
            result["status"] = "hard_deleted"
            return result

        deleted_title = title if title.startswith("【已删除】") else f"【已删除】{title}"
        deleted_content = (
            f"# {deleted_title}\n\n"
            f"> 该知识已从 MCP 知识库中删除，不再参与检索。\n"
            f"> skill_id: {skill_id}\n"
        )
        await self.update_document(doc_id=doc_id, title=deleted_title, content=deleted_content)
        result["soft_deleted"] = True

        if wiki_node_token and archive_parent_node_token:
            archived_node = await self.wiki_manager.archive_node(
                wiki_node_token=wiki_node_token,
                archive_parent_node_token=archive_parent_node_token,
            )
            result["archived"] = True
            result["archived_node_token"] = archived_node.get("node_token", "")
            result["wiki_node_token"] = archived_node.get("node_token", wiki_node_token)
            result["status"] = "archived"
            return result

        if wiki_node_token and should_unmount:
            await self.wiki_manager.unmount_node(wiki_node_token)
            result["unmounted"] = True
            result["status"] = "unmounted"
            return result

        result["status"] = "soft_deleted"
        return result

    async def get_document_content(self, doc_id: str) -> str:
        """
        获取文档的原始文本内容

        Args:
            doc_id: 文档 ID

        Returns:
            文档的文本内容
        """
        try:
            request = (
                RawContentDocumentRequest.builder()
                .document_id(doc_id)
                .build()
            )
            response = self.client.docx.v1.document.raw_content(request)

            if not response.success():
                raise RuntimeError(
                    f"获取文档内容失败: code={response.code}, msg={response.msg}"
                )

            return response.data.content

        except Exception as e:
            logger.error(f"获取文档内容失败: {doc_id} | 错误: {e}")
            raise

    async def get_document_info(self, doc_id: str) -> Dict[str, Any]:
        """
        获取文档基础元数据。

        返回尽可能稳定的最小字段集，供同步逻辑判断标题和更新时间。
        """
        try:
            data = self.feishu_client.request("GET", f"docx/v1/documents/{doc_id}")
            document = data.get("document", data)
            return {
                "doc_id": document.get("document_id") or doc_id,
                "title": document.get("title", ""),
                "revision_id": document.get("revision_id"),
                "create_time": document.get("create_time") or document.get("created_at") or "",
                "update_time": document.get("update_time") or document.get("updated_at") or "",
                "owner_id": document.get("owner_id") or "",
            }
        except Exception as e:
            logger.error(f"获取文档基础信息失败: {doc_id} | 错误: {e}")
            raise

    async def get_document_snapshot(
        self,
        doc_id: str,
        wiki_node_token: str = "",
        category: str = "",
    ) -> Dict[str, Any]:
        """
        获取同步所需的统一文档快照。

        包含标题、正文、更新时间、文档 URL 和分类等字段。
        """
        info = await self.get_document_info(doc_id)
        content = await self.get_document_content(doc_id)
        resolved_title = info.get("title") or doc_id
        return {
            "doc_id": doc_id,
            "title": resolved_title,
            "content": content,
            "update_time": info.get("update_time") or info.get("create_time") or "",
            "create_time": info.get("create_time") or "",
            "revision_id": info.get("revision_id"),
            "wiki_node_token": wiki_node_token,
            "category": category,
            "doc_url": f"https://feishu.cn/docx/{doc_id}",
        }

    async def delete_document(self, doc_id: str):
        """
        物理删除文档。

        当前主流程优先使用 soft_delete_document，只有在明确需要物理删除时才调用该方法。
        """
        try:
            request = (
                DeleteDocumentRequest.builder()
                .document_id(doc_id)
                .build()
            )
            response = self.client.docx.v1.document.delete(request)

            if not response.success():
                raise RuntimeError(
                    f"删除文档失败: code={response.code}, msg={response.msg}"
                )

            logger.info(f"文档删除成功: {doc_id}")

        except Exception as e:
            logger.error(f"删除文档失败: {doc_id} | 错误: {e}")
            raise