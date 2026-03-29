"""
MCP 工具 —— automation_review（自动沉淀审核队列）

提供自动沉淀候选知识的审核、驳回、批量处理能力。
审核通过会复用现有保存链路，确保飞书、注册表、向量索引写入逻辑保持一致。
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from mcp.types import TextContent

from knowledge.card import SkillCard, calculate_content_hash, extract_content_from_markdown
from tools.save_skill import persist_skill_card

logger = logging.getLogger(__name__)


def _trim_error(message: Exception | str, limit: int = 300) -> str:
    return str(message)[:limit]


def _normalize_text(value: str) -> str:
    return str(value or "").strip()


def _parse_review_ids(review_ids: str | List[str]) -> List[str]:
    if isinstance(review_ids, list):
        return [str(item).strip() for item in review_ids if str(item).strip()]

    raw_value = _normalize_text(review_ids)
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass

    return [item.strip() for item in raw_value.replace("，", ",").split(",") if item.strip()]


def _merge_saved_skill_ids(existing_ids: List[str], new_skill_id: str) -> List[str]:
    merged = [str(item).strip() for item in existing_ids if str(item).strip()]
    if new_skill_id and new_skill_id not in merged:
        merged.append(new_skill_id)
    return merged


def _merge_tags(existing_tags: List[str], new_tags: List[str]) -> List[str]:
    merged: List[str] = []
    for tag in [*(existing_tags or []), *(new_tags or [])]:
        clean = str(tag).strip()
        if clean and clean not in merged:
            merged.append(clean)
    return merged


def _normalize_compare_text(value: str) -> str:
    return " ".join(_normalize_text(value).lower().split())


def _build_merged_content(existing_content: str, candidate_content: str, review_item: Dict[str, Any]) -> str:
    base_content = _normalize_text(existing_content)
    incoming_content = _normalize_text(candidate_content)
    if not incoming_content:
        return base_content
    if not base_content:
        return incoming_content
    if _normalize_compare_text(incoming_content) == _normalize_compare_text(base_content):
        return base_content

    merged_sections = [
        base_content,
        "\n\n---\n\n## 自动沉淀补充\n",
        incoming_content,
    ]

    reasons = review_item.get("reasons") or []
    context_lines = []
    if review_item.get("review_id"):
        context_lines.append(f"- 审核ID：{review_item.get('review_id')}")
    if review_item.get("session_id"):
        context_lines.append(f"- 会话ID：{review_item.get('session_id')}")
    if reasons:
        context_lines.append(f"- 合并原因：{'；'.join(str(item) for item in reasons if str(item).strip())}")
    if context_lines:
        merged_sections.extend(["\n\n### 合并上下文\n", "\n".join(context_lines)])

    return "".join(merged_sections).strip()


async def _semantic_match_existing_skill(
    *,
    review_item: Dict[str, Any],
    config,
    embedder,
    vector_store,
) -> Dict[str, Any] | None:
    governance_config = (config.get("governance") or {}) if isinstance(config, dict) else {}
    if not governance_config.get("enabled", True):
        return None
    if not governance_config.get("semantic_merge_enabled", True):
        return None

    content = (
        review_item.get("draft_content")
        or review_item.get("excerpt")
        or review_item.get("source_text")
        or review_item.get("title")
        or ""
    ).strip()
    if not content:
        return None

    threshold = float(governance_config.get("semantic_merge_score_threshold", 0.9) or 0.9)
    filters = {}
    if review_item.get("project"):
        filters["project"] = review_item.get("project")
    if review_item.get("category"):
        filters["category"] = review_item.get("category")

    query_vector = embedder.encode(content)
    matches = await vector_store.search(
        query_vector=query_vector,
        filter_conditions=filters if filters else None,
        top_k=max(1, int(governance_config.get("max_related_skills", 3) or 3)),
        active_only=True,
    )
    for match in matches:
        score = float(match.get("score") or 0.0)
        skill_id = match.get("skill_id") or match.get("id") or ""
        if skill_id and score >= threshold:
            return {
                "skill_id": skill_id,
                "score": score,
                "metadata": match.get("metadata") or {},
            }
    return None


async def _decide_review_governance_target(
    *,
    review_item: Dict[str, Any],
    registry_store,
    config,
    embedder,
    vector_store,
) -> Dict[str, Any]:
    governance_config = (config.get("governance") or {}) if isinstance(config, dict) else {}
    content = (
        review_item.get("draft_content")
        or review_item.get("excerpt")
        or review_item.get("source_text")
        or review_item.get("title")
        or ""
    )
    content_hash = calculate_content_hash(content)

    if governance_config.get("enabled", True):
        exact_hash_match = await registry_store.get_by_content_hash(content_hash)
        if exact_hash_match is not None:
            return {
                "decision": "reuse_existing",
                "reason": "content_hash_match",
                "target": exact_hash_match,
                "content": content,
                "content_hash": content_hash,
                "score": 1.0,
            }

        if governance_config.get("exact_title_merge_enabled", True):
            title_matches = await registry_store.find_active_by_title(
                review_item.get("title") or "",
                project=review_item.get("project") or "",
                category=review_item.get("category") or "",
            )
            if title_matches:
                return {
                    "decision": "merge_existing",
                    "reason": "exact_title_match",
                    "target": title_matches[0],
                    "content": content,
                    "content_hash": content_hash,
                    "score": 1.0,
                }

        semantic_match = await _semantic_match_existing_skill(
            review_item=review_item,
            config=config,
            embedder=embedder,
            vector_store=vector_store,
        )
        if semantic_match is not None:
            target = await registry_store.get(semantic_match["skill_id"])
            if target is not None:
                return {
                    "decision": "merge_existing",
                    "reason": "semantic_similarity",
                    "target": target,
                    "content": content,
                    "content_hash": content_hash,
                    "score": semantic_match["score"],
                }

    return {
        "decision": "create_new",
        "reason": "new_skill",
        "target": None,
        "content": content,
        "content_hash": content_hash,
        "score": 0.0,
    }


async def _merge_into_existing_skill(
    *,
    target_record: Dict[str, Any],
    review_item: Dict[str, Any],
    merged_content: str,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store,
    dashboard_logger=None,
) -> Dict[str, Any]:
    skill_id = target_record.get("skill_id") or ""
    feishu_doc_token = target_record.get("feishu_doc_token") or ""
    if not skill_id or not feishu_doc_token:
        raise RuntimeError("目标知识缺少必要的 skill_id 或飞书文档 token，无法执行合并。")

    updated_title = target_record.get("title") or review_item.get("title") or "未命名知识"
    updated_category = target_record.get("category") or review_item.get("category") or "最佳实践"
    updated_tags = _merge_tags(target_record.get("tags") or [], review_item.get("tags") or [])
    now_iso = datetime.now().isoformat()
    next_version = int(target_record.get("version") or 1) + 1

    updated_card = SkillCard(
        title=updated_title,
        content=merged_content,
        category=updated_category,
        project=target_record.get("project") or review_item.get("project") or "",
        tags=updated_tags,
        skill_id=skill_id,
        created_at=target_record.get("created_at") or now_iso,
        updated_at=now_iso,
        source=target_record.get("source", "automation_review"),
        feishu_doc_url=target_record.get("feishu_doc_url") or None,
        feishu_doc_token=feishu_doc_token,
        wiki_node_token=target_record.get("wiki_node_token") or None,
        version=next_version,
        sync_status="PENDING_REINDEX",
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
    except Exception as exc:
        updated_card.sync_status = "PENDING_REINDEX"
        updated_card.last_error = _trim_error(exc)
        await registry_store.upsert(updated_card.to_registry_dict())
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
                    error=str(exc),
                )
            except Exception as log_error:
                logger.warning("治理合并日志记录失败: %s", log_error)
        return {
            "status": "partial",
            "card": updated_card,
            "error": str(exc),
            "action": "merge_existing",
        }

    updated_card.sync_status = "INDEXED"
    updated_card.last_error = None
    await registry_store.upsert(updated_card.to_registry_dict())
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
        except Exception as log_error:
            logger.warning("治理合并日志记录失败: %s", log_error)

    return {
        "status": "success",
        "card": updated_card,
        "error": None,
        "action": "merge_existing",
    }


def _build_review_digest(item: Dict[str, Any], index: int) -> str:
    return (
        f"### {index}. {item.get('title') or '未命名候选'}\n"
        f"- 审核ID：{item.get('review_id') or ''}\n"
        f"- 会话ID：{item.get('session_id') or ''}\n"
        f"- 分类：{item.get('category') or '最佳实践'}\n"
        f"- 项目：{item.get('project') or '未关联项目'}\n"
        f"- 置信度：{item.get('confidence') or 'unknown'}\n"
        f"- 评分：{item.get('score') or 0}\n"
        f"- 状态：{item.get('status') or 'pending'}\n"
        f"- 摘要：{item.get('excerpt') or ''}"
    ).rstrip()


async def approve_review_item_record(
    review_id: str,
    *,
    config,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store,
    dashboard_logger=None,
) -> Dict[str, Any]:
    if registry_store is None:
        raise RuntimeError("知识注册表未初始化，无法审批自动沉淀候选。")

    clean_review_id = _normalize_text(review_id)
    if not clean_review_id:
        raise RuntimeError("请输入有效的审核ID。")

    review_item = await registry_store.get_review_item(clean_review_id)
    if review_item is None:
        raise RuntimeError(f"未找到审核项：{clean_review_id}")

    if review_item.get("status") == "approved":
        return {
            "status": "noop",
            "review": review_item,
            "card": None,
            "text": f"ℹ️ 审核项已审批通过，无需重复处理：{clean_review_id}",
        }

    content = (
        review_item.get("draft_content")
        or review_item.get("excerpt")
        or review_item.get("source_text")
        or review_item.get("title")
        or "自动沉淀候选知识"
    )

    try:
        governance_target = await _decide_review_governance_target(
            review_item=review_item,
            registry_store=registry_store,
            config=config,
            embedder=embedder,
            vector_store=vector_store,
        )

        governance_action = governance_target["decision"]
        governance_reason = governance_target["reason"]

        if governance_action == "reuse_existing":
            related_skill_id = governance_target["target"].get("skill_id") or ""
            card = SkillCard.from_registry(governance_target["target"], content)
            save_result = {
                "status": "success",
                "card": card,
                "error": None,
                "action": governance_action,
            }
        elif governance_action == "merge_existing":
            target_record = governance_target["target"] or {}
            target_doc_token = target_record.get("feishu_doc_token") or ""
            raw_markdown = await feishu_doc_manager.get_document_content(target_doc_token)
            existing_content = extract_content_from_markdown(raw_markdown)
            merged_content = _build_merged_content(existing_content, content, review_item)
            save_result = await _merge_into_existing_skill(
                target_record=target_record,
                review_item=review_item,
                merged_content=merged_content,
                embedder=embedder,
                vector_store=vector_store,
                feishu_doc_manager=feishu_doc_manager,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
            )
            related_skill_id = getattr(save_result.get("card"), "skill_id", "")
            card = save_result.get("card")
        else:
            save_result = await persist_skill_card(
                title=review_item.get("title") or "未命名知识",
                content=content,
                category=review_item.get("category") or "最佳实践",
                project=review_item.get("project") or "",
                tags=review_item.get("tags") or [],
                source="automation_review",
                config=config,
                embedder=embedder,
                vector_store=vector_store,
                feishu_doc_manager=feishu_doc_manager,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
            )
            card = save_result.get("card")
            related_skill_id = getattr(card, "skill_id", "") if card is not None else ""
        review_item = await registry_store.upsert_review_item(
            {
                **review_item,
                "status": "approved",
                "related_skill_id": related_skill_id or review_item.get("related_skill_id") or "",
                "last_error": save_result.get("error") if save_result.get("status") == "partial" else None,
                "auto_decision": governance_action,
            }
        )

        session_id = review_item.get("session_id") or ""
        if session_id and related_skill_id:
            existing_session = await registry_store.get_automation_session(session_id)
            if existing_session is not None:
                await registry_store.upsert_automation_session(
                    {
                        "session_id": session_id,
                        "saved_skill_ids": _merge_saved_skill_ids(
                            existing_session.get("saved_skill_ids", []),
                            related_skill_id,
                        ),
                    }
                )

        if dashboard_logger:
            try:
                await dashboard_logger.log_automation(
                    session_id=review_item.get("session_id") or clean_review_id,
                    stage="review_approve",
                    status="partial" if save_result.get("status") == "partial" else "success",
                    project=review_item.get("project") or "",
                    query=review_item.get("title") or clean_review_id,
                    content_preview=content,
                    result_count=1,
                    error=save_result.get("error"),
                )
            except Exception as log_error:
                logger.warning("审核通过日志记录失败: %s", log_error)

        text_lines = [
            "✅ 审核项已通过并完成入库。",
            "",
            f"- 审核ID：{clean_review_id}",
            f"- 会话ID：{review_item.get('session_id') or ''}",
            f"- 标题：{review_item.get('title') or '未命名知识'}",
            f"- 关联知识ID：{review_item.get('related_skill_id') or ''}",
            f"- 当前状态：{review_item.get('status') or 'approved'}",
            f"- 治理动作：{governance_action}",
            f"- 判定原因：{governance_reason}",
        ]
        if getattr(card, "feishu_doc_url", ""):
            text_lines.append(f"- 飞书链接：{card.feishu_doc_url}")
        if save_result.get("status") == "partial" and save_result.get("error"):
            text_lines.append(f"- 异常说明：{_trim_error(save_result['error'])}")

        return {
            "status": "partial" if save_result.get("status") == "partial" else "success",
            "review": review_item,
            "card": card,
            "text": "\n".join(text_lines),
        }
    except Exception as e:
        await registry_store.upsert_review_item(
            {
                **review_item,
                "status": review_item.get("status") or "pending",
                "last_error": _trim_error(e),
            }
        )
        if dashboard_logger:
            try:
                await dashboard_logger.log_automation(
                    session_id=review_item.get("session_id") or clean_review_id,
                    stage="review_approve",
                    status="failed",
                    project=review_item.get("project") or "",
                    query=review_item.get("title") or clean_review_id,
                    content_preview=content,
                    result_count=0,
                    error=str(e),
                )
            except Exception:
                pass
        raise


async def reject_review_item_record(
    review_id: str,
    *,
    registry_store,
    dashboard_logger=None,
    reason: str = "",
) -> Dict[str, Any]:
    if registry_store is None:
        raise RuntimeError("知识注册表未初始化，无法驳回自动沉淀候选。")

    clean_review_id = _normalize_text(review_id)
    if not clean_review_id:
        raise RuntimeError("请输入有效的审核ID。")

    review_item = await registry_store.get_review_item(clean_review_id)
    if review_item is None:
        raise RuntimeError(f"未找到审核项：{clean_review_id}")

    if review_item.get("status") == "approved":
        return {
            "status": "noop",
            "review": review_item,
            "text": f"ℹ️ 审核项已审批通过，不能再驳回：{clean_review_id}",
        }

    if review_item.get("status") == "rejected":
        return {
            "status": "noop",
            "review": review_item,
            "text": f"ℹ️ 审核项已驳回，无需重复处理：{clean_review_id}",
        }

    normalized_reason = _normalize_text(reason)
    review_item = await registry_store.upsert_review_item(
        {
            **review_item,
            "status": "rejected",
            "last_error": normalized_reason or None,
        }
    )

    if dashboard_logger:
        try:
            await dashboard_logger.log_automation(
                session_id=review_item.get("session_id") or clean_review_id,
                stage="review_reject",
                status="success",
                project=review_item.get("project") or "",
                query=review_item.get("title") or clean_review_id,
                content_preview=review_item.get("draft_content") or review_item.get("excerpt") or "",
                result_count=1,
                error=normalized_reason or None,
            )
        except Exception as log_error:
            logger.warning("审核驳回日志记录失败: %s", log_error)

    text_lines = [
        "🗑️ 审核项已驳回。",
        "",
        f"- 审核ID：{clean_review_id}",
        f"- 会话ID：{review_item.get('session_id') or ''}",
        f"- 标题：{review_item.get('title') or '未命名候选'}",
        f"- 当前状态：{review_item.get('status') or 'rejected'}",
    ]
    if normalized_reason:
        text_lines.append(f"- 驳回原因：{normalized_reason}")

    return {
        "status": "success",
        "review": review_item,
        "text": "\n".join(text_lines),
    }


async def batch_review_items(
    *,
    action: str,
    review_ids: List[str],
    config,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store,
    dashboard_logger=None,
    reason: str = "",
) -> Dict[str, Any]:
    clean_action = _normalize_text(action).lower()
    if clean_action not in {"approve", "reject"}:
        raise RuntimeError("action 仅支持 approve 或 reject。")

    normalized_ids = [review_id for review_id in review_ids if _normalize_text(review_id)]
    if not normalized_ids:
        raise RuntimeError("请至少提供一个审核ID。")

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    success_count = 0

    for review_id in normalized_ids:
        try:
            if clean_action == "approve":
                result = await approve_review_item_record(
                    review_id,
                    config=config,
                    embedder=embedder,
                    vector_store=vector_store,
                    feishu_doc_manager=feishu_doc_manager,
                    registry_store=registry_store,
                    dashboard_logger=dashboard_logger,
                )
            else:
                result = await reject_review_item_record(
                    review_id,
                    registry_store=registry_store,
                    dashboard_logger=dashboard_logger,
                    reason=reason,
                )
            results.append(
                {
                    "review_id": review_id,
                    "status": result.get("status") or "success",
                    "text": result.get("text") or "",
                }
            )
            if result.get("status") != "failed":
                success_count += 1
        except Exception as e:
            errors.append({"review_id": review_id, "error": _trim_error(e)})
            results.append(
                {
                    "review_id": review_id,
                    "status": "failed",
                    "text": _trim_error(e),
                }
            )

    return {
        "action": clean_action,
        "total": len(normalized_ids),
        "success_count": success_count,
        "failed_count": len(errors),
        "results": results,
        "errors": errors,
    }


def register_automation_review(
    app,
    config,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store,
    dashboard_logger=None,
):
    @app.tool()
    async def list_review_queue(
        status: str = "pending",
        session_id: str = "",
        project: str = "",
        confidence: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[TextContent]:
        """查看自动沉淀审核队列。"""
        if registry_store is None:
            return [TextContent(type="text", text="❌ 知识注册表未初始化，无法查看审核队列。")]

        actual_limit = min(max(1, int(limit or 20)), 100)
        items = await registry_store.list_review_items(
            status=_normalize_text(status),
            session_id=_normalize_text(session_id),
            project=_normalize_text(project),
            confidence=_normalize_text(confidence),
            limit=actual_limit,
            offset=max(0, int(offset or 0)),
        )
        total = await registry_store.count_review_items(
            status=_normalize_text(status),
            session_id=_normalize_text(session_id),
            project=_normalize_text(project),
            confidence=_normalize_text(confidence),
        )

        if not items:
            return [TextContent(type="text", text="📭 当前没有匹配的审核项。")]

        blocks = [f"🧾 当前共匹配到 **{total}** 条审核项。", ""]
        for index, item in enumerate(items, 1):
            blocks.append(_build_review_digest(item, index))
            blocks.append("")
        payload = {
            "total": total,
            "limit": actual_limit,
            "offset": max(0, int(offset or 0)),
            "items": items,
        }
        blocks.extend(["```json", json.dumps(payload, ensure_ascii=False, indent=2), "```"])
        return [TextContent(type="text", text="\n".join(blocks).rstrip())]

    @app.tool()
    async def approve_review_item(review_id: str) -> list[TextContent]:
        """将指定审核项审批通过，并复用现有保存链路完成入库。"""
        try:
            result = await approve_review_item_record(
                review_id,
                config=config,
                embedder=embedder,
                vector_store=vector_store,
                feishu_doc_manager=feishu_doc_manager,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
            )
            return [TextContent(type="text", text=result.get("text") or "✅ 审核已完成。")]
        except Exception as e:
            logger.error("approve_review_item 执行失败: %s", e, exc_info=True)
            return [TextContent(type="text", text=f"❌ 审批审核项失败：{_trim_error(e)}")]

    @app.tool()
    async def reject_review_item(review_id: str, reason: str = "") -> list[TextContent]:
        """驳回指定审核项，阻止候选知识继续入库。"""
        try:
            result = await reject_review_item_record(
                review_id,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
                reason=reason,
            )
            return [TextContent(type="text", text=result.get("text") or "🗑️ 审核项已驳回。")]
        except Exception as e:
            logger.error("reject_review_item 执行失败: %s", e, exc_info=True)
            return [TextContent(type="text", text=f"❌ 驳回审核项失败：{_trim_error(e)}")]

    @app.tool()
    async def batch_review_items_tool(action: str, review_ids: str, reason: str = "") -> list[TextContent]:
        """批量审批或驳回自动沉淀审核项。review_ids 支持 JSON 数组或逗号分隔字符串。"""
        try:
            parsed_ids = _parse_review_ids(review_ids)
            result = await batch_review_items(
                action=action,
                review_ids=parsed_ids,
                config=config,
                embedder=embedder,
                vector_store=vector_store,
                feishu_doc_manager=feishu_doc_manager,
                registry_store=registry_store,
                dashboard_logger=dashboard_logger,
                reason=reason,
            )
            text = (
                f"✅ 批量审核已执行。\n\n"
                f"- 动作：{result['action']}\n"
                f"- 总数：{result['total']}\n"
                f"- 成功：{result['success_count']}\n"
                f"- 失败：{result['failed_count']}\n\n"
                f"```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```"
            )
            return [TextContent(type="text", text=text)]
        except Exception as e:
            logger.error("batch_review_items_tool 执行失败: %s", e, exc_info=True)
            return [TextContent(type="text", text=f"❌ 批量审核失败：{_trim_error(e)}")]
