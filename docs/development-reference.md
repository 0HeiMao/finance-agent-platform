# 智能财务分析平台

新增：前端默认使用四工具财务 Agent，支持动态工具选择、调用上限与持久化执行步骤。实现边界、真实任务评测和演示方法见 [Agent实现与验收](../docs/agent-tool-workflow.md)。

AI 应用开发面试说明见 [项目介绍与能力边界](../docs/ai-interview.md)，固定问答评测见 [30项评测清单](../docs/ai-evaluation.md)。离线回归入口：`.venv/Scripts/python.exe scripts/test_ai_reliability.py`。

知识库写入仅限管理员，上传时必须明确所属部门或选择公共发布；部门管理员的管理查询及删除也受部门范围限制。真实模型合成数据基线及发现的问题见 [评测结果](../docs/ai-evaluation-results.md)。

基于 Python、确定性财务计算、大模型与向量检索的智能财务分析 MVP。项目目标是打通“原始数据 → 清洗加工 → 指标分析 → 可视化报告 → 自然语言问答”的最小闭环。

## 目录结构

```text
智能财务分析平台/
├── backend/              # FastAPI 应用：按域拆成 api_*.py（看板在 api_dashboard.py，不再内联在 main.py）
│   ├── periods.py        # 月/季/年口径的纯函数（构成月份、规范写法、同粒度上一期）
│   ├── rollups.py        # 季度/年度合计的落库与读取（financial_rollups 表）
│   └── transactions.py   # 明细单行更正/冲销（不删原行；改完重算受影响期间）
├── frontend/             # Vue 3 + Vite 界面；纯函数与 .vue 组件都在 tests/ 下用 `npm test` 零依赖测（无 jsdom）
│   ├── Dockerfile        # 前端镜像：node 里构建 → nginx 托管静态包并反代 /api
│   └── nginx.conf        # SPA 回退、/api 反代、SSE 关缓冲
├── Dockerfile            # 后端镜像（python:3.11-slim + 单独的 CPU 版 torch）
├── docker-compose.yml    # 一条命令起 postgres + backend + frontend（项目名显式写死，见下）
├── tests/                # pytest 用例（真 PostgreSQL + 真 ASGI 接口）
├── data/
│   ├── raw/              # 原始销售、费用、银行流水等 CSV/Excel，只读保存
│   ├── uploads/          # 知识库上传件的落盘目录（财务导入的临时件处理完即删，不入版本库）
│   └── processed/        # 清洗、标准化后的数据，供分析脚本使用
├── scripts/              # 数据处理、分析、RAG 和报告生成脚本（含 check_all.py 一键自检）
├── output/
│   ├── reports/          # 导出的报告（docx/pptx/md），随时可重新生成，不提交
│   └── charts/           # check_pages.py 的验收截图，每次覆盖，不提交
├── .githooks/            # 版本化的提交前钩子（跑 check_all.py --quick）
├── .gitattributes        # 一律 LF：CRLF 会让 shell 钩子的 shebang 变成 `/bin/sh\r`
├── .venv/                # 本地 Python 虚拟环境（不提交到版本库）
└── README.md
```

## MVP 实施顺序

1. 将脱敏后的销售、费用、银行流水文件放入 `data/raw/`。
2. 在 `scripts/` 中编写 ETL 脚本，将标准化结果写入 `data/processed/`。
3. 使用确定性 Python 逻辑计算收入、费用、利润、回款和差异等指标。
4. 将结构化指标和必要的财务文档建立索引，用于自然语言检索和问答。
5. 生成 `output/reports/` 下的 HTML 仪表盘，并保留 `output/charts/` 下的图表文件。

## 数据与安全约束

- `data/raw/` 中的原始文件只读，任何脚本不得覆盖或删除。
- 所有金额必须由程序确定性计算，大模型只负责解释、检索和生成文字，不自行编造数字。
- 每个核心指标应记录来源文件、字段、筛选条件和计算时间。
- 开发时使用脱敏数据；真实财务数据应进行访问控制、日志记录和密钥隔离。
- 生成的报告和日志不得提交客户身份证号、银行卡号等敏感信息。

## 后续建议

先从“月度经营复盘”切入，优先支持销售、费用和银行流水三类数据，再逐步增加预算分析、应收应付对账和异常检测。

## 数据抓取与验证脚本

爬取到的原始财务文件放在 `data/raw/`，适配成导入模板的结果放在 `data/processed/`。
三个脚本构成一条可重复执行的管道，每一步都会自校验，且都不覆盖既有原始文件。

```powershell
# 1) 抓取：官方源优先，无网络时自动回退到本地已抓取副本
#    --dry-run 只抓取并校验，不落盘；目标已存在时按只读约定跳过（退出码 1）
.\.venv\Scripts\python.exe scripts\fetch_financial_source.py --dry-run

# 2) 适配：源文件 -> 平台导入模板（中文列名），并校验金额对平
#    --limit 0 表示全量；默认 500 行
.\.venv\Scripts\python.exe scripts\adapter_rgat_to_import.py --limit 0

# 3) 验证：真实 PostgreSQL + 真实 ASGI 接口跑完整闭环，结束整体回滚
.\.venv\Scripts\python.exe scripts\verify_crawled_import.py
```

`fetch_financial_source.py` 退出码：`0` 成功落盘 / `1` 目标已存在（正常跳过）/ `2` 输入缺失
/ `3` 在线失败且无回退 / `4` 完整性校验失败 / `5` 其他 IO 失败。

`verify_crawled_import.py` 的断言全部对着**源文件重算**，而不是对着接口返回值自证，
覆盖 认证 → 预览 → 导入 → 落库逐项对平 → 指标 → 预警规则 → 看板 KPI → 重复导入 409 → RAG 检索 → 回滚隔离。
明细与已知数据限制见 `data/processed/README.md`。

同一份文件若已被导入过，接口会按 sha256 判重返回 409（这是正确行为）。要在库里已有该批次时
仍然跑完整导入链路，加上 `--sentinel`：它给「摘要」列写入本次唯一标记后另存临时 CSV 再导入，
批次哈希因此变化，而金额列一个字节都不动，逐项对平仍然成立；临时文件在退出时删除。

```powershell
.\.venv\Scripts\python.exe scripts\verify_crawled_import.py --sentinel (New-Guid)
```

## 回归与演示脚本

`scripts/` 下二十四个验证脚本都是**真 PostgreSQL + 真 ASGI 接口**，跑完自行回滚或清理
（第二十五个 `verify_ci_workflow.py` 不碰数据库，它核对的是 CI 配置本身，见「持续集成」一节；
另外两个 `check_coverage.py` / `check_frontend_coverage.py` 是覆盖率门槛，见「覆盖率门槛」一节；
`check_openapi_snapshot.py` 也不碰数据库，它比的是「代码里的接口」与仓库里冻结的那一份，见「接口契约快照」一节；
`check_visual_baseline.py` 只在本地跑，它比的是首屏像素与仓库里的基线截图，见「截图基线视觉回归」一节；
lint 不进这里 —— 它由 `check_all.py --quick` 里的 ruff 与前端 `npm run lint`/`npm run format:check` 承担，
见「代码风格与 lint」一节）：

| 脚本 | 覆盖范围 |
| --- | --- |
| `verify_import_flow.py` | 注册/登录、模板、CSV+XLSX 预览与导入、Decimal 四舍五入、重复导入 409、非法行拒绝、预警、看板 |
| `verify_embedding_model.py` | 本地 Embedding 模型可加载、维度 384、两条中文检索样例 |
| `verify_crawled_import.py` | 爬取文件全链路 122 项断言（见上），含看板新字段（环比 reason、KPI 标签集合、图表口径说明）与报告导出（标题/小节/金额格式/401） |
| `verify_financial_agent.py` | Agent 证据层（财务数据 + 知识库 + 重排）、`/rag/*` 鉴权、上传去重、上传落盘目录、真实 SSE 端到端 28 项断言 |
| `verify_alert_rule_tiers.py` | 预警规则分档 30 项：建期间档/部门+期间档、409 同范围、422 两维都空与非法指标/期间、400 删默认规则、真库重算里三条口径各得三种结果（期间档盖默认、部门+期间档盖期间档）、对照期间不受影响、删掉后回落默认，跑完复原期间并清掉临时规则 |
| `verify_rollups.py` | 季度/年度落库 24 项：重算季/年落库、季度 `source=stored` 且等于三个月月指标之和、比率按合计重算、年度缺月如实点名、受限账号只读本部门落库行、管理端 403、三种 422、重算某月后季度幂等、没落库的期间现算兜底且不写库、审计留痕 |
| `verify_transactions.py` | 明细更正/冲销 45 项：按期间/部门口径/关键字/批次筛选与分页、受限账号 403（别的部门与全公司行都不许动）、更正后重算并让指标跟着变、改日期同时重算新旧期间、冲销新增负数行且原行保留（同期间合计为 0）、重复冲销与更正已冲销行 409、非法金额/带时区日期/未知字段 422、审计留 `transaction.update`/`transaction.reverse`，跑完撤销整批探针数据并删临时账号 |
| `verify_slow_paths.py` | 慢接口告警 16 项：默认阈值 1000ms 且按路径可覆盖、启动日志写明阈值、真发一次 `POST /ai/rag/chat`（检索 + 大模型，实测 16 秒）后 `slow_paths` 里出现该路径且超阈值次数 +1、`worst_ms`/`last_ms`/`last_at` 如实、**后端日志里真有一条 `WARNING 慢接口告警 path=… duration_ms=… threshold_ms=…`**、紧接着再发一次只记账不重复刷日志（`suppressed` = 1）、快请求 `/health` 不被记账、`/admin/overview` 给管理页的同一份观测里也有这条 |
| `verify_indexes.py` | 明细读路径索引 23 项：`missing_indexes()` 在真库上为空、三条索引的 `indexdef` 与列顺序（`(department, transaction_date)`，顺序反了第二列就用不上）、按期间拉明细结果与旧口径一致（93 行）、管理概览不断、`EXPLAIN (ANALYZE)` 里「部门 + 期间」真的走 `ix_financial_transactions_department_date`、冲销来源/预警状态各走自己的索引、**旧的 `to_char(...)` 形状用不上复合索引**（`Filter` 里逐行算）、80 万行同构临时表上 `Seq Scan` 36.70 ms → 建完复合索引 0.02 ms（1931×）、同结果集的一对写法（1314 行）区间比较 5.1× 且 `Index Cond` 里带日期、跑完 `DROP TABLE` |
| `verify_recalc_lock.py` | 并发重算互斥（期间锁）18 项：锁 key 稳定/不撞号/在 bigint 内、月会连季与年一起锁且按名字排序、起始干净（无数据无残留锁）、**进程外一条真连接持锁时另一个事务真的被挡住**（`SET LOCAL lock_timeout='400ms'` 必报 lock timeout）、另一年不被挡、同年不同季共享年锁而不同年份零共享、持锁方提交/回滚都自动释放、真机 HTTP：无竞争重算 32 ms → 有人持锁 2031 ms（持锁 2000 ms）、导入与单行更正同样被挡且指标随后等于明细之和、两个并发重算都 200 且不丢更新、跑完 0 条残留 advisory 锁 |
| `verify_session_revocation.py` | 会话版本号与强制下线 23 项：真库 `users.token_version` 是 integer/NOT NULL/默认 0（补列不会把老行算成「已作废」）、令牌里的 `ver` 等于库里那行、**没有 `ver` 声明的老令牌在版本 0 时仍可用**（升级不踢人）、`POST /auth/logout` 返回新版本且旧令牌（含「另一台设备」那份、含老令牌）立刻 401 并带「已在别处退出」、重登即恢复、审计留 `user.logout`（中文标签 + `session_version`，**detail 里没有任何含 token 的键**）、管理员 `POST /admin/users/{id}/logout` 踢掉别人而自己的令牌不受影响、被踢账号重登可用（被踢 ≠ 被停用）、用户列表带出 `token_version`、强制下线审计 actor/target/`from_version`→`to_version`、`/system/status` 的 `me.token_version` 与库一致、踢自己 400、未知账号 404、被管理员收窄成部门账号后强制下线别人 403、**受限账号自己 `PATCH /auth/me` 改范围被 403（⑳ 起账号没有自改入口）**、跑完删干净临时账号且**不登出演示账号**（demo 的版本 0 → 0，脚本可反复跑） |
| `verify_job_retry.py` | 后台任务幂等与重试 31 项，**分两阶段跑**（「重启之后会怎样」没法在一个进程里验，所以第一阶段埋现场后要重启后端，再跑 `--phase 2`）：无键去重复用队列里那条且 `payload.ip` 不参与指纹、`Idempotency-Key` 提交/同键重放拿回同一条（**成功过的任务重放仍回那条**，批次已被删也不是 404）、换个键拿 404、201 字符的键 422、真库部分唯一索引的 `indexdef` 与「直接插第二条同键」抛 `UniqueViolation`、失败任务第 4 次尝试要等 16 秒退避才回队列且 `next_attempt_at` 真在将来、`job.retry` 审计带 `previous_error`、重试未知任务 404 / 重试非失败状态 409、重启日志里的「启动接续」与埋下的 `running` 残骸被接续跑完（`attempts` 1→2、批次那 3 行真被删），跑完清掉探针批次与验证产生的任务行 |
| `verify_job_claim.py` | 后台任务跨进程执行权 23 项（⑱）：任务锁 key 稳定/不撞号/在 bigint 内且与接续锁、期间锁三族互不撞、起始干净（`worker_id` 列已建好、0 条残留 advisory 锁）、**两个真 Python 子进程（`--worker` 自调用，都走生产路径 `run_job(job_id)`）同时抢同一条 `queued` 任务 → 处理函数只跑了 1 遍**（计数文件 1 行，一个 `ran` 一个 `skipped`）、任务收在 `succeeded` 且 `attempts` 只加了 1、`worker_id` 又清空、`result.ran_by` 就是赢的那个进程、running 的任务 `claim` 返回 `None` 且 `attempts` 不变（`run_job(db=…)` 再走一遍也不重复执行）、`claim(不存在的 id)` 返回 `None`、**真有人持执行权锁的那条进 `still_running` 原样不动，只有锁是空的（进程已死）才被重排并清空 `worker_id`/`started_at`**、锁的连接一关（进程真的没了）那条才被重排、**池化连接 `close()` 之后锁还在**（所以执行权只能显式解锁，不能靠关连接——本脚本首跑就是栽在这个池子假象上，4 个 FAIL 全是它）、别人正在接续时本进程 `skipped=True` 一个字都不改、接续锁释放后同一个函数立刻能干活（不是被永久锁死）、真机 HTTP：`GET /admin/jobs` 每行都带 `worker_id`、导入 2097 小批次 → `POST /admin/jobs/batch-revert` 202 → 轮询看到 `running` 那一刻写着执行它的进程、最终 `succeeded` 且 `worker_id` 清空，跑完 0 条残留 advisory 锁且演示明细 8909 行一行没动 |
| `verify_exports.py` | 导出契约 36 项（⑲）：起始基线干净 → 导入 2097 探针批次 → 更正一行（`corrections=1`）与冲销一行（负数行 + `reversal_of_id`，重复冲销 409）→ 明细 CSV **17 列表头** / 5 行 / BOM / `X-Export-Rows=5`，三列标记（`冲销状态` 空/`已冲销`/`冲销行`、`冲销时间` 与 `冲销操作人` 只写在被冲销的原行上、`更正次数`）逐格核对 → 期间 `2097`（年）/`2097-Q4`（季）/`2097-12`（月）都收到 5 行而 `2097-11` 收到 0 行、坏期间 `2097-13`/`2097-Q5`/`2097-4`/`全部期间` 全 422 → `?batch=`/`?query=` 与页面**同源**且多条件是「且」→ XLSX 同表头同行数 + 文件名 → 指标月口径（097 收入 650.00 = 150+300-300+500）、季/年口径读 `financial_rollups`（2097-Q4 全公司 1350.00、`包含月份=2097-12`、`是否完整=否`、文件名 `metrics-quarter-…`）并与 `GET /admin/rollups` 逐值一致 → 演示数据 2026-Q2~Q4 只有 3 行全公司行、2026-Q3 全公司 12191391796.83、年度 2026 一行且「否」+ 7 个月 → 四种粒度/区间错误全 422（粒度不匹配的提示里带 `2026-Q3`）→ 临时账号绑 097 后受限明细只 4 行（无别的部门）、季导出只有「部门 097」、`?department=018` 403 → 预警仍按月（季度 422）、月度 10 列表头 → `/export/scope` 上限 20000 → 审计 `data.export` 逐条字段齐且 `truncated` 全 False → **导出配额 429 真发生**（打满 `Retry-After` 是 1..60 的数字、等过窗口同一个请求又 200，即「拦得住脚本也放得出来」；本脚本自己靠 `Retry-After` 排队重试，`QUOTA_HITS` 记下每次拦截）→ `POST /admin/jobs/batch-revert` 收尾（202 → `succeeded`、`deleted_rows=5`），跑完残留全 0 且五项基线行数与起始完全一致 |
| `verify_admin_identity.py` | 管理员标识与数据范围解耦 43 项（⑳）：真库 `users.is_admin` 列已补出且**迁移按「原先不受部门限制 = 管理员」回填**（演示账号 = 管理员）、`/auth/me` 与 `/system/status` 的 `me` 同时带 `is_admin` 与 `unrestricted`（**两个字段不能互相推**）、注册只回 `{id, username}` 且新账号默认 `is_admin=false` + 部门为空 → 明细一行都看不到（`total=0`）、未分配部门的新账号进不了管理端（403 文案「需要管理员权限：当前账号没有管理员标识」）、四种自助扩权 body（部门填 018 / 把部门清空 / 只给单值字段 / 直接要管理员标识）全部 403、被拒之后 `/auth/me` 三个字段与库里那一行**一个字都没变**、每次都留一条 `user.department_self_denied`（detail 记下想改成什么）→ 管理员用 `PATCH /admin/users/{id}` 分配 018：标识仍是 false、该账号看得到本部门行（行部门全是 018）而管理端仍 403（**数据范围不是权限**）、留 `user.department_change`（未分配 → 018）→ **只发 `{"is_admin": true}` 就能授予管理员，而且部门一个字都没动**（这条盯的正是"顺手把范围清掉"那个缺陷）、被收窄到某个部门的管理员 `is_admin=true` + `unrestricted=false` 照样进得了 `/admin/users` 与 `/admin/overview`，但读到的仍是本部门那本账、`?department=097` 403 → 授予与收回各留一条 `user.admin_change`（普通账号 ⇄ 管理员）、收回后**立刻**进不了管理端（权限每次从库里读，不必重新登录）→ 管理员把别人的部门清空后 `unrestricted` 仍是 false、那个账号什么都看不到（**清空不再等于升权**）→ 管理员改自己被 400 拒（请找另一个管理员）→ `scripts/grant_admin.py <账号>` 与 `--revoke` 两条救援路径都可用、不存在的账号退出码 1 并说明原因 → **临时摘掉其他管理员让演示账号成为唯一管理员 → `--revoke demo_analyst` 被拒（退出码 1 +「最后一个管理员」）→ 再把那几个标识原样补回来**（这条守卫在管理接口上不可达，只能这么验）→ 临时账号清干净、演示账号仍是管理员且 `token_version` 没被动过（脚本可反复跑） |
| `verify_login_lockout.py` | 账号锁定与口令强度 19 项：真库 `users` 三列（`failed_attempts` NOT NULL 默认 0、`locked_until`、`last_failed_at`）、退避序列真机核对（阈值前 0、5 次 60s → 120 → 240 → 480 → 900 封顶）、阈值前每次 401 且库里次数一一对上**不写锁定时间**、第 5 次落库锁定、**锁着时口令正确也 429 且带 `Retry-After`**、这条 429 与限流那条**文案一字不差**且不含用户名/「锁」/「密码」（防用户名枚举）、管理页读到 `failed_attempts`/`locked`/`locked_until`、审计正好阈值条 `user.login_failed`（detail 无口令）+ 两条 `user.login_locked`（一条「连续失败达到阈值」一条「账号已锁定」）、管理员解锁清掉计数且**不动会话版本**、解锁后不改口令立刻能登、解锁审计带 `was_locked_for`、静默期 16 分钟后重新从 0 数（连错 3 次 + 1 次 = 只算 1 次）、三种弱口令注册 400 且**一个账号都没建**（审计 `user.register_rejected` 带中文原因）、拿不存在的用户名猛打得到 `user.login_blocked` 限流 429、跑完删干净临时账号且演示账号的失败次数/锁定时间为 0 |
| `verify_rate_limit_store.py` | 三组配额计数落库 35 项（㉑）：真库 `rate_limit_hits` 三列（`bucket`/`hit_at` 都 NOT NULL）与 `(bucket, hit_at)` 复合索引在库里、启动体检（migrations）不点这张表的名 → **三次尝试 = 表里三行**（记的是逐次尝试的时间戳，不是一个计数）、`snapshot` 读出条数与最早/最晚尝试（还差多久一目了然）、攒够上限就拒并按最早那条滑出窗口给 `retry_after`、**被拒的那次不落库**（拒绝不延长封锁）、`reset` 只清本桶并报删了几行、`purge_expired` 只删比最长窗口还老的行 → 桶名 → advisory lock key 稳定、落在有符号 64 位、与期间锁不撞号 → **两个真 Python 子进程（全新解释器）看的是同一本账**：子进程 A 写下一行父进程立刻数得到、父进程填满后子进程 B 立刻被判拒（`retry_after=299`）、清空后子进程 C 又能过 → **脚本进程 ↔ uvicorn 进程**：脚本直接往库里填满登录桶，后端进程下一次登录立刻 429 且 `Retry-After` 是窗口还剩多少（多 worker 不再各记一份）、清掉行同一个请求立刻回到 401（运维救急）→ 登录被限流**留痕** `user.login_blocked` 而**导出配额的 429 一个字都不写审计**（两类 429 的差别是有意的）→ `/system/status.rate_limits` 的 `store`/`shared_across_processes`/`survives_restart` 与三组配额逐值对得上配置、普通账号也能读、**只给聚合桶数不给分桶键** → 真机 HTTP 导出打满 30 次（30 行入账）→ 第 31 次 429 + `Retry-After`（文案说明是导出配额）→ 被拒的那次没落库、`oldest_seconds_ago` 还在半窗口内（真滑动窗口）、另一账号此刻照样 200（按账号分桶）、`purge` 救急立刻 200、再填满 → **等过 `Retry-After` 同一个请求恢复 200**（没人需要重启服务）→ 临时桶与临时账号清干净、演示账号的失败次数与会话版本一字未动且仍能登录（可反复跑） |
| `verify_backup_restore.py` | 备份与恢复 29 项：真库打一份归档（9 张表 9928 行 + 上传件）→ `verify` 只对着归档自检（每张表一个 `COPY` 成员、sha256 一个个重算、`.env`/`data/models`/`data/index` 都不在里面且排除理由写在 manifest 里）→ **恢复进临时库 `finance_restore_check`**（先用不带 `--yes` 的一次预演确认「预演之后临时库仍然不存在」，再真恢复）→ 逐表 `count(*)` 与活库对平、`vector` 列 5 行全非空且维度 384、序列已推到 `max(id)` 之后、恢复出的表能真查 → 上传件往返（写探针 → 备份 → 删 → 恢复到 `output/_restore_uploads` → 字节级一致，且**没有**写回真实 `data/uploads`）→ 三个负例该红的都红（改一个数据字节 → `verify` 点名 sha256；清单里多一列 → 恢复拒绝并点名这一列；清单 sha256 改坏 → 恢复拒绝）→ 同一个归档可反复恢复 → 跑完删临时库、探针文件与探针归档，**全程不动演示库 `finance`** |
| `verify_security_headers.py` | 安全响应头 + GZip + 请求 id 29 项：正常响应与 404 都带四个静态头与严格 CSP、请求 id 合规则原样回传（128 位边界）而超长/带空格/带分号一律换成 32 位、**响应里那个 id 能在后端日志里 grep 到**（每次跑换新 id，否则上一轮那条会让这项假绿）、`/docs`/`/redoc`/`/openapi.json` 换成文档 CSP、HSTS 只在 `X-Forwarded-Proto: https`（或 `https, http`）时出现、`/openapi.json` 与 CSV 导出**真被压**且解压后与 `Accept-Encoding: identity` 那份 JSON 相等（用 `iter_raw()` 数网线上真实字节，`content` 是解压后的）、同样 8909 行的 xlsx 导出**不压**、SSE 没被压且仍按 chunked 传（带不带 gzip 收到的字节一致）、500 也带请求 id 与安全头（它绕过所有中间件，是处理器自己补的）。静态头期望值**写死在脚本里**、不从 `backend.http_headers` import —— 否则常量被改错时脚本跟着一起错。跑一次消耗 3 次导出配额 |
| `verify_metrics_logs.py` | Prometheus 指标与结构化日志 31 项：`/metrics` 匿名 200 且是 Prometheus 文本（桶单调不减、`le="+Inf"` 恒等于 `_count`、指标名全带 `finance_` 前缀）、**与活库对账**（`finance_background_jobs` 总数 == `/admin/jobs` 的 `total`、`finance_alerts` == `/admin/overview` 的 `counts.alert_rows`、`finance_db_up` = 1）、计数真跟流量走（连点三次 `/health` 正好 +3；两个不同 id 只长一条 `{row_id}` 序列且 body 里没有 `/123` 这种字面量；404 归 `unmatched`）、**`/metrics` 不在 `/openapi.json` 的 paths 里**（`include_in_schema=False`，否则加一个指标契约就飘）、gzip 网线字节 11360 → 1228 且**摘掉 `/metrics` 自己那条自增序列**之后与 `identity` 逐字节相等、`X-Request-ID` 回显且后端文本日志里那一次请求同时带 path/method/status/duration_ms；另起两个短命 uvicorn 验配置面：`LOG_FORMAT=json` + `METRICS_TOKEN` + `SLOW_PATH_MS=1`（无 token 401、带 token 200、每条日志都是合法单行 JSON、慢接口告警也是 JSON 且字段真在 JSON 里）与 `METRICS_ENABLED=false`（`/metrics` 404 而 `/api/v1/health` 仍 200）。两个只在这台机器上才有的坑写进了脚本注释：日志编码要**按 BOM 判**（`\| Tee-Object` 写 UTF-16、脚本自己 `Popen` 写 UTF-8，而任意偶数长度的 UTF-8 用 `utf-16` 解码不报错只出乱码），而 `\| Tee-Object` 会按控制台 120 列把长行**硬折**（`duration_ms=` 可能正好被折点切开），所以比对前要把空白整体去掉 |

