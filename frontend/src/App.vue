<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { checkAuth, logout, state } from './api'
import { ElMessageBox, ElMessage } from 'element-plus'

const route = useRoute()
const router = useRouter()
const authed = computed(() => state.loggedIn)
const ready = ref(false)
const version = computed(() => state.version || '1.0.0')
const repoUrl = 'https://github.com/laoXuHome/NodePoolME'

const isLogin = computed(() => route.name === 'login')
const navOpen = ref(false)
const isMobile = ref(false)
let mq = null
function syncMobile(e) {
  isMobile.value = e.matches
  if (!e.matches) navOpen.value = false
}
const title = computed(() => route.meta.title || 'NodePoolME')

const menus = [
  { path: '/dashboard', label: '仪表盘', icon: 'Odometer' },
  { path: '/airports', label: '机场管理', icon: 'OfficeBuilding' },
  { path: '/nodes', label: '节点池', icon: 'Connection' },
  { path: '/groups', label: '节点标签', icon: 'Aim' },
  { path: '/subscription', label: '订阅输出', icon: 'Link' },
  { path: '/settings', label: '系统设置', icon: 'Setting' }
]

async function refresh() {
  if (isLogin.value) { ready.value = true; return }
  await checkAuth()
  ready.value = true
  if (!state.loggedIn) router.replace('/login')
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
  navOpen.value = false
}
</script>

<template>
  <div v-if="!ready" style="padding:60px;text-align:center;color:#98a2b3">加载中…</div>

  <router-view v-else-if="isLogin" />

  <el-container v-else-if="authed" style="min-height:100vh">
    <el-aside v-if="!isMobile" width="208px" class="layout-sidebar">
      <div class="brand">
        <div class="name">NodePoolME</div>
        <div class="sub">MULTI-AIRPORT · NODE POOL</div>
      </div>
      <el-menu :default-active="route.path" @select="onMenu">
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
      </el-menu>
      <div style="padding:14px 20px;font-size:11px;color:#5f6c86;position:absolute;bottom:0">
        <div>v{{ version }} · 本地缓存优先</div>
        <a :href="repoUrl" target="_blank" rel="noopener"
           style="color:#7aa2ff;text-decoration:none;display:inline-block;margin-top:4px">GitHub 仓库 ↗</a>
      </div>
    </el-aside>
    <el-drawer v-model="navOpen" direction="ltr" size="240px" :with-header="false" class="np-nav-drawer">
      <div class="brand">
        <div class="name">NodePoolME</div>
        <div class="sub">MULTI-AIRPORT · NODE POOL</div>
      </div>
      <el-menu :default-active="route.path" @select="onMenu">
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
      </el-menu>
      <div class="np-drawer-foot">v{{ version }} · <a :href="repoUrl" target="_blank" rel="noopener">GitHub ↗</a></div>
    </el-drawer>
    <el-container direction="vertical">
      <el-header height="60px" class="header-bar">
        <el-button v-if="isMobile" text class="nav-toggle" @click="navOpen = true">
          <el-icon size="20"><Menu /></el-icon>
        </el-button>
        <span class="title">{{ title }}</span>
        <div class="np-flex" />
        <el-tag size="small" type="info" effect="plain">admin</el-tag>
        <el-button link type="primary" @click="doLogout">退出</el-button>
      </el-header>
      <el-main style="padding:0;background:#f5f7fb">
        <router-view />
      </el-main>
    </el-container>
  </el-container>

  <div v-else style="padding:80px;text-align:center;color:#98a2b3">
    需要登录 · <el-button link type="primary" @click="router.replace('/login')">前往登录</el-button>
  </div>
</template>
