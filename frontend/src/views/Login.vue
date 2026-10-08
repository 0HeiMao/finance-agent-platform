<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '../api/auth'
const router = useRouter(); const route = useRoute(); const loading = ref(false)
const formRef = ref(); const form = reactive({ username: '', password: '' })
const rules = { username: [{ required: true, message: '请输入用户名', trigger: 'blur' }], password: [{ required: true, message: '请输入密码', trigger: 'blur' }, { min: 6, message: '密码至少 6 位', trigger: 'blur' }] }
async function submit(){
  if (loading.value) return
  try { await formRef.value.validate() } catch { return }
  loading.value=true
  try {
    const {data}=await login({ username: form.username.trim(), password: form.password })
    const token=data?.access_token || data?.token
    if (!token) throw new Error('登录响应中缺少 access_token')
    localStorage.setItem('access_token',token)
    ElMessage.success('登录成功')
    await router.replace(typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard')
  } catch(e) { ElMessage.error(e.response?.data?.detail || e.message || '登录失败，请检查账号密码') }
  finally { loading.value=false }
}
</script>
<template><div class="auth-page"><el-card class="auth-card"><div class="auth-brand">智能财务分析</div><h1>欢迎回来</h1><p class="auth-subtitle">登录以继续使用财务分析平台</p><el-form ref="formRef" :model="form" :rules="rules" label-position="top" @keyup.enter="submit"><el-form-item label="用户名" prop="username"><el-input v-model="form.username" placeholder="请输入用户名" /></el-form-item><el-form-item label="密码" prop="password"><el-input v-model="form.password" type="password" show-password placeholder="请输入密码" /></el-form-item><el-button type="primary" :loading="loading" class="auth-submit" @click="submit">登录</el-button></el-form><div class="auth-switch">还没有账号？<router-link to="/register">立即注册</router-link></div></el-card></div></template>