| `verify_alerts_outbound.py` | 预警外发通道 41 项：真库建 Webhook 通道（201 且响应与列表都只有 `has_secret`、重名 409、`ftp://` 与带用户名口令的 URL 422、非法 `min_level`/`kind` 422、邮件收件人里的多余空格被规范化）、`POST …/test` 成功与失败**都回 200** 且**不写投递台账**、真导入一个探针期间后 4 条投递全部 `sent`（`attempts=1`、`response_status=200`）且本地 sink 收到的正文与 `sha256=<timestamp>.<body>` 签名 **4/4 对得上**、把 sink 切成一直失败后同一批按 2/4/8 秒退避真的重发（sink 三轮共收到 12 次）而 `attempts` 停在 `ALERT_MAX_ATTEMPTS`=3、同一期间再点补发 `created=0`（内容寻址去重的目的正在于此：重算不会把同一件事再通知一遍）、人工重发一次即 `sent`/`attempts=1`/`status=200`、通道计数 `{pending,sent,failed}` 与 `last_result`/`last_error` 如实、六个动作的审计逐条留痕，跑完撤销探针批次、删临时通道并断言该期 0 条 pending |

| `verify_agent_sessions.py` | Agent 会话与用量 39 项（**每次跑两次真模型调用**：一次完整回答、一次生成中就停止）：显式建会话 201 且显示「未命名会话」、空会话详情带 `status_labels`、没有正在生成的回答时停止 409、匿名 401、`start`→`delta`→`done` 事件齐全且 `start` 带 `session_id`/`message_id`/`question_message_id`/`title`、**落库正文与流式正文一字不差**（实测 1883 字、19 秒）、`usage_source=api` 且 prompt+completion=total 与 done 一致、**第一块 delta 时占位行就已经在库里且 `status=streaming`**、停止请求在流结束前被受理（`stopped=true`）且下一块就收尾成 `done.status=stopped`、被停回答只留已生成的部分并按字数估算记账（`estimated`）、停止之后再停止 409、`days=1` 的 `series` 就是今天这一天（**这条曾抓到「跑着的后端还是旧进程」**）、`api/estimated/free` 三个计数相加等于回答数、`days=0`/`days=400`/`limit=0` 422、`/admin/agent-usage` 的 `by_user`（未受限账号显示「不受限」）/`by_status`、别人的会话读写都 404、部门受限账号看不到管理员会话但能看自己的用量（管理端点 403）、删会话回报 `deleted_messages` 并留 `agent_session.delete` 审计，跑完删临时会话与临时账号 |
| `verify_agent_memory.py` | 多轮上下文 15 项（**真模型连问 8 轮 + 1 次生成中停止**，用临时不受限账号）：第一轮的 start/done 都带会话 id 且历史全 0（0 轮/0 条/0 tokens/未截断）、第一轮真调了模型（正文非空且 token>0）、第二轮历史变成 1 轮 2 条且 **★ 第一轮埋的随机口令原样出现在第二轮回答里**（这条才是"历史真送到上游"的证据，假模型会照抄、真模型不会）、逐轮历史轮数 `[0,1,2,3,4,5,6,6]` 单调递增、**第 7 轮刚攒满 6 对时 `truncated` 仍是 false、第 8 轮再多一对就翻成 true 且轮数停在 6**（`truncated` 的含义是"有更早的没带上"，不是"到上限了"）、每轮 `messages = turns × 2` 且估算 tokens ≤ 1500、8 轮问答全部落库且都是终态 `ok`、生成中停止被受理（200）后**被停的回答照样进下一轮上下文**（半截但写完了）、跑完临时会话与临时账号都清干净 |
| `verify_agent_stop.py` | 停止生成的标志位落库 27 项（㉒，**真模型流式 + 真子进程**）：`agent_messages.stop_requested_at` 列与类型在库里（可空、老行是 NULL）、启动体检不点它的名 → 真机 HTTP：第一块 delta 时停止被受理（200 且 `message_id` 就是那行）→ **库里那一行立刻写着 `stop_requested_at`**、生成器下一块收尾成 `done.status=stopped`、详情里 `stop_requested=true` 且正文只留已生成部分、**再停 409**（终态行不再接受停止）、没有问题的空会话停止 409、匿名 401 / 别人的会话 404 → **跨进程（这才是「多 worker 也停得下来」的证据）**：父进程开着一条流，另起一个**全新 Python 解释器**（`--set-stop <id>`，只带 SQLAlchemy 连库、不经过后端进程）往那一行写下标志，父进程这条流随即以 `stopped` 收尾 —— 全程没打过停止接口，所以停它的确实是**另一个进程**；子进程再跑一次同一个 id 时报「已经不在生成」（`rowcount=0`）→ **重启后读得出区别**：直接插两条挂着的 `streaming` 行（`created_at` 三分钟前），带标志的读成「已停止」、不带标志的读成「已中断」，库里两份都不动 → 同一进程里重复登记拿同一个句柄、`release` 清不掉库里的标志、本地注册表跑完归 0 → 临时会话/临时账号/临时行清干净、演示账号 `failed_attempts`/`locked_until`/`token_version` 一字未动且仍能登录（可反复跑） |

验证脚本会把自己的上传件写到临时子目录并在结束时删除，不会污染项目的 `data/uploads/`。

| `verify_timezone.py` | 业务时区 25 项（**不改任何数据**：只登录、读接口、导一次审计 CSV）：`app.business_timezone` 五字段齐全且 `now` 带 `+08:00`、上报的业务时刻与本机时钟偏差 < 300 秒（证明不是写死的常量）、`today` 等于脚本按 IANA 规则**独立**算出的那一天、业务日与 UTC 日确实不同（跑在 UTC+8 凌晨时业务 `2026-10-09` / UTC `2026-10-08`）且同一瞬间在 UTC/上海/纽约可以落在不同的日历日、`/dashboard` 的 `updated_at` 带偏移且就是刚才那一刻、`/export/scope` 报出 `Asia/Shanghai (UTC+08:00)`、**本次登录在「业务今天」的审计窗口里但不在「业务昨天」里**（那一刻按 UTC 算还停在昨天——这条最硬）、审计行时间戳是带偏移的显式 UTC 且按业务时区算确实落在今天、审计 CSV 用业务日窗口能导出并回报条数、`/ai/agent/usage` 与 `/admin/agent-usage` 的窗口起点是业务日 `00:00:00+08:00` 且最后一天是业务今天、本机 `biztime` 的时区名/文案与真机上报一致、`UTC+8`/`Not/AZone`/空串在两处判定都被拒、取非法时区抛 `TimezoneConfigError`、纽约夏令时那天只有 23 小时。**第一组检查顺带抓「跑着的后端还是旧进程」**：老进程里没有 `business_timezone` 字段，会直接红（⑮ 的同类检查真抓到过一次） |

还有七个不查数据、只查界面的脚本：

| 脚本 | 覆盖范围 |
| --- | --- |
| `check_pages.py` | 18 个页面 × 1440/768/375 三档，注入登录态后逐页断言 `scrollWidth == clientWidth`（无横向溢出）并整页截图到 `output/charts/`。每页先轮询到真的渲染出内容（`#app` 有子节点且 innerText ≥ 200 字，`--ready-timeout` 默认 20s）再判定：**空白页面天然没有横向溢出，固定 sleep 会把它变成假 PASS**——dev server 冷启动要现编译 chunk，第一次访问 `/admin` 就截到过 5.8KB 的全白图 |
| `ui_scope_smoke.py` | 在真浏览器里点「查看口径」下拉与导出按钮：下拉选项、切口径后 KPI 与提示条变化、导出文件名/行数。**只有它能发现「控件在但点了没反应」** —— 写它时当场抓到 `ScopeSwitcher` 的 `onMounted` 竞态 |
| `ui_alert_review_smoke.py` | 在真浏览器里点预警的「确认异常 / 备注 / 退回」：断言状态徽标与顶部「待处理」计数跟着变（按**相对变化**断言，不写死条数）。`ElMessageBox` 的输入框这类只在点击后才存在的 DOM，只有真点一遍才看得到 |
| `ui_alert_rule_tiers_smoke.py` | 在真浏览器里核对分档规则页 12 项：「适用范围」列与 `默认`/`分档` 标签、默认行删除按钮禁用并带原因、两个维度都空时前端拦下并提示、用 DOM 填表新增一条分档规则后表格真的多出一行（跑完删掉临时规则） |
| `ui_transactions_smoke.py` | 在真浏览器里点一遍明细更正页 20 项：筛选到探针期间、点「更正」出现行内编辑区（十个字段）、改收入金额保存后提示带「已重算 2097-03」且单元格变 `777.77`、点「冲销」先要原因再确认、列表从 3 行变 4 行且出现「冲销行」（费用 `-200.00`）与「已冲销」（跑完撤销整批探针数据） |
| `ui_components_smoke.py` | 在真浏览器里点四个自研组件 40 项：口径切换器（默认项、选项来自真接口、切到某部门后**后续请求真的带上 `department=`**、提示条出现与消失）、导出按钮（真下载 93 行 16 KB 且提示用后端 `Content-Disposition` 的名字）、Agent 对话框（空输入不发问、真模型流式回答带「依据：」、`Network.setBlockedURLs` 断流后出现错误与「重试」、点重试**用的是上一条问题**）、管理概览的慢接口告警区块（标题在、区块不是空白、告警行点名路径与次数，并把这张卡片截到 `output/charts/ui_slow_alerts.png`）、会话版本的两处界面（设置页有「登录与令牌」卡片且会话版本是真数字、有「退出登录」按钮；用户管理表头有「会话」列、每行有会话状态与「强制下线」按钮、自己那行禁用——**刻意不点退出登录**，点了会把 demo 账号的会话版本推进一版）、账号锁定的界面面（用户管理表头有「登录」列、每行登录状态是「正常 / 失败 N 次 / 已锁定」、每行有「解锁」按钮且**演示库没人被锁所以全部禁用**；再清掉本地令牌打开 `guestOnly` 的注册页，核对口令规则提示与 placeholder 都写到「至少 10 位」、填 6 位口令点注册被**前端当场拦下不发请求**）。**只有它能发现「单测过了但真页面上不工作」** —— 写它时当场抓到 AgentChat「重试」按钮拿到的是已被清空的输入框 |

| `check_visual_baseline.py` | 把 9 张首屏截图冻结进仓库（`docs/visual-baseline/`），之后每次跑都逐像素比对：`/dashboard`、`/analysis/revenue`、`/analysis/alerts`、`/financial/transactions`、`/agent`、`/admin/users`、`/admin/alert-channels` 各 1440×900，`/dashboard` 与 `/settings` 各 375×812。单通道差 ≤ 8 不算变、不一样像素占比 > 0.2% 判失败、**基线尺寸变了直接判失败**，并把差异图（变了的地方涂红）写到 `output/charts/`。为什么要有它：`check_pages.py` 只查「无横向溢出 + 渲染出内容」，把 KPI 数字改成白色、把图表容器高度写成 0、把表头文字删掉，它全都照样绿 |

七个脚本都要求后端 8000 与前端 dev server 5173 在跑（`check_pages.py` 支持 `--sizes/--pages/--out-dir/--viewport-only`）。

### 一键自检

本目录是 git 仓库（`main` 分支）。推送/PR 时 `.github/workflows/ci.yml` 会跑结构体检（含 ruff lint
与接口契约快照）、后端 pytest 与前端 `npm ci`/`npm run lint`/`npm run format:check`/`npm test`/`npm run build`（两边各自还带
一条覆盖率门槛，见下面「持续集成」一节）；而这台开发机上没有 CI 服务可挂，所以「CI」同时落成一个谁都能
在本机跑的入口：

```powershell
# 编码/语法体检 + 后端 ruff + 接口契约快照 + 后端 pytest + 覆盖率门槛 + 前端 node --test + 覆盖率门槛 + eslint + prettier
.\.venv\Scripts\python.exe scripts\check_all.py
# 只做编码/语法体检 + 后端 ruff + 接口契约快照（几秒钟、不碰数据库，适合改完文件先自查）
.\.venv\Scripts\python.exe scripts\check_all.py --quick
# 再加 18 页 × 3 档的真机页面扫描与首屏截图基线比对（要求前后端已启动）
.\.venv\Scripts\python.exe scripts\check_all.py --with-pages
```

第一步是**源文件编码体检**（查 UTF-8 BOM、非法 UTF-8 字节、替换字符 U+FFFD），因为这里真出过事故：
用 PowerShell 的 `Get-Content` / `Set-Content -Encoding utf8` 往返源文件时，它按系统 ANSI 代码页
（本机 cp936）重写内容，结果文件开头多出 BOM、正文变成 mojibake，python 直接报
`SyntaxError: invalid non-printable character U+FEFF`。**改源文件一律用编辑器/工具链的读写，不要用
PowerShell 文本 cmdlet 往返**；`--quick` 就是给这条纪律准备的哨兵（顺带带上 ruff 与接口契约快照 —— 它们
不碰数据库、几秒钟，够快才可能次次都跑）。

`npm run build` 在本机沙箱会失败（见下），所以自检不含构建步骤，界面验收走 `--with-pages`。

### 版本库与提交前钩子

仓库是用 `scripts/init_git.py` 初始化的（可重复执行）：

```powershell
.\.venv\Scripts\python.exe scripts\init_git.py   # git init -b main + 设 core.hooksPath + 体检打印
```

它一次性把四件事定下来，都是有原因的：

- **`core.hooksPath = .githooks`**：钩子放在 `.git/hooks/` 里不进版本库，换台机器就静默消失；
  放进 `.githooks/` 才会随仓库走。克隆下来只需再跑一次 `git config core.hooksPath .githooks`。
- **钩子默认只跑 `--quick`**（编码 + 语法 + ruff + 接口契约快照，几秒）：拦的正是「源文件被写成
  mojibake/BOM」、语法错误与「接口改了但没更新快照」这类进库就难查的问题。
  `PRECOMMIT_FULL=1 git commit …` 会连后端 301 项、前端 55 项与两侧的 lint/覆盖率门槛
  一起跑。
  钩子本身也验证过：故意 `git add` 一个带 BOM 的 `.py` 后提交，两步体检都 FAIL、提交被拒。
- **`core.autocrlf = false` + `.gitattributes` 的 `* text=auto eol=lf`**：Windows 上默认会把 LF 换成 CRLF，
  而钩子是 shell 脚本，行尾多一个 CR 会让 shebang 变成 `/bin/sh\r`，钩子直接报 bad interpreter。
- **`.env` 必须被忽略**：`init_git.py` 会用 `git check-ignore -q .env` 确认，没被忽略就报错退出；
  密钥文件进库比任何 bug 都严重。

`.gitignore` 里另有一批「测试残渣」条目（`pip-*`、`pytest-of-*`、`torchinductor_*`、`node_modules/`、
`frontend/dist/`、`output/_*`）：pytest 与 pip 会在仓库根目录随手建目录，不挡住就会混进 `git status`。

`verify_crawled_import.py` 有两个使用前提：

- **库里不能有同一份文件的演示批次**（否则期间汇总会被演示数据翻倍，断言按设计失败）。
  先跑 `python scripts/import_via_api.py --remove` 撤下演示数据再验证，验证完再导回来。
- 演示数据已入库时加 `--sentinel <标记>`（**需要值**，不是开关）：它只改「摘要」列加本次唯一标记，
  金额列一个字节不动，从而绕开 sha256 判重、仍能跑完整条导入链路。

`import_via_api.py --remove` 只撤「这份 CSV 对应批次」的行（按 sha256 定位），
同期间还有别的数据时会重算指标而不是清空，避免误删别人的数据。

### 部门口径的升级（一次性）

`analyze` 写两套行（全公司 + 各部门）是后来加的，老库里只有全公司行。升级路径：

```powershell
# 1) 补列、补索引、必要时加宽 numeric（幂等，可反复跑）
.\.venv\Scripts\python.exe scripts\init_pgvector_db.py
# 2) 按期间重建，让部门行从明细里长出来
.\.venv\Scripts\python.exe scripts\migrate_department_scope.py --dry-run
.\.venv\Scripts\python.exe scripts\migrate_department_scope.py
```

第 2 步对每个「有明细的期间」调用同一套 `calculate()` 重算，因此部门行与全公司行必然同源。
只有指标、没有明细的孤儿期间需要 `--force`（脚本会先列出来再让你决定），否则直接 `exit 2` 拒跑。

> `financial_metrics.gross_profit_ratio` / `net_profit_ratio` 原本是 `Numeric(10, 4)`。
> 部门口径下「收入极小、费用极大」会让 `net_profit_ratio` 冲到 10^6 以上
> （实测最大 `-1729784.9801`），`Numeric(10,4)` 会抛
> `NumericValueOutOfRange: numeric field overflow`，让**整次重算（进而整个导入）失败**，
> 因此第一次迁移把它加宽到 `Numeric(20, 4)`。这也是 `init_pgvector_db.py` 现在带
> `COLUMN_TYPE_MIGRATIONS` 的原因（改类型走那个脚本，加列走下面这条路径）。

### 数据库迁移（不引入 Alembic）

结构变更分四类，各有各的落点，**都不需要 `alembic upgrade head`**：

| 变更 | 落点 | 何时生效 |
| --- | --- | --- |
| 加表 | 定义在 `backend/models.py`（定义在别的模块也行，但**必须被 `migrations.py` 导入**）里的模型 | 应用启动时 `create_all()` 建缺失的表 |
| 加列 | `backend/migrations.py` 的 `COLUMNS` 加一行 `(表, 列, DDL)` | 启动时按 `information_schema` 查缺再 `ALTER TABLE`，幂等 |
| 加列**并要回填** | 同一行的 `COLUMNS`，再把回填写进 `backend/migrations.py` 的 `BACKFILLS[(表, 列)] = (为什么, SQL)` | 只在**这次刚 ADD COLUMN** 的那一次执行（`applied` 里会写「… 回填 N 行（为什么）」），幂等：已存在的列不会再跑回填，不会把后来手工改过的值冲掉（⑳ 的 `users.is_admin` 就是这条路径） |
| 加索引 | 模型里声明（`index=True` 或 `__table_args__` 的 `Index(...)`）**并且**在 `backend/migrations.py` 的 `INDEXES` 加一行 `(表, 索引名, 列清单, 为什么)` | 启动时按 `pg_indexes` 查缺再 `CREATE INDEX IF NOT EXISTS`，幂等；`create_all()` 只在建表时建索引，**老库的表不会被补** |
| 改列类型 | `scripts/init_pgvector_db.py` 的 `COLUMN_TYPE_MIGRATIONS` | 手工跑一次该脚本 |
| 装 `vector` 扩展 | `backend/migrations.py` 的 `ensure_pgvector()` | 启动时自动，且排在 `create_all()` **之前** |

为什么不上 Alembic：这个项目的部署形态是「一个进程 + 一个库」，`ensure_schema()` 在启动时自动补齐；
多一套「发版前记得跑 `alembic upgrade head`」的运维动作，反而多一个会忘的环节。代价是**加列与加索引的清单必须手工维护**，
而忘了写 `COLUMNS` 的表现是接口 500（`UndefinedColumn: column financial_alerts.review_status does not exist`），
忘了写 `INDEXES` 则一声不响——查询仍然对，只是退化成顺序扫；项目为此留了三道闸门：

- `missing_columns()` 在启动时对照模型与真库，把缺失的列写进 `ERROR` 日志（不拦启动）；
- `missing_indexes()` 同样在启动时点名缺的索引（`ERROR 数据库索引与模型不一致，缺失：…`）；
- 单元测试 `test_schema_matches_models` 断言 `missing_columns() == []` **与 `missing_indexes() == []`**，漏一行就让回归红。

索引这件事在真库上真抓到过：`financial_transactions` 与 `financial_alerts` 都是「先有表、后给模型加了
`index=True` 的列」，于是补索引之前按状态筛预警、按冲销来源回查原行一直是顺序扫。写进 `INDEXES` 后
`ensure_schema()` 一次补齐三条（`ix_financial_transactions_department_date`、
`ix_financial_transactions_reversal_of_id`、`ix_financial_alerts_review_status`）。

> 计划形状的实测证据在 `scripts/verify_indexes.py` 里（23 项，见下表）：80 万行同构临时表上，
> 无索引 `Seq Scan` 36.70 ms → 建完 `(department, transaction_date)` 复合索引 0.02 ms（**1931×**）。
>
> 用 `period_range()` 的模块名会被同名局部变量遮住——`transactions.py` 里就有
> `recalculate_periods(db, periods)` 这个入参。所以调用方统一 `from backend import periods as period_mod`，
> 再 `period_mod.period_range(...)`（`rollups.py` 早就是这么写的）；写成 `periods.period_range(...)`
> 不是语法错误，而是跑到那里才报 `'set' object has no attribute 'period_range'`。

> `create_all()` 只建缺失的**表**，不会给已存在的表补**列**：这是上面这些坑的总根源。
> 新加一列表看起来跑得通，是因为本地库往往是新库；老库升级时才会炸。

> 反过来的方向同样会骗人：**从零建的库**（CI、新同事、换台机器）暴露的是「表根本没注册进 metadata」
> 与「扩展没装」。这两个坑本来都被本地老库挡住了 —— 本地库是 `scripts/init_pgvector_db.py` 手工建的，
> 它顺手装了 `vector` 扩展、也顺手建了 `document_chunks`。真在空库上跑一遍才发现 `ensure_schema()`
> 只建出 8 张表，且在 `document_chunks` 上直接
> `type "vector" does not exist`（`embedding VECTOR(384) NOT NULL` 要求扩展先在）：
> ① `document_chunks` 定义在 `backend/rag/pgvector_store.py`，`migrations.py` 只导入了 `backend.models`；
> ② `CREATE EXTENSION` 只写在上面那个手工脚本里。现在这两件事都由 `migrations.py` 自己保证
> （导入 `pgvector_store` + `ensure_pgvector()` 先于 `create_all()`），回归闸门是
> `test_fresh_database_can_be_built_from_migrations_alone`（它在子进程里只导入 `backend.migrations`）。
> 本地库的 `create_all()` 与真机启动路径一直是好的，所以这类问题**只有空库才看得见** ——
> 这也是「CI 用一个干净的 postgres 服务」比「在本地库上跑」更值钱的地方。

### 界面核对（三态与响应式）

`npm run build` 在本机沙箱会因 esbuild 清理临时文件被拒而失败（`[vite:esbuild-transpile] remove …: Access is denied.`），
属于环境限制，改用 dev server 转译 + 无头浏览器核对：

```powershell
# 18 个页面 × 1440/768/375 三档：打印 scrollWidth/clientWidth 并列出超宽元素，同时整页截图
.\.venv\Scripts\python.exe scripts\check_pages.py
# 只核对某一页某一档
.\.venv\Scripts\python.exe scripts\check_pages.py --sizes 375x812 --pages /reports/pptx
# 真浏览器点一遍预警处理与口径切换/导出
.\.venv\Scripts\python.exe scripts\ui_alert_review_smoke.py
.\.venv\Scripts\python.exe scripts\ui_scope_smoke.py
```

脚本用真实登录接口拿 token，写一个临时自动登录页（`frontend/_autologin.html`，用完即删）注入
localStorage，再通过 CDP 逐页设置视口。两个踩坑记录写在脚本注释里：临时页必须放 Vite 根目录
（dev server 启动后往 `public/` 加文件不会刷新目录列表，会被 SPA fallback 吞掉）；从不带
`--remote-debugging-port` 的 `--screenshot` 一次性模式换到 CDP，是因为前者不设设备指标，
窗口宽 375 时页面仍按更大的布局视口渲染，截图会被误读成「溢出」。

核对**容器里的构建产物**要加 `--inject-token`：那条临时自动登录页只存在于 Vite 根目录，
不在 `vite build` 的产物里，所以容器里打不开它（直接扫会一路量到登录页的登录卡片上）：

```powershell
# 对着 nginx 容器（8080）核对，token 由 CDP 直接写进同源 localStorage
.\.venv\Scripts\python.exe scripts\check_pages.py --base-url http://127.0.0.1:8080 `
    --api-base http://127.0.0.1:8080 --inject-token
```

已核对结果：18 页 × 3 档共 54 组全部 `scrollWidth == clientWidth`，无横向溢出（`.table-wrap` 内表格
属预期横向滚动，脚本已排除）；截图见 `output/charts/check_*.png`。**容器里的构建产物也跑过同一脚本
（`--base-url http://127.0.0.1:8080 --inject-token`，exit 0）**，所以打包后界面与 dev server 一致。首轮扫描抓到的唯一一处真实溢出是
`/financial/import @375`（`scrollWidth=429`，`.upload-box` 里的「确认导入」按钮被挤出视口），
已在 `frontend/src/style.css` 给 `.upload-box` 加 `flex-wrap:wrap` 并让 `<input type=file>` 可收缩后归零。
管理员页面的表格与列表同样在三档下核对过：宽表用 `.table-wrap` 横滚，知识库、体检项与部门列表用
列表卡片，手机上不会只剩第一列。

