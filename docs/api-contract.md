# API 接口契约

Base URL：`http://localhost:8000/api/v1`

> 本文件是**手写**的散文契约（顺序、文案、脱敏这类「生成不出来的东西」都在这里）。机器可读的那一半由
> FastAPI 生成并冻结在仓库里：`docs/openapi.json`（当前 70 个接口 / 23 个模型）。两者由
> `scripts/check_openapi_snapshot.py` 守着 —— 它核对本文件接口表里点名的每个「方法 + 路径」都真实存在、
> 快照与当前代码逐字节一致、每个接口都带一句说明。改接口后跑
> `.\.venv\Scripts\python.exe scripts\check_openapi_snapshot.py --update` 并把快照一起提交（这一步在
> `check_all.py --quick`、也就是 CI 与提交前钩子里会跑到，所以忘了就会在提交那一刻红）。

## 认证

除 `POST /auth/login`、`POST /auth/register`、`GET /health` 外，**所有接口**（含 `/rag/*`）都必须携带：

```http
Authorization: Bearer <access_token>
```

未携带或令牌失效返回 401；前端收到 401 清除会话并跳转登录页。错误统一返回：

```json
{"detail":"错误描述","code":"BUSINESS_ERROR"}
```

> 说明：本文件此前把流式接口写成 `POST /agent/chat/stream`，与实际路由不符；实际前缀是
> `/ai`，且请求体字段是 `question` 而不是 `message`。以下为核对后的真实契约。

## 鉴权

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/register` | 请求 `{"username","password"}`，返回 `{"id","username"}`（**不发令牌**）；**先查重名（409）再查口令强度**（顺序反了会让种子脚本重注册同名演示账号时突然报「密码太弱」）；弱口令 400 并给出中文原因，留 `user.register_rejected` 审计。⑳ 起新账号默认 `is_admin=false`、没有任何部门（＝空范围，看不到数据）；只有**空库的第一个注册者**会被自动提升为管理员（`bootstrap_admin()`，给全新部署留一条能进管理端的路，表一旦非空就借不到第二次） |
| POST | `/auth/login` | 请求 `{"username","password"}`（JSON，非 OAuth2 表单）；返回 `{"access_token","token_type","token_version"}`，成功时若清掉过失败计数还带 `failures_cleared`。失败 401 / 被锁或限流 429，见下 |
| POST | `/auth/logout` | 退出登录：把该账号的会话版本 +1（此前签发的**每一份**令牌立即失效，含其它设备），返回 `{"username","token_version","logged_out":true}`；留 `user.logout` 审计 |
| GET | `/auth/me` | 返回 `{id,username,department,departments,is_admin,unrestricted,token_version}`；**权威值是数组 `departments`**，`department` 只是它的第一个元素（回落用）。⑳ 起 `is_admin`（权限）与 `unrestricted`（数据范围，即 `scope_of(user) is None`）是两个独立的东西：`departments` 为空**不再**等于管理员，而是「未分配部门 ⇒ 什么都看不到」 |
| PATCH | `/auth/me` | **⑳ 起一律 403**（审计 `user.department_self_denied`）：数据范围与管理员标识只能由管理员在 `PATCH /admin/users/{id}` 分配。只是想换一次口径，用请求里的 `department` 参数（见「数据范围」一节） |

### 会话版本号（令牌怎么被作废）

JWT 是无状态的，**签出去就收不回来**，所以每个请求都会比对一次版本号：

- 令牌声明里多一个 `ver`（签发时的 `users.token_version`）。`current_user()` 拿它与库里那行比对，
  不一致就 401，文案是 `{"detail":"登录已失效（该账号已在别处退出，请重新登录）"}`——
  这个理由与原「登录已失效」（令牌解不开 / 用户不存在）**必须分开**：前者重登即可，后者说明凭据已废。
- **老令牌与老库都算版本 0**：没有 `ver` 声明的令牌按 0 处理，`token_version` 列是 `NOT NULL DEFAULT 0`，
  所以升级不会把所有人踢下线。
- 版本号**只增不减**（回退会让上一次下线前签发的令牌复活）。`POST /auth/logout` 与管理员强制下线
  都只是 `+1`，数据一行都不动——被踢 ≠ 被停用，重新登录即可继续用。
- 审计 detail 里的字段叫 `session_version`（不叫 `token_version`）：`audit._scrub()` 是按**子串**匹配
  敏感键名的，键名含 `token` 会被整条丢掉，结果什么都没记下来。

登录有**滑动窗口限流**（默认 10 次 / 300 秒，按「用户名 + 来源 IP」分桶，配置项 `LOGIN_MAX_ATTEMPTS` /
`LOGIN_WINDOW_SECONDS`）：超过后即使密码正确也返回

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 213

{"detail":"登录尝试过于频繁，请在 213 秒后重试"}
```

**被拒的尝试不落库**（拒绝不是一次尝试——否则越被拒锁得越久，`decide()` 里是「凑够就提前返回」），
登录成功后该桶清零。
计数在 `rate_limit_hits`（`bucket` / `hit_at`）：**存逐次尝试的时间戳而不是一个计数**，
多进程/多副本共享同一本账、重启不清零，同桶判定靠 `pg_advisory_xact_lock` 串行化；
库不可用时是 fail-closed（报错，不静默放行）。运维入口是 `GET /system/status` 的 `rate_limits` 段。
`AUTH_SECRET_KEY` 留空或仍是占位值 `change-this-secret-in-env` 时，进程启动会用 `secrets.token_urlsafe(48)`
生成一个并**就地写回 `.env`**（保留其它行、不重复追加键）：占位密钥意味着任何人都能自己签一个合法 JWT。
副作用是密钥一变，之前签发的 token 全部失效，客户端需要重新登录。

### 账号锁定与口令强度（落库的那一半）

滑动窗口限流按「用户名 + 来源 IP」分桶，**换 IP 就能重来**（分桶键里带来源，这是口径不是存储），
所以连续失败还会落库，锁的是**账号**：

- `users` 上三列：`failed_attempts`（`NOT NULL DEFAULT 0`）、`locked_until`、`last_failed_at`。
  配置项 `LOGIN_LOCKOUT_THRESHOLD=5`、`LOGIN_LOCKOUT_BASE_SECONDS=60`、`LOGIN_LOCKOUT_MAX_SECONDS=900`、
  `LOGIN_FAILURE_TTL_SECONDS=900`、`PASSWORD_MIN_LENGTH=10`。
- 第 n 次失败（n ≥ 阈值）锁 `base · 2^(n−阈值)` 秒，**封顶 900**：
  `5→60s / 6→120 / 7→240 / 8→480 / 9 起→900`。没到阈值时 `locked_until` 一律清成 `NULL`
  （留着一个未来的时间会让「第几次失败」和「还剩多久」互相打架）。
- 失败 401 的 detail 带 `failed_attempts` / `lock_seconds` / `locked_until`，前端能直接显示还差几次；
  **口令正确但账号锁着也是 429**，且与限流那条**文案一字不差**：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60

{"detail":"登录尝试过于频繁，请在 60 秒后重试"}
```

  两句一样是故意的：回显「你被锁了」等于告诉爆破方「这个用户名存在」。这条 429 不含用户名、
  不含「锁」、不含「密码」——`scripts/verify_login_lockout.py` 专门断言了这三点。
- `last_failed_at` 距今超过 `LOGIN_FAILURE_TTL_SECONDS` 就从 0 重新数（静默期归零）；登录成功清零。
  `last_failed_at` 在库里可能是 naive datetime，比较时**按 UTC 补时区**再算。
- 管理员解锁：`POST /admin/users/{id}/unlock` 把计数与锁定时间清空、返回
  `{"id","username","failed_attempts":0,"locked_until":null,"locked":false,"cleared_attempts":N}`，
  **不碰口令、也不碰 `token_version`**（解锁 ≠ 踢下线：正锁着的账号解锁后不改口令就能立刻登录）。
  「原先锁了多久」只在审计 detail 的 `was_locked_for` 里（响应体不返回它）。
- 口令强度规则**不挂在 Pydantic 模型上**：`Credentials` 是登录与注册共用的，挂上去会把老用户的
  正常登录也一起判死。规则走 `backend/passwords.py` 的 `problems(password, username)`，
  按「精确弱口令 / 分词后的弱口令词 / 弱口令子串 / 键盘连打 / l33t 还原后再比一遍」逐层判。
- `GET /admin/users` 每条多出 `failed_attempts` / `locked_until`（ISO UTC 或 `null`）/ `locked`。

### 出口配额（除了登录，还有两组）

限流不止登录。**一次请求就很贵**的两类出口各自一本账，都按**账号**分桶，超限返回与登录同形的 429：

| 配额 | 覆盖 | 默认 | 配置项 |
| --- | --- | --- | --- |
| `export` | `GET /export/transactions`、`/export/metrics`、`/export/alerts` | 30 次 / 60 秒 | `EXPORT_MAX_PER_MINUTE` |
| `ai` | `POST /rag/search`、`/rag/answer`、`/rag/documents`、`/rag/documents/upload`、`/ai/rag/chat`、`/ai/agent/ask`、`/ai/agent/chat/stream` | 60 次 / 60 秒 | `AI_MAX_PER_MINUTE` |

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 42

{"detail":"导出过于频繁，请在 42 秒后重试"}
```

- 三组配额（`login` / `export` / `ai`）**互不影响**：登录被限流不影响看数据，导出打满也能继续问答。
- `/export/scope` 是元数据（给按钮用的口径与上限），不占配额。
- `EXPORT_LIMIT = 20000` 限制的是**单次**导出行数，与本表无关——它拦不住「脚本每分钟导 100 次」。
- **导出/问答的 429 不写审计**：一条被拒请求写一条审计等于给攻击者一个「用便宜请求写满审计表」的放大器；
  观测性里按 `429:路径` 计数，`GET /admin/overview` 的 `observability.requests` 能看到。
  **登录的 429 是例外，它会留痕**（`user.login_blocked`）——「有人在打这个账号」本身就是证据，
  值得一条记录；两类 429 的差别是有意的。
- **计数落库（㉑）**：三组配额共用 `rate_limit_hits(bucket, hit_at)`，桶名 `配额名:分桶键`
  （`export:张三`、`login:张三@10.0.0.9`）。记的是**逐次尝试的时间戳**，判定时按 `hit_at` 裁窗口，
  所以是真滑动窗口而不是「一个计数 + 固定边界」；同桶判定用 `pg_advisory_xact_lock` 串行化，
  避免两个并发尝试都读到最后一行然后各自放行。**跨进程/副本共享、重启不清零**，
  库不可用时 fail-closed。运维入口：`GET /system/status` 的 `rate_limits`（只报聚合桶数，不给分桶键）。

### 慢接口告警（按路径耗时阈值）

