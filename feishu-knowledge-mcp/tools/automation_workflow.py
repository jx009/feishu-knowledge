import hashlib
import json
import logging
from typing import Any, Dict, List

from mcp.types import TextContent

from knowledge.card import SkillCard, extract_content_from_markdown
from knowledge.extractor import RuleBasedSkillExtractor
from tools.automation_review import (
    _build_merged_content,
    _decide_review_governance_target,
    _merge_into_existing_skill,
)
from tools.save_skill import persist_skill_card

logger = logging.getLogger(__name__)


def _trim_error(message: Exception | str, limit: int = 300) -> str:
    return str(message)[:limit]


def _normalize_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _tokenize_keywords(text: str, max_keywords: int = 8) -> List[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []

    keywords: List[str] = []
    for token in normalized.replace("\n", " ").replace("，", " ").replace(",", " ").split():
        clean = token.strip().strip("：:()[]{}<>。.!?;；、")
        if len(clean) < 2:
            continue
        if clean not in keywords:
            keywords.append(clean)
        if len(keywords) >= max_keywords:
            break
    return keywords


def _normalize_query(goal: str, summary: str = "", decisions: str = "") -> str:
    parts = [part.strip() for part in [goal, summary, decisions] if str(part or "").strip()]
    return "；".join(parts[:3])


def _build_session_id(*parts: str) -> str:
    raw = "||".join(str(part or "").strip() for part in parts if str(part or "").strip())
    if not raw:
        raw = "automation-session"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"auto_session_{digest}"


def _build_review_id(session_id: str, candidate_title: str, index: int) -> str:
    raw = f"{session_id}:{candidate_title}:{index}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"review_{digest}"


def _serialize_candidate(candidate) -> Dict[str, Any]:
    if hasattr(candidate, "to_dict"):
        return candidate.to_dict()
    return dict(candidate)


def _format_retrieval_context(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "📭 未召回到相关历史知识。"

    blocks = [f"✅ 自动召回 **{len(results)}** 条历史知识：", ""]
    for index, item in enumerate(results, 1):
        metadata = item.get("metadata", {}) or {}
        title = metadata.get("title") or item.get("skill_id") or f"知识 {index}"
        category = metadata.get("category") or "未分类"
        project = metadata.get("project") or "未关联项目"
        score = float(item.get("score") or 0.0)
        url = metadata.get("feishu_doc_url") or ""
        document = (item.get("document") or "").strip()
        preview = document[:220] + ("..." if len(document) > 220 else "")

        blocks.append(f"### {index}. {title}")
        blocks.append(f"- 分类：{category}")
        blocks.append(f"- 项目：{project}")
        blocks.append(f"- 相似度：{score:.3f}")
        if url:
            blocks.append(f"- 飞书链接：{url}")
        if preview:
            blocks.append(f"- 核心摘要：{preview}")
        blocks.append("")
    return "\n".join(blocks).rstrip()


def _build_candidate_digest(candidate: Dict[str, Any], index: int) -> str:
    title = candidate.get("title") or f"候选知识 {index}"
    category = candidate.get("category") or "未知"
    confidence = candidate.get("confidence") or "low"
    score = candidate.get("score") or 0
    excerpt = candidate.get("excerpt") or ""
    return f"### {index}. {title}\n- 分类：{category}\n- 置信度：{confidence}\n- 评分：{score}\n- 摘要：{excerpt}".rstrip()


async def _run_vector_search(embedder, vector_store, query: str, project: str, top_k: int) -> List[Dict[str, Any]]:
    query_embedding = embedder.encode(query.strip())
    filters = {}
    if project:
        filters["project"] = project
    return await vector_store.search(
        query_vector=query_embedding,
        filter_conditions=filters if filters else None,
        top_k=top_k,
        active_only=True,
    )


def register_automation_workflow(
    app,
    config,
    embedder,
    vector_store,
    feishu_doc_manager,
    registry_store,
    dashboard_logger=None,
):
    automation_config = (config.get("automation") or {}) if isinstance(config, dict) else {}
    extractor = RuleBasedSkillExtractor(config)
    auto_enabled = bool(automation_config.get("enabled", True))
    auto_retrieve_enabled = bool(automation_config.get("auto_retrieve_enabled", True))
    auto_extract_enabled = bool(automation_config.get("auto_extract_enabled", True))
    auto_save_enabled = bool(automation_config.get("auto_save_enabled", True))
    retrieval_top_k = max(1, int(automation_config.get("retrieval_top_k", 5) or 5))
    high_confidence_score = int(automation_config.get("high_confidence_score", 10) or 10)
    medium_confidence_score = int(automation_config.get("medium_confidence_score", 6) or 6)
    max_auto_save_items = max(1, int(automation_config.get("max_auto_save_items", 3) or 3))
    max_review_queue_items = max(1, int(automation_config.get("max_review_queue_items", 5) or 5))

    @app.tool()
    async def start_auto_session(
        user_goal: str,
        project: str = "",
        top_k: int = 0,
    ) -> list[TextContent]:
        """任务开始时自动检索相关历史知识，并生成可直接注入上下文的知识包。"""
        if not auto_enabled or not auto_retrieve_enabled:
            return [TextContent(type="text", text="⚠️ 自动检索当前未启用。")]

        normalized_goal = _normalize_text(user_goal)
        if not normalized_goal:
            return [TextContent(type="text", text="❌ 请输入任务目标或问题描述。")]

        actual_top_k = top_k if top_k > 0 else retrieval_top_k
        actual_top_k = min(max(1, actual_top_k), 20)
        session_id = _build_session_id(project, normalized_goal)
        keywords = _tokenize_keywords(normalized_goal)
        normalized_query = _normalize_query(normalized_goal)

        await registry_store.upsert_automation_session(
            {
                "session_id": session_id,
                "project": project,
                "user_goal": normalized_goal,
                "raw_query": normalized_goal,
                "normalized_query": normalized_query,
                "keywords": keywords,
                "retrieval_status": "running",
                "extraction_status": "pending",
                "save_status": "pending",
                "auto_retrieval_count": 0,
                "extracted_candidates": 0,
                "auto_saved_count": 0,
                "review_queued_count": 0,
                "discarded_count": 0,
                "saved_skill_ids": [],
                "last_error": None,
            }
        )

        try:
            queries = [normalized_goal]
            if normalized_query and normalized_query not in queries:
                queries.append(normalized_query)
            if keywords:
                keyword_query = " ".join(keywords[:5])
                if keyword_query not in queries:
                    queries.append(keyword_query)

            result_map: Dict[str, Dict[str, Any]] = {}
            for query in queries[:3]:
                hits = await _run_vector_search(embedder, vector_store, query, project.strip(), actual_top_k)
                for hit in hits:
                    skill_id = hit.get("skill_id") or hit.get("id") or ""
                    if not skill_id:
                        continue
                    existing = result_map.get(skill_id)
                    if existing is None or float(hit.get("score") or 0.0) > float(existing.get("score") or 0.0):
                        result_map[skill_id] = hit

            results = sorted(
                result_map.values(),
                key=lambda item: float(item.get("score") or 0.0),
                reverse=True,
            )[:actual_top_k]

            await registry_store.upsert_automation_session(
                {
                    "session_id": session_id,
                    "project": project,
                    "user_goal": normalized_goal,
                    "raw_query": normalized_goal,
                    "normalized_query": normalized_query,
                    "keywords": keywords,
                    "retrieval_status": "success",
                    "auto_retrieval_count": len(results),
                    "last_error": None,
                }
            )

            if dashboard_logger:
                try:
                    await dashboard_logger.log_automation(
                        session_id=session_id,
                        stage="retrieve",
                        status="success",
                        project=project,
                        query=normalized_goal,
                        content_preview=normalized_goal,
                        result_count=len(results),
                        top_score=float(results[0].get("score") or 0.0) if results else 0.0,
                    )
                except Exception as log_error:
                    logger.warning("自动检索日志记录失败: %s", log_error)

            payload = {
                "session_id": session_id,
                "project": project,
                "query": normalized_goal,
                "normalized_query": normalized_query,
                "keywords": keywords,
                "retrieval_count": len(results),
                "results": [
                    {
                        "skill_id": item.get("skill_id") or item.get("id"),
                        "score": float(item.get("score") or 0.0),
                        "title": (item.get("metadata") or {}).get("title", ""),
                        "category": (item.get("metadata") or {}).get("category", ""),
                        "project": (item.get("metadata") or {}).get("project", ""),
                        "feishu_doc_url": (item.get("metadata") or {}).get("feishu_doc_url", ""),
                        "excerpt": (item.get("document") or "")[:220],
                    }
                    for item in results
                ],
            }
            text = (
                f"🧠 自动会话已启动\n\n"
                f"🆔 **会话ID**：{session_id}\n"
                f"🏷️ **项目**：{project or '未指定'}\n"
                f"🔎 **召回数量**：{len(results)}\n\n"
                f"{_format_retrieval_context(results)}\n\n"
                f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
            )
            return [TextContent(type="text", text=text)]

        except Exception as e:
            logger.error("start_auto_session 执行失败: %s", e, exc_info=True)
            await registry_store.upsert_automation_session(
                {
                    "session_id": session_id,
                    "project": project,
                    "user_goal": normalized_goal,
                    "raw_query": normalized_goal,
                    "normalized_query": normalized_query,
                    "keywords": keywords,
                    "retrieval_status": "failed",
                    "last_error": _trim_error(e),
                }
            )
            if dashboard_logger:
                try:
                    await dashboard_logger.log_automation(
                        session_id=session_id,
                        stage="retrieve",
                        status="failed",
                        project=project,
                        query=normalized_goal,
                        content_preview=normalized_goal,
                        error=str(e),
                    )
                except Exception:
                    pass
            return [TextContent(type="text", text=f"❌ 自动检索失败：{_trim_error(e)}")]

    @app.tool()
    async def finish_auto_session(
        session_id: str,
        user_goal: str,
        conversation_summary: str,
        project: str = "",
        tool_summary: str = "",
        code_change_summary: str = "",
        decisions: str = "",
        errors_and_fixes: str = "",
        final_conclusion: str = "",
        auto_save: bool = True,
    ) -> list[TextContent]:
        """任务结束时自动提取候选知识，并自动保存高置信度知识。"""
        if not auto_enabled or not auto_extract_enabled:
            return [TextContent(type="text", text="⚠️ 自动沉淀当前未启用。")]

        normalized_summary = _normalize_text(conversation_summary)
        normalized_goal = _normalize_text(user_goal)
        clean_session_id = session_id.strip()
        if not clean_session_id:
            return [TextContent(type="text", text="❌ 请输入有效的会话ID。")]
        if not normalized_summary:
            return [TextContent(type="text", text="❌ 请输入对话总结或任务复盘内容。")]

        existing_session = await registry_store.get_automation_session(clean_session_id)
        project_name = project.strip() or (existing_session or {}).get("project", "")
        normalized_query = _normalize_query(normalized_goal, normalized_summary, decisions)
        keywords = _tokenize_keywords(" ".join([normalized_goal, normalized_summary, decisions]))
        source_blocks = [
            f"用户目标：{normalized_goal}" if normalized_goal else "",
            f"对话摘要：{normalized_summary}",
            f"工具操作摘要：{_normalize_text(tool_summary)}" if _normalize_text(tool_summary) else "",
            f"代码/配置修改摘要：{_normalize_text(code_change_summary)}" if _normalize_text(code_change_summary) else "",
            f"关键决策：{_normalize_text(decisions)}" if _normalize_text(decisions) else "",
            f"错误与修复：{_normalize_text(errors_and_fixes)}" if _normalize_text(errors_and_fixes) else "",
            f"最终结论：{_normalize_text(final_conclusion)}" if _normalize_text(final_conclusion) else "",
        ]
        source_text = "\n\n".join(block for block in source_blocks if block)

        await registry_store.upsert_automation_session(
            {
                "session_id": clean_session_id,
                "project": project_name,
                "user_goal": normalized_goal or (existing_session or {}).get("user_goal", ""),
                "raw_query": normalized_goal or normalized_summary,
                "normalized_query": normalized_query,
                "keywords": keywords,
                "retrieval_status": (existing_session or {}).get("retrieval_status", "pending"),
                "extraction_status": "running",
                "save_status": "running" if auto_save and auto_save_enabled else "skipped",
                "last_error": None,
            }
        )

        try:
            candidates = extractor.extract(
                text=source_text,
                project=project_name,
                top_k=max(max_auto_save_items + max_review_queue_items, 5),
            )

            serialized_candidates = [_serialize_candidate(candidate) for candidate in candidates]
            high_confidence = [item for item in serialized_candidates if int(item.get("score") or 0) >= high_confidence_score]
            medium_confidence = [
                item for item in serialized_candidates
                if medium_confidence_score <= int(item.get("score") or 0) < high_confidence_score
            ]
            low_confidence = [item for item in serialized_candidates if int(item.get("score") or 0) < medium_confidence_score]

            saved_skill_ids: List[str] = []
            auto_saved_summaries: List[str] = []
            review_ids: List[str] = []
            save_errors: List[str] = []

            if dashboard_logger:
                try:
                    await dashboard_logger.log_automation(
                        session_id=clean_session_id,
                        stage="extract",
                        status="success",
                        project=project_name,
                        query=normalized_goal,
                        content_preview=source_text,
                        result_count=len(serialized_candidates),
                        top_score=float(serialized_candidates[0].get("score") or 0.0) if serialized_candidates else 0.0,
                    )
                except Exception as log_error:
                    logger.warning("自动提取日志记录失败: %s", log_error)

            if auto_save and auto_save_enabled:
                for candidate in high_confidence[:max_auto_save_items]:
                    try:
                        candidate_payload = {
                            "session_id": clean_session_id,
                            "title": candidate.get("title") or "未命名知识",
                            "category": candidate.get("category") or "最佳实践",
                            "project": candidate.get("project") or project_name,
                            "tags": candidate.get("tags") or [],
                            "excerpt": candidate.get("excerpt") or "",
                            "draft_content": candidate.get("draft_content") or "",
                            "reasons": candidate.get("reasons") or [],
                            "source_text": source_text,
                        }
                        governance_target = await _decide_review_governance_target(
                            review_item=candidate_payload,
                            registry_store=registry_store,
                            config=config,
                            embedder=embedder,
                            vector_store=vector_store,
                        )

                        governance_action = governance_target["decision"]
                        if governance_action == "reuse_existing":
                            related_skill_id = governance_target["target"].get("skill_id") or ""
                            card = SkillCard.from_registry(governance_target["target"], governance_target["content"])
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
                            merged_content = _build_merged_content(existing_content, governance_target["content"], candidate_payload)
                            save_result = await _merge_into_existing_skill(
                                target_record=target_record,
                                review_item=candidate_payload,
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
                                title=candidate.get("title") or "未命名知识",
                                content=candidate.get("draft_content") or candidate.get("excerpt") or source_text,
                                category=candidate.get("category") or "最佳实践",
                                project=candidate.get("project") or project_name,
                                tags=candidate.get("tags") or [],
                                source="auto_session",
                                config=config,
                                embedder=embedder,
                                vector_store=vector_store,
                                feishu_doc_manager=feishu_doc_manager,
                                registry_store=registry_store,
                                dashboard_logger=dashboard_logger,
                            )
                            card = save_result.get("card")
                            related_skill_id = getattr(card, "skill_id", "") if card is not None else ""

                        if card is not None:
                            final_skill_id = related_skill_id or card.skill_id
                            saved_skill_ids.append(final_skill_id)
                            auto_saved_summaries.append(
                                f"- {card.title}（{final_skill_id}，状态：{card.sync_status}，治理动作：{governance_action}）"
                            )
                    except Exception as save_error:
                        save_errors.append(_trim_error(save_error))
                        logger.error("自动保存候选知识失败: %s", save_error, exc_info=True)

            for index, candidate in enumerate(medium_confidence[:max_review_queue_items], 1):
                candidate_title = str(candidate.get("title") or f"候选知识 {index}")
                review_id = _build_review_id(clean_session_id, candidate_title, index)
                candidate_payload = {
                    "session_id": clean_session_id,
                    "title": candidate_title,
                    "category": candidate.get("category") or "最佳实践",
                    "project": candidate.get("project") or project_name,
                    "tags": candidate.get("tags") or [],
                    "excerpt": candidate.get("excerpt") or "",
                    "draft_content": candidate.get("draft_content") or "",
                    "reasons": candidate.get("reasons") or [],
                    "source_text": source_text,
                }
                governance_target = await _decide_review_governance_target(
                    review_item=candidate_payload,
                    registry_store=registry_store,
                    config=config,
                    embedder=embedder,
                    vector_store=vector_store,
                )
                await registry_store.upsert_review_item(
                    {
                        "review_id": review_id,
                        "session_id": clean_session_id,
                        "title": candidate_title,
                        "category": candidate.get("category") or "最佳实践",
                        "project": candidate.get("project") or project_name,
                        "tags": candidate.get("tags") or [],
                        "excerpt": candidate.get("excerpt") or "",
                        "draft_content": candidate.get("draft_content") or "",
                        "reasons": candidate.get("reasons") or [],
                        "source_text": source_text,
                        "score": int(candidate.get("score") or 0),
                        "confidence": candidate.get("confidence") or "medium",
                        "status": "pending",
                        "auto_decision": governance_target.get("decision") or "review",
                        "related_skill_id": (governance_target.get("target") or {}).get("skill_id", ""),
                        "last_error": None,
                    }
                )
                review_ids.append(review_id)

            extraction_status = "success"
            save_status = "success"
            last_error = None
            if save_errors:
                save_status = "failed"
                last_error = "；".join(save_errors[:3])

            await registry_store.upsert_automation_session(
                {
                    "session_id": clean_session_id,
                    "project": project_name,
                    "user_goal": normalized_goal or (existing_session or {}).get("user_goal", ""),
                    "raw_query": normalized_goal or normalized_summary,
                    "normalized_query": normalized_query,
                    "keywords": keywords,
                    "retrieval_status": (existing_session or {}).get("retrieval_status", "pending"),
                    "extraction_status": extraction_status,
                    "save_status": save_status if auto_save and auto_save_enabled else "skipped",
                    "extracted_candidates": len(serialized_candidates),
                    "auto_saved_count": len(saved_skill_ids),
                    "review_queued_count": len(review_ids),
                    "discarded_count": len(low_confidence),
                    "saved_skill_ids": saved_skill_ids,
                    "last_error": last_error,
                }
            )

            if dashboard_logger:
                try:
                    await dashboard_logger.log_automation(
                        session_id=clean_session_id,
                        stage="save",
                        status="success" if not save_errors else "failed",
                        project=project_name,
                        query=normalized_goal,
                        content_preview=source_text,
                        result_count=len(saved_skill_ids),
                        error="；".join(save_errors[:3]) if save_errors else None,
                    )
                except Exception:
                    pass

            blocks = [
                f"🤖 自动会话已完成：{clean_session_id}",
                "",
                f"- 自动提取候选：{len(serialized_candidates)} 条",
                f"- 自动保存成功：{len(saved_skill_ids)} 条",
                f"- 进入审核队列：{len(review_ids)} 条",
                f"- 已丢弃低价值候选：{len(low_confidence)} 条",
            ]

            if auto_saved_summaries:
                blocks.extend(["", "### 已自动保存", *auto_saved_summaries])

            if medium_confidence[:max_review_queue_items]:
                blocks.extend(["", "### 已进入审核队列"])
                for index, candidate in enumerate(medium_confidence[:max_review_queue_items], 1):
                    review_id = review_ids[index - 1] if index - 1 < len(review_ids) else ""
                    blocks.append(_build_candidate_digest(candidate, index))
                    blocks.append(f"- 审核ID：{review_id}")
                    blocks.append("")

            if save_errors:
                blocks.extend(["", "### 自动保存异常"])
                blocks.extend([f"- {error}" for error in save_errors[:5]])

            payload = {
                "session_id": clean_session_id,
                "project": project_name,
                "extracted_candidates": len(serialized_candidates),
                "auto_saved_count": len(saved_skill_ids),
                "review_queued_count": len(review_ids),
                "discarded_count": len(low_confidence),
                "saved_skill_ids": saved_skill_ids,
                "review_ids": review_ids,
                "high_confidence_threshold": high_confidence_score,
                "medium_confidence_threshold": medium_confidence_score,
            }
            blocks.extend(["", "```json", json.dumps(payload, ensure_ascii=False, indent=2), "```"])
            return [TextContent(type="text", text="\n".join(blocks).rstrip())]

        except Exception as e:
            logger.error("finish_auto_session 执行失败: %s", e, exc_info=True)
            await registry_store.upsert_automation_session(
                {
                    "session_id": clean_session_id,
                    "project": project_name,
                    "user_goal": normalized_goal or (existing_session or {}).get("user_goal", ""),
                    "raw_query": normalized_goal or normalized_summary,
                    "normalized_query": normalized_query,
                    "keywords": keywords,
                    "retrieval_status": (existing_session or {}).get("retrieval_status", "pending"),
                    "extraction_status": "failed",
                    "save_status": "failed" if auto_save and auto_save_enabled else "skipped",
                    "last_error": _trim_error(e),
                }
            )
            if dashboard_logger:
                try:
                    await dashboard_logger.log_automation(
                        session_id=clean_session_id,
                        stage="extract",
                        status="failed",
                        project=project_name,
                        query=normalized_goal,
                        content_preview=source_text,
                        error=str(e),
                    )
                except Exception:
                    pass
            return [TextContent(type="text", text=f"❌ 自动沉淀失败：{_trim_error(e)}")]