### 截图基线视觉回归

`check_pages.py` 查的是**几何与存在性**（无横向溢出、页面真的渲染出内容），`ui_*_smoke.py` 查的是**交互路径**。
这两类都拦不住「没坏但变丑了」：把 KPI 数字改成白色、把图表容器高度写成 0、把表头文字删掉，它们照样全绿。
所以这里退一步，把**首屏的像素**冻结进仓库，以后每次跑都逐像素比对：

```powershell
# 比对（要求后端 8000 与前端 dev server 5173 在跑）
.\.venv\Scripts\python.exe scripts\check_visual_baseline.py
# 有意的界面改动之后重刷基线，并把 docs/visual-baseline/ 与代码一起提交
.\.venv\Scripts\python.exe scripts\check_visual_baseline.py --update-baseline
# 只核对某一页某一档
.\.venv\Scripts\python.exe scripts\check_visual_baseline.py --pages /settings --sizes 375x812
```

基线是 `docs/visual-baseline/` 下 9 张首屏图（`/dashboard`、`/analysis/revenue`、`/analysis/alerts`、
`/financial/transactions`、`/agent`、`/admin/users`、`/admin/alert-channels` 各 1440×900，`/dashboard` 与 `/settings` 各 375×812；
合计约 1 MB，`manifest.json` 记下每张的页面、视口、宽高与 sha256）。**只截首屏**：整页图动辄 1.7 MB × 18 页，
进版本库不值当，`--full-page` 留着手动看。

判定口径（都能单测）：单个通道差值 ≤ `--tolerance`（默认 8）不算变 —— 抗锯齿与字体微调本来就让几个像素差 1~3，
容差太小会把每次运行都变成假红；不一样的像素占比超过 `--threshold`（默认 0.2%）判失败；
**基线尺寸变了直接判失败**（版面高度也是视觉结果：多出一行、图表被压扁都会改高度）；
基线文件本身也被校验（少一张、多一张、宽高或 sha256 对不上都报出来）。差异图写到 `output/charts/`，
**把变了的地方涂红、其余压成灰底**，所以「差 0.4%」不用人自己找。

**页面上会自己变的内容由前端标注 `data-visual-mask`**，截图时注入的样式把它的字变透明（占位还在，
所以版面变了仍然会红）。这是**在源头消掉噪声**，而不是把阈值抬高到噪声之上：抬高阈值会把真正的视觉改动
一起放过。现在只有 `/dashboard` 的「数据更新于 2026/10/8 21:3x:xx」用了它 —— 加它之前实测同一份代码跑两次，
当时那 8 张里有 6 张逐像素完全一致，`/dashboard` 两张差 193 / 196 个像素（0.015% / 0.064%，差异图里红点全落在那句时间戳的秒数上），
加完 `data-visual-mask` 后 8 张**全部 0 像素差异**。⑭ 新增的第 9 张（`/admin/alert-channels`）连同刷新后的
其余 8 张（侧边栏多了一行，所以宽屏那几张都变了）重刷完之后，整套 9 张复跑同样是 0 像素差异。

**重刷是「补拍」不是「清空」**：`--update-baseline` 把这次拍的几张**并进**旧 manifest（只替换「页面 + 视口」
相同的那几条），其余的条目原样留着，所以 `--update-baseline --pages /agent --sizes 1440x900` 只动 `/agent` 那一张。
这条是真踩过的坑：改之前它整份重写 manifest，补拍一页之后 manifest 只剩那一页，下一次比对就把另外 8 张
报成「基线目录里有 manifest 没登记的图」——`merge_manifest()` 现在把这套合并口径单独锁在单测里。

**会随时间变的内容别放进首屏**：`/agent` 工作台的「近 7 天用量」按天明细带日期（今天 10-08，明天就是 10-09），
展开渲染会让这条基线**每天**都红一次——所以它默认收在 `<details>` 里，截图拿到的是收起状态（概览数字仍在，
逐日明细在管理概览的用量卡里另有一份）。页面上一律自己变的数字用 `data-visual-mask`，日期这种「本来就会翻页」
的内容则从首屏基线里挪走。

**它不进 CI**：像素依赖本机字体与抗锯齿，换台机器、换个 Edge 版本就会整片差一点，这类基线只在
「同一台机器上前后的两次运行」之间有比较意义（理由与其它本地门禁一起写在「有意不进 CI 的门禁」一节）。
在这台机器上它是 `check_all.py --with-pages` 的第二步（第一步是页面溢出扫描）：临时改一处样式（`body` 换个底色）
实测 **243241/1296000 = 18.769%** 差异 → 退出码 1 并写出差异图，复原后回到 0 像素，所以这条门禁真的会红。

### 单元测试

```powershell
$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest
```

`tests/` 下三个文件共 **301** 项：`test_units.py`（导入校验/分块/编码/部门过滤/环比与 KPI 计算/**规则分档优先级**/**期间口径与上期换算（季度、年度、跨年）**/**落库合计与缺月判定**/按路径耗时采样/**慢接口告警阈值、按路径覆盖与日志限流**/**期间边界是左闭右开且区间比较不含 `to_char`**/**期间锁（key 稳定不撞号、补全含季与年、加锁顺序与去重且只传月名也会补全）**/**账号锁定退避（5 次→60 秒、翻倍、900 秒封顶、静默期归零、没到阈值不写锁定时间、`last_failed_at` 是 naive 时按 UTC 读）与解锁（清计数、返回原先锁了多久）**/**弱口令规则（9 个弱口令被拒各有中文原因、4 个正常口令放行、l33t 还原后 `Passw0rd!2026` 仍被认出、用户名本身不能当口令）**/**配额计数的判定与桶锁（㉑：`decide()` 是纯函数滑动窗口——边界是「早于窗口」而不是「恰好等于窗口」，凑够上限时 `retry_after` 按最早那条滑出窗口算且至少 1 秒；桶名 → advisory lock key 稳定、落在有符号 64 位内、与期间锁不撞号；配额注册表读配置且对未登记的名字显式报错）**/**停止信号与状态归一（㉒：`effective_status()` 把「用户点的停止」和「进程崩了」分开——带标志的挂起行读成「已停止」、不带的读成「已中断」、老行没有这一列时也读成「已中断」；停止信号按节拍读库——第一次一定读、节拍内不重复读、`set_poll_seconds(0)` 变成每次都读、库里有标志后本地就知道不再查；**读库失败按「没停」处理**（fail-open，另记一条告警）而本地显式 `set()` 仍然生效；`request_stop` 只写得中 `streaming` 那一行、终态行写不进、不存在的 id 写不进、`release` 清不掉库里的标志）**/**覆盖率门槛的解析（node 20 平铺与 node 22+ 树形两种覆盖表解析成同一组路径、pytest 的 TOTAL 行与用例计数——「7 files skipped due to complete coverage」不能被读成用例被跳过）**/**接口契约快照（快照与真实路由双向一致、契约文档点名的接口必须存在、每个接口都有说明、差异文案会点名增删的接口与模型、`{id}` 与 `{transaction_id}` 归一成同一路径）**/**截图基线（容差边界 8 不算变而 9 算变、尺寸不符单独判失败、差异图把变了的像素涂红、基线文件名稳定、manifest 能抓出少一张/多一张/被手工改过与尺寸不符、基线图缺失、重拍时只替换被重拍的那几条而不是清空 manifest）**/**后台任务幂等（退避序列 2/4/8/16 与 60 秒封顶、`attempts`/`max_attempts` 为 NULL 时按 0/3 读、活跃去重指纹忽略 `ip` 且与字段顺序无关）**/**备份与恢复（manifest 逐成员校验：sha256 与字节数对不上、成员被删、多出没记录的文件、逐表行数与清单不一致、字段列表为空、格式版本不对、文件计数不一致，各给一句中文说明；成员名拒绝空名/绝对路径/`..`/盘符，含穿越成员的归档一读就拒并说明「跑到归档外面」；逐表行数对平点名是哪张表；字节数与归档文件名的格式化；上传件清单带 sha256；`prune` 只留最近 N 份）**/**安全响应头（请求 id 的合规边界 128 算、129 不算，中文/换行/分号/斜杠一律重生成；接口与文档页两套 CSP 只在 CSP 与 HSTS 上有差别、HSTS 只认 `X-Forwarded-Proto` 的第一段；文档路由判定不误伤 `/api/v1/docs`；gzip 排除表含 `text/event-stream` 与 Office/PDF，门槛 ≥1024 字节；中间件用的是 `setdefault`——已有的头不覆盖，唯独请求 id 一律覆盖成服务器认定的那个；`Tee-Object` 在 Windows PowerShell 5.1 上写出的 **UTF-16 日志也能读**，同一份字节按 utf-8 解就是找不到 `request_id=`）**/**Prometheus 指标与结构化日志（计数器与仪表渲染出的文本、直方图内部只记「落在哪个桶」而渲染时才累加且 `le="+Inf"` 恒等于 `_count`、标签值转义与标签个数不匹配直接报错、`route` 取路由模板而没匹配上路由记 `unmatched`、读库失败时 `finance_db_up` 置 0 且端点不抛、JSON 日志只留白名单字段（塞进 `extra` 的口令不会出现）且 `configure_logging()` 不摘别人挂的 handler、反复调用幂等、慢接口告警的 `path`/`duration_ms`/`threshold_ms`/`exceeded`/`suppressed` 真进了 JSON）**/**Agent 会话与用量（用量三路记账：网关报量 = `api`、按字数折算 = `estimated`、一次模型都没调 = `free`；全 0 的用量是网关占位不算账单，`total` 缺了用 prompt + completion 补；流式回退只在「还没吐出任何字」时发生且只一次；状态归一：120 秒没动静的 `streaming` 读成「已中断」而库里那份不动；停止注册表能收下「生成器还没起来」时早到的停止请求并释放 `Event`；`session_counts` 只数 `assistant`；`start_exchange` 先写占位行并派生标题；`finish_exchange` 按报量与估算两路记账；删会话连同它的消息；`usage_stats` 与 `daily_series` 的窗口含今天、缺日补 0）**/**业务时区（`Zone`/`is_valid`/`now`/`today` 取值、naive 时间戳按 UTC 读而不是按本机时区、`day_bounds` 左闭右开且夏令时那天只有 23 小时、`month_of` 跟业务日历而不是 UTC、`label`/`describe` 说清用了哪个偏移、`usage_stats` 的窗口起点是业务日 00:00）**）、
**多轮上下文（⑰）**：`history_for` 只收写完的成对问答（`error`/`interrupted`/还在 `streaming` 的、以及没有回答的孤零零提问整对丢掉）、超预算从最老的一整对开始丢（宁可少一轮不给半轮）、任一预算为 0 即关掉多轮、`truncated` 只表示「有更早的没带上」；`_messages` 的顺序是 system → 历史 → 本轮，历史里混进的 `system`/非法 role/空正文被丢掉且本轮的证据紧挨本轮提问；`stream` 透传 history 而位置参数不变（旧调用照旧）**、`test_config.py`（占位密钥与 CORS 校验、启动 strict 语义、**`BUSINESS_TIMEZONE` 默认 `Asia/Shanghai`、`UTC+8`/`Not/AZone`/空串进 `config_problems`、带尾空格的合法名被规范化**、**`AGENT_HISTORY_MAX_TURNS` 默认 6、`AGENT_HISTORY_MAX_TOKENS` 默认 1500、两项为 0 是合法配置（= 关掉多轮）、负数进 `config_problems`**）、`test_api.py`（受保护接口匿名 401、
真实 ASGI 走完导入→重算→预警→看板→报告→RAG 去重→SSE 流式→PPT 大纲与下载→系统状态脱敏→
管理员入口鉴权与批次撤销重算→**部门数据隔离**：受限账号只读到本部门口径、拿别人的预警 id 也钻取不到明细、
管理员看到的一直是全公司行→**多部门账号**：绑定两个部门后看板/汇总/报告都是并集（金额相加、比率按合计
重算）、能收窄到其中一个、切没绑定的部门 403、`/admin/users?department=X` 两个部门都能查到这个账号、
`/system/status` 的 `me.departments` 给的是完整列表（只给单值列会让设置页把多部门账号缩成一个部门）、
`/analysis/departments` 的 `switchable` 对多部门账号是 `true`（换口径的下拉要对它开放）→预警处理闭环（状态继承、误报必填原因、跨口径 403、规则可编辑且对重算生效）→
**预警规则分档**：新增/改/删分档规则（同类型可按部门、按期间各配一条）、两个维度都空 422、
同「类型+部门+期间」重复 409、默认规则不能删只能停用（400）、
真库重算里期间档盖住默认规则而部门+期间档又盖住期间档（三条口径同时给出三种结果）、
对照期间不受影响（越界参数 422）→
**审计日志**：注册/登录/登录失败留痕且不含密码、七类写操作各一条、按动作与操作人过滤翻页、
`_scrub` 脱敏单测、审计行没有改/删入口、按 IP 前缀与日期范围筛选（本地日历日、含首尾）、
导出 CSV 带 BOM 且筛选在导出前生效、导出本身留一条 `audit.export`、清理默认只预览、
真删只动过期行并留 `audit.purge`、`keep_days=0` 422→**分页与筛选**：`limit` 截断时 `total` 仍是筛选后的总行数、
`offset` 真的换页、部门/是否受限与文档类型筛选、`status` 在服务端过滤、越界参数 422）→
**后台任务**：提交后立刻 202 且明细还在（提交 ≠ 执行）、执行成功才删明细并重算、
审计里带任务 id、不存在的批次 404 且不留任务记录、未注册的任务类型标记失败、
双击同一批次不排出第二条任务（`deduplicated: true`）、同一个 `Idempotency-Key` 重放拿回同一条任务
（连已经成功过的也算，且同一个键在批次已被删掉之后仍然回同一条任务而不是 404）、超长键 422、
失败按 2/4/8… 秒退避自动重排（`next_attempt_at` 有值才算在等）、重跑时「一行都没删」判成功并标
`already_reverted`（审计里第二条 `batch.revert` 记 `deleted_rows: 0`）、人工重试只认 failed 且
**追加**一份预算（原失败原因保留、留 `job.retry` 审计）、重启接续把 queued/running 放回队列
（预算用完或类型已注销才判失败）→**多进程抢占（⑱）**：`claim` 对 `queued` 任务成功一次即写
`worker_id`/`attempts`/`started_at`（并清掉 `next_attempt_at`），**第二次 `claim` 返回 `None` 且 `attempts` 不变**、
对 already-running 的任务直接 `None`、对不存在的 id 也是 `None`（不抛异常）；执行权锁是真 DB 会话锁
（第二个连接抢不到、同连接重入两次要放两次、连接关了才放开——用例用 `NullPool` 连接模拟「另一个进程」）；
别人持着执行权锁时 `run_job(job_id)` 走不到处理函数；接续时**持锁的 `running` 行原样进 `still_running`**、
只有锁是空的才被重排并清空 `worker_id`；接续全局锁被别的进程持着时返回 `{"skipped": true}` 且一个字都不改、非管理员 403→
**结构漂移**：模型里的列都在真库里
（漏写 `migrations.COLUMNS` 时点名，而不是等接口 500）、模型里声明的索引也都在真库里且列顺序不变
（漏写 `migrations.INDEXES` 时点名）、明细接口发出的 SQL 里期间过滤是区间比较而不是 `to_char`（抓真实
执行的 SQL 断言）、**从零建的库也建得全**：子进程里只导入 `backend.migrations` 时 `document_chunks`
已在 metadata 里（表定义在 RAG 模块，漏导入就是空库少一张表），且真库的 `vector` 扩展就位
（否则 `VECTOR(384)` 建不出来）→**按路径耗时**：`record_request` 后
`snapshot()["paths"]` 给出每条的 count/p95/max，且 500 与 200 分开计数→**出口配额**：
导出打满返回 429 + `Retry-After`、三个导出接口共用一本账（换路径绕不过）、
问答是另一本账（导出打满仍能问答）、`/rag/search`+`/rag/answer`+`/ai/*` 共用问答配额、
配额表对未登记名字显式报错且 `reset` 后回到配置默认值→**配额计数落库（㉑）**：
不看 HTTP，直接对 `ratelimit.consume/reset/purge/tracked_buckets` 断言「两次放行、第三次拒且
`retry_after ≥ 1`、桶里正好两行」——即计数在库里而不在内存里（用例自带 autouse 清库夹具，
因为这些行走独立会话立即提交，**不随用例事务回滚**）→**明细更正与冲销**：列表按期间/部门口径/关键字/批次
筛选且 `total` 是筛选后总数、受限账号读不到别的部门的行、全公司行只有不受限账号能改、
更正就地改字段并让该期间指标跟着变（`recalculated`）、改日期会同时重算新期间并清掉腾空的旧期间
（`cleared`）、冲销不删原行而是新增一条金额取负的冲销行（两行合计为 0、`import_batch` 沿用原批次，
否则撤销批次只收得回一半）、重复冲销/冲销冲销行/更正已冲销行都是 409、非法金额与带时区的日期 422、
未知字段被 `extra="forbid"` 在业务代码之前拦下、改完留 `transaction.update`/`transaction.reverse` 审计）→
**导出契约（⑲）**：`_grain` 把省略与空串都当 `month`、非法粒度 422 且提示里给三种粒度的样例；
`_grain_period` 要求区间端点与 `grain` 同粒度（`grain=quarter&period_from=2026-04` 422 且点名 `2026-Q3`）；
明细期间的形状收月/季/年而拒半成品（`2097-13`/`2097-Q5`/`2097-1`/`97-01`/全部期间）；冲销三态由
`_reversal_state` 判定且「冲销行压过已冲销」；**截断不再恒为 false**——把 `EXPORT_LIMIT` 压到 1 时真出现
`X-Export-Truncated: true` 且 `/export/scope` 跟着报同一个上限，调回 100 就没有这个头；
**导出与页面共用筛选**：`?batch=` 只出该批次、`?query=` 只出命中的行、两个叠起来是「且」（导出的行数必须
等于页面列表的 `total`，这条是防「导出比页面多几行少几行」的）→
**并发重算互斥（期间锁）**：`/analysis/calculate` 先锁「月 + 季 + 年」再删旧指标（抓真发出的 SQL 断言顺序）、
更正会在**改行之前**同时锁新旧期间、导入在插行之前一次性锁全部月份（多出来的 advisory key 正好是那把
文件哈希去重锁）、`/admin/rollups/rebuild` 锁它要重写的季）→
**会话版本号与强制下线**：令牌里带 `ver` 且退出登录后旧令牌（含另一台设备那份、含没有任何 `ver`
声明的老令牌）立刻 401、重登即恢复、审计 detail 用 `session_version` 且不含任何含 `token` 的键、
管理员能踢掉别人而自己不受影响、踢自己 400、非管理员 403、未知账号 404）→
**账号锁定与口令强度**：连续失败写进库并按指数退避（阈值前不写锁定时间、到点锁定、封顶后再错也不再加长）、
被锁账号**口令填对也是 429** 且与限流那条 429 文案一字不差、锁着时不回显「该账号是否存在」、
静默期过后重新从零数、管理员解锁清掉计数但**不动会话版本**（解锁 ≠ 踢下线）、
弱口令注册 400 且一个账号都不建（`problems` 给出中文原因）、
口令强度规则不挂 Pydantic 模型上（登录与注册共用一个请求体，挂上去会把老用户的登录也一起判死）→
**预警外发通道**：通道增删改查（201 建、重名 409、`ftp://` 与带用户名口令的 URL、非法级别/类型都 422、
响应与列表只回 `has_secret`、邮件收件人被规范化成「逗号+空格」）、
`POST …/{channel_id}/test` 成功与失败**都回 200** 且**不写投递台账**、台账按状态/级别/期间/通道筛选、
**按内容寻址去重**（同一期间再点补发 `created=0` 且不重复排队）、失败退避时人工重发把 `attempts` 归零
之后立刻重排、`/analysis/calculate` 与 CSV 导入都会顺手排好投递单（响应里带 `deliveries`）、
四个端点都要求管理员（匿名 401 / 部门账号 403）→
**Agent 会话落库**：显式建会话 201（直接返回会话体）且标题压掉空白、列表与详情里的显示名（`untitled` / 「未命名会话」）
与 `status_labels`、删除回报 `deleted_messages` 并留 `agent_session.delete` 审计（`target_id` 是会话 id）、
别人的会话读与删都 404→**真实流式的整链路用例走真库 + 真会话**：`start` 事件给出会话与两条消息 id、
正文与落库内容一字不差、`done.tokens` 三项自洽且 `usage_source="api"`、追问带 `?session_id=` 累积到同一条会话
（`answers` 从 1 到 2、tokens 相加）且不覆盖已有标题、未知 `session_id` 404、
生成期间占位行确实是 `streaming` 时停止请求被受理、**那一行上立刻写下了 `stop_requested_at`**（㉒）
并在收尾时变成 `stopped`（正文只留已生成部分）、
**停止请求早于第一块时一块都不吐、`total_tokens` 记 0**（一次模型都没调用，不按字数估算，详见 `test_api.py`）、
没有正在生成的回答时再停止是 409、四个会话/用量端点匿名 401 而部门受限账号看管理端点 403、看自己的用量照常
（`days=0` → 422）→**多轮上下文（⑰）**：第一轮 `history_*` 全 0 且没截断、第二轮
`history_turns=1` / `history_messages=2` / `history_tokens>0` 且模型假件收到的 `history` 正是第一轮那对问答、
第三轮改用估算口径时 `prompt_tokens` 把历史一起算进去（≥30，只算本轮问题会是个位数）→**业务时区**：`/system/status` 的 `app.business_timezone` 五字段齐全且 `today` 等于按 IANA 规则算出的那一天、
看板 `updated_at` 带业务时区偏移、`/export/scope` 回报 `timezone` 文案、
**审计的日期窗口按业务时区切**（真库插三行 UTC 16:00Z / 16:30Z / 15:30Z，前两行落在业务 `2026-10-09` 的窗口里、第三行落在业务 `2026-10-08`——
那一刻按服务器本机 UTC 算还停在昨天，所以这条用例真的能抓到「照旧读本机时区」）。
异步用例用 **anyio** 插件（`pytestmark = pytest.mark.anyio`，用例 id 显示 `[asyncio]`），
不依赖未安装的 pytest-asyncio；数据库不可用时自动 `pytest.skip` 而不是失败。
测试自身在外层事务里跑，结束整体回滚，不会污染库里数据。

PPT 的下载用例会真的用 `python-pptx` 打开返回的字节流，断言存在**原生图表对象**（不是截图），
并断言「口径与限制」页随文档一起下发——否则「毛利率 100.00%」会被读成经营结论。

### 前端单元测试

```powershell
cd frontend; npm test        # 等价于 node --test --import ./tests/support/register.mjs tests/*.test.mjs
cd frontend; npm run lint    # eslint，只看「写错了」（配置见下「代码风格与 lint」）
```

55 项，跑在 Node 自带的测试运行器上（**不引入 vitest / jsdom / @vue/test-utils**，本机 Node v24 直接用）：

- `frontend/tests/format.test.mjs`：`src/utils/format.js` 里的表格/时长/期间格式化，
  以及部门文本的收发（`parseDepartments` 按 `、,，;；` 与空白切分、去重保序、截到 20 个上限；
  `departmentsText` 反向拼成「018、097」）。去重不只是好看：后端按月校验「行数 == 部门数」，
  元组里有重复元素会把完整数据判成缺数据。这些函数原本写在 `AdminBatches.vue` 的 `<script setup>` 里，
  `.vue` 需要编译器才能 import，所以抽成了普通 ES 模块——**能被 node 直接测的纯函数就别留在组件里**。
  时间断言一律用不带时区的本地时间字符串，任何时区的机器结果一致。
- `frontend/tests/markdown.test.mjs`：`src/utils/markdown.js` 的安全与结构。
  最要紧的一条是 `renderMarkdown('<script>alert(1)</script>')` 的输出里**不含 `<script>`**——
  报告页与 Agent 回答都用 `v-html` 渲染它，一旦「先解析后转义」的顺序被改反，模型输出就成了可执行 HTML。
