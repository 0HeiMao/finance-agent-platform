<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { currentUser, loadCurrentUser } from '../api/session'

const user = currentUser()

const props = defineProps({
  open: { type: Boolean, default: true }
})
const emit = defineEmits(['request-close'])

const route = useRoute()
const router = useRouter()

// 侧边栏与路由一一对应：path 为空表示功能尚未实现，渲染成不可点条目并说明原因。
// 「智能助手」单独成组并置顶：它是贯穿所有页面的问数入口，不隶属于某一类数据页面。
// 「管理员」组只对不受部门限制的账号显示（权限口径见 frontend/src/api/session.js）。
const NAV_GROUPS = [
  {
    label: '智能助手',
    items: [
      {
        name: '财务分析 Agent',
        path: '/agent',
        icon: '✦',
        accent: true,
        note: '基于已导入的财务数据与知识库问答'
      }
    ]
  },
  {
    label: '数据分析',
    items: [
      { name: '看板概览', path: '/dashboard' },
      { name: '收入分析', path: '/analysis/revenue' },
      { name: '成本费用', path: '/analysis/cost' },
      { name: '预警监控', path: '/analysis/alerts' }
    ]
  },
  {
    label: '数据管理',
    items: [
      { name: '数据导入', path: '/financial/import' },
      {
        name: '明细更正',
        path: '/financial/transactions',
        note: '更正就地改并留审计；冲销不删原行而是加一条负数行'
      }
    ]
  },
  {
    label: '报告导出',
    items: [
      { name: 'Word 报告', path: '/reports' },
      { name: 'PPT 报告', path: '/reports/pptx' }
    ]
  },
  { label: '系统', items: [{ name: 'System 设置', path: '/settings' }] },
  // 管理员放在最下面：它平时用不到，且只对不受部门限制的账号显示
  {
    label: '管理员',
    adminOnly: true,
    items: [
      { name: '管理概览', path: '/admin', icon: '◎' },
      { name: '用户管理', path: '/admin/users' },
      { name: '部门权限', path: '/admin/departments', note: '部门决定财务数据与知识库的可见范围' },
      { name: '预警规则', path: '/admin/alert-rules', note: '阈值与开关改完对下一次重算生效' },
      {
        name: '预警外发',
        path: '/admin/alert-channels',
        note: 'Webhook / 邮件通道与投递台账，发失败会退避重试'
      },
      { name: '审计日志', path: '/admin/audit', note: '谁在什么时候改了什么，只读不可清' },
      { name: '导入批次', path: '/admin/batches' },
      { name: '知识库管理', path: '/admin/knowledge' }
    ]
  }
]

const groups = computed(() =>
  // ⑳ 起管理组的开关是「管理员标识」，不是「部门为空」：被收窄到某个部门的管理员照样看得到管理组。
  NAV_GROUPS.filter((group) => !group.adminOnly || Boolean(user.value?.is_admin))
)

const activePath = computed(() => {
  // /alerts/:id 属于预警监控这条线
  if (route.path.startsWith('/alerts/')) return '/analysis/alerts'
  return route.path
})

function isActive(item) {
  return Boolean(item.path) && activePath.value === item.path
}

function go(item) {
  emit('request-close')
  if (item.path && item.path !== route.path) router.push(item.path)
}

// 窄屏下侧边栏默认收起，由页面顶部的 ☰ 控制；宽屏始终展开。
const narrow = ref(false)
let mq = null
function sync(event) {
  narrow.value = event.matches
}
onMounted(() => {
  mq = window.matchMedia('(max-width: 768px)')
  narrow.value = mq.matches
  mq.addEventListener('change', sync)
  // 侧边栏决定要不要显示「管理员」组，所以自己拉一次当前用户（模块内缓存，整站只请求一次）
  loadCurrentUser()
})
onBeforeUnmount(() => {
  if (mq) mq.removeEventListener('change', sync)
})

const visible = computed(() => (narrow.value ? props.open : true))
</script>

<template>
  <aside class="sidebar" :class="{ open: visible }">
    <div class="brand"><b>财</b><span>智能财务分析</span></div>
    <section v-for="group in groups" :key="group.label" class="nav-group">
      <div class="nav-label">{{ group.label }}</div>
      <div
        v-for="item in group.items"
        :key="item.name"
        class="nav-item"
        :class="{ active: isActive(item), disabled: !item.path, accent: item.accent }"
        :title="item.note || item.name"
        @click="go(item)"
      >
        <span class="icon">{{ item.icon || '▦' }}</span
        >{{ item.name }}
      </div>
    </section>
    <slot />
  </aside>
</template>
