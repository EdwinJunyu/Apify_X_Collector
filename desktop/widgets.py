import json
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView, QDialog,
    QDialogButtonBox, QFormLayout, QScrollArea, QPlainTextEdit, QCheckBox)


def label(text, name=None):
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    if name:
        item.setObjectName(name)
    return item


def button(text, callback=None, primary=False, danger=False):
    item = QPushButton(text)
    item.setAccessibleName(text)
    item.setProperty('primary', primary)
    item.setProperty('danger', danger)
    if callback:
        item.clicked.connect(callback)
    return item


class ConfirmDialog(QDialog):
    def __init__(self, parent, title, message, target=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.addWidget(label(message))
        self.input = QLineEdit()
        self.input.setAccessibleName('输入确认目标 ID')
        if target:
            layout.addWidget(label('请输入完整 ID：' + target))
            layout.addWidget(self.input)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText('确认')
        box.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        if target:
            box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self.input.textChanged.connect(lambda text: box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(text == target))


class EditorDialog(QDialog):
    submitted = Signal(dict)

    def __init__(self, parent, title, fields, help_text=''):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 620)
        self.setMinimumWidth(540)
        self.widgets = {}
        self.fields = fields
        self.busy = False
        outer = QVBoxLayout(self)
        outer.addWidget(label(help_text))
        self.error = label('', 'error')
        self.error.setAccessibleName('表单错误')
        self.error.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.error.hide()
        outer.addWidget(self.error)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        for name, title, kind, default in fields:
            if isinstance(kind, list):
                widget = QComboBox()
                widget.addItems(kind)
                widget.setCurrentText(str(default))
            elif kind == 'bool':
                widget = QCheckBox('启用')
                widget.setChecked(bool(default))
            elif kind == 'multiline':
                widget = QPlainTextEdit(str(default or ''))
                widget.setMaximumHeight(110)
            else:
                widget = QLineEdit(str(default or ''))
                if kind == 'secret':
                    widget.setEchoMode(QLineEdit.EchoMode.Password)
            widget.setAccessibleName(title)
            field_label = QLabel(title)
            field_label.setBuddy(widget)
            form.addRow(field_label, widget)
            if kind == 'secret':
                reveal = QCheckBox('显示 Token')
                reveal.setAccessibleName('显示或隐藏 Token')
                reveal.toggled.connect(lambda checked, field=widget: field.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
                form.addRow('', reveal)
            self.widgets[name] = widget
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.box.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        self.box.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.box.accepted.connect(self.submit)
        self.box.rejected.connect(self.reject)
        outer.addWidget(self.box)
        self.original = self.values()

    def values(self):
        values = {}
        for name, title, kind, default in self.fields:
            widget = self.widgets[name]
            if isinstance(widget, QComboBox):
                value = widget.currentText()
            elif isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QPlainTextEdit):
                value = widget.toPlainText()
            else:
                value = widget.text()
            values[name] = value
        return values

    def submit(self):
        if self.busy:
            return
        self.busy = True
        self.error.hide()
        self.box.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        self.box.button(QDialogButtonBox.StandardButton.Save).setText('正在保存…')
        self.submitted.emit(self.values())

    def failed(self, message):
        self.busy = False
        self.box.button(QDialogButtonBox.StandardButton.Save).setEnabled(True)
        self.box.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        self.error.setText('未保存：' + message + '。请检查字段后重试。')
        self.error.show()
        self.error.setFocus()

    def reject(self):
        if self.busy:
            return
        if self.values() != self.original:
            ask = ConfirmDialog(self, '放弃未保存的修改', '修改尚未保存。确认放弃？')
            if ask.exec() != QDialog.DialogCode.Accepted:
                return
        super().reject()


class DataPage(QWidget):
    requested = Signal()

    def __init__(self, title, subtitle, columns, filters=None):
        super().__init__()
        self.columns = columns
        self.rows = []
        self.offset = 0
        self.total = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.addWidget(label(title, 'pageTitle'))
        layout.addWidget(label(subtitle, 'subtitle'))
        self.actions = QHBoxLayout()
        layout.addLayout(self.actions)
        search_bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索当前数据集…')
        self.search.setAccessibleName(title + '搜索')
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.reset_query)
        search_bar.addWidget(self.search, 1)
        search_bar.addWidget(button('清除', self.search.clear))
        search_bar.addWidget(button('刷新', self.requested.emit))
        layout.addLayout(search_bar)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(250)
        self.debounce.timeout.connect(self.reset_query)
        self.search.textChanged.connect(lambda value: self.debounce.start() if value else self.reset_query())
        self.filter_widgets = {}
        if filters:
            row = QHBoxLayout()
            for key, title, options in filters:
                field = QComboBox() if options else QLineEdit()
                field.setAccessibleName(title)
                if options:
                    field.addItem(title + '：全部')
                    field.addItems(options)
                    field.currentTextChanged.connect(self.reset_query)
                else:
                    field.setPlaceholderText(title)
                    field.editingFinished.connect(self.reset_query)
                row.addWidget(field)
                self.filter_widgets[key] = field
            layout.addLayout(row)
        self.notice = label('正在读取…', 'notice')
        self.notice.setAccessibleName('表格状态')
        layout.addWidget(self.notice)
        self.table = QTableWidget(0, len(columns))
        self.table.setAccessibleName(title + '数据表')
        titles = []
        for column in columns:
            titles.append(column[1])
        self.table.setHorizontalHeaderLabels(titles)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for index, (key, title) in enumerate(columns):
            if key.endswith('_at') or key == 'valid_until':
                self.table.setColumnWidth(index, 190)
            elif key in ('job_id', 'media_id', 'account_id'):
                self.table.setColumnWidth(index, 170)
            elif key == 'post_id':
                self.table.setColumnWidth(index, 160)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        footer.addWidget(button('上一页', self.previous))
        self.counter = label('0 条')
        footer.addWidget(self.counter, 1)
        footer.addWidget(label('每页 100 条 · 列排序仅当前页'))
        footer.addWidget(button('下一页', self.next))
        layout.addLayout(footer)

    def query_args(self, kind):
        filters = {}
        for key, field in self.filter_widgets.items():
            if isinstance(field, QComboBox):
                value = field.currentText() if field.currentIndex() > 0 else ''
            else:
                value = field.text()
            if value and value != '全部':
                filters[key] = value
        return {'kind': kind, 'offset': self.offset, 'limit': 100, 'search': self.search.text(), 'filters': filters}

    def reset_query(self):
        self.offset = 0
        self.requested.emit()

    def previous(self):
        self.offset = max(0, self.offset - 100)
        self.requested.emit()

    def next(self):
        if self.offset + 100 < self.total:
            self.offset += 100
            self.requested.emit()

    def selected(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def populate(self, result):
        old = self.selected()
        self.rows = result['rows']
        self.total = result['total']
        if self.offset and self.offset >= self.total:
            self.offset = max(0, ((self.total - 1) // 100) * 100)
            self.requested.emit()
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            for col_index, (key, title) in enumerate(self.columns):
                value = row.get(key)
                text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value if value is not None else '—')
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col_index == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row_index, col_index, item)
            if old and row.get(self.columns[0][0]) == old.get(self.columns[0][0]):
                self.table.selectRow(row_index)
        self.table.setSortingEnabled(True)
        self.counter.setText(f'第 {self.offset // 100 + 1} 页 · 共 {self.total} 条')
        self.notice.setText('暂无数据。使用上方操作开始。' if not self.total else '已更新 · 双击一行查看详情')