- `frontend/tests/components.test.mjs`（6 项）与 `frontend/tests/interaction.test.mjs`（27 项）：
  `.vue` 组件在**没有 DOM 的 Node 里真挂载、真点击**。做法是两条自造的支撑：
  `tests/support/sfc-loader.mjs` 是个 Node ESM 钩子，用 `vue/compiler-sfc` 的
  `parse` + `compileScript({ inlineTemplate: true })` 现场把 `.vue` 编成客户端组件
  （不能走 Vite 的 `ssrLoadModule`：SFC 被编成 SSR，注入的 `useSSRContext()` 没有上下文，
  挂载时报 `Cannot read properties of undefined (reading 'modules')`），并把 `src/**/*.js` 里的
  `import.meta.env` 换成 `(globalThis.__VITE_ENV__ || {})`；
  `tests/support/render.mjs` 用 Vue 的 `createRenderer` 造了一棵对象树渲染器 + 最小浏览器垫片
  （`localStorage`/`fetch`/`matchMedia`/`URL.createObjectURL`/`document.createElement('a')`，
  下载落到 `downloads` 数组里），`mount()` 返回 `text()/button()/click()/fire()/setSelectValue()/input()`。
  两个垫片细节是必须的：`v-model` 在原生 `input` 上要 `addEventListener` 与 `getRootNode()`
  （并让根节点 `instanceof globalThis.Document`），`v-html` 的内容要单独记进 `el.rawHtml` 才算可见文本。
  还有两条是这套垫片**撑不住**、于是反过来约束了产品代码的：Element Plus 的 `ElMessage` 在假元素上会崩
  （`el.addEventListener is not a function`，`@vueuse/core` 里那句），所以被测页面一律用行内提示条而不是浮层消息；
  原生 `<select v-model>` 也挂不上（`vModelSelect` 挂载时读 `el.options` 并自己绑 `change`），
  所以下拉框一律写成 `:value` + `@change` —— 这是把「测试跑不起来」变成「少一个组件库依赖」的取舍。
  覆盖的是**行为而不是快照**：分页守卫（末页再点不发事件）、口径切换器写进请求参数、
  导出提示取自响应头、侧边栏高亮、Agent 的 SSE 流与错误重试、后台任务失败行点「重试」打的是
  `POST /admin/jobs/{id}/retry`、被后端去重时提示的是「没有重复提交」而不是「已提交」、
  运行中的后台任务要写出「运行进程：…」而队列中的不许写（⑱）、
  预警外发页点「测试」打的是 `POST /admin/alert-channels/{id}/test` 并把失败原因写回「最近结果」、
  「补发通知」用 `{"period": ""}` 触发台账重读、台账筛选带 `status=` 且行内「重发」打到
  `POST /admin/alert-deliveries/{id}/retry`、
  **导出按钮组（⑲）**：指标导出的季度/年度两个按钮靠标签前缀区分、点击后请求里真的带上 `grain=quarter|year`，
  以及**页面自带口径时不再叠加全局口径**（`department=` 在查询串里只出现一次且是页面上那个值，
  全局那个 018 一个字都不出现，`period`/`batch`/`query` 跟着页面上的筛选一起走）、
  **管理员标识（⑳）**：侧边栏「管理员」组按 `is_admin` 显隐（`unrestricted` 为真但 `is_admin` 为假
  时**不**出现「管理概览」，两者不能互相推），被收窄到某个部门的管理员**照样进得了**管理端
  （能不能管与数据范围是两件事），系统设置页那三格（当前账号 / 数据范围 / 管理员标识）只读。
  这套测试当场抓到一个真 bug：`AgentChat.vue` 的「重试」按钮读的是发送后已被清空的输入框，
  等于点了没点——修法是记住 `lastQuestion`。

`npm run build` 在本机沙箱会因 esbuild 清理临时文件被拒而失败
（`[vite:esbuild-transpile] remove …: Access is denied.`），所以界面验收不靠构建产物，
而是 dev server 转译 + 无头浏览器（见上）。
把 `$env:TEMP`/`$env:TMP` 指到工作区内的临时目录后构建就能过（容器里没这个限制，
`frontend/Dockerfile` 里就是普通的 `npm run build`）。

### 覆盖率门槛

```powershell
.\.venv\Scripts\python.exe scripts\check_coverage.py            # 后端：语句 + 分支
.\.venv\Scripts\python.exe scripts\check_frontend_coverage.py   # 前端：固定名单的模块
```

两条门槛都**不**写进 `pytest.ini` / `package.json`，而是各自的脚本：`--cov-fail-under` 一旦进了
`addopts`，`pytest -k rollup` 这种「只跑一小部分用例定位问题」的运行会因为覆盖率天然很低而必然失败 ——
一个必然失败的过滤器等于没有过滤器。覆盖率只有在整套用例跑完时才有意义，所以这两个脚本不接受任何
用例过滤器，CI 与 `check_all.py` 调用的也是它们。

| 范围 | 实测水位 | 门槛 | 口径 |
| --- | --- | --- | --- |
| 后端（`--cov=backend --cov-branch`） | 语句 88.56%、分支 87.79% | **87.0% / 86.0%** | 全量 241 项跑完后的 TOTAL 行 |
| 前端（node `--experimental-test-coverage`） | 平均语句 95.82%、平均分支 88.85% | **95.0% / 86.0%** | 12 个固定模块的平均值 |

两侧都还有一条比百分比更要紧的检查：

- 后端：**跳过数必须是 0**。`tests/conftest.py` 连不上库就 `pytest.skip`，那时覆盖率会掉下来；
  更阴的是给一批用例加上 skip 标记、总覆盖率仍然达标 —— 这条拦住「用跳过冒充通过」。
- 前端：**名单里的模块必须出现在覆盖表里**。node 的覆盖表只列被加载过的文件，删掉一个用例会让
  对应模块直接从表里消失，而 `all files` 的百分比反而可能升高；这正是覆盖率门槛最容易被骗过去的地方。
  名单是脚本里的 `COVERED_MODULES`（12 个模块），加新模块时同步加进去并抬阈值。

阈值只写在一个地方：脚本里的 `STATEMENT_THRESHOLD` / `BRANCH_THRESHOLD`（前端是 `LINE_THRESHOLD`），
`scripts/verify_ci_workflow.py` 会核对这节里的数字与脚本一致 —— 阈值和文档分开维护时，下一个人就会
用「把阈值调低」代替「补用例」。缺口集中在要真模型 / 真 API Key 的路径（`ai_service.py`、`llm.py`、
`rag/answer.py`、`rag/rerank.py`），跑完的覆盖表会直接列出缺口行号（满覆盖的文件不打印）。

前端那两个数字是**真的跨 node 版本对齐过**的：本机 node 24（树形表）与 `node:20` 容器（平铺表）的真实
输出喂给同一份解析，都是 12/12 个模块、平均语句 95.82%、平均分支 88.37% —— 这正是「只算固定名单的
平均值、不看 `all files`」的原因（`all files` 在两个版本里分别是 95.99% 与 97.13%：node 20 的表把
`tests/*.test.mjs` 自己也算了进去，node 24 的树形表则根本不列测试文件）。

### 代码风格与 lint

两个方向各有一套工具，配置**都落在仓库里**（规则集写死，不吃工具版本的默认值）：

| 范围 | 工具与配置文件 | 跑法 |
| --- | --- | --- |
| 后端 | ruff —— 仓库根 `ruff.toml` | `check_all.py --quick` 里的一步（也就进了 CI 与 pre-commit）；单独跑 `.\.venv\Scripts\ruff.exe check .` |
| 前端 | eslint —— `frontend/eslint.config.js`；prettier —— `frontend/.prettierrc.json` | `npm run lint` / `npm run format:check`（`npm run format` 就地改写） |

**分工是刻意的：eslint 只管「写错了」，排版全归 prettier。** ESLint 10 的 `recommended` 里带着一批
排版规则，打开就会和 prettier 互相改来改去 —— 所以 `frontend/eslint.config.js` 只取 `flat/essential`
（真正会出错的规则：未使用变量、未定义引用、Vue 模板里的明显错误）。前端历史上那 6 个
`no-unused-vars` 全是真缺陷或真死代码（未使用的 `formatTime` 导入、算了没人用的页码变量），
不是「风格不合口味」。

`ruff.toml` 的规则集与忽略项也都是显式的（`select = E4/E7/E9/F/I/UP/B/SIM/C4/PIE`）：ruff 各版本的
默认规则集并不相同 —— 0.16 的无配置运行在一份干净代码上就报出 376 项（含 `RUF` 把中文全角标点当
「易混淆字符」的误报），靠默认值只会得到「换台机器红绿不同」。没选的（`RUF` 全族、`E501` 行长）与
ignore 掉的（`E402`、`E701`/`E702`、`B008`、`UP031`）各有理由，都写在配置文件的注释里：

- `E402`：本项目多处需要「先设环境变量 / 先 `sys.path` 再 import」（空库探针要先设 `DATABASE_URL`）。
- `E701` / `E702`：`if not x: return`、`except Exception: pass` 是既有写法。
- `B008`：FastAPI 的 `Depends(...)` / `Query(...)` 写在默认参数里是标准写法（全项目 125 处）。
- `RUF` 全族：`RUF001/002/003` 把中文全角标点当「易混淆字符」批量误报（注释与提示文案全是中文）。
- `E501`：中文文案本来就长，行宽交给评审（`line-length = 110` 只用于工具自身）。
- `UP031`：真机脚本里用 `%s` 往浏览器执行的 JS 片段插字面量（片段本身全是 `() => { ... }` 花括号，
  改 f-string 要到处写双花括号）。

三个配置文件都有守卫：`scripts/verify_ci_workflow.py` 会核对它们存在、CI 里真的跑了 eslint 与
prettier、并且这一节写明了「写错」与「排版」的边界 —— 否则下一个人把排版规则打开，两套工具就会打架。

### 接口契约快照（OpenAPI）

`docs/api-contract.md` 是**手写**的散文契约（先 409 再 400 的顺序、两处 429 的文案必须一字不差、
`session_version` 为什么不能叫 `token_version`），写得细，但**没有任何东西保证它和代码一致** ——
删掉一个接口、把 `period` 从必填改成可选、给响应模型加一个字段，散文不会自己跟着变。
机器可读的那一半由 FastAPI 生成，整份冻结成 `docs/openapi.json`（当前 71 个接口 / 23 个模型 / 130 KB）：

```powershell
.\.venv\Scripts\python.exe scripts\check_openapi_snapshot.py           # 比对（check_all --quick 里已经包含这一步）
.\.venv\Scripts\python.exe scripts\check_openapi_snapshot.py --update  # 接口确实改了：重新生成，然后把快照和代码一起提交
```

它查三件事：快照与**当前代码生成的**接口逐字节一致；`docs/api-contract.md` 接口表里点名的每个
「方法 + 路径」都真实存在；每个接口都带一句说明（`summary` / `description`，即路由函数的 docstring）。
不一致时它打印的是人话差异，而不是让人自己去看 130 KB 的 JSON diff：

```text
+ 新增接口 POST /api/v1/admin/rollups/rebuild
- 删除接口 GET /api/v1/health
~ 模型改动 FinancialTransaction（字段或约束变了）
```

几个刻意的选择：

- **放在 `--quick` 这一层**（`check_all.py --quick` → 也就是 CI 与 `.githooks/pre-commit`）：
  它要导入应用（约 2 秒，**不连数据库**），而「接口改了却没更新快照」正是最该在提交那一刻就红的事，
  等 CI 才发现就变成了「先提交、再补一个 fixup」。
- **快照是确定性文本**：`sort_keys` + `ensure_ascii=False` + 显式 LF 换行（仓库 `.gitattributes`
  把文本统一成 LF）。所以 diff 里只有真实变化，没有序列化抖动；中文说明保持可读。
- **依赖升级也可能让它合法地变化**（fastapi / pydantic / starlette 写进 OpenAPI 的细节）：同样跑
  `--update` 再 review diff —— 而不是把这项检查关掉。
- 守卫在 `scripts/verify_ci_workflow.py`：这项检查要是从 `check_all.py --quick` 里消失了、快照文件
  被删了、或者这一节没了（更新方式没人写下来），工作流自检就会红。

## 持续集成（CI）

`.github/workflows/ci.yml` 只做两件能在干净机器上完全复现的事：

| job | 跑什么 | 覆盖到什么 |
| --- | --- | --- |
| `backend` | 起一个 `pgvector/pgvector:pg16` 服务（与 compose 同一个 tag，带 `pg_isready` 健康检查）→ 装 CPU 版 torch 与 `requirements.txt` → `check_all.py --quick`（含 ruff lint 与接口契约快照）→ `verify_ci_workflow.py` → `check_coverage.py`（内部就是 `python -m pytest` 全量） | 结构体检（编码/语法）+ ruff lint + 接口契约快照 + 后端全量 301 项 + 覆盖率门槛 |
| `frontend` | node 20 + `npm ci` → `npm run lint`（eslint）→ `npm run format:check`（prettier）→ `npm test` → `check_frontend_coverage.py` → `npm run build` | 前端 55 项 + lint/排版 + 覆盖率门槛 + 生产构建（本机沙箱里构建要绕路，这里才是它的主场） |

三处都是「绿得没有意义」的坑，工作流里写成显式的一步：

- **必须设 `DATABASE_URL` 与 `AUTH_SECRET_KEY`**：`tests/conftest.py` 连不上库就 `pytest.skip`，
  少一个环境变量换来的是「**全 skip 的绿色流水线**」——一个问题都没验。密钥不设的话配置层会把它当占位值，
  生成随机串并往工作区写 `.env`。
- **镜像必须是 `pgvector/pgvector:pg16`**：换成普通 postgres，`document_chunks` 会以
  `type "vector" does not exist` 失败。
- **覆盖率门槛脚本内部要用 `python -m pytest`**：`-m` 才会把仓库根放进 `sys.path`，
  直接 `pytest` 会 `ModuleNotFoundError: backend`；阈值与理由见上「覆盖率门槛」一节。
  `verify_ci_workflow.py` 自己也要读 `backend/config.py`，所以它在开头把仓库根插进 `sys.path` ——
  `python scripts/xxx.py` 只把 `scripts/` 放进去，这条在本地（带 `PYTHONPATH` 手跑）看不出来，
  是拿掉 `PYTHONPATH` 复现 CI 命令时才发现的一条真失败。

这套流水线不是「补个仪式」，它一上来就抓到两个只在**从零建的库**上出现的问题（`ensure_schema()` 少建
`document_chunks`、`vector` 扩展没装），以及一个**断言依赖演示数据**的用例
（`test_admin_rollup_rebuild_is_audited_and_admin_only` 假设库里恰好有一行不完整的落库行）。
前两个的修法与回归闸门记录在「数据库迁移（不引入 Alembic）」一节，第三个改成了自己播数据。

### 有意不进 CI 的门禁

浏览器与真机验收留在本地，`PRECOMMIT_FULL=1 git commit …` 是它们的入口：

- `scripts/check_pages.py`（18 页 × 1440/768/375）与 `scripts/ui_components_smoke.py`：要演示数据、
  要真浏览器，后者还会**真的调一次大模型**（要 API Key）。
- `scripts/check_visual_baseline.py`：像素依赖本机字体与抗锯齿，换台机器、换个 Edge 版本整片都不一样，
  基线只在「同一台机器上前后两次运行」之间有比较意义（口径与约定见「截图基线视觉回归」一节）。
- `scripts/verify_*.py`：全是「真 PostgreSQL + 真 ASGI 接口」的验收脚本，要演示批次与本地向量模型
  （470MB，`data/models/` 不进版本库）。

把它们塞进 CI 只会长期红着 —— 一条永远要人手动忽略的红线，和没有这条线差不多。CI 只保证一件事：
**从零建的库上，表结构与测试套件是自洽的。**

### 工作流自检（`scripts/verify_ci_workflow.py`）

CI 配置本身没有测试，而它「绿」不等于它真的在测东西，所以这份 YAML 由
`scripts/verify_ci_workflow.py` 静态核对 51 项，并且**它自己也被工作流调用**（否则这份自检迟早变成
没人跑的死代码）。它卡住的都是具体的事：镜像 tag 与 `docker-compose.yml` 漂了、少设 `DATABASE_URL`
（全 skip）、`continue-on-error` 把失败吞了、环境变量名写错退化成默认值、脚本改名后工作流指向一个不存在的
路径、`npm install` 取代了 `npm ci`、真机脚本混进流水线、README 这一节被删掉、lint 或覆盖率门槛从流水线里
悄悄消失（含 README 里写的阈值/配置文件与脚本对不上），以及**契约快照这一步被人从 `check_all.py --quick`
里摘掉、快照文件被删、README 那节没写怎么 `--update`**、**截图基线图没进版本库或这一步被人从
`--with-pages` 里摘掉、README 那节没写怎么 `--update-baseline`**。它自己也有个坑：`python scripts/xxx.py` 只把
`scripts/` 放进 `sys.path`，所以脚本开头得自己把仓库根插进去 —— 这条在本地（带 `PYTHONPATH` 手跑）看不出来。
它还踩过一个真的解析坑：README 小节里的 PowerShell 代码块带 `# 注释` 行，用 `find("\n#")` 找小节结尾会被
当成标题截断（「截图基线视觉回归」那节一度只读到 187 字符，于是误报「没写怎么重刷基线」），所以取小节统一走
`readme_section()`（按 `##`/`###` 标题切，需要连着子小节读时传 `subsections=True`）。

真机等价地跑一遍整条 `backend` job（不需要 GitHub，用空库代替干净的服务）：

```powershell
docker exec finance-postgres psql -U finance -d postgres -c "CREATE DATABASE finance_ci"
$env:DATABASE_URL='postgresql+psycopg://finance:finance@localhost:5432/finance_ci'
.\.venv\Scripts\python.exe scripts\check_all.py --quick
.\.venv\Scripts\python.exe scripts\verify_ci_workflow.py
.\.venv\Scripts\python.exe scripts\check_coverage.py
Remove-Item Env:\DATABASE_URL      # 记得切回主库，跑完把 finance_ci 删掉
```

`frontend` job 同理（容器里没有本机那条 esbuild 限制）：

```powershell
docker run --rm -v "${PWD}\frontend:/src:ro" -w /build node:20 sh -c "cp -r /src/. /build/ && npm ci && npm run lint && npm run format:check && npm test && npm run build"
.\.venv\Scripts\python.exe scripts\check_frontend_coverage.py   # 覆盖门槛跑同一套用例，只是带 --experimental-test-coverage
```

## 容器化部署（Docker）

```powershell
docker compose up -d --build      # 起 postgres + backend + frontend
# 浏览器打开 http://127.0.0.1:8080（演示账号 demo_analyst / demo_password_2026）
.\.venv\Scripts\python.exe scripts\docker_smoke.py   # 从外面验收这套栈
docker compose down
```

三个服务的分工与几个**必须这么写**的点：

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| `postgres` | 5432 | `pgvector/pgvector:pg16`，卷 `finance_postgres_data`。与本地开发**共用同一个卷**，所以容器里看到的还是本地导入的那批数据 |
| `backend` | 8000 | `uvicorn` 跑 `backend.main:app`；`data/` 以卷挂进 `/app/data`（470MB 的向量模型不进镜像） |
| `frontend` | 8080 | nginx 托管 `vite build` 的静态包，并把 `/api/` 反代到 `backend:8000` |

- **`name: finance-platform` 是必须的**：compose 默认拿目录名当项目名，而本仓库目录是中文
  （`智能财务分析平台`），实测直接报 `project name must not be empty`。目录名改成英文也能绕过，
  但写死更稳。
- **前端只走同源 `/api`**：`frontend/src/api/client.js` 的 `API_BASE` 默认就是相对路径 `/api/v1`，
  所以镜像里**不需要**在构建期烧进后端地址；换机器、换域名都不用重新构建。
- **后端不对外发布端口**（`ports` 是给本机验证用的，生产删掉这一行就只暴露 frontend）：
  nginx 已经在 `X-Forwarded-For` 里传真实来源 IP，uvicorn 用 `--proxy-headers
  --forwarded-allow-ips '*'` 解析它。不这么做的话审计日志与登录限流看到的 IP 会全是 nginx 容器地址
  （实测：反代后审计里记的是 `172.23.0.1`，也就是宿主机的真实来源，而不是 `172.23.0.x` 的容器 IP）。
- **`proxy_buffering off`**：Agent 的流式问答走 SSE，nginx 默认会把响应攒起来再吐，
  界面上就成了「等半天然后一整段蹦出来」。
- **torch 单独装 CPU 版**：PyPI 上 linux x86_64 的默认 torch 是 CUDA 版，会顺带拖
  `nvidia-cudnn`（单个 553MB）等一堆轮子，镜像直接膨胀到 7GB 以上；这个服务只做 CPU 推理，
  所以 `Dockerfile` 先 `pip install --index-url https://download.pytorch.org/whl/cpu torch`
  再装 `requirements.txt`（torch 已满足，pip 不会再换成 CUDA 版）。实测后端镜像 **2.4GB**。
- **基础镜像拉不动时的办法**：本机 Docker Hub 的元数据解析会卡住，而 `docker.m.daocloud.io`
  这类镜像源是通的，所以先 `docker pull docker.m.daocloud.io/library/python:3.11-slim`
  再 `docker tag` 成本地 `python:3.11-slim`，`docker compose build` 就会直接用本地镜像不再联网解析。

`scripts/docker_smoke.py` 是这套栈的验收脚本：`compose config` 是否合法、三个容器是否 healthy、
前端首页是不是 nginx 发的 SPA 外壳、`/api/v1/health` 经反代是否通、同源登录能不能拿到 token、
带 token 拉看板有没有数据、SPA 深链接（`/admin/audit`）会不会 404、容器里挂到的是不是宿主机的
`data/models/finance-embedding`、nginx 配置里有没有关掉缓冲。
它用文件重定向而不是 `subprocess` 管道收 docker 输出——在本机受限沙箱里，python 用管道接子进程时
docker CLI 连不上 Docker Desktop 的命名管道（`npipe://./pipe/dockerDesktopLinuxEngine`），
那种环境下脚本会把容器相关几项标 SKIP、只验 HTTP 部分。

## 备份与恢复

一条命令打一份归档（数据库 + 上传件）：

```powershell
.\.venv\Scripts\python.exe scripts\backup_restore.py backup          # → backups\finance-20261008-213015.tar.gz
.\.venv\Scripts\python.exe scripts\backup_restore.py backup --keep 5 # 只留最近 5 份，其余删掉
```

实测：**9 张表 9928 行 + 上传件 = 169 KB**（演示库）。归档里只有三样东西：

```
manifest.json                 来源库/时间/代码 commit、逐表行数与 sha256、被排除的东西与理由
database/<表名>.copy          每张表一个成员，内容是 PostgreSQL 的 COPY 文本
files/uploads/**              上传件原样
```

**为什么不是 `pg_dump`。** 第一版就是拿 `docker exec finance-postgres pg_dump` 写的，两个理由让它改掉了：
① **本项目的库结构由代码定义**（`backend/models.py` + `backend/migrations.py` 的 `ensure_schema()`），
备份只需要数据 —— 再把 DDL 抄一份进归档，就会造出「归档里的结构与当前代码不一致」这种没人能修的状态；
② 本机受限沙箱里 **python 子进程碰不到 Docker Desktop 的命名管道**（捕获输出、`DEVNULL`、重定向到文件、
`inherit` 四种 stdio 都试过，全是 `permission denied ... npipe:////./pipe/dockerDesktopLinuxEngine`，
而 pwsh 里直接跑 `docker` 完全正常）。数据库端口本来就发布在宿主机，所以脚本改用 `DATABASE_URL` 直连。

数据走**服务端 `COPY (SELECT …) TO STDOUT` / `COPY … FROM STDIN`**（文本格式）：`vector`、`jsonb`、
`timestamptz`、`numeric` 的文本形式由服务端自己生成、也由服务端自己解析回去，脚本里不需要序列化任何一种类型。
实测 `document_chunks.embedding`（pgvector 384 维）往返后仍是 5 行非空、`vector_dims` 仍是 384。

恢复分三步，**默认什么都不写**：

```powershell
.\.venv\Scripts\python.exe scripts\backup_restore.py verify backups\finance-20261008-213015.tar.gz
.\.venv\Scripts\python.exe scripts\backup_restore.py restore backups\finance-20261008-213015.tar.gz --database finance_restore_check --create   # 预演
.\.venv\Scripts\python.exe scripts\backup_restore.py restore backups\finance-20261008-213015.tar.gz --database finance_restore_check --create --yes
.\.venv\Scripts\python.exe scripts\backup_restore.py compare backups\finance-20261008-213015.tar.gz --database finance_restore_check
```

- `verify` 只对着归档自己检查（每个成员的 sha256/字节数、逐表行数与清单是否一致、有没有 manifest
  没记录的多余文件），**不碰数据库**；`restore` 不带 `--yes` 时只打印「恢复几张表、恢复顺序、目标库在不在」。
- 恢复的目标库默认是**归档里记的来源库**，所以要么显式 `--database`，要么就得接受「恢复到生产库」。
- 恢复顺序按外键（`Base.metadata.sorted_tables`），装完还会 `setval` 把序列推到 `max(id)`——
  `COPY` 进来的 id 不推进序列，不补这一步下次插入就撞主键。
- 「结构来自代码、数据来自归档」的直接后果：**归档里有、当前模型里已经没有的列，恢复会点名这一列并拒绝**，
  而不是悄悄丢掉。反过来，代码新加的列恢复后是默认值。
- 归档里**没有** `.env`（含 `AUTH_SECRET_KEY`/`DEEPSEEK_API_KEY`）、`data/models`（约 470 MB，可重下）、
  `data/index`（`scripts/reindex_embeddings.py` 可重建）、`data/raw` 与 `data/processed`（随 git 分发）。
  这些理由写在 manifest 的 `excluded` 里，`verify` 会把它们打出来 —— 免得下一个人以为漏了。
- 归档会被拷来拷去，所以成员名一律过 `safe_member_name`（绝对路径、`..`、盘符、反斜杠直接拒绝），
  解包时还会再确认落点在目标目录内：路径穿越在读归档那一步就炸，不是「解包时小心一点」。

真机验证是 `scripts/verify_backup_restore.py`（29 项）：拿真库打归档 → 自检 → **恢复进临时库
`finance_restore_check`** → 逐表 `count(*)` 与活库对平、抽查 `vector` 列与序列 → 上传件往返
（写探针 → 备份 → 删 → 恢复 → 字节级一致）→ 三个负例（改一个数据字节、清单里多一列、清单 sha256
改坏）该红的都红 → 同一个归档可反复恢复 → 跑完删掉临时库与探针文件，**全程不动演示库 `finance`**。

## 安全响应头与压缩

每一条响应（成功的、404 的、500 的、SSE 的）都带同一套头：