配额管的是「请求太多」，阈值管的是「单次太慢」。没有阈值就必须有人盯管理页；有了阈值，
超标的请求自己写一条 `WARNING` 到应用日志：

| 项 | 默认 | 配置项 | 说明 |
| --- | --- | --- | --- |
| 全局阈值 | 1000 ms | `SLOW_PATH_MS` | 单次耗时 ≥ 阈值即计入 `slow_paths` |
| 按路径覆盖 | 空 | `SLOW_PATH_OVERRIDES` | 形如 `/api/v1/ai/rag/chat=15000,/api/v1/ai/agent/chat/stream=300000`；坏条目逐条跳过、不让服务起不来 |
| 同路径告警间隔 | 60 秒 | `SLOW_PATH_LOG_SECONDS` | 窗口内的后续超阈值请求只记账（`suppressed`）、不重复写日志 |


> 部门不要走请求头自述：早期实现用 `X-User-Department: finance-admin` 当管理员凭证，属于可伪造提权。
> **⑳ 起这条兼容路径已彻底删除**（`backend/api_rag.py` 不再有 `DEPARTMENT_ADMIN`，五个检索/问答端点上
> 连 `x_user_department` 参数都没有了）：知识库口径与财务证据一样只认服务端记录 `scope_of(user)`，
> 请求头自述一律不算数。「没分配部门」也不是「不受限」——那是空范围，看不到任何部门数据，
> 知识库里只剩**没标部门的公共资料**（`filter_visible` 里空元组 ⇒ 只见 `department IS NULL` 的片段）。

## 数据范围（scope）

一次 `calculate` 会写两套数据：**全公司口径行**（`department IS NULL`）与**各部门口径行**（`department` 有值）。
所有读 `financial_metrics` / `financial_alerts` 的查询都必须显式选一套，否则会把部门行与汇总行加总
（不会报错，只是数字悄悄变大）。过滤集中在 `backend/scope.py`：

| 函数 | 行为 |
| --- | --- |
| `departments_of(user)` | 读 `User.departments`（JSONB 数组），为空时回落到单值列 `user.department`；返回去重保序的元组 |
| `is_admin(user)` | 读 `User.is_admin`（⑳ 新增的显式管理员标识，与数据范围完全无关） |
| `scope_of(user)` | `tuple[str, ...] | None`；**None = 不受限**（全公司），有值 = 只看这几个部门，**空元组 = 未分配部门 ⇒ 什么都看不到**（⑳ 之前空元组被当成不受限，谁把自己部门清空谁就成了管理员） |
| `scope_metric(stmt, scope)` / `scope_alert(stmt, scope)` | `scope` 为 None 时加 `department IS NULL`，单元素退化 `department = d`，多元素 `department IN (…)`，**空元组 ⇒ `department IN ()` 恒假、一行都不返回** |
| `scope_transactions(stmt, scope)` | None 时不过滤；有值时 `FinancialTransaction.department IN (…)`（部门为空的行不属于任何部门），空元组同样恒假 |
| `in_scope(scope, department)` | 逐行判断（预警这类已经取出来的对象用），None 恒真，空元组恒假 |
| `metric_totals(rows, period_order=…)` | 按期间聚合：**单行期间沿用库里存的比率**，多行期间金额相加、比率按相加后的金额重算（比率不能相加） |
| `scope_payload(scope, requested=…)` | `{department,departments,label,unrestricted,note}`，随看板/分析/报告/Agent/系统状态响应一起返回；空范围的 `note` 会说明「看不到任何财务数据，知识库只剩没标部门的公共资料」 |
| `scope_label(scope)` / `scope_slug(scope)` / `scope_text(scope)` / `row_label(department)` | 文案与文件名的四种写法：「部门 018、097 等 N 个部门」/ 文件名的 `018-097`（空范围是 `none`）/ 审计用的 `018、097`（空范围是「未分配部门」，**不是空串**）/ 数据行级的「部门 097」。**`scope_label` 收的是元组，传字符串会按字符长度渲染成「部门 097 等 3 个部门」** |
| `resolve_requested_scope(user, requested)` | 解析请求里的 `department`：不受限账号可指定任意部门；受限账号只允许在自己绑定的部门里**收窄**，其他值返回违规说明（端点转成 403）；空范围账号填任何部门都被拒 |

> 多部门是「并集口径」而不是「取第一个部门」：多部门账号的看板/汇总/报告/Agent 证据/预测走的是同一份
> `metric_totals`，所以四个页面同一个数。跨期间的完整性判断用「每月行数 == 范围内部门数」
> （`_totals_by_months`），用 distinct 期间数会把缺一期数据当成完整、环比直接算错。

### 查看某个部门的口径（管理员切换视角）

`GET /dashboard`、`/analysis/summary`、`/analysis/alerts`、`/analysis/alerts/all`、`/reports/monthly`、
`/reports/docx`、`/reports/pptx*`、`/ai/agent/chat/stream`、`/export/*` 都接受可选的 `?department=<名称>`：

- **不受限账号**（`scope_of(user) is None`，即管理员且没被收窄）：可以指定任意部门，响应里的 `scope` 会多一个
  `"requested": true` 并把 `note` 换成「正在按「部门 X」口径查看…不是全公司口径。」——用于区分
  「本来就不受限」与「主动切到了某部门」。
- **受限账号**：只能不带参数（＝自己绑定的全部部门，并集）或填**自己范围内的一个**部门收窄；
  填别人的部门一律 **403** `{"detail":"当前账号的数据范围是「部门 A、B」，不能查看「部门 C」"}`，
  而不是静默回落到自己的范围（静默回落会让人以为自己在看别人的数据）。
- **未分配部门的账号**：数据本身就是空的（空范围恒假），填任何部门都 403，文案里的范围名是「未分配部门」。
- 注意 `scope.unrestricted` 描述的是**这次响应覆盖的范围**是否为全公司，不是账号属性；
  「账号本身不受限吗」看 `/auth/me` 的 `unrestricted`，而「账号能不能进管理端」看 `/auth/me` 的 `is_admin`
  （⑳ 起这两件事分开：被收窄到某个部门的管理员照样能进管理端，只是数据只有那一个部门）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/analysis/departments` | 口径下拉的选项：`{"scope":…,"departments":[{"name":"097","label":"部门 097","transactions":1523}],"switchable":true}`；受限账号只会拿到自己绑定的那几个，`switchable` 只在**绑定数 > 1**（可收窄）或不受限时为 `true`；**未分配部门的账号拿到 `departments: []`**（⑳ 起连「有哪些部门、各多少行」也不给） |

受限账号拿别人的预警 id 调 `/analysis/alerts/{id}/transactions` 会得到 HTTP 200 + `{"data":[],"message":"该预警不在当前账号的数据范围内"}`（不泄漏"这条预警存在"以外的信息）。

### 季度/年度落库（`financial_rollups`）

季/年合计不现算，落在独立表里；`/analysis/summary` 接受 `YYYY-MM`、`YYYY-QN`、`YYYY` 三种写法：

- **`source` 如实说明数字哪来的**：`month`（月口径读 `financial_metrics`）/ `stored`（读落库行）/
  `computed`（这一期还没落库，按同一算法现算兜底）/ `empty`（该期没有任何数据）。
  **读路径不会顺手写库**：现算的季/年不会留下落库行（真机冒烟断言过 `coverage.rows` 前后不变）。
- **`complete` / `missing_months` / `months`**：`months` 是**真的被求和的月份**，`covered_months` 是
  这个粒度应有的月份（季 3 / 年 12），`complete = 有月份 and 没有缺月`。演示数据只有 2026-04~10，
  所以 `period=2026` 返回 `complete=false`、`missing_months=['2026-01','2026-02','2026-03','2026-11','2026-12']`。
- **比率按相加后的金额重算**（不做比率平均），金额口径与看板一致（字符串→此处为 float，单位元）。
- **季/年不产生预警**：预警始终按月算，季/年只是合计。
- `GET /admin/rollups` 的 `coverage` 块：`{rows, company_rows, department_rows, quarters, years,
  incomplete:[{grain, period, period_label, department, department_label, missing_months}], grains, grain_labels}`。
  缺月清单带 `department`/`department_label` 且**全公司行排在同期的部门行前面**——同一个季度里全公司行
  齐全、某个部门只落了其中一个月是常事，早先只按 `(粒度, 期间)` 去重时页面上会出现「2026 年度缺 11 个月」。
- 自动刷新点：`POST /analysis/calculate?period=YYYY-MM`（重算某月→刷新它所在的季与年；
  注意 `SessionLocal` 是 `autoflush=False`，实现里先 `db.flush()` 再刷新，否则会漏掉刚算的这一个月）、
  `DELETE /admin/batches/{batch}`（撤销后刷新或清掉空掉的季/年）。`POST /admin/rollups/rebuild` 用于回填与修数。

## 财务数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/financial/template` | 下载导入模板（utf-8-sig CSV） |
| POST | `/financial/preview` | 只校验不落库，返回 `{filename,total,columns,missing,errors,error_count,valid,preview}` |
| POST | `/financial/import` | 真正入库；文件后缀限 xlsx/xls/csv，大小 ≤10MB；同一 sha256 批次重复提交返回 409 |
| GET | `/financial/transactions` | 明细列表。参数 `period`、`department`（**口径**，越界 403）、`batch`、`query`（科目/编码/摘要模糊）、`limit`（1..200）、`offset`；返回 `{data,total,offset,limit,periods,scope}`，`total` 是筛选后总数 |
| GET | `/financial/transactions/{id}` | 单行明细（含 `reversed`/`reversal_of_id`/`is_reversal`/`corrections`） |
| PATCH | `/financial/transactions/{id}` | 就地更正：`transaction_date`/`account_code`/`account_name`/`department`/`description` + 五个金额列（字符串或数字，按导入同一套规则解析）。未知字段由 `extra="forbid"` 拦成 422 |
| POST | `/financial/transactions/{id}/reverse` | 冲销：新增金额取负的冲销行并标记原行（body 可选 `{"reason": "…"}`，≤500 字）。不删原行 |

`.xls` 依赖服务端安装 `xlrd`；没装时返回 **400** 并说明两条出路，而不是 500 或一句「无法解析」：

```json
{"detail":"旧版 .xls 需要服务端安装 xlrd（pip install \"xlrd>=2.0.1\"）；也可以先在 Excel 里「另存为 .xlsx」再导入"}
```

### 明细更正与冲销

两条路径的语义刻意不同：**更正就地改字段**（旧值进审计），**冲销不删原行**而是新增一条金额取负的行，
两行合计为 0、指标仍然等于真实账。删行会让明细条数与原始凭证对不上，也解释不了这笔钱去哪了。

更正响应（冲销同形，`original` + `reversal`）：

```json
{
  "data": {"id": 537600, "period": "2097-03", "revenue_amount": 777.77, "corrections": 1, "reversed": false},
  "recalculated": ["2097-03"],
  "cleared": []
}
```

