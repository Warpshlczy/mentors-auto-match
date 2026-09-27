"""跨层共享的数据模型：纯 dataclass，不依赖 PyQt6，也不依赖 sqlite3。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Supervisor:
    """一位导师。homepage 是增量去重的唯一键。"""

    name: str
    university: str
    department: str = ""
    title: str = ""
    homepage: str = ""
    #: CSRankings 提供的 ORCID，用于精确检索论文（同名作者很常见）
    orcid: str = ""
    research_areas: list[str] = field(default_factory=list)
    papers: list[str] = field(default_factory=list)
    # 招生类型，取值 "硕士" / "博士"；空列表表示主页未标注
    degree_types: list[str] = field(default_factory=list)
    source: str = ""
    id: int | None = None

    def profile_text(self) -> str:
        """导师文本画像，供匹配引擎打分。"""
        parts = [self.name, self.university, self.department, self.title]
        parts.extend(self.research_areas)
        parts.extend(self.papers)
        return " ".join(p for p in parts if p)


@dataclass
class MatchResult:
    supervisor: Supervisor
    score: int  # 0-100
    level: str  # 高 / 中 / 低
    reason: str
    overlap: list[str] = field(default_factory=list)
    fit_points: list[str] = field(default_factory=list)
    mode: str = "offline"  # offline / llm
