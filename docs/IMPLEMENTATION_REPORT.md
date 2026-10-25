# M6–M8 工程实现与验收报告

版本 0.0.8，继承 M0–M5。当前 **141 项新系统测试 + 6 项原型回归 = 147 项通过**。环境 Linux / Python 3.12.14 / PySide6 6.10.2；Windows 原生分支未在本机执行。

## M6

- IANA 本地日、默认09:00、显式 GUI 启用、普通用户 Windows Task Scheduler XML/注册/停用、登录/每日触发。系统 Windows 时区映射须匹配；未登录时不伪报运行。
- DST 缺失/重复时间策略、跨日补跑合并、最多150账号持久化队列、deterministic request_id、防重复 POST；逐账号权限/预算复核。
- 外盘所有权标记、物理磁盘识别接口、同盘其他分区拒绝、未知映射拒绝；逐文件 SHA-256/长度和安全路径检查。
- 不可变 snapshot revision + 原子索引；新空目录恢复、恢复中封锁、当前删除与许可账本先合并；恢复后关闭计划并暂停新任务，保留预算/游标。
- 已登记旧快照的撤回内容/媒体清理，孤立已知副本清理；离线/损坏副本有 pending，不能假称全局删除完成。
- 外盘独立保存 ACCOUNTING 账本。RUN_INTENT 必须先完成本地持久化与外盘同步，再创建远端 Run；外盘缺失时停止于 POST 之前。恢复合并快照、外盘和仍可用的当前账本，保留较高的预留/结算金额、未结算任务及远端 Run 身份。账本缺失/损坏或同一任务出现冲突 Run 时封锁恢复目录。
- 七项专项测试实际覆盖旧快照之后的费用与 UNKNOWN Run、原数据盘丢失后的外盘账本恢复、外盘不可用时零 POST、账本损坏/丢失、磁盘映射变为未知、注册时同步账本和冲突 Run 拒绝恢复。

## M7

- 当前用户 protected DACL/POSIX 权限、独立凭据目录、代码/数据/IPC 隔离。
- 发布代码清单、哈希锁定 wheel 依赖、普通用户分版本 venv 安装、最后切 current.json；兼容性受 format-floor 约束，回滚不触及数据和删除记录。
- 安装器在临时目录**实际执行依赖安装**并在最终移动后的 venv 成功创建 Qt Application；日志的发布摘要在 installer-smoke.json。该试验是 Linux，不是 Windows安装验收。
- 脱敏诊断、带 BOM 的公式安全 CSV、原型 SQLite只读 Dry Run/幂等迁移；源文件前后字节一致。

## M8

四档 Mock 运行结果（每档独立临时数据根，零真实网络/扣费）：

| 账号 | 成功 | 合成帖子 | 重放一致 | 模式 |
|---|---:|---:|---|---|
| 5 | 4 | 16 | True | MOCK |
| 20 | 20 | 80 | True | MOCK |
| 50 | 50 | 200 | True | MOCK |
| 150 | 150 | 600 | True | MOCK |

20 条 synthetic gold 分类样本全部正确、关键身份/日期错误0。150档单Run并发1、页大小2；本次峰值RSS 33.8 MiB。该负载证据不外推到任意历史数据量或真实媒体解码。

新增 release-stage 测试覆盖：DST gap/fold、23天漏跑合并、手动测试门禁、关计划、未知/同盘拒绝、快照损坏/越界、旧快照删除后恢复、恢复中断封锁、当前权限撤回过滤、恢复默认关闭计划、共享凭据/未授权 raw 不备份、安装完整性、回滚不丢删除记录、公式注入、旧SQLite重复导入和Mock不能冒充真实扩档证据。原有Qt/Worker生命周期与核心回归全部保留。

## 实际边界和发布门禁

- 离线软件验收通过；Windows Task Scheduler/NTFS DACL/Named Pipe、原生盘映射与真实两盘恢复、硬件掉电：**NOT_RUN**。单元测试的盘探针和 OS 注册器注入仅为 Mock，不等于真实物理盘证据。
- 未执行真实 X/Apify Run 或真实媒体下载；**G2/G3 NOT_RUN**。真实授权、build/字段、价格、最终账单、人工抽样真实性需外部证据，不能由JSON自证。
- G1 的可离线部分有测试证据；完整 Windows 发布资格仍需原生测试。release-gates.json 将工程离线 PASS 与原生 NOT_RUN 分开，不一概标为发布完成。
- 恢复/安装 manifest 是完整性机制，不是发布者数字签名。用户仍需信任来源。删除为应用受控文件层，不保证 SSD 安全擦除；未知副本不在全局清理承诺内。
- 恢复后的数据根不继承旧备份目录注册。保持暂停，重新核对凭据/许可并登记另一物理盘上的 vault 后，才可重新启用计划；不要让原目录与恢复目录同时执行真实采集。独立费用账本仅保证已成功同步到已登记外盘的记录，不能推断其他未登记环境的费用。
- 大备份/恢复串行占用 Worker，GUI异步不冻结，但状态请求可能等待；超时应查询状态再决定重试，不能盲目重复。
- 真实来源长期保存、完整30日覆盖、自动AI摘要/正式后端/公开发布都未被此交付自动授权。

## 官方接口依据

- Windows Task Scheduler schema: https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema
- Volume physical extents: https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-ioctl_volume_get_volume_disk_extents
- Storage descriptor: https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-storage_device_descriptor
