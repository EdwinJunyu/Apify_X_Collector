"""Native desktop client. All domain commands go to the separate Worker."""
import copy
import json
import uuid
from pathlib import Path
from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, Signal, QTimer, QUrl
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QDesktopServices
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QStackedWidget, QSystemTrayIcon, QMenu, QFrame, QDialog, QPlainTextEdit, QLabel)
from collector.validation import digest, utc_now
from collector.quota import GiB
from .widgets import DataPage, EditorDialog, ConfirmDialog, button, label
from .theme import stylesheet, TOKENS


class TaskSignals(QObject):
    finished = Signal(bool, object)


class CommandTask(QRunnable):
    def __init__(self, client, command, args, request_id):
        super().__init__()
        self.client, self.command, self.args, self.request_id = client, command, args, request_id
        self.signals = TaskSignals()

    def run(self):
        try:
            result = self.client.call(self.command, self.args, self.request_id)
            self.signals.finished.emit(True, result)
        except Exception as exc:
            self.signals.finished.emit(False, str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)


def app_icon(color=None):
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setBrush(QColor(color or TOKENS['primary']))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 44, 44, 9, 9)
    painter.setPen(QColor('white'))
    font = painter.font()
    font.setPixelSize(29)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, 'A')
    painter.end()
    return QIcon(pixmap)


