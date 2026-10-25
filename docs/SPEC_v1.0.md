# ACG 情报 App｜Apify X 数据采集器最终 MVP 设计规格书

- 文档版本：**v1.0（设计冻结候选）**
- 日期：2026-10-24（America/Vancouver）
- 决策来源：GrillMe **Q1–Q37 全部确认**
- 实现状态：**仅规格书；尚未按本规格实施**。既有 `apify_x_probe_v0_1` 仅为验证原型。
- 目标运行环境：Windows 11，本地文件型持久化、独立后台 Worker、完整桌面管理 GUI 与 Tray。
- 性质：工程设计，不构成平台授权或法律意见。

## 0. 执行摘要和决策优先级

本项目是 ACG 情报 App 的**前期数据获取与积累基础设施**，目标是在核实来源接入及保存权限的前提下，获取日本 ACG 画师账号的近期动态，在本地建立可审计、可恢复、可扩容、最终可迁移至 Spring Boot 后端的数据资产。

**冻结的用户决策优先于本文的工程默认建议。** 所有“建议默认值”均为实施参数，可在不违反安全不变量的前提下从 GUI 修改，不视为用户另行作出的产品决策。

关键约束：

1. **Q4=C、Q8=A**：CSV/JSON 文件路线，**JSONL 为主、CSV 为审查导出**；不引入 SQLite 为当前主库。
2. **Q3=C、Q5=A、Q22=A**：尽可能保留被允许保存的文本、图片、视频及其元数据；媒体独立队列和磁盘索引；**不因技术可下载就默认获得保存权限**。
3. **Q29=B**：本地 `.env` 保存 Apify Token；不得进源码、日志、备份或共享输出。
4. **Q30=C、Q34=C**：完整桌面管理软件 + 系统托盘；采集 Worker 独立于 UI。
5. **Q32=A**：初始**每次 Apify Run 上限 US$0.50、试点累计上限 US$10**；超限停止创建新收费任务。
6. **Q33=B**：活动数据总容量上限 **50 GiB**，主磁盘保持至少 **10 GiB** 可用；备份在另一块物理磁盘（Q35=A）。
7. **Q36=A**：**先离线模拟并验收**；仅获得适用来源访问、保存、使用权限后执行真实 X Actor 测试。
8. **Q37=A**：由 GUI 手动启用每日计划，默认**本地上午 09:00**，可修改；开机漏跑后补跑。
9. **Q2=B、Q28=A**：至少 5 个符合测试条件的账号中 4 个成功；不虚报 30 天完整率，质量、费用与权限分别验收。
10. **Q25=A**：聚焦可靠采集型 MVP；**AI 摘要和 Spring Boot 实库集成不属于 MVP**。

## 1. 决策追踪矩阵（Q1–Q37）

| 编号 | 确认选项 | 设计约束 |
|---|---|---|
| Q1 | A | 本地独立积累，审核后未来可能迁入正式数据库 |
| Q2 | B | 5 账号至少 4 个成功、字段准确、成本可接受、记录漏帖 |
| Q3 | C | 目标覆盖全文/图片/视频/元数据；受许可及安全门禁约束 |
| Q4 | C | 文件型 CSV/JSON 存储，而非主库 SQLite |
| Q5 | A | 独立媒体目录 + 元数据和哈希索引 |
| Q6 | A | 手动测试通过后才启用每日同步 |
| Q7 | A | 可配置主 Actor 和备用 Actor |
| Q8 | A | JSONL 主存储，CSV 辅助人工查看 |
| Q9 | A | X Post ID 跨 Actor 去重主键 |
| Q10 | A | 版本历史、Tombstone、按权限清理 |
| Q11 | A | 全局/单批配额，哈希去重，超限有状态不默删 |
| Q12 | A | 原子写入、快照、备份、恢复校验 |
| Q13 | A | Schema 先行和版本迁移，原始响应按权限保留 |
| Q14 | A | `since_id` 可用则用 + 时间重叠窗口；否则已知 ID 比对 |
| Q15 | A | 已成功账号数据保留，失败账号单独续跑 |
| Q16 | A | 5 → 20 → 50 → 150 阶段扩张 |
| Q17 | A | 单次费用上限 + 本地累计预算 |
| Q18 | A | Windows 本地调度 + 开机补跑 |
| Q19 | A | Actor 字段严格规范化，异常进入 Quarantine |
| Q20 | A | VERIFIED/PARTIAL/UNKNOWN 覆盖状态 + 人工抽样 |
| Q21 | A | 原创/引用帖优先；回复补充；普通转发不直推首页 |
| Q22 | A | 帖子提交与媒体下载独立；失败可重试 |
| Q23 | A | 采集许可 + 保存/再利用许可双层门禁 |
| Q24 | A | Staging → Reviewed → Publishable 分层，公开另验 |
| Q25 | A | 可靠采集、文件存储、审核、恢复、GUI 为 MVP |
| Q26 | A | SFW/R18/UNKNOWN 账号与帖子分别标记；分级隔离 |
| Q27 | A | 可追踪删除、缓存/媒体/备份关联清理 |
| Q28 | A | 严格分层验收，4/5 与关键字段及覆盖证据分别计 |
| Q29 | B | `.env` 本地凭据方案；访问权限/脱敏硬约束 |
| Q30 | C | Windows Tray GUI，不能只有 CLI |
| Q31 | A | Fail-closed、故障注入、恢复与预算核账 |
| Q32 | A | 单次 $0.50，试点累计 $10 |
| Q33 | B | 总 50 GiB，磁盘空闲保护 10 GiB |
| Q34 | C | 完整桌面管理 GUI + 托盘，含可授权媒体预览和高级筛选 |
| Q35 | A | 另一块物理磁盘存储备份 |
| Q36 | A | 先 Mock/离线完整验收；真实测试先过许可门禁 |
| Q37 | A | GUI 手动开通；当地 09:00 默认、可调、漏跑补齐 |

