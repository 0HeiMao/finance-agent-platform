# API 接口契约

Base URL：`http://localhost:8000/api/v1`

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
| POST | `/auth/register` | 请求 `{"username","password"}`；用户名重复返回 409 |
| POST | `/auth/login` | 请求 `{"username","password"}`（JSON，非 OAuth2 表单）；返回 `{"access_token","token_type"}` |
| GET | `/auth/me` | 返回 `{id,username,department,departments,unrestricted}`；**权威值是数组 `departments`**，`department` 只是它的第一个元素（回落用），`departments` 为空表示不受限（财务管理员） |
| PATCH | `/auth/me` | 请求 `{"departments": ["018","097"]}`（字符串形式 `{"department":"018"}` 仍兼容，≤100 字；`[]` 与空串等于清空）；部门只由服务端记录 **`User.departments`** 决定，最多 20 个 |

登录有**滑动窗口限流**（默认 10 次 / 300 秒，按「用户名 + 来源 IP」分桶，配置项 `LOGIN_MAX_ATTEMPTS` /
`LOGIN_WINDOW_SECONDS`）：超过后即使密码正确也返回

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 213

{"detail":"登录尝试过于频繁，请在 213 秒后重试"}
```

**被拒的尝试也计数**（否则爆破方只要保证成功一次就能重置窗口），登录成功后该桶清零。
`AUTH_SECRET_KEY` 留空或仍是占位值 `change-this-secret-in-env` 时，进程启动会用 `secrets.token_urlsafe(48)`
生成一个并**就地写回 `.env`**（保留其它行、不重复追加键）：占位密钥意味着任何人都能自己签一个合法 JWT。
副作用是密钥一变，之前签发的 token 全部失效，客户端需要重新登录。

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
- **429 不写审计**：一条被拒请求写一条审计等于给攻击者一个「用便宜请求写满审计表」的放大器；
  观测性里按 `429:路径` 计数，`GET /admin/overview` 的 `observability.requests` 能看到。
- **边界**：与登录限流同一实现（`backend/ratelimit.py`，进程内滑动窗口、无 Redis），重启清零、不跨副本。

### 慢接口告警（按路径耗时阈值）

配额管的是「请求太多」，阈值管的是「单次太慢」。没有阈值就必须有人盯管理页；有了阈值，
超标的请求自己写一条 `WARNING` 到应用日志：

| 项 | 默认 | 配置项 | 说明 |
| --- | --- | --- | --- |
| 全局阈值 | 1000 ms | `SLOW_PATH_MS` | 单次耗时 ≥ 阈值即计入 `slow_paths` |
| 按路径覆盖 | 空 | `SLOW_PATH_OVERRIDES` | 形如 `/api/v1/ai/rag/chat=15000,/api/v1/ai/agent/chat/stream=300000`；坏条目逐条跳过、不让服务起不来 |
| 同路径告警间隔 | 60 秒 | `SLOW_PATH_LOG_SECONDS` | 窗口内的后续超阈值请求只记账（`suppressed`）、不重复写日志 |


> 部门不要走请求头自述：早期实现用 `X-User-Department: finance-admin` 当管理员凭证，属于可伪造提权，
> 现在只有「用户自己没设部门」时才兼容这个旧头，其他情况一律忽略（见 `backend/api_rag.py: user_department_of`）。
> 财务数据也认**服务端记录的 `User.department`**，但不认请求头：`backend/api_ai.py` 里知识库片段允许走旧头
> 兼容路径，而财务证据一律用 `scope_of(user)`，否则等于把「看全公司还是看本部门」的开关交给调用方。

## 数据范围（scope）

一次 `calculate` 会写两套数据：**全公司口径行**（`department IS NULL`）与**各部门口径行**（`department` 有值）。
所有读 `financial_metrics` / `financial_alerts` 的查询都必须显式选一套，否则会把部门行与汇总行加总
（不会报错，只是数字悄悄变大）。过滤集中在 `backend/scope.py`：

| 函数 | 行为 |
| --- | --- |
| `departments_of(user)` | 读 `User.departments`（JSONB 数组），为空时回落到单值列 `user.department`；返回去重保序的元组 |
| `scope_of(user)` | `tuple[str, ...] | None`；**空元组与 None 都表示不受限**（全公司），一个有值的元组 = 只看这几个部门 |
| `scope_metric(stmt, scope)` / `scope_alert(stmt, scope)` | `scope` 为 None 时加 `department IS NULL`，单元素退化 `department = d`，多元素 `department IN (…)` |
| `scope_transactions(stmt, scope)` | None 时不过滤；有值时 `FinancialTransaction.department IN (…)`（部门为空的行不属于任何部门） |
| `in_scope(scope, department)` | 逐行判断（预警这类已经取出来的对象用），None 恒真 |
| `metric_totals(rows, period_order=…)` | 按期间聚合：**单行期间沿用库里存的比率**，多行期间金额相加、比率按相加后的金额重算（比率不能相加） |
| `scope_payload(scope, requested=…)` | `{department,departments,label,unrestricted,note}`，随看板/分析/报告/Agent/系统状态响应一起返回 |
| `scope_label(scope)` / `scope_slug(scope)` / `scope_text(scope)` / `row_label(department)` | 文案与文件名的四种写法：「部门 018、097 等 N 个部门」/ 文件名的 `018-097` / 审计用的 `018、097` / 数据行级的「部门 097」。**`scope_label` 收的是元组，传字符串会按字符长度渲染成「部门 097 等 3 个部门」** |
| `resolve_requested_scope(user, requested)` | 解析请求里的 `department`：不受限账号可指定任意部门；受限账号只允许在自己绑定的部门里**收窄**，其他值返回违规说明（端点转成 403） |

> 多部门是「并集口径」而不是「取第一个部门」：多部门账号的看板/汇总/报告/Agent 证据/预测走的是同一份
> `metric_totals`，所以四个页面同一个数。跨期间的完整性判断用「每月行数 == 范围内部门数」
> （`_totals_by_months`），用 distinct 期间数会把缺一期数据当成完整、环比直接算错。

### 查看某个部门的口径（管理员切换视角）

`GET /dashboard`、`/analysis/summary`、`/analysis/alerts`、`/analysis/alerts/all`、`/reports/monthly`、
`/reports/docx`、`/reports/pptx*`、`/ai/agent/chat/stream`、`/export/*` 都接受可选的 `?department=<名称>`：

- **不受限账号**（`departments` 为空）：可以指定任意部门，响应里的 `scope` 会多一个 `"requested": true`
  并把 `note` 换成「正在按「部门 X」口径查看…不是全公司口径。」——用于区分「本来就不受限」与「主动切到了某部门」。
- **受限账号**：只能不带参数（＝自己绑定的全部部门，并集）或填**自己范围内的一个**部门收窄；
  填别人的部门一律 **403** `{"detail":"当前账号的数据范围是「部门 A、B」，不能查看「部门 C」"}`，
  而不是静默回落到自己的范围（静默回落会让人以为自己在看别人的数据）。
- 注意 `scope.unrestricted` 描述的是**这次响应覆盖的范围**是否为全公司，不是账号属性；
  「账号本身不受限吗」看 `/auth/me` 的 `unrestricted`（前端口径下拉也用它决定是否显示）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/analysis/departments` | 口径下拉的选项：`{"scope":…,"departments":[{"name":"097","label":"部门 097","transactions":1523}],"switchable":true}`；受限账号只会拿到自己绑定的那几个，`switchable` 只在**绑定数 > 1**（可收窄）或不受限时为 `true` |

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
| GET | `/export/transactions` | `period?`（省略=全部期间）、`account?`（科目名模糊匹配）、`department?`、`format` | 交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出,导入批次 |
| GET | `/export/metrics` | `period_from?`、`period_to?`、`department?`、`format` | 期间,口径,收入金额,成本金额,费用金额,毛利金额,净利润,毛利率,净利率 |
| GET | `/export/alerts` | `period?`（单期）或 `period_from?`/`period_to?`（区间，看板按「本年」展示时用）、`level?`、`department?`、`format` | 期间,口径,预警类型,描述,级别,指标,当前值,阈值,需人工复核,预警 id |
| GET | `/export/scope` | 无 | 给前端按钮用的口径说明：`{scope,formats:["csv","xlsx"],limit}` |

- `format` 只接受 `csv`（默认）或 `xlsx`，其他值 **422**。
- 响应头告诉调用方结果规模，前端据此提示，避免「以为导全了其实被截断」：

```http
Content-Disposition: attachment; filename="export.csv"; filename*=UTF-8''transactions-2026-07-097.csv
X-Export-Rows: 1523
X-Export-Truncated: true
Access-Control-Expose-Headers: X-Export-Rows, X-Export-Truncated, Content-Disposition
```

  上限 `EXPORT_LIMIT = 20000` 行，超过时置 `X-Export-Truncated: true`。
  中文文件名走 RFC 5987 的 `filename*`（HTTP 头只能是 latin-1），`filename=` 是同名 ASCII 兜底。
  文件名里的期间标记与筛选一致（`transactions-2026-07-097.csv`、`alerts-2026-04~2026-10-all.csv`），
  不要出现「写着全部期间、实际只导了一段区间」这种情况。
- CSV 是 `utf-8-sig`（带 BOM，Excel 直接双击不乱码）；**CSV 里一切皆文本**，避免 Excel 把 `2026-10`
  认成日期、把部门号 `097` 变成 `97`；**XLSX 里数字列是真数字**（能直接求和），文本列仍是文本。
- 导出永远只包含调用者口径的数据：受限账号导出不会出现别的部门，管理员导出的是全公司行，
  不会把 `department IS NULL` 与各部门行加总（这是这套两套行模型最容易悄悄出错的地方，回归脚本专门断言）。

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

返回 `{app,llm,database,embedding,rag,directories,data,me,config_problems}`：

- `app`：`env`/`is_development`/`strict_config`/`api_prefix`/`cors_origins`/`log_level`/`token_expire_minutes`。
- `llm`：`provider_configured`（**只报有没有配，不回显密钥**）、`base_url`、`model`、`rerank_model`。
- `database`：`ok`/`error`/`version`/`url`（口令打码为 `user:***@host`）/`pgvector`。
- `embedding`：`model`/`dimension`(384)/`using_fallback`。
- `rag`：`max_distance`/`candidate_k`。
- `directories[]`：`{name,path,configured,writable,error}`——`writable` 是真写一个探针文件再删的结果。
- `data`：`periods`/`latest_period`/`counts{transactions,metrics,alerts,knowledge_chunks,users}`。
- `config_problems[]`：复用启动时的 `Settings.validate()`（占位密钥、通配 CORS、非本机来源）。

## 知识库（RAG）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rag/documents` | 直接写入文本片段；内容与已入库片段完全一致时跳过，返回 `{success,ids,skipped}` |
| POST | `/rag/documents/upload` | 上传 txt/md/csv/pdf/docx；同来源同内容不重复入库，返回 `{success,source,chunks,skipped}` |
| POST | `/rag/search` | `{query,top_k}`，先多召回再重排 |
| POST | `/rag/answer` | `{query,top_k}`；按 `X-User-Department` 过滤（`finance-admin` 可见全部） |

## Agent 与问答

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| POST | `/ai/rag/chat` | `{"question":"..."}` | 知识库问答，返回 `{answer_status,answer,sources,embedding_model}` |
| POST | `/ai/agent/ask` | `{"question":"..."}` | 同上 |
| POST | `/ai/agent/chat/stream` | `{"question":"..."}` | SSE 流式 |

流式响应为 `text/event-stream`：

```text
event: delta\ndata: {"text":"本月"}\n\n
event: done\ndata: {"sources":[{"name":"财务汇总表 2026-07","kind":"financial_metric"}],"has_financial_data":true,"matched_period":"2026-07"}\n\n
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

## 管理员

所有 `/admin/*` 接口都经过 `admin_user` 依赖：**`User.department` 为空 = 不受部门限制 = 管理员**
（与 `GET /auth/me` 的 `unrestricted` 同一口径）。非管理员一律 `403 {"detail":"需要管理员权限：当前账号已限定部门"}`，
匿名 `401`——返回 403 而不是空数据，是为了不让「看不到」被误读成「没有」。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/admin/overview` | 管理概览：计数、完整性体检、**季/年落库覆盖（`rollups`）**、最近批次、接口观测（含**慢接口告警** `observability.slow_paths`） |
| GET | `/admin/rollups?grain=quarter\|year` | 季度/年度落库清单：`{data, coverage, grains:[{value,label}]}`；每行含 `grain/period/period_label/department/department_label/months/covered_months/missing_months/complete/note/computed_at` 与金额比率 |
| POST | `/admin/rollups/rebuild` | 重算落库：`{"grains":["quarter","year"], "periods":["2026-Q3","2026"]}`（都可省略＝全部）；写审计 `rollups.rebuild`；`grains` 非季/年 → 422，`periods` 里塞月口径 → 422 |
| GET | `/admin/users?query=&department=&unrestricted=&limit=&offset=` | 用户列表（含 `is_self`、`total`、`limit` 上限 500，以及筛选下拉用的 `departments` 与 `unrestricted_count`） |
| PATCH | `/admin/users/{id}` | `{"department":"财务部"}`；空字符串＝清空（升级为管理员） |
| DELETE | `/admin/users/{id}` | 删除账号（只删登录凭据，不动财务数据） |
| GET | `/admin/batches?limit=&offset=` | 按 `import_batch` 分组的批次列表（`total` 是批次个数，`limit` 上限 200） |
| DELETE | `/admin/batches/{batch}` | 撤销批次：删明细 + 重算或清空受影响期间 |
| GET | `/admin/knowledge?source=&document_type=&limit=&offset=` | 按来源聚合的知识库片段（一行 = 一个来源；返回 `total` 来源数、`matched_chunks` 片段数、`all_chunks` 全库片段数、`document_types`，`limit` 上限 500） |
| DELETE | `/admin/knowledge?source=` | 按来源整份删除片段，并清理**没有别的来源再引用**的上传件（返回 `{"source","deleted_chunks","removed_files"}`；`stored_path` 落在 `rag_upload_dir` 之外的一律不删） |
| GET | `/admin/alert-rules` | 预警规则一览（含 `comparators`、指标中文名 `metrics`、级别 `levels`、分档选项 `departments`/`periods`）；首次访问会补齐默认两条 |
| POST | `/admin/alert-rules` | 新建**分档规则**：`{alert_type, department?, period?, metric_name, comparator, threshold_value, level, require_metric?, description?, enabled?}`，成功 **201** |
| PUT | `/admin/alert-rules/id/{rule_id}` | 按 id 改一条规则（默认与分档同一端点）：`{department?, period?, metric_name?, comparator?, threshold_value?, level?, require_metric?, description?, enabled?}` |
| DELETE | `/admin/alert-rules/id/{rule_id}` | 删一条**分档**规则；默认规则 → 400 |
| PUT | `/admin/alert-rules/{alert_type}` | 兼容入口：只改**默认规则**（全公司 · 所有期间）：`{threshold_value, level, enabled, description}` |
| GET | `/admin/audit?action=&actor=&target_type=&ip=&since=&until=&limit=&offset=` | 审计日志（倒序，返回 `data/total/limit/offset/actions/actors/ips/retention_days/export_limit`） |
| GET | `/admin/audit/export?action=&actor=&target_type=&ip=&since=&until=` | 导出审计 CSV（带 BOM，Excel 可直接打开） |
| POST | `/admin/audit/purge` | 按保留期清理审计（默认 `dry_run` 只报数，真删留痕） |
| POST | `/admin/jobs/batch-revert` | `{"batch":"…"}`，提交撤销批次的后台任务，立即 **202** 返回任务（`status="queued"`）；批次不存在 → 404 |
| GET | `/admin/jobs?status=&kind=&limit=&offset=` | 后台任务列表（倒序，返回 `data/total/limit/offset/kinds/statuses`，`limit` 上限 200） |
| GET | `/admin/jobs/{job_id}` | 单条任务（不存在 → 404） |

### 后台任务

撤销批次要重算所有受影响期间，接口不再同步等着跑完：`POST /admin/jobs/batch-revert` 只往
`background_jobs` 表写一条 `queued` 记录并交给 FastAPI 的 `BackgroundTasks`，**202** 就返回
（`{"id","kind","kind_label","status","actor","payload","result","error","created_at","started_at","finished_at"}`）。
前端轮询 `GET /admin/jobs/{id}` 看 `status`：

| status | 含义 |
| --- | --- |
| `queued` | 已入队，还没开始（**提交成功 ≠ 已执行**，此时明细还在） |
| `running` | 正在删除明细并重算受影响期间 |
| `succeeded` | `result = {"batch","deleted_rows","recalculated":[…],"cleared":[…]}` |
| `failed` | `error` 是失败原因（截断 500 字）；进程重启遗留的任务由启动时的 `reap_stale_jobs()` 标成这个状态，`error = "进程重启，任务未完成"` |

`status` 越界（不在 `queued/running/succeeded/failed` 里）→ 422，`limit=0`/`offset=-1` → 422；
`kind` 是任务类型（当前只有 `batch.revert`，中文名见 `kinds`）。同步的
`DELETE /admin/batches/{batch}` 仍然可用（脚本与 curl 方便），与后台任务调的是同一份
`backend/batch_ops.revert_batch()`。**边界**：这是进程内队列，多副本部署需要换成外部队列。

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
  "result": null, "error": null,
  "created_at": "2026-10-08T08:23:17.7Z", "started_at": null, "finished_at": null }
```

执行完 `result` 与 `DELETE` 的响应同形（`{"batch","deleted_rows","recalculated","cleared"}`），
审计里那条 `batch.revert` 的 `detail["job"]` 就是这里的 `id`——界面上的任务和审计能互相对上。

`GET /admin/jobs?limit=5` 的形状：

```json
{ "data": [ { "id": 20, "kind": "batch.revert", "kind_label": "撤销导入批次",
              "status": "succeeded", "actor": "demo_analyst",
              "result": {"batch": "verify-jobs-42246f", "deleted_rows": 1,
                         "recalculated": ["2026-10"], "cleared": []},
              "error": null, "created_at": "…", "started_at": "…", "finished_at": "…" } ],
  "total": 2, "offset": 0, "limit": 5,
  "kinds": [{"value": "batch.revert", "label": "撤销导入批次"}],
  "statuses": [{"value": "queued", "label": "排队中"}, {"value": "running", "label": "运行中"},
               {"value": "succeeded", "label": "已完成"}, {"value": "failed", "label": "失败"}] }
```

管理接口的两个刻意约束（都有测试覆盖）：

- **不能改自己/删自己**（`400`）：清空或填写自己的部门会立刻把自己锁在管理入口之外，属自伤操作，
  需要自行调整时走 `PATCH /auth/me`（System 设置页的「我的部门」）。
- **撤销批次必须重算期间**：只删明细不重算会在库里留下与明细对不上的历史指标（幽灵报表）。

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

- 登记的 17 个动作：`user.register`、`user.login`、`user.login_failed`、`user.login_blocked`、
  `user.department_self`、`user.department_change`、`user.delete`、`data.import`、`data.export`、
  `metrics.calculate`、`batch.revert`、`knowledge.add`、`knowledge.delete`、`alert.review`、
  `alert_rule.update`、`audit.export`、`audit.purge`。写未登记的动作不会失败，但会在服务端打 warning。
- `action` / `actor` / `target_type` 都是精确匹配；`ip` 是**前缀匹配**（前端下拉直接给整串，
  手工传 `127` 这种前缀也能用）；`limit` 默认 50，`offset` 用于翻页，`total` 不受 `limit` 影响
  （`total` 是筛选后的总行数，筛选发生在 count 与 limit 之前）。
- `since` / `until` 是**日期**（`YYYY-MM-DD`，含首尾两天，按服务器本机日历日）：内部用
  `astimezone()` 转成带时区的时刻再比，所以 UTC+8 机器上「今天 8 点前的记录」不会漏。
  只给 `since` 表示「从那天的 00:00 起」。
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
