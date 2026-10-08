/**
 * 组件测试用的 Node ESM 加载钩子。
 *
 * `node --test` 本身不认识 `.vue`，也不会补 `./client` 这种省略扩展名的相对导入（Vite 会）。
 * 这里补上这两件事，让测试可以直接 `import PagePager from '../../src/components/PagePager.vue'`：
 *
 * 1. `.vue` → 用 `vue/compiler-sfc` 现场编译 `<script setup>`（`inlineTemplate: true`，
 *    把模板编进 setup 的 render，和 Vite 客户端构建同一个编译器），样式块在测试里没有意义，忽略。
 * 2. 省略扩展名的相对导入 → 依次试 `.js` / `.mjs` / `/index.js`。
 * 3. `import.meta.env` → 换成 `globalThis.__VITE_ENV__`：`client.js` 里读 `VITE_API_BASE`，
 *    而 Node 没有 Vite 注入的 `import.meta.env`，不替换会直接 TypeError。
 *
 * 刻意**不引入 jsdom / vitest**：断言在无 DOM 的自定义渲染器（tests/support/render.mjs）上跑。
 */
import { readFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { parse, compileScript } from 'vue/compiler-sfc'

const EXTENSIONS = ['.js', '.mjs', '/index.js', '/index.mjs']

function withExtensions(base) {
  for (const suffix of EXTENSIONS) {
    const candidate = new URL(`${base.href}${suffix}`)
    if (existsSync(fileURLToPath(candidate))) return candidate
  }
  return base
}

export async function resolve(specifier, context, nextResolve) {
  const parent = context.parentURL
  if ((specifier.startsWith('./') || specifier.startsWith('../')) && parent?.startsWith('file:')) {
    const base = new URL(specifier, parent)
    if (!path.extname(base.pathname)) return nextResolve(withExtensions(base).href, context)
  }
  return nextResolve(specifier, context)
}

export async function load(url, context, nextLoad) {
  if (!url.startsWith('file:')) return nextLoad(url, context)

  if (url.endsWith('.vue')) {
    const filename = fileURLToPath(url)
    const source = await readFile(filename, 'utf8')
    const { descriptor, errors } = parse(source, { filename })
    if (errors.length) throw new Error(`解析 ${path.basename(filename)} 失败：${errors[0].message}`)
    const compiled = compileScript(descriptor, {
      // 给个稳定的 id：不给的话 compiler-sfc 会打「compileTemplate now requires the id option」警告，
      // 并退化成同名 scopeId（组件本身不下载样式，id 只用来消警告）
      id: `test-${path.basename(filename)}`,
      inlineTemplate: true,
    })
    return { format: 'module', source: compiled.content, shortCircuit: true }
  }

  if (url.includes('/src/') && (url.endsWith('.js') || url.endsWith('.mjs'))) {
    const source = await readFile(fileURLToPath(url), 'utf8')
    if (!source.includes('import.meta.env')) return nextLoad(url, context)
    return {
      format: 'module',
      source: source.replaceAll('import.meta.env', '(globalThis.__VITE_ENV__ || {})'),
      shortCircuit: true,
    }
  }

  return nextLoad(url, context)
}

export { pathToFileURL }
