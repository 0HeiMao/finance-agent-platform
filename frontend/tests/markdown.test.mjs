/**
 * Markdown 渲染器的安全与结构测试。
 *
 * 最要紧的一条是「先转义、再解析」：报告页与 Agent 回答都用 v-html 渲染它的输出，
 * 一旦顺序反了，模型输出里的 <script> / <img onerror> 就成了真的 HTML。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { escapeHtml, renderMarkdown } from '../src/utils/markdown.js'

test('escapeHtml 转义五个 HTML 元字符', () => {
  assert.equal(
    escapeHtml(`<a href="x" title='y'>&</a>`),
    '&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;'
  )
  assert.equal(escapeHtml(null), '')
  assert.equal(escapeHtml(undefined), '')
})

test('renderMarkdown 把脚本与事件属性渲染成纯文本', () => {
  const html = renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror="alert(2)">')
  assert.ok(!html.includes('<script'), html)
  assert.ok(!html.includes('<img'), html)
  assert.ok(html.includes('&lt;script&gt;'), html)
  assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(2)&quot;&gt;'), html)
})

test('renderMarkdown 渲染标题层级：一级变 h2、二级变 h3', () => {
  assert.equal(renderMarkdown('# 月度报告'), '<h2>月度报告</h2>')
  assert.equal(renderMarkdown('## 核心指标'), '<h3>核心指标</h3>')
  assert.equal(renderMarkdown('#### 很深的小节'), '<h5>很深的小节</h5>')
})

test('renderMarkdown 渲染无序与有序列表', () => {
  assert.equal(renderMarkdown('- 收入\n- 成本'), '<ul><li>收入</li><li>成本</li></ul>')
  assert.equal(renderMarkdown('1. 取数\n2. 重算'), '<ol><li>取数</li><li>重算</li></ol>')
})

test('renderMarkdown 渲染表格，并把分隔行吃掉', () => {
  const html = renderMarkdown('| 指标 | 金额 |\n| --- | --- |\n| 收入 | 100 |\n| 成本 | 90 |')
  assert.equal(
    html,
    '<table><thead><tr><th>指标</th><th>金额</th></tr></thead>' +
      '<tbody><tr><td>收入</td><td>100</td></tr><tr><td>成本</td><td>90</td></tr></tbody></table>'
  )
})

test('renderMarkdown 渲染粗体与行内代码，且不吃掉多余字符', () => {
  assert.equal(renderMarkdown('**净利润**为 `-9.99`'), '<p><strong>净利润</strong>为 <code>-9.99</code></p>')
})

test('renderMarkdown 用 br 连接同一段内的换行，空行分段', () => {
  assert.equal(renderMarkdown('第一行\n第二行\n\n另一段'), '<p>第一行<br>第二行</p><p>另一段</p>')
  assert.equal(renderMarkdown('   \n\n'), '')
})

test('renderMarkdown 只加粗不带引号的普通星号文本', () => {
  // 财务文本里 "*" 常被当作乘号或脚注，未成对时不能变成 <strong>
  assert.equal(renderMarkdown('3 * 4 = 12'), '<p>3 * 4 = 12</p>')
})