- `recalculated`：被重算的期间（改日期会同时重算新旧两个期间）。
- `cleared`：已无剩余明细、连指标与季年落库一起清掉的期间。
- 金额一律按导入同一套规则解析（千分位、中文逗号、`|v| < 1e16`）；前端不做金额运算。

错误码：

| 状态 | 触发条件 | detail |
| --- | --- | --- |
| 401 | 未登录 | — |
| 403 | 行的部门不在账号范围内，或受限账号要动 `department` 为空的全公司行 | `该明细不在当前账号的数据范围内` |
| 403 | `?department=` 切到账号范围外的口径 | `当前账号的数据范围是「部门 018」，不能查看「部门 097」` |
| 404 | 行不存在 | `没有找到这条明细` |
| 409 | 重复冲销 / 冲销一条冲销行 / 更正已冲销的行 | `这一行已经冲销过` / `这一行本身就是冲销行，不能再次冲销` / `这一行已经冲销，冲销后再改原行会让账更乱；请先撤销冲销（如果有）或重新导入` |
| 422 | 未知字段（`extra="forbid"`）、空改动、值与原值一致、非法金额、带时区的日期 | `不认识的字段：nope` / `没有要更正的字段` / `改动的值与原值一致，没有需要保存的内容` / `收入金额不是有效金额：abc` / `交易日期不能包含时区` |

审计：`transaction.update`（`detail` 带 `before`/`after`/`periods`）与 `transaction.reverse`
（`detail` 带 `period`/`reversal_id`/`reason`/`revenue_amount`）。这一页没有删除接口——要整批收回请撤销导入批次。

### 写路径的期间锁（并发重算互斥）

`POST /analysis/calculate`、`POST /financial/import`、`PATCH /financial/transactions/{id}`、
`POST /financial/transactions/{id}/reverse`、`DELETE /admin/batches/{id}`、`POST /admin/rollups/rebuild`
这些会改动某一期的接口，进入临界区前都要先拿「这一期（含它的季与年）」的事务级 advisory lock。
锁是数据库级的，跨进程有效，随事务结束自动释放（提交、回滚、连接断掉都释放）。

对调用方只有一条可观察的影响：**同期并发时请求会排队而不是算错**，所以「同一期的写请求突然变慢」是
正常现象（不是超时、不是 5xx）。**不会**因此多出状态码：仍然 200，只是耗时里多了一段等锁时间。
不同期间不互相排队（只有同一年下的不同月份会共享年锁——月重算要刷新年度落库行，属于刻意取舍）。

## 指标与预警

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/analysis/calculate?period=YYYY-MM` | 重算某期指标与预警（写全公司 + 各部门两套行）；顺手刷新该月所在的季/年落库 |
| GET | `/analysis/summary?period=YYYY-MM\|YYYY-QN\|YYYY` | 合计 + 口径说明：`{has_data,revenue_amount,cost_amount,net_profit_amount,net_profit_ratio,scope,period,grain,grain_label,period_label,source,months,covered_months,missing_months,complete}` |
| GET | `/analysis/alerts?period=YYYY-MM` | 该期预警列表；每项含 `{id,period,department,type,description,level,metric}` + 处理状态字段（见下） |
| GET | `/analysis/alerts/all?limit=N&offset=M&status=&period=&department=` | 全部期间的预警 + `periods` + `scope` + `{total,offset,limit}`（`total` 是**筛选后**的总数，不是本页条数）；`status` 在服务端过滤，取值同下，非法值 422「状态只能是 …」 |
| GET | `/analysis/alerts/stats?period=YYYY-MM` | 按处理状态计数：`{counts:{pending,confirmed,false_positive,resolved,total}, labels, statuses, scope}`；`period` 省略则统计全部期间 |
| PATCH | `/analysis/alerts/{id}` | 处理一条预警：`{status, note}`，返回更新后的预警 JSON |
| GET | `/analysis/alerts/{id}/transactions?limit=N&offset=M` | 触发该预警的明细（`limit` 上限 2000，默认 200；响应带 `total`）；预警口径与查看者范围不一致时返回空列表 + `message` |

### 预警处理状态

`status` 只能是 `pending`（待处理）/ `confirmed`（已确认）/ `false_positive`（误报）/ `resolved`（已处理）：

```json
{ "id": 128, "period": "2026-10", "type": "利润异常", "level": "high",
  "status": "confirmed", "status_label": "已确认", "note": "已核对凭证原件",
  "reviewed_by": "demo_analyst", "reviewed_at": "2026-10-08T07:44:12+00:00",
  "needs_human_review": false }
```

- `note` 最长 2000 字；**`status=false_positive` 必须带非空 `note`**，否则 422「标记为误报时必须填写原因」。
- 改回 `pending` 会清空 `reviewed_by` / `reviewed_at`（序列化成空串），`needs_human_review` 随状态派生。
- 状态由 `POST /analysis/calculate` 按「口径 + 预警类型 + 指标名」继承，重算不会抹掉；行 `id` 会变。
- 受限账号处理别的部门（或全公司口径）的预警得到 **403**，`detail` 含「不能处理」。
- `GET /analysis/alerts/all` 的 `status` 过滤发生在 `count` 与 `limit` **之前**：筛选若放在前端对当前页
  做 `filter`，第 2 页的误报会被静默漏掉（这正是改造前的行为）。


## 看板

```http
GET /dashboard?period=month|quarter|year
```

`period` 的取值按**库里最新一期**（`financial_metrics` 最大 `period`）推算出自然月/季/年范围，
而不是当前系统时间；该范围内没有数据就返回 `has_data:false` 与 `message`。
`updated_at` 是**带业务时区偏移**的时刻（如 `2026-10-09T00:33:45+08:00`，⑯ `BUSINESS_TIMEZONE`）——
以前是 naive 的本机时刻，前端只能当浏览器本地时间猜。

返回（金额一律为**字符串**，避免 5.8 万亿量级上 float 丢分位）：

```json
{"period":"year","period_label":"本年","has_data":true,"updated_at":"...",
 "range":{"year":2026,"months":["2026-01","..."],"label":"2026-01~2026-12"},
 "compare":{"type":"环比","label":"2025-01~2025-12","available":false,"reason":"缺少 ... 的完整数据，无法计算环比"},
 "kpi":[{"label":"营业收入","value":"25,374,046,483.60","unit":"元","icon":"↗","change":null,"dir":"flat","note":"无可比期间"}],
 "months":["2026-04","..."],
 "revenue":["1490807420.19","..."],
 "expenses":[{"name":"097（部门）","value":"5358555937588.18","ratio":"91.03"}],
 "cash":{"x":["现金收支（未分类）"],"inflow":["0.00"],"outflow":["0.00"]},
 "alerts":[{"id":233,"type":"利润异常","desc":"本期净利润为负","level":"high","time":"...","period":"2026-10","department":null}],
 "scope":{"department":null,"label":"全公司","unrestricted":true,"note":"..."},
 "source_note":"...","chart_notes":{"revenue":"...","expenses":"...","cash":"...","cash_note":"..."}}
```

- 6 个 KPI：营业收入、成本金额、费用金额、收支差额（简化口径）、收支差额率、成本率（带 `threshold_hint`）。
- `change` 只有在**上一期数据完整**时才有值；否则为 `null` 且 `note` 说明原因（不编造同比/环比）。
- `expenses` 是按部门支出 Top5（不再是「成本/费用」两片），带 `ratio`。
- `scope` 说明这份数据属于哪个口径；受限账号（`unrestricted:false`）的 KPI 与 `revenue`/`expenses`
  只统计本部门，前端每页顶部的提示条就来自它。

## 数据导出

把「数据库里的数据」交回用户手里：财务同事需要能拿去做透视表，而不是只看网页。
四个端点都在 `/api/v1/export` 下，都要登录，都受部门口径约束。

| 方法 | 路径 | 参数 | 列 |
| --- | --- | --- | --- |
| GET | `/export/transactions` | `period?`（省略=全部期间；月 `2026-07`、季 `2026-Q3`、年 `2026` 都认）、`batch?`（导入批次精确匹配）、`query?`（科目编码/名称/摘要模糊匹配）、`account?`（科目名模糊匹配）、`department?`、`format` | 明细 id,交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出,导入批次,冲销状态,原始行 id,冲销时间,冲销操作人,更正次数 |
| GET | `/export/metrics` | `grain?`（`month`（默认）/`quarter`/`year`）、`period_from?`、`period_to?`（**必须与 `grain` 同粒度**）、`department?`、`format` | 期间,粒度,口径,包含月份,是否完整,收入金额,成本金额,费用金额,毛利金额,净利润,毛利率,净利率 |
| GET | `/export/alerts` | `period?`（单期）或 `period_from?`/`period_to?`（区间，看板按「本年」展示时用）、`level?`、`department?`、`format` | 期间,口径,预警类型,描述,级别,指标,当前值,阈值,需人工复核,预警 id |
| GET | `/export/scope` | 无 | 给前端按钮用的口径说明：`{scope,formats:["csv","xlsx"],limit,generated_at,timezone}`；`timezone` 是业务时区的可读文案（如 `Asia/Shanghai (UTC+08:00)`），`generated_at` 带该偏移——下游别去猜机房在哪 |

- `format` 只接受 `csv`（默认）或 `xlsx`，其他值 **422**。
- **明细导出的筛选与页面同源**：`period`/`batch`/`query` 走的是 `backend/transactions.py::apply_filters`，
  与 `/financial/transactions` 列表**同一个函数**，避免出现「页面上筛出来 12 行、导出 15 行」。
  多个筛选条件之间是**且**（批次 + 关键字只出同时满足的行）。
- **指标的季/年不重算，读的是 `financial_rollups`**（⑧ 的落库表）：`grain=quarter|year` 返回落库行，
  `period` 形如 `2026-Q3`/`2026`，与 `/admin/rollups`（管理概览那张卡）**同一份数据**。
  两列专门解释「这个合计到底含了几个月」：`包含月份` 是实际落库的月份（顿号分隔），
  `是否完整` 在 `complete=False` 时写 `否`——月表只有 7 个月时，年度行**不会假装是全年**。
- `grain` 与端点写错、粒度不匹配（如 `grain=quarter&period_from=2026-04`）、区间倒过来
  （`period_from` 晚于 `period_to`）都返回 **422**，并在提示里给出样例（`2026-Q3`），
  而不是安静地返回空表。
- **预警导出保持月口径**：预警行本身就是月粒度的，`?period=2026-Q3` 会 **422**（不静默当成空结果）。
- `grain=month` 走 `financial_metrics`（单月），此时 `period_from`/`period_to` 必须是月。
- 响应头告诉调用方结果规模，前端据此提示，避免「以为导全了其实被截断」：

```http
Content-Disposition: attachment; filename="export.csv"; filename*=UTF-8''transactions-2026-07-097.csv
X-Export-Rows: 1523
X-Export-Truncated: true
Access-Control-Expose-Headers: X-Export-Rows, X-Export-Truncated, Content-Disposition
```

  上限 `EXPORT_LIMIT = 20000` 行，超过时置 `X-Export-Truncated: true`（三个导出端点都用
  「实际行数 > 上限」判定，不是写死的常量）。没有截断时**不出现这个头**——前端据此区分
  「导全了」与「被切了」。
  中文文件名走 RFC 5987 的 `filename*`（HTTP 头只能是 latin-1），`filename=` 是同名 ASCII 兜底。
  文件名里的期间标记与筛选一致（`transactions-2026-07-097.csv`、`metrics-quarter-2026-Q3-all.csv`、
  `alerts-2026-04~2026-10-all.csv`），不要出现「写着全部期间、实际只导了一段区间」这种情况。
- CSV 是 `utf-8-sig`（带 BOM，Excel 直接双击不乱码）；**CSV 里一切皆文本**，避免 Excel 把 `2026-10`
  认成日期、把部门号 `097` 变成 `97`；**XLSX 里数字列是真数字**（能直接求和），文本列仍是文本。
  唯一的例外是纯数字的期间串：XLSX 会把年度期间 `2026` 写成数字单元格，读回来是 `2026`（int）而不是
  `"2026"`——按文本比对期间的下游要注意这一点。
- 导出永远只包含调用者口径的数据：受限账号导出不会出现别的部门，管理员导出的是全公司行，
  不会把 `department IS NULL` 与各部门行加总（这是这套两套行模型最容易悄悄出错的地方，回归脚本专门断言）。
- **明细导出的三列冲销/更正标记**（⑨ 的更正与冲销在导出里必须有解释）：
  `冲销状态` 空 = 正常行、`已冲销` = 被冲销的原行（此时 `冲销时间`/`冲销操作人` 记的是**冲销那一刻**的
  业务时间与账号，不是这一行的交易日期）、`冲销行` = 冲销产生的负数行（`原始行 id` 指回被冲销的行，
  摘要以 `冲销：` 开头）；`更正次数` 是这一行被就地更正过几次（0 表示没改过）。
  负数行的金额就是负的，导出的合计与页面看到的净额一致。

## 预测与报告

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/forecast/financial?periods=1..12` | 线性趋势外推，返回 `method/periods/revenue_amounts/net_profit_amounts/needs_human_review` |
| GET | `/reports/monthly?period=YYYY-MM` | 返回 `text/plain` 的 Markdown 月报（前端页面 `/reports`） |
| GET | `/reports/docx?period=YYYY-MM[&department=X]` | 返回 `.docx` 二进制（`application/vnd.openxmlformats-officedocument.wordprocessingml.document`），见下 |
| GET | `/reports/pptx/outline?period=YYYY-MM` | PPTX 的逐页大纲（JSON），见下 |
| GET | `/reports/pptx?period=YYYY-MM` | 返回 `.pptx` 二进制（`Content-Disposition: attachment; filename="financial-report-YYYY-MM.pptx"`） |

