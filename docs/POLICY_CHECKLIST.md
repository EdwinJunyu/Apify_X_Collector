# Policy 清单（M0）

| operation | 所需证据 | 默认 |
|---|---|---|
| access | 原站与 Actor 自动访问用途、访问条件、审核人、有效期 | UNKNOWN → DENY |
| retain | 保存范围、METADATA/TEXT/RAW 分别授权、保留期、删除规则 | UNKNOWN → DENY |
| download_media | 复制图片/视频许可、内容分级与来源限制 | UNKNOWN → DENY |
| redistribute | 对外展示/再利用范围、地域、用途 | UNKNOWN → DENY |

判定键为 policy_id + source_id + operation；version、purpose、evidence_uri、reviewer、reviewed_at、valid_until 必须匹配且有效。
同一策略键按版本递增输入，最新版本覆盖旧版本；过期或撤销不可回退到旧授权。账号绑定的 source_policy_id 必须与调用上下文一致。

fixtures/offline.json 的 mock:// 证据仅代表项目自产的合成测试资料；不能当作任何真实 X 账号的授权。M0–M1 没有网络入口。当前只有 retain 门禁接入实际写入路径，另三类权限的执行点属于后续 Milestone。

账号与帖子 content_rating 分别记录；不从账号评级推断所有帖子评级。M1 不生成缩略图、不下载媒体；素材队列与年龄/地区审查属于 M4。

物理删除、到期清理、旧段 Compaction、导出/缓存/快照与备份清理属于 M4/M6。M1 Tombstone 只从当前态移除并阻断再次导入，verification_status 始终 PENDING_PHYSICAL_CLEANUP，不伪报完成。
