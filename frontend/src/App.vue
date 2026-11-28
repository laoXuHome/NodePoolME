<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import HeaderTools from './components/HeaderTools.vue'
import RailNetworkMap from './components/RailNetworkMap.vue'
import { useRoute, useRouter } from 'vue-router'
import { api, checkAuth, logout, state } from './api'
import { ElMessageBox } from 'element-plus'

const route = useRoute()
const router = useRouter()
const authed = computed(() => state.loggedIn)
const ready = ref(false)
const version = computed(() => state.version || '1.0.0')

const isLogin = computed(() => route.name === 'login')
const isMobile = ref(false)
let mq = null
function syncMobile(e) { isMobile.value = e.matches }
const routeMeta = computed(() => route.meta || {})

const menus = [
  { path: '/dashboard', label: '控制总览', short: '总览', code: '01', icon: 'Odometer' },
  { path: '/airports', label: '机场接入', short: '机场', code: '02', icon: 'OfficeBuilding' },
  { path: '/nodes', label: '节点池', short: '节点', code: '03', icon: 'Connection' },
  { path: '/nodepool', label: '三层节点池', short: '三层', code: '04', icon: 'Files' },
  { path: '/health', label: '基础健康', short: '健康', code: '05', icon: 'FirstAidKit' },
  { path: '/groups', label: '检测标签', short: '标签', code: '06', icon: 'Aim' },
  { path: '/subscription', label: '订阅分发', short: '订阅', code: '07', icon: 'Link' },
  { path: '/settings', label: '系统设置', short: '设置', code: '08', icon: 'Setting' }
]

const rail = reactive({
  score: null,
  total: 0,
  qualified: 0,
  published: 0,
  airports: 0
})
const railScoreText = computed(() => rail.score === null ? '—' : Number(rail.score).toFixed(1))
const railNetwork = ref({ regions: [], total: 0, available: 0 })
let railTimer = null

function applyRailSummary(dash) {
  if (!dash) return
  const rows = (dash.airport_list || []).filter((row) => row.enabled)
  const scored = rows.filter((row) => Number.isFinite(Number(row.score)))
  let weight = 0
  let sum = 0
  for (const row of scored) {
    const w = Math.max(1, Number(row.pool_count) || 1)
    weight += w
    sum += Number(row.score) * w
  }
  rail.score = weight ? Math.round((sum / weight) * 10) / 10 : null
  rail.total = Number(dash.nodes_total) || 0
  rail.qualified = Number(dash.nodes_qualified) || 0
  rail.published = rows.reduce((acc, row) => acc + Number(row.published_count || 0), 0)
  rail.airports = rows.length
}

async function loadRailSummary() {
  if (!state.loggedIn) return
  try { applyRailSummary(await api.get('/api/dashboard?days=7')) } catch { /* 保持上一帧 */ }
}

async function loadRailNetwork() {
  if (!state.loggedIn) return
  try { railNetwork.value = await api.get('/api/nodes/regions') } catch { /* 保持上一帧 */ }
}

function onDashboard(event) {
  if (event?.detail) applyRailSummary(event.detail)
  loadRailNetwork()
}

watch(authed, (value) => { if (value) loadRailSummary() })

async function refresh() {
  if (isLogin.value) { document.title = '登录 · NodePoolME'; ready.value = true; return }
  await checkAuth()
  ready.value = true
  if (!state.loggedIn) { router.replace('/login'); return }
  document.title = `${routeMeta.value.title || '控制台'} · NodePoolME`
  loadRailSummary()
  loadRailNetwork()
}

async function doLogout() {
  await ElMessageBox.confirm('确定退出登录吗？', '提示', { type: 'warning' })
  await logout()
  router.replace('/login')
}

onMounted(async () => {
  mq = window.matchMedia('(max-width: 860px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', syncMobile)
  window.addEventListener('np:dashboard', onDashboard)
  await refresh()
  railTimer = setInterval(() => { if (state.loggedIn) loadRailNetwork() }, 15000)
})

