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
          <span>v2</span>
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
    radial-gradient(1000px 620px at 10% -12%, rgba(46, 230, 168, .11), transparent 62%),
    radial-gradient(900px 620px at 96% 12%, rgba(124, 58, 237, .17), transparent 62%),
    linear-gradient(145deg, #070b12, #060a11);
}
.login-grid {
  position: absolute; inset: 0; pointer-events: none; opacity: .2;
  background-image: linear-gradient(rgba(139, 168, 205, .10) 1px, transparent 1px), linear-gradient(90deg, rgba(139, 168, 205, .10) 1px, transparent 1px);
  background-size: 44px 44px;
  mask-image: radial-gradient(ellipse at 50% 36%, #000, transparent 72%);
}
.login-shell {
  position: relative;
  width: min(1040px, 100%);
  min-height: 620px;
  display: grid;
  grid-template-columns: 1.16fr .84fr;
  overflow: hidden;
  border: 1px solid rgba(139, 168, 205, .16);
  border-radius: 28px;
  background: rgba(11, 18, 29, .82);
  box-shadow: 0 50px 130px rgba(0, 0, 0, .55), inset 0 1px 0 rgba(255, 255, 255, .05);
  backdrop-filter: blur(28px);
}
.login-story {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 38px 40px 30px;
  background:
    radial-gradient(580px 420px at 12% 0%, rgba(46, 230, 168, .12), transparent 60%),
    radial-gradient(520px 380px at 100% 28%, rgba(124, 58, 237, .19), transparent 64%),
    linear-gradient(150deg, rgba(10, 20, 32, .9), rgba(7, 12, 20, .88));
}
.login-story::after {
  content: ""; position: absolute; width: 300px; height: 300px; right: -110px; bottom: -120px;
  border-radius: 50%; background: radial-gradient(circle, rgba(46, 230, 168, .16), transparent 68%);
  pointer-events: none;
}
.login-brand { position: relative; z-index: 1; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.login-brand-badge {
  padding: 5px 8px; border: 1px solid rgba(46, 230, 168, .20); border-radius: 999px;
  background: rgba(46, 230, 168, .06); color: #7ae4c0; font-size: 8.5px; font-weight: 700; letter-spacing: .14em;
}
.login-story-copy { position: relative; z-index: 1; margin-top: 56px; }
.login-kicker { display: inline-flex; align-items: center; gap: 8px; font-size: 9.5px; font-weight: 700; letter-spacing: .17em; color: #55d4ae; }
.login-kicker i { width: 18px; height: 1px; background: #2ee6a8; box-shadow: 0 0 9px rgba(46, 230, 168, .8); }
.login-story-copy h1 {
  margin: 15px 0 14px; font-size: clamp(31px, 3.35vw, 45px); line-height: 1.16; letter-spacing: -.052em; font-weight: 760; color: #f6fbff;
}
.login-story-copy h1 em {
  font-style: normal;
  background: linear-gradient(92deg, #2ee6a8, #91f3d5 52%, #c9b6ff);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.login-story-copy p { margin: 0; max-width: 440px; font-size: 13px; line-height: 1.82; color: #8ca1b8; }
.login-telemetry { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 34px; }
.login-telemetry div { padding: 12px 12px 10px; border: 1px solid rgba(139, 168, 205, .11); border-radius: 12px; background: rgba(255, 255, 255, .025); }
.login-telemetry span { display: block; font-size: 8px; font-weight: 700; letter-spacing: .13em; color: #52677e; }
.login-telemetry b { display: block; margin: 5px 0 4px; color: #dff9ef; font-size: 14px; font-weight: 650; }
.login-telemetry i { display: block; font-style: normal; font-size: 8.5px; color: #6d8299; white-space: nowrap; }
.login-flow { position: relative; z-index: 1; display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 22px; font-size: 9px; color: #8fe9ce; }
.login-flow span { padding: 5px 9px; border-radius: 999px; border: 1px solid rgba(46, 230, 168, .13); background: rgba(46, 230, 168, .04); }
.login-flow i { width: 12px; height: 1px; background: rgba(46, 230, 168, .25); }
.login-story-foot {
  position: relative; z-index: 1; margin-top: auto; display: flex; align-items: center; gap: 13px; flex-wrap: wrap;
  padding-top: 18px; border-top: 1px solid rgba(139, 168, 205, .09);
  font-size: 8.5px; letter-spacing: .08em; color: #52677e;
}
.login-story-foot span:first-child { display: inline-flex; align-items: center; gap: 6px; color: #77cbb1; }
.login-story-foot i { width: 5px; height: 5px; border-radius: 50%; background: #2ee6a8; box-shadow: 0 0 8px rgba(46, 230, 168, .7); }
.login-story-foot span:last-child { margin-left: auto; }
.login-panel {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 54px 40px 30px;
  border-left: 1px solid rgba(139, 168, 205, .10);
  background:
    radial-gradient(340px 260px at 100% 0%, rgba(46, 230, 168, .035), transparent 70%),
    rgba(9, 15, 24, .66);
}
.login-panel-head > span { font-size: 9px; font-weight: 700; letter-spacing: .18em; color: #6d8299; }
.login-panel-head h2 { margin: 12px 0 8px; font-size: 28px; letter-spacing: -.042em; color: #f1f8ff; font-weight: 720; }
.login-panel-head p { margin: 0 0 28px; font-size: 12.5px; line-height: 1.65; color: #7e93aa; }
.login-panel :deep(.el-input__wrapper) { height: 48px; border-radius: 12px !important; background: rgba(6, 12, 20, .76) !important; }
.login-submit {
  width: 100%; height: 46px; margin-top: 4px; border-radius: 12px; font-weight: 650; letter-spacing: .02em;
  box-shadow: 0 12px 28px rgba(46, 230, 168, .19);
}
.login-hint { margin-top: 24px; padding-top: 18px; border-top: 1px solid rgba(139, 168, 205, .09); }
.login-hint > span { display: inline-flex; align-items: center; gap: 7px; font-size: 10px; color: #71869d; }
.login-hint > span i { width: 6px; height: 6px; border-radius: 50%; background: #2ee6a8; box-shadow: 0 0 0 3px rgba(46, 230, 168, .09); }
.login-hint p { margin: 9px 0 0; font-size: 10.5px; line-height: 1.7; color: #61768d; }
.login-hint code { padding: 2px 5px; border: 1px solid rgba(139, 168, 205, .12); border-radius: 5px; background: rgba(139, 168, 205, .06); color: #98b1c9; font-size: 9.5px; }
.login-foot { margin-top: auto; padding-top: 22px; display: flex; justify-content: space-between; gap: 10px; font-size: 8.5px; letter-spacing: .08em; color: #465a70; }
@media (max-width: 820px) {
  .login-wrap { padding: 14px; align-items: stretch; }
  .login-shell { display: flex; flex-direction: column; min-height: 0; border-radius: 22px; }
  .login-story { padding: 25px 22px 20px; }
  .login-story-copy { margin-top: 32px; }
  .login-story-copy h1 { font-size: 29px; }
  .login-story-copy p { font-size: 11.5px; }
  .login-telemetry { display: none; }
  .login-flow { display: none; }
  .login-story-foot { margin-top: 24px; padding-top: 13px; }
  .login-panel { padding: 28px 22px 22px; border-left: 0; border-top: 1px solid rgba(139, 168, 205, .10); }
  .login-panel-head h2 { font-size: 24px; }
  .login-panel-head p { margin-bottom: 20px; }
}
</style>
