// 给 `node --test` 装上 .vue / 省略扩展名的加载钩子（见 tests/support/sfc-loader.mjs）。
// package.json 的 test 脚本里用 `--import ./tests/support/register.mjs` 引入。
import { register } from 'node:module'

register('./sfc-loader.mjs', import.meta.url)
