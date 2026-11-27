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
    <div class="login-grid" aria-hidden="true"></div>
    <div class="login-shell">
      <section class="login-story">
        <div class="login-brand">
          <BrandLogo :size="46" light />
          <span class="login-brand-badge">NODE ASSET OS</span>
        </div>
        <div class="login-story-copy">
          <span class="login-kicker"><i></i> PRIVATE NODE INFRASTRUCTURE</span>
          <h1>把散落的机场节点<br />汇成一个<em>可运营的资产池</em></h1>
          <p>统一接入订阅，缓存全部节点，执行双阶段检测，用评分与成熟度筛选，再以单一入口交付给所有客户端。</p>
        </div>
        <div class="login-telemetry">
          <div><span>SOURCES</span><b>多机场</b><i>独立拉取 · 失败回滚</i></div>
          <div><span>PIPELINE</span><b>双阶段</b><i>延迟 · 标签 · 评分</i></div>
          <div><span>DELIVERY</span><b>单订阅</b><i>原子替换 · Token</i></div>
        </div>
        <div class="login-flow">
          <span>机场接入</span><i></i><span>全量缓存</span><i></i><span>双阶段检测</span><i></i><span>统一发布</span>
        </div>
        <div class="login-story-foot">
          <span><i></i> LOCAL FIRST</span>
          <span>检测只读取本地缓存</span>
          <span>v2.005</span>
        </div>
      </section>

      <section class="login-panel">
        <div class="login-panel-head">
          <span>SECURE ACCESS / ADMIN</span>
          <h2>进入控制台</h2>
          <p>输入管理密码，查看节点池的实时状态与运行节奏。</p>
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
        <div class="login-foot">
          <span>NODEPOOLME</span>
          <span>多机场缓存池与统一订阅</span>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  padding: 32px;
  background:
    radial-gradient(900px 540px at 8% 0%, #e7efdf, transparent 64%),
    radial-gradient(800px 520px at 96% 96%, #f1e6cb, transparent 62%),
    linear-gradient(140deg, #f8f4e9, #edf3ea);
}
.login-grid {
  position: absolute; inset: 0; pointer-events: none; opacity: .38;
  background-image: linear-gradient(rgba(47, 77, 70, .08) 1px, transparent 1px), linear-gradient(90deg, rgba(47, 77, 70, .08) 1px, transparent 1px);
  background-size: 46px 46px;
  mask-image: radial-gradient(ellipse at 50% 28%, #000, transparent 74%);
}
.login-shell {
  position: relative;
  width: min(1000px, 100%);
  min-height: 580px;
  display: grid;
  grid-template-columns: 1.14fr .86fr;
  overflow: hidden;
  border: 1px solid #dfe7dc;
  border-radius: 30px;
  background: #f7f4eb;
  box-shadow: 0 42px 110px rgba(35, 62, 51, .20), inset 0 1px 0 #fff;
}
.login-story {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 38px 40px 28px;
  background: linear-gradient(154deg, #33574d 0%, #244840 52%, #1d3a32 100%);
  color: #f5f8ef;
}
.login-story::after {
  content: ""; position: absolute; width: 320px; height: 320px; right: -130px; bottom: -150px;
  border-radius: 50%; background: radial-gradient(circle, #3f645a, transparent 70%);
  pointer-events: none;
}
.login-brand { position: relative; z-index: 1; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.login-brand-badge {
  padding: 5px 9px; border: 1px solid rgba(232, 195, 109, .42); border-radius: 999px;
  background: #3c5e55; color: #ecd29a; font-size: 8.5px; font-weight: 700; letter-spacing: .14em;
}
.login-story-copy { position: relative; z-index: 1; margin-top: 48px; }
.login-kicker { display: inline-flex; align-items: center; gap: 8px; font-size: 9px; font-weight: 700; letter-spacing: .17em; color: #e8c36d; }
.login-kicker i { width: 18px; height: 1px; background: #e8c36d; }
.login-story-copy h1 {
  margin: 15px 0 14px; font-size: clamp(30px, 3.2vw, 43px); line-height: 1.18; letter-spacing: -.05em; font-weight: 760; color: #f7f8ef;
}
.login-story-copy h1 em { font-style: normal; color: #e8c36d; }
.login-story-copy p { margin: 0; max-width: 430px; font-size: 12.5px; line-height: 1.84; color: rgba(244, 248, 242, .72); }
.login-telemetry { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 30px; }
.login-telemetry div { padding: 12px 12px 10px; border: 1px solid #45685e; border-radius: 13px; background: #35564d; }
.login-telemetry span { display: block; font-size: 7.5px; font-weight: 700; letter-spacing: .13em; color: rgba(244, 248, 242, .45); }
.login-telemetry b { display: block; margin: 5px 0 4px; color: #f2f5ea; font-size: 14px; font-weight: 650; }
.login-telemetry i { display: block; font-style: normal; font-size: 8px; color: rgba(244, 248, 242, .55); white-space: nowrap; }
.login-flow { position: relative; z-index: 1; display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 22px; font-size: 8.5px; color: #ecd29a; }
.login-flow span { padding: 5px 9px; border-radius: 999px; border: 1px solid #4c6a60; background: #31524a; }
.login-flow i { width: 12px; height: 1px; background: #7e9388; }
.login-story-foot {
  position: relative; z-index: 1; margin-top: auto; display: flex; align-items: center; gap: 13px; flex-wrap: wrap;
  padding-top: 18px; border-top: 1px solid #45645a;
  font-size: 8px; letter-spacing: .08em; color: rgba(244, 248, 242, .45);
}
.login-story-foot span:first-child { display: inline-flex; align-items: center; gap: 6px; color: #e8c36d; }
.login-story-foot i { width: 5px; height: 5px; border-radius: 50%; background: #e8c36d; }
.login-story-foot span:last-child { margin-left: auto; }
.login-panel {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 52px 40px 28px;
  border-left: 1px solid #e4e9de;
  background: #f8f5ec;
}
.login-panel-head > span { font-size: 8.5px; font-weight: 700; letter-spacing: .18em; color: #93a29b; }
.login-panel-head h2 { margin: 12px 0 8px; font-size: 28px; letter-spacing: -.042em; color: #1d352e; font-weight: 740; }
.login-panel-head p { margin: 0 0 28px; font-size: 12px; line-height: 1.65; color: #73837b; }
.login-panel :deep(.el-input__wrapper) {
  height: 48px; border-radius: 12px !important; background: #fff !important;
  border: 1px solid #dfe7da !important; box-shadow: none !important;
}
.login-panel :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px #2f5d51 inset !important; }
.login-submit {
  width: 100%; height: 46px; margin-top: 4px; border-radius: 12px; font-weight: 650; letter-spacing: .02em;
  --el-button-bg-color: #2f5d51; --el-button-border-color: #2f5d51; --el-button-text-color: #fff;
  --el-button-hover-bg-color: #3b6b5d; --el-button-hover-border-color: #3b6b5d;
  background-color: #2f5d51 !important; background-image: none !important; border-color: #2f5d51 !important; color: #fff !important;
  box-shadow: 0 12px 24px rgba(47, 93, 81, .20);
}
.login-submit:hover { background-color: #3b6b5d !important; background-image: none !important; border-color: #3b6b5d !important; }
.login-hint { margin-top: 24px; padding-top: 18px; border-top: 1px solid #e3e9df; }
.login-hint > span { display: inline-flex; align-items: center; gap: 7px; font-size: 9.5px; color: #6f8178; }
.login-hint > span i { width: 6px; height: 6px; border-radius: 50%; background: #b88624; box-shadow: 0 0 0 3px #f4ead3; }
.login-hint p { margin: 9px 0 0; font-size: 10px; line-height: 1.7; color: #7e8d85; }
.login-hint code { padding: 2px 5px; border: 1px solid #dfe7da; border-radius: 5px; background: #f1f5ed; color: #52685c; font-size: 9px; }
.login-foot { margin-top: auto; padding-top: 22px; display: flex; justify-content: space-between; gap: 10px; font-size: 8px; letter-spacing: .08em; color: #9aa8a1; }
@media (max-width: 820px) {
  .login-wrap { padding: 14px; align-items: stretch; }
  .login-shell { display: flex; flex-direction: column; min-height: 0; border-radius: 24px; }
  .login-story { padding: 24px 22px 20px; }
  .login-story-copy { margin-top: 30px; }
  .login-story-copy h1 { font-size: 28px; }
  .login-story-copy p { font-size: 11.5px; }
  .login-telemetry { display: none; }
  .login-flow { display: none; }
  .login-story-foot { margin-top: 22px; padding-top: 13px; }
  .login-panel { padding: 26px 22px 22px; border-left: 0; border-top: 1px solid #e4e9de; }
  .login-panel-head h2 { font-size: 24px; }
  .login-panel-head p { margin-bottom: 20px; }
}
</style>