## 2. 产品范围与不在范围

### 2.1 MVP P0 功能

- 画师账号的人工维护、来源身份校验、R18 账号标签；初期样本以已有候选名单为**候选输入**，并非自动认可或入正式 Entities。
- Actor Registry、ScrapeSage Adapter、备用 Actor Adapter 接口、Actor 运行/数据集读取、失败与异常隔离。
- 权限审查与证据保存；来源级、操作级的 Permission Gate。
- 首次近 30 日候选回填；每日增量检查、重叠窗口、覆盖评估、失败按账号重试。
- 规范化推文、Post ID 去重、内容历史版本、原始记录（仅当获准）、Tombstone。
- JSONL 事件主存储、提交清单、派生索引、CSV 审核导出、恢复与另一磁盘备份。
- 图片及视频的**获准**下载、校验、哈希去重、R18/UNKNOWN 隔离、磁盘配额、状态化重试。
- 桌面管理 GUI（仪表板、账号、任务、审核、素材、预算、权限、备份、日志）+ Tray + 独立 Worker + Windows 调度。
- 费用总账、未结算任务预留、账单对账、手动授权运行、限额保护。

### 2.2 MVP 不做

- 自动 AI 日中翻译、摘要、新闻价值预测、AI 内容训练；留给后续独立模块。
- 自动上传到 Google Sheets/Drive、正式 Spring Boot 数据库、App 首页自动发布。
- 绕过 X 限制、模拟登录、验证码规避、代理轮换等访问控制规避。
- 为了堆积数据强制归档不可合法保存的全文或媒体；未知许可条件下保持阻断。
- 声称“只要设置 30 日就一定完整抓到 30 日所有推文”。

## 3. 系统架构

### 3.1 进程与边界

**Desktop GUI**：全功能桌面窗口和系统托盘，查看状态与发出用户操作，不直接操作 Apify 网络访问、JSONL 提交或媒体下载。建议技术实现 Python GUI（如 PySide6），这是工程建议而非用户选定框架。

**Background Worker**：独立普通用户进程，运行调度、访问许可校验、Actor 任务、规范化、存储和媒体下载。可在 GUI 退出后继续已启动工作；任务计划程序可以启动未登录 GUI 的 Worker。GUI 退出与 Worker 关闭是两种不同操作。

**Task Scheduler**：Windows Task Scheduler 每天当地时间 09:00 默认触发。计划需经 GUI 显式启用。未运行期间的遗漏日期作为**补跑待办**，不盲目为每个错过日创建付费任务；同一账号仅一次逻辑更新窗口并报告可能存在的覆盖缺口。

**IPC**：建议采用仅本机可访问、同用户会话校验的 Named Pipe（或具有严格令牌鉴权的 loopback 接口）。GUI 只发送语义命令 `start/pause/resume/review/settings`；Worker 负责再校验安全门禁。

### 3.2 业务模块接口（契约级）

