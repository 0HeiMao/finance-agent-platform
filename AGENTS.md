# 项目开发规范

## 技术栈
- 前端：Vue 3 + Vite + ECharts 5。
- 后端：Python FastAPI。
- API 前缀：`/api/v1`；所有业务数据必须由后端返回。
- 状态：页面局部状态使用 Vue `ref`/`computed`；跨页面状态统一放入 Pinia（引入后再使用）。

## 代码规范
- 组件使用 PascalCase 命名，组合式 API 使用 `<script setup>`。
- 所有请求集中在 `frontend/src/api/`，组件不得散落请求地址。
- 必须处理 loading、empty、error 三种状态；异步请求必须捕获异常并可取消或安全结束。
- 禁止 `any`（TypeScript 文件）；新增代码优先使用 TypeScript。

## 安全约束
- 严禁在前端硬编码 API Key、模型密钥或数据库凭据。
- 大模型、向量数据库和 Agent 请求必须通过后端 API 中转。
- 请求使用 Bearer Token；收到 401 时清除本地会话并跳转 `/login`。
- 展示模型输出时必须进行安全的文本渲染，禁止直接注入未经处理的 HTML。

## AI 交互
- 流式接口使用 SSE 或 Fetch `ReadableStream`，提供打字机效果、断线重连和停止生成按钮。
- 长耗时请求显示骨架屏或进度状态，禁止页面假死。
- 模型错误、超时、空响应和疑似幻觉结果都要显示友好提示。

## 验收尺寸
- 必须检查 375px、768px、1440px 三种宽度。
- 验收顺序：生成 → 代码审查 → 本地运行 → 视觉检查 → 接口联调。
