# M0–M3 实施与验收报告

版本 0.0.3，继承 M0–M1。M2–M3 新增 21 项测试；现有 53 项新系统测试 + 6 项原型基线，共 **59 项通过**。真实输出见 TEST_RESULTS.txt；Linux + Python 3.12.14。Windows 11/NTFS 实机仍待验收。

M2：标准库 Apify REST Transport（Bearer 头、固定主机、禁止重定向、有界响应、POST 不重试）、FakeTransport、Actor Registry、严格 ScrapeSage/Fixture Adapter、持久化 RUN_INTENT 与预算预留、远端 Run 查询、Dataset 分页、UNKNOWN 人工核对与费用最终对账。金额 Decimal 计算，单次 0.50/累计 10.00 默认限制；预算包括未结算预留。

M3：首次 30 日候选窗口，48 小时重叠增量，ID 去重，分页续读，按账号独立任务，Quarantine、失败保留、重试审核、窗口与 Coverage 报告、最终检查点与 job 同批原子提交。单次 advance 有界，HTTP 读超时可恢复，无后台无限循环。

验证证据：POST 超时跨重启 creates=1；中途页超时从 offset=2 续读，保留前两条且不提前推进游标；20 个账号第 13 个失败，19 个成功账号的数据与检查点保留；连续 20 次各 0.50，累计 10.00 后第 21 次被阻断；账单未知阻断，证据对账后释放预留；无 access 授权时 creates=0。

实时接口未发起任何收费请求；Transport 单元测试使用模拟 HTTP opener。官方接口依据：
- https://docs.apify.com/api/v2/actors-runs-post
- https://docs.apify.com/api/v2/actor-run-get
- https://docs.apify.com/api/v2/dataset-items-get

边界：真实 Actor build/input/output/定价/授权仍待外部核验，默认 unverified 阻断 live。观察费用不自动认定最终费用；人工最终账单证据需自行核对真实性。当前不实现自动列出远端任务猜测 UNKNOWN 对应 Run，不盲目重跑；提供人工核对绑定接口。Coverage 当前保守 PARTIAL/UNKNOWN，未实现外部覆盖证据录入和 VERIFIED 认证流程。不声称 30 天完整。媒体下载/素材映射、物理清理、GUI/Tray、调度、备份与 ACL 均留待 M4–M7；未取得完整 G1/G2/G3 发布资格。

事件日志统一包含 JOB_STATE 的预算/运行/页进度历史；不是将临时观察值覆盖成不可追溯单文件。M1 数据向前可读，新增事件后不可直接降级旧程序。Schema 1.0 新增 job 契约，旧数据无需重写；旧版本严格验证器不识别新事件时会阻断。

下一阶段：M4 媒体队列、四维权利门禁、磁盘配额、分级隔离与物理清理。
