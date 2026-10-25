# M0–M5 实施与验收报告

版本 0.0.5。M5 新增 22 项测试；全量 **94 + 6 = 100 项通过**。环境 Linux / Python 3.12.14 / Qt & PySide6 6.10.2，原生 Qt offscreen 平台，中文 Noto Sans CJK 字体。完整输出 TEST_RESULTS.txt。

## 交付与职责

九页原生 Qt Widgets 管理台，统一表格/搜索/组合筛选/分页/表单/错误恢复；Dashboard/Accounts/Jobs/Posts/Media/Budget/Policies/Backup/Settings & Logs。Tray 菜单与窗口/Worker 分离；独立 Worker 持有单写者，UI 通过白名单 IPC 命令调用。

账号、许可、设置、审核和日志均为 Schema 验证的追加事件。策略四维修订同批推进统一版本，避免仅改某一维就误判其余维度过期而错误清理。retain 撤销需确认并触发核心清理；正文与素材预览复核许可。STAGING 不可直达 PUBLISHABLE，后者需再分发许可和明确内容分级；不执行公开发布。

IPC 优先 QLocalServer UserAccessOption + capability token，不开放 TCP。当前环境禁止 AF_UNIX，因此实际双进程验证使用 0700/0600 私有目录 mailbox 回退；两种传输共用消息鉴权、白名单、大小限制与命令分发。Windows 代码包含当前 SID 的目录 ACL 与同用户 Pipe，尚未实机运行。

凭据在独立用户配置目录，不进入采集目录/日志/诊断/发布包。GUI 仅获配置状态；配置输入默认遮罩且可明确显示。诊断摘要仅含状态、数量、配额、预算和操作代码，不含正文、来源 URL、Token 或本机配置路径。

## 测试证据

- QtTest：九页导航、共享表格排序选择/搜索清除、原生下拉框打开、表单保存失败保留输入、遮罩、主题 token 一致性。
- 真实进程 E2E：GUI 选账号→采集→Posts→填写审核→REVIEWED；GUI 进程退出后 Worker 仍完成原 Run；停止/重启 Worker 后同一 command_id 返回同一 job，不创建新 Run。
- IPC 错误 capability token 被拒绝；窗口请求异步执行并合并状态刷新，同一表格忽略过期查询，保留离线旧数据。
- Service：账号冲突/稳定 ID 去重、暂停仅阻止新任务、四维策略原子版本、retain 撤回确认和物理清理、审核门禁、分发权限、查询组合过滤/限页、LIVE 未确认阻断、Token/诊断不泄密、审核备注随删除清理、预览许可撤回后阻断。
- 其余 M0–M4 测试和原型六项回归全部保留并通过。

DESIGN.md lint 0 errors（5 个语义 token 直接引用警告）；premium-audit.json strict 0 errors。该静态脚本不分析 Python Qt 行为，运行证据以 QtTest/双进程测试和实际截图为准。pip-audit 固定的 PySide6 / Addons / Essentials / shiboken6 / tzdata 无已知漏洞，结果见 dependency-audit.json；不宣称覆盖未列出的内嵌原生库。

截图：ui-evidence/dashboard.png、accounts.png、posts.png、posts-960.png、media.png、policies.png、logs.png、settings.png，均使用合成数据。

## 未完成与风险

没有真实 X/Apify Run 或真实媒体 HTTP；没有产生费用。Windows 托盘宿主、NTFS、icacls、Named Pipe、屏幕阅读器与高 DPI 实机验证待执行。无桌面宿主的 Linux offscreen 无法证明真实 Windows 托盘展示，但已验证菜单构造及独立进程生命周期。

M6 的每日计划/开机补跑/另一物理盘备份/恢复在 UI 中明确显示未接入，执行按钮禁用，不伪报成功。当前可保存计划时间、时区和备份路径，但不会启用计划。M7 安装/升级/回滚与更完整安全包装、M8 真实授权试点仍未完成。

旧版不能安全打开新增事件或忽略删除屏障。继续现有数据时显式传 --root，不自动读取任意其他目录。第三方 GUI 依赖是新增安装项，核心 CLI 仍可仅用标准库运行；未安装 PySide6 时 M5 测试明确 skip，不算已验收。

下一阶段：M6。
