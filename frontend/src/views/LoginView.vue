<template>
  <div class="auth-wrapper">
    <el-card class="auth-card">
      <template #header><h2 class="card-title">登录</h2></template>
      <el-form :model="form" :rules="rules" ref="formRef" label-position="top">
        <el-form-item label="账号 / ID" prop="username">
          <el-input v-model="form.username" placeholder="账号或用户 ID" :prefix-icon="User" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" placeholder="请输入密码"
            :prefix-icon="Lock" show-password @keyup.enter="submit" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" class="full-btn" @click="submit">登录</el-button>
        </el-form-item>
      </el-form>
      <div class="auth-footer">还没有账号？<router-link to="/register">立即注册</router-link></div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { User, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const router  = useRouter()
const route   = useRoute()
const auth    = useAuthStore()
const formRef = ref()
const loading = ref(false)
const form    = reactive({ username: '', password: '' })

const rules = {
  // 这个字段既收账号也收用户 ID（纯数字时后端先按账号找、再按 ID 找）
  username: [{ required: true, message: '请输入账号或用户 ID', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码',   trigger: 'blur' }],
}

async function submit() {
  // 校验没过就直接返回（与后台页面一致：validate() 失败会 reject，不 catch 会留下未处理的 rejection）
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    router.push(route.query.redirect || '/')
  } catch {} finally {
    loading.value = false
  }
}
</script>

<style scoped>
.auth-wrapper { min-height: calc(100vh - 60px); display: flex; align-items: center; justify-content: center; }
.auth-card    { width: 400px; }
.card-title   { margin: 0; text-align: center; font-size: 20px; }
.full-btn     { width: 100%; }
.auth-footer  { text-align: center; color: var(--app-text-secondary); font-size: 14px; margin-top: 8px; }
</style>
