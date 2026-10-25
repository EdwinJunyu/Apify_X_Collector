# Apify X Collector

[中文使用手册](#1-第一次启动先跑离线示例) · [English User Guide](#english-guide)

用于 ACG 情报 App 前期数据积累的本地桌面工具：通过 Apify Actor 采集 X 账号动态，在本机保存、审核、下载获准媒体，并管理任务费用与备份。

**当前版本：0.0.9。** 主要面向自己在 Windows 上使用。默认 **OFFLINE**，可以先体验完整流程，不需要 Token，也不会产生 Apify 费用。真实采集需完成账号、来源许可及 Actor 收费配置。

> 当前自动测试记录为 150 项通过，包含 Qt 离屏与独立 Worker 测试。Windows 原生任务计划、ACL/Named Pipe、真实双物理盘恢复及真实 Apify 试点仍待实机验证。不要把离线通过理解为真实账号已验证可采集。

## 1. 第一次启动：先跑离线示例

### 准备与安装

1. 安装能通过 `py -3` 启动的 **64 位 Python 3.10 或更高版本**。
2. 从本仓库选择 **Code → Download ZIP**，解压到普通用户可写的目录；不要直接在压缩包内运行。
3. 双击 `Install_Desktop.cmd`，首次安装桌面依赖。安装需要网络，脚本会校验依赖包哈希。
4. 安装成功后双击 `Run_Desktop.cmd`。

日常启动只需要第 4 步。若窗口一闪而过，在项目目录的终端执行 `py -3 desktop_app.py` 查看错误。

### 五分钟体验流程

1. 在总览点击 **首次使用：载入离线示例**。
2. 打开 **画师账号**，选中示例账号，点击 **采集所选账号**。
3. 在 **任务与 Runs** 查看状态，等待 `DONE`。
4. 到 **帖子与审核**，选中帖子，点击 **查看正文**，再通过 **人工审核** 标记 `REVIEWED`。
5. 查看 **预算与存储** 和 **设置与日志**，熟悉费用、容量与错误提示。

示例帖子和示例费用都是合成数据，不来自真实 X 采集。建议演示数据与后续正式数据分开，避免示例任务干扰账号、预算和模式切换。可在项目目录运行 `py -3 desktop_app.py --root D:\ACGCollectorDemo` 使用专门的演示目录；正式使用另选目录。

## 2. 界面怎么用

| 页面 | 常用操作 |
|---|---|
| 总览 | 查看运行状态、暂停新任务、载入离线示例 |
| 画师账号 | 添加/编辑账号，选择一个账号启动采集 |
| 任务与 Runs | 查看进度和 Coverage，恢复原任务，核对 UNKNOWN，登记账单 |
| 帖子与审核 | 筛选帖子、查看正文、人工审核、打开原帖、导出 CSV、删除 |
| 媒体素材库 | 添加媒体下载、下载所选、修复链接、授权预览 |
| 预算与存储 | 查看累计费用及磁盘使用量，修改上限，扩档审批 |
| 权限与合规 | 分别维护访问、保存、媒体下载与再分发许可 |
| 备份与恢复 | 注册外盘目录，创建/校验快照，恢复到新空目录 |
| 设置与日志 | 设置模式/Actor/时区，配置 Token，启停每日计划，导出诊断，停止 Worker |

涉及“所选”的操作，都要先在表格中选中一行。列表支持分页，每页最多 100 行；列排序仅针对当前页。

### 窗口、托盘和 Worker 的区别

- **关闭窗口/退出界面**：有托盘时可缩到托盘；退出 GUI 后独立 Worker 仍可继续工作。
- **暂停新任务**：阻止新任务启动，已经开始的任务仍可能继续处理。
- **停止 Worker…**：在“设置与日志”中操作，停止本机后台处理。
- **远端 Run**：已经创建的 Apify Run 不会因为关闭 GUI 或停止 Worker 自动取消，仍可能计费。需要时到 Apify Console 检查。

## 3. 配置真实采集

推荐顺序：**账号身份 → 来源许可 → Token → Actor/定价 → LIVE → 单账号手动测试**。

### 3.1 添加画师账号

在 **画师账号 → 添加账号** 填写：

| 字段 | 怎么填 |
|---|---|
| X Handle | 当前用户名，不含 `@` |
| 稳定 X User ID | 数字用户 ID，不是 Handle，也不是帖子 ID |
| 身份核验来源 | 可核对身份的链接，每行一条 |
| 身份核验状态 | 信息核实后设为 `VERIFIED`；未知时保留 `PENDING` |
| 来源策略 ID | 自己给这组许可起的标识，例如 `artist-source-001`，须与许可记录一致 |
| 账号分级 | `SFW`、`R18` 或 `UNKNOWN`；不能代替单条媒体审核 |
| 采集状态 | 准备就绪设为 `ACTIVE`，暂不采集设为 `PAUSED` |

可先保存资料不完整的候选账号，但仅有 Handle 不能开始采集。尽量在启动任务前完成资料核对；任务进行中更改账号或策略可能阻止内容续读，需要重新核验。

### 3.2 录入来源许可

进入 **权限与合规 → 新增许可记录**。来源格式是 `x:数字用户ID`，策略 ID 与账号中的“来源策略 ID”一致。

| operation | 对应行为 |
|---|---|
| `access` | 访问/采集来源 |
| `retain` | 在本机保留数据 |
| `download_media` | 下载和保留媒体副本 |
| `redistribute` | 后续再分发资格检查 |

至少需要有效的 `access` 与 `retain` 才能采集保存；保存正文还需要相应 `TEXT` 范围，基础资料使用 `METADATA`。下载媒体另需核对 `MEDIA` 范围及媒体下载许可。

记录真实许可证据、审核人、用途/保存规则和有效期；时间使用 UTC，例如 `2027-01-01T00:00:00Z`。`UNKNOWN`、拒绝、撤回、过期均可能阻断操作。不要为了让程序运行而把未核实许可改成 `PERMITTED`；示例的 `mock:` 证据不能授权真实采集。

**撤销 retain 可能触发内容删除。** 界面要求输入策略 ID 确认，操作前先核对来源。

### 3.3 Token 与 Actor

1. 在 **设置与日志 → 配置 Apify Token** 输入自己的 Apify Token。
2. 在 **应用设置** 填写 Actor ID、已核验的 Actor Build、定价证据及有效期、最低收费，并核对 PPE 收费封顶支持。
3. 准备完成后将模式改为 `LIVE`。

当前内置真实适配器面向 `scrapesage/twitter-scraper` 的预期字段。**填写任意 Actor ID 不会自动获得兼容性**；换 Actor 或 Build 前需要核对输入/输出字段与适配器。默认 Build 为 `unverified`，会阻止真实运行。

Token 保存在本机私有配置目录，通过界面管理即可，不要上传到仓库。切换 LIVE 本身不会立即启动采集。

### 3.4 手动跑一个账号

选中已就绪账号，点击 **采集所选账号**，阅读真实费用确认提示后开始。在 **任务与 Runs** 跟踪任务，并到 Apify Console 检查远端结果。

初次请求最多回填最近 30 天；增量按保存的检查点计算，默认重叠 48 小时，并对重复帖子去重。实际取得多少内容取决于 Actor 返回结果，不能保证完整覆盖 30 天。

默认单次上限 **US$0.50**，试点累计上限 **US$10**。累计包括未结算预留，不自动按月清零；上限不能通过界面提高到硬限制以上。建议一条真实任务完成、核账后再继续。

## 4. 任务失败、UNKNOWN 和账单怎么处理

| 状态/提示 | 含义与处理 |
|---|---|
| `RUN_INTENT` | 已保存创建意图；先检查 Console，不能假定远端没有创建成功 |
| `RUNNING` | 远端任务进行中，或等待查询结果 |
| `READING` | 正在读取原 Dataset 的分页数据 |
| `DONE` | 本次导入完成；不等于数据覆盖完整，也不等于费用已结算 |
| `NEEDS_REVIEW` | 存在需检查的数据问题；排查后恢复原任务 |
| `FAILED` | 查看日志和 Console，确认失败原因与已有费用 |
| `REMOTE_OUTCOME_UNKNOWN` | 创建请求结果不明；可能已产生收费 Run，必须先对账 |
| Coverage `PARTIAL` / `UNKNOWN` | 覆盖证据有限或未知，不能当作“没有更多帖子” |

### UNKNOWN 的处理顺序

1. 到 Apify Console 找到对应时间的 Run，核对 Actor、输入及目标账号。
2. 选中本机任务，点击 **核对 UNKNOWN Run**。
3. 填写已经核对的 Run ID 和证据 URI。
4. 使用 **恢复所选** 继续读取原 Run，避免重复创建付费任务。

分页读取失败也优先使用 **恢复所选**，而不是新增采集。

### 登记最终账单

任务进入可结算状态后，到 Console 核对最终金额，再点击 **登记最终账单**，填写十进制金额与账单证据 URI。`observed_usd` 是观察费用，不等于最终账单。

未结算任务会阻断新付费 Run。0.0.9 支持在来源许可撤回或 Handle 改名后登记既有费用，但仍核对稳定账号、来源和 Actor 身份；这不会恢复内容访问权限。

## 5. 帖子审核与媒体下载

### 帖子

在 **帖子与审核** 中查看正文或打开原帖，核对后点击 **人工审核**，填写审核人、证据、分级和备注。

通常先由 `STAGING` 标记为 `REVIEWED`。`PUBLISHABLE` 还需要再分发许可等检查，不能直接从 `STAGING` 跳过去；它只是内部资格标记，不会自动发布到网站或 App。

**导出 CSV** 用于外部查看审查元数据，不是完整数据备份。删除帖子需输入对应 Post ID 确认，会清理相关历史和已登记媒体，并记录删除屏障。未连接的外盘清理可能保持 pending，重新连接后继续处理；自行复制到其他位置的未知副本不在自动清理范围内。

### 媒体

1. 在 **媒体素材库 → 添加下载任务** 填写已有 Post ID、允许的 HTTPS 媒体链接、审核分级、证据和保留截止时间。
2. 选中队列项，点击 **下载所选**。
3. 成功后点击 **授权预览**。

帖子保存与媒体下载分开，媒体失败不会回滚已保存帖子。`UNKNOWN` 不自动下载；R18 需要单独明确审核，受限制内容会被阻断。链接永久失效时先用 **修复链接**，不要反复重试旧链接。

## 6. 每日自动运行

先让每个启用账号完成手动同步，再注册另一物理盘备份目录，然后进入 **设置与日志**：

1. 在 **应用设置** 设定本地时间和 IANA 时区，默认 `09:00` / `America/Vancouver`。
2. Windows 系统时区必须与选定时区匹配。当前支持 Vancouver/Los_Angeles、Toronto、UTC、Tokyo 对应映射。
3. 点击 **每日计划…**，输入 `ENABLE` 确认启用。

每日计划使用当前用户的 Windows Task Scheduler，包含每日与登录触发；用户未登录时不承诺后台运行。漏跑多日合并补跑，DST 重复时间只触发一次。首次启用时若当天计划时刻已过，可能补跑一次。

启用前会检查有效来源许可、账号手动测试、外盘、预算和活动账号数量。改计划时间后保存设置；停止每日创建任务仍通过 **每日计划…** 操作。仅停止 GUI 不会停用计划。

## 7. 数据放在哪里，怎样备份和恢复

| 内容 | Windows 默认位置 |
|---|---|
| 活动数据 | `%LOCALAPPDATA%\ACGCollectorData` |
| Token 与 IPC 配置 | `%LOCALAPPDATA%\ACGCollector\` 下按数据根区分的私有子目录 |
| 分版本安装的程序 | `%LOCALAPPDATA%\ACGCollectorApp\versions\0.0.9` |
| 备份 vault | 自己在界面注册的另一物理盘目录 |

活动数据使用 JSONL 事件及 Manifest，索引是派生数据。不要直接修改 journal、删除屏障或费用记录。活动目录默认上限 50 GiB，所在磁盘至少保留 10 GiB 空闲。

### 备份

在 **备份与恢复** 点击 **注册外盘目录**，输入另一块物理磁盘上的空目录并再次确认路径。然后选择该目录，点击 **创建快照**，完成后 **校验快照**。

同一 SSD 分成 C/D 两个盘符不算两块物理磁盘；无法确认物理映射时会拒绝注册。设置里的“备份目标路径”不能替代注册步骤。当前快照通过按钮手动创建，别把开启每日采集误认为每天也会创建快照。

注册外盘后，新的 Run 意图与费用预留需要同步过去；外盘断开可能阻止新 Run。独立费用账本用于避免恢复旧快照后预算倒退。

### 恢复

1. 选中快照，点击 **恢复到新目录…**。
2. 指定新的空目录并确认；不能覆盖现有活动数据。
3. 完成后先停止旧数据目录的 Worker，再用新目录启动，例如 `py -3 desktop_app.py --root D:\ACGCollectorRestored`。
4. 检查帖子、任务、费用和权限，重新配置必要凭据、注册备份目录，再决定是否恢复新任务和计划。

恢复会合并删除记录与费用账本，重新校验许可，默认暂停新任务并关闭每日计划。0.0.9 会从原 Dataset 重读需要重建的导入内容，避免继承缺少对应帖子的分页进度；这是读取原 Run，不是重新创建 Run。

恢复失败的目标目录会保留封锁标记。请另选空目录重试，不要手动删除标记绕过校验。原数据盘完全丢失时可运行 `py -3 recover_backup.py --help`，按参数指定 vault、快照、空目标目录及确认 store ID。

## 8. 升级、验证与常见问题

### 使用习惯

- 日常：启动 → 看任务/费用 → 手动采集或检查计划 → 审核 → 按需备份。
- 离开前：分清是只退出界面、暂停新任务，还是停止 Worker。
- 升级前：停用计划、停止 Worker，核对已有远端 Run，创建并校验快照。

可以继续使用源码启动方式。若希望按版本安装，运行 `Install_User.cmd`，之后使用 `Run_Installed.cmd`。安装程序把代码放入独立版本目录，不覆盖数据目录；不要让两个版本同时写同一数据目录。

在项目目录执行 `py -3 manage_release.py verify` 校验发布文件；`py -3 validate.py` 运行自动测试，结果写入 `docs/TEST_RESULTS.txt`。桌面测试需要先安装依赖，缺少 Qt 时会跳过相应测试。`py -3 run_acceptance.py --count 5` 运行离线试点报告，也支持 20、50、150；这些模拟规模不授权真实扩档。

| 遇到的情况 | 优先处理 |
|---|---|
| 找不到 `py` / Python | 检查 Python 安装及 Windows Python Launcher |
| 缺少 `PySide6` | 在实际启动使用的 Python 环境运行 `Install_Desktop.cmd` |
| `ACCOUNT_POLICY_BINDING` | 核对稳定 ID、VERIFIED、身份来源及策略绑定 |
| `LIVE_ACTOR_PRICE_REVIEW_REQUIRED` | 核对 Build、定价证据有效期、最低收费及封顶支持 |
| `BUDGET_BLOCKED` | 检查未结算/UNKNOWN 任务与累计额度，先对账 |
| `JOB_CONTEXT_CHANGED` | 对照任务创建时账号、策略及 Actor 配置，核实变化后再恢复 |
| `BACKUP_REQUIRES_OTHER_PHYSICAL_DISK` | 确认另一物理盘且磁盘映射可识别，不只换盘符 |
| `MANUAL_SYNC_TEST_REQUIRED` | 各 ACTIVE 账号先完成当前配置下的手动同步 |
| `RESTORE_INCOMPLETE_ROOT_BLOCKED` | 使用新的空目录重新恢复，保留失败目录供诊断 |
| 操作超时 / 结果未知 | 先刷新任务、快照或目标状态，确认结果后再决定重试 |
| 需要定位故障 | 在“设置与日志”导出诊断摘要，结合错误代码和 Console 检查 |

真实扩档通过 **预算与存储 → 扩档人工审批**，按 5 → 20 → 50 → 150 逐步推进，需要上一档真实结果、账单及人工证据；总预算不会随扩档自动增加。

更多实现与验收说明：[0.0.9 修复记录](docs/V0_0_9_FIXES.md) · [发布门禁](docs/release-gates.json) · [测试记录](docs/TEST_RESULTS.txt) · [设计规格书](docs/SPEC_v1.0.md)。

---

<a id="english-guide"></a>

# English User Guide

**Apify X Collector v0.0.9** is a local desktop tool for collecting X account updates through Apify, reviewing posts, downloading permitted media, tracking costs, and managing backups for the ACG information app.

The default mode is **OFFLINE**. It uses synthetic data and does not require a token or incur Apify charges. Live collection requires verified account identity, source permissions, and a compatible Actor with reviewed pricing.

The recorded automated suite passed 150 tests, including Qt offscreen and separate Worker tests. Native Windows scheduling/ACL/Named Pipe behavior, real two-disk recovery, and a live Apify pilot still require validation.

## 1. Install and try the offline workflow

1. Install **64-bit Python 3.10+**, available through `py -3` on Windows.
2. Download this repository using **Code → Download ZIP** and extract it into a user-writable directory.
3. Run `Install_Desktop.cmd` once to install hash-locked desktop dependencies. Network access is required.
4. Run `Run_Desktop.cmd` to open the application.
5. On the dashboard, click **首次使用：载入离线示例** (Load offline demo).
6. Open **画师账号** (Accounts), select the demo account, and click **采集所选账号** (Collect selected account).
7. Watch **任务与 Runs** (Jobs/Runs) until the job reaches `DONE`, then inspect and review posts in **帖子与审核** (Posts/Review).

If the window closes immediately, run `py -3 desktop_app.py` from the project directory to inspect the error. Keep demo and live data separate: `py -3 desktop_app.py --root D:\ACGCollectorDemo` starts with a dedicated demo root. Use another root for live work.

The current interface is Chinese; the labels below match the buttons you will see.

## 2. Navigation and background behavior

| Page | Purpose |
|---|---|
| 总览 — Dashboard | Status, pause new work, load demo |
| 画师账号 — Accounts | Identity, account settings, manual collection |
| 任务与 Runs — Jobs/Runs | Progress, resume, UNKNOWN reconciliation, final bills |
| 帖子与审核 — Posts/Review | Filters, text, review, original links, CSV, deletion |
| 媒体素材库 — Media | Queue, download, repair URLs, permitted preview |
| 预算与存储 — Budget/Storage | Cumulative cost, disk limits, staged expansion |
| 权限与合规 — Policies | Separate permissions for each operation |
| 备份与恢复 — Backup/Recovery | Register a vault, create/verify/restore snapshots |
| 设置与日志 — Settings/Logs | Mode, Actor, token, schedule, diagnostics, Worker shutdown |

Select a table row before using a “selected” action. Tables show at most 100 rows per page; column sorting applies to the current page.

Closing or exiting the GUI does **not** necessarily stop the independent Worker. When a tray host is available, closing the window may minimize it to the tray. **暂停新任务** pauses new jobs; existing work may continue. Use **停止 Worker…** to stop local background processing.

An Apify Run already created remotely may continue and incur charges even after the GUI or Worker stops. Check Apify Console separately.

## 3. Prepare live collection

Follow this order: **account identity → permissions → token → Actor/pricing → LIVE → one manual test**.

### Account identity

In **画师账号 → 添加账号**, enter:

- **X Handle:** current username without `@`.
- **稳定 X User ID:** numeric user ID, not a username or post ID.
- **身份核验来源:** identity-verification links, one per line.
- **身份核验状态:** use `VERIFIED` only after checking identity; otherwise keep `PENDING`.
- **来源策略 ID:** your identifier for the matching permission group, such as `artist-source-001`.
- **账号分级:** `SFW`, `R18`, or `UNKNOWN`; this does not replace individual media review.
- **采集状态:** `ACTIVE` when ready, `PAUSED` when collection should stop.

Incomplete candidate accounts can be saved, but a handle alone is insufficient for collection. Complete configuration before starting a job; changing account or policy details during a job may block content resumption until reviewed.

### Permissions

Use **权限与合规 → 新增许可记录**. The source must be `x:<numeric user ID>`, and the policy ID must match the account.

| Operation | Meaning |
|---|---|
| `access` | Access/collect from the source |
| `retain` | Keep data locally |
| `download_media` | Download media copies |
| `redistribute` | Eligibility for later redistribution |

Collection requires valid `access` and `retain` permissions. Metadata uses the `METADATA` scope; retaining text requires `TEXT`. Media needs the applicable `MEDIA` scope and download permission.

Record real evidence, reviewer, retention/display/deletion rules, and an expiry in UTC, for example `2027-01-01T00:00:00Z`. Unknown, denied, revoked, or expired permissions can block operations. Demo `mock:` evidence cannot authorize live collection.

Revoking `retain` can delete retained content. Check the source carefully before entering the policy ID to confirm.

### Token, Actor, and pricing

1. Use **设置与日志 → 配置 Apify Token** to save your token locally.
2. Open **应用设置** and provide the Actor ID, verified Build, pricing evidence and expiry, minimum charge, and reviewed PPE charge-cap support.
3. Switch the mode to `LIVE` once these prerequisites are ready.

The bundled live adapter targets the expected fields of `scrapesage/twitter-scraper`. Entering a different Actor ID does not make its input/output compatible automatically. Review the adapter before changing Actor or Build. The default `unverified` Build blocks live execution.

Tokens belong in the private local configuration, not in Git. Switching to LIVE alone does not launch a Run.

### Start one account

Select a prepared account and click **采集所选账号**, then confirm the live cost prompt. Monitor both Jobs/Runs and Apify Console.

Initial requests cover up to the last 30 days. Incremental requests use stored checkpoints with a default 48-hour overlap and deduplication. Actual coverage depends on Actor results; a complete 30-day history is not guaranteed.

The hard limits are **US$0.50 per Run** and **US$10 cumulative pilot budget**. Outstanding reservations count toward the budget, which does not reset monthly. Settle the first live job before proceeding.

## 4. Resume jobs and reconcile costs

| State | What to do |
|---|---|
| `RUN_INTENT` | Creation intent is stored. Check Console before assuming no remote Run exists. |
| `RUNNING` | Remote execution or polling is in progress. |
| `READING` | The existing Dataset is being read page by page. |
| `DONE` | Import finished; coverage and billing may still need review. |
| `NEEDS_REVIEW` | Inspect data problems, then resume the existing job. |
| `FAILED` | Check logs, Console, and any cost already incurred. |
| `REMOTE_OUTCOME_UNKNOWN` | A paid Run may exist. Reconcile before attempting new collection. |
| Coverage `PARTIAL` / `UNKNOWN` | Evidence is insufficient to claim complete coverage. |

For UNKNOWN, identify the Run in Console by Actor, input/account, and time. Select the local job, click **核对 UNKNOWN Run**, and provide the verified Run ID and evidence URI. Then use **恢复所选** to continue the existing Run. Use this same resume action after a Dataset page failure rather than creating another paid Run.

When eligible for billing, click **登记最终账单** and enter the final decimal USD amount plus bill evidence. `observed_usd` is an observed amount, not a final invoice. Unsettled jobs block new paid Runs.

Version 0.0.9 allows historical billing after permission revocation or handle changes while still checking stable account/source and Actor identity. This does not reauthorize source-content access.

## 5. Review posts and download media

In **帖子与审核**, inspect the text or original post and use **人工审核** to record reviewer, evidence, rating, and notes. Normally move from `STAGING` to `REVIEWED` first. `PUBLISHABLE` requires additional permission checks and is only an internal status: it does not publish anything automatically.

CSV export is a review-metadata export, not a full backup. Deleting a post requires its Post ID and cleans related history and registered media, with an erasure barrier to prevent resurrection. Disconnected backup cleanup remains pending; unknown copies made outside the application are not automatically erased.

For media, use **添加下载任务**, enter an existing Post ID, permitted HTTPS URL, reviewed rating, evidence, and retention expiry. Select the task and click **下载所选**, then **授权预览** after success.

Media failures do not discard saved posts. UNKNOWN media is not automatically downloaded; R18 requires explicit individual review and restricted content is blocked. Use **修复链接** for permanently invalid URLs before retrying.

## 6. Enable a daily schedule

First complete a manual sync for every ACTIVE account and register a backup vault on another physical disk.

1. In **应用设置**, set the local time and IANA time zone; defaults are `09:00` and `America/Vancouver`.
2. Ensure Windows uses the matching system time zone. Supported mappings currently cover Vancouver/Los_Angeles, Toronto, UTC, and Tokyo.
3. Open **每日计划…** and type `ENABLE` to confirm.

The Windows task runs as the current user, with daily and logon triggers. It does not promise execution while the user is logged out. Missed days are merged into catch-up work; repeated DST times run once. Enabling after today's scheduled time may trigger catch-up.

Permissions, manual-test history, physical backup disk, budget, and account count are checked before enabling. Disable through **每日计划…**; closing the GUI does not disable scheduling. Creating daily collection tasks does not automatically create daily snapshots.

## 7. Storage, backups, and recovery

| Item | Default Windows location |
|---|---|
| Active data | `%LOCALAPPDATA%\ACGCollectorData` |
| Private token/IPC configuration | Per-data-root subdirectory under `%LOCALAPPDATA%\ACGCollector` |
| Versioned application | `%LOCALAPPDATA%\ACGCollectorApp\versions\0.0.9` |
| Backup vault | Another physical disk chosen and registered by you |

Data uses JSONL events and Manifests. Do not manually edit journals, erasure barriers, or accounting records. Default storage limits are 50 GiB of active data and at least 10 GiB free on the disk.

### Back up

In **备份与恢复**, use **注册外盘目录** and confirm an empty directory on another physical disk. Select it, choose **创建快照**, then **校验快照**. Different drive letters on the same SSD do not qualify. An unidentifiable physical mapping is refused; setting a path in application settings does not register it.

After registration, new Run intents and reservations must also reach the vault. Disconnecting it may therefore block new Runs. A separate accounting ledger prevents an older snapshot from restoring an artificially larger remaining budget.

### Restore

1. Select a snapshot and click **恢复到新目录…**.
2. Confirm a new empty directory; existing data cannot be overwritten.
3. Stop the old data root's Worker and launch with the restored root, for example `py -3 desktop_app.py --root D:\ACGCollectorRestored`.
4. Review posts, jobs, costs, and permissions; reconfigure credentials and register the backup vault before re-enabling new jobs or scheduling.

Restore merges erasure/accounting records and checks permissions. New jobs are paused and scheduling is disabled by default. Version 0.0.9 replays necessary imports from the original Dataset rather than trusting newer page offsets that lack matching restored posts. This reads an existing Run rather than creating a new one.

If restore fails, use another empty directory. Do not remove the in-progress seal to bypass validation. If the original disk is lost, run `py -3 recover_backup.py --help` and provide the vault, snapshot, new target, and confirmed store ID.

## 8. Upgrade, verify, and troubleshoot

Before upgrading, disable scheduling, stop the Worker, check remote Runs, and create/verify a snapshot. Continue with the source launcher, or run `Install_User.cmd` for a versioned install and then `Run_Installed.cmd`. Installation keeps application code separate from data; do not let two versions write the same data root.

Useful commands from the project directory:

- `py -3 manage_release.py verify` — verify the release file manifest.
- `py -3 validate.py` — run tests and write `docs/TEST_RESULTS.txt`; install Qt dependencies first or desktop tests will be skipped.
- `py -3 run_acceptance.py --count 5` — generate a Mock pilot report; counts 20, 50, and 150 are also supported. Mock results do not authorize live expansion.

| Problem | First check |
|---|---|
| Python/`py` not found | Python and Windows Python Launcher installation |
| Missing `PySide6` | Install dependencies in the Python environment used by the launcher |
| `ACCOUNT_POLICY_BINDING` | Stable ID, VERIFIED status, identity evidence, policy binding |
| `LIVE_ACTOR_PRICE_REVIEW_REQUIRED` | Verified Build, valid pricing evidence, minimum charge, cap support |
| `BUDGET_BLOCKED` | Outstanding/UNKNOWN jobs and cumulative budget; reconcile first |
| `JOB_CONTEXT_CHANGED` | Account, policy, and Actor changes since job creation |
| `BACKUP_REQUIRES_OTHER_PHYSICAL_DISK` | Another physical device with an identifiable mapping |
| `MANUAL_SYNC_TEST_REQUIRED` | Each ACTIVE account must finish a manual sync under current settings |
| `RESTORE_INCOMPLETE_ROOT_BLOCKED` | Retry into another empty directory; retain the failed root for diagnosis |
| Timeout or unknown result | Refresh jobs/snapshots/target state before deciding whether to retry |
| Further diagnosis needed | Export a diagnostic summary from Settings/Logs and compare with Console |

Live expansion uses **扩档人工审批** in Budget/Storage: 5 → 20 → 50 → 150, with prior real results, final bills, and reviewer evidence. Expansion does not raise the cumulative budget.

See [v0.0.9 fixes](docs/V0_0_9_FIXES.md), [release gates](docs/release-gates.json), [test results](docs/TEST_RESULTS.txt), and the [design specification](docs/SPEC_v1.0.md).
