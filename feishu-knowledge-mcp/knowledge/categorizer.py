"""
知识分类器模块

定义知识分类体系和分类相关的工具方法。
"""

from typing import List, Optional

# 知识分类体系
CATEGORY_TREE = {
    "架构方案": {
        "description": "系统架构设计、技术选型方案",
        "icon": "🏗️",
        "keywords": ["架构", "设计", "选型", "方案", "系统设计"],
    },
    "产品迭代": {
        "description": "产品功能迭代记录、需求变更",
        "icon": "🚀",
        "keywords": ["产品", "迭代", "需求", "功能", "版本"],
    },
    "优化沉淀": {
        "description": "性能优化、代码优化、流程优化",
        "icon": "⚡",
        "keywords": ["优化", "性能", "提升", "改进", "重构"],
    },
    "避坑记录": {
        "description": "踩坑经验、Bug 修复方案",
        "icon": "⚠️",
        "keywords": ["踩坑", "Bug", "问题", "修复", "排查", "错误"],
    },
    "最佳实践": {
        "description": "编码规范、设计模式、工程实践",
        "icon": "✅",
        "keywords": ["最佳实践", "规范", "模式", "标准", "经验"],
    },
    "工具使用": {
        "description": "工具配置、环境搭建、命令速查",
        "icon": "🔧",
        "keywords": ["工具", "配置", "环境", "安装", "命令"],
    },
    "业务知识": {
        "description": "业务逻辑、领域知识",
        "icon": "📖",
        "keywords": ["业务", "逻辑", "领域", "流程", "规则"],
    },
}


def get_all_categories() -> List[str]:
    """获取所有支持的分类名称"""
    return list(CATEGORY_TREE.keys())


def is_valid_category(category: str) -> bool:
    """检查分类是否合法"""
    return category in CATEGORY_TREE


def get_category_description(category: str) -> Optional[str]:
    """获取分类的描述信息"""
    info = CATEGORY_TREE.get(category)
    return info["description"] if info else None


def get_category_icon(category: str) -> str:
    """获取分类的图标"""
    info = CATEGORY_TREE.get(category)
    return info["icon"] if info else "📄"


def format_categories_for_prompt() -> str:
    """
    格式化分类列表，用于 MCP 工具的描述文本
    让 AI 能清楚地了解每个分类的含义
    """
    lines = []
    for name, info in CATEGORY_TREE.items():
        lines.append(f"  - {info['icon']} {name}: {info['description']}")
    return "\n".join(lines)