| 模块 | 主要输入 | 返回/事件 | 出错语义 |
|---|---|---|---|
| `PolicyGate` | source_id, operation, purpose, policy_version | Permit / Deny / Review + evidence_id | UNKNOWN → DENY |
| `AccountRegistry` | 候选账号、别名、身份来源 | account_id、verification_status | 未核实身份不得标记 VERIFIED |
| `ActorRegistry` | actor_id、build、input_schema、能力描述 | Adapter 实例 + capability_flags | 未支持功能明确报错 |
| `ActorRunner` | FetchRequest、budget_reservation_id | run_id、status、dataset_id、cost_status | POST 结果未知不得无条件重试 |
| `DatasetReader` | run_id、dataset_id、offset | RawItemBatch、next_offset | 分页未结束不得推进游标 |
| `PostNormalizer` | 原始记录、Actor 映射版本 | StandardPost 或 Quarantine | 缺关键 ID/身份/时间 → Quarantine |
| `Deduplicator` | (platform, post_id, content_version_hash) | INSERT/REVISION/DUPLICATE | 一个 Post ID 一个当前版本 |
| `CoverageEvaluator` | 目标窗口、已知记录、外部证据 | VERIFIED/PARTIAL/UNKNOWN + reason | 无证据不返回 VERIFIED |
| `MediaManager` | media_request、rights_policy、quotas | queued/downloaded/failed/blocked | 媒体失败不回滚帖子 |
| `StorageEngine` | 验证后的事件批、rights_scope | commit_id、manifest_hash | 仅返回 durable COMMITTED 才可更新游标 |
| `SyncCoordinator` | account_id、cursor、window、due_time | checkpoint、job_status | 失败账号独立隔离与重试 |
| `BudgetManager` | actor_price_model、estimated/actual、reserved | Permit/Block、ledger_entry | 金额未知暂停新付费 Run |
| `ReviewManager` | post_id、action、reviewer | append-only 审核事件 | 不允许 Staging 直接 Publishable |
| `DeletionManager` | account/post/media 请求、证据 | Tombstone、cleanup_report | 需要清理派生物、快照及备份关联项 |
| `Scheduler` | local timezone、enabled、last_due | due jobs | 禁用或权限不满足时不运行 |
| `GuiService` | Worker 状态与用户意图 | UI ViewModel | UI 不能绕开核心门禁 |

每个接口应使用明确的 `schema_version`；适配器不能将不支持的 `since_id`、帖子分类或媒体能力伪装为支持。

## 4. 本地存储模型与 JSONL Schema

### 4.1 目录布局（逻辑）

| 相对目录/文件 | 用途 |
|---|---|
| `accounts/accounts.jsonl` | 账号观察记录、身份核验、handle 历史 |
| `policies/policies.jsonl` | 获取/保存/媒体/展示四种权限及证据 |
| `posts/post_events-*.jsonl` | **Source of Truth**：帖子新增、版本、撤回事件 |
| `raw/` | 经许可的 Actor 原始响应片段；默认不假设可无限期保存 |
| `media/media_events-*.jsonl` | 媒体任务、许可、hash、状态 |
| `media/blobs/<hash-prefix>/` | 经许可下载且完成校验的二进制文件 |
| `runs/run_events-*.jsonl` | Apify Run 生命周期及 Dataset 游标 |
| `sync/checkpoints.jsonl` | 账号同步检查点和覆盖状态 |
| `ledger/budget_events.jsonl` | 费用预留、结算、冲正、人工审批 |
| `reviews/review_events.jsonl` | 人工审核结果与理由 |
| `deletions/tombstone_events.jsonl` | 删除、失效、撤回和清理证据 |
| `quarantine/` | 不合规、不可信、损坏或身份不明的非敏感诊断记录 |
| `journal/` | 待提交事件批、提交清单、恢复日志 |
| `indexes/` | 可重建的当前态 Post ID 索引及其他派生视图 |
| `exports/` | CSV 审核表与质量报告（过滤未获准字段） |
| `snapshots/` | 经权限过滤的可恢复快照 |
| `logs/` | 脱敏操作日志 |

备份文件在**另一块物理磁盘**，不与上述根目录混用。GUI 提供备份路径验证。

### 4.2 事件通用信封

所有持久化 JSONL 事件均应包含：

- `schema_version`（初版 `1.0`）、`event_id`（全局唯一）、`event_type`、`recorded_at`（UTC ISO 8601）、`commit_id`（仅已提交批次有效）。
- `source_id`、`actor_run_id`（可空，人工来源可无 Actor）、`rights_policy_id`、`rights_policy_version`。
- `payload`（对象）、`payload_hash`（用于完整性）、`retention_class`、`redaction_state`。

**不能将用户账号改名视为新画师**；X 平台数值 Post ID 必须作为**字符串**保存，避免精度损失和跨语言整数处理问题。

### 4.3 `accounts` 标准字段

`account_id, entity_id, platform, external_user_id, current_handle, handle_history, verified_source_urls, verification_status, account_content_rating, source_policy_id, observed_at, active_status`。

- `external_user_id` 应优先保存平台稳定 ID；仅有 handle 的账号处于待核验状态。
- `entity_id` 是未来 App Entities 的外键候选，不允许仅凭近似名称强行绑定。

### 4.4 `post_events` 标准字段

