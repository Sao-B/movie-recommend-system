<template>
  <div class="auth-wrapper">
    <el-card class="auth-card">
      <template #header><h2 class="card-title">注册</h2></template>
      <el-form :model="form" :rules="rules" ref="formRef" label-position="top">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="4-20 个字符" prefix-icon="User" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" placeholder="至少 6 位"
            prefix-icon="Lock" show-password />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input v-model="form.confirm" type="password" placeholder="再次输入密码"
            prefix-icon="Lock" show-password @keyup.enter="submit" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" class="full-btn" @click="submit">注册</el-button>
        </el-form-item>
      </el-form>
      <div class="auth-footer">已有账号？<router-link to="/login">立即登录</router-link></div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const router  = useRouter()
const auth    = useAuthStore()
const formRef = ref()
const loading = ref(false)
const form    = reactive({ username: '', password: '', confirm: '' })

const rules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 4, max: 20, message: '用户名 4-20 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    {
      validator: (_, val, cb) =>
        val !== form.password ? cb(new Error('两次密码不一致')) : cb(),
      trigger: 'blur',
    },
  ],
}

async function submit() {
  await formRef.value.validate()
  loading.value = true
  try {
    await auth.register(form.username, form.password)
    ElMessage.success('注册成功，欢迎加入！')
    router.push('/')
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
