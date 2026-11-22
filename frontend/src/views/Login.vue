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
          <div class="sub">多机场缓存池 · 统一订阅</div>
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
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background:
    radial-gradient(900px 500px at 18% -10%, rgba(10, 132, 255, .16), transparent 62%),
    radial-gradient(800px 500px at 92% 12%, rgba(191, 90, 242, .1), transparent 60%),
    linear-gradient(180deg, #fbfbfd 0%, #f2f2f7 100%);
  overflow: hidden;
}
.login-card {
  position: relative;
  width: 392px;
  background: rgba(255, 255, 255, .78);
  border: 1px solid rgba(255, 255, 255, .65);
  border-radius: 28px;
  padding: 38px 34px 28px;
  box-shadow: 0 1px 0 rgba(255,255,255,.8) inset, 0 24px 70px rgba(0, 0, 0, .1);
  -webkit-backdrop-filter: saturate(180%) blur(24px);
  backdrop-filter: saturate(180%) blur(24px);
}
.logo-line { display: flex; gap: 14px; align-items: center; margin-bottom: 30px; }
.logo {
  width: 50px; height: 50px; border-radius: 14px;
  background: linear-gradient(145deg, #0a84ff 0%, #0066cc 100%);
  color: #fff; font-size: 16px; font-weight: 800; letter-spacing: -.04em;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 8px 20px rgba(0, 113, 227, .25), inset 0 1px 0 rgba(255,255,255,.3);
}
.name { font-size: 24px; line-height: 1.15; font-weight: 700; letter-spacing: -.03em; }
.sub { font-size: 13px; color: #6e6e73; margin-top: 3px; }
.hint {
  font-size: 12px; color: #86868b; margin-top: 22px; line-height: 1.6;
  padding-top: 17px; border-top: 1px solid rgba(60, 60, 67, .1);
}
@media (max-width: 768px) {
  .login-wrap { padding: 18px; align-items: center; }
  .login-card { width: 100%; max-width: 392px; padding: 30px 22px 22px; border-radius: 24px; }
  .logo { width: 46px; height: 46px; border-radius: 13px; }
  .name { font-size: 21px; }
  .logo-line { margin-bottom: 25px; }
}
</style>
