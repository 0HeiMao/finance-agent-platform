// ⑦ 前端 lint：ESLint 10 的 flat config。
//
// 只管「代码写错了」，不管「代码长得难看」——缩进、引号、换行全部交给 prettier
// （.prettierrc.json + `npm run format:check`）。两边同时管格式就会互相打架：
// eslint-plugin-vue 的 recommended 里带着一批排版规则，它们和 prettier 的输出不一致，
// 于是「prettier 改完 eslint 报，eslint 改完 prettier 报」，所以这里只取 essential。
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

export default [
  { ignores: ['dist/**', 'node_modules/**', '.build-tmp/**'] },
  js.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['src/**/*.{js,vue}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.browser }
    },
    rules: {
      // 视图组件就是按页面命名（Dashboard.vue / Alerts.vue …），一个词最清楚。
      'vue/multi-word-component-names': 'off',
      // 组件里引用的常量来自 `<script setup>` 的顶层绑定，这条规则在 SFC 里只会误报。
      'no-undef': 'off'
    }
  },
  {
    // node --test 的自定义渲染器、vite 配置、这份配置本身都跑在 Node 里。
    files: ['tests/**/*.mjs', 'vite.config.js', 'eslint.config.js'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.node }
    }
  }
]