三个下载接口的文件名都遵守同一条约定：`Content-Disposition` 里同时给
`filename="<ASCII 兜底>"` 与 `filename*=UTF-8''<百分号编码的中文名>`（RFC 5987）。
HTTP 头只能是 latin-1，中文名直接写会让 Starlette 抛 `UnicodeEncodeError: 'latin-1' codec can't encode characters`。
指定口径时名字会带上部门号（`financial-report-YYYY-MM-097.docx`），避免部门报告被当成公司报告发出去。

`.docx` 报告结构（python-docx 生成，可编辑；没有指标时是含「数据不足」的单页文档）：

```text
2026-10 财务分析报告                        ← 标题（受限口径时副标题追加「· 数据范围：部门 097」）
一、核心指标     表：指标 | 金额 / 比率
二、风险预警     表：级别 | 预警类型 | 描述（无预警时写「该期间未触发预警。」）
三、费用构成（按部门 Top5）   表：部门 | 费用金额
四、口径与限制   项目符号列表（口径恒 0、现金流按 0 处理等）
```

实现注意：中文字体必须显式设 `w:eastAsia`（python-docx 默认样式只有 Calibri，否则部分机器上中文会走字体替换）；
文件名与后缀要分开传给 `backend/files.py::content_disposition()`，名字里带 `.docx` 会让 ASCII 兜底因为有 `docx`
这几个字母而「合格」，中文兜底就失效了。

月报结构（金额为纯数字无千分位，比率已换算成百分数、保留两位小数）：

```markdown
# 2026-10 财务分析报告

## 核心指标
- 收入：1522959.18 元
- 成本：0.00 元
- 费用：31993341087.05 元
- 净利润：-31991818127.87 元
- 毛利率：100.00%
- 净利率：-2100635.30%

## 风险预警
- [high] 利润异常：本期净利润为负

## 口径与限制
- 指标由程序按期间确定性汇总，未经人工调整；金额保留两位小数，比率保留四位小数。
- 成本金额、成本率恒为 0：当前数据源没有成本科目，全部支出计入费用金额。
- 现金流入/流出按 0 处理：数据源没有银行流水时间轴，不从权责发生制金额推现金。
- 毛利 = 收入 - 成本，在成本恒为 0 时等于收入，毛利率因此显示为 100.00%，不代表真实盈利水平。
- 收支差额 = 收入 - 成本 - 费用，是简化口径，不是会计意义上的利润总额。
```

没有该期间指标时返回 `# 财务分析报告\n\n数据不足，无法生成报告。`。接口同样要求 Bearer Token（未登录 401）。

### PPT 大纲

```json
{"period":"2026-10","has_data":true,"filename":"财务分析报告-2026-10.pptx",
 "slides":[
   {"title":"2026-10 财务分析报告","kind":"cover","subtitle":"智能财务分析平台 · 指标由程序确定性汇总，报告只做呈现","bullets":[]},
   {"title":"核心指标","kind":"table","table":[["收入","1522959.18 元"],["成本","0.00 元"],["费用","31993341087.05 元"],["净利润","-31991818127.87 元"],["毛利率","100.00%"],["净利率","-2100635.30%"]],"bullets":[]},
   {"title":"风险预警","kind":"bullets","bullets":["[high] 利润异常：本期净利润为负"]},
   {"title":"费用构成（按部门 Top5）","kind":"chart","bullets":["097：31777534662.41 元","…"]},
   {"title":"口径与限制","kind":"bullets","bullets":["…5 条…"]}],
 "notes":["…同月报的 5 条口径与限制…"]}
```

`kind` 取值 `cover`/`table`/`bullets`/`chart`，前端据此决定排版。没有该期间指标时 `has_data:false`
且 `slides` 只有封面页（`bullets:["数据不足，无法生成报告。"]`）。

`/reports/pptx` 用 python-pptx 生成 5 页：封面、核心指标、风险预警、费用构成（**原生柱状图对象**，
不截图、不依赖 matplotlib）、口径与限制。风险预警页同样带 `alert_type`，口径与限制页随文档一起下发
（否则「毛利率 100.00%」会被读成经营结论）。未安装依赖返回 503 与安装命令。

## 容器化部署时的调用方式

`docker compose up -d --build` 起三个容器（前端 nginx `8080` / 后端 uvicorn `8000` / postgres `5432`），
**前端只走同源 `/api`**：

| 场景 | 基地址 |
| --- | --- |
| 浏览器（界面、Agent 流式问答） | `http://127.0.0.1:8080/api/v1/...`（nginx 转给 `backend:8000`） |
| 本机脚本/验收 | 同源地址，或 `http://127.0.0.1:8000/api/v1/...` 直连后端发布端口 |

两点与契约相关的后果：

- 反代带 `X-Forwarded-For`，uvicorn 以 `--proxy-headers --forwarded-allow-ips '*'` 解析它，
  所以审计里的 `ip` 是真实来源（实测 `172.23.0.1`）而不是 nginx 容器地址；生产部署应删掉后端的
  `ports`，只暴露前端。
- SSE（`/ai/agent/chat/stream`）依赖 nginx `proxy_buffering off`，否则流式回答会被攒成一次性响应。

## 系统状态

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/system/status` | 运行态汇总（前端页面 `/settings`） |

返回 `{app,llm,database,rate_limits,embedding,rag,directories,data,me,config_problems}`：

- `app`：`env`/`is_development`/`strict_config`/`api_prefix`/`cors_origins`/`log_level`/`token_expire_minutes`/
  `business_timezone`——后者是 `{name,label,offset,now,today}`（如 `{"name":"Asia/Shanghai",
  "label":"Asia/Shanghai (UTC+08:00)","offset":"UTC+08:00","now":"2026-10-09T00:33:45+08:00",
  "today":"2026-10-09"}`）：「今天」「本月」到底按哪个日历日算，这里直接给出答案（⑯，见
  README「业务时区（`BUSINESS_TIMEZONE`）」）。老版本没有这个键，前端显示「（未上报）」而不是报错。
- `llm`：`provider_configured`（**只报有没有配，不回显密钥**）、`base_url`、`model`、`rerank_model`。
- `database`：`ok`/`error`/`version`/`url`（口令打码为 `user:***@host`）/`pgvector`。
- `rate_limits`（㉑）：`{store, shared_across_processes, survives_restart, quotas, buckets}`——计数落在哪、
  三组配额在不在、当前有多少个活跃分桶（`quotas` 就是「配置项现在是什么值」的机器可读版本，
  改配置后这里立刻跟着变）。**只给聚合桶数，不给分桶键**：键里有用户名与来源 IP，而这个接口普通账号也能读。
  库不可用时 `buckets` 是 `-1` 并带 `error`，而不是把这个健康检查接口自己打成 500。
- `embedding`：`model`/`dimension`(384)/`using_fallback`。
- `rag`：`max_distance`/`candidate_k`。
- `directories[]`：`{name,path,configured,writable,error}`——`writable` 是真写一个探针文件再删的结果。
- `data`：`periods`/`latest_period`/`counts{transactions,metrics,alerts,knowledge_chunks,users}`。
- `me`：`{id,username,department,departments,is_admin,unrestricted,token_version}`——设置页的「当前账号 /
  数据范围 / 管理员标识」三张卡都读它；`unrestricted` 是数据范围（⑳ 起 `departments` 空 = 未分配 = 看不到
  数据，**不再**等于不受限），`is_admin` 才是权限（能不能进管理端），两者独立；
  `token_version` 是当前账号的会话版本（令牌里的 `ver` 与它一致才放行）。
- `config_problems[]`：复用启动时的 `Settings.validate()`（占位密钥、通配 CORS、非本机来源）。

## 知识库（RAG）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rag/documents` | 直接写入文本片段；内容与已入库片段完全一致时跳过，返回 `{success,ids,skipped}` |
| POST | `/rag/documents/upload` | 上传 txt/md/csv/pdf/docx；同来源同内容不重复入库，返回 `{success,source,chunks,skipped}` |
| POST | `/rag/search` | `{query,top_k}`，先多召回再重排 |
| POST | `/rag/answer` | `{query,top_k}`；按 `scope_of(user)` 过滤（不受限可见全部，未分配部门只剩 `department IS NULL` 的公共资料）；**⑳ 起请求头不再参与判定** |

