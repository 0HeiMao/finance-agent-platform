<script setup>
import { onMounted, ref, watch } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import PagePager from '../components/PagePager.vue'

const PAGE_SIZE = 50

const sidebarOpen = ref(false)
const sources = ref([])
const total = ref(0)
const offset = ref(0)
const chunkTotal = ref(0)
const matchedChunks = ref(0)
const documentTypes = ref([])
const sourceFilter = ref('')
const typeFilter = ref('')
const loading = ref(false)
const error = ref('')
const notice = ref('')
const busy = ref('')
const confirmSource = ref('')

async function load() {
  loading.value = true
  error.value = ''
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset.value) })
  if (sourceFilter.value) params.set('source', sourceFilter.value)
  if (typeFilter.value) params.set('document_type', typeFilter.value)
  try {
    const response = await apiFetch(`/admin/knowledge?${params.toString()}`)
    const payload = await response.json()
    sources.value = payload.data
    total.value = payload.total || 0
    chunkTotal.value = payload.all_chunks || 0
    matchedChunks.value = payload.matched_chunks || 0
    documentTypes.value = payload.document_types || []
  } catch (e) {
    error.value = `读取知识库失败：${e.message}`
    sources.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function reload() {
  offset.value = 0
  load()
}

function turn(next) {
  offset.value = next
  load()
}

// 边打边查会每个字打一次接口，停 300ms 再查
let timer = null
watch(sourceFilter, () => {
  clearTimeout(timer)
  timer = setTimeout(reload, 300)
})
watch(typeFilter, reload)

async function removeSource(item) {
  busy.value = item.source
  error.value = ''
  notice.value = ''
  try {
    const response = await apiFetch(`/admin/knowledge?source=${encodeURIComponent(item.source)}`, {
      method: 'DELETE'
    })
    const result = await response.json()
    notice.value = `已删除「${result.source}」的 ${result.deleted_chunks} 条片段。重新导入该材料即可恢复。`
    confirmSource.value = ''
    if (sources.value.length === 1 && offset.value >= PAGE_SIZE) {
      offset.value -= PAGE_SIZE
    }
    await load()
  } catch (e) {
    error.value = `删除失败：${e.message}`
  } finally {
    busy.value = ''
  }
}

function shortModel(name) {
  return (name || '').split(/[\\/]/).pop() || name
}

onMounted(load)
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>知识库管理</h1>
            <p>按来源查看已入库片段；删除按整份来源生效，不受部门限制。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">
            {{ loading ? '读取中…' : '↻ 重新读取' }}
          </button>
        </div>
      </header>

      <p v-if="loading && !sources.length" class="muted">正在读取知识库…</p>

      <section v-else-if="error && !sources.length" class="state-box error-state">
        <strong>读取失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="load">重试</button>
      </section>

      <template v-else>
        <section v-if="error" class="state-box error-state">
          <strong>操作未完成</strong>
          <p>{{ error }}</p>
        </section>
        <section v-else-if="notice" class="state-box notice">{{ notice }}</section>

        <section v-if="!sources.length" class="state-box empty-state">
          <strong>{{ sourceFilter || typeFilter ? '没有符合条件的来源' : '知识库是空的' }}</strong>
          <p v-if="sourceFilter || typeFilter">换个来源名或文档类型再查；也可以清空筛选看全部来源。</p>
          <p v-else>把制度文件、指标口径或合同文本放进 data/raw，或直接 POST /api/v1/rag/documents。</p>
        </section>

        <section v-else class="page-card">
          <div class="card-head">
            <h2>
              命中 {{ total }} 份来源 · {{ matchedChunks }} 条片段
              <span class="muted">（全库共 {{ chunkTotal }} 条片段）</span>
            </h2>
            <span>上传接口按内容哈希去重，同一份材料重复入库只会追加新增片段</span>
          </div>

          <div class="filter-row">
            <label class="field">
              <span>来源</span>
              <input
                v-model.trim="sourceFilter"
                class="select"
                maxlength="500"
                placeholder="按来源名模糊搜索"
              />
            </label>
            <label class="field">
              <span>文档类型</span>
              <select v-model="typeFilter" class="select">
                <option value="">全部类型</option>
                <option v-for="type in documentTypes" :key="type" :value="type">{{ type }}</option>
              </select>
            </label>
            <span class="muted">一行 = 一个来源；每页 {{ PAGE_SIZE }} 个来源</span>
          </div>

          <ul class="dir-list">
            <li v-for="item in sources" :key="item.source">
              <div class="dir-head">
                <strong>{{ item.source }}</strong>
                <span class="level low">{{ item.chunks }} 条</span>
              </div>
              <small class="quality-hint">
                类型：{{ item.document_types.join('、') || '未标注' }} · 入库：{{ item.first_at }} ~
                {{ item.last_at }}
              </small>
              <small class="quality-hint">
                向量模型：{{ item.embedding_models.map(shortModel).join('、') || '未标注（不参与检索）' }}
              </small>
              <p v-if="item.preview" class="muted">{{ item.preview }}…</p>
              <div class="row-actions">
                <template v-if="confirmSource !== item.source">
                  <button class="btn danger" @click="confirmSource = item.source">删除这份来源</button>
                </template>
                <template v-else>
                  <button class="btn danger" :disabled="busy === item.source" @click="removeSource(item)">
                    {{ busy === item.source ? '删除中…' : '确认删除' }}
                  </button>
                  <button class="btn" @click="confirmSource = ''">取消</button>
                </template>
              </div>
            </li>
          </ul>
          <PagePager
            :total="total"
            :limit="PAGE_SIZE"
            :offset="offset"
            :loading="loading"
            unit="份来源"
            @change="turn"
          />
          <p class="muted">
            向量模型标注为空或与当前模型不一致的片段不会参与检索（同维度不代表同一向量空间）， 删除后可用
            scripts/reindex_embeddings.py 统一重算。
          </p>
        </section>
      </template>
    </main>
  </div>
</template>
