from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .card import CATEGORIES, parse_tags
from .categorizer import CATEGORY_TREE

GENERIC_STOPWORDS = {
    "以及", "然后", "因为", "所以", "如果", "目前", "当前", "已经", "我们", "你们", "他们",
    "这个", "那个", "这些", "那些", "这里", "那里", "进行", "一个", "一些", "可以", "需要",
    "就是", "还是", "并且", "但是", "为了", "同时", "相关", "实现", "问题", "方案", "功能",
    "模块", "接口", "记录", "阶段", "开发", "处理", "支持", "通过", "使用", "主要", "完成",
    "优化", "设计", "配置", "流程", "知识", "卡片", "自动", "提取", "候选", "系统",
    "service", "server", "project", "feature", "issue", "task", "data", "code",
}

CATEGORY_SIGNAL_WORDS = {
    "架构方案": [
        "架构", "设计", "选型", "分层", "模块", "链路", "流程", "治理", "方案", "依赖", "接口",
        "注册表", "同步闭环", "统一口径", "事实源", "可观测性",
    ],
    "产品迭代": [
        "需求", "产品", "交互", "页面", "视图", "页签", "展示", "新增字段", "前端", "体验", "迭代",
    ],
    "优化沉淀": [
        "优化", "性能", "耗时", "吞吐", "并发", "缓存", "退避", "重试", "降本", "加速", "压缩",
    ],
    "避坑记录": [
        "踩坑", "报错", "异常", "失败", "故障", "排查", "修复", "回滚", "脏数据", "口径漂移", "bug",
        "问题定位", "不一致", "补偿",
    ],
    "最佳实践": [
        "最佳实践", "建议", "约定", "统一", "规范", "应该", "必须", "推荐", "原则", "范式",
    ],
    "工具使用": [
        "docker", "compose", "环境变量", "配置文件", "启动", "部署", "命令", "日志", "健康检查", "ready",
        "health", "脚本", "终端", "qdrant", "postgres", "feishu", "dashboard",
    ],
    "业务知识": [
        "业务", "领域", "规则", "口径", "状态", "分类", "项目", "运营", "监控", "指标", "数据口径",
    ],
}

CATEGORY_SECTION_TEMPLATES = {
    "架构方案": ("背景", "方案设计", "落地要点", "风险与注意事项"),
    "产品迭代": ("需求背景", "改动说明", "实现细节", "验收关注点"),
    "优化沉淀": ("问题背景", "优化思路", "实施方案", "收益与观察点"),
    "避坑记录": ("问题现象", "根因分析", "解决方案", "注意事项"),
    "最佳实践": ("适用场景", "推荐做法", "落地步骤", "注意事项"),
    "工具使用": ("使用场景", "配置要点", "操作步骤", "常见问题"),
    "业务知识": ("业务背景", "核心规则", "处理流程", "注意事项"),
}


@dataclass
class ExtractedSkillCandidate:
    title: str
    category: str
    project: str = ""
    tags: List[str] = field(default_factory=list)
    score: int = 0
    confidence: str = "low"
    reasons: List[str] = field(default_factory=list)
    excerpt: str = ""
    draft_content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "category": self.category,
            "project": self.project,
            "tags": self.tags,
            "score": self.score,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "excerpt": self.excerpt,
            "draft_content": self.draft_content,
        }


