"""设置窗口：LLM 接口配置、爬虫院校范围、数据存储路径、清空本地数据。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import app.config as config
from app.services.llm import LLMClient
from app.services.sources import CSRankingsSource, HttpFetcher


def _row(*widgets) -> QWidget:
    """把多个控件塞进一行，便于放进 QFormLayout。"""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    return holder


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, db, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._db = db
        self.setWindowTitle("设置")
        self.resize(640, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_llm_tab(), "LLM 接口")
        tabs.addTab(self._build_crawler_tab(), "爬虫院校")
        tabs.addTab(self._build_storage_tab(), "数据存储")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------- LLM ----------

    def _build_llm_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        llm = self._cfg["llm"]

        self.llm_enabled = QCheckBox("启用 LLM 模式（未启用时自动使用离线 RapidFuzz 匹配）")
        self.llm_enabled.setChecked(bool(llm["enabled"]))
        form.addRow(self.llm_enabled)

        self.llm_base = QLineEdit(llm["base_url"])
        form.addRow("接口地址 base_url", self.llm_base)

        self.llm_key = QLineEdit(llm["api_key"])
        self.llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Key", self.llm_key)

        self.llm_model = QLineEdit(llm["model"])
        form.addRow("模型名 model", self.llm_model)

        self.llm_timeout = QSpinBox()
        self.llm_timeout.setRange(5, 600)
        self.llm_timeout.setValue(int(llm["timeout"]))
        form.addRow("单次请求超时（秒）", self.llm_timeout)

        self.llm_status = QLabel("")
        self.llm_status.setWordWrap(True)
        test_button = QPushButton("测试连接")
        test_button.clicked.connect(self._test_llm)
        form.addRow(test_button, self.llm_status)

        hint = QLabel(
            "本地 Ollama：base_url 填 http://localhost:11434/v1，Key 留空，例如 qwen2.5:7b。\n"
            "云端 OpenAI 兼容接口：base_url 填 https://api.openai.com/v1 并填入自己的 Key。\n"
            "启用后简历内容会发送到该接口；不想把简历发出去就用 Ollama。"
        )
        hint.setStyleSheet("color:#57606a;")
        hint.setWordWrap(True)
        form.addRow(hint)
        return page

    def _test_llm(self) -> None:
        self._collect()
        ok, message = LLMClient(self._cfg).check()
        self.llm_status.setText(message)
        self.llm_status.setStyleSheet("color:#1a7f37;" if ok else "color:#cf222e;")

    # ---------- 爬虫 ----------

    def _build_crawler_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        crawler = self._cfg["crawler"]

        self.school_search = QLineEdit()
        self.school_search.setPlaceholderText("搜索院校：可搜英文名、缩写或国家代码，如 Tsinghua / Illinois / CN")
        self.school_search.textChanged.connect(self._filter_schools)

        self.school_list = QListWidget()
        self.school_list.setMinimumHeight(260)
        self.school_list.setToolTip("勾选即纳入抓取范围；名称直接取自 CSRankings，不要在别处手抄。")
        self.school_list.itemChanged.connect(self._refresh_school_hint)
        self.school_hint = QLabel()
        self.school_hint.setWordWrap(True)
        self._load_schools()

        form.addRow("搜索", self.school_search)
        form.addRow("院校名单", self.school_list)
        form.addRow(self.school_hint)

        self.max_per_school = QComboBox()
        for value in (10, 20, 30, 50, 100):
            self.max_per_school.addItem(f"每校 {value} 位", value)
        self.max_per_school.addItem("每校全部导师", 0)
        # 配置文件里可能有下拉框没覆盖的历史值，补一项进去而不是把它改掉
        current = int(crawler.get("max_per_school", 30))
        index = self.max_per_school.findData(current)
        if index < 0:
            self.max_per_school.addItem(f"每校 {current} 位", current)
            index = self.max_per_school.count() - 1
        self.max_per_school.setCurrentIndex(index)
        self.max_per_school.setToolTip(
            "每所勾选院校最多抓多少位导师：按 CSRankings 名单均匀取样，不是只取姓名靠前的。\n"
            "选「每校全部导师」会把这所学校在 CSRankings 里收录的导师全部抓下来，\n"
            "大型院校（如 CMU）可能上百位，抓取与后续 LLM 评估都会明显变慢。"
        )
        form.addRow("每校导师数上限", self.max_per_school)

        self.min_interval = QDoubleSpinBox()
        self.min_interval.setRange(0.0, 30.0)
        self.min_interval.setSingleStep(0.5)
        self.min_interval.setSuffix(" 秒")
        self.min_interval.setValue(float(crawler["min_interval"]))

        self.max_interval = QDoubleSpinBox()
        self.max_interval.setRange(0.0, 30.0)
        self.max_interval.setSingleStep(0.5)
        self.max_interval.setSuffix(" 秒")
        self.max_interval.setValue(float(crawler["max_interval"]))
        form.addRow("请求间隔", _row(self.min_interval, QLabel("～"), self.max_interval))

        self.retries = QSpinBox()
        self.retries.setRange(1, 10)
        self.retries.setValue(int(crawler["retries"]))
        form.addRow("异常重试次数", self.retries)

        self.fetch_papers = QCheckBox("抓取论文（访问 OpenAlex，每位导师 1-2 次请求，明显变慢）")
        self.fetch_papers.setChecked(bool(crawler["fetch_papers"]))
        form.addRow(self.fetch_papers)

        self.paper_years = QSpinBox()
        self.paper_years.setRange(1, 20)
        self.paper_years.setSuffix(" 年")
        self.paper_years.setValue(int(crawler["paper_years"]))
        form.addRow("论文年限", self.paper_years)

        self.cache_ttl = QSpinBox()
        self.cache_ttl.setRange(0, 60)
        self.cache_ttl.setSuffix(" 天")
        self.cache_ttl.setValue(int(crawler["cache_ttl_days"]))
        form.addRow("CSRankings 数据缓存有效期", self.cache_ttl)
        return page

    def _available_schools(self) -> list[tuple[str, str]]:
        """读 CSRankings 的完整院校名单（磁盘缓存优先）。取不到就返回空列表。"""
        try:
            fetcher = HttpFetcher(min_interval=0.5, max_interval=1.0, retries=1, timeout=5)
            source = CSRankingsSource(
                fetcher,
                config.cache_dir(self._cfg),
                self._cfg["crawler"]["cache_ttl_days"],
            )
            return source.institutions()
        except Exception:
            # 设置窗口不该因为网络问题打不开，上面已有降级提示
            return []

    def _load_schools(self) -> None:
        """把完整院校名单填成可勾选列表，用户不用再手抄名字，也能看到匹配池里有什么。"""
        selected = [s.strip() for s in self._cfg["crawler"]["schools"] if s.strip()]
        entries = self._available_schools()
        countries = dict(entries)

        self.school_list.clear()
        # 已配置但不在 CSRankings 名单里的名字也要列出来，否则用户看不到自己为什么抓不到人
        for name in sorted(set(countries) | set(selected)):
            if name in countries:
                label = f"{name}　·　{countries[name]}"
            else:
                label = f"{name}　·　不在 CSRankings 名单中，该行抓不到任何导师"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked
            )
            self.school_list.addItem(item)

        self._schools_loaded_ok = bool(entries)
        if not entries:
            self.school_hint.setStyleSheet("color:#b35900;")
            self.school_hint.setText(
                "拿不到 CSRankings 院校名单（首次下载失败或网络不可用），"
                "这里只列出你已配置的院校。恢复网络后重新打开设置即可看到完整名单。"
            )
        else:
            self.school_hint.setStyleSheet("color:#57606a;")
        self._refresh_school_hint()

    def _checked_schools(self) -> list[str]:
        return [
            self.school_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.school_list.count())
            if self.school_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _refresh_school_hint(self) -> None:
        if not self._schools_loaded_ok:
            return
        self.school_hint.setText(
            f"匹配池共 {self.school_list.count()} 所可选，"
            f"已勾选 {len(self._checked_schools())} 所。名称原样取自 CSRankings 的 institutions.csv。"
        )

    def _filter_schools(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.school_list.count()):
            item = self.school_list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    # ---------- 数据存储 ----------

    def _build_storage_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.data_dir = QLineEdit(self._cfg["storage"]["data_dir"])
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_dir)
        layout.addWidget(QLabel("数据存储路径（数据库与缓存）"))
        row = QHBoxLayout()
        row.addWidget(self.data_dir, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        self.storage_info = QLabel()
        self.storage_info.setStyleSheet("color:#57606a;")
        self.storage_info.setWordWrap(True)
        layout.addWidget(self.storage_info)

        note = QLabel("修改存储路径后需要重启应用才会生效（数据库连接在启动时建立）。")
        note.setStyleSheet("color:#b35900;")
        note.setWordWrap(True)
        layout.addWidget(note)

        clear = QPushButton("清空本地数据")
        clear.clicked.connect(self._clear_data)
        layout.addWidget(clear)
        layout.addStretch(1)

        self._refresh_storage_info()
        return page

    def _refresh_storage_info(self) -> None:
        try:
            count = self._db.count_supervisors()
        except Exception:
            count = 0
        self.storage_info.setText(
            f"数据库文件：{config.db_path(self._cfg)}\n已入库导师：{count} 位\n"
            f"PDF 缓存目录：{config.cache_dir(self._cfg)}"
        )

    def _browse_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择数据存储目录", self.data_dir.text())
        if chosen:
            self.data_dir.setText(chosen)

    def _clear_data(self) -> None:
        confirm = QMessageBox.question(
            self,
            "确认清空",
            "将删除本地全部导师记录与 PDF 解析缓存，此操作不可撤销。确定继续？",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._db.clear_all()
        self._refresh_storage_info()
        QMessageBox.information(self, "已清空", "本地导师记录与 PDF 缓存已全部清除。")

    # ---------- 保存 ----------

    def _collect(self) -> None:
        """把控件值写回 cfg（原地修改，保证主窗口持有的同一份 dict 是最新的）。"""
        llm = self._cfg["llm"]
        llm["enabled"] = self.llm_enabled.isChecked()
        llm["base_url"] = self.llm_base.text().strip()
        llm["api_key"] = self.llm_key.text().strip()
        llm["model"] = self.llm_model.text().strip()
        llm["timeout"] = self.llm_timeout.value()

        crawler = self._cfg["crawler"]
        crawler["schools"] = self._checked_schools()
        crawler["max_per_school"] = int(self.max_per_school.currentData())
        crawler["min_interval"] = self.min_interval.value()
        crawler["max_interval"] = self.max_interval.value()
        crawler["retries"] = self.retries.value()
        crawler["fetch_papers"] = self.fetch_papers.isChecked()
        crawler["paper_years"] = self.paper_years.value()
        crawler["cache_ttl_days"] = self.cache_ttl.value()

        self._cfg["storage"]["data_dir"] = self.data_dir.text().strip()

    def _save(self) -> None:
        self._collect()
        crawler = self._cfg["crawler"]
        if crawler["min_interval"] > crawler["max_interval"]:
            QMessageBox.warning(self, "参数有误", "请求间隔的最小值不能大于最大值。")
            return
        if not crawler["schools"]:
            QMessageBox.warning(self, "参数有误", "请至少勾选一所院校（可用上方搜索框筛选）。")
            return
        try:
            config.ensure_dirs(self._cfg)
            config.save(self._cfg)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"无法写入配置文件：{exc}")
            return
        self.accept()