| 头 | 值 / 规则 |
| --- | --- |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `no-referrer` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` |
| `Content-Security-Policy` | 接口与文档页两套口径，见下 |
| `X-Request-ID` | 客户端带的合规 id 原样回传，否则服务器生成 32 位十六进制 |
| `Strict-Transport-Security` | 只在请求确实走 https 时下发 |
| `X-Response-Time-Ms` | 处理耗时（这是既有的，不是这次加的） |

逻辑都在 `backend/http_headers.py`（纯函数，单测直接打），由最外层的 `RequestContextMiddleware`
统一补：`apply_security_headers()` 用 `setdefault` **只补不抢**（下层自己设了 `X-Frame-Options`
就听下层的），唯独 `X-Request-ID` 一律覆盖成服务器认定的那个。中间件注册顺序是
`GZip → 观测 → CORS → RequestContext` —— Starlette 里**后 `add_middleware` 的在更外层**，
所以请求 id 与安全头罩在最外面（404、异常处理器的响应都跑不掉），GZip 贴着应用。

**CSP 为什么要两套。** 接口响应是
`default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` —— JSON 接口
本来就该什么资源都不加载；而 `/docs`、`/redoc`、`/openapi.json` 是 Swagger UI，
它自己要跑 JS、要取 `openapi.json`，给 `default-src 'none'` 会直接白屏，所以文档路由只留
`frame-ancestors 'none'`。

**HSTS 为什么只在 https 下发。** 本机是 `http://127.0.0.1:8000`，无条件下发
`max-age=31536000; includeSubDomains` 会把 `127.0.0.1` 在浏览器里锁一年。判据是
`X-Forwarded-Proto` 的**第一段**（`https` 与 `https, http` 都算，`http` 不算）—— 反代后面
这个头是可信的，直接暴露时它只是客户端自称的，下发了 HSTS 也无害（因为那是 http 连接）。

**请求 id 干嘛用。** 响应里那个 id 会连同方法/路径/状态/耗时写进后端日志
（`request request_id=… path=… method=… status=… duration_ms=…`），用户截图报 500 时一条
`grep request_id=…` 就能对上这一条请求。取值规则是「客户端合规就用（便于网关/前端串联），
不合规就自己生成」：只认 `[A-Za-z0-9._-]{1,128}`，超长、含空格/分号/斜杠/中文一律换成新的
—— 请求头是客户端给的，直接回显进日志就等于让外部往日志里写任意内容。

**最容易漏的一条是 500。** Starlette 把注册在 `Exception` 上的处理器交给最外层的
`ServerErrorMiddleware`，它在所有用户中间件**之外**：500 不经过 `RequestContextMiddleware`，
所以 `backend/main.py` 里的处理器自己调 `apply_security_headers()` 补同一套头与 id，
body 只回 `{"detail": "服务器内部错误", "request_id": …}`，不回栈信息。

**GZip 的取舍。** 门槛 1024 字节、级别 6：更小的响应压了只是多一堆字节，级别 9 在小响应上
纯属浪费 CPU。排除表在 Starlette 默认（`text/event-stream`、`image/*`、`application/zip`、
字体、视频）之外又加了 Office 与 PDF（docx/xlsx/pptx/pdf/ods 本身就是 zip 包，压了不省还费 CPU）。
实测：

- `/openapi.json` 68 513 字节 → 网线 12 647 字节；
- `GET /api/v1/export/transactions?format=csv`（8909 行）1 553 151 → 210 970 字节（省 87%）；
- 同样 8909 行的 **xlsx 导出 469 970 字节不压**；
- SSE 从不压 —— 压了就得攒够一块才吐，等于把流式变成一次性。

真机验证是 `scripts/verify_security_headers.py`（29 项）：正常响应与 404 的头齐全、请求 id 的
回传/超长/非法值替换、**响应里那个 id 能在后端日志里 grep 到**、两套 CSP 的切换、HSTS 只在
`X-Forwarded-Proto: https` 时出现、大 JSON 与 CSV 真被压且解压后与 `Accept-Encoding: identity`
那份字节一致（用 `iter_raw()` 数网线字节，`content` 是解压后的）、xlsx 不压、SSE 没被压且仍是
chunked（带不带 gzip 收到的字节一致）、500 也带 id 与安全头。

## Prometheus 指标与结构化日志

`GET /metrics`（**根路径**，不在 `/api/v1` 下面）输出 Prometheus 文本格式
（`text/plain; version=0.0.4`），八个指标族：

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `finance_http_requests_total` | counter | `method`、`route`、`status` | 请求数 |
| `finance_http_request_duration_seconds` | histogram | `method`、`route` | 耗时（秒），桶 0.005 / 0.01 / 0.025 / 0.05 / 0.1 / 0.25 / 0.5 / 1 / 2.5 / 5 / 10 |
| `finance_http_requests_in_flight` | gauge | — | 正在处理的请求数（渲染 `/metrics` 时含它自己） |
| `finance_app_info` | gauge | `version`、`environment` | 恒为 1，用来在 Grafana 上按版本打标 |
| `finance_process_start_time_seconds` | gauge | — | 进程启动时刻（Unix 秒） |
| `finance_db_up` | gauge | — | 这次采集能不能读到库（1/0） |
| `finance_background_jobs` | gauge | `status` | 后台任务按状态计数 |
| `finance_alerts` | gauge | `review_status` | 预警按处理状态计数 |

**标签一律用路由模板。** `route` 取 `scope["route"].path`，也就是
`/api/v1/financial/transactions/{row_id}` 而不是 `…/transactions/123`；没匹配上路由的 404 记
`unmatched`。这不是洁癖：按真实路径打标签，一个 id 一条时间序列，几百次分页请求就能把
Prometheus 的基数撑爆，而被撑死的是监控不是接口。真机验证里专门有一项断言响应体里不出现字面量 `/123`。

**没引 `prometheus_client`。** 只需要计数器、直方图、仪表三样，格式规则少到能逐行单测
（`tests/test_units.py` 直接断言渲染出来的文本），为这三样多拉一个依赖不划算。`backend/metrics.py`
里的直方图**内部只记「落在哪个桶」**，渲染时才累加成累计值 —— 这个坑真踩过：一开始 `observe()`
把所有「装得下」的桶都 +1，于是观测三次 `le="0.025"` 会显示 4。`le="+Inf"` 永远等于 `_count`，
比所有上界都大的观测值只进 `_count`/`_sum`。

**读库失败不让端点崩。** 采集 `finance_background_jobs`/`finance_alerts` 时任何异常都只把
`finance_db_up` 置 0 并把这两族清空 —— 旧数字比没有数字更危险（运维看到 6 个任务在跑，其实库早就
连不上了）。

**为什么留着 `/api/v1/observability/metrics` 那份 JSON。** 那份是给管理页看的，字段可以自由演进；
`/metrics` 是给 Prometheus 的**契约**，改口径等于改别人的仪表盘，所以两边各留一份、不互相兼任。

开关与令牌（都在 `.env`）：

- `METRICS_ENABLED=false` → `/metrics` 返回 404（端点整个不暴露）；
- `METRICS_TOKEN` 非空 → 要 `Authorization: Bearer <token>`，不带或不对返回 401；比较用
  `secrets.compare_digest`（常量时间，避免按字节计时的猜测）；
- `curl -s http://127.0.0.1:8000/metrics | head` 就能看。

`docker-compose.yml` **没动** —— `LOG_FORMAT`/`METRICS_*` 都从 `.env` 走（compose 的 backend 段就是
`env_file: .env`），本机跑不起来 compose 也就不去改一份验不了的文件。

### 结构化日志

`LOG_FORMAT=text`（默认）是给人看的：

```
2026-05-12 03:11:07,418 INFO request request_id=verify-metrics-02bacfb9 path=/api/v1/health method=GET status=200 duration_ms=0.61
```

`LOG_FORMAT=json` 则一行一条 JSON：

```json
{"ts":"2026-05-12T03:11:07.418Z","level":"INFO","logger":"finance-api.access","message":"request","request_id":"verify-metrics-02bacfb9","path":"/api/v1/health","route":"/api/v1/health","method":"GET","status":200,"duration_ms":0.61}
```

**只吐白名单字段**（`request_id`、`path`、`route`、`method`、`status`、`duration_ms`、`threshold_ms`、
`exceeded`、`suppressed`、`worst_ms`），不是「`extra` 里有什么就吐什么」：日志是给别人看的，某天有人
往 `extra` 里塞了口令或者地址，白名单是最后一道闸。异常栈进同一个 `exception` 字段
（`formatException()`），不会把一行拆成多行。`configure_logging()` 只更新/安装自己带标记的 handler，
**不摘**别人挂的（pytest 的 `caplog`、uvicorn 自己的 `uvicorn.error`/`uvicorn.access` 都还在，后者仍是
文本）—— 所以想要一份访问日志就加 `--no-access-log`：应用日志里已经带了 method/path/status/duration_ms，
两份只会让人对不上。

慢接口告警（见「慢接口告警」一节）也走同一个 formatter，并且把 `path`/`duration_ms`/`threshold_ms`/
`exceeded`/`suppressed` 放进了 `extra` —— 正文一个字没改（⑩ 的验证脚本与单测仍按正文断言），但采集侧
不用再从正文里抠正则。

真机验证是 `scripts/verify_metrics_logs.py`（31 项）：指标文本与开关/令牌、**与活库对账**、计数跟流量走、
契约不飘、gzip 与请求 id，以及另起两个短命 uvicorn 分别验 `LOG_FORMAT=json` 与 `METRICS_ENABLED=false`
（细节见上面的脚本表）。

## 页面

| 路由 | 页面 | 数据来源 |
| --- | --- | --- |
| `/dashboard` | 财务看板概览 | `GET /dashboard?period=month/quarter/year` |
| `/analysis/revenue`、`/analysis/cost`、`/analysis/profit`、`/analysis/summary` | 收入 / 成本费用 / 利润 / 财务汇总 | `GET /analysis/summary?period=YYYY-MM` |
| `/analysis/alerts` | 预警监控（期间/状态筛选、各状态计数、确认/误报/已处理/备注、服务端分页） | `GET /analysis/alerts/all`、`GET /analysis/alerts/stats`、`PATCH /analysis/alerts/{id}` |
| `/alerts/:id` | 预警明细（最多 200 行） | `GET /analysis/alerts/{id}/transactions` |
| `/financial/import` | 数据导入（两步：预览校验 → 确认导入） | 财务导入接口 |
| `/financial/transactions` | 明细更正（按期间/部门口径/关键字/批次筛选、翻页、行内更正、两步冲销、**导出明细**——带上页面上这套筛选） | `GET /financial/transactions`、`PATCH /financial/transactions/{id}`、`POST /financial/transactions/{id}/reverse`、`GET /export/transactions` |
| `/reports` | 报告导出（Word `.docx` + Markdown 月报，可复制 / 打印导出 PDF / 下载 `.md`） | `GET /reports/docx`、`GET /reports/monthly?period=YYYY-MM` |
| `/reports/pptx` | PPT 报告（先看逐页大纲，再下载 `.pptx`） | `GET /reports/pptx/outline`、`GET /reports/pptx` |
| `/settings` | System 设置（当前账号 + 数据范围 + 管理员标识，只读；运行态与配置体检） | `GET /system/status` |
| `/agent` | 财务分析 Agent 工作台（示例问题 + 流式问答 + 依据） | `POST /ai/agent/chat/stream`（SSE） |
| `/admin` | 管理概览（数据规模、完整性体检、**季度/年度落库**与缺月清单、季/年**导出**、最近批次、接口观测） | `GET /admin/overview`、`GET /admin/rollups`、`POST /admin/rollups/rebuild`、`GET /export/metrics?grain=` |
| `/admin/users` | 用户管理（按用户名/部门/是否受限筛选、分页、改部门、删除） | `GET/PATCH/DELETE /admin/users[/{id}]` |
| `/admin/batches` | 导入批次（分页、查看、**异步**撤销并重算受影响期间 + 最近后台任务：第几次尝试、**谁在跑**（⑱ 运行中显示 `worker_id`）、失败可**重试**） | `GET /admin/batches`、`DELETE /admin/batches/{batch}`、`POST /admin/jobs/batch-revert`、`GET /admin/jobs`、`POST /admin/jobs/{job_id}/retry` |
| `/admin/knowledge` | 知识库管理（按来源模糊搜/文档类型筛选、分页、整份删除） | `GET/DELETE /admin/knowledge` |
| `/admin/departments` | 部门权限（账号绑部门、各部门数据规模） | `GET /admin/departments`、`PATCH /admin/users/{id}` |
| `/admin/alert-rules` | 预警规则（改阈值/级别/开关/描述；按部门、按期间分档） | `GET /admin/alert-rules`、`POST /admin/alert-rules`、`PUT /admin/alert-rules/id/{id}`、`DELETE /admin/alert-rules/id/{id}`、`PUT /admin/alert-rules/{alert_type}`（只改默认规则） |
| `/admin/alert-channels` | 预警外发（Webhook / 邮件通道的增删改与**测试发送**，投递台账按状态/级别/期间/通道筛选、人工**重发**、按当前配置**补发**已有待处理预警） | `GET/POST /admin/alert-channels`、`PUT/DELETE /admin/alert-channels/{channel_id}`、`POST /admin/alert-channels/{channel_id}/test`、`GET /admin/alert-deliveries`、`POST /admin/alert-deliveries/{delivery_id}/retry`、`POST /admin/alerts/notify` |
| `/admin/audit` | 审计日志（按动作/操作人/IP/日期范围筛选、翻页、导出 CSV、按保留期清理） | `GET /admin/audit`、`GET /admin/audit/export`、`POST /admin/audit/purge` |

所有业务页面共用 `frontend/src/components/AppSidebar.vue` 侧边栏；页面统一有三态：加载（骨架屏或
loading）、空数据（`.empty-state` + 去导入引导）、失败（`.error-state` + 重试按钮）。

`/reports` 的 Markdown 渲染与 Agent 回答共用 `frontend/src/utils/markdown.js`：**先把整段文本做 HTML
转义，再解析标题 / 列表 / 表格 / 粗体结构**，所以 `v-html` 拿到的永远是我们自己生成的标签，模型输出的
`<script>` 之类只会显示成文本；打印时 `@media print` 会隐藏侧边栏与顶部操作栏。

### 智能助手（财务分析 Agent）

侧边栏顶部「智能助手」组单独放置 `财务分析 Agent`（`/agent`），它与看板底部的对话卡片是**同一个组件**
（`frontend/src/components/AgentChat.vue`）：看板用卡片形态（`full=false`），工作台用 `full` 形态
（更高消息区、示例问题、清空会话）。两个入口共用一条流式链路，避免「看板里的助手」和「专属页面的助手」
行为漂移；看板卡片右上角有「专属页面 →」入口，工作台顶栏显示证据说明。

- 回答的事实来源是 `POST /api/v1/ai/agent/chat/stream`：财务证据层（`financial_evidence.py`，已导入的
  指标 / 预警 / 明细）+ 知识库重排片段，`done` 事件里的 `sources[]` 带 `kind`，前端按类型标注并在
  「依据」里**按标签去重**（同一份材料常命中多个片段）。注意后端给财务证据起的名字本身就以类型开头
  （「财务汇总表 2026-07」），前端只在名字缺少类型时才补「知识库·」前缀。
- 工作台的示例问题都按「库里确实有数据」来写：`2026-07 的费用异常在哪里？`、`本月收支差额是多少？`、
  `哪些部门支出最高？`、`成本率异常的预警阈值是多少？`。实测点第一个示例问题会流式返回约 2500 字的
  结论 + 数据依据 + 风险提示，并在末尾列出依据；截图见 `output/charts/agent_live_answer.png`。
- 向量检索**只有一条链路**：`document_chunks` 表 + pgvector（`backend/rag/pgvector_store.py`，384 维
  `Vector` 列 + `cosine_distance`），并且按 `metadata_json["embedding_model"]` 过滤，防止旧模型索引
  混进同一向量空间。仓库里没有任何 Chroma / `vector_store.py` 的代码或依赖，`init_pgvector_db.py`
  负责装扩展与建索引——换向量库时要从这里改起。

### Agent 会话落库、停止生成与 token 统计

问答不再是「看完就没了」：每一次提问与回答都落在 `agent_sessions` / `agent_messages` 两张表里，
可以回看、可以停止、可以算用量。状态机集中在 `backend/agent_sessions.py`，
`backend/api_agent.py` 只做读写与权限判断。

- **会话归属只有一条规则：会话属于提问的人。** 别人的会话 id 一律按「不存在」处理（**404，不区分
  「没有」与「不是你的」**），免得会话 id 变成探测他人提问的探针；跨用户的用量只在管理员端点看汇总。
- **先写占位行，再调用模型**：回答一开始就是库里的一行 `status="streaming"`，所以生成中刷新页面能看到
  「生成中」，而不是等写完才出现。进程被杀留下的孤儿行不靠定时任务清理——**读的时候按
  `STREAMING_STALE_SECONDS = 120` 判成终态**（`effective_status()`），库里那份不动；判成哪一个看
  `stop_requested_at`（㉒）：**有值就是「已停止」**（用户点过停止，只是那一块之后再没有进程写回终态），
  **没有值才是「已中断」**（进程崩了没人管）。都算「已中断」会把用户自己要的停止说成故障。
- **停止生成是显式请求**（`POST /api/v1/ai/agent/sessions/{id}/stop`）：生成器**按节拍回头读库**
  （`STOP_POLL_SECONDS = 1`，同一进程里另有一条零查询的本地事件快路径），看到标志就收尾、
  把已经生成的正文按 `status="stopped"` 落库、`done` 事件里回 `stopped: true`。
  前端「停止」必须**先打这个接口再 abort 本地流**：只 abort 的话服务端要到下一块才发现连接没了，
  那期间模型仍在按 token 计费。没有正在生成的回答时返回 **409**（不是 500，也不是默默成功）——
  包括「查出来还在生成、写下去时它自己刚好收尾了」这一瞬间，那也是 409，谎报 `stopped: true`
  会让用户以为停下了。**停止请求连一块都还没吐就到达时一块都不吐**，并且这一拍不按字数估算用量
  （一次模型都没调用，`total_tokens` 记 0，别在用量报表里凭空多一笔）。
- **停止标志位是库里的 `agent_messages.stop_requested_at`（㉒）**，不是进程内存里的 `threading.Event`：
  多 worker 部署时停止请求可能落在**别的 worker** 上，只有写进共享的库才停得下来；进程重启后
  读得出「当时是用户停的」。端点写这一列后**立即提交**，生成器（另一条连接）才看得见；
  同一个进程里顺手置一下本地事件，省掉那一拍查询。读库失败时**当作没停**（下一拍再问）——
  取消是尽力而为的动作，一次查询失败不该把用户正在等的回答掐掉（与㉑的限流相反，
  限流是授权闸门、读不到就必须拒绝）。这一列只增不改，终态行留着它。
- **token 统计分两路，估算不当账单**：网关在最后一帧报了用量 → `usage_source="api"`（账单口径）；
  被停止 / 出错时拿不到用量，就按字数折算（`CHARS_PER_TOKEN = 1.5`）→ `usage_source="estimated"`。
  `GET /api/v1/ai/agent/usage?days=` 把两者分开计数（`api_answers` / `estimated_answers` /
  `free_answers`），前端在被停的回答下面写「（按字数估算）」。
- **用量是向网关「要」来的，所以有一个开关**：`LLM_INCLUDE_USAGE`（默认 `true`，见 `.env.example`）让最后一个
  流式 chunk 带上 `usage`。不认这个参数的兼容网关会直接报错——后端只在**一个字都还没吐出去**时**去掉该参数
  重试一次**（已经吐过字的流不能重来，重来就是重复计费），把开关关掉最坏也只是全体退化成按字数估算。
- **窗口是「含今天的 N 个自然日」，起点对齐到那一天的 00:00（业务时区，⑯）**，`daily_series` 把没问的
  日子补 0。这里踩过两个坑：起点若按「此刻」算，① 图上的今天会消失（`by_day` 与 `series` 从不同的天开始铺）；
  ② `agent_messages.created_at` 用的是数据库的 `now()`（事务开始时刻，可能早于 Python 这边的「此刻」），
  同一个事务里刚写下的行会被 `created_at >= start` 自己筛掉。
- **追问时模型看得见前几轮（⑰）**：同一个会话里最近 6 轮「写完的成对问答」会按
  `system → 历史 → 本轮（问题 + 证据）` 的顺序交给模型，所以「那上个月呢？」知道「那」指什么。
  三条规则都在 `backend/agent_sessions.py` 的 `history_for()` 里：① 只带 `ok`/`stopped` 且正文非空的
  问答——`error`/`interrupted`/还在 `streaming` 的、以及没有回答的孤零零提问**整对丢掉**（它们是
  "没说完的话"，带上去模型会顺着半句话编）；② 超预算就从**最老的一整对**开始丢，宁可少一轮也不给半轮；
  ③ `AGENT_HISTORY_MAX_TURNS`（默认 6）与 `AGENT_HISTORY_MAX_TOKENS`（默认 1500，按
  `CHARS_PER_TOKEN = 1.5` 估）任一为 0 就等于关掉多轮。`start` 与 `done` 事件都带
  `history_turns` / `history_messages` / `history_tokens` / `history_truncated`，回答下面显示
  「已带上前 N 轮对话（更早的已省略）」；**这些数字不落库**（`agent_messages` 没这列），刷新页面后
  回答照旧可读，只是不再显示这句话。估算口径也跟着变：没用量上报时 `prompt_tokens` 把历史一起算进去。
- **显式建会话**（`POST /api/v1/ai/agent/sessions`，201）让前端先拿到 id 再开始流式——这样第一条回答
  一开始就能被停止；不显式建（看板卡片、直接问）时后端在 `start` 事件里建会话并把 id 交回去，
  前端后续追问自动带上 `?session_id=`。
- **工作台 `/agent` 左侧是会话列表**（标题 + `N 问 · M tokens`、删除、近 7 天用量），回答下面挂
  `本回答 N tokens`；管理概览页多了一张 **Agent 用量卡**（`GET /api/v1/admin/agent-usage?days=7`，
  按用户列 token、逐日列出），它有自己的错误状态——**用量统计挂了不该把整页概览变成错误页**。
- 真机验证是 `scripts/verify_agent_sessions.py`（39 项）、`scripts/verify_agent_memory.py`（⑰，15 项）
  与 `scripts/verify_agent_stop.py`（㉒，见上表）；单测在 `tests/test_units.py`
  （用量口径、估算与账单三种记账、状态归一、**停止信号的读库节拍与读不到库时不当作要停（㉒）**）
  与 `tests/test_api.py`（真库整链路，含**停止请求早于第一块时一块都不吐且不记估算用量**：
  `tests/test_api.py` 里那几个流式用例走**真库 + 真会话**，因为流式落库在 Starlette 的线程池里另开会话，
  外层事务的 savepoint 里未提交的占位行它看不见）。踩过的坑：agent 端点的完整前缀是
  `/api/v1/ai/agent/...`（`api_agent.py` 的 `prefix="/api/v1/ai"` 再加 `"/agent/…"`），少写 `/ai` 全都 404。

### Word 报告（.docx）

`GET /api/v1/reports/docx?period=YYYY-MM` 用 python-docx 生成一份**可编辑**的 Word 报告：
标题 + 副标题、一、核心指标（表）、二、风险预警（表）、三、费用构成（按部门 Top5，表）、
四、口径与限制（项目符号）。没有指标时给「数据不足」的单页文档，而不是 500 或 0 字节。
加 `department=部门号` 可以生成某个部门的报告，副标题和文件名都会标明数据范围。

两个实现细节值得记住：

- **中文字体要显式设 `w:eastAsia`**：python-docx 的默认样式只有西文（Calibri），
  不设东亚字体时在某些机器上中文会走字体替换甚至显示成方框。代码里 `_docx_set_fonts()`
  给 Normal / Title / Heading 1 / Heading 2 都补上了 `w:eastAsia="微软雅黑"`。
- **`Content-Disposition` 只能是 latin-1**，中文文件名直接塞会让 Starlette 抛
  `UnicodeEncodeError: 'latin-1' codec can't encode characters`。所以下载头统一走
  `backend/files.py` 的 `content_disposition(filename, suffix, fallback=...)`：
  `filename="<ASCII 兜底>"` + `filename*=UTF-8''<百分号编码>`（RFC 5987），现代浏览器用后者。
  规则是「名字含非 ASCII 就整个换成 fallback」——不去保留侥幸剩下的那几个字符，
  否则 `财务分析报告 2097-09 部门A` 会变成 `2097-09 A` 这种谁也认不出的名字。
  同理，**后缀要单独交给这个函数拼**：名字里带 `.docx` 的话，ASCII 兜底会因为有 `docx`
  这几个字母而「合格」，反倒把兜底机制废掉了。

前端 `/reports` 页面的「下载 Word (.docx)」按钮走 `frontend/src/api/download.js` 的
`saveBlob()`（延迟 `revokeObjectURL`，见下文「报告与文件下载的坑」）。

### PPT 报告

`GET /api/v1/reports/pptx?period=YYYY-MM` 用 python-pptx 生成 5 页演示文稿：封面、核心指标、
风险预警、费用构成（按部门 Top5，**原生柱状图对象**）、口径与限制。报告不做任何图形截图，
因此不需要 matplotlib/PIL。未安装依赖时返回 503 并给出安装命令。

`GET /api/v1/reports/pptx/outline` 返回同一份内容的 JSON 大纲（每页 `title`/`kind`/`bullets`，
指标页带 `table` 行），前端先展示大纲、确认后再下载，也让测试可以在不解压 pptx 的前提下断言结构。

依赖用 `python-pptx>=1.0`（见 `requirements.txt`）。本机若因镜像源不稳定卡住，可以手工取 wheel：

```powershell
# 从 PyPI 直接挑 py3-none-any / cp311-win_amd64 的 wheel（带依赖）下载到指定目录
.\.venv\Scripts\python.exe scripts\fetch_pptx_wheels.py --out output\_wheels
# 离线安装，不解析索引、不装依赖（依赖 wheel 也在这批里）
.\.venv\Scripts\python.exe -m pip install --no-index --no-deps (Get-ChildItem output\_wheels\*.whl).FullName
```

### System 设置