| 字段 | 类型 / 说明 |
|---|---|
| `platform`, `post_id` | 跨 Actor 主键，X Post ID 为字符串 |
| `account_id`, `author_external_id`, `author_handle_at_observation` | 归属与账号身份 |
| `post_type` | ORIGINAL / QUOTE / REPLY / REPOST / UNKNOWN |
| `quoted_post_id`, `in_reply_to_id`, `reposted_post_id` | 关系字段，允许为空 |
| `published_at`, `first_seen_at`, `observed_at`, `indexed_at` | 原帖、发现、观察、入库时间分离 |
| `source_url`, `language`, `text` | 文本字段只能按许可保存 |
| `media_refs`, `metrics`, `hashtags`, `mentions` | 按 Actor 实际支持字段及许可保存 |
| `version_number`, `content_hash`, `is_current` | 可演算当前态，不应成为双写真相源 |
| `content_rating` | SFW / R18 / UNKNOWN |
| `review_status` | STAGING / REVIEWED / REJECTED / PUBLISHABLE（尚不公开发布） |
| `rights_status` | PERMITTED / RESTRICTED / UNKNOWN / REVOKED |
| `coverage_evidence_id`, `raw_ref` | 覆盖证据和获准原始数据引用 |
| `deletion_state` | ACTIVE / TOMBSTONED / PURGED |

**约束**：同一 `(platform, post_id)` 可有多个历史事件，当前视图只有一份；`version_number` 单调递增。无法确认关键信息的记录进入隔离区，不得从错误文本自动生成 Post ID。

### 4.5 其他 JSONL 记录

- `media_events`：`media_id, post_id, media_kind, source_url, rights_status, requested_at, download_state, sha256, mime_detected, file_size, local_path, error_code, retry_count, last_checked_at`。媒体相同 hash 不代表转载许可可跨来源继承。
- `run_events`：`job_id, actor_id, actor_build, input_fingerprint, run_id, dataset_id, requested_window, run_status, began_at, completed_at, downloaded_offset, final_dataset_count, cost_status`。
- `sync.checkpoints`：`account_id, previous_cursor, requested_window, overlap_start, candidate_cursor, committed_cursor, coverage_status, coverage_reason, last_success_at`。
- `budget_events`：`accounting_scope_id, job_id, run_id, event_type, reserved_usd, finalized_usd, currency, source, finalized_at, approved_by`。
- `policies`：`policy_id, source_id, operation, permission_state, evidence_uri, reviewed_at, reviewer, valid_until, retention_rule, display_rule, deletion_rule`。访问、全文保存、媒体下载、对外展示权限必须分别判定。
- `review_events`：`post_id, reviewer_id, action, category, source_evidence, notes, reviewed_at`。
- `tombstone_events`：`subject_type, subject_id, request_reason, policy_ref, requested_at, cleanup_scope, cleaned_at, verification_status`。

### 4.6 CSV

CSV 是**派生视图**，包含已许可的审查字段，例如 Post ID、画师、时间、帖子类型、来源链接、是否含媒体、覆盖状态、审核状态、许可状态；不得另作真相源。UTF-8 BOM 对 Windows Excel 兼容可作为导出选项；公式注入字符应安全转义。

## 5. 提交协议、去重与同步游标

### 5.1 单写者和提交清单

因为 JSONL 不提供数据库 ACID 事务：

1. 同一数据根目录只能有**一个活跃写入 Worker**（跨进程互斥锁 + owner/PID 信息）；UI 不直接写事件。
2. 形成带唯一 `commit_id` 的待提交 Segment；以同卷临时文件写出并校验完整性，必要时执行文件同步。
3. 将 Segment 原子替换到固定目录，生成含字节数与哈希的 Commit Manifest，并原子更新当前 Manifest 指针。
4. **只有 Manifest 指针指向的完整、已校验 Segment 属于已提交数据**。索引、CSV 和当前态可以重建。
5. `committed_cursor` 随已提交事件一起成为可重放的同步状态；不得先推进进度后存数据。
6. 崩溃恢复扫描提交链，丢弃或隔离未提交 Segment，重建索引和检查点。

这些构成**本地近似事务语义**，不宣称跨 Apify 云端收费请求与本地提交实现全局 exactly-once。

### 5.2 Actor 非幂等副作用

在创建付费 Run 之前，将 `RUN_INTENT` 和预算预留持久化。POST 超时、网络断开且拿不到 `run_id` 时，标记 `REMOTE_OUTCOME_UNKNOWN`，按请求指纹、时间及 Apify 任务列表尝试人工/自动核对；**不得无条件重复提交创建 Run 的 POST**。

一旦获得 Run ID，恢复时优先查询现有 Run 和 Dataset，而不是新跑一遍。PPE 平台金额可能短暂未完成汇总；对账完成前保留预留、停止会造成累计超限的新任务。Apify 的未命名 Dataset 不能视作永久备份，必须在许可及其保留期限内完成必要读取和本地提交。

### 5.3 增量同步