onUnmounted(() => {
  if (mq) mq.removeEventListener('change', syncMobile)
  window.removeEventListener('np:dashboard', onDashboard)
  if (railTimer) clearInterval(railTimer)
})

router.afterEach((to) => {
  if (to.name !== 'login' && ready.value && !state.loggedIn) { router.replace('/login'); return }
  document.title = to.name === 'login' ? '登录 · NodePoolME' : `${to.meta.title || '控制台'} · NodePoolME`
})

function onMenu(path) { router.push(path) }
</script>

<template>
  <div v-if="!ready" class="np-boot">正在载入…</div>

  <router-view v-else-if="isLogin" />

  <div v-else-if="authed" class="np-shell np-console">
    <aside v-if="!isMobile" class="np-rail" aria-label="主导航">
      <div class="np-rail-navglass">
        <div class="np-side-label">WORKSPACE / MENU</div>
        <nav class="np-rail-nav">
          <button v-for="m in menus" :key="m.path" class="np-rail-link"
                  :class="{ active: route.path === m.path }" @click="onMenu(m.path)">
            <el-icon><component :is="m.icon" /></el-icon>
            <span class="np-rail-label">{{ m.label }}</span>
          </button>
        </nav>
      </div>

      <div class="np-rail-spacer" />
      <div class="np-rail-sys">
        <span>RUNTIME MODE</span>
        <b><i></i> LOCAL FIRST</b>
        <small>检测只读本地缓存，不触发机场拉取</small>
      </div>
    </aside>

    <header v-if="isMobile" class="np-mrail">
      <div class="np-rail-mark is-small">N</div>
      <div class="np-mrail-title">NODEPOOL</div>
      <nav class="np-mrail-nav" aria-label="主导航">
        <button v-for="m in menus" :key="m.path" class="np-mrail-link"
                :class="{ active: route.path === m.path }" :aria-label="m.label" @click="onMenu(m.path)">
          <el-icon><component :is="m.icon" /></el-icon>
        </button>
      </nav>
    </header>

    <section class="np-stage">
      <section v-if="!isMobile" class="np-stage-headrow">
        <section class="np-console-head">
          <div class="np-rail-brand np-console-brand">
            <div class="np-rail-mark">N</div>
            <div class="np-rail-brandtext"><b>NODEPOOL</b><small>SCORE ROOM / 评分控制台</small></div>
            <span class="np-console-page">{{ routeMeta.title || '控制总览' }}</span>
          </div>
          <div class="np-console-mesh">
            <RailNetworkMap compact :regions="railNetwork.regions" :score="railScoreText"
                            :available="railNetwork.available" :total="railNetwork.total" />
          </div>
        </section>
        <div class="np-console-spacer" />
        <div class="np-console-tools">
          <span class="np-system-live"><i></i> SYSTEM LIVE</span>
          <HeaderTools compact />
        </div>
      </section>

      <header v-if="isMobile" class="np-commandbar">
        <div class="np-command-crumb">
          <span>NODEPOOL /</span>
          <b>{{ routeMeta.title || '控制总览' }}</b>
        </div>
        <div class="np-command-spacer" />
        <span v-if="!isMobile" class="np-system-live"><i></i> SYSTEM LIVE</span>
        <span v-if="isMobile" class="np-ver is-mobile" title="当前版本"><i class="np-ver-dot" /><span class="np-ver-text">v{{ version }}</span></span>
        <a v-if="isMobile" class="np-icon-btn np-mobile-gh" href="https://github.com/laoXuHome/NodePoolME"
           target="_blank" rel="noopener" title="GitHub · NodePoolME" aria-label="打开 NodePoolME 项目主页">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path fill="currentColor" d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.015-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.295-1.552 3.3-1.23 3.3-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 21.795 24 17.295 24 12c0-6.63-5.37-12-12-12" />
          </svg>
        </a>
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
  </div>

  <div v-else class="np-boot">
    需要登录 · <el-button link type="primary" @click="router.replace('/login')">前往登录</el-button>
  </div>
</template>