`GET /api/v1/system/status` 汇总运行态：应用环境、令牌有效期、CORS、日志级别、数据库版本与
pgvector 是否安装、Embedding 模型与维度、大模型是否已配置、检索阈值与召回数、各表行数与最新期间、
**三组配额（登录/导出/问答）的计数存在哪**（㉑ 的 `rate_limits`：`store` 是
`postgres:rate_limit_hits`、`shared_across_processes`/`survives_restart` 都是 true、当前活跃桶数），
以及**目录可写性**（真写一个探针文件再删，而不是只看路径是否存在）。密钥与口令只报告「有没有配」，
数据库 URL 里的口令会打码成 `user:***@host`；`config_problems` 复用启动时的 `Settings.validate()`。
`rate_limits` 只报**聚合桶数**，不给分桶名——键里有用户名与来源 IP，而这个接口普通账号也能读。

设置页现在**只读**：三张卡分别是「当前账号」「数据范围」「管理员标识」，都读 `GET /system/status` 的
`me`。（⑳ 之前这里有输入框，走 `PATCH /api/v1/auth/me`；那条自助入口正是「把自己部门清空 = 升成
管理员」的入口，现在一律 403 并留 `user.department_self_denied` 审计。）想改范围请找管理员在
`用户管理` 里分配；只是想换一次查看口径，用页面右上角的「查看口径」下拉——那走的是请求参数，
不写库、也不改账号属性。

一个账号可以有**多个部门**，看到的是这些部门的并集——金额相加、比率按相加后的金额重算（比率不能相加）。
**一个部门都没分配**的账号什么都看不到（空范围不是「不受限」），知识库里只剩没标部门的公共资料。
部门**只认服务端记录**，不采信请求头自述（⑳ 起连兼容参数都删掉了：那条路径同样能被伪造头提权）。

> 设置页读的是 `GET /system/status` 里的 `me.departments` 与 `me.is_admin`。
> 这里踩过一个真坑：最初只回了单值列 `department`，多部门账号（018+097）一进页面就只看到
> `018`、页面写着「限定 018」，点一次保存就把自己缩成了一个部门。真机冒烟脚本当场抓到，
> 现在 `me` 同时给 `departments`（完整列表）与 `department`（第一个部门，老文案用）。
> ⑳ 之后还多一个坑：`unrestricted`（数据范围）与 `is_admin`（权限）**不能互相推**——早先
> 前端正是用 `unrestricted` 当管理员判据，才把「清空部门」变成了自助升权。

同一页还有「登录与令牌」卡片：显示当前账号与会话版本（`me.token_version`）、本机会话是否有效，
以及「退出登录（作废全部令牌）」按钮——令牌是无状态的，退出不是清本地那一份，而是把账号的会话版本 +1
（详见「会话版本号与强制下线（退出登录）」）。

「运行态」卡还显示**业务时区**（`app.business_timezone.label`，如 `Asia/Shanghai (UTC+08:00)`）与它算出的
业务日、当下时刻——「今天」「本月」到底按哪个日历日算，页面上直接能看见，不用去读代码或猜机房在哪
（详见「业务时区（`BUSINESS_TIMEZONE`）」）。老后端没这个字段时显示「（未上报）」，页面其余部分照常。

### 上传件的去留（有意的不对称）

`data/uploads/` 下现在只有知识库文档，**财务导入的临时件处理完就删**：

- `POST /financial/import` 会把上传内容先落盘再解析（预览与导入同一条代码路径）。这份副本是纯临时的：
  内容 sha256 已经成为批次号、明细已经入库、审计也已经留痕，留在磁盘上只会让目录只增不减
  （成功、失败、重复导入 409 都会各留一份，实测历次导入攒了 8 个 974KB 的 CSV）。所以在 `finally` 里
  `path.unlink(missing_ok=True)`，解析失败的请求同样不残留。
- 知识库文档上传**必须保留到没有片段引用为止**：`document_chunks.metadata_json["stored_path"]` 指向它，
  删掉就成悬空路径。所以清理是**跟着删来源一起做**的：`DELETE /admin/knowledge?source=` 在删片段之后，
  按 `stored_path` 数一遍还有没有别的来源引用这份文件，归零才 unlink（返回与审计里都有 `removed_files`）；
  文件路径还必须是 `rag_upload_dir` **之内**的才删——`stored_path` 是库里的值，被改坏或指向
  `data/raw` 的只读原件时，拿它直接 unlink 就是在删别人的数据。多个来源共用一份上传件时，
  只有最后一个引用被删掉的那次才会让文件消失。

对应回归：`tests/test_api.py` 断言导入成功后 `upload_dir` 里没有 `*.csv`、解析失败后临时目录为空；
`test_admin_knowledge_delete_removes_only_orphan_upload_files` 用 `tmp_path` 造三个文件（两来源共用一份、
一份独占、一份指向上传目录之外），断言只有引用归零的那份被删、越界路径不动。

### 查看口径切换（管理员）

看板、分析、报告、PPT、Agent 的右上角有一个「查看口径」下拉：不受限账号可以临时切到某个部门，
整站所有页面跟着换（口径是**会话级视角**，放在 `frontend/src/api/scope.js` 的模块级 `activeScope`），
URL 上体现为 `?department=097`。切换后页面顶部会出现提示条，明确当前不是全公司口径。

- 选项来自 `GET /api/v1/analysis/departments`（数据里真实出现过的部门 + 明细行数）；
  受限账号看不到这个控件，且后端对填别人部门名的请求一律 **403**（不静默回落）。
- 有个坑值得记下来：控件最初用 `onMounted` 拉选项，而挂载时 `/auth/me` 往往还没回来，
  `switchable` 仍是 false，`load()` 直接 return 后再没人调用它——表现是**下拉里只有「全公司」一个选项**。
  改成 `watch(switchable, …, { immediate: true })` 才对。截图纯看下拉「存在」是发现不了的，
  必须在真浏览器里点一遍（`scripts/check_pages.py` 只查溢出）。

### 数据导出

`/api/v1/export/{transactions,metrics,alerts,scope}` 把库里的数据交回用户手里（CSV / XLSX，上限 2 万行）。
完整契约见 `docs/api-contract.md` 的「数据导出」。几个刻意的选择：

- 导出范围由**服务端权限**决定：受限账号导不出别的部门，管理员导出的是全公司行，两套行永不混算。
- 行数与文件名取自响应头（`X-Export-Rows` / `X-Export-Truncated` / RFC 5987 的 `filename*`），
  前端不自己数一遍——否则「被上限截断」这件事会被悄悄掩盖。
- CSV 带 BOM 且**一切皆文本**（避免 Excel 把 `2026-10` 认成日期、把部门号 `097` 变成 `97`）；
  XLSX 里数字列是真数字，能直接求和。
- **指标导出可以按粒度导（⑲）**：`grain=month|quarter|year`。季/年**不重算，直接读 `financial_rollups`**
  （⑧ 的落库表，与 `/admin/rollups` 同一份数据），并多出「包含月份」与「是否完整」两列：
  `complete=False` 的期间写「否」，年度合计不假装是全年。粒度与区间端点不一致（`grain=quarter&period_from=2026-04`）
  是 **422 并给出样例**，而不是安静地返回空表。
- **明细导出的期间不再被当月份字符串（⑲）**：`?period=2026-Q3` 与 `?period=2026` 都认（区间比较），
  另加 `?batch=`（导入批次精确）与 `?query=`（科目编码/名称/摘要模糊）。这三个条件与
  `/financial/transactions` 列表**共用 `backend/transactions.py::apply_filters`** —— 页面筛出几行，导出就是几行，
  多条件是「且」。以前两边各写一套，迟早会出现「页面 12 行、导出 15 行」这种没人能解释的差。
- **明细表尾三列解释 ⑨ 的更正与冲销**：`冲销状态`（空 / `已冲销` / `冲销行`）、
  `原始行 id` + `冲销时间` + `冲销操作人`（后两列只写在被冲销的原行上，记的是**冲销那一刻**）、`更正次数`。
  导出的合计与页面看到的净额一致（冲销行是负数行，原行保留）。
- `X-Export-Truncated` 由「实际行数 > 上限」判定（三个端点统一），**没截断时这个头不出现**，
  前端据此区分「导全了」与「被切了一刀」。
- 界面上三个入口各管一段：`/financial/transactions` 的「导出明细」带上页面上看得见的期间/部门/批次/关键字
  （页面自带口径时**不再叠加**全局口径，否则同名的 `department` 会出现两次、谁生效看解析顺序）；
  `/admin` 概览落库卡上的「导出季度」/「导出年度」；`/analysis/revenue` 的导出改成本页指标
  （以前是明细，和屏幕上那些指标卡不是一回事）。

### 预警处理闭环

改造前 `financial_alerts.needs_human_review` 建行时写 `True`，**全项目没有任何地方能改它**：
预警列表读起来永远像「没人看过」，而 `calculate()` 又是「先删再建」，所以就算手工改了也会在下一次
导入 / 撤销批次时被静默抹掉。现在补齐了这条链路：

- 每条预警有处理状态：`pending` 待处理 / `confirmed` 已确认 / `false_positive` 误报 /
  `resolved` 已处理，附带备注、处理人、处理时间（`PATCH /api/v1/analysis/alerts/{id}`）。
  **标记误报必须写原因**（422「标记为误报时必须填写原因」）——判成误报却不写理由，
  等于把「为什么不是问题」这件事丢了，后续没人能复核。
- **状态随重算保留**：`calculate()` 先读 `_review_state(db, period)`（键是「口径 + 预警类型 + 指标名」），
  重建时按同一键继承，`needs_human_review` 由 `review_status == "pending"` 推出来，不再各写各的。
  行 id 会变，结论不会丢。
- 阈值从写死的常量搬进 `alert_rules` 表（`backend/alert_rules.py`），管理员在
  `/admin/alert-rules` 上调；**改完只对下一次重算生效，不追溯改写历史预警**——历史预警是当时口径下的判断，
  追改会让当月报告与当时的结论对不上。规则形状（比哪个指标、大于还是小于）不开放编辑：
  平台只有「某指标超过/低于阈值」这一种真实需求，做成表达式编辑器反而无法验证。
- **规则可以分档**：同一个 `alert_type` 允许存在多条规则，分别限定**部门**、**期间**或两者。
  重算时对每个 `(期间, 部门)` 只取**最具体的一条**：部门+期间 > 只限一个维度 > 默认规则，
  **命中即覆盖，不叠加**（`backend/alert_rules.py` 的 `resolve_rules`）。所以「部门 018 在 2026-10 从严」
  不会顺手改掉兄弟部门或其它月份的口径。默认规则（两个维度都空）**只能停用、不能删**：
  删了那个 `alert_type` 就彻底不报了，而「不报」和「停用」在页面上看起来一样、后果差很远。
  分档规则必须至少限定一个维度（两个都空的 422，请改默认规则）；同一「类型+部门+期间」重复 409。
  老库升级要同时**补 `department`/`period` 两列**并**去掉 `alert_type` 的唯一性**——
  注意模型里的 `unique=True, index=True` 在 Postgres 里是 **UNIQUE INDEX**，
  `information_schema`/`pg_constraint` 都查不到，必须按 `pg_indexes.indexdef` 判断再 `DROP INDEX` +
  `CREATE INDEX`，否则第二次插入同样类型的分档规则会 `UniqueViolation`（`migrations.py` 的 `PLAIN_INDEXES`）。
- 预警监控页顶部有各状态计数（`GET /api/v1/analysis/alerts/stats`）与状态筛选；统计**不跟着筛选项走**，
  否则一筛「待处理」就再也看不到自己在处理什么。

### 预警外发通道（Webhook / 邮件）

预警以前只能在页面上看，没人打开页面就等于没发生。现在管理员可以配外发通道（`backend/alerts_outbound.py`），
重算/导入/更正/冲销产生的预警会自己发出去：

- 两张表：`alert_channels`（名称、`kind` = `webhook`/`email`、目标 URL 或收件人、**级别门槛**
  `min_level`、可选的**部门限定**、开关、HMAC 密钥 `secret`）与 `alert_deliveries`（投递台账）。
  `secret` **只写不读**：接口与页面永远只回 `has_secret`，改通道时留空表示「不动密钥」、填空串才清空。
- **去重键是内容寻址的**：`alert_key = f"{period}|{department or ''}|{alert_type}|{metric_name}"`，
  与 `channel_id` 一起做唯一约束。因为 `calculate()` 是「先删再建」，预警行 id 每重算一次就换一批，
  按行 id 去重等于**每重算一次就把同一件事再通知一遍**；内容寻址之后，同一条预警对同一条通道只有一张投递单。
  台账里另存一份 payload 快照——重算会把预警行删掉，重试时不能依赖它还在。
- 级别门槛按 `low < medium < high` 比，通道限定部门时只发该部门的预警（空 = 不限，含全公司口径那条）。
- **失败退避重试复用后台任务**：`plan()` 建 `pending` 投递单，`schedule()` 提交一个 `alert.dispatch` 任务，
  `jobs.py` 的 2/4/8…/60 秒封顶退避照旧，尝试上限取 `ALERT_MAX_ATTEMPTS`（默认 3）。
  处理函数**只处理还没成功的投递单**（幂等），并且**先 `db.commit()` 落盘再抛异常**让任务退避——
  `run_job()` 在异常时会 `rollback()`，不先提交已经发成功的那些，下一轮就会把它们重发一遍。
- 人工「重发」的口径与 `jobs` 的不同：投递单的 `attempts` 只用来决定「还要不要再试」，
  管理员判定通道已经修好，就应该能立刻重试，所以重发把 `attempts` 归零（`jobs.prepare_retry` 则是追加预算、保留编号）。
- **出口安全取舍**（写在模块 docstring 里）：只允许 `http`/`https`；拒绝 URL 里带用户名口令
  （凭据会顺手写进日志与台账）；**不跟随 3xx**（否则带签名的正文会被转发到任意主机）；超时 5 秒、
  响应体最多读 2 KB。**刻意不封禁内网地址**——告警网关/机器人本来就住在内网，封了等于这个功能不能用于真实环境。
  Webhook 正文是 JSON，带 `X-Finance-Event`/`X-Finance-Timestamp`/`X-Finance-Delivery`/
  `X-Finance-Signature`（`sha256=<hmac>`，签名串是 `"{timestamp}.{body}"`，收端可据此验真）。
- 邮件通道走 SMTP（`ALERT_SMTP_*`，587 + STARTTLS；465 隐式加密不支持）。**没配 SMTP 是配置错，
  直接判失败、不烧重试预算**——重试三次也变不出一个 SMTP 主机，只会把退避时间浪费掉。
- 接口：`GET/POST /api/v1/admin/alert-channels`、`PUT/DELETE /api/v1/admin/alert-channels/{channel_id}`、
  `POST …/{channel_id}/test`（同步发一条测试，**发送失败也回 200**，看 `ok` 字段；不写台账）、
  `GET /api/v1/admin/alert-deliveries`（台账，可按状态/级别/期间/通道筛）、
  `POST …/{delivery_id}/retry`、`POST /api/v1/admin/alerts/notify`（把已有「待处理」预警按当前配置补发，
  新建通道后的主入口）。页面在 `/admin/alert-channels`，含通道表与投递台账。
- 真机验证是 `scripts/verify_alerts_outbound.py`（41 项）：起一个本地 sink（支持「一直失败」与「先失败后成功」），
  真的建通道、真导入一个探针期间、验 4 条投递全部送达且签名 4/4 对得上、把 sink 切到失败后验退避真的重发
  12 次（3 轮 × 4 条）且 `attempts` 停在 3、人工重发一次就成功、通道计数与审计留痕，跑完撤销探针批次并删掉临时通道。

### 审计日志

撤销导入批次、删账号、删知识库、改预警规则、处理预警这些动作以前**只改数据不留痕**：
出问题只能翻 uvicorn 日志，而它重启即丢。现在每次写操作都落一条 `audit_logs`（`backend/audit.py`）：

- 记录的是**结构化事实**：时间、操作人、操作人当时的部门、动作（`user.delete` 这类机器可读码 +
  中文标签）、对象类型与 id、来源 IP、以及一小袋 detail（如改规则的前后阈值、撤销批次删了多少行）。
  **不记密码、令牌、文件内容与明细金额**：`_scrub()` 会把键名含 `password/token/secret/authorization/api_key/hash`
  的字段整条丢掉（任意嵌套深度都丢）、字符串截断到 200 字、列表只留前 50 项。
- 37 个动作在 `ACTIONS`（`backend/audit.py`）里登记（含 ⑩ 的 `job.retry` 重试后台任务、⑭ 的 `alert_channel.*` 与 `alert_delivery.retry`、⑮ 的 `agent_session.delete`、⑳ 的 `user.department_self_denied` 与 `user.admin_change`）；写一条未登记的动作不会失败，但会打 warning——避免埋点时漏改这里就静默少记。
- ⑳ 起 `PATCH /auth/me` 一律 **403** 并留 `user.department_self_denied`（detail 带你打算改成什么）：
  数据范围与管理员标识只能由管理员在 `PATCH /admin/users/{id}` 改，两条变化分别留
  `user.department_change` / `user.admin_change`。`user.department_self` 保留但已无成功路径，
  老库里的历史记录仍能按它筛出来。
- **审计失败不影响业务**：`record()` 内部兜住所有异常，写日志失败只记 error，不会把接口带崩。
- detail 与业务写在**同一个事务**里（`record(..., commit=False)`），业务回滚时日志也不落地；
  只有登录失败/被限流这两条走 `commit=True`（那时没有业务事务可依附）。
- 管理员在 `/admin/audit` 看（`GET /api/v1/admin/audit`），可按**动作、操作人、来源 IP（前缀匹配）、
  日期范围**四类条件筛选并翻页。日期是「本机日历日、含首尾两天」：`_audit_window()` 把日期转成
  带时区的时刻再比，否则 UTC+8 机器上「今天 8 点前的记录」会查不到。
- **导出**：`GET /api/v1/admin/audit/export` 按同一套筛选条件给 CSV（正文带 UTF-8 BOM，否则 Excel
  打开中文是乱码；响应头 `X-Export-Rows`/`X-Export-Truncated` 说明条数与是否被 `AUDIT_EXPORT_LIMIT`
  截断）。导出这件事自己也写一条 `audit.export`——「谁把审计捞走了」和「谁改了数据」同样要能查。
- **只增不减，唯一的删除是按保留期清理**：`POST /api/v1/admin/audit/purge` 默认 `dry_run=true` 只报数，
  确认后才删 `created_at` 早于 N 天（默认 `AUDIT_RETENTION_DAYS=180`，下限 1 天）的记录，并写一条
  `audit.purge`（含截止时间与删除条数）。界面上是「预览 → 确认」两步。
  审计行本身没有任何 PUT/DELETE 接口：能改的审计等于没有审计。
  这是进程内的策略实现，**没有定时任务**——要不要清、什么时候清由管理员决定（详见「数据库迁移」一节的取舍口径）。

`audit_logs` 是新建的表，而 `create_all()` 只建「已注册进 metadata」的表——注册靠导入。
`backend/migrations.py` 因此显式 `from backend import models`：只 `from backend.migrations import ensure_schema`
的场景（一次性脚本、测试夹具）曾经因此把这张表静默漏掉，接口再报 `relation "audit_logs" does not exist`。
同理，`tests/conftest.py` 加了 session 级 autouse 夹具 `ensure_application_schema()`：
`httpx.ASGITransport` **不会触发 FastAPI 的 startup 事件**，不补这一步新表在用例里根本不存在。

### 分页与筛选

列表页都走「服务端分页 + 服务端筛选」，前端只负责把筛选条件拼进查询串、把 `total` 显示出来。
改造前的两个真实毛病值得记下来：

- **`total` 撒谎**：`/admin/users`、`/admin/batches`、`/admin/knowledge` 的 `total` 都是 `len(当前页数据)`
  （而不是 `count(*)`），没有 `offset`。列表一旦被 `limit` 截断（用户超过 50、知识库来源超过 50），
  页面上的「共 N 条」就变成本页条数，翻页按钮也永远只有一页。
- **筛选发生在分页之后**：`/alerts/all` 原本没有 `status` 参数，`AlertsList.vue` 是拿回一页数据后在
  前端 `filter`。库里 68 条预警时「全部期间」只显示 50 条且毫无提示，**第 2 页里的误报会被静默漏掉**——
  筛选必须在分页之前做，这是接口参数而不是前端视图的事。

现在的约定（`docs/api-contract.md` 有完整字段）：

- 每个列表接口都返回 `total`（筛选后的总行数，与 `limit` 无关）、`offset`、`limit`；
  `limit` 有上限（用户/知识库 500、批次 200、预警与审计 200），越界的 `limit=0`/`limit=501`/`offset=-1`
  一律 **422**（边界由参数校验挡，不在函数体里悄悄夹取）。
- 知识库是「一行 = 一个来源」的聚合视图，片段级条件（文档类型）必须在 `group_by` **之前** `where`，
  否则会得到「来源匹配但片段数是全库的」。响应因此同时给 `total`（来源数）、`matched_chunks`（筛选后片段数）
  与 `all_chunks`（全库片段数）——前端显示成「命中 3 份来源 · 5 条片段（全库共 5 条）」，三个数各有出处。
- 筛选下拉的选项由后端提供（`/admin/users` 回 `departments` 与 `unrestricted_count`，
  `/admin/knowledge` 回 `document_types`）。选项要是拿当前页数据现凑，翻到第 2 页就会出现选项变少、
  已选中的值凭空消失。
- 前端共用 `frontend/src/components/PagePager.vue`（上一页 / 第 x / y 页 / 当前区间 / 总数 / 下一页），
  五个列表页（预警监控、审计日志、用户、批次、知识库）都用它，避免各写一份「共 N 条」而各自算错。
- 删除操作要照顾页码：删掉当前页最后一条时若 `offset` 已经超出，自动回退一页——否则会停在空白页，
  看起来像「删一条把整页删没了」。

### 后台任务（取代 Celery 脚手架）

撤销一个导入批次不是「删几行」：明细删掉后，每个受影响期间都要重新聚合指标与预警，期间一多就是
「一个 HTTP 请求里重算所有期间」，超时与否全看数据量。这件事现在走**后台任务**。

原来的脚手架是 `backend/celery_app.py` 与 `backend/tasks.py`：**它们从来没跑起来过**——这个部署没有
Redis（连 `redis` 包都没装），broker 与 worker 都不存在，而且 `run_monthly_analysis` 没有任何调用点。
留着一套「看起来能异步」的死代码，比没有更糟，所以删掉了这两个文件与 `requirements.txt` 里的
`celery`，`backend/config.py` 的 `redis_url` 也一并去掉。

现在的实现（`backend/jobs.py` + `background_jobs` 表）：

- **进程内队列 + 数据库记录**：`POST /admin/jobs/batch-revert` 先往 `background_jobs` 写一条
  `queued`，再交给 FastAPI 的 `BackgroundTasks` 在响应发出后执行，接口立刻回 **202**。任务本身
  自己开一个 `Session`（请求级 Session 那时已经关了）。
- **状态机**：`queued → running → succeeded / failed`。失败原因截断到 500 字写进 `error`，
  结果（删除行数、重算期间、清空期间）写进 `result`，界面上「最近的后台任务」直接读它。
- **提交是幂等的**：同一批次已有任务在队列里（`queued`/`running`）时再点一次，不排第二条，
  返回的还是那条老任务并带上 `deduplicated: true`（界面上必须说出来，否则用户以为点了两次就撤销了两次）。
  另外认 `Idempotency-Key` 请求头：同一个键重放拿回同一条任务 —— **连已经成功过的也算**，
  因为「这个键代表的那件事已经做完了」正是客户端重试时最想听到的答案。兜底是一张
  **部分唯一索引**（`idempotency_key` 唯一，但只约束 `queued/running/succeeded`）：先查后插在并发下
  仍有窗口，索引负责在那一刻抛 `IntegrityError`，代码捕获后回头取那条已存在的任务。
  键**不参与**活跃去重的指纹计算：`payload` 里的 `ip` 每请求都在变，拿它当身份会让同一次撤销被排两遍。
- **失败会重试**：处理函数抛异常不再等于判死。`attempts` 每次执行加一，`max_attempts` 默认 3；
  还有预算就放回 `queued` 并写 `next_attempt_at = now + 2·2^(n-1) 秒`（封顶 60 秒，退避序列 2、4、8…），
  由模块级 `SCHEDULER`（默认 `threading.Timer`，测试直接替换它）到点再跑一次；预算用尽才落 `failed`。
- **重复执行必须安全**：处理函数要按**幂等**写。撤销任务的重跑是这样收口的 —— `context["is_retry"]`
  会传给 `batch_ops.revert_batch(..., allow_missing=...)`：第一次跑时「一行都没删」是真异常（批次不存在
  或已被别人撤销），重跑时同样的情形说明**上次已经把活干完了**，于是判成功并在 `result` 里标
  `already_reverted: true`，而不是让一个已经完成的任务永远显示失败。
- **重启接续（⑱ 起跨进程安全）**：进程重启时 `jobs.resume_interrupted_jobs()` 把遗留的 `queued`/`running`
  放回队列接着跑（`error = 进程重启，任务已重新排队（下次是第 N 次尝试）`），并且**保留**已经用掉的尝试次数；
  只有两种情况判失败：预算已用完（`error` 里点名用了几次），或者这个 `kind` 已经没有处理函数了
  （代码删了、库里还留着行）。放回队列的任务会立刻被调度一次，所以重启后不需要人工去点。
  **⑱ 的两道护栏**：① 只重排「真的没人跑」的任务——每条任务的执行权由一把 Postgres **会话级 advisory 锁**代表，
  接续时先探一下这把锁，抢得到说明持有它的进程已经死了（连接断开、锁由数据库释放）→ 才重排并清空
  `worker_id`/`started_at`；抢不到说明那个副本还活着 → 原样不动，只列进返回值的 `still_running`。
  以前这里一律重排，于是第二个副本一启动就把别人的活又排了一遍。② 接续本身也用一把全局锁串行化，
  抢不到的副本直接返回 `{"skipped": true}`、一个字都不改。执行入口同理：`jobs.run_job(job_id)` 先抢执行权锁，
  抢不到就安静跳过；抢到后再用**条件 UPDATE**（`WHERE id=… AND status='queued'`）把行改成 `running`，
  `rowcount != 1` 就当没抢到——所以「两个进程同时拿到同一条任务」在数据库那一层就不可能发生，
  `attempts` 也只会加一次。`worker_id` 字段记着「谁在跑」，跑完/判失败/放回队列都会清空。