- 有官方支持且 Actor 确实实现 `since_id` 等功能时才利用；否则使用最后成功帖子 + 可配置时间重叠窗口 + Post ID 比对。
- 建议初始重叠窗口 **48 小时**（工程默认，可通过 GUI 修改）；不保证与所有账号的发布频率相适配。
- 只有本次所有必需 Dataset 页成功读取、规范化与许可校验完成且已提交，才推进该账号 `committed_cursor`。
- 回复、引用帖与原创帖分别统计；普通转发不是首页直接情报候选。
- 不依赖某个 Actor 私有的 `monitorMode` 作为唯一成功游标；可以把其内部增量信息当辅助手段。
- 当 Actor 窗口过短或历史缺口不可验证时，Coverage = PARTIAL 或 UNKNOWN，不能因 `SUCCEEDED` 就写 VERIFIED。

## 6. R18、素材、删除和来源权限

### 6.1 来源与使用权限四维门禁

`access`（是否可自动访问）、`retain`（是否可保留文本/元数据）、`download_media`（是否可复制媒体文件）、`redistribute`（是否可展示/二次分发）分别判定，状态为 PERMITTED/DENIED/UNKNOWN/REVOKED。

- 各阶段未获许可或证据过期一律 **Fail-closed**，不由用户“关注画师”行为取代授权。
- Apify Actor、X 原站、作者版权、图片/视频著作权及数据保护义务分开核实。
- 允许保留事实性元数据的范围不自动包括原帖全文、图片、视频或对外 AI 摘要。
- 真实 Actor 试点须先满足所需来源访问许可；否则仅能用离线 Mock / 获准替代来源做工程测试。

### 6.2 R18 状态机

账号与 Post 分别有 `SFW / R18 / UNKNOWN` 标签。账号标记 R18 不应自动将其所有作品视为 R18。UNKNOWN 初始走隔离、不显示缩略图；明确受限制或疑似违法、涉及未成年人的性内容，不予自动下载，并进入严格隔离/人工合规处理。

完整 GUI 可提供经权限许可、经过内容与年龄/地区规则审查的媒体预览。默认审查列表仅显示安全元数据，不自动播放成人视频，不生成未经许可的缩略图缓存。

### 6.3 媒体下载队列

- 帖子和媒体**独立提交**：文章字段已获准保存时，可先提交 Post；媒体排队后续处理。
- 权限通过后才发送下载请求；仅允许 HTTPS，限制目标主机和重定向目标，禁止访问内网、回环、特殊协议、路径穿越与任意本地路径。
- 下载至临时文件；校验 MIME/签名、文件大小、内容哈希，提交后更新 Media Manifest。重复媒体使用哈希节省空间，但保留独立授权关系。
- 失败状态区分：NO_RIGHTS、NOT_FOUND、EXPIRED_URL、NETWORK_ERROR、OVERSIZE、NO_SPACE、UNSUPPORTED_FORMAT、BLOCKED_RATING。有限次重试并允许在权利检查通过后手动修复。
- 工程建议初始限制：单文件 250 MiB、单批新增 1 GiB、同时 2 个下载、可重试故障最多 2 次；这些是**尚未单独表决的技术默认值**，GUI 应允许调整至符合系统硬约束的范围。

### 6.4 权利撤回、删除与备份

- 收到失效、删帖或删除请求时，先阻断新获取与展示，然后生成 Tombstone；清理当前态、活动媒体、派生缩略图/导出以及受影响备份的内容。
- 已有历史版本**只能保留适用规则允许的部分**；必要时执行数据段重写/压缩（Compaction）、验证删除不可通过旧 Snapshot 恢复。
- 保留必要的不包含已删除受限内容的审计信息与验证结果；删除失败按缺陷处理，不能标记 Cleanup Completed。
- 原始数据与媒体不默认永久保存，按各来源 Policy 的期限执行清理。

## 7. 费用和磁盘资源治理

### 7.1 费用默认值（用户已确认）

- **单次 Run 上限 US$0.50**。
- **试点累计预算 US$10**，包括已结算金额 + 未结算的预留额度；累计额度**不自动每月重置**，需要 GUI 人工批准新预算周期。
- 同时最多 **1 个付费 Actor Run**（推荐默认）。
- 创建任务前验证 Actor 当前收费模型、事件价格、账号权限、运行参数与预计剩余预算；费用上限参数仅在适用计费模型下有意义；如 Actor 要求的最小单次费用高于 US$0.50，则拒绝运行，等待人工重新审批。
- 日志区分 `estimated`, `reserved`, `observed`, `finalized`；账单尚未最终一致或拉取失败时 BLOCK 新付费 Run。
- 本地费用账本仅为第二层保护，并非保证外部平台永不扣除超额费用；要交叉检查 Apify Console 实际账单。

### 7.2 存储默认值（用户已确认）

