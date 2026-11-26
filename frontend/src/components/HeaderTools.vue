<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { checkUpdate, logout, state } from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

defineProps({ compact: { type: Boolean, default: false } })

const router = useRouter()
const version = computed(() => state.version || '1.0.0')
const repoUrl = 'https://github.com/laoXuHome/NodePoolME'
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
    <span class="np-ver" :title="verTitle">
      <i class="np-ver-dot" :class="{ 'is-new': update && update.has_update }" />
      <span class="np-ver-text">v{{ version }}</span>
    </span>
    <button class="np-mini-btn" :class="{ 'is-busy': checking }" title="对比 GitHub 最新离线包" @click="runCheck">
      <el-icon><Refresh /></el-icon>
      <span>{{ checking ? '检测中' : '检测更新' }}</span>
    </button>
    <a class="np-gh" :href="repoUrl" target="_blank" rel="noopener">GitHub ↗</a>
    <button class="np-quit" @click="doLogout">退出</button>
  </div>
</template>
