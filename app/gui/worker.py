"""后台流水线线程：PDF 校验 → 定向爬虫 → 匹配打分。

爬虫与匹配都在 QThread 内执行，UI 线程只接收信号，界面不会卡死。
"""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.data.db import Database
from app.services.crawler import Crawler
from app.services.llm import LLMClient
from app.services.matcher import Matcher
from app.services.pdf_checker import PdfChecker

# 三个阶段在总进度条上占的区间（百分比）
PDF_BAND = (0, 10)
CRAWL_BAND = (10, 70)
MATCH_BAND = (70, 100)


class PipelineWorker(QThread):
    progress = pyqtSignal(int, str)  # 总百分比, 状态文案
    pdf_failed = pyqtSignal(str)  # 校验不通过的具体原因
    finished_ok = pyqtSignal(object, object)  # list[MatchResult], 统计 dict
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()  # 用户主动取消：不是错误，界面不该弹报错框

    def __init__(self, db: Database, cfg: dict, pdf_path: str, degree: str, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._cfg = cfg
        self._pdf_path = pdf_path
        self._degree = degree
        self._stop = False

    def cancel(self) -> None:
        self._stop = True

    def _should_stop(self) -> bool:
        return self._stop

    def _emit_band(self, band: tuple[int, int], message: str, fraction: float | None) -> None:
        low, high = band
        ratio = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
        self.progress.emit(int(low + (high - low) * ratio), message)

    def run(self) -> None:  # noqa: C901 - 线性流水线，拆开反而更难读
        try:
            checker = PdfChecker(self._db)
            self._emit_band(PDF_BAND, "正在校验 PDF 有效内容…", 0.0)
            check = checker.check(self._pdf_path)
            if not check.ok:
                self.pdf_failed.emit(check.reason)
                return
            note = "，命中本地缓存，无需二次解析" if check.cached else ""
            self._emit_band(
                PDF_BAND,
                f"校验通过：{check.doc_type}，{check.char_count} 字 / {check.word_count} 词，"
                f"语义密度 {check.density}{note}",
                1.0,
            )

            self._emit_band(CRAWL_BAND, "准备启动定向爬虫…", 0.0)
            crawler = Crawler(
                self._db,
                self._cfg,
                on_progress=lambda message, fraction: self._emit_band(
                    CRAWL_BAND, message, fraction
                ),
                should_stop=self._should_stop,
            )
            stats = crawler.run(self._degree)
            if self._stop:
                self.cancelled.emit()
                return

            # 匹配池 = 勾选的院校（与抓取范围一致）。库里是累积的，不加这层过滤，
            # 勾选 3 所院校也会把以前爬过的十几所院校的导师一起送去打分。
            supervisors = self._db.list_supervisors(
                self._degree, self._cfg["crawler"]["schools"]
            )
            if not supervisors:
                self.failed.emit(
                    "本地数据库中没有当前勾选院校的导师。请到「设置」确认院校已勾选，"
                    "且名称与 CSRankings 完全一致（例如 Univ. of Illinois at Urbana-Champaign），"
                    "并确认网络可以访问 CSRankings 与 OpenAlex。"
                )
                return

            llm = LLMClient(self._cfg)
            if llm.available():
                self._emit_band(MATCH_BAND, f"使用 LLM 模式（{llm.model}）评估…", 0.0)
            else:
                self._emit_band(MATCH_BAND, "未启用 LLM，使用离线 RapidFuzz 匹配…", 0.0)

            matcher = Matcher(self._cfg, llm)
            results = matcher.match_all(
                check.text,
                supervisors,
                on_progress=lambda message, fraction: self._emit_band(
                    MATCH_BAND, message, fraction
                ),
                should_stop=self._should_stop,
            )
            if self._stop:
                self.cancelled.emit()
                return

            self.progress.emit(100, f"完成：共评估 {len(results)} 位导师，结果已按匹配度排序。")
            self.finished_ok.emit(results, stats)
        except Exception as exc:  # 后台线程不能让异常逃逸，否则界面进程直接挂掉
            self.failed.emit(f"运行出错：{type(exc).__name__}: {exc}")
