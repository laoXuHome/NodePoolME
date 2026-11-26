<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import HeaderTools from './components/HeaderTools.vue'
import BrandLogo from './components/BrandLogo.vue'
import { useRoute, useRouter } from 'vue-router'
import { checkAuth, logout, state } from './api'
import { ElMessageBox } from 'element-plus'

const route = useRoute()
const router = useRouter()
const authed = computed(() => state.loggedIn)
const ready = ref(false)
const version = computed(() => state.version || '1.0.0')

const isLogin = computed(() => route.name === 'login')
const isMobile = ref(false)
let mq = null
function syncMobile(e) {
  isMobile.value = e.matches
}
const routeMeta = computed(() => route.meta || {})

const menus = [
  { path: '/dashboard', label: '仪表盘', short: '概览', icon: 'Odometer' },
  { path: '/airports', label: '机场管理', short: '机场', icon: 'OfficeBuilding' },
  { path: '/nodes', label: '缓存池', short: '缓存', icon: 'Connection' },
  { path: '/groups', label: '检测标签', short: '标签', icon: 'Aim' },
  { path: '/subscription', label: '订阅输出', short: '订阅', icon: 'Link' },
  { path: '/settings', label: '系统设置', short: '设置', icon: 'Setting' }
]

async function refresh() {
  if (isLogin.value) { document.title = '登录 · NodePoolME'; ready.value = true; return }
  await checkAuth()
  ready.value = true
  if (!state.loggedIn) { router.replace('/login'); return }
  document.title = `${routeMeta.value.title || '控制台'} · NodePoolME`
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
  if (to.name !== 'login' && ready.value && !state.loggedIn) { router.replace('/login'); return }
  document.title = to.name === 'login' ? '登录 · NodePoolME' : `${to.meta.title || '控制台'} · NodePoolME`
})

function onMenu(path) {
  router.push(path)
}
</script>

<template>
  <div v-if="!ready" class="np-boot">正在载入…</div>

  <router-view v-else-if="isLogin" />

  <div v-else-if="authed" class="np-shell">
    <section class="np-workspace">
      <header class="header-bar">
        <div v-if="isMobile" class="np-mobile-brand">
          <BrandLogo :size="30" compact />
        </div>
        <div v-else class="np-brandline">
          <BrandLogo :size="48" />
        </div>
        <nav v-if="!isMobile" class="np-tabsnav" aria-label="主导航">
          <button v-for="m in menus" :key="m.path" class="np-tabx"
                  :class="{ active: route.path === m.path }" @click="onMenu(m.path)">
            <el-icon><component :is="m.icon" /></el-icon>
            <span>{{ m.label }}</span>
          </button>
        </nav>
        <div class="np-flex" />
        <div v-if="isMobile" class="np-mobile-tools">
          <span class="np-ver is-mobile" title="当前版本">
            <i class="np-ver-dot" />
            <span class="np-ver-text">v{{ version }}</span>
          </span>
        </div>
        <HeaderTools v-if="!isMobile" compact />
        <button v-if="isMobile" class="np-icon-btn" aria-label="退出登录" @click="doLogout">
          <el-icon size="19"><SwitchButton /></el-icon>
        </button>
      </header>
      <main class="np-main">
        <section v-if="route.name !== 'dashboard' && routeMeta.title" class="np-route-masthead">
          <div class="np-route-mast-copy">
            <span class="np-route-eyebrow">{{ routeMeta.eyebrow || 'NODEPOOL CONTROL' }}</span>
            <h1>{{ routeMeta.title }}</h1>
            <p>{{ routeMeta.desc }}</p>
          </div>
          <div class="np-route-mast-side">
            <span class="np-route-dot"></span>
            <span>{{ routeMeta.side || 'OPERATIONS' }}</span>
          </div>
        </section>
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