- **活动数据根目录硬上限 50 GiB**（包括 raw、posts、media、snapshots、logs、indexes 和导出）。
- **活动磁盘必须保留至少 10 GiB 空闲**，任何配额将被触及时阻止新的非必要写入；预留少量安全空间供日志、删除和恢复使用。
- 建议达到 80%（40 GiB）时警告；不自动删除最早文件。
- **备份必须位于另一块物理磁盘**，不能仅仅是同一磁盘的另一分区；备份磁盘容量也应单独检测并保留可恢复空间。
- 用户可从 GUI 调整配额，但不得关闭保证文件完整性和系统最低空闲空间的硬保护。

## 8. Windows GUI、自动任务和运行生命周期

### 8.1 完整 GUI 页面

1. **Dashboard**：总账号数、今日/本周新记录、覆盖状态、来源权限、错误、成本、存储与备份健康。
2. **Accounts**：画师账号、别名、X ID、身份校验、来源策略、R18 标签、回填/增量状态和手动启停。
3. **Jobs / Runs**：Run ID、Actor、版本、时间、已读取页数、状态、失败恢复、覆盖等级、费用估计与核账。
4. **Posts / Review**：帖子搜索、按类型/画师/时间/审核/权限筛选，原帖链接与人工标注；不允许直接公发。
5. **Media Library**：仅在许可及分级允许时预览缩略图/图片/视频；检索文件状态、hash、路径、下载失败修复。
6. **Budget & Storage**：US$0.50 / US$10 试点限制，预留与最终计费，50 GiB 上限、10 GiB 保留、单文件/批次限制。
7. **Policies & Compliance**：来源许可证据、授权到期、媒体权限、删除请求、审计状态。
8. **Backup & Recovery**：第二物理盘选择、完整性检测、快照、恢复演练、失败报警。
9. **Settings & Logs**：`.env` Token 状态（只显示已配置/未配置，不回显）、每日同步时刻、时区、Actor 配置、错误与诊断导出。

### 8.2 Tray 行为

托盘可显示 IDLE/RUNNING/PAUSED/NEEDS_ATTENTION；快捷操作“打开管理窗口”“开始一次手动检查”“暂停新任务”“恢复符合条件的任务”“查看错误”“退出界面”。退出 UI **不等于终止 Worker**；终止 Worker 需另设有明确确认与安全收尾的操作。

### 8.3 调度和时区

- 第一次安装默认**调度关闭**；用户在 GUI 内确认来源权限、预算、存储和备份路径后手动启用。
- 默认本地上午 **09:00**，使用 Windows 时区规则处理夏令时；本项目默认 `America/Vancouver`（系统运行时应显式保存 IANA 时区标识并正确映射 Windows 时区）。
- Task Scheduler 负责触发 Worker；开机/登录后补跑只生成一个**合并的增量任务**并标记错过天数，避免连续补跑多次产生重复费用。
- 同一账号同一同步阶段串行，防止“双 Worker”同时工作；Worker 重启时恢复已有远端 Run 状态。
- 若未登录或机器关机导致 Windows 无法启动指定用户任务，不宣称当日已同步；醒来后如实补跑或标记缺口。

### 8.4 Token 与日志安全

- `.env` 保存 Token，但放在用户专属的本地配置路径而非公共共享目录；设置 NTFS ACL，禁止普通其他用户读取。
- `.env`、日志、主数据、诊断包、备份导出均有明确的不同访问规则；`.env` 必须排除 Git、Zip 发布包、备份及上传服务。
- API Token 仅通过 `Authorization: Bearer` 请求头传输；避免 URL 查询字符串泄露。
- 错误信息和 GUI 通知必须脱敏；不把 API 响应中的不可信文本作为指令执行，不把内容直接插入 HTML 富文本执行环境。
- 默认使用普通 Windows 用户权限，不申请管理员权限运行日常同步。

## 9. 测试验收矩阵及发布门禁

### 9.1 关键自动测试

