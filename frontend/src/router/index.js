import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import Login from '../views/Login.vue'
import Register from '../views/Register.vue'
import FinancialImport from '../views/FinancialImport.vue'
import FinancialTransactions from '../views/FinancialTransactions.vue'
import FinancialAnalysis from '../views/FinancialAnalysis.vue'
import AlertsList from '../views/AlertsList.vue'
import AlertDetail from '../views/AlertDetail.vue'
import Reports from '../views/Reports.vue'
import ReportsPptx from '../views/ReportsPptx.vue'
import Settings from '../views/Settings.vue'
import AgentWorkbench from '../views/AgentWorkbench.vue'
import AdminOverview from '../views/AdminOverview.vue'
import AdminUsers from '../views/AdminUsers.vue'
import AdminDepartments from '../views/AdminDepartments.vue'
import AdminAlertRules from '../views/AdminAlertRules.vue'
import AdminAudit from '../views/AdminAudit.vue'
import AdminBatches from '../views/AdminBatches.vue'
import AdminKnowledge from '../views/AdminKnowledge.vue'
import { loadCurrentUser } from '../api/session'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/login', component: Login, meta: { guestOnly: true } },
    { path: '/register', component: Register, meta: { guestOnly: true } },
    { path: '/dashboard', component: Dashboard, meta: { requiresAuth: true } },
    { path: '/agent', component: AgentWorkbench, meta: { requiresAuth: true } },
    { path: '/financial/import', component: FinancialImport, meta: { requiresAuth: true } },
    { path: '/financial/transactions', component: FinancialTransactions, meta: { requiresAuth: true } },
    { path: '/analysis/alerts', component: AlertsList, meta: { requiresAuth: true } },
    // /analysis/:type 必须放在具体的 /analysis/alerts 之后，否则 "alerts" 会被当成 type 捕获
    {
      path: '/analysis/:type(revenue|cost|profit|summary)',
      component: FinancialAnalysis,
      props: true,
      meta: { requiresAuth: true }
    },
    { path: '/alerts/:id', component: AlertDetail, meta: { requiresAuth: true } },
    { path: '/reports', component: Reports, meta: { requiresAuth: true } },
    { path: '/reports/pptx', component: ReportsPptx, meta: { requiresAuth: true } },
    { path: '/settings', component: Settings, meta: { requiresAuth: true } },
    // 管理入口：meta.admin 由守卫校验（部门为空 = 不受限 = 财务管理员）
    { path: '/admin', component: AdminOverview, meta: { requiresAuth: true, admin: true } },
    { path: '/admin/users', component: AdminUsers, meta: { requiresAuth: true, admin: true } },
    { path: '/admin/departments', component: AdminDepartments, meta: { requiresAuth: true, admin: true } },
    { path: '/admin/alert-rules', component: AdminAlertRules, meta: { requiresAuth: true, admin: true } },
    { path: '/admin/audit', component: AdminAudit, meta: { requiresAuth: true, admin: true } },
    { path: '/admin/batches', component: AdminBatches, meta: { requiresAuth: true, admin: true } },
    { path: '/admin/knowledge', component: AdminKnowledge, meta: { requiresAuth: true, admin: true } },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' }
  ]
})
router.beforeEach(async (to) => {
  const loggedIn = Boolean(localStorage.getItem('access_token'))
  if (to.meta.requiresAuth && !loggedIn) return { path: '/login', query: { redirect: to.fullPath } }
  if (to.meta.guestOnly && loggedIn) return '/dashboard'
  if (to.meta.admin && loggedIn) {
    // 侧边栏会把管理组藏起来，但地址栏仍然可以被手动输入，所以守卫这里必须自己判一次权限
    const me = await loadCurrentUser()
    if (!me?.unrestricted) return { path: '/dashboard', query: { denied: 'admin' } }
  }
})
export default router
