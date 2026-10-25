# M0–M1 实施与验收报告

实现版本：0.0.1。用户任务日期：2026-10-24（America/Vancouver）。规格书：SPEC_v1.0.md。

| 阶段 | 结果 | 证据 |
|---|---|---|
| M0 | 完成当前阶段代码与契约 | 15 份 JSON Schema、Policy 清单、Actor 能力矩阵、离线 Fixtures、目录和配置 |
| M1 | 离线阶段测试通过 | JSONL Segment/Manifest、索引、版本、去重、Quarantine、Tombstone、崩溃恢复 |
| T03 | 通过 | ID 数值类型、缺字段、作者错配、时间无效、URL 漂移、未知字段等隔离，不落盘原始诊断文本 |
| T08 | 通过 | 同一帖子跨三次打开重放仅一条当前态；内容变化生成递增版本 |
| T09 | 通过 | 四处提交边界异常注入 + 子进程 os._exit，无清理退出；首次提交中断也无虚假帖子或游标 |
| 旧版六项基线 | 通过 | 原始代码与六项测试完整保留；仅在临时目录运行 SQLite 基线 |
| Windows 11/NTFS | 待实机验收 | Windows 锁与 WRITE_THROUGH 分支尚未在本环境执行 |

新实现 32 项测试 + 原始回归 6 项，共 **38 项测试通过**。本次实际环境：Linux、Python 3.12.14，无第三方运行依赖。离线命令演示首次 inserted=1，第二次 duplicate=1，recover 后 posts=1/events=1。

提交阶段：AFTER_PENDING / AFTER_SEGMENT / AFTER_MANIFEST 发生中断，恢复只见上一提交；AFTER_POINTER 中断，恢复能完整重放新提交与同批检查点。写入抛出异常后实例进入 poisoned 状态，必须重开判断结果，防止基于不确定状态继续写入。

安全检查：数据根目录路径限制、符号链接拒绝、跨进程锁、Schema 版本、重复 JSON 键、输入类型、来源和策略绑定、METADATA/TEXT 范围、未核验覆盖状态拒绝。损坏的已提交段/Manifest/指针阻断恢复，不默默回退。

设计取舍：统一 Segment 承载帖子、隔离、检查点与 Tombstone，避免跨多个逻辑 JSONL 文件的非原子双写；schema 文件和运行验证器使用同一契约，build_contracts.py 可确定性重新生成。最小验证器只实现当前文件使用的 JSON Schema vocabulary，不冒充通用 JSON Schema 引擎。

明确未完成：真实 Actor、费用账本、Dataset/分页、调度、GUI/Tray、媒体、物理删除/Compaction、保留期执行、快照与备份、ACL 和诊断包、实机 Windows 验收。M1 Tombstone 的 verification_status 为 PENDING_PHYSICAL_CLEANUP；未将逻辑删除冒充完整删除。金额以十进制字符串契约表示，避免浮点费用核算。

下一阶段：M2 先使用 Fake Actor 完成 Runner、RUN_INTENT/预算预留、远端结果 UNKNOWN、Dataset 分页和 Adapter；确认当前 Actor build/input/output/定价后才接入真实测试。当前 Actor 功能矩阵对所有未验证外部能力标记 UNKNOWN，未用旧文档推断其当前状态。

额度说明：本环境没有个人 ChatGPT 剩余额度查询接口，不能监控或保证在 5% 阈值前自动停止。本次限制在 M0–M1，已形成可独立运行与接续的源码包。
