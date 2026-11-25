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
const speedOverview = ref({})
const speedAirports = ref([])
const speedNodes = ref([])
const speedNodeId = ref(null)
const speedBusy = ref('')
let speedTimer = null
const GRADE_ROWS = [
  { key: 'excellent', label: '优', emoji: '🟢', color: '#16A34A' },
  { key: 'good', label: '良', emoji: '🔵', color: '#2563EB' },
  { key: 'poor', label: '差', emoji: '🟠', color: '#EA580C' },
  { key: 'danger', label: '危', emoji: '🔴', color: '#DC2626' }
]
const THRESHOLD_ROWS = GRADE_ROWS.slice(0, 3)
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
    const [settings, overview, nodes] = await Promise.all([
      api.get('/api/settings'), api.get('/api/speed/overview'), api.get('/api/speed/nodes')
    ])
    speedForm.value = { ...settings.settings,
                        speed_parallel_nodes: settings.settings.speed_parallel_nodes ?? 4 }
    speedOverview.value = overview
    speedSources.value = overview.items || []
    speedAirports.value = overview.airports || []
    speedNodes.value = nodes.items || []
  } catch (e) { ElMessage.error(e.message) }
}

function speedNumber(key) { return Number(speedForm.value[key]) }

async function saveSpeed() {
  speedSaving.value = true
  try {
    await api.patch('/api/settings', {
      speed_enabled: !!Number(speedForm.value.speed_enabled),
      speed_schedule_enabled: !!Number(speedForm.value.speed_schedule_enabled),
      speed_window_start: speedForm.value.speed_window_start || '18:00',
      speed_window_end: speedForm.value.speed_window_end || '23:30',
      speed_schedule_every_days: speedNumber('speed_schedule_every_days'),
      speed_traffic_limit_mb: speedNumber('speed_traffic_limit_mb'),
      speed_result_ttl_days: speedNumber('speed_result_ttl_days'),
      speed_connections: speedNumber('speed_connections'),
      speed_parallel_nodes: speedNumber('speed_parallel_nodes'),
      speed_warmup_seconds: speedNumber('speed_warmup_seconds'),
      speed_sample_seconds: speedNumber('speed_sample_seconds'),
      speed_connect_timeout: speedNumber('speed_connect_timeout'),
      speed_total_timeout: speedNumber('speed_total_timeout'),
      speed_retry: speedNumber('speed_retry'),
      speed_grade_excellent: speedNumber('speed_grade_excellent'),
      speed_grade_good: speedNumber('speed_grade_good'),
      speed_grade_poor: speedNumber('speed_grade_poor')
    })
    ElMessage.success('测速参数已保存')
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
  finally { speedSaving.value = false }
}

async function runAllSpeed() {
  await ElMessageBox.confirm('将对所有未被排除测速的节点重新测速，单节点最多下载 50 MB。已有有效结果也会被新结果替换。',
                             '全部重新测速', { type: 'warning', confirmButtonText: '开始测速' })
  speedBusy.value = 'all'
  try {
    const res = await api.post('/api/speed/run/all', {})
    ElMessage.success(`已入队 ${res.created} 个节点${res.duplicate ? `，跳过重复 ${res.duplicate}` : ''}`)
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
  finally { speedBusy.value = '' }
}

async function runNewSpeed() {
  speedBusy.value = 'new'
  try {
    const res = await api.post('/api/speed/run/new', {})
    ElMessage.success(res.created ? `已入队 ${res.created} 个待测节点` : '没有尚无有效测速结果的节点')
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
  finally { speedBusy.value = '' }
}

async function runSelectedSpeed() {
  if (!speedNodeId.value) return ElMessage.warning('请先选择节点')
  speedBusy.value = 'manual'
  try {
    const res = await api.post('/api/speed/test', { node_id: speedNodeId.value })
    if (res.status === 'queued') ElMessage.info(res.message)
    else if (res.mb_per_s != null) ElMessage.success(`节点测速完成：${res.mb_per_s.toFixed(2)} MB/s`)
    else ElMessage.warning(`测速未取得有效结果：${res.message || res.status}`)
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
  finally { speedBusy.value = '' }
}

async function toggleAirportSpeed(row, participate) {
  try {
    await api.patch(`/api/speed/airports/${row.id}`, { speed_excluded: !participate })
    ElMessage.success(participate ? '该机场已参与测速' : '该机场已排除测速')
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
}

async function checkSpeedSources() {
  speedChecking.value = true
  try {
    const res = await api.post('/api/speed/sources/check', {})
    ElMessage.success(`测试源健康检测完成：${res.ok}/${res.total} 个源正常`)
    speedSources.value = res.items || []
  } catch (e) { ElMessage.error(e.message) }
  finally { speedChecking.value = false }
}

async function toggleSpeedSource(row) {
  try {
    await api.patch(`/api/speed/sources/${row.id}`, { enabled: !!row.enabled })
    ElMessage.success(row.enabled ? '已启用，首次测速前会重新检测测速源' : '已停用')
    await loadSpeed()
  } catch (e) { ElMessage.error(e.message) }
}

onMounted(() => {
  mq = window.matchMedia('(max-width: 768px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', syncMobile)
  load()
  loadSpeed()
  speedTimer = setInterval(loadSpeed, 15000)
})
onUnmounted(() => {
  if (mq) mq.removeEventListener('change', syncMobile)
  if (speedTimer) clearInterval(speedTimer)
})
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">节点标签</h2>
    <div class="np-page-desc">
      每个标签对应一个检测目标与一个延迟上限（命中即打标）。超时统一走系统设置里的全局检测超时，标签本身不再单独配置超时和周期。「名称」只在本页面内部使用；真正进入节点名中间段的是「标识」（如「机场·GPTᐧTG·节点名」）。命中标签不影响是否下发，下发只看「系统设置 → 延迟检测」的延迟上限。
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

    <div class="np-card speed-panel" style="padding:20px;margin-top:16px">
      <div class="np-section-title">
        下载测速
        <el-tag size="small" effect="plain" type="info" style="margin-left:10px">独立模块 · 不参与评分</el-tag>
        <div class="np-flex" />
        <span class="speed-queue-strip">
          运行 {{ speedOverview.queue?.counts?.running || 0 }} ·
          排队 {{ (speedOverview.queue?.counts?.queued || 0) + (speedOverview.queue?.counts?.waiting || 0) }} ·
          近 24 小时有效 {{ speedOverview.runs_ok_24h || 0 }}
        </span>
        <el-button size="small" type="primary" :loading="speedSaving" @click="saveSpeed">保存测速设置</el-button>
      </div>
      <div class="np-muted speed-desc">
        通过节点本地 SOCKS 端口分段下载公共 HTTPS 测速源。定时任务只在晚高峰窗口内随机分散启动，手动任务立即入队；全部任务共用独立测速并发与防重复队列；与节点检测评分互斥，任一方进行中时另一方自动延迟。
      </div>

      <div class="speed-actions">
        <div class="speed-action-main">
          <el-button type="primary" plain :loading="speedBusy === 'all'"
                     :disabled="!Number(speedForm.speed_enabled)" @click="runAllSpeed">全部重新测速</el-button>
          <el-button plain :loading="speedBusy === 'new'"
                     :disabled="!Number(speedForm.speed_enabled)" @click="runNewSpeed">新节点测速</el-button>
        </div>
        <el-select v-model="speedNodeId" filterable clearable placeholder="选择单个节点"
                   style="width:min(420px, 100%)">
          <el-option v-for="n in speedNodes" :key="n.id" :label="n.name" :value="n.id"
                     :disabled="n.excluded" />
        </el-select>
        <el-button :loading="speedBusy === 'manual'" :disabled="!speedNodeId"
                   @click="runSelectedSpeed">立即测速</el-button>
      </div>
      <div class="speed-note">
        <span v-if="speedOverview.schedule?.in_window">当前处于测速窗口</span>
        <span v-else>当前不在测速窗口，手动任务仍可执行</span>
        <span v-if="speedOverview.schedule?.next_due_date"> · 下次定时日期 {{ speedOverview.schedule.next_due_date }}</span>
        <span v-if="speedOverview.work?.busy">
          · 当前引擎由{{ speedOverview.work.label }}占用，另一项任务自动延迟
        </span>
        <span v-if="speedOverview.queue?.active?.length">
          · 进行中：{{ speedOverview.queue.active.slice(0, 3).map(j => j.trigger_label + ' #' + j.node_id).join('、') }}
        </span>
      </div>

      <el-form label-width="102px" size="small" class="speed-form">
        <div class="speed-config-grid">
          <section class="speed-config-panel">
            <div class="speed-subhead">
              计划与流量
              <span class="speed-subhead-note">北京时间 · 频率 1 = 每天，2 = 隔 2 天</span>
            </div>
            <el-form-item label="启用下载测速">
              <el-switch v-model="speedForm.speed_enabled" active-value="1" inactive-value="0" />
            </el-form-item>
            <el-form-item label="定时测速">
              <el-switch v-model="speedForm.speed_schedule_enabled" active-value="1" inactive-value="0" />
            </el-form-item>
            <el-form-item label="执行频率">
              <el-input-number v-model="speedForm.speed_schedule_every_days" :min="1" :max="30"
                               class="speed-num" />
            </el-form-item>
            <el-form-item label="流量上限 MB">
              <el-input-number v-model="speedForm.speed_traffic_limit_mb" :min="1" :max="1024"
                               :step="10" class="speed-num" />
            </el-form-item>
            <div class="speed-cap-note">
              仅完整测完的测速地址计入上限；失败或未跑完的地址不占额度，重试和下一个地址仍拿完整上限。
            </div>
            <el-form-item label="开始时间">
              <el-time-picker v-model="speedForm.speed_window_start" format="HH:mm"
                              value-format="HH:mm" placeholder="18:00" style="width:100%" />
            </el-form-item>
            <el-form-item label="结束时间">
              <el-time-picker v-model="speedForm.speed_window_end" format="HH:mm"
                              value-format="HH:mm" placeholder="23:30" style="width:100%" />
            </el-form-item>
            <el-form-item label="有效期 天">
              <el-input-number v-model="speedForm.speed_result_ttl_days" :min="1" :max="30"
                               class="speed-num" />
            </el-form-item>
          </section>

          <section class="speed-config-panel">
            <div class="speed-subhead">
              测速参数
              <span class="speed-subhead-note">独立于节点检测 · 并行节点 / 每源连接数</span>
            </div>
            <el-form-item label="并行节点">
              <el-input-number v-model="speedForm.speed_parallel_nodes" :min="1" :max="8" class="speed-num" />
            </el-form-item>
            <el-form-item label="并发连接">
              <el-input-number v-model="speedForm.speed_connections" :min="1" :max="8" class="speed-num" />
            </el-form-item>
            <el-form-item label="预热（秒）">
              <el-input-number v-model="speedForm.speed_warmup_seconds" :min="0" :max="5" :step="0.5" class="speed-num" />
            </el-form-item>
            <el-form-item label="采样（秒）">
              <el-input-number v-model="speedForm.speed_sample_seconds" :min="3" :max="30" class="speed-num" />
            </el-form-item>
            <el-form-item label="连接超时">
              <el-input-number v-model="speedForm.speed_connect_timeout" :min="3" :max="30" class="speed-num" />
            </el-form-item>
            <el-form-item label="单源超时">
              <el-input-number v-model="speedForm.speed_total_timeout" :min="8" :max="60" class="speed-num" />
            </el-form-item>
            <el-form-item label="失败重试">
              <el-input-number v-model="speedForm.speed_retry" :min="0" :max="2" class="speed-num" />
            </el-form-item>
          </section>
        </div>

        <div class="speed-subhead">
          速度等级阈值（MB/s）
          <span class="speed-subhead-note">数值为各档下限</span>
        </div>
        <div class="speed-threshold-grid">
          <div v-for="g in THRESHOLD_ROWS" :key="g.key" class="speed-threshold">
            <span class="speed-threshold-label">
              <span class="np-speed-dot is-tag" :style="{ background: g.color }"></span>{{ g.label }}
            </span>
            <el-input-number v-model="speedForm['speed_grade_' + g.key]" :min="0.01" :max="1024"
                             :step="g.key === 'poor' ? 0.05 : 1" :precision="2" class="speed-num-wide" />
          </div>
        </div>
        <div class="speed-status-queue">
          <span class="speed-status">
            <span class="np-speed-dot is-tag" style="background:#DC2626"></span>
            <b style="color:#DC2626">危</b><i>低于「差」阈值</i>
          </span>
          <span class="speed-status">
            <span class="np-speed-dot is-tag" style="background:#9CA3AF"></span>
            <b>已排除</b><i>机场不参与测速</i>
          </span>
          <span class="speed-status">
            <span class="np-speed-dot is-tag is-warn" style="background:#F59E0B">!</span>
            <b style="color:#F59E0B">异常</b><i>最近一次测速失败</i>
          </span>
          <span class="speed-status">
            <span class="np-noentry"></span>
            <b style="color:#DC2626">全部源失败</b><i>所有测速源不可用</i>
          </span>
          <span class="speed-status">
            <span class="np-speed-dot is-tag" style="background:#D1D5DB"></span>
            <b>待测速</b><i>尚无有效结果</i>
          </span>
        </div>
      </el-form>

      <div class="speed-tables">
        <div class="speed-table-block">
          <div class="speed-table-head">
            机场测速范围
            <span class="np-muted">排除后不创建任务、不消耗流量，显示中灰点</span>
          </div>
          <el-table :data="speedAirports" size="small">
            <el-table-column label="参与测速" width="110">
              <template #default="{ row }">
                <el-switch :model-value="!row.speed_excluded"
                           style="margin-right:6px" @change="v => toggleAirportSpeed(row, v)" />
                <span :style="{ color: row.speed_excluded ? '#EA580C' : '#64748b', fontSize: '12px' }">
                  {{ row.speed_excluded ? '排除' : '参与' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="name" label="机场" min-width="180" />
            <el-table-column label="节点" width="90">
              <template #default="{ row }">
                <span class="np-muted">{{ speedNodes.filter(n => n.airport === row.name).length }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <div class="speed-table-block">
          <div class="speed-table-head">
            <div class="speed-table-title">
              测速源
              <span class="np-muted">检测直连源站，不经过节点</span>
            </div>
            <el-button size="small" :loading="speedChecking" @click="checkSpeedSources">测试源健康检测</el-button>
          </div>
          <el-table :data="speedSources" size="small">
            <el-table-column label="启用" width="64" align="center">
              <template #default="{ row }">
                <el-switch v-model="row.enabled" :active-value="1" :inactive-value="0" @change="toggleSpeedSource(row)" />
              </template>
            </el-table-column>
            <el-table-column prop="name" label="测速源" min-width="150" />
            <el-table-column prop="region" label="地区" width="86" />
            <el-table-column label="健康" width="80">
              <template #default="{ row }">
                <el-tag size="small" effect="plain"
                        :type="row.health === 'ok' ? 'success' : (row.health === 'fail' ? 'danger' : 'info')">
                  {{ row.health === 'ok' ? '正常' : (row.health === 'fail' ? '异常' : '未检查') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Range" width="70">
              <template #default="{ row }"><span class="np-mono">{{ row.range_ok ? '206' : '—' }}</span></template>
            </el-table-column>
            <el-table-column label="连接 ms" width="82">
              <template #default="{ row }">{{ row.latency_ms ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="地址" min-width="260">
              <template #default="{ row }"><span class="np-muted">{{ row.url }}</span></template>
            </el-table-column>
          </el-table>
        </div>
      </div>
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

<style scoped>
.speed-panel {
  border-top: 3px solid var(--np-accent, #2563eb);
}
.speed-queue-strip {
  font-size: 12px;
  color: var(--np-muted, #64748b);
  margin-right: 10px;
  white-space: nowrap;
}
.speed-desc {
  margin: 2px 0 10px;
  line-height: 1.55;
}
.speed-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 8px 12px;
  margin-bottom: 6px;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  background: #f8fafc;
}
.speed-action-main {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.speed-note {
  font-size: 12px;
  color: #64748b;
  margin: 0 2px 8px;
}
.speed-num {
  width: 100% !important;
  max-width: 112px;
}
.speed-num-wide {
  width: 100% !important;
  max-width: 132px;
}
.speed-form :deep(.el-form-item) {
  margin-bottom: 4px;
}
.speed-form :deep(.el-form-item__label) {
  font-size: 12px;
  line-height: 30px;
  padding-right: 8px;
}
.speed-subhead {
  font-size: 13px;
  font-weight: 600;
  color: #334155;
  padding: 8px 0 4px;
  border-top: 1px dashed #e2e8f0;
  margin-top: 2px;
}
.speed-form > .speed-subhead:first-child {
  border-top: 0;
  padding-top: 0;
}

.speed-config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  align-items: start;
  margin-top: 6px;
}
.speed-config-panel {
  min-width: 0;
  padding: 0 14px 6px;
  border: 1px solid #eef2f7;
  border-radius: 10px;
  background: #fbfcfe;
}
.speed-config-panel .speed-subhead {
  border-top: 0;
  margin-top: 0;
  padding: 10px 0 6px;
}
.speed-config-panel :deep(.el-form-item) {
  margin-bottom: 6px;
}
.speed-subhead-note {
  font-weight: 400;
  font-size: 11px;
  color: #94a3b8;
  margin-left: 8px;
}
.speed-field-hint {
  width: 100%;
  font-size: 11px;
  line-height: 1.3;
  color: #94a3b8;
  margin-top: 2px;
}
.speed-cap-note {
  margin: -6px 0 6px 102px;
  font-size: 11px;
  line-height: 1.5;
  color: #94a3b8;
}
.speed-threshold-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 4px 12px;
  padding-bottom: 2px;
}
.speed-threshold {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.speed-threshold-label {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  font-size: 12px;
  color: #334155;
}
.speed-threshold .speed-num-wide {
  flex: 1 1 auto;
  width: auto !important;
  min-width: 0;
  max-width: 132px;
}
.speed-status-queue {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin-top: 4px;
  padding-top: 6px;
  border-top: 1px dashed #e2e8f0;
  font-size: 12px;
  line-height: 1.6;
  color: #475569;
  margin-bottom: 6px;
}
.speed-status {
  display: inline-flex;
  align-items: center;
  white-space: nowrap;
}
.speed-status b {
  font-weight: 600;
}
.speed-status i {
  font-style: normal;
  margin-left: 4px;
  font-size: 11px;
  color: #94a3b8;
}
.speed-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 12px;
  color: #475569;
  padding: 6px 10px;
  background: #f8fafc;
  border-radius: 8px;
  margin-bottom: 8px;
}
.speed-tables {
  display: grid;
  grid-template-columns: minmax(320px, .85fr) minmax(620px, 2fr);
  gap: 16px;
  align-items: start;
}
.speed-table-block {
  min-width: 0;
}
.speed-table-head {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 8px 12px;
  font-weight: 600;
  font-size: 13px;
  color: #334155;
  margin-bottom: 8px;
}
.speed-table-title {
  display: flex;
  align-items: baseline;
  gap: 12px;
  min-width: 0;
}
.speed-table-head .np-muted {
  font-weight: 400;
  font-size: 11px;
}
.speed-table-head > .el-button {
  margin-left: auto;
}
@media (max-width: 1100px) {
  .speed-tables { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .speed-queue-strip { width: 100%; margin: 4px 0; }
  .speed-actions { align-items: stretch; }
  .speed-action-main .el-button { flex: 1; }
  .speed-actions .el-select { width: 100% !important; }
}
@media (max-width: 768px) {
  .speed-config-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }
  .speed-config-panel {
    padding: 0 12px 4px;
  }
  .speed-num,
  .speed-num-wide {
    max-width: none;
  }
  .speed-cap-note {
    margin-left: 0;
  }
  .speed-threshold-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px 10px;
  }
  .speed-threshold-label {
    flex-basis: 40px;
  }
  .speed-status-queue {
    gap: 6px 12px;
  }
}
</style>
