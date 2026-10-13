# 本次验证记录（2026-10-11）

## 第二轮：权限、时间解析与真实模型评测

- 知识库权限19项真实数据库测试通过，覆盖文本/文件写入、普通账号、部门管理员、公共发布与同名来源跨部门删除。
- 相关后端回归35项通过（包含上述19项）；前端56项通过（含新增上传交互）；时间与重排离线回归13项通过。
- 新上传页与问答页均检查375、768、1440宽度，无横向溢出；新上传页截图已查看。
- 真实模型30题基线及T02定向复测已执行，见 [评测及复核](ai-evaluation-results.md)。基线发现的上下文误导已修正，原始回答保留。
- 新增 `/rag/write-scope`，文本与文件上传要求明确资料范围；接口快照已更新。
- 生产构建通过。Windows系统临时目录出现esbuild清理权限问题时，本次将进程的 `TEMP` 与 `TMP` 指向 `frontend/.build-tmp` 后构建成功；未修改系统级环境变量。构建仍有既有大文件提示，未进行包体优化。

以下保留第一轮的历史验证记录，其“未验证项”不代表第二轮状态。

## 已通过

- 离线回归 10 项：明确月份缺失、默认最新期、双月份解析与证据、指定重排模型、空/无效重排结果、失败回退、PostgreSQL查询权限条件。
- 真实数据库及 ASGI 接口回归 13 项：RAG 权限、向量模型标识、限流、SSE、会话归属与落库、停止生成、缺失月份零模型用量、真实向量查询在权限范围内选 Top K。
- 修改文件的 Ruff 检查及 `git diff --check`。
- 本地启动 FastAPI 与 Vite；真实登录后查看 `/agent` 在 375×812、768×1000、1440×900 的截图，均无横向溢出。截图位于 `output/charts/check_agent_*.png`。

## 复现命令

```powershell
.\.venv\Scripts\python.exe scripts/test_ai_reliability.py
.\.venv\Scripts\python.exe -m pytest tests/test_api.py tests/test_ai_retrieval.py -k 'rag or agent or department_filter_precedes_top_k' -q
.\.venv\Scripts\python.exe scripts/check_pages.py --pages /agent --sizes 375x812 768x1000 1440x900 --viewport-only
```

数据库使用项目已有 `finance-postgres` 容器；接口测试包含真实 PostgreSQL 与模型替身。新增向量数据由事务回滚，流式接口测试使用仓库既有的独立测试数据库管理器。页面验收需要本地服务与演示账号。

## 未验证项

未执行全仓库测试或30项真实模型质量评测；未测重排对真实问题准确率的提升、费用或延迟。上述回归通过不能用作模型准确率结论。未实现自主工具规划、OCR、复杂财报表格解析或事件游标断点续传。
