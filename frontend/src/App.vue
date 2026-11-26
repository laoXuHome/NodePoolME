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
  { path: '/dashboard', label: '控制总览', short: '总览', code: '01', icon: 'Odometer' },
  { path: '/airports', label: '机场接入', short: '机场', code: '02', icon: 'OfficeBuilding' },
  { path: '/nodes', label: '节点池', short: '节点', code: '03', icon: 'Connection' },
  { path: '/groups', label: '检测标签', short: '标签', code: '04', icon: 'Aim' },
  { path: '/subscription', label: '订阅分发', short: '订阅', code: '05', icon: 'Link' },
  { path: '/settings', label: '系统设置', short: '设置', code: '06', icon: 'Setting' }
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

  <div v-else-if="authed" class="np-shell np-console">
    <aside v-if="!isMobile" class="np-rail" aria-label="主导航">
      <div class="np-rail-brand">
        <BrandLogo :size="42" light />
        <span class="np-rail-badge">CORE</span>
      </div>

      <div class="np-rail-caption">WORKSPACE</div>
      <nav class="np-rail-nav">
        <button v-for="m in menus" :key="m.path" class="np-rail-link"
                :class="{ active: route.path === m.path }" @click="onMenu(m.path)">
          <span class="np-rail-code">{{ m.code }}</span>
          <el-icon><component :is="m.icon" /></el-icon>
          <span class="np-rail-label">{{ m.label }}</span>
          <i class="np-rail-dot" />
        </button>
      </nav>

      <div class="np-rail-spacer" />
      <div class="np-rail-sys">
        <span>RUNTIME MODE</span>
        <b><i></i> LOCAL FIRST</b>
        <small>检测只读本地缓存，不触发机场拉取</small>
      </div>
      <div class="np-rail-foot">
        <span class="np-rail-ver">v{{ version }}</span>
        <button class="np-rail-out" @click="doLogout">退出</button>
      </div>
    </aside>

    <section class="np-stage">
      <header class="np-commandbar">
        <div v-if="isMobile" class="np-mobile-brand">
          <BrandLogo :size="30" compact light />
        </div>
        <div v-else class="np-command-crumb">
          <span>NODEPOOL /</span>
          <b>{{ routeMeta.title || '控制总览' }}</b>
        </div>
        <div class="np-command-spacer" />
        <span v-if="!isMobile" class="np-system-live"><i></i> SYSTEM LIVE</span>
        <span v-if="isMobile" class="np-ver is-mobile" title="当前版本"><i class="np-ver-dot" /><span class="np-ver-text">v{{ version }}</span></span>
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