## Agent 与问答

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| POST | `/ai/rag/chat` | `{"question":"..."}` | 知识库问答，返回 `{answer_status,answer,sources,embedding_model}`；**不落库、不记账** |
| POST | `/ai/agent/ask` | `{"question":"..."}` | 同上 |
| POST | `/ai/agent/chat/stream` | `{"question":"..."}` | SSE 流式；可用查询参数 `?session_id=<会话 id>` **接着已有会话问**（不带就按会话里的立场新起一条会话） |
| GET | `/ai/agent/sessions?limit=&offset=` | — | 我的会话列表（倒序）：`{data,total,limit,offset,untitled}`，每行 `{id,title,display_title,scope,answers,total_tokens,created_at,updated_at}` |
| POST | `/ai/agent/sessions` | `{"title"?: "..."}` | 显式建会话，成功 **201 且直接返回会话体（不裹 `data`）**；标题会压掉首尾空白，超过 120 字 → 422 |
| GET | `/ai/agent/sessions/{session_id}` | — | 会话详情：`{data,messages,status_labels}`；每条消息带 `status`/`status_label`/`stop_requested`（㉒：这条回答被用户点过停止就是 `true`）；**别人的会话与不存在的会话都 404** |
| DELETE | `/ai/agent/sessions/{session_id}` | — | 删会话（连同它的消息）：`{"id","title","deleted_messages","deleted":true}`；别人的会话 404 |
| POST | `/ai/agent/sessions/{session_id}/stop` | — | 停止正在生成的回答：**把 `stop_requested_at` 写到那一行上并立即提交**（㉒，跨 worker 也停得下来），返回 `{"session_id","message_id","stopped":true}`；该会话当前**没有**正在生成的回答、或那一行刚好收尾了 → **409 `这个会话当前没有正在生成的回答`**；别人的会话 404 |
| GET | `/ai/agent/usage?days=30` | — | 我的用量：`{data,series,status_labels}`；`days` 取 1..365（越界 422） |

流式响应为 `text/event-stream`：

```text
event: start\ndata: {"session_id":12,"message_id":34,"question_message_id":33,"title":"本月收入","history_turns":1,"history_messages":2,"history_tokens":57,"history_truncated":false}\n\n
event: delta\ndata: {"text":"本月"}\n\n
event: done\ndata: {"session_id":12,"message_id":34,"status":"ok","stopped":false,"sources":[{"name":"财务汇总表 2026-07","kind":"financial_metric"}],"has_financial_data":true,"matched_period":"2026-07","knowledge_chunks":3,"scope":{"departments":[]},"tokens":{"prompt_tokens":2223,"completion_tokens":1100,"total_tokens":3323,"usage_source":"api"},"history_turns":1,"history_messages":2,"history_tokens":57,"history_truncated":false,"duration_ms":19063}\n\n
event: error\ndata: {"message":"模型暂时不可用"}\n\n
```

`sources[].kind` 取值：`financial_metric`（汇总表）、`financial_alert`（预警）、
`financial_transaction`（明细账）、`knowledge_base`（知识库片段）。Agent 会先把已导入的
财务数据整理成确定性证据（见 `backend/rag/financial_evidence.py`），再叠加知识库检索结果；
两者都没有时回答「当前知识库中未找到相关数据，请补充材料或人工复核。」

前端消费方：看板底部的对话卡片与独立工作台 `/agent` 共用 `frontend/src/components/AgentChat.vue`，
都调这个接口。财务证据的 `name` 已经带了类型前缀（如「财务汇总表 2026-07」），只有 `knowledge_base`
的 `name` 是文件名，所以展示时按「名字是否已以类型开头」决定要不要补前缀，并对同名片段去重
（`frontend/src/utils/markdown.js` 只负责把回答渲染成标题 / 列表 / 表格，转义在解析之前完成）。

### 会话落库、停止生成与用量口径

`/ai/rag/chat` 与 `/ai/agent/ask` 是一次性的，`/ai/agent/chat/stream` 则会把提问与回答都写进
`agent_sessions` / `agent_messages`（`backend/agent_sessions.py`），所以它有下面这些额外语义：

- **`start` 事件先于第一块 `delta`**：会话 id 与两条消息 id 在这时就交给前端了。前端若没显式建会话，
  就用这个 id 记住会话（后续追问带 `?session_id=`）；显式建过的会话沿用原来的 id。
- **回答先落一行 `status="streaming"` 再调用模型**，所以生成中刷新页面能看到「生成中」。
  进程被杀会留下孤儿行，读的时候按 `STREAMING_STALE_SECONDS = 120` 判成终态（`effective_status()`），
  库里那份不动；**判成哪一个看 `stop_requested_at`（㉒）**：有值（用户点过停止）读成「已停止」，
  没有值（进程崩了没人管）读成「已中断」——把用户自己要的停止说成故障是错的。
  状态只有五个：`streaming` / `ok` / `stopped` / `error` / `interrupted`。
- **停止生成是显式请求**，不是断开连接：`POST …/stop` 把 `agent_messages.stop_requested_at`
  写到那一行上（**立即提交**），生成器**按节拍回头读库**（`STOP_POLL_SECONDS = 1`，本地
  `threading.Event` 只做快路径），收尾时把已生成的正文按 `status="stopped"` 落库并在 `done` 里回
  `stopped: true`。
  - 标志位**落库而不是只放在进程内存里**（㉒）：多 worker 部署时停止请求可能落在别的 worker 上，
    重启后也要读得出「这条是用户停的、那条是进程崩的」。
  - 409 有两种情形：会话里**没有**正在生成的回答；或者查出来的那一行还在 `streaming`、写下去时它
    刚好收尾了（`rowcount = 0`）——**假装成功会让用户以为停下了**，所以宁可报 409。
  - **读库失败时当作「没停」**（fail-open，日志留痕）：一次查询失败不该掐掉用户正在等的回答。
    这与 ㉑ 的限流必须 fail-closed 是两回事——那是授权闸门，这是尽力而为的取消。
  - 停止请求**早于第一块**到达时一块都不吐，并且这一拍不调用模型、`total_tokens` 记 0
    （没有用量可估算，不凭空多记一笔）。
- **token 两路记账，别把估算当账单**：最后一帧报了用量就是 `usage_source="api"`；被停止 / 出错拿不到用量，
  按 `CHARS_PER_TOKEN = 1.5` 折算为 `estimated`；一次模型都没调用则是 `free`。`data` 里三者分开计数
  （`api_answers` / `estimated_answers` / `free_answers`），`series` 是**含今天的 N 个自然日**（起点对齐
  那一天的 00:00，**那一天是业务时区的日历日**：`window_start` 形如 `2026-10-09T00:00:00+08:00`，
  分组也在 SQL 里按业务时区切），没问的日子补 0。
- **归属只有一条规则**：会话属于提问的人，别人的会话 id 一律 404（不区分「没有」与「不是你的」）。
  跨用户的用量只在管理员端点 `GET /admin/agent-usage` 看汇总。
- 令牌与数据范围的口径与其它接口一致（⑳ 起只认服务端记录，请求头不算）：会话的 `scope` 记的是提问时的口径快照；
  未分配部门的账号只检索得到没标部门的公共片段。
- 完整前缀是 `/api/v1/ai/agent/...`（`backend/api_agent.py` 的 `prefix="/api/v1/ai"`），漏掉 `/ai` 会 404。
- 真机验证：`scripts/verify_agent_stop.py`（真模型流式 + **真子进程**：父进程开着一条流，另起一个
  全新 Python 解释器只连库往那一行写标志，父进程这条流随即以 `stopped` 收尾——全程没打过停止接口，
  所以停它的确实是另一个进程；另验重启后「已停止」与「已中断」读得出来、跑完不留临时会话与临时行）。
  `scripts/verify_agent_sessions.py` 与 `verify_agent_memory.py` 里那两次「生成中停止」走的仍是
  本进程的 HTTP 路径。

### 多轮上下文（追问时模型看得见前几轮）

`start` 与 `done` 两个事件都带 `history_turns` / `history_messages` / `history_tokens` /
`history_truncated`：**这一轮到底把几轮历史交给了模型**（`history_messages` 恒等于
`history_turns × 2`）。前端 `AgentChat.vue` 把它们渲染成回答下方的「已带上前 N 轮对话」，不显示就是 0。

- **只带写完的成对问答**（`backend/agent_sessions.py` 的 `history_for()`）：回答是 `ok` 或 `stopped`
  且正文非空才算一轮；`error` / `interrupted` / 还在 `streaming` 的，以及没有回答的孤零零提问，
  **整对丢掉**——它们是"没说完的话"，带上去只会让模型顺着半句话往下编。
- **预算是配置项**：`AGENT_HISTORY_MAX_TURNS`（默认 6，按问答对数）与 `AGENT_HISTORY_MAX_TOKENS`
  （默认 1500，按 `CHARS_PER_TOKEN = 1.5` 估算）。超预算时从**最老的一整对**开始丢，
  宁可少一轮也不给半轮；任一为 0 就等于关掉多轮。`history_truncated: true` 只表示"有更早的对话没带上"，
  不区分是被预算截断还是被关掉。
- **消息顺序是 system → 历史 → 本轮（问题 + 证据）**：系统提示不能被历史里模型自己上一轮的话冲掉，
  而本轮的检索证据必须紧挨着本轮提问（两轮证据长得很像，只差一个期间）。
- **估算口径跟着变**：没有用量上报时，按字数估算的 `prompt_tokens` 把历史一起算进去
  （`prompt_text` 里含历史的角色与正文），所以关掉多轮会直接看到这个数字变小。
- 落库的回答里**不记录**这一轮带了哪些历史（`agent_messages` 没这列）：这些数字只在事件里给，
  刷新页面后回答本身照旧可读，只是不再显示「带上了前 N 轮」。
- 真机验证：`scripts/verify_agent_memory.py`（临时账号连问 7 轮，第一轮埋一个随机口令、
  第二轮只问口令，回答里出现口令才算真送到了上游；另验预算封顶、被停止的回答照样进上下文）。

## 管理员

