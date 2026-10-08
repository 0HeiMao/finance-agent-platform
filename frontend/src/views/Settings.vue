<script setup>
import { onMounted, ref } from 'vue'
import { apiFetch } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'

const sidebarOpen = ref(false)
const status = ref(null)
const loading = ref(false)
const error = ref('')
const saved = ref('')
const saving = ref(false)
const department = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const r = await apiFetch('/system/status')
    status.value = await r.json()
    department.value = status.value.me?.department || ''
  } catch (e) {
    error.value = `读取系统状态失败：${e.message}`
    status.value = null
  } finally {
    loading.value = false
  }
}

async function saveDepartment() {
  saving.value = true
  error.value = ''
  saved.value = ''
  try {
    const r = await apiFetch('/auth/me', {
      method: 'PATCH',
      body: JSON.stringify({ department: department.value }),
    })
    const me = await r.json()
    status.value.me = me
    saved.value = me.department
      ? `已保存：只检索「${me.department}」与公共片段。`
      : '已清空部门：当前账号不受部门限制（等同于财务管理员）。'
  } catch (e) {
    error.value = `保存失败：${e.message}`
  } finally {
    saving.value = false
  }
}

function fmtCount(key) {
  return status.value?.data?.counts?.[key] ?? 0
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
            <h1>System 设置</h1>
            <p>查看运行态配置与体检结果；密钥只显示「是否已配置」，不回显原文。</p>
          </div>
        </div>
        <div class="actions">
          <button class="btn" :disabled="loading" @click="load">{{ loading ? '读取中…' : '↻ 重新读取' }}</button>
        </div>
      </header>

      <p v-if="loading && !status" class="muted">正在读取系统状态…</p>

      <section v-else-if="error && !status" class="state-box error-state">
        <strong>读取失败</strong>
        <p>{{ error }}</p>
        <button class="btn" @click="load">重试</button>
      </section>

      <template v-else-if="status">
        <section v-if="error" class="state-box error-state"><strong>操作未完成</strong><p>{{ error }}</p></section>
        <section v-if="saved" class="state-box notice">{{ saved }}</section>

        <section
          v-if="status.config_problems.length"
          class="state-box notice"
        >
          <strong>配置体检发现 {{ status.config_problems.length }} 个问题</strong>
          <ul>
            <li v-for="(problem, index) in status.config_problems" :key="index">{{ problem }}</li>
          </ul>
        </section>
        <section v-else class="state-box ok">
          <strong>配置体检通过</strong>
          <p>没有发现占位密钥、通配 CORS 或非本机来源等风险项。</p>
        </section>

        <section class="page-card">
          <div class="card-head"><h2>我的部门</h2></div>
          <p class="muted">
            留空表示不受限制（财务管理员）；填写后<strong>财务数据与知识库都只按本部门收窄</strong>：
            看板/分析/报告/Agent 只统计本部门明细（读该部门的指标与预警口径行），知识库检索只返回
            「本部门 + 未标注部门」的片段，管理接口一律 403。
            部门由服务端记录，不接受请求头自述，避免伪造提权。
          </p>
          <div class="upload-box">
            <input v-model.trim="department" class="select" maxlength="100" placeholder="例如：财务部（留空=不受限）">
            <button class="btn primary" :disabled="saving" @click="saveDepartment">{{ saving ? '保存中…' : '保存' }}</button>
          </div>
          <p class="muted">
            当前：<strong>{{ status.me.username }}</strong>（id {{ status.me.id }}）·
            {{ status.me.unrestricted ? '不受部门限制' : `限定 ${status.me.department}` }}
          </p>
        </section>

        <section class="page-card">
          <div class="card-head"><h2>运行态</h2></div>
          <div class="settings-grid">
            <div><span>应用环境</span><strong>{{ status.app.env }}</strong></div>
            <div><span>严格配置</span><strong>{{ status.app.strict_config ? '开启' : '关闭' }}</strong></div>
            <div><span>API 前缀</span><strong>{{ status.app.api_prefix }}</strong></div>
            <div><span>令牌有效期</span><strong>{{ status.app.token_expire_minutes }} 分钟</strong></div>
            <div><span>允许来源</span><strong>{{ status.app.cors_origins.join('、') || '（空）' }}</strong></div>
            <div><span>日志级别</span><strong>{{ status.app.log_level }}</strong></div>
          </div>
        </section>

        <section class="page-card">
          <div class="card-head"><h2>数据与检索</h2></div>
          <div class="settings-grid">
            <div>
              <span>数据库</span>
              <strong>{{ status.database.ok ? `连通（PostgreSQL ${status.database.version}）` : '连接失败' }}</strong>
              <small>{{ status.database.url }}</small>
            </div>
            <div>
              <span>pgvector</span>
              <strong>{{ status.database.pgvector ? '已安装' : '未安装' }}</strong>
            </div>
            <div>
              <span>Embedding 模型</span>
              <strong>{{ status.embedding.model }}</strong>
              <small>{{ status.embedding.dimension }} 维 · {{ status.embedding.using_fallback ? '使用降级实现' : '本地模型' }}</small>
            </div>
            <div>
              <span>大模型</span>
              <strong>{{ status.llm.provider_configured ? status.llm.model : '未配置（仅返回检索依据）' }}</strong>
              <small>{{ status.llm.base_url || '—' }}</small>
            </div>
            <div>
              <span>检索阈值 / 召回数</span>
              <strong>{{ status.rag.max_distance }} / {{ status.rag.candidate_k }}</strong>
              <small>重排模型：{{ status.llm.rerank_model || '未启用（沿用余弦序）' }}</small>
            </div>
            <div>
              <span>知识库片段</span>
              <strong>{{ fmtCount('knowledge_chunks') }} 条</strong>
              <small>用户 {{ fmtCount('users') }} 人</small>
            </div>
            <div>
              <span>财务数据</span>
              <strong>{{ fmtCount('transactions') }} 行交易</strong>
              <small>{{ fmtCount('metrics') }} 期指标 · {{ fmtCount('alerts') }} 条预警</small>
            </div>
            <div>
              <span>最新期间</span>
              <strong>{{ status.data.latest_period || '（无）' }}</strong>
              <small>{{ status.data.periods.length }} 个可用期间</small>
            </div>
          </div>
        </section>

        <section class="page-card">
          <div class="card-head"><h2>目录可写性</h2></div>
          <p class="muted">真写一个探针文件再删除，而不是只看路径是否存在。</p>
          <!-- 用列表而不是表格：三列表格在手机宽度下会横向滚动，路径和状态都看不见 -->
          <ul class="dir-list">
            <li v-for="dir in status.directories" :key="dir.name">
              <div class="dir-head">
                <strong>{{ dir.name }}</strong>
                <span class="level" :class="dir.writable ? 'low' : 'high'">
                  {{ dir.writable ? '可写' : '不可写' }}
                </span>
              </div>
              <code>{{ dir.path }}</code>
              <small v-if="!dir.writable">{{ dir.error || '未知原因' }}</small>
            </li>
          </ul>
        </section>
      </template>
    </main>
  </div>
</template>