| ID | 场景 | 验收标准 |
|---|---|---|
| T01 | 无 Token / 调度未启用 | 不调用真实 Actor，不产生费用 |
| T02 | 来源许可 UNKNOWN/REVOKED | 请求被拒绝且生成可审计原因 |
| T03 | 单个 Actor 字段缺失/类型漂移 | 进入 Quarantine；已存记录不污染 |
| T04 | 5 账号有效样本 | 满足来源许可前提下真实测试至少 4/5 成功，失败可诊断 |
| T05 | 标准化 ID/作者/时间 | 已接受数据的全部必要字段通过校验 |
| T06 | 分类抽样 | 至少抽检 20 条（不足则全检），帖型正确率不低于 95%，关键身份及日期错误数为 0 |
| T07 | 30 天覆盖 | 每账号有范围、最早/最新记录、页数及 VERIFIED/PARTIAL/UNKNOWN 依据；无证据不能 VERIFIED |
| T08 | 同一 Post 重放 3 次 | 当前态重复数 0，历史事件可解释 |
| T09 | 模拟写入中断/截断 | 恢复后仅重放已提交 Segment；不存在假游标推进 |
| T10 | Actor POST 超时，远端实际成功 | 进入 UNKNOWN + 对账；不得盲目重新创建付费任务 |
| T11 | 第 13/20 个账号失败 | 前 12 个账号保留；失败账号可单独恢复 |
| T12 | 媒体下载超时、链接失效、超额 | Post 不被丢弃，媒体状态可重试/阻断，无错误完成标记 |
| T13 | R18/UNKNOWN | 未获准媒体不下载，默认不预览；分级不可越权 |
| T14 | 预算 $0.50 / $10 边界 | 超预算不创建新收费 Run；账单未知也 Block |
| T15 | 50 GiB / 剩余 10 GiB 边界 | 不执行高风险写入，不清理旧资产冒充成功 |
| T16 | GUI 关闭 / Worker 运行 | 持续运行不受 UI 关闭影响，托盘状态可恢复 |
| T17 | Windows 重启、漏跑、DST | 合并补跑、互斥防重、日期计算准确 |
| T18 | 第二物理盘备份恢复 | 内容哈希、事件、游标、权限和索引一致 |
| T19 | 删除后从旧快照恢复 | 被撤回原文、媒体、缩略图不重新进入当前数据资产 |
| T20 | Token / 诊断包检查 | 不包含 Secret；CSV 不产生公式注入；路径不可越界 |
| T21 | 迁移器重复导入 | 目标逻辑记录无重复，保持来源与发布时间 |
| T22 | 150 账号模拟负载 | 内存/任务队列受限，超限停止而非数据损坏 |

### 9.2 三层 Release Gate

**G1 Engineering Ready**：使用 Fake/Mock Actor 完成除 T04 真实试点之外所有可离线执行的测试，包括字段准确率抽样、恢复演练和成本上限模拟；无真实 X 抓取。技术问题不应被当成授权问题。

**G2 Authorized Pilot**：来源访问与数据用途获得适用许可；每次费用封顶、累计预算、5 账号真实测试、人工抽样及账单核验通过；没有证明 30 天覆盖时按 PARTIAL/UNKNOWN 保存。

**G3 Authorized Data Seeding**：上述通过之后依次 5 → 20 → 50 → 150；每档明确记录账号可用率、有效情报数量、媒体成功率、漏帖证据、人工审核时长、实际成本、删除与备份可靠性。每次扩档必须通过权限和预算复核；US$10 试点上限不足以支持任意规模的无审核增长，不自动提高预算。

## 10. 开发 Milestone 与验收结果

| Milestone | 交付内容 | 阶段出口 |
|---|---|---|
| M0｜规范冻结 | Schema 1.0、Policy 清单、Actor 功能矩阵、测试 Fixtures、目录与配置 | 全部工作包可由测试定义驱动 |
| M1｜Storage Engine | JSONL Segments、Manifest、索引、去重、Quarantine、Tombstone、恢复 | T03/T08/T09 通过 |
| M2｜Apify Integration | Actor Runner、异步状态、Dataset、适配器、预算账本 | Mock 全链路，创建 Run 未知结果可恢复 |
| M3｜Sync + Coverage | 30 日回填、游标、重叠窗、分页、失败账号重试、覆盖报告 | 不提前推进游标，不虚报完整率 |
| M4｜Media + Rights | 媒体队列、权限门禁、磁盘配额、R18/UNKNOWN、清理 | T02/T12/T13/T15/T19 通过 |
| M5｜完整桌面 GUI | Dashboard、Accounts、Jobs、Posts、Media、Budget、Policies、Logs、Tray | GUI 不绕开 Worker 与权限门禁 |
| M6｜调度 + 备份 | Windows Scheduler、开机补跑、单写者互斥、第二盘快照及恢复 | T16/T17/T18 通过 |
| M7｜安全与发布包装 | `.env` ACL、脱敏、诊断包、安装/升级/回滚、容量检查 | T01/T14/T20 通过 |
| M8｜E2E 验收及试点 | 5 账号模拟、授权后实际测试、报告、逐步扩档证据 | G1 后才可进入 G2/G3 |

M0–M8 为工程计划编号，不对应现有程序已完成的版本号；不得将任何未运行的真实测试写成已通过。

## 11. `apify_x_probe_v0_1` 升级与迁移

### 11.1 已核实的 v0.1 状态

经检查现有 `apify_x_probe_v0_1.zip`：

