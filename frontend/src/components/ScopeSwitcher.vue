<template>
  <div v-if="switchable" class="scope-switcher">
    <span class="scope-label">查看口径</span>
    <select :value="activeScope" :disabled="loading" @change="onChange">
      <option value="">全公司（全部部门）</option>
      <option v-for="item in options" :key="item.name" :value="item.name">
        {{ item.label }}（{{ item.transactions }} 行）
      </option>
    </select>
    <small v-if="activeScope" class="scope-hint">只看「{{ scopeLabel(activeScope) }}」的明细与据此重算的指标</small>
  </div>
</template>

<script setup>
/**
 * 管理员的口径切换器：不受限账号可以临时切到某个部门看，受限账号看不到这个控件。
 *
 * 「能不能切换」直接读会话里的 `unrestricted`（与路由守卫、侧边栏同一份 `/auth/me` 缓存），
 * 而不是让每个页面各自传一个 scope 对象进来——页面少一层耦合，也不会出现某个页面忘了传、
 * 结果管理员在那一页切不了口径的情况。受限账号即便手动改 URL 也会被服务端 403 挡住。
 *
 * 选项来自 `GET /analysis/departments`（数据里真实出现过的部门）。切换只写模块级 `activeScope`，
 * 由各页面自己 watch 重新取数——组件不直接改数据，避免「选择器变了页面还是旧数字」。
 */
import { computed, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import { activeScope, setActiveScope, scopeLabel } from '../api/scope'
import { currentUser } from '../api/session'

const options = ref([])
const loading = ref(false)
const loaded = ref(false)

const switchable = computed(() => currentUser().value?.unrestricted === true)

async function load() {
  if (!switchable.value || loaded.value) return
  loading.value = true
  try {
    const response = await apiFetch('/analysis/departments')
    const payload = await response.json()
    options.value = payload.departments || []
    loaded.value = true
  } catch (error) {
    // 拿不到列表就退回「不显示选择器」，而不是留一个空下拉让人以为系统坏了。
    options.value = []
  } finally {
    loading.value = false
  }
}

function onChange(event) {
  setActiveScope(event.target.value)
}

// 必须 watch 而不是 onMounted：挂载时 `/auth/me` 往往还没回来，`switchable` 仍是 false，
// 那时 load() 会直接 return，之后再没人调用它——表现就是下拉里只有「全公司」一个选项。
watch(switchable, (value) => {
  if (value) load()
}, { immediate: true })
</script>

<style scoped>
.scope-switcher { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.scope-label { font-size: 13px; color: #4a5a6a; }
.scope-switcher select { padding: 6px 10px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; font-size: 13px; max-width: 260px; }
.scope-hint { font-size: 12px; color: #8a6d3b; }
</style>
