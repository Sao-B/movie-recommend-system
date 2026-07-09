<template>
  <div class="login-page">
    <el-card class="login-card">
      <h2 style="text-align:center; margin-bottom:24px">🎬 电影推荐系统</h2>

      <el-tabs v-model="tab" stretch>
        <el-tab-pane label="登录" name="login">
          <el-form :model="form" @submit.prevent="handleLogin" label-position="top">
            <el-form-item label="用户名">
              <el-input v-model="form.username" placeholder="请输入用户名" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.password" type="password" placeholder="请输入密码"
                        show-password @keyup.enter="handleLogin" />
            </el-form-item>
            <el-button type="primary" style="width:100%" :loading="loading" @click="handleLogin">
              登录
            </el-button>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="注册" name="register">
          <el-form :model="form" @submit.prevent="handleRegister" label-position="top">
            <el-form-item label="用户名">
              <el-input v-model="form.username" placeholder="请输入用户名（3~50字符）" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.password" type="password" placeholder="至少6位" show-password />
            </el-form-item>
            <el-button type="primary" style="width:100%" :loading="loading" @click="handleRegister">
              注册
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'

const router    = useRouter()
const userStore = useUserStore()

const tab     = ref('login')
const loading = ref(false)
const form    = ref({ username: '', password: '' })

async function handleLogin() {
  if (!form.value.username || !form.value.password) {
    return ElMessage.warning('请填写用户名和密码')
  }
  loading.value = true
  try {
    await userStore.login(form.value.username, form.value.password)
    router.push('/')
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  if (!form.value.username || !form.value.password) {
    return ElMessage.warning('请填写用户名和密码')
  }
  if (form.value.password.length < 6) {
    return ElMessage.warning('密码至少 6 位')
  }
  loading.value = true
  try {
    await userStore.register(form.value.username, form.value.password)
    ElMessage.success('注册成功，已自动登录')
    router.push('/')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.login-card {
  width: 380px;
  padding: 16px;
}
</style>
