"""
飞书知识空间操作封装

负责飞书知识空间（Wiki）的操作，包括：
- 列出知识空间下所有节点
- 精确查询节点详情
- 将文档挂载到指定节点
- 取消挂载或归档节点
- 获取所有文档（用于迁移或人工导入）
"""

import logging
from typing import Any, Dict, List, Optional

from lark_oapi.api.wiki.v2 import CreateSpaceNodeRequest, ListSpaceNodeRequest, Node

from .client import FeishuAPIError, FeishuClient

logger = logging.getLogger(__name__)


class WikiManager:
    """
    飞书知识空间管理器

    封装知识空间的精确节点操作与遍历能力。
    常规保存、删除、迁移流程都应优先复用这里提供的接口，
    避免在上层直接拼接 wiki 请求。
    """

    def __init__(self, config: dict):
        """
        初始化知识空间管理器

        Args:
            config: 完整配置字典，或仅 feishu 配置字典
        """
        self.full_config = config
        self.feishu_config = config.get("feishu", config)
        self.space_id = self.feishu_config.get("wiki_space_id", "")
        self.category_nodes = self.feishu_config.get("category_nodes", {}) or {}
        self.feishu_client = FeishuClient(self.feishu_config)
        self.client = self.feishu_client.get_client()

        logger.info("飞书知识空间管理器初始化完成 | Wiki空间: %s", self.space_id or "<未配置>")

    def _resolve_space_id(self, space_id: str = "") -> str:
        resolved = space_id or self.space_id
        if not resolved:
            raise ValueError("未配置 wiki_space_id，无法执行知识空间操作。")
        return resolved

    @staticmethod
    def _normalize_node(node: Any) -> Dict[str, Any]:
        if node is None:
            return {}

        source = node.get("node", node) if isinstance(node, dict) else node
        return {
            "node_token": getattr(source, "node_token", None) or source.get("node_token", ""),
            "title": getattr(source, "title", None) or source.get("title", ""),
            "obj_type": getattr(source, "obj_type", None) or source.get("obj_type", ""),
            "obj_token": getattr(source, "obj_token", None) or source.get("obj_token", ""),
            "parent_node_token": getattr(source, "parent_node_token", None) or source.get("parent_node_token", ""),
            "origin_node_token": getattr(source, "origin_node_token", None) or source.get("origin_node_token", ""),
            "origin_space_id": getattr(source, "origin_space_id", None) or source.get("origin_space_id", ""),
            "has_child": bool(getattr(source, "has_child", None) if not isinstance(source, dict) else source.get("has_child", False)),
        }

    def _category_for_node(self, node_token: str, inherited_category: str = "") -> str:
        if not node_token:
            return inherited_category

        for category, category_node_token in self.category_nodes.items():
            if category_node_token and category_node_token == node_token:
                return category
        return inherited_category

    async def list_nodes(
        self,
        space_id: str = "",
        parent_node_token: str = "",
    ) -> List[Dict[str, Any]]:
        """
        列出知识空间下的节点。

        Args:
            space_id: 知识空间 ID，未传时使用配置中的 wiki_space_id
            parent_node_token: 父节点 token（为空则列出根节点下的所有节点）

        Returns:
            节点列表
        """
        resolved_space_id = self._resolve_space_id(space_id)
        nodes: List[Dict[str, Any]] = []
        page_token = None

        while True:
            request_builder = (
                ListSpaceNodeRequest.builder()
                .space_id(resolved_space_id)
                .page_size(50)
            )
            if page_token:
                request_builder = request_builder.page_token(page_token)
            if parent_node_token:
                request_builder = request_builder.parent_node_token(parent_node_token)

            request = request_builder.build()
            response = self.client.wiki.v2.space_node.list(request)

            if not response.success():
                raise RuntimeError(
                    f"列出知识空间节点失败: code={response.code}, msg={response.msg}"
                )

            if response.data and response.data.items:
                nodes.extend(self._normalize_node(node) for node in response.data.items)

            if response.data and response.data.has_more:
                page_token = response.data.page_token
            else:
                break

        logger.info(
            "获取到 %s 个知识空间节点 | space_id=%s | parent=%s",
            len(nodes),
            resolved_space_id,
            parent_node_token or "<root>",
        )
        return nodes

    async def get_node(
        self,
        wiki_node_token: str,
        space_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """按 wiki_node_token 查询节点详情。"""
        if not wiki_node_token:
            return None

        resolved_space_id = self._resolve_space_id(space_id)
        try:
            data = self.feishu_client.request(
                "GET",
                f"wiki/v2/spaces/{resolved_space_id}/nodes/{wiki_node_token}",
            )
            node = self._normalize_node(data)
            if node.get("node_token"):
                return node
        except FeishuAPIError as exc:
            logger.warning(
                "精确获取 wiki 节点失败，回退到遍历匹配: node=%s | 错误=%s",
                wiki_node_token,
                exc,
            )

        return await self._find_node_by_token(resolved_space_id, wiki_node_token)

    async def get_node_by_obj_token(
        self,
        obj_token: str,
        space_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """按文档 obj_token 查询挂载节点详情。"""
        if not obj_token:
            return None

        resolved_space_id = self._resolve_space_id(space_id)
        all_nodes = await self._walk_nodes(resolved_space_id)
        for node in all_nodes:
            if node.get("obj_token") == obj_token:
                return node
        return None

    async def mount_document(
        self,
        doc_token: str,
        title: str,
        parent_node_token: str = "",
        space_id: str = "",
    ) -> Dict[str, Any]:
        """将指定文档挂载到知识空间的目标父节点下。"""
        if not doc_token:
            raise ValueError("doc_token 不能为空，无法挂载到知识空间。")

        resolved_space_id = self._resolve_space_id(space_id)
        node_body_builder = (
            Node.builder()
            .obj_type("docx")
            .obj_token(doc_token)
            .title(title)
        )
        if parent_node_token:
            node_body_builder = node_body_builder.parent_node_token(parent_node_token)

        request = (
            CreateSpaceNodeRequest.builder()
            .space_id(resolved_space_id)
            .request_body(node_body_builder.build())
            .build()
        )
        response = self.client.wiki.v2.space_node.create(request)
        if not response.success():
            raise RuntimeError(
                f"挂载到知识空间失败: code={response.code}, msg={response.msg}"
            )

        node = self._normalize_node(response.data.node if response.data else None)
        logger.info(
            "文档已挂载到知识空间: doc=%s | node=%s | parent=%s",
            doc_token,
            node.get("node_token", ""),
            parent_node_token or "<root>",
        )
        return node

    async def unmount_node(
        self,
        wiki_node_token: str,
        space_id: str = "",
    ) -> bool:
        """取消指定 wiki 节点的挂载。"""
        if not wiki_node_token:
            return False

        resolved_space_id = self._resolve_space_id(space_id)
        self.feishu_client.request(
            "DELETE",
            f"wiki/v2/spaces/{resolved_space_id}/nodes/{wiki_node_token}",
        )
        logger.info("知识空间节点已取消挂载: %s", wiki_node_token)
        return True

    async def move_node(
        self,
        wiki_node_token: str,
        target_parent_node_token: str,
        space_id: str = "",
    ) -> Dict[str, Any]:
        """
        将节点迁移到新的父节点。

        当前实现采用“重新挂载 + 取消旧挂载”的方式，
        避免把移动逻辑散落到上层，同时兼容文档删除归档场景。
        """
        if not target_parent_node_token:
            raise ValueError("target_parent_node_token 不能为空，无法移动 wiki 节点。")

        resolved_space_id = self._resolve_space_id(space_id)
        current_node = await self.get_node(wiki_node_token, resolved_space_id)
        if not current_node:
            raise RuntimeError(f"未找到待移动的 wiki 节点: {wiki_node_token}")

        new_node = await self.mount_document(
            doc_token=current_node.get("obj_token", ""),
            title=current_node.get("title", ""),
            parent_node_token=target_parent_node_token,
            space_id=resolved_space_id,
        )

        if current_node.get("node_token") and current_node.get("node_token") != new_node.get("node_token"):
            await self.unmount_node(current_node["node_token"], resolved_space_id)

        logger.info(
            "知识空间节点迁移完成: old=%s | new=%s | target_parent=%s",
            wiki_node_token,
            new_node.get("node_token", ""),
            target_parent_node_token,
        )
        return new_node

    async def archive_node(
        self,
        wiki_node_token: str,
        archive_parent_node_token: str,
        space_id: str = "",
    ) -> Dict[str, Any]:
        """将知识节点归档到指定父节点。"""
        return await self.move_node(
            wiki_node_token=wiki_node_token,
            target_parent_node_token=archive_parent_node_token,
            space_id=space_id,
        )

    async def get_all_documents(
        self,
        space_id: str = "",
    ) -> List[Dict[str, Any]]:
        """
        获取知识空间下所有文档（递归遍历）。

        该接口保留用于迁移或人工导入，不再作为常规同步主路径。
        """
        resolved_space_id = self._resolve_space_id(space_id)
        all_nodes = await self._walk_nodes(resolved_space_id)
        all_docs = [node for node in all_nodes if node.get("obj_type") == "docx"]
        logger.info("共获取到 %s 个文档", len(all_docs))
        return all_docs

    async def list_documents_with_categories(
        self,
        space_id: str = "",
    ) -> List[Dict[str, Any]]:
        """
        获取知识空间下所有文档，并尽量推导所属知识分类。

        规则：
        - 若文档直接位于配置的分类节点下，继承该分类
        - 若文档位于分类节点的子目录下，则沿父链继承分类
        - 若无法识别，则分类留空，由上层决定默认值
        """
        resolved_space_id = self._resolve_space_id(space_id)
        all_nodes = await self._walk_nodes_with_category(resolved_space_id)
        documents = [node for node in all_nodes if node.get("obj_type") == "docx"]
        logger.info("共获取到 %s 个带分类文档", len(documents))
        return documents

    async def _walk_nodes(
        self,
        space_id: str,
        parent_node_token: str = "",
    ) -> List[Dict[str, Any]]:
        all_nodes: List[Dict[str, Any]] = []
        child_nodes = await self.list_nodes(space_id, parent_node_token)
        for node in child_nodes:
            all_nodes.append(node)
            if node.get("has_child"):
                all_nodes.extend(await self._walk_nodes(space_id, node["node_token"]))
        return all_nodes

    async def _walk_nodes_with_category(
        self,
        space_id: str,
        parent_node_token: str = "",
        inherited_category: str = "",
    ) -> List[Dict[str, Any]]:
        all_nodes: List[Dict[str, Any]] = []
        child_nodes = await self.list_nodes(space_id, parent_node_token)
        for node in child_nodes:
            category = self._category_for_node(node.get("node_token", ""), inherited_category)
            enriched_node = dict(node)
            enriched_node["category"] = category
            all_nodes.append(enriched_node)
            if node.get("has_child"):
                all_nodes.extend(
                    await self._walk_nodes_with_category(
                        space_id,
                        node["node_token"],
                        category,
                    )
                )
        return all_nodes

    async def _find_node_by_token(
        self,
        space_id: str,
        wiki_node_token: str,
    ) -> Optional[Dict[str, Any]]:
        all_nodes = await self._walk_nodes(space_id)
        for node in all_nodes:
            if node.get("node_token") == wiki_node_token:
                return node
        return None