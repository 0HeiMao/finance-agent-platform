<script setup>
import { computed, onMounted, ref } from 'vue'
import AppSidebar from '../components/AppSidebar.vue'
import AgentChat from '../components/AgentChat.vue'
import DataScopeNotice from '../components/DataScopeNotice.vue'
import ScopeSwitcher from '../components/ScopeSwitcher.vue'
import { currentUser, loadCurrentUser } from '../api/session'

const sidebarOpen = ref(false)
const user = currentUser()
onMounted(loadCurrentUser)

// Agent 的证据范围由后端按账号部门收窄（scope_of(user)），这里只把同一件事显示出来：
// 受限账号如果不提示，很容易把「部门 097 的费用」当成公司费用引用出去。
const scope = computed(() => {
  const me = user.value
  if (!me || me.unrestricted) return null
  return {
    department: me.department,
    label: `部门 ${me.department}`,
    unrestricted: false,
    note: '当前账号只统计本部门明细，回答里的金额不是全公司口径。',
  }
})

// 示例问题按「库里确实有数据」来写：都能命中财务证据层或知识库，不会一问就答不上来
const EXAMPLES = [
  '2026-07 的费用异常在哪里？',
  '本月收支差额是多少？',
  '哪些部门支出最高？',
  '成本率异常的预警阈值是多少？',
]
</script>

<template>
  <div class="app">
    <AppSidebar :open="sidebarOpen" @request-close="sidebarOpen = false" />

    <main>
      <header class="topbar">
        <div class="title-wrap">
          <button class="menu-btn" @click="sidebarOpen = !sidebarOpen">☰</button>
          <div>
            <h1>财务分析 Agent</h1>
            <p>基于已导入的财务数据与知识库作答：金额与比率由程序确定性汇总，模型只做解释与归因。</p>
          </div>
        </div>
        <div class="actions">
          <ScopeSwitcher />
        </div>
      </header>

      <DataScopeNotice :scope="scope" />

      <AgentChat full :examples="EXAMPLES" />

      <div class="footer">智能财务分析平台 · 回答可能不完整，请对照「依据」列出的来源人工复核</div>
    </main>
  </div>
</template>
