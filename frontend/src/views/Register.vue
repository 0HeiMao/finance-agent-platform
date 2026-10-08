<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { register } from '../api/auth'
const router=useRouter();const loading=ref(false);const formRef=ref();const form=reactive({username:'',password:'',confirmPassword:''})
const validateConfirm=(rule,value,callback)=>value!==form.password?callback(new Error('两次密码不一致')):callback()
const rules={username:[{required:true,message:'请输入用户名',trigger:'blur'}],password:[{required:true,message:'请输入密码',trigger:'blur'},{min:6,message:'密码至少 6 位',trigger:'blur'}],confirmPassword:[{required:true,message:'请确认密码',trigger:'blur'},{validator:validateConfirm,trigger:'blur'}]}
async function submit(){await formRef.value.validate(async valid=>{if(!valid)return;loading.value=true;try{await register({username:form.username,password:form.password});ElMessage.success('注册成功，请登录');router.replace('/login')}catch(e){ElMessage.error(e.response?.data?.detail||'注册失败')}finally{loading.value=false}})}
</script>
<template><div class="auth-page"><el-card class="auth-card"><div class="auth-brand">智能财务分析</div><h1>创建账号</h1><p class="auth-subtitle">注册后开始使用财务分析平台</p><el-form ref="formRef" :model="form" :rules="rules" label-position="top"><el-form-item label="用户名" prop="username"><el-input v-model="form.username" placeholder="请输入用户名" /></el-form-item><el-form-item label="密码" prop="password"><el-input v-model="form.password" type="password" show-password placeholder="至少 6 位" /></el-form-item><el-form-item label="确认密码" prop="confirmPassword"><el-input v-model="form.confirmPassword" type="password" show-password placeholder="再次输入密码" /></el-form-item><el-button type="primary" :loading="loading" class="auth-submit" @click="submit">注册</el-button></el-form><div class="auth-switch">已有账号？<router-link to="/login">返回登录</router-link></div></el-card></div></template>
