/**
 * 极简 Markdown 渲染：报告页与 Agent 回答共用。
 *
 * 安全前提：**先把整段文本做 HTML 转义，再解析结构**。因此模型或后端拼进来的任何
 * `<script>` / `<img onerror>` 都已经是纯文本，v-html 渲染的是我们自己生成的标签。
 * 只支持财务场景真正会用到的语法：标题、有序/无序列表、表格、粗体、行内代码。
 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (char) => ESCAPES[char])
}

function inline(text) {
  return text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
}

function isTableRow(line) {
  return /^\s*\|.*\|\s*$/.test(line)
}

function isTableSeparator(line) {
  return /^\s*\|[\s:|-]+\|\s*$/.test(line)
}

function tableCells(line) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

const isBullet = (line) => /^\s*[-*]\s+/.test(line)
const isOrdered = (line) => /^\s*\d+[.、]\s+/.test(line)
const headingMatch = (line) => /^(#{1,6})\s+(.*)$/.exec(line)

export function renderMarkdown(markdown) {
  const lines = escapeHtml(markdown).split(/\r?\n/)
  const out = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      index += 1
      continue
    }

    if (isTableRow(line) && isTableSeparator(lines[index + 1] || '')) {
      const head = tableCells(line)
      index += 2
      const rows = []
      while (index < lines.length && isTableRow(lines[index])) {
        rows.push(tableCells(lines[index]))
        index += 1
      }
      const headHtml = head.map((cell) => `<th>${inline(cell)}</th>`).join('')
      const bodyHtml = rows
        .map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join('')}</tr>`)
        .join('')
      out.push(`<table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`)
      continue
    }

    const heading = headingMatch(line)
    if (heading) {
      // `# 标题` -> h2、`## 小节` -> h3，与报告页原本的层级保持一致
      const level = Math.min(heading[1].length + 1, 6)
      out.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`)
      index += 1
      continue
    }

    if (isBullet(line) || isOrdered(line)) {
      const ordered = isOrdered(line)
      const items = []
      while (index < lines.length && (isBullet(lines[index]) || isOrdered(lines[index]))) {
        items.push(lines[index].replace(/^\s*(?:[-*]|\d+[.、])\s+/, ''))
        index += 1
      }
      const tag = ordered ? 'ol' : 'ul'
      out.push(`<${tag}>${items.map((item) => `<li>${inline(item)}</li>`).join('')}</${tag}>`)
      continue
    }

    const paragraph = []
    while (
      index < lines.length &&
      lines[index].trim() &&
      !isTableRow(lines[index]) &&
      !headingMatch(lines[index]) &&
      !isBullet(lines[index]) &&
      !isOrdered(lines[index])
    ) {
      paragraph.push(lines[index])
      index += 1
    }
    out.push(`<p>${paragraph.map(inline).join('<br>')}</p>`)
  }

  return out.join('')
}
