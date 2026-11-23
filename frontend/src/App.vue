<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { checkAuth, checkUpdate, logout, state } from './api'
import { ElMessage, ElMessageBox } from 'element-plus'

const route = useRoute()
const router = useRouter()
const authed = computed(() => state.loggedIn)
const ready = ref(false)
const version = computed(() => state.version || '1.0.0')
const repoUrl = 'https://github.com/laoXuHome/NodePoolME'

const isLogin = computed(() => route.name === 'login')
const isMobile = ref(false)
let mq = null
function syncMobile(e) {
  isMobile.value = e.matches
}
const title = computed(() => route.meta.title || 'NodePoolME')

const menus = [
  { path: '/dashboard', label: '仪表盘', short: '概览', icon: 'Odometer' },
  { path: '/airports', label: '机场管理', short: '机场', icon: 'OfficeBuilding' },
  { path: '/nodes', label: '缓存池', short: '缓存', icon: 'Connection' },
  { path: '/groups', label: '节点标签', short: '标签', icon: 'Aim' },
  { path: '/subscription', label: '订阅输出', short: '订阅', icon: 'Link' },
  { path: '/settings', label: '系统设置', short: '设置', icon: 'Setting' }
]

async function refresh() {
  if (isLogin.value) { ready.value = true; return }
  await checkAuth()
  ready.value = true
  if (!state.loggedIn) router.replace('/login')
}

const checking = ref(false)
const update = ref(null)
const verTitle = computed(() => {
  if (!update.value) return '当前版本'
  if (update.value.error) return update.value.error
  if (update.value.has_update) return `最新 v${update.value.latest}`
  return `已是最新（最新 v${update.value.latest}）`
})

async function runCheck(force = true) {
  if (checking.value) return
  checking.value = true
  try {
    const data = await checkUpdate(force)
    update.value = data
    if (data.error) {
      ElMessage.warning(data.error)
    } else if (data.has_update) {
      const asset = !data.asset ? '离线包未打包'
        : (data.asset.size ? `${data.asset.name}（${(data.asset.size / 1048576).toFixed(1)}MB）` : data.asset.name)
      ElMessageBox({
        title: '发现新版本',
        message: `<div style="line-height:1.9;font-size:13px">
          <div>当前版本：<b>v${data.current}</b></div>
          <div>最新版本：<b style="color:#2f6bdd">v${data.latest}</b></div>
          <div>离线包：${asset}</div>
          <div style="margin-top:6px;color:#8d939d">NAS 升级：<code>sudo npup v${data.latest}</code></div></div>`,
        dangerouslyUseHTMLString: true,
        confirmButtonText: data.asset ? '去下载' : '知道了',
        showCancelButton: !!data.asset,
        cancelButtonText: '稍后',
      }).then(() => {
        if (data.asset) window.open(data.asset.url || data.html_url, '_blank', 'noopener')
      }).catch(() => {})
    } else {
      ElMessage.success(`已是最新版本 v${data.latest}`)
    }
  } catch (e) {
    ElMessage.error('检测更新失败，请检查服务器网络')
  } finally {
    checking.value = false
  }
}

async function doLogout() {
  await ElMessageBox.confirm('确定退出登录吗？', '提示', { type: 'warning' })
  await logout()
  router.replace('/login')
}

onMounted(async () => {
  mq = window.matchMedia('(max-width: 768px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', syncMobile)
  await refresh()
})

onUnmounted(() => { if (mq) mq.removeEventListener('change', syncMobile) })

router.afterEach((to) => {
  if (to.name !== 'login' && ready.value && !state.loggedIn) router.replace('/login')
})

function onMenu(path) {
  router.push(path)
}
</script>

<template>
  <div v-if="!ready" class="np-boot">正在载入…</div>

  <router-view v-else-if="isLogin" />

  <div v-else-if="authed" class="np-shell">
    <aside v-if="!isMobile" class="layout-sidebar">
      <div class="brand">
        <div class="brand-row">
          <span class="brand-mark">NP</span>
          <div>
            <div class="name">NodePoolME</div>
            <div class="sub">MULTI-AIRPORT NODE POOL</div>
          </div>
        </div>
      </div>
      <el-menu :default-active="route.path" @select="onMenu">
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
      </el-menu>
    </aside>

    <section class="np-workspace">
      <header class="header-bar">
        <div v-if="isMobile" class="np-mobile-brand">
          <span class="brand-mark brand-mark-sm">NP</span>
          <span>NodePoolME</span>
        </div>
        <span v-else class="title">{{ title }}</span>
        <div class="np-flex" />
        <div v-if="!isMobile" class="np-header-tools">
          <span class="np-ver" :title="verTitle">
            <i class="np-ver-dot" :class="{ 'is-new': update && update.has_update }" />
            <span class="np-ver-text">v{{ version }}</span>
          </span>
          <button class="np-mini-btn" :class="{ 'is-busy': checking }" title="对比 GitHub 最新离线包" @click="runCheck">
            <el-icon><Refresh /></el-icon>
            <span>{{ checking ? '检测中' : '检测更新' }}</span>
          </button>
          <a class="np-gh" :href="repoUrl" target="_blank" rel="noopener">GitHub ↗</a>
          <el-button link type="primary" @click="doLogout">退出</el-button>
        </div>
        <button v-else class="np-icon-btn" aria-label="退出登录" @click="doLogout">
          <el-icon size="19"><SwitchButton /></el-icon>
        </button>
      </header>
      <main class="np-main">
        <router-view />
      </main>
    </section>

    <nav v-if="isMobile" class="np-tabbar" aria-label="主导航">
      <button v-for="m in menus" :key="m.path" class="np-tab"
              :class="{ active: route.path === m.path }" @click="onMenu(m.path)">
        <el-icon><component :is="m.icon" /></el-icon>
        <span>{{ m.short }}</span>
      </button>
    </nav>
  </div>

  <div v-else class="np-boot">
    需要登录 · <el-button link type="primary" @click="router.replace('/login')">前往登录</el-button>
  </div>
</template>
