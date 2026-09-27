"""主窗口。

布局：顶部文件选择 + 学位下拉 + 开始按钮；中部进度条 + 状态提示；下部导师结果列表。
风格按「简洁原生」处理：只用 Qt 原生控件 + 一层轻量 QSS，不引主题框架。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import app.config as config
from app.data.db import Database
from app.gui.cards import DetailDialog, SupervisorCard
from app.gui.settings_dialog import SettingsDialog
from app.gui.worker import PipelineWorker

DEGREES = ["硕士", "博士"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._cfg = config.load()
        config.ensure_dirs(self._cfg)
        self._db = Database(config.db_path(self._cfg))
        self._pdf_path = ""
        self._results: list = []
        self._worker: PipelineWorker | None = None
        #: 是否有流水线在跑。用自己维护的标记而不是 worker.isRunning()：
        #: 线程退出与信号投递之间有窗口期，isRunning() 会提前变 False，按钮就会错位。
        self._running = False
        #: 已点过取消，等待线程退出。此时忽略迟到的进度信号，避免状态提示被刷回去。
        self._cancelling = False

        self.setWindowTitle("导师匹配助手")
        self.resize(1000, 780)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        self._build_toolbar(root)
        self._build_progress(root)
        root.addWidget(self._build_results_area(), 1)

        self._update_status_bar()
        self._render_results()

    # ---------- 界面搭建 ----------

    def _build_toolbar(self, root: QVBoxLayout) -> None:
        bar = QHBoxLayout()

        self.choose_button = QPushButton("选择 PDF…")
        self.choose_button.clicked.connect(self._choose_pdf)
        bar.addWidget(self.choose_button)

        self.file_label = QLabel("未选择文件（简历 / Research Plan）")
        self.file_label.setStyleSheet("color:#57606a;")
        bar.addWidget(self.file_label, 1)

        bar.addWidget(QLabel("学位"))
        self.degree_box = QComboBox()
        self.degree_box.addItems(DEGREES)
        self.degree_box.currentIndexChanged.connect(self._on_degree_changed)
        bar.addWidget(self.degree_box)

        self.start_button = QPushButton("开始匹配")
        self.start_button.clicked.connect(self._start)
        bar.addWidget(self.start_button)

        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self._open_settings)
        bar.addWidget(self.settings_button)

        root.addLayout(bar)

    def _set_running(self, running: bool) -> None:
        """运行中只允许点「取消」，其余入口一律禁用，避免中途改学位/开设置导致状态错乱。"""
        self._running = running
        self.choose_button.setEnabled(not running)
        self.degree_box.setEnabled(not running)
        self.settings_button.setEnabled(not running)
        self.start_button.setEnabled(True)
        self.start_button.setText("取消" if running else "开始匹配")

    def _build_progress(self, root: QVBoxLayout) -> None:
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.status_label = QLabel("就绪。上传 PDF、选好学位后点击「开始匹配」。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#57606a;")

        root.addWidget(self.progress)
        root.addWidget(self.status_label)

    def _build_results_area(self) -> QScrollArea:
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        container = QWidget()
        self.results_layout = QVBoxLayout(container)
        self.results_layout.setContentsMargins(0, 0, 8, 0)
        self.results_layout.setSpacing(8)
        self.results_layout.addStretch(1)
        self.scroll.setWidget(container)
        return self.scroll

    # ---------- 结果渲染 ----------

    def _clear_results(self) -> None:
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_results(self, results: list | None = None) -> None:
        if results is not None:
            self._results = results
        self._clear_results()

        if not self._results:
            hint = QLabel("尚无匹配结果。选择 PDF 与学位后点击「开始匹配」。")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color:#57606a; padding:32px;")
            self.results_layout.addWidget(hint)
            self.results_layout.addStretch(1)
            return

        for result in self._results:
            card = SupervisorCard(result)
            card.clicked.connect(self._show_detail)
            self.results_layout.addWidget(card)
        self.results_layout.addStretch(1)

    def _show_detail(self, result) -> None:
        DetailDialog(result, self).exec()

    # ---------- 交互 ----------

    def _choose_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 PDF 文件", "", "PDF 文件 (*.pdf)")
        if not path:
            return
        self._pdf_path = path
        self.file_label.setText(path)
        self.file_label.setStyleSheet("color:#1f2328;")
        self.status_label.setText("已选择文件，点击「开始匹配」启动校验与抓取。")

    def _on_degree_changed(self) -> None:
        if self._results:
            self._results = []
            self._render_results()
            self.status_label.setText("学位已切换，请重新点击「开始匹配」。（学位不同，候选导师集合不同）")

    def _update_status_bar(self) -> None:
        schools = self._cfg["crawler"]["schools"]
        self.statusBar().showMessage(
            f"本地导师库：{self._db.count_supervisors()} 位导师"
            f"（当前勾选 {len(schools)} 所院校内 {self._db.count_supervisors(schools)} 位，"
            f"只有这些会参与匹配）　|　数据库：{config.db_path(self._cfg)}"
        )

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._cfg, self._db, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self._update_status_bar()
            self.status_label.setText("设置已保存。存储路径的修改需重启应用生效。")

    # ---------- 流水线 ----------

    def _start(self) -> None:
        if self._running:
            self._cancel()
            return

        if not self._pdf_path:
            QMessageBox.warning(self, "提示", "请先选择要分析的 PDF 文件。")
            return

        self._worker = PipelineWorker(
            self._db, self._cfg, self._pdf_path, self.degree_box.currentText(), self
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.pdf_failed.connect(self._on_pdf_failed)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.finished.connect(self._on_worker_done)

        self.progress.setValue(0)
        self._cancelling = False
        self._set_running(True)
        self.status_label.setText("已启动，正在校验 PDF…")
        self._worker.start()

    def _cancel(self) -> None:
        """点取消：立刻给出反馈并锁住按钮，防止连点。"""
        if self._worker is None or self._cancelling:
            return
        self._cancelling = True
        self.start_button.setText("正在取消…")
        self.start_button.setEnabled(False)
        self.status_label.setText("正在取消，请稍候…")
        self._worker.cancel()

    def _on_progress(self, percent: int, message: str) -> None:
        # 取消后不再刷新进度条，否则「正在取消…」会被迟到的进度文案盖掉
        if self._cancelling:
            return
        self.progress.setValue(percent)
        self.status_label.setText(message)

    def _on_pdf_failed(self, reason: str) -> None:
        self.status_label.setText(f"PDF 校验未通过：{reason}")
        QMessageBox.warning(self, "PDF 校验未通过", reason)

    def _on_finished(self, results: list, stats: dict) -> None:
        self._render_results(results)
        self._update_status_bar()
        QMessageBox.information(
            self,
            "匹配完成",
            f"共评估 {len(results)} 位导师。\n"
            f"本轮新增/更新 {stats.get('saved', 0)} 位，"
            f"其中招生类型匹配的有 {stats.get('matched', 0)} 位。\n"
            "结果已按匹配度从高到低排序，点击卡片查看详情。",
        )

    def _on_failed(self, message: str) -> None:
        self.status_label.setText(message)
        QMessageBox.warning(self, "任务未完成", message)

    def _on_cancelled(self) -> None:
        """用户主动取消：只更新状态，不弹报错框。"""
        self.progress.setValue(0)
        self.status_label.setText("已取消。本地已抓到的导师记录会保留，下次可继续。")

    def _on_worker_done(self) -> None:
        self._set_running(False)
        self._cancelling = False

    def closeEvent(self, event) -> None:
        if self._running and self._worker is not None:
            self._worker.cancel()
            if not self._worker.wait(8000):
                # 线程还在跑，此时关掉数据库连接会让后台线程写到已关闭的连接上
                QMessageBox.warning(
                    self, "正在取消", "后台任务还在退出，请稍候一秒钟再关闭窗口。"
                )
                event.ignore()
                return
        self._db.close()
        super().closeEvent(event)