- **人工重试**：`POST /admin/jobs/{job_id}/retry`（只认 `failed`，其它状态 409）把任务放回队列并
  **追加**一份预算（`max_attempts = attempts + 3`），不把 `attempts` 归零 —— 归零会让重跑被当成
  「第一次尝试」，正好丢掉「上次可能已经做完了」这个信息。上次的失败原因保留在 `error` 里，
  按下去会在 `audit` 记一条 `job.retry`（带 `previous_error`）。
- **提交不等于执行**：用例专门断言「POST 之后明细还在」，再手工调 `jobs.run_job` 才断言数据被删，
  避免把「入队成功」当成「撤销完成」。
- **撤销逻辑只有一份**：同步的 `DELETE /admin/batches/{batch}` 与后台任务都调
  `backend/batch_ops.revert_batch()`——删除、重算、审计写在同一个事务里，撤销到一半不会留下
  「明细没了、指标还在」的幽灵报表。
- **单进程派发的边界（⑱ 收窄）**：多副本部署仍要换成外部队列，但**抢占与接续本身已经跨进程安全**——
  执行权锁 + 条件 UPDATE 保证同一条任务只会被一个副本执行一次，接续只会重排真的没人跑的任务，
  且同一时刻只有一个副本在接续。剩下的边界是：`BackgroundTasks` + `threading.Timer` 只在**提交任务的那个进程**
  里有意义（任务不会漂到其它副本去跑，重试定时器重启即丢，靠启动接续补回来），
  所以这条约束写进了代码 docstring 与本节。
- **前端看得见「谁在跑」**：`/admin/batches` 的任务行在 `running` 时会多写一行 `运行进程：<worker_id>`
  （`worker_id` 形如 `主机名-pid-随机串`）。它是排障用的——多副本下能一眼确认这条任务归哪个副本。
- 前端 `/admin/batches` 提交后按 800ms 轮询任务状态，撤销中的批次按钮变成「撤销中…」并禁止重复提交；
  页面下方列出最近 5 条任务（类型/状态/第几次尝试/提交人/耗时/结果），失败的行有「重试」按钮，
  排队等退避的行把 `next_attempt_at` 显示在结果列的 tooltip 里。
- **真机验证**：`scripts/verify_job_retry.py`（31 项）打真后端、查真库，**分两阶段**——第一阶段跑完会把
  一条任务造成 `running` 残骸并写下 `output/_job_retry_state.json`，人为重启后端（启动日志里能看到
  「启动接续：N 条未完成的后台任务已重新排队」），第二阶段再确认那条残骸被接续跑完（`attempts` 从 1 涨到 2、
  批次的行真的被删掉）。这条链路里「重启」是脚本自己造不出来的前提，所以立项时就按两阶段设计，
  而不是把重启之后的断言删掉。
- **真机验证（⑱）**：`scripts/verify_job_claim.py`（23 项）**起两个真 Python 子进程抢同一条任务**——
  两个子进程都走生产路径 `run_job(job_id)`（不传 db），谁真跑到处理函数由计数文件数出来（1 行才算对）。
  另有一条检查专门把「池化连接 `close()` 之后锁还在」固定成事实：执行权只能显式 `pg_advisory_unlock`，
  不能指望关连接。跑法见上表。

### 安全收口

- **占位密钥自动替换**：`AUTH_SECRET_KEY` 为空或仍是 `change-this-secret-in-env` 时，进程启动会用
  `secrets.token_urlsafe(48)` 生成并**就地写回 `.env`**（保留其它行、不重复追加键）。占位密钥意味着
  任何人都能自己签一个合法 JWT。副作用：密钥一变旧 token 全失效，需要重新登录。
- **登录限流**：滑动窗口（默认 10 次 / 300 秒，按「用户名 + 来源 IP」分桶），超限 429 + `Retry-After`；
  被拒的尝试**不计**（拒绝不延长封锁），登录成功后清零。**边界**：分桶键里带来源 IP，
  所以换个 IP 仍能重来（这是分桶口径，不是存储口径）；账号级的封堵由下面的「登录失败锁定」负责。
- **三组配额的计数都落库（㉑）**：`login` / `export` / `ai` 的每一次尝试都是一行
  `rate_limit_hits(bucket, hit_at)`（桶名形如 `login:张三@10.0.0.9`、`export:张三`），
  判定时取同桶的行、按 `hit_at` 裁出窗口内的那些 —— **存的是逐次尝试的时间戳，不是一个计数**，
  否则跨窗口边界会出现「上一分钟的额度在这一分钟又用一遍」的突发。
  于是它**跨进程/副本共享、重启不清零**：多 worker 部署时是同一本账（`SELECT pg_advisory_xact_lock`
  按桶串行化，避免两个并发尝试都读到最后一行然后各自放行）；库连不上时是 **fail-closed**（报错，
  不静默放行）。运维入口：`GET /system/status` 的 `rate_limits` 段如实报「共享、跨重启」与三组配额，
  只给**聚合桶数**（分桶键里有用户名与来源 IP，普通账号也能看这个接口）。
  进程启动时会顺手清掉一小时前的死行（比最长的窗口还老，删了不会让谁提前解封）。
- **出口配额（导出 / 问答）**：限流不止登录，另外两组「一次请求就很贵」的出口各自独立计数：
  `export`（明细/指标/预警导出，默认 **30 次/分钟**）与 `ai`（RAG 检索与问答、知识库写入，默认 **60 次/分钟**），
  按**账号**分桶，超限同样是 429 + `Retry-After`。`EXPORT_LIMIT = 20000` 只限制**单次**导出行数，
  拦不住「脚本每分钟导 100 次」；问答每次都要调模型，是按量计费的东西，必须有配额。
  三组配额（`login` / `export` / `ai`）互不影响——登录被限流不该影响看数据，导出打满也不该让人问不出问题；
  配置项 `EXPORT_MAX_PER_MINUTE` / `AI_MAX_PER_MINUTE`（默认 30 / 60，回归脚本一次要连问 28 个问题，
  所以默认值给得比「防脚本」的理论下限宽松，避免误伤自己）。
- **两类 429 在审计上不一样（有意为之）**：**登录被限流会留痕**（`user.login_blocked`）——
  那是「有人在打这个账号」的证据，值得一条记录；而**导出/问答的配额 429 一个字都不写**
  （`backend/limits.py` 的注释）：一次被拒的请求写一条审计，等于给攻击者一个「用便宜请求写满审计表」
  的放大器；这类拒绝已在观测性里按 `429:路径` 计数，管理概览能看到。
- **`.xls` 友好失败**：没装 `xlrd` 时返回 400 并说明两条出路（装 xlrd 或另存为 .xlsx），
  而不是 500 或者一句「无法解析」。
- **前端错误信息**：`frontend/src/api/client.js` 新增 `errorMessage()`，把 FastAPI 的 422
  （`detail: [{loc,msg}]`）拼成一句人话——之前会显示成「导出失败：[object Object]」。

### 会话版本号与强制下线（退出登录）

JWT 是无状态的：**签出去就收不回来**。改密码、删账号都拦不住一份已经签发的令牌继续用（删账号只在
「用户不存在」时 401，而账号还在、只是不该再让人用的情况更常见：笔记本丢了、共用账号要收回）。
所以加的是**会话版本号**：`users.token_version`（整数）写进令牌声明 `ver`，每个请求比对一次，
不一致就 401「登录已失效（该账号已在别处退出，请重新登录）」——`+1` 就等于把该账号此前签发的**每一份**
令牌作废。

- **默认 0 而不是 1**：`server_default="0"`，并且 `token_version_of()` 把 NULL 当 0、令牌里没有 `ver`
  也当 0。这样老库补列、老令牌都继续可用，**升级不会把所有人踢下线**（否则上线当天全员要重登，
  而「所有人都掉线」这种信号会掩盖真正该看的异常）。
- **老库补列必须带 DEFAULT**：`migrations.COLUMNS` 里是 `("users", "token_version", "INTEGER DEFAULT 0 NOT NULL")`。
  不带 DEFAULT 的话已有行是 NULL，而「令牌 ver(0) != 这一列(NULL)」当场把所有人挡在门外。
- **只增不减**：`revoke_tokens()` 永远 `+1`，不提供「设回 0」。版本号回退会让上一次下线前签发的令牌
  重新生效，那是把安全功能变成安全洞。
- `POST /auth/logout`：自己退出（`user.logout` 审计，detail 里叫 `session_version`——键名含 `token`
  会被 `audit._scrub()` 整条丢掉，这是踩过的坑）。**代价是该账号在别的设备上的登录也一起掉**，
  这正是「退出登录」该有的语义。
- `POST /admin/users/{user_id}/logout`：管理员强制下线（`user.force_logout` 审计，记 `from_version`→`to_version`）。
  复用 `_target_user()`，所以对自己操作同样是 400——想让自己下线请用 `/auth/logout`，两者的审计语义不同。
- 前端：System 设置页新增「登录与令牌」卡片（当前账号 + 会话版本、本机会话是否有效、「退出登录（作废全部令牌）」）。
  **先调接口再清本机 `localStorage`**：顺序反了就没令牌可带，服务端永远收不到这次退出；服务端没确认也
  照常清本机（用户按了退出，本机就该退），但不自动跳转，留一个「去登录页」按钮把「服务端已作废」先让人看见。
  用户管理页每行新增「会话」列（`未作废过` / `已作废 N 次`）与「强制下线」按钮，自己那行禁用。
- 真机验证：`scripts/verify_session_revocation.py`（22 项）真登出、真踢人，并核对审计与
  「没有 `ver` 的老令牌仍然可用」这条升级兼容性。

### 账号锁定与口令强度（连续失败与弱口令）

限流按「用户名 + 来源 IP」分桶，挡的是「同一个来源猛打同一个用户名」；它管不着同一账号换来源
（分布式尝试）—— 换 IP 就能重来。所以连续失败还要落库，锁的是**账号**而不是「这个 IP 的用户名」：

- `users` 三列：`failed_attempts`（`INTEGER DEFAULT 0 NOT NULL`）、`locked_until`、`last_failed_at`
  （都走 `migrations.COLUMNS`，补列**必须带 DEFAULT**，理由与 `token_version` 那条一样）。
  配置项 `LOGIN_LOCKOUT_THRESHOLD=5` / `LOGIN_LOCKOUT_BASE_SECONDS=60` /
  `LOGIN_LOCKOUT_MAX_SECONDS=900` / `LOGIN_FAILURE_TTL_SECONDS=900` / `PASSWORD_MIN_LENGTH=10`。
- **退避**：第 n 次失败锁 `base · 2^(n−阈值)`，封顶 `max`，即 `5→60s / 6→120 / 7→240 / 8→480 / 9 起→900`。
  没到阈值时 `locked_until` 必须清成 `NULL`——留一个未来的时间会让「失败了几次」与「还剩多久」
  互相打架，页面上出现「失败 2 次，锁定 60 秒」这种自相矛盾的状态。
- **锁着时口令填对也是 429**，而且和限流那条**文案一字不差**（`登录尝试过于频繁，请在 N 秒后重试`
  + `Retry-After`）。两句一样是故意的：回显「你被锁了」等于告诉爆破方「这个用户名存在」。
  这条 429 里不含用户名、不含「锁」、不含「密码」——回归脚本专门断言了这三点。
- **静默期归零**：`last_failed_at` 距今超过 `LOGIN_FAILURE_TTL_SECONDS` 就从 0 重新数
  （否则一年前错过的 4 次会让人在今天第 1 次输错就被锁）；登录成功清零。库里可能是 naive datetime，
  比较时按 UTC 补时区再算。
- **解锁不等于踢下线**：`POST /admin/users/{id}/unlock` 清计数与锁定时间，**不碰口令、不碰
  `token_version`**——正锁着的账号解锁后不改口令就能立刻登录。「原先锁了多久」只在审计 detail 的
  `was_locked_for` 里（响应体不返回它，脚本第一版就是断言错这个才红的）。
- **口令强度不挂在 Pydantic 模型上**：`Credentials` 是登录与注册共用的，挂上去会把老用户的正常登录
  也一起判死（他们当年注册的口令本来就不满足新规则，而登录时**不该**再判一次强度）。
  规则走 `backend/passwords.py` 的 `problems(password, username)`，逐层判：精确弱口令 → 分词后
  命中弱口令词 → 弱口令子串（`password`/`admin`/用户名等）→ 键盘连打 → l33t 还原（`Passw0rd!2026`
  归一化后仍是 `password`）→ 长度，返回中文原因列表。
- **注册的顺序是「先查重名（409）再查强度（400）」**：反过来的话，种子脚本重注册同名演示账号会突然
  报「密码太弱」，让人以为是口令规则坏了，其实是名字被占了。弱口令注册**一个账号都不建**
  （审计 `user.register_rejected` 带 `problems`）。
- 审计：`user.login_failed`（detail 带次数，**不带口令**）、`user.login_locked`（两种原因：
  「连续失败达到阈值，账号锁定」与「账号已锁定（连续失败次数超阈值）」）、`user.unlock`、`user.register_rejected`。
- 前端：用户管理页新增「登录」列（`正常` / `失败 N 次` / `已锁定`）与「解锁」按钮（没被锁时禁用）；
  注册页把客户端规则从 6 位提到 10 位并说明弱口令（**服务端仍是权威**，前端只是省一次往返）。
- 真机验证：`scripts/verify_login_lockout.py`（19 项）用临时账号真连错、真被锁、真解锁，并核对
  静默期、弱口令不建号、以及「被锁的 429」与「限流的 429」一字不差；界面面在
  `scripts/ui_components_smoke.py` 里只核对渲染与禁用态（演示库没人被锁，解锁必须是灰的）。

### 慢接口告警（按路径耗时阈值）

按路径统计 P95 有个致命前提：**得有人去看**。值班的人不会盯管理页，出问题时只会在事后翻日志，
所以单次请求一旦超过阈值就主动写一条 WARNING（`backend/observability.py` 的
`logger = logging.getLogger("finance-api.slow")`），文案是
`慢接口告警 path=%s duration_ms=%s threshold_ms=%s 累计超阈值=%s 次`：

- **默认阈值 1000ms**，配置项 `SLOW_PATH_MS`；**按路径可覆盖**（`SLOW_PATH_OVERRIDES`，
  形如 `/api/v1/ai/rag/chat=15000,/api/v1/ai/agent/chat/stream=300000`）——把全局阈值调到
  「谁都不会超」等于把告警关掉，重接口（一次要调模型的、长连接的 SSE）反而需要单独放宽。
  解析器对坏条目（缺 `=`、值不是数字、值非正、路径为空）**逐条跳过而不是启动失败**：调优旋钮
  写错一个逗号不该让服务起不来，跳过的条目在启动日志里看得出来。
- **按路径限流**（`SLOW_PATH_LOG_SECONDS`，默认 60 秒）：同一个路径在窗口内的后续超阈值请求
  不逐条写日志，条数累计进下一次告警（`另有 N 次被限流未逐条上报`）。不限流的话一条几十秒的
  SSE 连接会把日志刷满，真正该看的告警被埋掉。
- **启动日志先说清阈值**：`慢接口告警阈值：默认 1000 ms；按路径覆盖 …；同路径告警间隔 60 秒` ——
  看日志的人得先知道「超过多少才算慢」。
- **管理概览**的「接口运行观测」卡片里新增「慢接口告警（单次超过阈值）」区块
  （`observability.snapshot()["slow_paths"]`）：路径、超阈值次数、阈值/最慢/最近耗时与时间戳；
  「最慢的接口」那段的等级颜色也改用同一套阈值判定（原先写死 500ms，与可真调的阈值不一致）。
- 真机验证：`scripts/verify_slow_paths.py`（16 项）发一次真模型问答（实测 16 秒）确认告警落到
  **日志文件**里，再立刻发第二次确认只记账不刷日志，并核对 `/health` 这类快请求不被记账。

### 管理员入口

权限模型曾经没有角色字段，而是沿用数据范围那条语义：**部门列表为空 = 不受部门限制 = 管理员**。
那个设计有个真口子——`PATCH /auth/me` 允许账号改自己的部门，于是**任何新注册账号只要把自己部门清空
（或干脆重发一个空数组）就成了管理员**，可读全库、可改别人账号、可撤销批次。⑳ 把它拆成两件事：

- **权限**：显式的 `User.is_admin`（BOOLEAN NOT NULL DEFAULT false，⑳ 新增列）。管理接口的 `admin_user`
  依赖只认它，非管理员拿到 **403（而不是空数据）**，避免「看不到」被误读成「没有」。
- **数据范围**：`User.departments`（JSONB 数组，权威值）+ 老的单值列 `User.department`（「第一个部门」的
  回落，列表为空时按它读）。空数组 = **未分配部门 = 什么都看不到**（空范围不是「不受限」）。

两条规则各有各的入口：管理员标识只能由**另一个管理员**在 `PATCH /admin/users/{id}` 上改（或运维脚本
`scripts/grant_admin.py <账号> [--revoke]`），数据范围同样只能由管理员分配。`PATCH /auth/me` 现在一律
**403** 并留 `user.department_self_denied` 审计；只是想换一次查看口径，用请求里的 `?department=` 参数。

迁移把老库按「原先不受部门限制 = 管理员」回填（`backend/migrations.py` 的 `BACKFILLS`：`departments`
为空且 `department` 为空的账号 `is_admin = true`），所以升级后原管理员不会突然进不去；全新部署则靠
`bootstrap_admin()`——**空库的第一个注册者**自动是管理员，表一旦非空就借不到第二次。
收尾两条守卫：**不能收回/删除最后一个管理员**（接口 400，`grant_admin.py --revoke` 同样拒绝），
免得出事后这个库再没有人能管。

侧边栏「管理员」组（`管理概览 / 用户管理 / 部门权限 / 预警规则 / 审计日志 / 导入批次 / 知识库管理`）放在**最下面**（在「系统」之后）：
它平时用不到，只有需要运维时才找。这一组只对**有管理员标识**的账号显示（`is_admin`，与数据范围无关：
被收窄到某个部门的管理员照样进得来，只是数据只有那一个部门），但地址栏可以被
手动输入，所以 `frontend/src/router/index.js` 的守卫对 `meta.admin` 路由会自己拉一次
`/auth/me` 再判 `is_admin`，不通过就跳回 `/dashboard?denied=admin`（看板顶部会解释原因）。
前端把这次查询收敛在 `frontend/src/api/session.js`：模块级 ref + 进行中的 Promise 缓存，
整站只请求一次，侧边栏和守卫读同一份。

导航现在有 6 组共 17 项，比窗口高度高，所以 `.sidebar` 加了 `overflow-y:auto`（`overscroll-behavior:contain`）：
内容放不下时侧边栏自己滚动，而不是把最后几项裁掉；同时把分组间距、项内边距和品牌区间距压紧了一档，
1440×900 下刚好完整显示，更矮的窗口滚动即可。

六个页面各自解决一个此前只能靠脚本做的事：

- **管理概览**：各表实时计数、`质量体检`（有明细但缺指标的期间 / 有指标但无明细的期间 / 部门为空的行 /
  三项金额全为 0 的行 / 没有批次号的行）、最近 5 个批次、`observability.snapshot()` 的累计请求与 P95，
  以及**按路径 P95 排出的最慢 5 个接口**（`snapshot()["paths"]`，每路径保留最近 200 次采样，
  最慢一条超过慢接口阈值就标红——等级颜色与告警用同一套阈值，早先写死的 500ms 与可真调的阈值不一致）——全局 P95 只能说明整体感受，路径 P95 才指出该去优化谁，排查时不必翻日志行。
  体检项只做便宜的计数，但能直接抓出「指标与明细对不上」这类真问题。
  规模数字里的「期间指标 / 预警」只数全公司口径行（`department IS NULL`），另用
  `metric_rows` / `alert_rows` 报出含部门行的总行数——一次 `calculate` 会给每个部门各写一行，
  不分口径地数会看着像涨了几十倍。
  同一张卡片里还有**慢接口告警**（`observability.snapshot()["slow_paths"]`）：单次请求超过阈值的那几条路径，
  带「超阈值 N 次 / 阈值 / 最慢 / 最近」——它是下面那条「P95 只是观测、阈值才是告警」的落地位置。
  另外还有一张 **Agent 用量卡**（`GET /admin/agent-usage?days=7`）：近 7 天的回答数 / tokens、
  按用户（未受限账号的部门列写「不受限」）与逐日明细，账单价与估算价分开报。
  它单独发一个请求、单独一套错误状态：**用量统计失败只让这张卡报错，整页概览照常显示**。
- **用户管理**：改部门立即改变该账号**财务数据与知识库**的可见范围；部门栏可以填**多个**部门
  （用「、」分隔，后端收 `departments` 数组），**留空＝未分配（看不到任何数据），不再等于升级为管理员**。
  每行的「权限」列是管理员徽章 + 一组按钮：`授予管理员` / `收回管理员` 只发 `{"is_admin": true|false}`
  一个字段，**部门一个字都不动**（后端按 `model_fields_set` 判断客户端到底发没发部门字段，早先的
  实现会把只发 `is_admin` 的请求当成「部门也是空的」、顺手把范围清掉）；`is_admin` 与 `unrestricted`
  两列各自独立，被收窄到某个部门的管理员那一行会同时写「管理员」与「看 N 个部门」。
  每行还有「会话」列（`未作废过` / `已作废 N 次`，读 `token_version`）
  与「强制下线」按钮（`POST /admin/users/{id}/logout`，把自己账号的会话版本 +1，见「会话版本号与强制下线」），
  自己那行禁用。
  **不允许通过管理接口改自己或删自己**（`400`）：改自己的部门会立刻把自己锁在管理入口之外（管理员标识
  也不行——自助扩权是 ⑳ 修掉的那个口子，请找另一个管理员）。删除账号只移除登录凭据，财务数据是按批次
  入库的，不受影响；**最后一个管理员删不得**（400）。列表支持按用户名模糊搜、按部门
  （含「未分配部门」这一项）筛选与翻页；按部门筛选用的是
  `(department == X) | departments.contains([X])`，否则「018+097」这种账号在按 018 查时会被漏掉。
- **部门权限**（`/admin/departments`）：`GET /admin/departments` 把「明细里的部门列 + 指标/预警的部门行 +
  被账号引用的部门」合并成一份部门清单，逐部门给出明细行数、收入/费用金额、期间区间、指标/预警行数、
  绑定账号数；页面上直接把部门填给某个账号（写的是 `PATCH /admin/users/{id}` 的 `departments`）。
  多部门账号会出现在**它每一个**绑定部门下，所以页面的账号表按 id 去重后再渲染——不去重会得到
  重复 `:key` 的表格行，同一个账号出现两次。
  部门是**从数据里长出来的**，没有「新建部门」——避免出现一个没有任何数据、只用于授权的空壳部门。
- **导入批次**：按 `import_batch` 分组显示行数、日期范围、涉及期间与导入时间。撤销时先删该批明细，
  再对每个受影响期间**重算**（还有别的批次的数据）或**清空**（没有剩余明细）指标与预警——
  否则库里会留下与明细对不上的幽灵报表。`data/raw/` 下的原始文件按只读约定不动。
  这等价于 `scripts/import_via_api.py --remove` 的语义，现在在界面上就能做。批次多了会分页。
- **预警规则**（`/admin/alert-rules`）：`GET /api/v1/admin/alert-rules` 列出每条规则的指标、比较符、
  阈值、级别、启用开关与更新时间，表格多一列「适用范围」（`默认`/`分档` 标签 + `scope_label`），
  行内改完点保存一律走 `PUT /api/v1/admin/alert-rules/id/{id}`（默认与分档同一端点，页面不必记用哪条 URL）。
  下方「新增分档规则」卡片按部门、按期间各选一个维度后 `POST /api/v1/admin/alert-rules`；
  分档规则多一个删除按钮（`DELETE /api/v1/admin/alert-rules/id/{id}`），默认规则的删除按钮**禁用**并给出原因。
  页面草稿按 **`rule.id`** 存：`alert_type` 不再唯一，按类型存会让「部门 018 的 0.95」和「全期间的 0.05」
  互相覆盖。`ensure_rules()` 会在首次访问时补齐默认两条（成本率 > 0.8、净利润 < 0），所以老库打开就有内容；
  阈值绝对值 ≥ 1e16 会被拒（422「阈值超出可表示范围」），级别只接受 `high|medium|low`。
  旧的 `PUT /api/v1/admin/alert-rules/{alert_type}` 仍然可用，但只匹配**默认规则**。
- **预警外发**（`/admin/alert-channels`）：上半页是通道表（类型、目标、级别门槛、部门、启用、最近结果与
  投递计数），行内可改、可点「测试」真发一条（`POST /admin/alert-channels/{id}/test`，**失败也回 200**，
  页面按 `ok` 字段把原因写回「最近结果」列）；下半页是投递台账，可按状态/级别/期间/通道筛，
  失败行有「重发」（`POST /admin/alert-deliveries/{id}/retry`，**人工重发把 `attempts` 归零**），
  顶部「补发通知」按钮对已有待处理预警重跑一遍投递计划（`POST /admin/alerts/notify`，可选期间）。
  密钥只在新增/修改时单向下发，列表与详情永远只回 `has_secret`；行内草稿里的密钥框**永远从空串开始**
  = 「这次不动它」，否则每次改个部门都会把密钥覆盖成界面上那几个星号。
