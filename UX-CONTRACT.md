# UX Contract

## Product context

中文 Windows 本地管理台，原帖内容可为日文。公历、UTC 数据，America/Vancouver 计划时区。遵循原生桌面可访问名称、Tab/Shift+Tab、Enter/Escape、焦点可见；目标 WCAG 2.2 AA 适用原则，未宣称完成屏幕阅读器实机审计。

## Business-context sources

docs/SPEC_v1.0.md 的第 3/6/7/8 节为架构、权限、预算、GUI 与生命周期依据；collector/policy.py、sync.py、media.py、deletion.py 为执行点。

## Visual contract

DESIGN.md → desktop/theme.py → Qt shared widgets。浅色主题。test_desktop.py 验证主题 token 和可访问名称。

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Table Selection | DataPage | UX-CONTRACT.md | single-row | QtTest |
| Select/Listbox | QComboBox | UX-CONTRACT.md | native | QtTest popup |
| Date | EditorDialog | UX-CONTRACT.md | typed UTC | validation |
| Form | EditorDialog | UX-CONTRACT.md | create/edit | QtTest |
| Scrollbar | Qt stylesheet | DESIGN.md | native desktop | screenshot |
| Toast | MainWindow status bar | UX-CONTRACT.md | success/error | QtTest |
| CRUD | WorkerService | SPEC_v1.0.md | pessimistic | process E2E |

## Dataset navigation

共享搜索带清除按钮；按 Enter 或 250ms 防抖查询，清除立即生效。保留每页查询、筛选、页码和选择，桌面应用不使用 URL。每页 100 行，显示总数，按列排序仅作用当前页并明确提示。空数据和无结果有文字提示。高级筛选按画师、日期、帖子类型、评级、审核、权限组合。请求序号忽略过期响应。

## Forms and feedback

统一 EditorDialog，字段有标签与说明。保存中禁用提交，失败保留输入并显示修正信息，成功关闭返回原列表并刷新。取消/Escape 对非空改动显示放弃确认；敏感值遮罩，不回显 Token。模态关闭恢复原触发控件焦点。删除必须再次输入完整目标 ID，按钮文本明确不可撤销。

## Async and resilience

所有文件与采集写入仅在 Worker；UI 仅发送白名单语义命令。命令异步执行，不阻塞窗口；断连保留表格并标记数据可能过期，提供重连。开始收费任务命令 ID 持久化在 job，重复命令不得重复 POST。暂停只阻止新任务，已启动 Run 继续安全完成。退出界面不停止 Worker；停止 Worker 需确认，完成当前有界步骤后退出，保留远端 Run 恢复信息。

## Permission UI

来源 UNKNOWN 默认阻断。审核 STAGING 只能先变为 REVIEWED，再通过 redistribute 门禁变 PUBLISHABLE；不公开发布。预览需 Worker 实时许可/分级检查，每两秒复核，撤回即关闭预览。图片/视频不自动显示或播放。调度需独立明确启用，外盘备份需物理盘验证；未知/同盘/损坏/非空恢复目标显示错误并阻断。恢复默认暂停且调度关闭。

## Verification

python3 validate.py；QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -p test_desktop.py -v。原生 QtTest、进程 IPC 生命周期测试、离屏截图；未运行真实 Windows 托盘宿主与 NTFS 验证。premium-audit.json 为静态契约证据，不代替原生运行测试。

## M6–M8 生命周期

每日计划仅用独立确认命令启用，普通设置不能切换开关。外盘注册和新目录恢复要求重复输入完整路径；快照显示验证状态、逻辑ID和创建时间。长操作异步发送，结果未知时提示先查询。升级后新GUI不连接旧版本写入者；扩档需上一档真实记录和人工审批，Mock不构成实跑授权。
