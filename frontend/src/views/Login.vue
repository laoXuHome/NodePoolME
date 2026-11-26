<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '../api'
import BrandLogo from '../components/BrandLogo.vue'

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
    <div class="login-shell">
      <section class="login-story">
        <div class="login-story-glow"></div>
        <div class="login-brand">
          <BrandLogo :size="42" light />
          <span>CONTROL CENTER</span>
        </div>
        <div class="login-story-copy">
          <span class="login-kicker"><i></i> NODE POOL OPERATIONS</span>
          <h1>把散落的机场节点<br />汇成一个<em>可运营的池</em></h1>
          <p>接入机场订阅，缓存全部节点，跑双阶段检测，用评分与成熟度筛选，再以统一订阅交付给客户端。</p>
        </div>
        <div class="login-flow">
          <span>机场接入</span><i></i><span>全量缓存</span><i></i><span>双阶段检测</span><i></i><span>统一发布</span>
        </div>
        <div class="login-story-foot">
          <span>多机场 · 单节点池</span>
          <span>本地检测 · 原子发布</span>
        </div>
      </section>

      <section class="login-panel">
        <div class="login-panel-head">
          <span>ADMIN ACCESS</span>
          <h2>进入控制台</h2>
          <p>输入管理密码，查看节点池的实时状态。</p>
        </div>
        <el-form @submit.prevent="submit">
          <el-form-item>
            <el-input v-model="password" type="password" size="large" placeholder="管理密码"
                      show-password @keyup.enter="submit" autofocus>
              <template #prefix><el-icon><Lock /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-button type="primary" size="large" class="login-submit" :loading="loading" @click="submit">
            进入控制台
            <el-icon><Right /></el-icon>
          </el-button>
        </el-form>
        <div class="login-hint">
          <span><i></i> 凭据只在本地校验</span>
          <p>首次部署密码由环境变量 <code>NODEPOOL_ADMIN_PASSWORD</code> 指定，默认 nodepool。</p>
        </div>
        <div class="login-foot">NodePoolME · 多机场缓存池与统一订阅</div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background:
    radial-gradient(900px 560px at 8% -10%, rgba(59, 130, 246, .16), transparent 62%),
    radial-gradient(760px 480px at 96% 10%, rgba(124, 58, 237, .11), transparent 60%),
    radial-gradient(600px 420px at 52% 110%, rgba(6, 182, 212, .09), transparent 66%),
    #f2f5f9;
}
.login-shell {
  width: min(980px, 100%);
  min-height: 570px;
  display: grid;
  grid-template-columns: 1.12fr .88fr;
  border-radius: 30px;
  overflow: hidden;
  background: #fff;
  border: 1px solid rgba(255, 255, 255, .82);
  box-shadow: 0 34px 90px rgba(15, 37, 71, .16), 0 1px 0 rgba(255, 255, 255, .8) inset;
}
.login-story {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 38px 38px 30px;
  color: #eaf2ff;
  background:
    radial-gradient(540px 380px at 12% 0%, rgba(59, 130, 246, .42), transparent 64%),
    radial-gradient(540px 420px at 96% 26%, rgba(124, 58, 237, .34), transparent 64%),
    radial-gradient(620px 420px at 58% 112%, rgba(6, 182, 212, .26), transparent 64%),
    linear-gradient(145deg, #071426 0%, #0b1c37 58%, #111a36 100%);
}
.login-story::before {
  content: "";
  position: absolute; inset: 0;
  background-image: linear-gradient(rgba(148, 190, 255, .08) 1px, transparent 1px), linear-gradient(90deg, rgba(148, 190, 255, .08) 1px, transparent 1px);
  background-size: 32px 32px;
  mask-image: radial-gradient(ellipse at 50% 40%, #000 8%, transparent 76%);
  opacity: .55;
}
.login-story-glow {
  position: absolute; width: 340px; height: 340px; right: -100px; bottom: -100px;
  border-radius: 50%; background: radial-gradient(circle, rgba(103, 232, 249, .22), transparent 68%);
}
.login-brand { position: relative; z-index: 1; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.login-brand > span { font-size: 9.5px; letter-spacing: .19em; color: rgba(191, 219, 254, .52); }
.login-story-copy { position: relative; z-index: 1; margin-top: 66px; }
.login-kicker { display: inline-flex; align-items: center; gap: 8px; font-size: 10px; letter-spacing: .17em; color: rgba(191, 219, 254, .68); }
.login-kicker i { width: 18px; height: 1px; background: #60a5fa; box-shadow: 0 0 8px #60a5fa; }
.login-story-copy h1 {
  margin: 15px 0 14px; font-size: clamp(30px, 3.4vw, 42px); line-height: 1.18; letter-spacing: -.048em; font-weight: 760; color: #f5f9ff;
}
.login-story-copy h1 em {
  font-style: normal;
  background: linear-gradient(90deg, #93c5fd, #c4b5fd 55%, #67e8f9);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.login-story-copy p { margin: 0; max-width: 410px; font-size: 13px; line-height: 1.8; color: rgba(203, 219, 244, .72); }
.login-flow {
  position: relative; z-index: 1; display: flex; align-items: center; flex-wrap: wrap; gap: 7px;
  margin-top: 30px; font-size: 10px; color: rgba(210, 231, 255, .72);
}
.login-flow span { padding: 5px 9px; border-radius: 999px; border: 1px solid rgba(148, 197, 255, .18); background: rgba(96, 165, 250, .09); }
.login-flow i { width: 14px; height: 1px; background: rgba(147, 197, 253, .3); }
.login-story-foot {
  position: relative; z-index: 1; margin-top: auto; display: flex; justify-content: space-between; gap: 14px;
  padding-top: 18px; border-top: 1px solid rgba(148, 197, 255, .13);
  font-size: 9.5px; letter-spacing: .04em; color: rgba(191, 219, 254, .42);
}
.login-panel { position: relative; display: flex; flex-direction: column; padding: 46px 40px 28px; background: rgba(255, 255, 255, .86); }
.login-panel-head > span { font-size: 9.5px; letter-spacing: .18em; font-weight: 650; color: #8a9bb3; }
.login-panel-head h2 { margin: 11px 0 7px; font-size: 27px; letter-spacing: -.04em; color: #111827; font-weight: 720; }
.login-panel-head p { margin: 0 0 26px; font-size: 12.5px; line-height: 1.6; color: #7b889b; }
.login-submit { width: 100%; height: 45px; border-radius: 13px; font-weight: 600; letter-spacing: .01em; box-shadow: 0 10px 24px rgba(37, 99, 235, .22); }
.login-hint { margin-top: 22px; padding-top: 17px; border-top: 1px solid #edf1f6; }
.login-hint > span { display: inline-flex; align-items: center; gap: 7px; font-size: 10px; color: #8a9bb3; }
.login-hint > span i { width: 6px; height: 6px; border-radius: 50%; background: #34c759; box-shadow: 0 0 0 3px rgba(52, 199, 89, .10); }
.login-hint p { margin: 9px 0 0; font-size: 11px; line-height: 1.65; color: #96a3b6; }
.login-hint code { padding: 1px 4px; border-radius: 4px; background: #f1f4f9; color: #64748b; font-size: 10px; }
.login-foot { margin-top: auto; padding-top: 20px; font-size: 9.5px; letter-spacing: .03em; color: #b0bdcf; }
@media (max-width: 820px) {
  .login-wrap { padding: 16px; align-items: stretch; }
  .login-shell { display: flex; flex-direction: column; min-height: 0; border-radius: 24px; }
  .login-story { padding: 24px 22px 18px; min-height: 230px; }
  .login-story-copy { margin-top: 30px; }
  .login-story-copy h1 { font-size: 27px; }
  .login-story-copy p { font-size: 12px; }
  .login-flow { display: none; }
  .login-story-foot { margin-top: 20px; padding-top: 12px; }
  .login-panel { padding: 28px 22px 20px; }
  .login-panel-head h2 { font-size: 23px; }
  .login-panel-head p { margin-bottom: 20px; }
}
</style>