- `apify_x_probe.py` 为 Python 标准库单文件 CLI；默认 Dry Run；使用 `Authorization: Bearer`；可创建并轮询 Apify Run、读取 Dataset。
- 默认 5 个已知画师、30 天、每账号至多 30 条；过滤普通转发；区分 ORIGINAL/QUOTE/REPLY。
- `staging.sqlite3` 存储跨执行去重结果，另有 `review_queue.csv` 与 `report.json`；`--save-raw` 与 `--save-text` 控制保留。
- 本地 6 个 `unittest` 用例已在本次规格书准备过程中实际重新运行，全部通过。
- **未进行真实 X 采集和真实账单验收**；不可把 6 个逻辑测试等同系统已达到 MVP。

### 11.2 可复用与必须替换

| v0.1 部分 | 处理 | 理由 |
|---|---|---|
| Handle 校验和归一 | 保留并扩展 | 需要加稳定 X User ID 与 handle 历史 |
| Actor 输入构造 | 移到 Adapter | 每个 Actor 的输入字段不同 |
| `request_json` / Run/轮询/Dataset | 拆分保留 | 加幂等意图、核账与恢复机制 |
| Post 分类函数 | 保留测试基线，增强字段映射 | 兼容多 Actor、身份消歧与不明分类 |
| `normalize` | 重写为严格 Normalizer | 明确 Quarantine、来源权限与 Schema |
| `staging.sqlite3` / `add_to_staging` | **替换** | Q4=C/Q8=A 明确 JSONL 为主 |
| `review_queue.csv` | 保留为派生导出 | 不作真相源，避免双写 |
| `report.json` | 升级为覆盖/费用/权限报告 | 区分测试通过、可保存、可公开 |
| 默认 CLI 入口 | 保留为内部诊断/Worker 启动支持 | 用户主入口转为完整 GUI + Tray |
| 6 项测试 | 全部保留并扩展 | 回归基线，不得丢失 |

### 11.3 旧数据迁移

如果曾在本地生成 `staging.sqlite3`：以**只读迁移器**读取旧记录，生成 JSONL `LEGACY_IMPORT` 事件，并记录 `migration_source`、字段缺失与来源权利状态。不能在不核验来源权限的情况下将旧 `full_text`、旧 `raw_actor_items.json` 或本地媒体假定为可长期保留。

只有成功生成新 Manifest 并通过重放校验后才允许归档旧 SQLite；原文件先保留原样供人工审计，不在迁移过程中原地修改。旧报告与 CSV 不得重复当作独立 Post 导入。

## 12. 文件格式版本和未来正式数据库迁移

初版 Schema 采用 `1.0`，新版本应声明 backward compatible / migration required。每次 Schema 变更留迁移脚本设计、字段字典和测试 Fixtures。

未来映射：`accounts` → `Entities/Source Accounts`；`post_events` → `Source Records`；`policies` → 来源策略；`runs`/`checkpoints` → `Sync Runs/Sync Status`；`review_events` → 审核历史；`media_events` → 授权媒体资产；新闻 `News Events` 必须经未来单独整理，不将一帖机械地视为一条可公开新闻。

未来迁移器需要 `Dry Run → 冲突报告 → 按来源和版本幂等导入 → 核对数量/哈希/关联 → 可回滚记录` 的步骤。对现有 Google Sheets/五张表的精确字段映射须以届时最新表头为准，本文不虚构现有工作表中的具体字段。

## 13. 设计状态与未满足的外部条件

**Q1–Q37 的用户选择已经全部冻结，不存在必须再次提问的产品选项。** 尚未完成但可作为工程条件或外部事实验证的事项：

1. 真实 X/Apify 数据接入许可及其文本、媒体、长期保存、公共展示等具体用途范围。
2. Actor 当时的实际定价、可访问性、返回字段、分页/补采能力和真实 30 天覆盖质量。
3. 实机是否存在适合的**另一块物理磁盘**用于备份，及可用空闲容量。
4. Windows GUI 框架、安装打包方式、媒体推荐上限等工程默认参数的实现选择。
5. v0.1 已有记录（如有）与正式产品表结构之间的实际字段及权限复核。

这些条件未满足时，项目仍可以完成完整离线工程开发与验收，但**不能宣称已经获准真实批量采集、永久保存 X 原帖或公开分发媒体**。

## 14. 官方参考链接和材料依据

- Apify API 总览：https://docs.apify.com/api/v2
- Run Actor：https://docs.apify.com/api/v2/actors-runs-post
- Get Actor Run：https://docs.apify.com/api/v2/actor-run-get
- Dataset Items：https://docs.apify.com/api/v2/actor-run-dataset-items-get
- Apify Dataset 保存策略：https://docs.apify.com/storage/dataset
- Actor 文档：https://apify.com/scrapesage/twitter-scraper
- X Terms：https://x.com/en/tos
- 内部依据：本次对话已确认的 Q1–Q37；已检查 `apify_x_probe_v0_1.zip`（含 6 项通过的单元测试）；ACG 情报 App 已提供的产品概念文档和画师候选文档。

**文档结束：设计冻结候选 v1.0，暂不编写代码。**