- **知识库管理**：按来源聚合片段数、类型、向量模型与首段预览。删除按整份来源生效（管理员操作，
  不受部门限制）**并顺手清掉孤儿的落盘文档**（还有别的来源引用就保留，越界路径不碰，见「上传件的去留」）；
  `embedding_model` 为空或与当前模型不一致的片段本来就不参与检索，删除后可用
  `scripts/reindex_embeddings.py` 统一重算。列表可按来源模糊搜、按文档类型筛，并分页显示
  「命中几份来源 · 几条片段（全库共几条）」。

破坏性操作一律是「点一次 → 变成确认/取消」的两步式，不用浏览器原生 `confirm()`，也不需要后端会话状态。

### 报告与文件下载的坑

前端统一用 `frontend/src/api/download.js` 的 `saveBlob(blob, filename)` 保存文件：必须先把 `<a>`
挂到文档里再 `click()`，并且**不能立刻 `revokeObjectURL`**（下载是异步读取 blob 的，稍大的文件会
静默失败——页面提示照常出现，磁盘上却没有文件），所以延迟 60 秒再释放。
注意：在无头浏览器里无法验证「文件真的落盘」——实测连一个 10 字节的 `text/plain` blob 都不会写入
下载目录，属环境限制；因此 `/reports/pptx` 的浏览器侧验收只断言到「取到 blob 并显示已保存（38 KB）」。

## 业务时区（`BUSINESS_TIMEZONE`）

「今天」「本月」「近 7 天」这类**日历日**口径过去是隐式读服务器本机时区（`datetime.now()` 与
`datetime.combine(..., time.min).astimezone()`）：容器时区是 UTC、开发机是 UTC+8，同一份数据在两个环境里
会算出不同的「今天」——北京时间 10-09 凌晨 00:30，UTC 那边还是 10-08。

现在它是一条显式声明的配置：`BUSINESS_TIMEZONE`（IANA 名，默认 `Asia/Shanghai`，见 `backend/biztime.py`）。
启动时校验（`Settings.validate()` 里那条 `BUSINESS_TIMEZONE=... 不是合法的 IANA 时区名`），
非法名会出现在 `/system/status` 的 `config_problems` 里；Windows 上还需要 `tzdata` 包（venv 里已装）。

按它算的一共这几处，都是**日历日边界**，不是「把时间显示成哪个时区」：

| 位置 | 口径 |
| --- | --- |
| 看板「今天」（`backend/api_dashboard.py`） | `biztime.now()` 作为当日锚点；`updated_at` 带业务时区偏移 |
| 报告 `generated_at`（`backend/api_export.py`） | 带业务时区偏移；`/export/scope` 额外回报 `timezone`（如 `Asia/Shanghai (UTC+08:00)`），别让下游猜 |
| 审计日志的 `since`/`until`（`backend/api_admin.py` 的 `_audit_window`） | 页面上的日期 = **业务时区**的日历日，左闭右开；以前用 `.astimezone()` 读服务器本机时区，换台机器筛出来的条数就变 |
| 预警外发测试通知的 `period`（`backend/alerts_outbound.py`） | `biztime.month_of()`：UTC 看还停在上个月的月末，业务口径已经是新月份 |
| Agent 用量窗口（`backend/agent_sessions.py`） | 窗口起点是业务日 00:00、`by_day` 按业务日分组（SQL 里 `timezone('Asia/Shanghai', created_at)`），「近 7 天」含今天 |

有意**不**改的两处，写在这里免得下次被当 bug：

- **审计行内的时间戳保持 UTC**（`audit.serialize()` 里 `"at"` 是 `+00:00`，导出 CSV 的表头就是「时间(UTC)」）。
  逐行时刻是给人对数的，UTC 最不容易吵；审计页把它换算成**浏览器本机时间**并把列头写成「时间（本机）」。
  于是页面上同时存在两个口径：筛选边界按业务时区、行内时刻按看表人本机——两个都显式标出来，不让人猜。
- Agent/审计的**库里仍存 UTC**，只在查询与展示边界换算，避免出现「半套数据换了时区」。

运行态与验证：

- `GET /api/v1/system/status` 的 `app.business_timezone` 回报 `{name, label, offset, now, today}`；
  System 设置页「运行态」卡把它显示出来（业务日与当下时刻标了 `data-visual-mask`，否则截图基线每天都红）。
- 真机验证是 `scripts/verify_timezone.py`（25 项）：断言业务日与 UTC 日确实不同、`today` 等于脚本按 IANA
  规则独立算出的那一天、`updated_at`/`generated_at`/用量窗口都带 `+08:00`、**本次登录在「业务今天」的审计窗口里
  但不在「业务昨天」里**（这条最硬：那一刻按 UTC 算还停在昨天）、纽约夏令时那天只有 23 小时。
  它同时能抓「跑着的后端还是旧进程」：老进程里没有 `business_timezone` 字段，第一组检查就红。
- 单测：`tests/test_units.py`（`biztime` 的取值、naive 时间戳按 UTC 读、`day_bounds` 左闭右开与夏令时、
  `month_of` 跟业务日历、`label/describe` 说清用了哪个偏移、`usage_stats` 窗口是业务日）、
  `tests/test_config.py`（默认值与非法名）、`tests/test_api.py`（`/system/status`、看板 `updated_at`、
  `/export/scope`、审计日期窗口）。

> 一个坑：**同一 tzinfo 实例**的两个 aware datetime 相减走墙上时间（PEP 495），
> `day_bounds` 在夏令时那天直接相减仍是 24 小时；要看出 23 小时必须先 `astimezone(UTC)` 再减。
> 第一版断言就是这么写错的。

## 看板口径

- **不编造同比**：只有「上一期数据完整」时才给 `change`，否则 `change` 为 `null` 并在卡片上显示
  `note`（如「缺少 2025-01~2025-12 的完整数据，无法计算环比」）。看板顶部的黄色提示条同源。
- **6 个 KPI**：营业收入、成本金额、费用金额、收支差额（简化口径）、收支差额率、成本率；
  成本率带 `threshold_hint`（预警阈值 80.00%）。金额一律以**字符串**返回（`str(money(x))`），
  因为 float 在 5.8 万亿量级会丢分位、与源文件对不上；前端用 `Number()` 解析即可。
- **图表口径**：`chart_notes` 说明每张图的取数口径；费用饼图按**部门** Top5 拆分（源数据无成本科目，
  成本恒为 0，所以饼图没有成本扇区）；现金流图只有「现金收支（未分类）」，因为源文件没有银行流水时间轴。
- **期间语义**：`period=month|quarter|year` 以库里**最新一期**为锚点往前推（不是当前自然年月），
  所以导入历史数据后看板仍能显示对应期间；返回 `range` 与 `period_label` 说明实际范围。
- 部门 Top5 之和与「费用金额」KPI 存在几百万元差：适配脚本逐行四舍五入到分再入库，
  而校验脚本先把源文件整列相加再四舍五入，5.8 万亿量级上两者不可能逐分相等。
  正确的不变量是「Top5 降序且不超过全量费用」，回归脚本按这条断言。

## 部门数据权限

权限模型见「管理员入口」一节：**权限**看显式的 `User.is_admin`（能不能进管理端），**数据范围**看
`User.departments`（`None` = 不受限，有值 = 只看这些部门，**空数组 = 未分配部门 = 什么都看不到**）。
知识库片段一直按部门过滤，**财务数据现在也按部门过滤**，而且过滤落在数据层而不是查询层：

- 一次 `calculate()` 写**两套行**：全公司口径（`financial_metrics.department IS NULL`）与
  各部门口径（`department` 有值）。预警同理（`financial_alerts.department`）。
- 所有读这两张表的代码都必须过 `backend/scope.py` 的 `scope_metric` / `scope_alert`；
  明细查询过 `scope_transactions`。**漏一个就会把部门行与汇总行加总**——不报错，只是数字变大，
  所以这条约束写进了 `backend/models.py` 的字段注释里。
- 受影响并已改造的读路径：看板 `backend/main.py`、`/analysis/*`、月报与 PPT、Agent 财务证据、
  预测 `/forecast/financial`、`/system/status` 的期间与计数、管理概览与部门页。
- 受限账号的 KPI、图表、报告正文、Agent 证据全部按同一口径取数，页面上还有一条提示条
  （`frontend/src/components/DataScopeNotice.vue`）说明「看到的不是全公司口径」。
- 明细钻取按预警所属口径校验：拿别人的预警 id 会得到空列表 + 「该预警不在当前账号的数据范围内」。
- 部门为空的历史明细行不属于任何部门账号（它们只进全公司口径），这是刻意选择：
  公开数据里有 6 行没有部门，把它们塞给某个部门会让那个部门的口径凭空变大。

### 一个账号多个部门（并集口径）

`User.departments` 是权威值（JSONB 数组），含义从「一个部门」放宽成「一组部门」，实现上分三层：

- **读权限**：`backend/scope.py` 的 `Scope` 类型是 `tuple[str, ...] | None`（`None` = 不受限，
  **空元组 = 未分配部门 = 什么都看不到**）。`scope_metric` / `scope_alert` 用 `.department.in_(scope)`
  （单部门退化成 `==`，不受限时是 `.is_(None)`，空元组时 `in_(())` 恒假），`scope_transactions`
  用 `.in_(scope)`；`in_scope(scope, department)` 给预警这类逐行判断用（不受限恒真、空范围恒假）。
  RAG 侧同理：`filter_visible` 放行的条件是「片段没标部门 or 片段部门在元组里」，所以空范围账号
  只剩没标部门的公共资料。
- **同期多行怎么算**：`metric_totals(rows, period_order=...)` 按期间聚合——**单行期间沿用库里存的
  比率**（避免与已经展示过的数字末位跳动），**多行期间金额相加、比率用相加后的金额重算**
  （比率不能相加，实测多部门「收支差额率」-422189.52% 与差额÷收入完全一致）。
  看板、`/analysis/summary`、月报/Word/PPT、Agent 财务证据、预测走的是同一份实现，四个页面同一个数。
- **口径收窄**：`resolve_requested_scope(user, requested)` 只允许在自己绑定的部门里收窄；
  切到没绑定的部门返回 `(自己的范围, 文案)` → 接口 **403**「当前账号的数据范围是「部门 018、097」，
  不能查看「部门 122」」，不静默回落成别的数。不受限账号仍可切任意部门。
  `GET /analysis/departments` 的 `switchable` 是「这个账号能不能自己换口径」：不受限账号可以，
  **绑定多个部门的账号也可以**（下拉里只有它自己的部门，不选＝这些部门的并集）。
  `ScopeSwitcher.vue` 早先只认 `me.unrestricted`，多部门账号在看板上根本看不到这个下拉——
  而它恰恰是最需要在两个部门之间来回收窄的那个账号；下拉首项也按账号类型换文案
  （不受限 =「全公司（全部部门）」，多部门 =「我绑定的 N 个部门（合计）」），
  否则受限账号会以为自己不选就是全公司口径。
- **完整性判断**：`_totals_by_months` 要求「每月的行数 == 范围内部门数」（`expected = 1 if scope is None
  else len(scope)`；全公司一行、多部门一本账一行、空范围 0 行），某一部门缺一期就不算环比——用 distinct
  期间数会把缺数据当成完整，环比直接算错。
- 报告/PPT/导出的标题、副标题、文件名都带多部门口径：`scope_label`（「部门 018、097 等 N 个部门」）、
  `scope_slug`（文件名用的 `018-097`，空范围是 `none`）、`scope_text`（审计与封面用的 `018、097`，
  空范围是「未分配部门」而不是空串——审计详情里的 `scope` 空着就分不清「全公司」和「压根没范围」）、
  `row_label`（数据行级的「部门 097」）。**别把字符串传给收元组的 `scope_label`**——早先有一处
  这么写，结果按字符长度渲染出了「部门 097 等 3 个部门」。

这套东西管的是**数据可见范围**，不是安全边界；能不能进管理端另看 `User.is_admin`（见「管理员入口」），
两者的关系是「权限决定能进哪个页面、范围决定能读哪几行」，互相推不出来。

把数据真正留在库里供前端浏览：

```powershell
# 后端需先启动：.\.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
.\.venv\Scripts\python.exe scripts\import_via_api.py --username demo_analyst --password demo_password_2026
```

## 季度/年度落库

季/年合计不现算，而是落进独立表 `financial_rollups`（`backend/rollups.py`）。为什么不给
`financial_metrics` 加一列「粒度」：那张表被十几个读点按「一行 = 一个月」使用（预测趋势线按
`period` 排序读全部行、导出按 `grain` 换表、期间下拉来自 `distinct(period)`），混入季/年行以后
「这一行是不是月行」就成了每个读点都要记得判断的隐式约定，漏一处就出假数（把 `2026` 当成第 13 个月
画进趋势线）。单独一张表把语义固定下来，读的人不需要猜。

- **期间口径**在 `backend/periods.py`（纯函数）：`grain_of / quarter_of / year_of / months_of /
  containing_periods / previous_period / sort_key / period_label`。`previous_period` 只给**同粒度**的
  上一期（服务环比），跨年回退到上一年的 12 月/Q4。
- **写**：`rollup(db, grains=("quarter", "year"), periods=None)` 对每个目标期间**先删后建**，按
  `department` 分组（全公司行 `NULL` 与各部门行各写各的，全公司行不把部门金额重复计入），
  金额相加、比率用相加后的金额重算。`refresh_for_months(db, months)` 刷新这些月所在的季与年。
- **自动刷新**：`POST /analysis/calculate?period=YYYY-MM` 重算某个月时顺手刷新它所在的季/年；
  `DELETE /admin/batches/{id}` 撤销批次后同样刷新（否则看板上会挂着一个凭空的季度合计）。
  这里有个**真实的坑**：`SessionLocal` 是 `autoflush=False`，新算出的 `FinancialMetric` 行在
  `db.add()` 之后还只在内存里，落库那边读库会漏掉刚算的这一个月（季度只有 2 个月、金额少一截，
  而且不报错）。所以 `calculate()` 在调 `refresh_for_months` 之前显式 `db.flush()`。
- **读**：`GET /analysis/summary?period=` 月口径读 `financial_metrics`，季/年读落库行，
  落库没覆盖时按同一算法现算兜底。响应新增 `grain / grain_label / period_label / source /
  months / covered_months / missing_months / complete`，`source ∈ {month, stored, computed, empty}`
  如实说明数字是哪来的——页面才不会把现算的数当成落库的数。
- **缺月要说出来**：`complete=false` 且 `missing_months` 点名缺哪几个月，`months` 是**真的被求和的月份**。
  演示数据只有 2026-04~10，所以 2026 年度缺 5 个月、`complete=false`；假装它是全年才是真正的错。
- **季/年不产生预警**：预警按月算。同一件事按季、按年再报一次只会让预警列表变成噪音。
- **看板不动**：`/dashboard` 仍然按明细算（KPI 对所选月份的明细求和、逐月收入曲线、部门 Top5）——
  图表本来就需要逐月数据，改成读季/年行反而拿不到曲线。落库只喂 `summary` 与管理页。
- **管理端**：`GET /admin/rollups?grain=quarter|year` 列出落库行 + 覆盖清单；`POST /admin/rollups/rebuild`
  （body `{"grains": [...], "periods": [...]}`）用于老库回填与修数，写审计 `rollups.rebuild`。
  `grains` 只能是 `quarter`/`year`，把月口径塞进 `periods` 会 **422** 而不是静默什么都不算。
  `/admin/overview` 返回 `rollups` 覆盖块，「季度/年度落库」卡片把它渲染成计数 + 缺月清单 + 重算按钮，
  外加「导出季度」/「导出年度」两个按钮（⑲，屏幕上就是季/年合计，导出也必须能按季/年导，
  「是否完整」列会把 `complete=False` 标出来）。
- **缺月清单必须说清是哪一行缺月**：同一个季度里全公司行齐全、某个部门只落了其中一个月是常事。
  清单项带 `department` / `department_label`，全公司行排在同期的部门行前面，页面上显示
  「2026 年度」或「2026 年度 · 部门 097」。这是真机冒烟抓到的：早先只按 `(粒度, 期间)` 去重，
  页面上出现过「2026 年度缺 11 个月」，而全公司那一行其实只缺 5 个月。
- **导出也读这张表（⑲）**：`/export/metrics?grain=quarter|year` 直接取落库行（与 `/admin/rollups` 同一份），
  不再拿月行现算，并把「包含月份」与「是否完整」两列一起交给下游——导出表里出现 `2026` 年度合计时，
  旁边就写着它只含 7 个月。
- 真机验证 `scripts/verify_rollups.py`（24 项）：重算后季度 `source=stored` 且金额等于三个月的月指标
  之和、比率按合计重算、年度 `complete=false` 且点名 5 个月、受限账号只读到本部门落库行、读管理端 403、
  三个 422、重算某个月后季度数字幂等不变、没落库的期间现算兜底且读路径不写库、审计留痕。
  导出侧另有 `scripts/verify_exports.py`（36 项，⑲）断言同一期间从两个入口读到的金额逐值相等。

## 明细更正与冲销

数据导入之后总会有「这一行录错了」和「这笔不该算」两种需求，它们要的语义完全不同，所以分成两条路径：

- **更正**（`PATCH /financial/transactions/{id}`）就地改字段：`account_code` / `account_name` / `department` /
  `description` / `transaction_date` / 五个金额列。金额一律以**字符串**收，用导入同一套规则解析
  （支持千分位与中文逗号、`|v| < 1e16`），前端不做金额运算——否则「前端认为合法、后端拒绝」的双份实现
  迟早分家。改一次 `corrections` 计数 +1，旧值写进审计 `transaction.update` 的 `detail.before`。
- **冲销**（`POST /financial/transactions/{id}/reverse`）**不删原行**：新增一条金额取负的冲销行
  （`reversal_of_id` 指向原行、`description` 前缀「冲销：」），原行只标记 `reversed_at` / `reversed_by`。
  删行会让「冲销前后的明细条数」与原始凭证对不上，也解释不了这笔钱去哪了；负数行参与合计、数学上自然
  抵消，指标仍然等于真实账。冲销行的 `import_batch` **沿用原批次**——批次号是文件内容哈希，造假批次会让
  「撤销批次」只收回一半的行。
- **改完必须重算**：更正会重算 `{旧期间, 新期间}`，冲销重算该期间；某期已经没有剩余明细时连同
  `FinancialMetric` / `FinancialAlert` / 季年落库一起清掉（`recalculated` / `cleared` 在响应里如实返回，
  页面上渲进提示）。`backend/transactions.py::recalculate_periods` 与「撤销导入批次」共用同一段逻辑，
  避免两套重算慢慢长歪。
- **坑：`SessionLocal` 是 `autoflush=False`**。数「这一期还剩几行」之前必须先 `db.flush()`，否则刚 `add` /
  刚改的字段还在内存里，会把有数据的一期当成空的清掉；`correct()` 里的 `old_period` 也必须在改字段**之前**
  读出来，否则改日期后新旧期间相同，腾空的那个期间永远不会被清理（这两个都是用例当场抓出来的）。
- **权限与口径**：`department` 为空的行属于「全公司口径」，只有不受限账号能改；受限账号读列表时只看到
  自己部门（`scope_transactions`），拿别人的行 id 去改是 **403**（`该明细不在当前账号的数据范围内`）。
  `GET /financial/transactions?department=X` 里的 `department` 是**口径切换**，越界同样 403。
- **这一页不做删除**：要整批收回请到「导入批次」撤销批次（它会连指标、预警、季年落库一起重算）。
- **导出要带着解释走（⑲）**：明细导出的最后三列就是上面这套语义——`冲销状态`（空 / `已冲销` / `冲销行`）、
  `原始行 id` + `冲销时间` + `冲销操作人`、`更正次数`。`冲销时间`/`冲销操作人` 只写在**被冲销的原行**上
  （记的是冲销那一刻，不是这行的交易日期），冲销行靠 `原始行 id` 指回原行。导出的负数行与页面上的
  净额一致，收下 CSV 的人不需要再去问「这 -300 是什么」。
- 真机验证 `scripts/verify_transactions.py`（45 项）与界面冒烟 `scripts/ui_transactions_smoke.py`（20 项，
  在真浏览器里点「筛选 → 更正 → 冲销」并核对表格变化）；导出的三列标记由 `scripts/verify_exports.py`（36 项）
  逐格核对。

## 并发重算互斥（期间锁）

重算是**读改写**：读一段时间窗里的明细 → 删掉旧指标与预警 → 按明细重写一行。两个请求同时重算同一期时，
彼此都读不到对方还没提交的明细，于是各自写出一份少算了对方数据的指标——**没有任何报错**，也不会有人
再自动重算这一期，看板上的数字从此一直是错的（「丢更新」里最难受的一种：不炸、不报、只是错）。所以
`backend/locks.py` 用 Postgres 的**事务级 advisory lock** 把「同一期间的重算」串起来：锁随事务结束自动
释放（提交、回滚、连接断掉都释放，不需要谁去清理）。

- **锁的对象是「期间」，不是行也不是表**：明细行本身还在被插入、删除、冲销，行锁锁不住「这一期的明细
  集合」这个逻辑对象；锁整张表又会让不相关的期间互相排队。key 由期间名哈希算出（`lock_key()`，命名空间
  `finance:recalc`，落在有符号 64 位内），**跨进程、跨重启**指的都是同一把锁。
- **月会连它的季与年一起锁**：`period_lock_set(["2026-05"])` → `["2026", "2026-05", "2026-Q2"]`。重算一个月
  要顺手刷新它所在的季/年落库行（见「季度/年度落库」），那张表也是这个临界区的一部分，否则两个月份的
  并发重算会各自覆盖季度合计。**代价**：同一年下的不同月份会共享年锁、互相排队——这是刻意的取舍，宁可
  偶尔多等一会儿，也不要一次少算。
- **补全收在加锁函数内部**：`lock_periods(db, periods)` 自己先过一遍 `period_lock_set()` 再排序加锁，所以
  调用方直接传月名也不会漏锁。这一点是真机脚本第一版抓出来的：那时 `lock_periods()` 只锁传入的名字，
  脚本里顺手传了原始月名，「季/年没人保护」当场露馅。所有写路径都走它，而且**多期调用方一次把全集传
  进来**——两个多期请求各持一半、又都在等对方剩下那半，就是 Postgres 的 deadlock；加锁顺序按期间名排序，
  与传入顺序无关（顺序一致比顺序本身更重要）。
- **接线点**（每处都写着「为什么必须在这里锁」的注释）：`calculate()`（`POST /analysis/calculate` 与导入里的
  逐期重算；锁在最前、`db.flush()` 之前）、`rollups.rollup()`（`/admin/rollups/rebuild` 与月重算后的刷新）、
  `recalculate_periods()`（撤销批次 / 更正 / 冲销共用的那一段）、`import_excel()`（插行之前锁该文件的全部
  月份）、`revert_batch()`（删行之前）、`correct()` / `reverse()`（**改行之前**；`correct()` 把新旧期间一起锁，
  分两次锁会与「反向搬运」的另一个请求互等成 deadlock）。
- **导入还有第二把锁，不是一回事**：`api_financial.py` 里那个以**文件内容哈希**为 key 的
  `pg_advisory_xact_lock` 管的是「同一份文件并发上传时别双双通过查重」（否则两份都会插进去）。期间锁管
  「别把某一期的指标算错」。两把锁的 key 加了命名空间前缀，互不干扰，所以**不能把文件哈希那把删掉**。
- 回归：`tests/test_units.py` 三个单测（key 稳定且不撞号、补全含季与年、加锁顺序与去重且只传月名也会补全）
  + `tests/test_api.py` 四个接口用例（用 `sqlalchemy.event` 抓接口真发出的 SQL，断言「先加锁、后删/写」
  且锁集含季与年，导入那例还断言多出来的 key 正好是文件哈希锁）。真机冒烟 `scripts/verify_recalc_lock.py`
  （18 项），关键数字：无竞争重算 32 ms → 有人持锁 2031 ms（持锁 2000 ms），导入与单行更正同样被挡，
  两个并发重算都 200 且指标仍等于明细之和，跑完 `pg_locks` 里 0 条残留。

## 检索与 Agent 口径

- 检索阈值由 `RAG_MAX_DISTANCE` 控制（默认 `0.8`）。本地小模型对相关与不相关片段的余弦距离
  存在重叠（实测相关片段 0.33~0.57、不相关 0.52~0.99），单靠阈值无法干净切分，因此先按
  `RAG_CANDIDATE_K`（默认 20）宽松多召回，再由 `backend/rag/rerank.py` 压缩到 top_k。
  `RERANK_MODEL` 为空时直接沿用余弦序，行为与未引入重排时一致。
- Agent 回答前会把**已导入的财务数据**整理成确定性证据（`backend/rag/financial_evidence.py`：
  期间定位、汇总表、按部门/科目聚合、当期预警），与知识库检索片段一起作为上下文，
  避免「账已导入但助手看不见」。`done` 事件会返回 `has_financial_data` 与 `matched_period`。
- 金额在证据里统一按元输出、不带千分位，便于模型逐字引用而不改写。
- 配置键名兼容：`MODEL_API_KEY`/`MODEL_BASE_URL` 为空时回退到 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`
  （见 `Settings.effective_api_key`）。不要改用 `validation_alias` —— 实测在
  pydantic-settings 2.15 下会读不到 `.env`，静默变成空字符串。
- 启动时 `sanitize_proxy_env()` 会剔除 `NO_PROXY` 里 `[::1]` 这种 httpx 无法解析的条目，
  否则 `openai` 客户端一构造就抛 `httpx.InvalidURL: Invalid port: ':1]'`，被误报成「模型不可用」。


