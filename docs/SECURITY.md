# M0–M1 安全边界

输入边界：不可信标准帖子、账号、策略、配置、已有 Manifest/Segment；本次仅使用自产合成数据。保护对象：事件完整性、身份、游标、未授权全文和未来凭据。

- 所有输入按 Schema 1.0 验证；Post ID、作者稳定 ID 必须为字符串。固定来源 URL 必须匹配 handle 与 Post ID；handle 可参考已核验历史，但不会改变稳定账号 ID。
- JSON 重复键、NaN、未来 Schema、未知字段、类型漂移、缺失 ID/时间/身份一律拒绝或隔离。Quarantine 只含原因码和序号，不复制原始文本、Token 或异常消息。
- 文本保留必须额外具备 TEXT scope；只允许 METADATA 的策略不能落盘全文。M1 raw_ref 固定为 null，原始响应保留待 M2/M4。
- 互斥锁由 OS 内核持有；PID 文件只做诊断。锁不通过删除文件“解锁”。跨线程写入与 fork 后继承写入均被拒绝。
- 所有数据路径由程序生成，不使用 Post 文本作为文件名。限制数据根目录、拒绝符号链接和越界路径。Manifest 不携带任意外部文件路径。
- SHA-256 检测损坏，不是签名或加密；本机同用户可写根目录的攻击者不在此完整性保证内。数据根目录须为普通用户私有本地目录，不用网络盘/同步盘。NTFS ACL 和凭据管理在 M7 实现。
- 不读取 .env，不执行真实网络请求；legacy/ 为原样旧原型与测试基线，包含历史网络 CLI，不作为本版本入口，不运行其 --execute。
- 使用 Python 标准库，第三方运行依赖为零；无依赖安装脚本。M0–M1 不引入 SQLite 主库；旧版六项测试在临时目录使用 SQLite 仅为验证历史逻辑。
- 单批最多 1000 条、单 Segment 16 MiB。总容量/磁盘空闲预算配置已固定，但完整配额执行点在 M4，费用限额执行在 M2。

损坏已提交段、Manifest 或指针会阻断打开，不自动回退指针或推进游标。写入出现异常后实例必须关闭并重开，由恢复结果确定是否已提交；不要根据异常猜测提交必然失败。

Windows 使用 msvcrt 字节范围锁、文件 fsync 与 MoveFileExW WRITE_THROUGH；POSIX 使用 flock、文件及目录 fsync。当前验收环境为 Linux，Windows 11/NTFS 的实际锁、权限和断电测试尚待实机执行。进程崩溃恢复不等同于已验证任意硬件断电场景。

## M2–M3 更新

新增可显式启用的固定 Apify REST 主机 Transport；演示均使用 FakeTransport。API token 仅进入 Bearer 请求头，不存入 JOB_STATE/日志；不自动读取 .env。Live 额外校验来源权限、Actor/build 与 PPE 费用上限支持的证据，默认 build=unverified 不允许实跑。POST 不自动重试；遇未知结果保留预算，人工核对后绑定 Run。账单 observed 与 finalized 分离；核账证据 URI 写入事件。真实 live 定价与数据映射尚未验证。

## M4 更新

MediaHTTP 使用固定已校验公网 IP 的 HTTPSConnection，服务器名称校验证书，每跳 allowlist + DNS 重验。下载器无 Apify Token。媒体状态事件受 Schema 验证，权限三维与分级在网络前和提交前重验，预览也重验。删除路径只在私有根目录受控子目录操作，派生文件带 hash 关联校验；独立删除屏障不参与旧快照指针回滚。详细恢复约束及未完成范围见新版验收报告。

## M5 更新

原生界面通过同用户 Local Socket/受保护文件 mailbox 和随机 capability token 调用 Worker，不直接持有 JSONL writer。Token 与 IPC 信箱处于独立私有目录；Windows 目录按当前用户 SID 设置 ACL，普通业务不需管理员权限。请求/响应有界，凭据请求处理后删除，旧响应两分钟清理。仅固定命令可执行，不接受 shell、脚本或任意写路径。收费意图 command_id 随 job 持久化，Worker 重启后仍防重复 POST。文本纯文本显示；审查/预览与来源权限由 Worker 重验。依赖审计和 Windows 实机验证边界见最新版报告。

## M6–M8 更新

外盘vault含受控所有权、删除账本、当前许可证据和原子快照索引；restore-in-progress在敏感内容复制前即封锁新目录，清理/校验后解除。当前权限优先于旧快照，恢复不会复制Token或启用计划。版本安装只切代码指针，schema reader floor阻断不兼容回滚。Windows盘识别用只读FILE_ANY_ACCESS IOCTL并拒绝未知/虚拟映射，不自动提权。Task Scheduler仅当前用户低权限和应用hash任务名；默认关闭，合并补跑，未知收费Run不重建。发布依赖有官方wheel hash锁；manifest不提供发行方认证。
