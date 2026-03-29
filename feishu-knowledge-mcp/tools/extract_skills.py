import json
import logging

from mcp.types import TextContent

from knowledge.extractor import RuleBasedSkillExtractor

logger = logging.getLogger(__name__)


def _format_candidate(candidate: dict, index: int) -> str:
    reasons = candidate.get("reasons") or []
    tags = candidate.get("tags") or []
    project = candidate.get("project") or "未指定"
    draft_content = candidate.get("draft_content") or ""

    lines = [
        f"### 候选知识 {index}",
        f"- 标题：{candidate.get('title', '未命名')}",
        f"- 分类：{candidate.get('category', '未知')}",
        f"- 项目：{project}",
        f"- 标签：{', '.join(tags) if tags else '无'}",
        f"- 置信度：{candidate.get('confidence', 'low')}",
        f"- 评分：{candidate.get('score', 0)}",
    ]

    if reasons:
        lines.append(f"- 识别依据：{'；'.join(reasons)}")
    if candidate.get("excerpt"):
        lines.append(f"- 摘要：{candidate['excerpt']}")

    if draft_content:
        lines.extend([
            "",
            "建议沉淀内容：",
            "```markdown",
            draft_content,
            "```",
        ])

    return "\n".join(lines)


def register_extract_skills(app, config, dashboard_logger=None):
    extractor = RuleBasedSkillExtractor(config)

    @app.tool()
    async def extract_skills(
        text: str,
        project: str = "",
        top_k: int = 3,
        output_format: str = "markdown",
    ) -> list[TextContent]:
        """从对话、复盘或需求文本中自动识别候选知识卡片。

        该工具不会直接写入飞书或向量库，而是先给出结构化候选结果，
        适合在完成开发、修复故障、形成最佳实践或沉淀流程经验后使用。
        """
        try:
            candidates = extractor.extract(text=text, project=project, top_k=top_k)

            if dashboard_logger:
                try:
                    await dashboard_logger.log_extract(
                        project=project,
                        source_text=text,
                        candidate_count=len(candidates),
                        status="success",
                    )
                except Exception as log_error:
                    logger.warning("extract_skills 日志记录失败: %s", log_error)

            if not candidates:
                return [TextContent(
                    type="text",
                    text=(
                        "📭 当前文本中未识别到足够明确的候选知识。\n\n"
                        "建议补充以下信息后再尝试：\n"
                        "- 背景 / 问题现象\n"
                        "- 根因 / 设计思路\n"
                        "- 解决步骤 / 配置要点\n"
                        "- 注意事项 / 边界条件"
                    ),
                )]

            output_format = (output_format or "markdown").strip().lower()
            if output_format == "json":
                payload = {
                    "count": len(candidates),
                    "candidates": [item.to_dict() for item in candidates],
                }
                return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]

            blocks = [
                f"✅ 共识别到 **{len(candidates)}** 条候选知识卡片。",
                "",
                "这些结果不会自动入库；如需正式沉淀，请将合适的候选结果交给 `save_skill`。",
                "",
            ]
            for index, candidate in enumerate(candidates, 1):
                blocks.append(_format_candidate(candidate.to_dict(), index))
                blocks.append("")

            return [TextContent(type="text", text="\n".join(blocks).rstrip())]

        except Exception as e:
            logger.error("extract_skills 执行失败: %s", e, exc_info=True)

            if dashboard_logger:
                try:
                    await dashboard_logger.log_extract(
                        project=project,
                        source_text=text,
                        candidate_count=0,
                        status="failed",
                        error=str(e),
                    )
                except Exception:
                    pass

            return [TextContent(type="text", text=f"❌ 自动知识提取失败：{str(e)[:300]}")]
