"""导师卡片与详情弹窗（GUI 层，只消费 MatchResult，不碰数据库与网络）。"""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.data.models import MatchResult

LEVEL_COLORS = {"高": "#1a7f37", "中": "#9a6700", "低": "#6e7781"}

CARD_QSS = """
QFrame#SupervisorCard {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 8px;
}
QFrame#SupervisorCard:hover { border-color: #0969da; }
"""


def _esc(text: str) -> str:
    return html.escape(text or "")


class SupervisorCard(QFrame):
    """导师卡片：姓名、院校、职称、研究方向标签、匹配得分/等级、匹配简述。"""

    clicked = pyqtSignal(object)

    def __init__(self, result: MatchResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.result = result
        sup = result.supervisor

        self.setObjectName("SupervisorCard")
        self.setStyleSheet(CARD_QSS)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        head = QHBoxLayout()
        name = QLabel(f"<b>{_esc(sup.name)}</b>")
        name.setTextFormat(Qt.TextFormat.RichText)
        head.addWidget(name)
        head.addWidget(QLabel(_esc(sup.title)))
        head.addStretch(1)
        badge = QLabel(f"{result.score} 分 · {result.level}匹配")
        badge.setStyleSheet(
            f"color: {LEVEL_COLORS.get(result.level, '#6e7781')}; font-weight: bold;"
        )
        head.addWidget(badge)
        layout.addLayout(head)

        meta = QLabel(f"{_esc(sup.university)}　{_esc(sup.department)}")
        meta.setStyleSheet("color: #57606a;")
        layout.addWidget(meta)

        if sup.research_areas:
            areas = QLabel("　".join(f"#{_esc(area)}" for area in sup.research_areas[:6]))
            areas.setStyleSheet("color: #0969da;")
            areas.setWordWrap(True)
            layout.addWidget(areas)

        reason = QLabel(result.reason)
        reason.setWordWrap(True)
        reason.setStyleSheet("color: #57606a;")
        layout.addWidget(reason)

        if result.fit_points:
            points = QLabel("适配点：" + "；".join(_esc(p) for p in result.fit_points))
            points.setWordWrap(True)
            points.setStyleSheet("color: #57606a;")
            layout.addWidget(points)

    def mousePressEvent(self, event) -> None:
        self.clicked.emit(self.result)
        super().mousePressEvent(event)


class DetailDialog(QDialog):
    """点击卡片后的详情：完整研究方向、论文列表、主页链接。"""

    def __init__(self, result: MatchResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        sup = result.supervisor
        self.setWindowTitle(f"{sup.name} · {sup.university}")
        self.resize(680, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        header = QLabel(f"<b>{_esc(sup.name)}</b>　{_esc(sup.title)}")
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setStyleSheet("font-size: 15px;")
        layout.addWidget(header)

        meta = QLabel(f"{_esc(sup.university)}　{_esc(sup.department)}")
        meta.setStyleSheet("color: #57606a;")
        layout.addWidget(meta)

        degree = "、".join(sup.degree_types) if sup.degree_types else "主页未标注"
        degree_label = QLabel(f"招生类型：{_esc(degree)}")
        degree_label.setStyleSheet("color: #57606a;")
        layout.addWidget(degree_label)

        if sup.homepage:
            link = QLabel(f'主页：<a href="{_esc(sup.homepage)}">{_esc(sup.homepage)}</a>')
            link.setOpenExternalLinks(True)
            link.setWordWrap(True)
            link.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(link)

        score = QLabel(
            f"匹配结果：{result.score} 分（{result.level}）· 模式："
            f"{'LLM' if result.mode == 'llm' else '离线 RapidFuzz'}"
        )
        score.setStyleSheet(
            f"color: {LEVEL_COLORS.get(result.level, '#6e7781')}; font-weight: bold;"
        )
        layout.addWidget(score)

        reason = QLabel(f"匹配理由：{result.reason}")
        reason.setWordWrap(True)
        layout.addWidget(reason)

        if result.fit_points:
            points = QLabel("适配点：\n· " + "\n· ".join(result.fit_points))
            points.setWordWrap(True)
            layout.addWidget(points)

        if result.overlap:
            overlap = QLabel("重合关键词：" + "、".join(result.overlap))
            overlap.setWordWrap(True)
            layout.addWidget(overlap)

        layout.addWidget(QLabel("<b>研究方向</b>"))
        areas = QLabel(
            "、".join(sup.research_areas) if sup.research_areas else "未获取到研究方向"
        )
        areas.setWordWrap(True)
        layout.addWidget(areas)

        layout.addWidget(QLabel("<b>近年论文</b>"))
        papers = QListWidget()
        if sup.papers:
            papers.addItems(sup.papers)
        else:
            papers.addItem("未获取到论文数据（OpenAlex 未收录或网络不可用）")
        papers.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        layout.addWidget(papers, 1)

        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
