# 智能财务分析平台

基于 Python、确定性财务计算、大模型与向量检索的智能财务分析 MVP。项目目标是打通“原始数据 → 清洗加工 → 指标分析 → 可视化报告 → 自然语言问答”的最小闭环。

## 目录结构

```text
智能财务分析平台/
├── backend/              # FastAPI 应用：按域拆成 api_*.py（看板在 api_dashboard.py，不再内联在 main.py）
├── frontend/             # Vue 3 + Vite 界面；纯函数在 src/utils/，`npm test` 零依赖单测
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

`scripts/` 下四个验证脚本都是**真 PostgreSQL + 真 ASGI 接口**，跑完自行回滚或清理：

| 脚本 | 覆盖范围 |
| --- | --- |
| `verify_import_flow.py` | 注册/登录、模板、CSV+XLSX 预览与导入、Decimal 四舍五入、重复导入 409、非法行拒绝、预警、看板 |
| `verify_embedding_model.py` | 本地 Embedding 模型可加载、维度 384、两条中文检索样例 |
| `verify_crawled_import.py` | 爬取文件全链路 122 项断言（见上），含看板新字段（环比 reason、KPI 标签集合、图表口径说明）与报告导出（标题/小节/金额格式/401） |
| `verify_financial_agent.py` | Agent 证据层（财务数据 + 知识库 + 重排）、`/rag/*` 鉴权、上传去重、上传落盘目录、真实 SSE 端到端 28 项断言 |

验证脚本会把自己的上传件写到临时子目录并在结束时删除，不会污染项目的 `data/uploads/`。

还有三个不查数据、只查界面的脚本：

| 脚本 | 覆盖范围 |
| --- | --- |
| `check_pages.py` | 16 个页面 × 1440/768/375 三档，注入登录态后逐页断言 `scrollWidth == clientWidth`（无横向溢出）并整页截图到 `output/charts/` |
| `ui_scope_smoke.py` | 在真浏览器里点「查看口径」下拉与导出按钮：下拉选项、切口径后 KPI 与提示条变化、导出文件名/行数。**只有它能发现「控件在但点了没反应」** —— 写它时当场抓到 `ScopeSwitcher` 的 `onMounted` 竞态 |
| `ui_alert_review_smoke.py` | 在真浏览器里点预警的「确认异常 / 备注 / 退回」：断言状态徽标与顶部「待处理」计数跟着变（按**相对变化**断言，不写死条数）。`ElMessageBox` 的输入框这类只在点击后才存在的 DOM，只有真点一遍才看得到 |

三个脚本都要求后端 8000 与前端 dev server 5173 在跑（`check_pages.py` 支持 `--sizes/--pages/--out-dir/--viewport-only`）。

### 一键自检

本目录是 git 仓库（`main` 分支），但**没有 CI 服务**可挂，所以「CI」落成一个谁都能在本机跑的入口：

```powershell
# 编码/语法体检 + 后端 pytest + 前端 node --test
.\.venv\Scripts\python.exe scripts\check_all.py
# 只做编码与语法体检（几秒钟，适合改完文件先自查）
.\.venv\Scripts\python.exe scripts\check_all.py --quick
# 再加 16 页 × 3 档的真机页面扫描（要求前后端已启动）
.\.venv\Scripts\python.exe scripts\check_all.py --with-pages
```

第一步是**源文件编码体检**（查 UTF-8 BOM、非法 UTF-8 字节、替换字符 U+FFFD），因为这里真出过事故：
用 PowerShell 的 `Get-Content` / `Set-Content -Encoding utf8` 往返源文件时，它按系统 ANSI 代码页
（本机 cp936）重写内容，结果文件开头多出 BOM、正文变成 mojibake，python 直接报
`SyntaxError: invalid non-printable character U+FEFF`。**改源文件一律用编辑器/工具链的读写，不要用
PowerShell 文本 cmdlet 往返**；`--quick` 就是给这条纪律准备的哨兵。

`npm run build` 在本机沙箱会失败（见下），所以自检不含构建步骤，界面验收走 `--with-pages`。

### 版本库与提交前钩子

仓库是用 `scripts/init_git.py` 初始化的（可重复执行）：

```powershell
.\.venv\Scripts\python.exe scripts\init_git.py   # git init -b main + 设 core.hooksPath + 体检打印
```

它一次性把四件事定下来，都是有原因的：

- **`core.hooksPath = .githooks`**：钩子放在 `.git/hooks/` 里不进版本库，换台机器就静默消失；
  放进 `.githooks/` 才会随仓库走。克隆下来只需再跑一次 `git config core.hooksPath .githooks`。
- **钩子默认只跑 `--quick`**（编码 + 语法，几秒）：拦的正是「源文件被写成 mojibake/BOM」与语法错误
  这类进库就难查的问题。`PRECOMMIT_FULL=1 git commit …` 会连后端 118 个用例与前端 15 个用例一起跑。
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

结构变更分三类，各有各的落点，**都不需要 `alembic upgrade head`**：

| 变更 | 落点 | 何时生效 |
| --- | --- | --- |
| 加表 | `backend/models.py` 里的模型 | 应用启动时 `create_all()` 建缺失的表 |
| 加列 | `backend/migrations.py` 的 `COLUMNS` 加一行 `(表, 列, DDL)` | 启动时按 `information_schema` 查缺再 `ALTER TABLE`，幂等 |
| 改列类型 / 加索引 / 装 `vector` 扩展 | `scripts/init_pgvector_db.py` 的 `COLUMN_TYPE_MIGRATIONS` | 手工跑一次该脚本 |

为什么不上 Alembic：这个项目的部署形态是「一个进程 + 一个库」，`ensure_schema()` 在启动时自动补齐；
多一套「发版前记得跑 `alembic upgrade head`」的运维动作，反而多一个会忘的环节。代价是**加列清单必须手工维护**，
而忘了写 `COLUMNS` 的表现是接口 500（`UndefinedColumn: column financial_alerts.review_status does not exist`），
很容易被当成业务代码写错——项目为此留了两道闸门：

- `missing_columns()` 在启动时对照模型与真库，把缺失的列写进 `ERROR` 日志（不拦启动）；
- 单元测试 `test_schema_matches_models` 断言 `missing_columns() == []`，漏一行就让回归红。

> `create_all()` 只建缺失的**表**，不会给已存在的表补**列**：这是上面这些坑的总根源。
> 新加一列表看起来跑得通，是因为本地库往往是新库；老库升级时才会炸。

### 界面核对（三态与响应式）

`npm run build` 在本机沙箱会因 esbuild 清理临时文件被拒而失败（`[vite:esbuild-transpile] remove …: Access is denied.`），
属于环境限制，改用 dev server 转译 + 无头浏览器核对：

```powershell
# 16 个页面 × 1440/768/375 三档：打印 scrollWidth/clientWidth 并列出超宽元素，同时整页截图
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

已核对结果：16 页 × 3 档共 48 组全部 `scrollWidth == clientWidth`，无横向溢出（`.table-wrap` 内表格
属预期横向滚动，脚本已排除）；截图见 `output/charts/check_*.png`。首轮扫描抓到的唯一一处真实溢出是
`/financial/import @375`（`scrollWidth=429`，`.upload-box` 里的「确认导入」按钮被挤出视口），
已在 `frontend/src/style.css` 给 `.upload-box` 加 `flex-wrap:wrap` 并让 `<input type=file>` 可收缩后归零。
管理员页面的表格与列表同样在三档下核对过：宽表用 `.table-wrap` 横滚，知识库、体检项与部门列表用
列表卡片，手机上不会只剩第一列。

### 单元测试

```powershell
$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m pytest
```

`tests/` 下三个文件共 **121** 项：`test_units.py`（导入校验/分块/编码/部门过滤/环比与 KPI 计算/按路径耗时采样）、
`test_config.py`（占位密钥与 CORS 校验、启动 strict 语义）、`test_api.py`（受保护接口匿名 401、
真实 ASGI 走完导入→重算→预警→看板→报告→RAG 去重→SSE 流式→PPT 大纲与下载→系统状态脱敏→
管理员入口鉴权与批次撤销重算→**部门数据隔离**：受限账号只读到本部门口径、拿别人的预警 id 也钻取不到明细、
管理员看到的一直是全公司行→预警处理闭环（状态继承、误报必填原因、跨口径 403、规则可编辑且对重算生效）→
**审计日志**：注册/登录/登录失败留痕且不含密码、七类写操作各一条、按动作与操作人过滤翻页、
`_scrub` 脱敏单测、审计行没有改/删入口、按 IP 前缀与日期范围筛选（本地日历日、含首尾）、
导出 CSV 带 BOM 且筛选在导出前生效、导出本身留一条 `audit.export`、清理默认只预览、
真删只动过期行并留 `audit.purge`、`keep_days=0` 422→**分页与筛选**：`limit` 截断时 `total` 仍是筛选后的总行数、
`offset` 真的换页、部门/是否受限与文档类型筛选、`status` 在服务端过滤、越界参数 422）→
**后台任务**：提交后立刻 202 且明细还在（提交 ≠ 执行）、执行成功才删明细并重算、
审计里带任务 id、不存在的批次 404 且不留任务记录、未注册的任务类型标记失败、
重启收尸把 queued/running 标成失败、非管理员 403→**结构漂移**：模型里的列都在真库里
（漏写 `migrations.COLUMNS` 时点名，而不是等接口 500）→**按路径耗时**：`record_request` 后
`snapshot()["paths"]` 给出每条的 count/p95/max，且 500 与 200 分开计数→**出口配额**：
导出打满返回 429 + `Retry-After`、三个导出接口共用一本账（换路径绕不过）、
问答是另一本账（导出打满仍能问答）、`/rag/search`+`/rag/answer`+`/ai/*` 共用问答配额、
配额表对未登记名字显式报错且 `reset` 后回到配置默认值）。
异步用例用 **anyio** 插件（`pytestmark = pytest.mark.anyio`，用例 id 显示 `[asyncio]`），
不依赖未安装的 pytest-asyncio；数据库不可用时自动 `pytest.skip` 而不是失败。
测试自身在外层事务里跑，结束整体回滚，不会污染库里数据。

PPT 的下载用例会真的用 `python-pptx` 打开返回的字节流，断言存在**原生图表对象**（不是截图），
并断言「口径与限制」页随文档一起下发——否则「毛利率 100.00%」会被读成经营结论。

### 前端单元测试

```powershell
cd frontend; npm test        # 等价于 node --test tests/*.test.mjs
```

15 项，跑在 Node 自带的测试运行器上（**不引入 vitest / jsdom**，本机 Node v24 直接用）：

- `frontend/tests/format.test.mjs`：`src/utils/format.js` 里的表格/时长/期间格式化。
  这些函数原本写在 `AdminBatches.vue` 的 `<script setup>` 里，`.vue` 需要编译器才能 import，
  所以抽成了普通 ES 模块——**能被 node 直接测的纯函数就别留在组件里**。
  时间断言一律用不带时区的本地时间字符串，任何时区的机器结果一致。
- `frontend/tests/markdown.test.mjs`：`src/utils/markdown.js` 的安全与结构。
  最要紧的一条是 `renderMarkdown('<script>alert(1)</script>')` 的输出里**不含 `<script>`**——
  报告页与 Agent 回答都用 `v-html` 渲染它，一旦「先解析后转义」的顺序被改反，模型输出就成了可执行 HTML。

`npm run build` 在本机沙箱会因 esbuild 清理临时文件被拒而失败
（`[vite:esbuild-transpile] remove …: Access is denied.`），所以界面验收不靠构建产物，
而是 dev server 转译 + 无头浏览器（见上）。

## 页面

| 路由 | 页面 | 数据来源 |
| --- | --- | --- |
| `/dashboard` | 财务看板概览 | `GET /dashboard?period=month/quarter/year` |
| `/analysis/revenue`、`/analysis/cost`、`/analysis/profit`、`/analysis/summary` | 收入 / 成本费用 / 利润 / 财务汇总 | `GET /analysis/summary?period=YYYY-MM` |
| `/analysis/alerts` | 预警监控（期间/状态筛选、各状态计数、确认/误报/已处理/备注、服务端分页） | `GET /analysis/alerts/all`、`GET /analysis/alerts/stats`、`PATCH /analysis/alerts/{id}` |
| `/alerts/:id` | 预警明细（最多 200 行） | `GET /analysis/alerts/{id}/transactions` |
| `/financial/import` | 数据导入（两步：预览校验 → 确认导入） | 财务导入接口 |
| `/reports` | 报告导出（Word `.docx` + Markdown 月报，可复制 / 打印导出 PDF / 下载 `.md`） | `GET /reports/docx`、`GET /reports/monthly?period=YYYY-MM` |
| `/reports/pptx` | PPT 报告（先看逐页大纲，再下载 `.pptx`） | `GET /reports/pptx/outline`、`GET /reports/pptx` |
| `/settings` | System 设置（我的部门 + 运行态与配置体检） | `GET /system/status`、`PATCH /auth/me` |
| `/agent` | 财务分析 Agent 工作台（示例问题 + 流式问答 + 依据） | `POST /ai/agent/chat/stream`（SSE） |
| `/admin` | 管理概览（数据规模、完整性体检、最近批次、接口观测） | `GET /admin/overview` |
| `/admin/users` | 用户管理（按用户名/部门/是否受限筛选、分页、改部门、删除） | `GET/PATCH/DELETE /admin/users[/{id}]` |
| `/admin/batches` | 导入批次（分页、查看、**异步**撤销并重算受影响期间 + 最近后台任务） | `GET /admin/batches`、`DELETE /admin/batches/{batch}`、`POST /admin/jobs/batch-revert`、`GET /admin/jobs` |
| `/admin/knowledge` | 知识库管理（按来源模糊搜/文档类型筛选、分页、整份删除） | `GET/DELETE /admin/knowledge` |
| `/admin/departments` | 部门权限（账号绑部门、各部门数据规模） | `GET /admin/departments`、`PATCH /admin/users/{id}` |
| `/admin/alert-rules` | 预警规则（改阈值/级别/开关/描述） | `GET /admin/alert-rules`、`PUT /admin/alert-rules/{alert_type}` |
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
以及**目录可写性**（真写一个探针文件再删，而不是只看路径是否存在）。密钥与口令只报告「有没有配」，
数据库 URL 里的口令会打码成 `user:***@host`；`config_problems` 复用启动时的 `Settings.validate()`。

「我的部门」走 `PATCH /api/v1/auth/me`：留空表示不受限制（财务管理员），填写后知识库检索只返回
「本部门 + 未标注部门」的片段。部门**只认服务端记录**，不采信请求头自述（避免伪造提权）。

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
- 预警监控页顶部有各状态计数（`GET /api/v1/analysis/alerts/stats`）与状态筛选；统计**不跟着筛选项走**，
  否则一筛「待处理」就再也看不到自己在处理什么。

### 审计日志

撤销导入批次、删账号、删知识库、改预警规则、处理预警这些动作以前**只改数据不留痕**：
出问题只能翻 uvicorn 日志，而它重启即丢。现在每次写操作都落一条 `audit_logs`（`backend/audit.py`）：

- 记录的是**结构化事实**：时间、操作人、操作人当时的部门、动作（`user.delete` 这类机器可读码 +
  中文标签）、对象类型与 id、来源 IP、以及一小袋 detail（如改规则的前后阈值、撤销批次删了多少行）。
  **不记密码、令牌、文件内容与明细金额**：`_scrub()` 会把键名含 `password/token/secret/authorization/api_key/hash`
  的字段整条丢掉（任意嵌套深度都丢）、字符串截断到 200 字、列表只留前 50 项。
- 17 个动作在 `ACTIONS` 里登记；写一条未登记的动作不会失败，但会打 warning——避免埋点时漏改这里就静默少记。
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
- **重启收尸**：进程重启时 `jobs.reap_stale_jobs()` 把遗留的 `queued`/`running` 一律标成
  `failed`（`error = 进程重启，任务未完成`）——否则界面上会永远挂着一个「运行中」。
  代价说明写进了代码 docstring：**这是单进程内存队列，多副本部署要换成外部队列**。
- **提交不等于执行**：用例专门断言「POST 之后明细还在」，再手工调 `jobs.run_job` 才断言数据被删，
  避免把「入队成功」当成「撤销完成」。
- **撤销逻辑只有一份**：同步的 `DELETE /admin/batches/{batch}` 与后台任务都调
  `backend/batch_ops.revert_batch()`——删除、重算、审计写在同一个事务里，撤销到一半不会留下
  「明细没了、指标还在」的幽灵报表。
- 前端 `/admin/batches` 提交后按 800ms 轮询任务状态，撤销中的批次按钮变成「撤销中…」并禁止重复提交；
  页面下方列出最近 5 条任务（类型/状态/提交人/耗时/结果）。

### 安全收口

- **占位密钥自动替换**：`AUTH_SECRET_KEY` 为空或仍是 `change-this-secret-in-env` 时，进程启动会用
  `secrets.token_urlsafe(48)` 生成并**就地写回 `.env`**（保留其它行、不重复追加键）。占位密钥意味着
  任何人都能自己签一个合法 JWT。副作用：密钥一变旧 token 全失效，需要重新登录。
- **登录限流**：滑动窗口（默认 10 次 / 300 秒，按「用户名 + 来源 IP」分桶），超限 429 + `Retry-After`；
  被拒的尝试也计数，登录成功后清零。**边界**：这是 `backend/ratelimit.py` 里的**单进程内存实现**，
  没有 Redis，所以计数不跨进程/副本，进程重启即清零；要横向扩容得换成集中式计数（与后台任务同一个约束，
  见上面的「后台任务」一节）。
- **出口配额（导出 / 问答）**：限流不止登录，另外两组「一次请求就很贵」的出口各自独立计数：
  `export`（明细/指标/预警导出，默认 **30 次/分钟**）与 `ai`（RAG 检索与问答、知识库写入，默认 **60 次/分钟**），
  按**账号**分桶，超限同样是 429 + `Retry-After`。`EXPORT_LIMIT = 20000` 只限制**单次**导出行数，
  拦不住「脚本每分钟导 100 次」；问答每次都要调模型，是按量计费的东西，必须有配额。
  三组配额（`login` / `export` / `ai`）互不影响——登录被限流不该影响看数据，导出打满也不该让人问不出问题；
  配置项 `EXPORT_MAX_PER_MINUTE` / `AI_MAX_PER_MINUTE`（默认 30 / 60，回归脚本一次要连问 28 个问题，
  所以默认值给得比「防脚本」的理论下限宽松，避免误伤自己）。**429 不写审计**：一次被拒的请求写一条审计，
  等于给攻击者一个「用便宜请求写满审计表」的放大器；观测性里已按 `429:路径` 计数，管理概览能看到。
- **`.xls` 友好失败**：没装 `xlrd` 时返回 400 并说明两条出路（装 xlrd 或另存为 .xlsx），
  而不是 500 或者一句「无法解析」。
- **前端错误信息**：`frontend/src/api/client.js` 新增 `errorMessage()`，把 FastAPI 的 422
  （`detail: [{loc,msg}]`）拼成一句人话——之前会显示成「导出失败：[object Object]」。

### 管理员入口

权限模型没有引入角色字段，而是沿用上面同一条语义：**`User.department` 为空 = 不受部门限制 = 管理员**。
这样不需要迁移就能落地，代价是所有新建账号默认都是管理员——所以它只是「数据可见范围」的开关，
不是安全边界；`backend/api_admin.py` 的 `admin_user` 依赖对每个管理接口都做一次校验，
非管理员拿到的是 **403（而不是空数据）**，避免「看不到」被误读成「没有」。

侧边栏「管理员」组（`管理概览 / 用户管理 / 部门权限 / 预警规则 / 审计日志 / 导入批次 / 知识库管理`）放在**最下面**（在「系统」之后）：
它平时用不到，只有需要运维时才找。这一组只对管理员显示，但地址栏可以被
手动输入，所以 `frontend/src/router/index.js` 的守卫对 `meta.admin` 路由会自己拉一次
`/auth/me` 再判权限，不通过就跳回 `/dashboard?denied=admin`（看板顶部会解释原因）。
前端把这次查询收敛在 `frontend/src/api/session.js`：模块级 ref + 进行中的 Promise 缓存，
整站只请求一次，侧边栏和守卫读同一份。

导航现在有 6 组共 16 项，比窗口高度高，所以 `.sidebar` 加了 `overflow-y:auto`（`overscroll-behavior:contain`）：
内容放不下时侧边栏自己滚动，而不是把最后几项裁掉；同时把分组间距、项内边距和品牌区间距压紧了一档，
1440×900 下刚好完整显示，更矮的窗口滚动即可。

六个页面各自解决一个此前只能靠脚本做的事：

- **管理概览**：各表实时计数、`质量体检`（有明细但缺指标的期间 / 有指标但无明细的期间 / 部门为空的行 /
  三项金额全为 0 的行 / 没有批次号的行）、最近 5 个批次、`observability.snapshot()` 的累计请求与 P95，
  以及**按路径 P95 排出的最慢 5 个接口**（`snapshot()["paths"]`，每路径保留最近 200 次采样，
  最慢一条超过 500ms 标红）——全局 P95 只能说明整体感受，路径 P95 才指出该去优化谁，排查时不必翻日志行。
  体检项只做便宜的计数，但能直接抓出「指标与明细对不上」这类真问题。
  规模数字里的「期间指标 / 预警」只数全公司口径行（`department IS NULL`），另用
  `metric_rows` / `alert_rows` 报出含部门行的总行数——一次 `calculate` 会给每个部门各写一行，
  不分口径地数会看着像涨了几十倍。
- **用户管理**：改部门立即改变该账号**财务数据与知识库**的可见范围；空字符串等于清空（升级为管理员）。
  **不允许通过管理接口改自己或删自己**（`400`）：改自己的部门会立刻把自己锁在管理入口之外。
  删除账号只移除登录凭据，财务数据是按批次入库的，不受影响。列表支持按用户名模糊搜、按部门
  （含「未限定部门」这一项）筛选与翻页。
- **部门权限**（`/admin/departments`）：`GET /admin/departments` 把「明细里的部门列 + 指标/预警的部门行 +
  被账号引用的部门」合并成一份部门清单，逐部门给出明细行数、收入/费用金额、期间区间、指标/预警行数、
  绑定账号数；页面上直接把部门填给某个账号（写的是 `PATCH /admin/users/{id}` 的 `department`）。
  部门是**从数据里长出来的**，没有「新建部门」——避免出现一个没有任何数据、只用于授权的空壳部门。
- **导入批次**：按 `import_batch` 分组显示行数、日期范围、涉及期间与导入时间。撤销时先删该批明细，
  再对每个受影响期间**重算**（还有别的批次的数据）或**清空**（没有剩余明细）指标与预警——
  否则库里会留下与明细对不上的幽灵报表。`data/raw/` 下的原始文件按只读约定不动。
  这等价于 `scripts/import_via_api.py --remove` 的语义，现在在界面上就能做。批次多了会分页。
- **预警规则**（`/admin/alert-rules`）：`GET /api/v1/admin/alert-rules` 列出每条规则的指标、比较符、
  阈值、级别、启用开关与更新时间，行内改完点保存走 `PUT /api/v1/admin/alert-rules/{alert_type}`。
  `ensure_rules()` 会在首次访问时补齐默认两条（成本率 > 0.8、净利润 < 0），所以老库打开就有内容；
  阈值绝对值 ≥ 1e16 会被拒（422「阈值超出可表示范围」），级别只接受 `high|medium|low`。
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

权限模型见「管理员入口」一节：`User.department` 为空 = 不受限（管理员），有值 = 只看本部门。
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

「限制部门账号」的代价是所有新建账号默认都是管理员（见「管理员入口」），
所以这套东西管的是**数据可见范围**，不是安全边界。

把数据真正留在库里供前端浏览：

```powershell
# 后端需先启动：.\.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000
.\.venv\Scripts\python.exe scripts\import_via_api.py --username demo_analyst --password demo_password_2026
```

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


