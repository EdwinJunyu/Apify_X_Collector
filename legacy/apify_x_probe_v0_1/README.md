# ACG 情报 App：Apify X 推文采集能力验证器 v0.1

## 作用与边界

本程序用于**验证采集能力，不作为正式生产抓取器**：5 个已知画师账号、近 30 日、每账号最多 30 条，提取帖子 ID、日期、账号、类型、链接、语言及媒体数量，排除普通转发，区分回复与引用帖。结果仅写入本地 `output/` 的 `review_queue.csv`、`report.json`、`staging.sqlite3`（待人工审核），不修改 Google Sheets、正式数据库或 App 新闻 Feed。默认不保存帖文全文、不下载图片。

选用的第三方 Actor：`scrapesage/twitter-scraper`（非 X 官方服务）。依照公开文档输入：`handles`、`includeProfile`、`includeTweets`、`includeReplies`、`maxTweetsPerProfile`、`onlyTweetsNewerThan`、`monitorMode`。使用 Apify REST API 启动、轮询并读取 Dataset。**Actor 可能只能列出约 20–50 条近期帖子，不保证全部 30 日记录**；无数据不等于没有发帖。Actor 不需要 X API，但需要 **Apify API token**；平台访问能力不等于数据存储、商用及公开转载权限。

## 环境

Python 3.10+，仅用标准库，无额外 pip 安装。Windows PowerShell 下（命令均在源码目录运行）：

1. 安全保存 Apify token：`$env:APIFY_TOKEN="<YOUR_APIFY_TOKEN>"`（仅为当前 PowerShell 会话设置；不要提交 `.env`、日志或 token）
2. **离线预览（默认）**：`python apify_x_probe.py` 或 `python apify_x_probe.py --handles yoneyamai ui_shig`
3. **真实测试（可能产生收费）**：`python apify_x_probe.py --execute`
4. **只测一个账号、最多 10 帖**：`python apify_x_probe.py --execute --handles yoneyamai --max-per-account 10 --max-items 15 --max-charge-usd 0.50`
5. **需要在本机持久化全文时才启用**：`python apify_x_probe.py --execute --save-text`。仍然只用于内部审核，先核实适用权利。
6. 测试：`python -m unittest discover -s tests -v`

macOS/Linux 将步骤 1 换为 `export APIFY_TOKEN='...'`，其余相同。首次连接可先在 [Apify Console](https://console.apify.com/) 检查 Actor 使用权限、定价和额度。

**安全成本上限**：默认 `--max-charge-usd 0.50`、`--max-items 160`、`--timeout 180`，同时限制运行最多 5 个账号（高于需明确开关 `--allow-more-accounts`）。平台支持 `maxTotalChargeUsd` 与 `maxItems`，但 Actor 收费项目、套餐价格、事件费用须以真实运行记录核实；收费封顶不是对任何外部账单和额外订阅费用的保证。请求超时后不要盲目重试，以免产生重复计费。

## 结果与验证规则

- `request_preview.json`：保存请求参数与费用限制，**不会包含 token**。
- `run_id.txt` / `run_metadata.json`：用于查询官方 run 状态、`usageTotalUsd`（如果返回）及 Dataset ID。
- `review_queue.csv`：本次符合初筛条件的去重帖子，`review_status=pending`。
- `staging.sqlite3`：跨多次执行按 `Post ID` 去重的独立待审库，**不等于正式 Entities / News 入库**。
- `report.json`：成功帖数、各账号数量、被排除原因、未知 30 日完整性、实际账单字段。
- `--save-raw` 可另存 `raw_actor_items.json`，默认关闭。不要把 output 提交 Git、上传 Google Drive 或转给第三方；按合法用途定期清理。

建议的人工验收：

1. 逐一对比 5 个账号主页与采集链接；统计有效账号数和失败原因。
2. 核对原帖时间、是否真为作者发布、转发/引用/回复分类，检查是否漏掉作品和重要公告。
3. 观察是否覆盖过去 30 天；若只取到最近 20 条，明确判为 `coverage unknown`，不认定完整覆盖。
4. 查看 Apify Console 的 run 与计费，用「可核验有效情报数 / 实际成本」衡量，而非总推文数。
5. 未完成来源授权、缓存保留、删除同步及版权/个人数据评估前，不允许自动进入公开 Feed 或大规模预填充。

## 下一步（尚未实现）

- 并排对比其他 Actor（需为其单独实现字段适配与输入 Schema），记录漏帖率、跑通率、速度和费用。
- 在准许的来源下，实现 Actor 增量同步/游标与删除状态检查；不能把 `monitorMode` 当作已经可靠等价于 X 官方 `since_id`。
- 待审核数据转换成现有 `Entities / Sources / Records / Events` 的导入映射和多来源证据链。

## 官方参考

- Apify API: https://docs.apify.com/api/v2
- Apify run/get Dataset: https://docs.apify.com/api/v2/getting-started
- 选用 Actor 的输入输出: https://apify.com/scrapesage/twitter-scraper
- 另一候选 Actor（注意低量/免费账号限制）: https://apify.com/apidojo/tweet-scraper

注意：没有提供 Apify API Token，也未产生真实抓取或运行成本；本地测试仅覆盖逻辑层。项目用途中的第三方 X 数据长期共享/AI 处理/公开展示权限尚待确认。
