<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login, state } from '../api'

const router = useRouter()
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!password.value) return
  loading.value = true
  try {
    await login(password.value)
    ElMessage.success('登录成功')
    router.replace('/dashboard')
  } catch (err) {
    ElMessage.error(err.message || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="logo-line">
        <div class="logo">NP</div>
        <div>
          <div class="name">NodePoolME</div>
          <div class="sub">多机场节点池 · 统一订阅</div>
        </div>
      </div>
      <el-form @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="password" type="password" size="large" placeholder="管理密码"
                    show-password @keyup.enter="submit" autofocus />
        </el-form-item>
        <el-button type="primary" size="large" style="width:100%" :loading="loading" @click="submit">
          进入控制台
        </el-button>
      </el-form>
      <div class="hint">首次部署密码由环境变量 NODEPOOL_ADMIN_PASSWORD 指定，默认 nodepool。</div>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: radial-gradient(1200px 600px at 20% 0%, #1d2942 0%, #0f1420 55%, #0b0f18 100%);
}
.login-card {
  width: 380px; background: #fff; border-radius: 16px; padding: 34px 32px 26px;
  box-shadow: 0 30px 60px rgba(0, 0, 0, .35);
}
.logo-line { display: flex; gap: 12px; align-items: center; margin-bottom: 26px; }
.logo {
  width: 42px; height: 42px; border-radius: 11px; background: #141b2f; color: #8ab4ff;
  font-weight: 700; display: flex; align-items: center; justify-content: center; letter-spacing: .5px;
}
.name { font-size: 19px; font-weight: 700; }
.sub { font-size: 12px; color: #8a94a6; }
.hint { font-size: 12px; color: #98a2b3; margin-top: 18px; line-height: 1.6; }
</style>
