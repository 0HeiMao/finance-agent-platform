<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { deleteKnowledge, knowledgeWriteScope, listKnowledge, uploadKnowledge } from '../api/knowledge'
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
const writeScope = ref(null)
const scopeError = ref('')
const visibility = ref('')
const department = ref('')
const file = ref(null)
const uploading = ref(false)
const lifetime = new AbortController()
let loadController = null

async function loadWriteScope() {
  scopeError.value = ''
  try {
    writeScope.value = await knowledgeWriteScope(lifetime.signal)
  } catch (e) {
    if (e.name !== 'AbortError') scopeError.value = `读取上传权限失败：${e.message}`
  }
}

async function upload() {
  if (!file.value || !visibility.value || (visibility.value === 'department' && !department.value.trim())) {
    error.value = '请选择文件及可见范围，并填写所属部门。'
    return
  }
  uploading.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await uploadKnowledge(
      file.value,
      visibility.value === 'department' ? department.value.trim() : '',
      visibility.value === 'public',
      lifetime.signal
    )
    if (lifetime.signal.aborted) return
    notice.value = `「${result.source}」已处理：新增 ${result.chunks} 条片段，跳过 ${result.skipped} 条重复片段。`
    await load()
  } catch (e) {
    if (e.name !== 'AbortError') error.value = `上传失败：${e.message}`
  } finally {
    uploading.value = false
  }
}

async function load() {
  loadController?.abort()
  const controller = new AbortController()
  loadController = controller
  loading.value = true
  error.value = ''
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset.value) })
  if (sourceFilter.value) params.set('source', sourceFilter.value)
  if (typeFilter.value) params.set('document_type', typeFilter.value)
  try {
    const payload = await listKnowledge(params.toString(), controller.signal)
    if (controller.signal.aborted) return
    sources.value = payload.data
    total.value = payload.total || 0
    chunkTotal.value = payload.all_chunks || 0
    matchedChunks.value = payload.matched_chunks || 0
    documentTypes.value = payload.document_types || []
  } catch (e) {
    if (e.name === 'AbortError') return
    error.value = `读取知识库失败：${e.message}`
    sources.value = []
    total.value = 0
  } finally {
    if (loadController === controller) loading.value = false
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
    const result = await deleteKnowledge(item.source, lifetime.signal)
    if (lifetime.signal.aborted) return
    notice.value = `已删除「${result.source}」的 ${result.deleted_chunks} 条片段。重新导入该材料即可恢复。`
    confirmSource.value = ''
    if (sources.value.length === 1 && offset.value >= PAGE_SIZE) {
      offset.value -= PAGE_SIZE
    }
    await load()
  } catch (e) {
    if (e.name === 'AbortError') return
    error.value = `删除失败：${e.message}`
  } finally {
    busy.value = ''
  }
}

function shortModel(name) {
  return (name || '').split(/[\\/]/).pop() || name
}

onMounted(() => {
  load()
  loadWriteScope()
})
onUnmounted(() => {
  clearTimeout(timer)
  loadController?.abort()
  lifetime.abort()
})
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
            <p>维护授权范围内的知识资料；公共资料仅由全公司管理员发布。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">
            {{ loading ? '读取中…' : '↻ 重新读取' }}
          </button>
        </div>
      </header>

      <section class="page-card">
        <h2>上传知识资料</h2>
        <p v-if="scopeError" class="muted">
          {{ scopeError }} <button class="btn" @click="loadWriteScope">重试</button>
        </p>
        <p v-else-if="!writeScope" class="muted">正在读取上传权限…</p>
        <p v-else-if="!writeScope.can_write" class="muted">当前账号只能查询资料，请联系管理员上传。</p>
        <form v-else @submit.prevent="upload">
          <div class="filter-row">
            <label class="field"
              ><span>文件</span
              ><input
                type="file"
                accept=".txt,.md,.csv,.pdf,.docx"
                :disabled="uploading"
                @change="file = $event.target.files?.[0] || null"
            /></label>
            <label class="field"
              ><span>可见范围</span
              ><select v-model="visibility" class="select" :disabled="uploading">
                <option value="">请选择</option>
                <option value="department">指定部门</option>
                <option v-if="writeScope.can_publish_public" value="public">所有登录用户</option>
              </select></label
            >
            <label v-if="visibility === 'department'" class="field"
              ><span>所属部门</span
              ><select
                v-if="writeScope.departments"
                v-model="department"
                class="select"
                :disabled="uploading"
              >
                <option value="">请选择部门</option>
                <option v-for="name in writeScope.departments" :key="name" :value="name">
                  {{ name }}
                </option></select
              ><input
                v-else
                v-model="department"
                class="select"
                maxlength="100"
                placeholder="填写部门名称"
                :disabled="uploading"
            /></label>
            <button class="btn primary" :disabled="uploading">
              {{ uploading ? '解析并入库中…' : '上传并入库' }}
            </button>
          </div>
          <p class="muted">支持文本、PDF 和 Word；请选择资料所属范围。扫描件和复杂表格请先转换为可读文本。</p>
        </form>
      </section>

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
          <p v-else>使用上方上传入口添加制度文件、指标口径或合同文本。</p>
        </section>

        <section v-else class="page-card">
          <div class="card-head">
            <h2>
              命中 {{ total }} 份来源 · {{ matchedChunks }} 条片段
              <span class="muted">（可维护范围共 {{ chunkTotal }} 条片段）</span>
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

<style scoped>
.filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 12px;
  margin-block: 16px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.field input[type='file'] {
  width: 260px;
  max-width: 100%;
}
@media (max-width: 600px) {
  .field {
    width: 100%;
  }
  .field .select {
    width: 100%;
  }
}
</style>