class PreviewDialog(QDialog):
    def __init__(self, parent, task):
        super().__init__(parent)
        self.owner, self.task = parent, task
        self.setWindowTitle('授权媒体预览')
        self.resize(820, 640)
        self.layout = QVBoxLayout(self)
        self.note = label('正在核对权限…', 'notice')
        self.layout.addWidget(self.note)
        self.surface = QLabel()
        self.surface.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout.addWidget(self.surface, 1)
        self.player = None
        self.play = button('核验并播放', self.verify_play)
        self.play.hide()
        self.layout.addWidget(self.play)
        self.layout.addWidget(button('关闭预览', self.close))
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.verify)
        self.timer.start()
        self.loaded = False
        self.verify()

    def verify_play(self):
        self.owner.call('preview', {'media_id': self.task['media_id']}, lambda result: self.player.play(), self.denied)

    def verify(self):
        self.owner.call('preview', {'media_id': self.task['media_id']}, self.allowed, self.denied)

    def allowed(self, result):
        from shiboken6 import isValid
        if not isValid(self) or not self.isVisible():
            return
        self.note.setText('许可与内容分级已核验 · 每 2 秒重新核验')
        if self.loaded:
            return
        self.loaded = True
        if result['mime'].startswith('image/'):
            pixmap = QPixmap(result['path'])
            if pixmap.isNull():
                self.note.setText('格式识别通过，但 Qt 无法解码该图片。')
            else:
                self.surface.setPixmap(pixmap.scaled(760, 500, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            try:
                from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
                from PySide6.QtMultimediaWidgets import QVideoWidget
                self.player = QMediaPlayer(self)
                audio = QAudioOutput(self)
                self.player.setAudioOutput(audio)
                video = QVideoWidget(self)
                self.player.setVideoOutput(video)
                self.layout.replaceWidget(self.surface, video)
                self.surface.hide()
                self.player.setSource(QUrl.fromLocalFile(result['path']))
                self.play.show()  # deliberately no autoplay
            except ImportError:
                self.note.setText('缺少 Qt 视频组件，无法播放。')

    def denied(self, error):
        from shiboken6 import isValid
        if not isValid(self):
            return
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        self.surface.clear()
        self.timer.stop()
        self.note.setText('预览已停止：权限、分级或文件状态发生变化。')
        self.play.setEnabled(False)

    def closeEvent(self, event):
        self.timer.stop()
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, client, polling=True):
        super().__init__()
        self.client = client
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.tasks = set()
        self.last_snapshot = None
        self.status_pending = False
        self.query_pending = set()
        self.request_sequence = {}
        self.dialogs = []
        self.force_quit = False
        self.setWindowTitle('ACG Observatory · 数据采集管理台')
        self.setWindowIcon(app_icon())
        self.resize(1320, 860)
        self.setMinimumSize(960, 640)
        self.setStyleSheet(stylesheet())
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        side = QWidget()
        side.setObjectName('sidebar')
        side.setFixedWidth(210)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 8)
        side_layout.addWidget(label('ACG\nOBSERVATORY', 'brand'))
        side_layout.addWidget(label('采集 · 审核 · 积累', 'sidebarNote'))
        self.navigation = QListWidget()
        self.navigation.setObjectName('navigation')
        self.navigation.setAccessibleName('主导航')
        for text in ('总览 Dashboard', '画师账号 Accounts', '任务与 Runs', '帖子与审核', '媒体素材库', '预算与存储', '权限与合规', '备份与恢复', '设置与日志'):
            self.navigation.addItem(text)
        side_layout.addWidget(self.navigation, 1)
        side_layout.addWidget(label('本地工作区\nGUI 与 Worker 独立运行', 'sidebarNote'))
        layout.addWidget(side)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.pages = {}
        self.build_dashboard()
        self.build_data_pages()
        self.build_budget()
        self.build_policy()
        self.build_backup()
        self.build_logs()
        self.navigation.currentRowChanged.connect(self.navigate)
        self.navigation.setCurrentRow(0)
        self.statusBar().showMessage('正在连接 Worker…')
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu(self)
        for text, callback in (('打开管理窗口', self.open_window), ('开始一次手动检查', self.manual_check),
                               ('暂停新任务', lambda: self.action('pause')), ('恢复符合条件的任务', self.resume),
                               ('查看错误', lambda: self.show_page(8)), ('退出界面', self.quit_ui),
                               ('停止 Worker…', self.stop_worker)):
            menu.addAction(text, callback)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.open_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh_status)
        if polling:
            self.timer.start()
            QTimer.singleShot(0, self.refresh_status)

    def call(self, command, args=None, success=None, failure=None, request_id=None):
        task = CommandTask(self.client, command, args or {}, request_id or uuid.uuid4().hex)
        self.tasks.add(task)
        def complete(ok, result):
            self.tasks.discard(task)
            if ok:
                if success:
                    success(result)
            elif failure:
                failure(result)
            else:
                self.statusBar().showMessage('操作未完成：' + str(result) + '。请刷新后检查。')
        task.signals.finished.connect(complete)
        self.pool.start(task)

    def action(self, command, args=None):
        self.statusBar().showMessage('正在执行…')
        self.call(command, args, lambda result: self.action_done(command, result))

    def action_done(self, command, result):
        message = '操作已完成：' + command
        if isinstance(result, dict) and result.get('path'):
            message += ' · 文件：' + result['path']
        if command == 'import_legacy' and isinstance(result, dict):
            message += ' · ' + ('预检' if result.get('dry_run') else '导入') + ' 接受 ' + str(result.get('accepted',0)) + ' / 隔离 ' + str(result.get('quarantined',0))
        self.statusBar().showMessage(message, 15000)
        self.refresh_status()
        self.refresh_current()

    def build_dashboard(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 22, 24, 24)
        layout.addWidget(label('数据积累工作台', 'pageTitle'))
        layout.addWidget(label('从来源检查到审核入库，每一步都保留证据。', 'subtitle'))
        self.health = label('Worker · 正在连接', 'notice')
        layout.addWidget(self.health)
        self.summary = label('暂无状态')
        self.summary.setStyleSheet('font-size: 19px; padding: 22px 0px;')
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        row.addWidget(button('开始手动检查', self.manual_check, primary=True))
        row.addWidget(button('暂停新任务', lambda: self.action('pause')))
        row.addWidget(button('恢复任务', self.resume))
        row.addStretch()
        layout.addLayout(row)
        panel = QFrame()
        panel.setObjectName('panel')
        body = QVBoxLayout(panel)
        body.addWidget(label('工作顺序', 'pageTitle'))
        body.addWidget(label('账号身份  →  来源许可  →  手动采集  →  Coverage 核对  →  帖子审核'))
        body.addWidget(label('没有有效证据的来源会被阻断。Run 成功不会自动变为 VERIFIED，也不会自动公开帖子。'))
        body.addWidget(button('首次使用：载入离线示例', self.load_demo))
        layout.addWidget(panel)
        self.workflow_note = label('调度默认关闭；需手动检查通过、来源许可和外盘备份就绪后再明确启用每日计划。', 'notice')
        layout.addWidget(self.workflow_note)
        layout.addStretch()
        self.stack.addWidget(page)

    def new_data(self, kind, title, subtitle, columns, filters=None):
        page = DataPage(title, subtitle, columns, filters)
        self.pages[kind] = page
        page.requested.connect(lambda: self.refresh_page(kind))
        page.table.itemDoubleClicked.connect(lambda item: self.show_detail(kind))
        self.stack.addWidget(page)
        return page

    def build_data_pages(self):
        page = self.new_data('accounts', '画师账号', '维护稳定 X ID、身份来源、评级与采集开关。',
            [('account_id', '账号 ID'), ('current_handle', 'X Handle'), ('external_user_id', '稳定 X ID'),
             ('verification_status', '身份状态'), ('account_content_rating', '账号分级'), ('active_status', '启停')])
        page.actions.addWidget(button('添加账号', lambda: self.edit_account(), primary=True))
        page.actions.addWidget(button('编辑所选', lambda: self.edit_account(page.selected())))
        page.actions.addWidget(button('采集所选账号', lambda: self.start_selected()))
        page.actions.addStretch()
        page = self.new_data('jobs', '任务与 Runs', '失败后继续原 Run；UNKNOWN 需要先在 Console 核对，禁止盲目重跑。',
            [('job_id', '任务 ID'), ('account_id', '账号'), ('state', '状态'), ('run_id', 'Run ID'),
             ('pages', '页数'), ('offset', '已读条数'), ('coverage_status', 'Coverage'), ('observed_usd', '观察费用'), ('finalized_usd', '最终费用')])
        for text, callback in (('恢复所选', self.recover_job), ('核对 UNKNOWN Run', self.reconcile), ('登记最终账单', self.bill)):
            page.actions.addWidget(button(text, callback))
        page.actions.addStretch()
        page = self.new_data('posts', '帖子与审核', '全文按保存许可显示；先 REVIEWED，再核验是否可标记 PUBLISHABLE。',
            [('post_id', 'Post ID'), ('account_id', '账号'), ('published_at', '发布时间 UTC'), ('post_type', '帖型'),
             ('content_rating', '评级'), ('review_status', '审核'), ('rights_status', '权限')],
            [('account_id', '账号 ID', None), ('post_type', '帖型', ['ORIGINAL', 'QUOTE', 'REPLY', 'REPOST', 'UNKNOWN']),
             ('content_rating', '分级', ['SFW', 'R18', 'UNKNOWN']),
             ('review_status', '审核', ['STAGING', 'REVIEWED', 'REJECTED', 'PUBLISHABLE']),
             ('rights_status', '权限', ['PERMITTED', 'RESTRICTED']), ('from', '起始 YYYY-MM-DD', None), ('until', '结束 YYYY-MM-DD', None)])
        page.actions.addWidget(button('查看正文', lambda: self.show_detail('posts')))
        page.actions.addWidget(button('人工审核', self.review))
        page.actions.addWidget(button('导出 CSV', lambda: self.action('export_csv')))
        page.actions.addWidget(button('打开原帖', self.open_post))
        page.actions.addWidget(button('删除所选帖子…', self.delete_post, danger=True))
        page.actions.addStretch()
        page = self.new_data('media', '媒体素材库', '不自动显示缩略图或播放。下载与预览均再次检查许可和分级。',
            [('media_id', '素材 ID'), ('post_id', 'Post ID'), ('state', '状态'), ('content_rating', '评级'),
             ('mime', '格式'), ('file_size', '字节数'), ('error_code', '失败原因'), ('sha256', 'SHA-256')],
            [('state', '状态', ['QUEUED', 'DOWNLOADING', 'DOWNLOADED', 'FAILED', 'BLOCKED']),
             ('content_rating', '分级', ['SFW', 'R18', 'UNKNOWN'])])
        page.actions.addWidget(button('添加下载任务', self.queue_media, primary=True))
        page.actions.addWidget(button('下载所选', self.process_media))
        page.actions.addWidget(button('修复链接', self.repair_media))
        page.actions.addWidget(button('授权预览', self.preview))
        page.actions.addStretch()

    def build_budget(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 22, 24, 24)
        layout.addWidget(label('预算与存储', 'pageTitle'))
        layout.addWidget(label('累计预算包括已结算费用和未结算预留，不自动按月重置。', 'subtitle'))
        self.budget = label('正在读取…', 'notice')
        layout.addWidget(self.budget)
        self.storage = label('正在读取…', 'notice')
        layout.addWidget(self.storage)
        layout.addWidget(button('修改资源上限', self.edit_settings))
        layout.addWidget(button('扩档人工审批', self.approve_stage))
        layout.addWidget(label('硬保护：单次最多 US$0.50、试点最多 US$10；活动目录最多 50 GiB，最低空闲 10 GiB。'))
        layout.addWidget(label('未知账单会阻断新付费 Run。最终金额应与 Apify Console 核对，不能只采用 observed 值。'))
        layout.addStretch()
        self.stack.addWidget(page)

    def build_policy(self):
        page = self.new_data('policies', '权限与合规', 'access / retain / download_media / redistribute 分开审核，UNKNOWN 默认阻断。',
            [('policy_id', '策略 ID'), ('source_id', '来源'), ('operation', '操作'), ('permission_state', '许可'),
             ('version', '版本'), ('valid_until', '有效至 UTC'), ('evidence_uri', '证据')])
        page.actions.addWidget(button('新增许可记录', lambda: self.edit_policy(), primary=True))
        page.actions.addWidget(button('修订所选许可', lambda: self.edit_policy(page.selected())))
        page.actions.addWidget(button('执行到期清理', lambda: self.action('cleanup')))
        page.actions.addStretch()

    def build_backup(self):
        page = self.new_data('backups', '备份与恢复', '仅另一块物理磁盘可注册；恢复写入新空目录，先合并删除记录。',
            [('snapshot_id','快照 ID'),('created_at','创建时间 UTC'),('total_bytes','字节数'),('status','状态'),('path','外盘目录')])
        self.backup = label('正在读取备份状态…', 'notice')
        page.layout().insertWidget(2, self.backup)
        page.actions.addWidget(button('注册外盘目录', self.register_backup, primary=True))
        page.actions.addWidget(button('创建快照', self.create_backup))
        page.actions.addWidget(button('校验快照', self.verify_backup))
        page.actions.addWidget(button('恢复到新目录…', self.restore_backup))
        page.actions.addWidget(button('本地恢复/清理', lambda: self.action('cleanup')))
        page.actions.addStretch()

    def build_logs(self):
        page = self.new_data('logs', '设置与日志', '日志只记录操作与错误代码，不回显 Token 或远端响应正文。',
            [('at', '时间 UTC'), ('operation', '操作'), ('outcome', '结果'), ('error_code', '错误代码')])
        page.actions.addWidget(button('应用设置', self.edit_settings))
        page.actions.addWidget(button('配置 Apify Token', self.edit_token))
        page.actions.addWidget(button('导出诊断摘要', self.export_diagnostics))
        page.actions.addWidget(button('每日计划…', self.schedule_dialog))
        page.actions.addWidget(button('离线验收', lambda: self.action('run_acceptance')))
        page.actions.addWidget(button('迁移原型数据…', self.import_legacy))
        page.actions.addWidget(button('重新连接 Worker', self.refresh_status))
        page.actions.addWidget(button('停止 Worker…', self.stop_worker, danger=True))
        page.actions.addStretch()

    def navigate(self, index):
        self.stack.setCurrentIndex(index)
        self.refresh_current()

    def show_page(self, index):
        self.navigation.setCurrentRow(index)
        self.open_window()

    def refresh_page(self, kind):
        if kind in self.query_pending:
            return
        self.query_pending.add(kind)
        page = self.pages[kind]
        requested_args = page.query_args(kind)
        sequence = self.request_sequence.get(kind, 0) + 1
        self.request_sequence[kind] = sequence
        def updated(result):
            self.query_pending.discard(kind)
            if requested_args != page.query_args(kind):
                self.refresh_page(kind)
                return
            if self.request_sequence.get(kind) == sequence:
                page.populate(result)
        def failed(error):
            self.query_pending.discard(kind)
            if self.request_sequence.get(kind) == sequence:
                page.notice.setText('读取失败，保留上次数据：' + str(error) + '。点击刷新重试。')
        self.call('query', requested_args, updated, failed)

    def refresh_current(self):
        current = self.stack.currentWidget()
        for kind, page in self.pages.items():
            if page is current:
                self.refresh_page(kind)

    def refresh_status(self):
        if self.status_pending:
            return
        self.status_pending = True
        self.call('snapshot', success=self.update_status, failure=self.disconnected)

    def update_status(self, snapshot):
        self.status_pending = False
        self.last_snapshot = snapshot
        state = snapshot['state']
        self.health.setText(f"Worker · {state}     模式 · {snapshot['settings']['mode']}     调度 · {snapshot['schedule_status']}")
        self.summary.setText(f"{snapshot['accounts']} 个账号   /   {snapshot['posts']} 条帖子   /   今日新增 {snapshot['today']}   /   {snapshot['active']} 个活动任务")
        self.budget.setText(f"单次上限 US${snapshot['settings']['per_run_usd']} · 试点 US${snapshot['settings']['pilot_usd']}\n已结算 US${snapshot['finalized_usd']}    未结算预留 US${snapshot['reserved_usd']}")
        quota = snapshot['quota']
        self.storage.setText(f"活动数据 {quota['used_bytes'] / GiB:.3f} / {quota['limit_bytes'] / GiB:.0f} GiB\n磁盘空闲 {quota['free_bytes'] / GiB:.1f} GiB" + ('  · 容量预警' if quota['warning'] else ''))
        self.backup.setText('活动目录：' + snapshot['root'] + '\n备份目标：' + str(snapshot['settings']['backup_root'] or '未设置') + '\n备份状态：' + snapshot['backup_status'])
        self.tray.setToolTip('ACG Collector · ' + state)
        self.tray.setIcon(app_icon(TOKENS['danger'] if state == 'NEEDS_ATTENTION' else TOKENS['primary']))
        self.statusBar().showMessage('Worker 已连接 · Token ' + ('已配置' if snapshot['token_configured'] else '未配置') + ' · 日志和时间字段为 UTC')
        self.refresh_current()

    def disconnected(self, error):
        self.status_pending = False
        self.health.setText('Worker 未连接 · 当前页面可能为旧数据；点击“重新连接 Worker”。')
        self.statusBar().showMessage('连接失败：' + str(error) + '。退出界面不会终止已经启动的 Worker。')

    def selected(self, kind):
        row = self.pages[kind].selected()
        if not row:
            self.statusBar().showMessage('请先在表格中选择一行。')
        return row

    def form(self, title, fields, command, build_args, help_text=''):
        dialog = EditorDialog(self, title, fields, help_text)
        self.dialogs.append(dialog)
        def submit(values):
            try:
                args = build_args(values)
            except Exception as exc:
                dialog.failed('字段格式无效：' + type(exc).__name__)
                return
            def saved(result):
                dialog.busy = False
                dialog.accept()
                self.action_done(command, result)
            self.call(command, args, saved, dialog.failed)
        dialog.submitted.connect(submit)
        dialog.show()
        return dialog

    def edit_account(self, row=None):
        account = copy.deepcopy(row) if row else {'account_id': uuid.uuid4().hex, 'entity_id': None, 'platform': 'X',
            'external_user_id': None, 'current_handle': '', 'handle_history': [], 'verified_source_urls': [],
            'verification_status': 'PENDING', 'account_content_rating': 'UNKNOWN', 'source_policy_id': None,
            'observed_at': utc_now(), 'active_status': 'PAUSED'}
        fields = [('current_handle', 'X Handle（不含 @）', 'text', account['current_handle']),
                  ('external_user_id', '稳定 X User ID（未知可留空）', 'text', account['external_user_id']),
                  ('handle_history', '历史 Handle（逗号分隔）', 'text', ','.join(account['handle_history'])),
                  ('verified_source_urls', '身份核验来源（每行一条）', 'multiline', '\n'.join(account['verified_source_urls'])),
                  ('verification_status', '身份核验状态', ['PENDING', 'VERIFIED', 'REJECTED'], account['verification_status']),
                  ('account_content_rating', '账号分级', ['UNKNOWN', 'SFW', 'R18'], account['account_content_rating']),
                  ('source_policy_id', '来源策略 ID', 'text', account['source_policy_id']),
                  ('active_status', '采集状态', ['PAUSED', 'ACTIVE', 'DISABLED'], account['active_status'])]
        def args(values):
            updated = copy.deepcopy(account)
            updated.update(values)
            updated['external_user_id'] = values['external_user_id'] or None
            updated['source_policy_id'] = values['source_policy_id'] or None
            updated['handle_history'] = []
            for item in values['handle_history'].split(','):
                if item.strip():
                    updated['handle_history'].append(item.strip())
            updated['verified_source_urls'] = []
            for item in values['verified_source_urls'].splitlines():
                if item.strip():
                    updated['verified_source_urls'].append(item.strip())
            return {'account': updated, 'revision': digest(row) if row else None}
        self.form('编辑账号' if row else '添加画师账号', fields, 'save_account', args,
                  '仅有 Handle 不代表身份已核验。VERIFIED 必须提供稳定 ID 和来源证据。')

    def edit_policy(self, row=None):
        row = row or {}
        fields = [('policy_id', '策略 ID', 'text', row.get('policy_id', '')),
            ('source_id', '来源（x:稳定用户ID）', 'text', row.get('source_id', 'x:')),
            ('operation', '授权操作', ['access', 'retain', 'download_media', 'redistribute'], row.get('operation', 'access')),
            ('permission_state', '许可状态', ['UNKNOWN', 'PERMITTED', 'DENIED', 'REVOKED'], row.get('permission_state', 'UNKNOWN')),
            ('content_scope', '范围（METADATA,TEXT,RAW,MEDIA）', 'text', ','.join(row.get('content_scope', []))),
            ('evidence_uri', '许可证据 URI', 'text', row.get('evidence_uri', '')),
            ('reviewer', '审核人', 'text', row.get('reviewer', '')),
            ('valid_until', '有效至（UTC，例如 2027-01-01T00:00:00Z）', 'text', row.get('valid_until', '2027-01-01T00:00:00Z')),
            ('retention_rule', '保存期限与用途规则', 'text', row.get('retention_rule', '')),
            ('display_rule', '展示规则', 'text', row.get('display_rule', 'internal-only')),
            ('deletion_rule', '删除规则', 'text', row.get('deletion_rule', 'purge-on-revocation')),
            ('confirmation', '撤销 retain 将删除数据；确认时输入策略 ID', 'text', '')]
        def args(values):
            version = 0
            for policy in self.pages['policies'].rows:
                if (policy['policy_id'], policy['source_id']) == (values['policy_id'], values['source_id']):
                    version = max(version, policy['version'])
            record = dict(values)
            confirmation = record.pop('confirmation')
            scope = []
            for item in values['content_scope'].split(','):
                if item.strip():
                    scope.append(item.strip())
            record.update({'version': version + 1, 'purpose': 'acg-research', 'reviewed_at': utc_now(), 'content_scope': scope})
            return {'policy': record, 'confirmation': confirmation}
        self.form('来源许可记录', fields, 'save_policy', args, '四种操作分别授权。修订会原子更新同组策略版本；撤销 retain 会触发物理清理。')

    def edit_settings(self):
        if not self.last_snapshot:
            return
        original = copy.deepcopy(self.last_snapshot['settings'])
        fields = [('mode', '采集模式', ['OFFLINE', 'LIVE'], original['mode']),
            ('per_run_usd', '单次上限 USD（最多 0.50）', 'text', original['per_run_usd']),
            ('pilot_usd', '试点上限 USD（最多 10.00）', 'text', original['pilot_usd']),
            ('quota_gib', '活动数据上限 GiB（最多 50）', 'text', original['quota_bytes'] // GiB),
            ('min_free_gib', '最低空闲 GiB（至少 10）', 'text', original['min_free_bytes'] // GiB),
            ('max_file_mib', '单文件上限 MiB', 'text', original['max_file_bytes'] // 1024 ** 2),
            ('batch_mib', '单批上限 MiB', 'text', original['batch_bytes'] // 1024 ** 2),
            ('overlap_hours', '增量重叠小时', 'text', original['overlap_hours']),
            ('schedule_local_time', '每日计划本地时间', 'text', original['schedule_local_time']),
            ('timezone', 'IANA 时区', 'text', original['timezone']),
            ('backup_root', '备份目标路径（需另行注册）', 'text', original['backup_root']),
            ('actor_id', 'Actor ID', 'text', original['actor_id']),
            ('actor_build', '已核验 Actor Build', 'text', original['actor_build']),
            ('price_evidence_uri', '当前定价审核证据', 'text', original['price_evidence_uri']),
            ('price_valid_until', '定价证据有效至 UTC', 'text', original['price_valid_until']),
            ('price_minimum_usd', 'Actor 最低收费 USD', 'text', original['price_minimum_usd']),
            ('cap_supported', '已核验 PPE 收费封顶支持', 'bool', original['cap_supported'])]
        revision = digest(original)
        def args(values):
            state = copy.deepcopy(original)
            for name in ('mode', 'per_run_usd', 'pilot_usd', 'schedule_local_time', 'timezone', 'actor_id', 'actor_build',
                         'price_valid_until', 'price_minimum_usd', 'cap_supported'):
                state[name] = values[name]
            state['price_evidence_uri'] = values['price_evidence_uri'] or None
            state['backup_root'] = values['backup_root'] or None
            state.update({'quota_bytes': int(values['quota_gib']) * GiB, 'min_free_bytes': int(values['min_free_gib']) * GiB,
                'max_file_bytes': int(values['max_file_mib']) * 1024 ** 2, 'batch_bytes': int(values['batch_mib']) * 1024 ** 2,
                'overlap_hours': int(values['overlap_hours'])})
            return {'settings': state, 'revision': revision}
        self.form('应用设置', fields, 'save_settings', args, 'LIVE 仍需 Token、来源许可和有效收费证据；仅切换模式不会发起采集。每日计划需通过独立操作启用。')

    def register_backup(self):
        fields = [('path','另一物理盘上的空目录','text',''),('confirmation','再次输入完整备份路径','text','')]
        self.form('注册外盘备份目录', fields, 'register_backup', lambda values: values,
            '将创建应用专用 vault 标记。拒绝同物理盘的其他分区、未知磁盘及非空未登记目录。')

    def create_backup(self):
        row = self.selected('backups')
        if row:
            self.action('create_backup', {'vault_id':row['vault_id']})

    def verify_backup(self):
        row = self.selected('backups')
        if row and row.get('snapshot_id'):
            self.action('verify_backup', {'vault_id':row['vault_id'],'snapshot_id':row['snapshot_id']})

    def restore_backup(self):
        row = self.selected('backups')
        if row and row.get('snapshot_id'):
            fields=[('target','新的空数据目录','text',''),('confirmation','再次输入完整恢复路径','text','')]
            self.form('从快照恢复到新目录',fields,'restore_backup',
                lambda values:dict(values,vault_id=row['vault_id'],snapshot_id=row['snapshot_id']),
                '不覆盖当前目录。将先合并删除屏障，校验哈希；恢复后计划关闭、暂停新任务。')

    def schedule_dialog(self):
        enabled = self.last_snapshot and self.last_snapshot['settings']['schedule_enabled']
        if enabled:
            ask=ConfirmDialog(self,'关闭每日计划','停止创建新的计划任务；已经启动的远端 Run 不会被强行终止。')
            if ask.exec()==QDialog.DialogCode.Accepted:
                self.action('disable_schedule')
        else:
            self.form('明确启用每日计划',[('confirmation','输入 ENABLE 确认启用','text','')],
                'enable_schedule',lambda values:values,
                '需手动测试通过、许可/预算有效、外盘已注册。Windows 系统时区须匹配设置中的 IANA 时区；默认当地 09:00，漏跑合并一次。')

    def approve_stage(self):
        self.form('逐档扩展真实采集', [('stage','下一档账号数（20/50/150）','text','20'),
            ('reviewer','审核人','text',''),('evidence_uri','上一档真实试点验收证据 URI','text',''),
            ('confirmation','输入 APPROVE 确认','text','')], 'approve_stage',
            lambda values:dict(values,stage=int(values['stage'])),
            '必须逐档审批且有上一档真实结算与成功记录；Mock 不满足此条件。预算不会增加。')

    def import_legacy(self):
        self.form('只读迁移 v0.1 原型数据', [('source','旧 staging.sqlite3 完整路径','text',''),
            ('account_id','已登记且核验的目标账号 ID','text',''),('execute','执行导入（不勾选仅 Dry Run）','bool',False),
            ('confirmation','执行时再次输入目标账号 ID','text','')], 'import_legacy',lambda values:values,
            '先 Dry Run 检查字段与来源许可，再执行；原 SQLite 不修改。重复导入按文件 hash/账号幂等，缺字段隔离。')

    def edit_token(self):
        self.form('配置 Apify Token', [('token', 'Apify Token（仅保存到本机私有目录）', 'secret', '')],
            'save_token', lambda values: values, '不写入采集数据、日志、诊断或发布包；界面只显示是否已配置。')

    def export_diagnostics(self):
        def saved(result):
            self.statusBar().showMessage('诊断摘要已导出：' + result['filename'], 15000)
        self.call('export_diagnostics', success=saved)

    def manual_check(self):
        self.show_page(1)
        self.statusBar().showMessage('选择一个已核验且 ACTIVE 的账号，点击“采集所选账号”。')

    def start_selected(self):
        row = self.selected('accounts')
        if not row:
            return
        live = self.last_snapshot and self.last_snapshot['settings']['mode'] == 'LIVE'
        if live:
            ask = ConfirmDialog(self, '确认真实采集费用', '将创建一个收费 Apify Run。上限 US$0.50，仍需核对 Console 账单。确认开始？', row['account_id'])
            if ask.exec() != QDialog.DialogCode.Accepted:
                return
        self.action('start_sync', {'account_id': row['account_id'], 'confirm_live': bool(live)})

    def resume(self):
        self.action('resume')

    def load_demo(self):
        self.action('load_demo')

    def recover_job(self):
        row = self.selected('jobs')
        if row:
            self.action('recover_job', {'job_id': row['job_id']})

    def reconcile(self):
        row = self.selected('jobs')
        if row:
            self.form('核对已有 Run', [('run_id', 'Console 中已核对的 Run ID', 'text', row.get('run_id')),
                ('evidence_uri', '输入指纹/Actor/时间匹配证据 URI', 'text', '')], 'reconcile',
                lambda values: dict(values, job_id=row['job_id']), '先核对已有收费任务，不会创建新 Run。')

    def bill(self):
        row = self.selected('jobs')
        if row:
            self.form('登记最终账单', [('actual_usd', '实际 USD（十进制字符串）', 'text', ''),
                ('evidence_uri', '最终账单证据 URI', 'text', '')], 'finalize_bill', lambda values: dict(values, job_id=row['job_id']))

    def review(self):
        row = self.selected('posts')
        if row:
            self.form('人工审核帖子', [('status', '审核结果', ['REVIEWED', 'REJECTED', 'PUBLISHABLE'], 'REVIEWED'),
                ('category', '帖子内容分级', ['UNKNOWN', 'SFW', 'R18'], row.get('content_rating', 'UNKNOWN')),
                ('reviewer', '审核人', 'text', ''), ('evidence_uri', '核对来源证据 URI', 'text', ''),
                ('notes', '审核备注', 'multiline', '')], 'review', lambda values: dict(values, post_id=row['post_id']),
                '不能从 STAGING 直接跳至 PUBLISHABLE；此操作不会公开发布。')

    def delete_post(self):
        row = self.selected('posts')
        if not row:
            return
        ask = ConfirmDialog(self, '删除帖子及相关素材（不可撤销）', '将写入删除屏障并清理当前态、历史和已登记素材。未知备份仍可能待清理。', row['post_id'])
        if ask.exec() == QDialog.DialogCode.Accepted:
            self.action('delete_post', {'post_id': row['post_id'], 'confirmation': ask.input.text()})

    def media_form(self, command, row=None):
        fields = [('post_id', '已有 Post ID', 'text', row.get('post_id') if row else ''),
            ('url', '允许的 HTTPS 媒体 URL', 'text', row.get('source_url') if row else ''),
            ('rating', '已审核的媒体分级', ['UNKNOWN', 'SFW', 'R18'], row.get('content_rating', 'UNKNOWN') if row else 'UNKNOWN'),
            ('evidence_uri', '内容/年龄/地区审查证据 URI', 'text', row.get('rating_evidence') if row else ''),
            ('expires_at', '获准保留至 UTC', 'text', row.get('expires_at') if row else '2027-01-01T00:00:00Z'),
            ('restricted', '涉及受限制或疑似违法内容（勾选即阻断）', 'bool', row.get('restricted', False) if row else False),
            ('allow_r18', '已明确完成 R18 年龄/地区审查', 'bool', row.get('allow_r18', False) if row else False)]
        def args(values):
            if row:
                values.pop('post_id')
                values.pop('restricted')
                values['media_id'] = row['media_id']
            return values
        self.form('下载素材' if command == 'queue_media' else '修复素材链接', fields, command, args,
            'UNKNOWN 不自动下载；单独核验媒体复制许可，不能将“可下载”当作授权。')

    def queue_media(self):
        self.media_form('queue_media')

    def repair_media(self):
        row = self.selected('media')
        if row:
            self.media_form('repair_media', row)

    def process_media(self):
        row = self.selected('media')
        if row:
            self.action('process_media', {'media_id': row['media_id']})

    def preview(self):
        row = self.selected('media')
        if row:
            dialog = PreviewDialog(self, row)
            self.dialogs.append(dialog)
            dialog.show()

    def open_post(self):
        row = self.selected('posts')
        if row:
            def opened(post):
                url = QUrl(post['source_url'])
                if url.scheme() == 'https' and url.host() == 'x.com':
                    QDesktopServices.openUrl(url)
            self.call('post_detail', {'post_id': row['post_id']}, opened)

    def show_detail(self, kind):
        row = self.selected(kind)
        if not row:
            return
        def display(value):
            dialog = QDialog(self)
            dialog.setWindowTitle('详情 · ' + kind)
            dialog.resize(780, 600)
            layout = QVBoxLayout(dialog)
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            text.setAccessibleName('详情纯文本')
            layout.addWidget(text)
            layout.addWidget(button('关闭', dialog.accept))
            if kind == 'posts':
                guard = QTimer(dialog)
                guard.setInterval(2000)
                def denied(error):
                    from shiboken6 import isValid
                    if isValid(text):
                        text.setPlainText('正文已隐藏：保存许可或帖子状态发生变化。')
                    guard.stop()
                def recheck():
                    if dialog.isVisible():
                        self.call('post_detail', {'post_id': row['post_id']}, failure=denied)
                    else:
                        guard.stop()
                guard.timeout.connect(recheck)
                guard.start()
            self.dialogs.append(dialog)
            dialog.show()
        if kind == 'posts':
            self.call('post_detail', {'post_id': row['post_id']}, display)
        else:
            display(row)

    def stop_worker(self):
        ask = ConfirmDialog(self, '停止独立 Worker', '停止后不会创建新任务。远端已启动的 Run 可能继续计费，下次启动将读取已有 Run。确认安全停止？')
        if ask.exec() == QDialog.DialogCode.Accepted:
            self.action('stop_worker')

    def open_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_ui(self):
        self.force_quit = True
        if self.close():
            self.tray.hide()
            QApplication.instance().quit()
        else:
            self.force_quit = False

    def closeEvent(self, event):
        from shiboken6 import isValid
        for dialog in self.dialogs:
            if isValid(dialog) and dialog.isVisible():
                dialog.close()
                if dialog.isVisible():
                    event.ignore()
                    return
        if QSystemTrayIcon.isSystemTrayAvailable() and not self.force_quit:
            self.hide()
            event.ignore()
            return
        self.timer.stop()
        self.tray.hide()
        event.accept()