class RuleBasedSkillExtractor:
    """规则驱动的知识提取器。"""

    def __init__(self, config: dict | None = None):
        extraction_config = ((config or {}).get("extraction") or {}) if isinstance(config, dict) else {}
        self.enabled = bool(extraction_config.get("enabled", True))
        self.max_candidates = max(1, int(extraction_config.get("max_candidates", 5) or 5))
        self.min_score = max(1, int(extraction_config.get("min_score", 3) or 3))
        self.min_segment_length = max(40, int(extraction_config.get("min_segment_length", 120) or 120))
        self.max_excerpt_length = max(120, int(extraction_config.get("max_excerpt_length", 320) or 320))
        self.include_full_text_fallback = bool(extraction_config.get("include_full_text_fallback", True))

    def extract(self, text: str, project: str = "", top_k: int | None = None) -> List[ExtractedSkillCandidate]:
        if not self.enabled:
            return []

        normalized = self._normalize_text(text)
        if not normalized:
            return []

        limit = min(max(1, int(top_k or self.max_candidates)), self.max_candidates)
        segments = self._segment_text(normalized)
        candidates: List[ExtractedSkillCandidate] = []

        for segment in segments:
            candidate = self._build_candidate(segment, project=project.strip())
            if candidate and candidate.score >= self.min_score:
                candidates.append(candidate)

        if not candidates and self.include_full_text_fallback:
            fallback = self._build_candidate(normalized, project=project.strip())
            if fallback and fallback.score >= max(1, self.min_score - 1):
                candidates.append(fallback)

        deduped = self._dedupe_candidates(candidates)
        deduped.sort(key=lambda item: (item.score, len(item.draft_content), item.title), reverse=True)
        return deduped[:limit]

    def _normalize_text(self, text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _segment_text(self, text: str) -> List[str]:
        heading_sections = [part.strip() for part in re.split(r"\n(?=#{1,6}\s)", text) if part.strip()]
        raw_segments: List[str] = []

        for section in heading_sections or [text]:
            sub_parts = [part.strip() for part in re.split(r"\n{2,}", section) if part.strip()]
            if sub_parts:
                raw_segments.extend(sub_parts)
            else:
                raw_segments.append(section)

        segments: List[str] = []
        buffer = ""
        for part in raw_segments:
            if len(part) >= self.min_segment_length:
                segments.extend(self._chunk_if_needed(part))
                buffer = ""
                continue

            if not buffer:
                buffer = part
            else:
                merged = f"{buffer}\n\n{part}".strip()
                if len(merged) >= self.min_segment_length:
                    segments.extend(self._chunk_if_needed(merged))
                    buffer = ""
                else:
                    buffer = merged

        if buffer:
            segments.extend(self._chunk_if_needed(buffer))

        cleaned = []
        seen = set()
        for segment in segments:
            value = segment.strip()
            if not value:
                continue
            key = re.sub(r"\s+", " ", value)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(value)

        return cleaned or [text]

    def _chunk_if_needed(self, text: str) -> List[str]:
        if len(text) <= 1200:
            return [text.strip()]

        chunks: List[str] = []
        start = 0
        step = 900
        overlap = 120
        while start < len(text):
            end = min(len(text), start + step)
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
        return chunks

    def _build_candidate(self, segment: str, project: str = "") -> ExtractedSkillCandidate | None:
        if not segment.strip():
            return None

        category_scores, reasons = self._score_categories(segment)
        if not category_scores:
            return None

        category, score = max(category_scores.items(), key=lambda item: item[1])
        if category not in CATEGORIES:
            return None

        tags = self._extract_tags(segment, category)
        title = self._derive_title(segment, category, tags)
        confidence = self._score_to_confidence(score)
        excerpt = self._build_excerpt(segment)
        draft_content = self._build_draft_content(segment, category)

        return ExtractedSkillCandidate(
            title=title,
            category=category,
            project=project,
            tags=tags,
            score=score,
            confidence=confidence,
            reasons=reasons[:4],
            excerpt=excerpt,
            draft_content=draft_content,
        )

    def _score_categories(self, text: str) -> tuple[Dict[str, int], List[str]]:
        normalized = text.lower()
        scores: Dict[str, int] = {}
        reasons: List[str] = []

        for category, words in CATEGORY_SIGNAL_WORDS.items():
            score = 0
            matched_words: List[str] = []
            keyword_pool = list(CATEGORY_TREE.get(category, {}).get("keywords", [])) + list(words)
            for keyword in keyword_pool:
                keyword_text = str(keyword).strip().lower()
                if not keyword_text:
                    continue
                hit_count = normalized.count(keyword_text)
                if hit_count <= 0:
                    continue
                score += min(hit_count, 3)
                matched_words.append(keyword)

            if matched_words:
                unique_matched = list(dict.fromkeys(matched_words))
                if len(unique_matched) >= 2:
                    score += 2
                elif len(unique_matched) == 1:
                    score += 1
                reasons.append(f"命中{category}信号：{', '.join(unique_matched[:4])}")
                scores[category] = score

        structure_bonus = 0
        if re.search(r"(^|\n)#{1,6}\s", text):
            structure_bonus += 1
        if "```" in text or "`" in text:
            structure_bonus += 1
        if re.search(r"(^|\n)(\d+\.|- |• )", text):
            structure_bonus += 1
        if any(marker in text for marker in ["背景", "结论", "步骤", "原因", "方案", "修复", "验证", "注意事项"]):
            structure_bonus += 2

        if len(text) >= 200:
            structure_bonus += 1
        if len(text) >= 500:
            structure_bonus += 1

        boosted_scores = {
            category: value + structure_bonus
            for category, value in scores.items()
        }
        return boosted_scores, reasons

    def _extract_tags(self, text: str, category: str) -> List[str]:
        candidates: List[str] = []

        for matched in re.findall(r"`([^`]+)`", text):
            token = matched.strip()
            if 1 < len(token) <= 40:
                candidates.append(token)

        english_tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9_\-]{2,}\b", text)
        for token in english_tokens:
            lower = token.lower()
            if lower in GENERIC_STOPWORDS:
                continue
            candidates.append(token)

        chinese_phrases = re.findall(r"[\u4e00-\u9fff]{2,8}", text)
        for phrase in chinese_phrases:
            if phrase in GENERIC_STOPWORDS:
                continue
            if phrase in CATEGORY_SIGNAL_WORDS.get(category, []) or phrase in CATEGORY_TREE.get(category, {}).get("keywords", []):
                candidates.append(phrase)

        frequency = Counter()
        for item in candidates:
            clean = item.strip().strip("：:，,。.!?()[]{}")
            if not clean:
                continue
            if clean.lower() in GENERIC_STOPWORDS:
                continue
            if clean in CATEGORIES:
                continue
            frequency[clean] += 1

        most_common = [item for item, _ in frequency.most_common(6)]
        return parse_tags(most_common[:5])

    def _derive_title(self, text: str, category: str, tags: List[str]) -> str:
        lines = [self._clean_heading(line) for line in text.splitlines() if line.strip()]

        for line in lines:
            if 6 <= len(line) <= 40 and not line.startswith("📂") and not line.startswith("🆔"):
                if not any(line == prefix for prefix in ["背景", "结论", "步骤", "方案", "问题", "原因"]):
                    return line

        primary_tag = tags[0] if tags else "知识"
        category_templates = {
            "架构方案": f"{primary_tag} 方案设计与落地说明",
            "产品迭代": f"{primary_tag} 功能迭代说明",
            "优化沉淀": f"{primary_tag} 优化实践",
            "避坑记录": f"{primary_tag} 问题排查与修复记录",
            "最佳实践": f"{primary_tag} 最佳实践",
            "工具使用": f"{primary_tag} 使用指南",
            "业务知识": f"{primary_tag} 业务规则说明",
        }
        return category_templates.get(category, f"{primary_tag} 经验沉淀")

    def _clean_heading(self, text: str) -> str:
        value = re.sub(r"^#{1,6}\s*", "", text.strip())
        value = re.sub(r"^[-*\d.\s]+", "", value)
        return value.strip(" ：:-")

    def _score_to_confidence(self, score: int) -> str:
        if score >= 10:
            return "high"
        if score >= 6:
            return "medium"
        return "low"

    def _build_excerpt(self, text: str) -> str:
        flat = re.sub(r"\s+", " ", text).strip()
        if len(flat) <= self.max_excerpt_length:
            return flat
        return flat[: self.max_excerpt_length - 1].rstrip() + "…"

    def _build_draft_content(self, text: str, category: str) -> str:
        title_1, title_2, title_3, title_4 = CATEGORY_SECTION_TEMPLATES.get(
            category,
            ("背景", "关键结论", "实施说明", "注意事项"),
        )

        key_points = self._extract_key_points(text)
        context = key_points[:2] if key_points else [self._build_excerpt(text)]
        action_points = key_points[2:6] if len(key_points) > 2 else key_points[:4]

        if not action_points:
            action_points = [
                "补充更具体的操作步骤、关键配置和边界条件。",
                "明确该知识适用的模块、项目和运行前提。",
            ]

        caution_points = self._build_caution_points(text, category)
        body_lines = [
            f"## {title_1}",
            *[f"- {item}" for item in context],
            "",
            f"## {title_2}",
            *[f"- {item}" for item in action_points[:4]],
            "",
            f"## {title_3}",
            self._format_source_block(text),
            "",
            f"## {title_4}",
            *[f"- {item}" for item in caution_points],
        ]
        return "\n".join(body_lines).strip()

    def _extract_key_points(self, text: str) -> List[str]:
        raw_sentences = re.split(r"[\n。！？!?;；]+", text)
        candidates: List[str] = []
        for sentence in raw_sentences:
            clean = sentence.strip().strip("-•* ")
            if len(clean) < 12:
                continue
            if len(clean) > 120:
                clean = clean[:120].rstrip() + "…"

            if any(keyword in clean for keyword in [
                "问题", "原因", "方案", "修复", "优化", "统一", "新增", "支持", "失败", "补偿", "同步", "状态",
                "配置", "日志", "健康检查", "注册表", "向量", "飞书",
            ]):
                candidates.append(clean)

        deduped: List[str] = []
        seen = set()
        for item in candidates:
            key = re.sub(r"\s+", " ", item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:6]

    def _build_caution_points(self, text: str, category: str) -> List[str]:
        caution_points = []
        if "失败" in text or "异常" in text:
            caution_points.append("需要明确失败场景下的补偿路径与回滚策略。")
        if "配置" in text or "环境变量" in text:
            caution_points.append("上线前确认配置项、默认值和环境变量覆盖关系。")
        if "同步" in text or "状态" in text:
            caution_points.append("保持状态字段和 Dashboard 统计口径一致，避免出现展示与真实状态不一致。")
        if "日志" in text or "监控" in text:
            caution_points.append("保留足够的日志和监控信息，便于后续排查。")
        if not caution_points:
            caution_points.append(f"补充 {category} 场景下的边界条件、依赖关系和适用范围。")
        return caution_points[:3]

    def _format_source_block(self, text: str) -> str:
        snippet = text.strip()
        if len(snippet) > 800:
            snippet = snippet[:800].rstrip() + "…"
        return snippet

    def _dedupe_candidates(self, candidates: List[ExtractedSkillCandidate]) -> List[ExtractedSkillCandidate]:
        best_by_key: Dict[str, ExtractedSkillCandidate] = {}
        for candidate in candidates:
            key = f"{candidate.category}::{candidate.title}".strip().lower()
            existing = best_by_key.get(key)
            if existing is None or candidate.score > existing.score:
                best_by_key[key] = candidate
        return list(best_by_key.values())