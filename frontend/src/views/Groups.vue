<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const items = ref([])
const loading = ref(false)
const dialog = ref(false)
const editing = ref(null)
const blank = () => ({ name: '', ident: '', url: '', method: 'GET', expect_status: '200-399',
                       expect_body: '', max_latency: 0, enabled: true, sort_order: 10 })
const form = ref(blank())
const isMobile = ref(false)
const speedForm = ref({})
const speedSources = ref([])
const speedChecking = ref(false)
const speedSaving = ref(false)
let mq = null
function syncMobile(e) { isMobile.value = e.matches }

async function load() {
  loading.value = true
  try { items.value = await api.get('/api/groups') } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function openCreate() { editing.value = null; form.value = blank(); dialog.value = true }
function openEdit(row) { editing.value = row; form.value = { ...row }; dialog.value = true }

async function submit() {
  if (!form.value.name || !form.value.ident || !form.value.url) return ElMessage.warning('名称、标识、检测 URL 必填')
  const payload = {
    name: form.value.name, ident: form.value.ident, url: form.value.url, method: form.value.method,
    expect_status: form.value.expect_status,
    expect_body: form.value.expect_body, max_latency: form.value.max_latency,
    enabled: form.value.enabled,
    sort_order: form.value.sort_order
  }
  try {
    if (editing.value) {
      await api.patch(`/api/groups/${editing.value.id}`, payload)
      ElMessage.success('已更新节点标签')
    } else {
      await api.post('/api/groups', payload)
      ElMessage.success('已新增节点标签')
    }
    dialog.value = false
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

async function toggle(row, enabled) {
  try { await api.patch(`/api/groups/${row.id}`, { enabled }); await load() }
  catch (e) { ElMessage.error(e.message) }
}

async function remove(row) {
  await ElMessageBox.confirm(`删除节点标签「${row.name}」会同时删除其检测结果，节点名的中间段将不再包含 ${row.name}。`,
                             '确认删除', { type: 'warning' })
  try { await api.del(`/api/groups/${row.id}`); ElMessage.success('已删除'); await load() }
  catch (e) { ElMessage.error(e.message) }
}

async function loadSpeed() {
  try {
    const [settings, overview] = await Promise.all([
      api.get('/api/settings'), api.get('/api/speed/overview')
    ])
    speedForm.value = { ...settings.settings }
    speedSources.value = overview.items || []
  } catch (e) { ElMessage.error(e.message) }
}

async function saveSpeed() {
  speedSaving.value = true
  try {
    await api.patch('/api/settings', {
      speed_enabled: !!Number(speedForm.value.speed_enabled),
      speed_connections: Number(speedForm.value.speed_connections),
      speed_warmup_seconds: Number(speedForm.value.speed_warmup_seconds),
      speed_sample_seconds: Number(speedForm.value.speed_sample_seconds),
      speed_connect_timeout: Number(speedForm.value.speed_connect_timeout),
      speed_total_timeout: Number(speedForm.value.speed_total_timeout),
      speed_retry: Number(speedForm.value.speed_retry),
      speed_auto_enabled: !!Number(speedForm.value.speed_auto_enabled),
      speed_auto_interval_minutes: Number(speedForm.value.speed_auto_interval_minutes),
      speed_auto_nodes: Number(speedForm.value.speed_auto_nodes)
    })
    ElMessage.success('测速参数已保存')
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
  finally { speedSaving.value = false }
}

async function checkSpeedSources() {
  speedChecking.value = true
  try {
    const res = await api.post('/api/speed/sources/check', {})
    ElMessage.success(`健康检查完成：${res.ok}/${res.total} 个源正常`)
    speedSources.value = res.items || []
  } catch (e) { ElMessage.error(e.message) }
  finally { speedChecking.value = false }
}

async function toggleSpeedSource(row) {
  try {
    await api.patch(`/api/speed/sources/${row.id}`, { enabled: !!row.enabled })
    ElMessage.success(row.enabled ? '已启用，首次测速前会重新健康检查' : '已停用')
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
}

onMounted(() => {
  mq = window.matchMedia('(max-width: 768px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', syncMobile)
  load()
  loadSpeed()
})
onUnmounted(() => { if (mq) mq.removeEventListener('change', syncMobile) })
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">节点标签</h2>
    <div class="np-page-desc">
      每个标签对应一个检测目标与一个延迟上限（命中即打标）。超时统一走系统设置里的全局检测超时，标签本身不再单独配置超时和周期。「名称」只在本页面内部使用；真正进入节点名中间段的是「标识」（如「机场⁃GPTᐧTG⁃节点名」）。命中标签不影响是否下发，下发只看「系统设置 → 延迟检测」的延迟上限。
      <br />标签不提供单独运行，所有启用标签随全局检测一起执行。
    </div>

    <div class="np-toolbar">
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon>新增节点标签</el-button>
      <el-button @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
      <div class="np-flex" />
      <span class="np-muted">顺序决定节点名中间段的排列：{{ items.filter(i => i.enabled).map(i => i.ident).join('ᐧ') || '（无启用标签）' }}</span>
    </div>

    <div v-if="isMobile" class="np-mobile-list">
      <div v-for="row in items" :key="row.id" class="np-mobile-card">
        <div class="np-mobile-card-head">
          <div class="np-mobile-title">
            {{ row.name }}
            <div class="np-mobile-sub">
              <el-tag effect="plain" size="small">{{ row.ident }}</el-tag>
              <span style="margin-left:6px">{{ row.method }}</span>
            </div>
          </div>
          <el-switch :model-value="row.enabled" size="small" @change="(v) => toggle(row, v)" />
        </div>
        <div class="np-mobile-meta">
          <span class="np-mono" style="max-width:100%;overflow:hidden;text-overflow:ellipsis">{{ row.url }}</span>
          <span>状态 {{ row.expect_status }}</span>
          <span v-if="row.expect_body">包含 {{ row.expect_body }}</span>
          <span v-if="row.max_latency">≤ {{ row.max_latency }}ms</span>
        </div>
        <div class="np-mobile-actions">
          <span class="np-muted">通过 {{ row.passed }} · {{ formatTs(row.last_run_at) }}</span>
          <div class="np-flex" />
          <el-button link size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" size="small" :disabled="row.is_default" @click="remove(row)">删除</el-button>
        </div>
      </div>
      <div v-if="!items.length" class="np-empty">暂无节点标签</div>
    </div>

    <div v-if="!isMobile" class="np-card">
      <el-table :data="items">
        <el-table-column prop="name" label="名称（内部）" width="120" />
        <el-table-column label="标识" width="100">
          <template #default="{ row }"><el-tag effect="plain" size="small">{{ row.ident }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="url" label="检测 URL" min-width="200" show-overflow-tooltip />
        <el-table-column prop="method" label="方法" width="70" />
        <el-table-column label="成功条件" min-width="160">
          <template #default="{ row }">
            <div class="np-mono">状态: {{ row.expect_status }}</div>
            <div class="np-mono" v-if="row.expect_body">包含: {{ row.expect_body }}</div>
            <div class="np-mono" v-if="row.max_latency">延迟 ≤ {{ row.max_latency }}ms</div>
          </template>
        </el-table-column>
        <el-table-column prop="passed" label="通过" width="60" />
        <el-table-column label="最近运行" width="150">
          <template #default="{ row }">{{ formatTs(row.last_run_at) }}</template>
        </el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="(v) => toggle(row, v)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button link @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" :disabled="row.is_default" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="np-card" style="padding:20px;margin-top:16px">
      <div class="np-section-title">
        下载测速（独立模块）
        <div class="np-flex" />
        <el-button size="small" :loading="speedChecking" @click="checkSpeedSources">健康检查</el-button>
        <el-button size="small" type="primary" :loading="speedSaving" @click="saveSpeed">保存测速设置</el-button>
      </div>
      <div class="np-muted" style="margin-bottom:12px">
        走节点本地 SOCKS 端口直连公共 HTTPS 测速源，4 连接分段下载。测速结果不参与评分，测速源故障与节点故障分开记录。
      </div>
      <el-form label-width="150px" size="default">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="启用下载测速"><el-switch v-model="speedForm.speed_enabled" active-value="1" inactive-value="0" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="并发连接"><el-input-number v-model="speedForm.speed_connections" :min="1" :max="8" style="width:100%" /></el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="预热 (秒)"><el-input-number v-model="speedForm.speed_warmup_seconds" :min="0" :max="5" :step="0.5" style="width:100%" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="采样 (秒)"><el-input-number v-model="speedForm.speed_sample_seconds" :min="3" :max="30" style="width:100%" /></el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="单连接超时 (秒)"><el-input-number v-model="speedForm.speed_connect_timeout" :min="3" :max="30" style="width:100%" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="整轮超时 (秒)"><el-input-number v-model="speedForm.speed_total_timeout" :min="8" :max="60" style="width:100%" /></el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="失败重试"><el-input-number v-model="speedForm.speed_retry" :min="0" :max="2" style="width:100%" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="自动测速"><el-switch v-model="speedForm.speed_auto_enabled" active-value="1" inactive-value="0" /></el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="自动间隔 (分钟)"><el-input-number v-model="speedForm.speed_auto_interval_minutes" :min="15" :max="1440" :step="15" style="width:100%" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="每轮节点数"><el-input-number v-model="speedForm.speed_auto_nodes" :min="1" :max="20" style="width:100%" /></el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <el-table :data="speedSources" size="small" max-height="260">
        <el-table-column prop="name" label="测速源" min-width="150" />
        <el-table-column prop="region" label="地区" width="90" />
        <el-table-column label="健康" width="86">
          <template #default="{ row }">
            <el-tag size="small" effect="plain"
                    :type="row.health === 'ok' ? 'success' : (row.health === 'fail' ? 'danger' : 'info')">
              {{ row.health === 'ok' ? '正常' : (row.health === 'fail' ? '异常' : '未检查') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Range" width="76">
          <template #default="{ row }"><span class="np-mono">{{ row.range_ok ? '206' : '—' }}</span></template>
        </el-table-column>
        <el-table-column label="连接 ms" width="86">
          <template #default="{ row }">{{ row.latency_ms ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="启用" width="66">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" :active-value="1" :inactive-value="0" @change="toggleSpeedSource(row)" />
          </template>
        </el-table-column>
        <el-table-column label="地址" min-width="280">
          <template #default="{ row }"><span class="np-muted">{{ row.url }}</span></template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑节点标签' : '新增节点标签'" width="620px">
      <el-form label-width="96px">
        <el-row :gutter="12">
          <el-col :span="14">
            <el-form-item label="名称">
              <el-input v-model="form.name" placeholder="内部名称，例如 ChatGPT" />
            </el-form-item>
          </el-col>
          <el-col :span="10">
            <el-form-item label="标识">
              <el-input v-model="form.ident" placeholder="进入节点名，如 GPT" maxlength="12" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="检测 URL">
          <el-input v-model="form.url" placeholder="https://example.com/generate_204" />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="HTTP 方法">
              <el-select v-model="form.method" style="width:100%">
                <el-option label="GET" value="GET" />
                <el-option label="HEAD" value="HEAD" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">

          </el-col>
          <el-col :span="8">

          </el-col>
        </el-row>
        <el-form-item label="预期状态码">
          <el-input v-model="form.expect_status" placeholder="例如 200 / 200-399 / 204,302" />
        </el-form-item>
        <el-form-item label="响应包含">
          <el-input v-model="form.expect_body" placeholder="留空则不校验；例如某个页面标题片段" />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="最大延迟">
              <el-input-number v-model="form.max_latency" :min="0" :max="60000" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="排序">
              <el-input-number v-model="form.sort_order" :min="0" :max="999" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <div class="np-muted">延迟 0 表示不限制。节点标签只决定节点名里的代号，不决定是否下发；是否下发由「系统设置 → 延迟检测」的延迟上限决定。</div>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
