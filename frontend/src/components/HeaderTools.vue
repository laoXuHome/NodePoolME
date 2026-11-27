<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { checkUpdate, logout, state } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

defineProps({ compact: { type: Boolean, default: false } })

const router = useRouter()
const version = computed(() => state.version || '1.0.0')
const checking = ref(false)
const update = ref(null)
const verTitle = computed(() => {
  if (!update.value) return '当前版本'
  if (update.value.error) return update.value.error
  if (update.value.has_update) return `最新 v${update.value.latest}`
  return `已是最新（最新 v${update.value.latest}）`
})

async function runCheck() {
  if (checking.value) return
  checking.value = true
  try {
    const data = await checkUpdate(true)
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
          <div>离线包：${asset}</div></div>`,
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
  try {
    await ElMessageBox.confirm('确定退出登录吗？', '提示', { type: 'warning' })
    await logout()
    router.replace('/login')
  } catch { /* 取消 */ }
}
</script>

<template>
  <div class="np-header-tools" :class="{ 'is-compact': compact }">
    <a class="np-gh" href="https://github.com/laoXuHome/NodePoolME" target="_blank" rel="noopener"
       title="GitHub · NodePoolME" aria-label="打开 NodePoolME 项目主页">
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.015-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.295-1.552 3.3-1.23 3.3-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 21.795 24 17.295 24 12c0-6.63-5.37-12-12-12" />
      </svg>
      <span>NodePoolME</span>
    </a>
    <span class="np-ver" :title="verTitle">
      <i class="np-ver-dot" :class="{ 'is-new': update && update.has_update }" />
      <span class="np-ver-text">v{{ version }}</span>
    </span>
    <button class="np-mini-btn" :class="{ 'is-busy': checking }" title="对比 GitHub 最新离线包" @click="runCheck">
      <el-icon><Refresh /></el-icon>
      <span>{{ checking ? '检测中' : '检测更新' }}</span>
    </button>
    <button class="np-logout" title="退出登录" aria-label="退出登录" @click="doLogout">
      <el-icon><SwitchButton /></el-icon>
      <span>退出</span>
    </button>
  </div>
</template>