所有 `/admin/*` 接口都经过 `admin_user` 依赖：**⑳ 起只看显式的 `User.is_admin`**（以前「部门为空 = 不受限 =
管理员」——那就等于任何账号把自己部门清空就能自升管理员）。非管理员一律 `403 {"detail":"需要管理员权限：
当前账号没有管理员标识"}`，匿名 `401`——返回 403 而不是空数据，是为了不让「看不到」被误读成「没有」。
管理员标识与数据范围是两件事：被收窄到某个部门的管理员照样进得来，只是数据只有那一个部门。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/overview` | 管理概览：计数、完整性体检、**季/年落库覆盖（`rollups`）**、最近批次、接口观测（含**慢接口告警** `observability.slow_paths`） |
| GET | `/admin/agent-usage?days=7` | **全员** Agent 用量（`days` 取 1..365，越界 422，窗口口径与 `GET /ai/agent/usage` 完全一致：业务时区的自然日、含今天）：`{data,series,status_labels}`，比 `GET /ai/agent/usage` 多一份按用户的 `data.by_user`（每行 `{user_id,username,department,answers,total_tokens}`，未受限账号的 `department` 是「不受限」）。管理概览页的「Agent 用量」卡就走它，**它挂掉不该影响整页概览** |
| GET | `/admin/rollups?grain=quarter\|year` | 季度/年度落库清单：`{data, coverage, grains:[{value,label}]}`；每行含 `grain/period/period_label/department/department_label/months/covered_months/missing_months/complete/note/computed_at` 与金额比率 |
| POST | `/admin/rollups/rebuild` | 重算落库：`{"grains":["quarter","year"], "periods":["2026-Q3","2026"]}`（都可省略＝全部）；写审计 `rollups.rebuild`；`grains` 非季/年 → 422，`periods` 里塞月口径 → 422 |
| GET | `/admin/users?query=&department=&unrestricted=&limit=&offset=` | 用户列表（含 `is_admin`、`is_self`、`token_version`、登录态三个字段 `failed_attempts`/`locked_until`/`locked`、`total`、`limit` 上限 500，以及筛选下拉用的 `departments` 与 `unrestricted_count`） |
| PATCH | `/admin/users/{id}` | `{"department":"财务部"}` 相当于 `{"departments":["财务部"]}`；`{"departments":[]}`/空串 = **清空范围**（看不到任何数据，**不再**升级为管理员）；`{"is_admin":true\|false}` 单独发才授予/收回管理员标识。**只发 `is_admin` 时部门一个字都不动**（按 `model_fields_set` 判断客户端到底发没发部门字段）；`department_change`/`admin_change` 两条审计各自独立。收回/删除**最后一个管理员** → 400「不能…：收回后这个库再没有人能管」 |
| POST | `/admin/users/{id}/unlock` | 解除账号锁定：清掉 `failed_attempts` 与 `locked_until`，返回 `{"id","username","failed_attempts":0,"locked_until":null,"locked":false,"cleared_attempts":N}`；留 `user.unlock` 审计（detail 含 `cleared_attempts`、`was_locked_for`、`effect`）。**不动口令、不动 `token_version`** |
| POST | `/admin/users/{id}/logout` | 强制下线：把该账号的会话版本 +1（它此前签发的令牌全部立刻失效），返回 `{"id","username","token_version","logged_out":true}`；留 `user.force_logout` 审计（含 `from_version`/`to_version`）。对自己操作复用 `_target_user()` 的 **400**（想让自己下线用 `/auth/logout`，两者审计语义不同） |
| DELETE | `/admin/users/{id}` | 删除账号（只删登录凭据，不动财务数据）；**最后一个管理员删不得** → 400「不能删除最后一个管理员：删掉后这个库再没有人能管」 |
| GET | `/admin/batches?limit=&offset=` | 按 `import_batch` 分组的批次列表（`total` 是批次个数，`limit` 上限 200） |
| DELETE | `/admin/batches/{batch}` | 撤销批次：删明细 + 重算或清空受影响期间 |
| GET | `/admin/knowledge?source=&document_type=&limit=&offset=` | 按来源聚合的知识库片段（一行 = 一个来源；返回 `total` 来源数、`matched_chunks` 片段数、`all_chunks` 全库片段数、`document_types`，`limit` 上限 500） |
| DELETE | `/admin/knowledge?source=` | 按来源整份删除片段，并清理**没有别的来源再引用**的上传件（返回 `{"source","deleted_chunks","removed_files"}`；`stored_path` 落在 `rag_upload_dir` 之外的一律不删） |
| GET | `/admin/alert-rules` | 预警规则一览（含 `comparators`、指标中文名 `metrics`、级别 `levels`、分档选项 `departments`/`periods`）；首次访问会补齐默认两条 |
| POST | `/admin/alert-rules` | 新建**分档规则**：`{alert_type, department?, period?, metric_name, comparator, threshold_value, level, require_metric?, description?, enabled?}`，成功 **201** |
| PUT | `/admin/alert-rules/id/{rule_id}` | 按 id 改一条规则（默认与分档同一端点）：`{department?, period?, metric_name?, comparator?, threshold_value?, level?, require_metric?, description?, enabled?}` |
| DELETE | `/admin/alert-rules/id/{rule_id}` | 删一条**分档**规则；默认规则 → 400 |
| PUT | `/admin/alert-rules/{alert_type}` | 兼容入口：只改**默认规则**（全公司 · 所有期间）：`{threshold_value, level, enabled, description}` |
| GET | `/admin/alert-channels` | 外发通道一览（含 `kinds`/`min_levels`/`levels`/`departments` 与按通道分组的投递计数 `stats`）。**任何响应都不含 `secret`**，只给 `has_secret` |
| POST | `/admin/alert-channels` | 新建通道：`{name, kind:"webhook"\|"email", target, min_level?, department?, enabled?, secret?}`，成功 **201**；重名 **409**；webhook 地址非法（非 http/https、带用户名口令、没主机名）或收件人不像邮箱 → **422** |
| PUT | `/admin/alert-channels/{channel_id}` | 改通道：字段都可省略（`None` = 不改，**`secret:""` = 清空密钥**）；`kind` 变了就按新类型重校 `target`；重名 **409**，不存在 **404** |
| DELETE | `/admin/alert-channels/{channel_id}` | 删通道，**投递台账保留**（`channel_id` 置空 + 台账里存了通道名快照）；不存在 **404** |
| POST | `/admin/alert-channels/{channel_id}/test` | 同步发一条测试通知（`event="alert.channel_test"`），**不写投递台账**：`{"ok","status","detail","duration_ms","channel"}`；**发不出去也是 200**（`ok:false` + `detail` 里是真实原因），因为「通道不通」是配置问题不是请求问题 |
| GET | `/admin/alert-deliveries?status=&channel_id=&period=&level=&limit=&offset=` | 投递台账（倒序，返回 `data/total/limit/offset/status_labels/levels/stats`，`limit` 上限 200）；`status`/`level` 不在枚举内 → **422** |
| POST | `/admin/alert-deliveries/{delivery_id}/retry` | 人工重发一条：放回 `pending` 并把 `attempts` 归零，随即提交 `alert.dispatch` 任务（`{"data","job","queued","deduplicated"}`）；不存在 **404** |
| POST | `/admin/alerts/notify` | 补发：把库里**还没处理**（`review_status="pending"`）的预警按启用通道补建投递单（`{"period"?}` 可只补某一期），返回 `{"period","created","queued","job","deduplicated"}`；先落库再排队 |
| GET | `/admin/audit?action=&actor=&target_type=&ip=&since=&until=&limit=&offset=` | 审计日志（倒序，返回 `data/total/limit/offset/actions/actors/ips/retention_days/export_limit`） |
| GET | `/admin/audit/export?action=&actor=&target_type=&ip=&since=&until=` | 导出审计 CSV（带 BOM，Excel 可直接打开） |
| POST | `/admin/audit/purge` | 按保留期清理审计（默认 `dry_run` 只报数，真删留痕） |
| POST | `/admin/jobs/batch-revert` | `{"batch":"…"}`，提交撤销批次的后台任务，立即 **202** 返回任务（`status="queued"`）；批次不存在 → 404；可选请求头 `Idempotency-Key`（≤200 字符，超长 422），重放同一个键拿回同一条任务并带 `deduplicated: true` |
| GET | `/admin/jobs?status=&kind=&limit=&offset=` | 后台任务列表（倒序，返回 `data/total/limit/offset/kinds/statuses`，`limit` 上限 200） |
| GET | `/admin/jobs/{job_id}` | 单条任务（不存在 → 404） |
| POST | `/admin/jobs/{job_id}/retry` | 重试失败的任务：放回 `queued` 并**追加**一份预算（`max_attempts = attempts + 3`），保留上次的 `error`；任务不存在 → 404，状态不是 `failed` → 409 |

### 后台任务

撤销批次要重算所有受影响期间，接口不再同步等着跑完：`POST /admin/jobs/batch-revert` 只往
`background_jobs` 表写一条 `queued` 记录并交给 FastAPI 的 `BackgroundTasks`，**202** 就返回
（`{"id","kind","kind_label","status","actor","payload","result","error","created_at","started_at","finished_at",
"idempotency_key","attempts","max_attempts","next_attempt_at","worker_id","deduplicated"}`）。
前端轮询 `GET /admin/jobs/{id}` 看 `status`：

| status | 含义 |
| --- | --- |
| `queued` | 已入队，还没开始（**提交成功 ≠ 已执行**，此时明细还在）；带着 `next_attempt_at` 说明是在等退避重试 |
| `running` | 正在删除明细并重算受影响期间；`worker_id` 写着**哪个进程**在跑它（形如 `主机名-pid-随机串`，⑱） |
| `succeeded` | `result = {"batch","deleted_rows","recalculated":[…],"cleared":[…]}`；重跑时若上次已经删干净，`deleted_rows=0` 且带 `already_reverted: true` |
| `failed` | `error` 是失败原因（截断 500 字）；只有**预算用完**（`attempts >= max_attempts`）或任务类型已经没有处理函数才会落在这里。重试一次会把 `max_attempts` 抬高 3 |

`status` 越界（不在 `queued/running/succeeded/failed` 里）→ 422，`limit=0`/`offset=-1` → 422；
`kind` 是任务类型（当前只有 `batch.revert`，中文名见 `kinds`）。同步的
`DELETE /admin/batches/{batch}` 仍然可用（脚本与 curl 方便），与后台任务调的是同一份
`backend/batch_ops.revert_batch()`。

**提交是幂等的**（⑩）：

- 同一批次已有 `queued`/`running` 的任务时，再提交不排第二条，返回那条老任务 + `deduplicated: true`；
- 带 `Idempotency-Key` 时按这个键去重，**`succeeded` 的也算命中**（「这件事已经做完了」正是客户端重试
  最想听到的答案），所以同一个键在批次已经被删掉之后仍然回同一条任务（不会变成 404）；
- 兜底是 `background_jobs` 上的**部分唯一索引** `uq_background_jobs_idempotency_key`
  （只约束 `queued/running/succeeded`）：先查后插在并发下仍有窗口，索引负责那一刻抛
  `IntegrityError`，代码捕获后回头取那条已存在的任务。注意**不参与**指纹计算的字段是 `payload.ip`
  —— 它每请求都在变，拿它当身份会让同一次撤销被排两遍。

**失败会退避重试**（⑩）：处理函数抛异常时，还有预算就放回 `queued` 并写
`next_attempt_at = now + 2·2^(n−1) 秒`（2、4、8…，封顶 60），到点由进程内的调度器再跑；
`attempts` 每次执行加一（从 1 开始计），`error` 记为「第 N 次尝试失败：…」。
**重复执行必须安全**：撤销任务把 `context["is_retry"]`（`attempts > 1`）传给
`revert_batch(allow_missing=…)`，重跑时「一行都没删」判成功并标 `already_reverted`。

**进程重启接续**（⑩，⑱ 起跨进程安全）：启动时 `jobs.resume_interrupted_jobs()` 把遗留的 `queued`/`running` 放回队列
（`error = "进程重启，任务已重新排队（下次是第 N 次尝试）"`，保留已用掉的次数）并立刻调度一次；
预算用完的判 `failed`（`error` 点名用了几次），`kind` 已无处理函数的判 `failed`
（`error = "进程重启，且这个任务类型已经没有处理函数"`）。

**跨进程执行权**（⑱）：每条在跑的任务由一把 Postgres **会话级 advisory 锁**代表（命名空间 `finance:job-claim`），
`worker_id` 字段记着执行它的进程。两道护栏：

- **执行入口**：`jobs.run_job(job_id)` 先抢执行权锁，抢不到直接安静返回（`status` 不动、`attempts` 不加）；
  抢到后再用**条件 UPDATE**（`WHERE id = … AND status = 'queued'`）把行改成 `running`，`rowcount != 1` 就当没抢到。
  所以两个副本同时拿到同一条任务在数据库那一层不可能发生，处理函数只会跑一遍。
- **启动接续**：接续本身用一把全局锁串行化，抢不到的副本返回 `{"requeued": [], "failed": [], "still_running": [],
  "skipped": true, "total": 0}`、**一个字都不改**；对每条 `running` 行先探它的执行权锁——抢得到说明持有它的进程
  已经死了（连接断开、锁由数据库释放）→ 才重排并清空 `worker_id`/`started_at`；抢不到说明那个副本还活着 →
  原样保留，只列进返回值的 `still_running`。以前这里一律重排，于是第二个副本一启动就把别人的活又排了一遍。
- **advisory 锁的代价**（脚本 `scripts/verify_job_claim.py` 里固定成一条检查）：锁挂在**会话**上，
  池化连接 `close()` 只是把连接还回池子、锁还在——所以 `jobs` 里所有取锁的地方都在 `finally` 里
  显式 `pg_advisory_unlock`，不靠关连接。

**边界**：任务仍然只在**提交它的那个进程**里执行（不会漂到别的副本），重试定时器也活在进程内存里
（重启即丢，靠启动接续补回来），多副本部署需要换成外部队列。

### 列表分页与筛选（`/admin/*` 与 `/analysis/alerts/all`）

这几个接口共用同一套约定：

```json
{ "data": [ ... ],
  "total": 137,        // 筛选后的总行数，与 limit 无关（不是 len(data)）
  "offset": 50, "limit": 50 }
```

- `limit` 有硬上限（用户/知识库 500、批次 200、预警与审计 200，`/analysis/alerts/{id}/transactions` 2000），
  `limit=0`、`limit=501`、`offset=-1` 一律 **422**（Pydantic 校验，不在函数体里夹取）。
- `total` 必须是 `count(*)`（或 `count(distinct 来源/批次)`），**不能用当前页条数冒充**：否则列表一旦被
  `limit` 截断，「共 N 条」就变成每页条数，翻页按钮也永远只有一页。
- 筛选一律发生在分页之前。`/analysis/alerts/all` 的 `status`、`/admin/knowledge` 的 `document_type`
  都必须进 `where`（知识库的条件还要在 `group_by` 之前），前端不允许拿回一页数据再 `filter`。
- 筛选下拉的选项由后端给（`departments`/`unrestricted_count`、`document_types`、`actions`/`actors`），
  前端从当前页数据里凑选项会在翻页后自相矛盾。
- `GET /admin/users` 的 `department=` 匹配该部门，`unrestricted=true|false` 分别匹配「未限定部门」与
  「已限定部门」（兼容 `department` 为 `NULL` 或空白字符串两种历史写法）。


`PUT /admin/alert-rules/id/{rule_id}` 的响应（`POST` 返回同一形状，`GET` 的 `data[]` 也是）：

```json
{ "id": 8, "alert_type": "成本率异常", "department": "018", "period": "2026-10",
  "scope_label": "018 · 2026-10", "is_default": false,
  "metric_name": "cost_ratio", "metric_label": "成本率（成本 ÷ 收入）",
  "comparator": "gt", "comparator_label": "大于", "threshold_value": "0.5000",
  "level": "high", "require_metric": "revenue_amount", "description": "成本占收入超过 50%",
  "enabled": true, "updated_at": "2026-10-08 15:42" }
```

- 不存在该规则 → 404「没有这条预警规则」；`level` 只接受 `high|medium|low` → 否则 422；
  `|threshold_value| >= 1e16` → 422「阈值超出可表示范围」。
- **改完只对下一次重算生效，不追溯改写已有预警**：历史预警是当时口径下的判断，追改会让当月报告与当时的
  结论对不上。`require_metric` 为空或 `enabled=false` 时该规则不参与判定。
- 规则形状（比哪个指标、`gt`/`lt`）不开放编辑——平台只有「某指标超过/低于阈值」这一种真实需求，
  做成表达式编辑器反而无法验证。

### 预警规则分档（同一个 `alert_type` 多条共存）

`alert_type` **不再唯一**：同一个类型可以按**部门**、按**期间**各配一条阈值。一次重算里，对每个
`(期间, 部门)` 只取**最具体的一条**——优先级 `部门+期间(2) > 只限一个维度(1) > 默认规则(0)`，
同级按 `id` 小的胜出；**命中即覆盖，不叠加**（`backend/alert_rules.py` 的 `scope_rank` / `resolve_rules`，
返回时按 `(alert_type, id)` 排序）。所以「部门 018 在 2026-10 从严」不会顺手改掉兄弟部门或其它月份的口径。

| 情况 | 结果 |
| --- | --- |
| `POST` 的 `department` 与 `period` 都空 | **422**「分档规则至少要限定部门或期间之一；全公司全期间的阈值请改默认规则」 |
| `period` 不是 `YYYY-MM` | 422（Pydantic 的 `string_pattern_mismatch`） |
| `metric_name`/`require_metric` 不在允许集合 | 422「metric_name 只能是 …」 |
| 同「类型 + 部门 + 期间」已存在 | **409**「已有同样范围的「…」规则（018 · 2026-10），请直接改那一条」 |
| `PUT .../id/{id}` 把分档规则改成两个维度都空 | **422**「…要回到全公司口径请新建默认规则」 |
| `PUT .../id/{id}` 改成与另一条同范围 | **409** |
| `DELETE .../id/{id}` 指向默认规则 | **400**「全公司口径的默认规则不能删除，请改用停用（enabled=false）」 |

- 默认规则**只能停用不能删**：删了那个 `alert_type` 就彻底不报了，而「不报」和「停用」在页面上看起来
  一样、后果差很远。
- 改动留痕：`alert_rule.create` / `alert_rule.update` / `alert_rule.delete` 三条审计动作，detail 里带
  `scope`（如 `018 · 2026-10`）与改前改后快照。
- **老库升级**要同时补 `alert_rules.department`/`period` 两列，并去掉 `alert_type` 的唯一性。注意模型里的
  `unique=True, index=True` 在 Postgres 里是 **UNIQUE INDEX**，`information_schema`/`pg_constraint` 都查不到，
  得按 `pg_indexes.indexdef` 判断再 `DROP INDEX` + `CREATE INDEX`（`migrations.py` 的 `PLAIN_INDEXES`），
  否则插入第二条同类型的分档规则会 `UniqueViolation: duplicate key value violates unique constraint
  "ix_alert_rules_alert_type"`。

`GET /admin/overview` 返回：

```json
{
  "counts": {"users": 5, "admins": 5, "transactions": 8909, "metrics": 7, "alerts": 7,
             "knowledge_chunks": 5, "knowledge_sources": 3, "batches": 1},
  "periods": {"latest": "2026-10", "all": ["2026-10", "..."]},
  "recent_batches": [{"batch": "1e0d43e5…", "short": "1e0d43e5b8af", "rows": 8909,
                      "first_date": "2026-04-01", "last_date": "2026-10-02",
                      "imported_at": "2026-10-08 14:20:11",
                      "periods": ["2026-04", "..."]}],
  "quality": [{"name": "有明细但缺指标的期间", "value": 0, "level": "ok",
               "hint": "指标与明细一一对应"}],
  "observability": {"requests": {"200:/api/v1/health": 3}, "total": 12, "p95_duration_ms": 309.05,
                    "paths": {"/api/v1/analysis/calculate": {"count": 2, "p95_ms": 412.3, "max_ms": 501.8}},
                    "samples_per_path": 200,
                    "slow_default_ms": 1000, "slow_overrides": {"/api/v1/ai/rag/chat": 15000},
                    "slow_log_seconds": 60,
                    "slow_paths": [{ "path": "/api/v1/ai/rag/chat", "threshold_ms": 15000,
                                     "exceeded": 2, "suppressed": 1, "worst_ms": 16179.58,
                                     "last_ms": 16179.58, "last_at": "2026-10-08T11:22:25+00:00" }]},
  "me": {"id": 16, "username": "demo_analyst", "department": ""}
}
```

- `periods` 来自 `financial_metrics`（不是明细），所以「导入了但还没算指标」的期间不会出现在这里，
  而是会出现在 `quality` 的「有明细但缺指标的期间」里——这正是这两处要分开看的原因。
- `quality[].level` 取 `ok`/`warn`；`value` 是命中数量，`hint` 为 0 时给的是「正常」说明。
- `observability` 是进程内计数，重启即清零；`p95_duration_ms` 只统计最近 1000 次采样。
  `requests` 按 `状态码:路径` 分桶计数，`paths` 给出**每条路径**的 `count` / `p95_ms` / `max_ms`
  （每路径只留最近 `samples_per_path` 次采样）——「哪个接口慢」看 `paths`，不要拿全局 P95 猜。
  `GET /api/v1/observability/metrics` 返回同一份 `snapshot()`，无需登录。
  慢接口部分是**主动告警**而不是事后统计：`slow_paths` 只收「单次耗时 ≥ 该路径阈值」的请求，
  `threshold_ms` 是这条路径实际生效的阈值（`slow_overrides` 里没有它就回落 `slow_default_ms`），
  `exceeded` 是累计超阈值次数、`suppressed` 是按 `slow_log_seconds` 窗口被压掉（只记账不写日志）
  的次数，`worst_ms`/`last_ms`/`last_at` 是最慢与最近一次。每超一次阈值，后端还会写一条
  `WARNING 慢接口告警 path=… duration_ms=… threshold_ms=… 累计超阈值=… 次` 到应用日志。

`DELETE /admin/batches/{batch}` 返回 `{"batch","deleted_rows","recalculated","cleared"}`：
对每个受影响期间，还有剩余明细就重算指标与预警（`recalculated`），没有就一并删掉（`cleared`）。
批次不存在或已被撤销返回 `404`。

`POST /admin/jobs/batch-revert` 返回 `202` + 任务对象（字段同 `GET /admin/jobs/{id}`）：

```json
{ "id": 20, "kind": "batch.revert", "kind_label": "撤销导入批次", "status": "queued",
  "actor": "demo_analyst", "payload": {"batch": "verify-jobs-42246f", "ip": "127.0.0.1"},
  "result": null, "error": null, "worker_id": null,
  "created_at": "2026-10-08T08:23:17.7Z", "started_at": null, "finished_at": null }
```

跑起来之后 `status` 变 `running` 且 `worker_id` 有值（⑱，`主机名-pid-随机串`），落终态时又清回 `null`：

```json
{ "id": 20, "status": "running", "worker_id": "finance-host-4242-ab12cd",
  "attempts": 1, "started_at": "2026-10-08T08:23:18.1Z", "next_attempt_at": null }
```

执行完 `result` 与 `DELETE` 的响应同形（`{"batch","deleted_rows","recalculated","cleared"}`），
审计里那条 `batch.revert` 的 `detail["job"]` 就是这里的 `id`——界面上的任务和审计能互相对上。

`GET /admin/jobs?limit=5` 的形状：

```json
{ "data": [ { "id": 20, "kind": "batch.revert", "kind_label": "撤销导入批次",
              "status": "succeeded", "actor": "demo_analyst", "worker_id": null,
              "result": {"batch": "verify-jobs-42246f", "deleted_rows": 1,
                         "recalculated": ["2026-10"], "cleared": []},
              "error": null, "created_at": "…", "started_at": "…", "finished_at": "…" } ],
  "total": 2, "offset": 0, "limit": 5,
  "kinds": [{"value": "batch.revert", "label": "撤销导入批次"}],
  "statuses": [{"value": "queued", "label": "排队中"}, {"value": "running", "label": "运行中"},
               {"value": "succeeded", "label": "已完成"}, {"value": "failed", "label": "失败"}] }
```

管理接口的两个刻意约束（都有测试覆盖）：

- **不能改自己/删自己**（`400`）：清空或收回自己的标识会立刻把自己锁在管理入口之外，属自伤操作，
  要调整只能**换另一个管理员**来改（⑳ 起连 `PATCH /auth/me` 这条自助路径也封了：一律 403，
  System 设置页上的「当前账号 / 数据范围 / 管理员标识」三格只读，只看不动）。
  另外**最后一个管理员收不掉也删不掉**（`400`），否则这个库再没有人能管；
  真到了那一步用 `python scripts/grant_admin.py <用户名>` 从命令行救回来。
- **撤销批次必须重算期间**：只删明细不重算会在库里留下与明细对不上的历史指标（幽灵报表）。

### 预警外发通道（Webhook / 邮件）

预警先生成在库里，再由**通道**推出去。三条容易踩的设计取舍：

- **去重键是内容，不是行号**：`alert_deliveries.alert_key = "期间|部门|预警类型|指标名"`，
  与 `channel_id` 一起唯一。重算会**先删后建**预警行（行 id 每轮都变），按行 id 去重等于每重算一次
  就把同样的通知再轰一遍。台账里还存了当时的 **payload 快照**：重试时预警行可能已经被删掉了。
- **发不出去交给后台任务退避重试**：每次尝试 `attempts += 1`，按 `2^(n-1)` 秒退避（上限 60 秒，
  默认 3 次预算，`ALERT_MAX_ATTEMPTS` 可调）。处理函数只发「还没成功」的单子，成功与否都先提交，
  再抛异常让任务自己重排——所以「部分失败」不会把已经送达的重发一遍。
- **人工重试归零 `attempts`**：与后台任务 `prepare_retry` 的「追加预算、保留编号」口径不同——
  管理员点重发是因为「通道已经修好了」，这时候还卡着历史次数没有意义。

出口侧的安全取舍（刻意为之）：只允许 `http`/`https`、**地址里不许带用户名口令**（会进日志与台账）、
**不跟随 3xx 跳转**（否则签名正文会被转发到任意主机）、5 秒超时、最多读 2 KB 响应；
**不封禁内网地址**——企业告警网关本来就在内网。配了 `secret` 的 webhook 会带
`X-Finance-Signature: sha256=…`（对 `"{unix 秒}.{正文}"` 做 HMAC-SHA256，`X-Finance-Timestamp` 是同源时间戳）。
邮件通道需要 `ALERT_SMTP_HOST` / `ALERT_MAIL_FROM`（可选 `ALERT_SMTP_STARTTLS`、`ALERT_SMTP_USER`/`_PASSWORD`），
没配就是 `ConfigurationError`：直接判失败、**不烧重试预算**。

### 审计日志

`GET /admin/audit` 返回（倒序，最新的在前）：

```json
{
  "data": [
    {"id": 42, "at": "2026-10-08T15:59:31+08:00", "actor": "demo_analyst", "department": null,
     "action": "alert.review", "action_label": "处理预警", "target_type": "alert", "target_id": "1482",
     "detail": {"period": "2026-10", "alert_type": "利润异常", "department": "全公司",
                "from": "pending", "to": "confirmed", "note": "接口冒烟：审计留痕"},
     "ip": "127.0.0.1"}
  ],
  "total": 5, "limit": 50, "offset": 0,
  "actions": [{"value": "user.login", "label": "登录成功"}],
  "actors": ["demo_analyst"],
  "ips": ["127.0.0.1"],
  "retention_days": 180,
  "export_limit": 5000
}
```

- 登记的动作（以 `backend/audit.py` 的 `ACTIONS` 为准，当前 **37** 条）：`user.register`、`user.login`、
  `user.login_failed`、`user.login_blocked`、`user.login_locked`、`user.unlock`、`user.register_rejected`、
  `user.department_self`（⑳ 起停用：不再有成功路径）、`user.department_self_denied`（⑳ 新增：自助改范围/
  标识被拒，detail 含 `attempted`）、`user.department_change`、`user.admin_change`（⑳ 新增：授予/收回管理员
  标识，detail 记 `from`/`to`）、`user.delete`、`user.logout`、`user.force_logout`、`data.import`、
  `data.export`、`transaction.update`、`transaction.reverse`、`metrics.calculate`、`rollups.rebuild`、
  `batch.revert`、`job.retry`、`knowledge.add`、`knowledge.delete`、`alert.review`、
  `alert_rule.update`、`alert_rule.create`、`alert_rule.delete`、`alert_channel.create`、
  `alert_channel.update`、`alert_channel.delete`、`alert_channel.test`、`alert_delivery.retry`、
  `alert.notify`、`agent_session.delete`、`audit.export`、`audit.purge`。
  写未登记的动作不会失败，但会在服务端打 warning。
- `action` / `actor` / `target_type` 都是精确匹配；`ip` 是**前缀匹配**（前端下拉直接给整串，
  手工传 `127` 这种前缀也能用）；`limit` 默认 50，`offset` 用于翻页，`total` 不受 `limit` 影响
  （`total` 是筛选后的总行数，筛选发生在 count 与 limit 之前）。
- `since` / `until` 是**日期**（`YYYY-MM-DD`，含首尾两天，按**业务时区**的日历日——`BUSINESS_TIMEZONE`，
  默认 `Asia/Shanghai`，不是服务器本机时区）：窗口取 `[since 00:00, until+1 00:00)` 再换算成 UTC 比较，
  所以 UTC+8 的凌晨（那一刻机器本机 UTC 还停在昨天）筛「今天」既不会漏也不会多。只给 `since` 表示
  「从那天的 00:00 起」。**这一口径只管筛选边界**：行内的 `at` 是带偏移的显式 UTC、导出 CSV 的表头
  也是「时间(UTC)」，审计页把行内时刻按**浏览器本机时间**显示、列头写「时间（本机）」。
- `ips` 是最近出现过的来源 IP（空 IP 不进下拉），`retention_days` 是当前保留期，
  `export_limit` 是导出的单次上限——三个字段都由后端给，前端不写死。
- **detail 已脱敏**：键名含 `password`/`token`/`secret`/`authorization`/`api_key`/`apikey`/`hash` 的字段
  在任何嵌套深度都会被丢掉，字符串截断到 200 字，列表最多留 50 项。审计里**不会有**密码、令牌、
  文件内容与明细金额。
- **审计行没有改/删接口**（`GET` 只有这一个），唯一的删除路径是下面这节的保留期清理。
- 非管理员 `403`，匿名 `401`。审计写入与业务写在同一个事务里，业务回滚时日志不落地；
  写日志自身失败只记 error，不会让业务接口失败。

### 审计导出与保留期清理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/audit/export?action=&actor=&target_type=&ip=&since=&until=` | 按同一套条件导出 CSV（`text/csv; charset=utf-8`） |
| POST | `/admin/audit/purge` | 按保留期清理，默认 `{"dry_run": true}` 只报数 |

`GET /admin/audit/export` 的响应头：

```
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="audit-20261008-090616.csv"
X-Export-Rows: 75
X-Export-Truncated: false
```

正文以 **UTF-8 BOM** 开头（Excel 靠它认编码，否则中文列名是乱码），列为
`时间(UTC),操作人,部门,动作,动作标签,对象类型,对象ID,详情,IP`；`详情` 是压成一行的 JSON
（`ensure_ascii=False`、键排序），时间用 ISO-8601 UTC——导出的表格是拿去对数、进底稿的，
含混的本地时间比时区偏移更容易出错。一次最多 `export_limit`（5000）行，超出时
`X-Export-Truncated: true`，按时间或动作分段再导。**导出会写一条 `audit.export`**，
detail 里记导出条数与当时的筛选条件。

`POST /admin/audit/purge` 的请求体与响应：

```json
{"keep_days": 180, "dry_run": true}
{"dry_run": true, "keep_days": 180, "before": "2026-04-11T09:06:17+00:00", "deleted": 1, "total": 77, "sample_id": 3029}
```

- `keep_days` 省略时用 `AUDIT_RETENTION_DAYS`（默认 180）；范围 `1..3650`，`keep_days=0` → `422`
  （**没有「保留 0 天」= 清空这条捷径**）。
- `dry_run=true` 只统计不删除；`dry_run=false` 才真删，且**清理自己写一条 `audit.purge`**
  （detail：`keep_days`/`before`/`deleted`）。清理动作本身必须可查，否则它就是审计的漏洞。
- 非管理员 `403`。
