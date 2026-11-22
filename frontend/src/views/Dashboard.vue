<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs, logLevelLabel, logSourceLabel } from '../api'

const data = ref(null)
const AIRPORT_COLORS = ['#3f8cff', '#2fbf71', '#f5a524', '#9b6cf2', '#26b6c9', '#ef5b5b', '#71b33a', '#e07aa8']
const loading = ref(false)
const running = ref(false)
const logs = ref([])
const logLevel = ref('ALL')
const logsLoading = ref(false)
const timer = ref(null)
const liveTimer = ref(null)
let liveBusy = false

async function loadLogs() {
  logsLoading.value = true
  try {
    const query = logLevel.value !== 'ALL' ? `&level=${encodeURIComponent(logLevel.value)}` : ''
    logs.value = await api.get(`/api/logs?limit=300${query}`)
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    logsLoading.value = false
  }
}

async function refreshDashboard() {
  if (liveBusy) return
  liveBusy = true
  try {
    const next = await api.get('/api/dashboard')
    data.value = next
    running.value = next.runtime.running
    syncLiveTimer()
  } catch { /* 自动刷新失败时保留上一帧，不弹错打断阅读 */ }
  finally { liveBusy = false }
}

async function load() {
  loading.value = true
  try {
    data.value = await api.get('/api/dashboard')
    running.value = data.value.runtime.running
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    loading.value = false
  }
  syncLiveTimer()
  await loadLogs()
}

function syncLiveTimer() {
  clearInterval(liveTimer.value)
  liveTimer.value = null
  if (data.value?.batch?.status === 'running') {
    liveTimer.value = setInterval(() => { if (!document.hidden) refreshDashboard() }, 3000)
  }
}

async function pauseCycle() {
  try {
    await api.post('/api/run/pause')
    ElMessage.success('已暂停，当前请求结束后停止发起新请求')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function resumeCycle() {
  try {
    await api.post('/api/run/resume')
    ElMessage.success('已继续检测')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function stopCycle() {
  try {
    await ElMessageBox.confirm(
      '停止当前检测，并立即按已经完成的进度下发？未检测到的节点本次不参与。',
      '停止检测',
      { type: 'warning', confirmButtonText: '停止并下发', cancelButtonText: '取消' }
    )
    await api.post('/api/run/stop')
    ElMessage.success('正在停止，当前请求结束后按进度下发')
    await load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(err.message || String(err))
  }
}

async function runNow() {
  try {
    await api.post('/api/run', { force: true })
    ElMessage.success('已启动：延迟检测 + 全部标签检测，完成后自动替换订阅')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

const poolPct = computed(() => {
  const d = data.value
  if (!d || !d.nodes_total) return 0
  return Math.round(d.nodes_available / d.nodes_total * 100)
})
const qualifyPct = computed(() => {
  const d = data.value
  if (!d || !d.nodes_total) return 0
  return Math.round((d.nodes_qualified || 0) / d.nodes_total * 100)
})
const unavailablePct = computed(() => 100 - poolPct.value)
const publishTotal = computed(() => chartTotal(data.value?.airport_list || [], 'published_count'))
const nodeChartRows = computed(() => rankRows(data.value?.airport_list || [], 'pool_count'))
const publishChartRows = computed(() => rankRows(data.value?.airport_list || [], 'published_count'))
const rankedAirports = computed(() => rankRows(data.value?.airport_list || [], 'pool_count'))
const fetchState = computed(() => {
  const f = data.value?.fetch
  if (!f) return { text: '—', type: 'info' }
  if (f.fetching) return { text: `拉取中 ${f.fetching}`, type: 'warning' }
  if (f.error) return { text: `${f.error} 个失败`, type: 'danger' }
  if (!f.total) return { text: '未配置', type: 'info' }
  return { text: '正常', type: 'success' }
})
const runStateText = computed(() => {
  const r = data.value?.runtime
  if (!r?.running) return '空闲'
  if (r.stop_requested) return '正在停止'
  if (r.paused) return '已暂停'
  return '检测中'
})
const runTagType = computed(() => {
  const r = data.value?.runtime
  if (!r?.running) return 'success'
  return r.stop_requested ? 'danger' : r.paused ? 'info' : 'warning'
})
const batchStatusLabel = computed(() => {
  const map = { running: '检测中', complete: '已完成', stopped: '已停止', failed: '失败', aborted: '已中止' }
  return map[data.value?.batch?.status] || data.value?.batch?.status || '—'
})
const cyclePct = computed(() => {
  const r = data.value?.runtime
  if (!r || !r.total) return 0
  return Math.round((r.done || 0) / r.total * 100)
})
const batchPct = computed(() => {
  const b = data.value?.batch
  if (!b?.target_count) return 0
  return Math.max(0, Math.min(100, Math.round((b.processed_count || 0) / b.target_count * 100)))
})
const batchDuration = computed(() => {
  const b = data.value?.batch
  if (!b?.started_at) return '—'
  if (!b.finished_at) return b.status === 'running' ? '进行中' : '—'
  const seconds = Math.max(0, Math.round((new Date(b.finished_at) - new Date(b.started_at)) / 1000))
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
})
const batchRemaining = computed(() => {
  const b = data.value?.batch
  if (!b) return 0
  return Math.max(0, (b.target_count || 0) - (b.processed_count || 0))
})
const batchTone = computed(() => {
  const s = data.value?.batch?.status
  if (s === 'complete') return 'done'
  if (s === 'running') return 'running'
  return s ? 'failed' : 'idle'
})
function airportNodeShare(row) {
  const total = data.value?.nodes_total || 0
  if (!total) return 0
  return Math.min(100, Math.round((row.pool_count || 0) / total * 100))
}
function airportPublishRate(row) {
  const pool = row.pool_count || 0
  if (!pool) return 0
  return Math.min(100, Math.round((row.published_count || 0) / pool * 100))
}
function airportColor(row) {
  const rows = data.value?.airport_list || []
  const index = rows.findIndex((item) => item.id === row.id)
  if (!row.enabled) return '#c7c7cc'
  return AIRPORT_COLORS[Math.max(0, index) % AIRPORT_COLORS.length]
}
function chartAmount(row, key) {
  return row.enabled ? (row[key] || 0) : 0
}
function chartTotal(rows, key) {
  return (rows || []).reduce((sum, row) => sum + chartAmount(row, key), 0)
}
function chartShare(rows, row, key) {
  const total = chartTotal(rows, key)
  if (!total) return 0
  return Math.round(chartAmount(row, key) / total * 100)
}
function rankRows(rows, key) {
  return [...(rows || [])].sort((a, b) => {
    if (!!a.enabled !== !!b.enabled) return a.enabled ? -1 : 1
    const diff = chartAmount(b, key) - chartAmount(a, key)
    if (diff) return diff
    return (b.pool_count || 0) - (a.pool_count || 0)
  })
}
function donutStyle(rows, key) {
  const total = chartTotal(rows, key)
  if (!total) return { background: '#edf0f4' }
  let acc = 0
  const stops = []
  for (const row of rows || []) {
    const value = chartAmount(row, key)
    if (!value) continue
    const from = acc / total * 100
    acc += value
    const to = acc / total * 100
    stops.push(`${airportColor(row)} ${from.toFixed(2)}% ${to.toFixed(2)}%`)
  }
  return { background: `conic-gradient(${stops.join(', ')})` }
}
function airportClock(ts) {
  if (!ts) return '—'
  const d = new Date(String(ts).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return shortTs(ts)
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const day = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diff = Math.round((day - today) / 86400000)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (diff === 0) return hm
  if (diff === 1) return `明天 ${hm}`
  if (diff === -1) return `昨天 ${hm}`
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hm}`
}
function airportStateText(row) {
  if (!row.enabled) return '未启用'
  if (row.last_status === 'ok') return '正常'
  if (row.last_status === 'error') return '失败'
  return '未拉取'
}
const subText = computed(() => {
  const ps = data.value?.publish_schedule
  const p = data.value?.publish
  if (!ps) return '—'
  if (!ps.enabled) return '未启用'
  if (ps.mode === 'interval') return `每 ${ps.interval_minutes} 分钟`
  return `每天 ${ps.time}`
})
function shortTs(ts) {
  if (!ts) return '—'
  return String(ts).replace(/^\d{4}-/, '').replace('T', ' ').slice(0, 11)
}
const nextAutoText = computed(() => {
  const ps = data.value?.publish_schedule
  if (!ps || !ps.enabled) return '未启用'
  if (ps.next_at) return shortTs(ps.next_at)
  return ps.mode === 'interval' ? `每 ${ps.interval_minutes} 分钟（等待对齐）` : `每天 ${ps.time}（等待对齐）`
})

function roundDone(name) {
  const item = (data.value?.runtime?.group_progress || []).find((g) => g.name === name)
  return item ? `${item.done}/${item.total}` : '—'
}
function roundPercent(name) {
  const item = (data.value?.runtime?.group_progress || []).find((g) => g.name === name)
  if (!item || !item.total) return 0
  return Math.round(item.done / item.total * 100)
}

onMounted(() => {
  load()
  timer.value = setInterval(() => { if (!document.hidden) load() }, 15000)
})
onUnmounted(() => { clearInterval(timer.value); clearInterval(liveTimer.value) })
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">仪表盘</h2>
    <div class="np-page-desc">机场拉取、节点缓存与检测批次总览。检测只读取本地缓存，不会触发机场重新拉取。</div>

    <div class="np-dash-layout" v-if="data">
      <!-- 节点池 + 机场拉取：合并成主统计卡 -->
      <div class="np-card np-dash-pool">
        <div class="np-section-title">
          <span class="np-dot" :class="(data.fetch?.error || data.fetch?.fetching) ? 'is-warning' : 'is-success'" />
          节点缓存池
          <el-tag :type="fetchState.type" size="small" effect="plain">
            {{ data.fetch?.fetching ? fetchState.text : data.fetch?.error ? `${data.fetch.error} 个机场失败` : `${data.fetch?.ok || 0}/${data.fetch?.total || 0} 机场正常` }}
          </el-tag>
        </div>

        <div class="np-pool-overview">
          <div class="np-pool-status">
            <div class="np-pool-status-title">节点池状态</div>
            <div class="np-pool-status-total"><strong>{{ data.nodes_total }}</strong><span>节点</span></div>
            <div class="np-pool-health-bar">
              <i class="is-ok" :style="{ width: `${poolPct}%` }" />
              <i class="is-bad" :style="{ width: `${unavailablePct}%` }" />
            </div>
            <div class="np-pool-status-list">
              <div class="is-qualified"><i /><span>下发达标</span><b>{{ data.nodes_qualified }}</b><em>{{ qualifyPct }}%</em></div>
              <div class="is-ok"><i /><span>可用</span><b>{{ data.nodes_available }}</b><em>{{ poolPct }}%</em></div>
              <div class="is-bad"><i /><span>不可用</span><b>{{ data.nodes_unavailable }}</b><em>{{ unavailablePct }}%</em></div>
            </div>
          </div>

          <div v-if="data.airport_list?.length" class="np-pool-charts">
            <div class="np-pool-chart">
              <div class="np-pool-chart-head">
                <b>节点占比</b>
                <span>{{ data.nodes_total }} 节点 · 高 → 低</span>
              </div>
              <div class="np-pool-chart-body">
                <div class="np-pie" :style="donutStyle(nodeChartRows, 'pool_count')"
                     :title="`节点池 ${data.nodes_total} 个节点`" />
                <div class="np-donut-legend is-two-col">
                  <div v-for="row in nodeChartRows" :key="`node-${row.id}`"
                       class="np-donut-legend-row" :class="{ 'is-off': !row.enabled }">
                    <i :style="{ background: airportColor(row) }" />
                    <span>{{ row.name }}</span>
                    <b>{{ chartShare(nodeChartRows, row, 'pool_count') }}%</b>
                  </div>
                </div>
              </div>
            </div>
            <div class="np-pool-chart">
              <div class="np-pool-chart-head">
                <b>下发占比</b>
                <span>{{ publishTotal }} 已下发 · 高 → 低</span>
              </div>
              <div class="np-pool-chart-body">
                <div class="np-pie" :style="donutStyle(publishChartRows, 'published_count')"
                     :title="`已下发 ${publishTotal} 个节点`" />
                <div class="np-donut-legend is-two-col">
                  <div v-for="row in publishChartRows" :key="`pub-${row.id}`"
                       class="np-donut-legend-row" :class="{ 'is-off': !row.enabled }">
                    <i :style="{ background: airportColor(row) }" />
                    <span>{{ row.name }}</span>
                    <b>{{ row.enabled && row.published_count ? `${chartShare(publishChartRows, row, 'published_count')}%` : '—' }}</b>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="np-pool-subhead">
          <span>机场明细</span>
          <span>按节点占比降序 · 上次 / 下次拉取</span>
        </div>

        <div v-if="data.airport_list?.length" class="np-pool-airports">
          <div v-for="(row, idx) in rankedAirports" :key="row.id" class="np-pool-airport-row">
            <div class="np-pool-airport-name">
              <div class="np-pool-airport-title">
                <span class="np-pool-rank" :class="{ 'is-off': !row.enabled }">{{ idx + 1 }}</span>
                <span class="np-airport-dot" :class="!row.enabled ? 'is-off' : `is-${row.last_status || 'none'}`" :title="airportStateText(row)" />
                <b>{{ row.name }}</b>
                <span v-if="row.enabled">{{ row.pool_count }} 节点</span>
                <span v-else>{{ row.node_count }} 缓存</span>
                <span v-if="!row.enabled" class="np-pool-off">未启用</span>
              </div>
              <div class="np-pool-airport-meta"
                   :title="`上次拉取 ${formatTs(row.last_success_at)} · 下次拉取 ${!row.enabled ? '未启用' : row.next_run_at ? formatTs(row.next_run_at) : '仅手动'}`">
                <span>上次 {{ airportClock(row.last_success_at) }}</span>
                <span>下次 {{ !row.enabled ? '未启用' : row.next_run_at ? airportClock(row.next_run_at) : '仅手动' }}</span>
              </div>
            </div>
            <div class="np-pool-metric">
              <span class="np-pool-metric-label">节点占比</span>
              <div class="np-pool-metric-value"><b>{{ airportNodeShare(row) }}%</b><em>{{ row.pool_count }} / {{ data.nodes_total }}</em></div>
            </div>
            <div class="np-pool-metric">
              <span class="np-pool-metric-label">下发率</span>
              <div class="np-pool-metric-value"><b>{{ airportPublishRate(row) }}%</b><em>{{ row.enabled ? `${row.published_count} / ${row.pool_count}` : '未参与' }}</em></div>
            </div>
          </div>
        </div>
        <div v-else class="np-empty">节点池为空，先到「机场管理」添加并拉取订阅。</div>

      </div>

      <!-- 检测任务 + 最近批次：合并成一块实时状态 -->
      <div class="np-card np-dash-detect">
        <div class="np-detect-head">
          <div class="np-detect-title">
            检测任务
            <el-tag v-if="data?.runtime.running" :type="runTagType" size="small">{{ runStateText }}</el-tag>
            <el-tag v-else type="success" size="small">空闲</el-tag>
          </div>
          <div class="np-detect-actions">
            <el-button v-if="!data?.runtime.running" size="small" type="primary" @click="runNow">立即检测并下发</el-button>
            <el-button v-if="data?.runtime.running && !data.runtime.paused"
                       size="small" :disabled="data.runtime.stop_requested" @click="pauseCycle">暂停</el-button>
            <el-button v-if="data?.runtime.running && data.runtime.paused"
                       size="small" type="success" :disabled="data.runtime.stop_requested" @click="resumeCycle">继续</el-button>
            <el-button v-if="data?.runtime.running" size="small" type="danger" plain
                       :disabled="data.runtime.stop_requested" @click="stopCycle">
              {{ data.runtime.stop_requested ? '正在停止' : '停止并下发' }}
            </el-button>
          </div>
        </div>

        <div class="np-detect-meta">
          <span class="np-detect-engine">引擎 <b>{{ data.runtime.engine }}</b></span>
          <span v-if="data.runtime.running" class="np-detect-phase">
            块 {{ data.runtime.block_index || 0 }}/{{ data.runtime.blocks || 0 }}
            · {{ data.runtime.block_size || 0 }} 节点 · {{ data.runtime.phase }}
          </span>
          <span v-else class="np-detect-phase">所有标签随全局检测一起执行</span>
          <div class="np-flex" />
          <span class="np-detect-time">上次启动 {{ shortTs(data.runtime.last_cycle_at || data.batch?.started_at) }}</span>
        </div>

        <div v-if="data.batch" class="np-detect-batch" :class="`is-${batchTone}`">
          <div class="np-detect-batch-main">
            <div class="np-batch-gauge is-compact" :class="`is-${batchTone}`">
              <svg viewBox="0 0 160 160" aria-hidden="true">
                <circle class="np-batch-gauge-track" cx="80" cy="80" r="65" pathLength="100" />
                <circle class="np-batch-gauge-value" cx="80" cy="80" r="65" pathLength="100"
                        :style="{ strokeDasharray: `${batchPct} 100` }" />
              </svg>
              <div class="np-batch-gauge-label">
                <strong>{{ batchPct }}</strong><span>%</span>
              </div>
            </div>
            <div class="np-detect-batch-copy">
              <div class="np-batch-state">
                <i class="np-batch-state-dot" />
                <b>{{ batchStatusLabel }}</b>
                <span>批次 #{{ data.batch.id }}</span>
              </div>
              <div class="np-batch-count">
                <strong>{{ data.batch.processed_count }}</strong>
                <span>/ {{ data.batch.target_count }}</span>
              </div>
              <div class="np-batch-count-label">已处理检测项 · {{ data.batch.published ? '已发布' : '未发布' }}</div>
              <div class="np-detect-batch-kpis">
                <div><span>剩余</span><b>{{ batchRemaining }}</b></div>
                <div><span>耗时</span><b>{{ batchDuration }}</b></div>
              </div>
            </div>
          </div>
          <div class="np-detect-batch-steps">
            <div class="is-done"><i>1</i><span>开始</span><b>{{ shortTs(data.batch.started_at) }}</b></div>
            <div :class="{ 'is-active': data.batch.status === 'running', 'is-done': batchTone === 'done', 'is-failed': batchTone === 'failed' }">
              <i>2</i><span>检测</span><b>{{ batchPct }}%</b>
            </div>
            <div :class="{ 'is-done': data.batch.published, 'is-failed': batchTone === 'failed' && !data.batch.published }">
              <i>3</i><span>发布</span><b>{{ data.batch.published ? '已发布' : '未发布' }}</b>
            </div>
          </div>
        </div>
        <div v-else class="np-empty">尚未运行过检测批次</div>

        <div class="np-detect-sumstats">
          <div><span>启用标签</span><b>{{ data.group_summary?.count || 0 }}</b></div>
          <div><span>命中节点</span><b>{{ data.group_summary?.hits || 0 }}</b></div>
          <div><span>累计通过</span><b>{{ data.group_summary?.passed || 0 }}</b></div>
        </div>

        <div class="np-detect-list">
          <div v-for="row in data.groups" :key="row.id" class="np-detect-label-row" :class="{ 'is-off': !row.enabled }">
            <span class="np-detect-ident">{{ row.ident }}</span>
            <div class="np-detect-label-main">
              <div class="np-detect-label-title">
                <span class="np-detect-label-name">{{ row.name }}</span>
                <el-tag :type="row.enabled ? 'success' : 'info'" size="small" effect="plain">
                  {{ row.enabled ? '启用' : '停用' }}
                </el-tag>
              </div>
              <div class="np-detect-label-url" :title="row.url">{{ row.url }}</div>
              <div v-if="data.runtime.running" class="np-detect-progress">
                <div class="np-detect-track"><i :style="{ width: `${roundPercent(row.name)}%` }" /></div>
                <div class="np-detect-progress-meta"><span>{{ roundDone(row.name) }}</span><b>{{ roundPercent(row.name) }}%</b></div>
              </div>
            </div>
            <div class="np-detect-passed">
              <b>{{ row.passed }}</b><span>累计通过</span>
            </div>
          </div>
        </div>

        <div class="np-detect-footer">
          <span>延迟门槛</span>
          <el-tooltip :content="`${data.latency_probe?.url || '—'} / ${data.latency_probe?.url2 || '—'}`" placement="top">
            <b>{{ data.latency_probe?.names?.G?.short || 'G' }} ≤ {{ data.latency_probe?.ceiling || '不限' }}ms · {{ data.latency_probe?.names?.W?.short || 'W' }} ≤ {{ data.latency_probe?.ceiling2 || '不限' }}ms</b>
          </el-tooltip>
          <span class="np-flex" />
          <span>每批重测 G / W</span>
        </div>

        <div class="np-detect-sub">
          <div class="np-detect-sub-head">
            <span class="np-dot" :class="data.publish_schedule?.enabled ? 'is-success' : 'is-info'" />
            <b>订阅下发</b>
            <el-tag :type="data.publish_schedule?.enabled ? 'success' : 'info'" size="small" effect="plain">
              {{ data.publish_schedule?.enabled ? '已启用' : '未启用' }}
            </el-tag>
            <span class="np-flex" />
            <span class="np-detect-sub-note">{{ subText }}自动下发</span>
          </div>
          <div class="np-detect-sub-grid">
            <div><span>下次执行</span><b>{{ nextAutoText }}</b></div>
            <div><span>上次下发</span><b>{{ shortTs(data.publish?.last_auto_at || data.publish?.last_batch_at) }}</b></div>
            <div><span>最近生成</span><b>{{ shortTs(data.subscription?.updated_at) }}</b></div>
          </div>
        </div>
      </div>
    </div>

    <div class="np-card np-log-card">
      <div class="np-section-title">
        系统日志
        <el-tag type="info" size="small" effect="plain">唯一入口</el-tag>
        <div class="np-flex" />
        <div class="np-log-tools">
          <el-select v-model="logLevel" size="small" style="width:118px" @change="loadLogs">
            <el-option label="全部级别" value="ALL" />
            <el-option label="信息" value="INFO" />
            <el-option label="警告" value="WARN" />
            <el-option label="错误" value="ERROR" />
            <el-option label="调试" value="DEBUG" />
          </el-select>
          <span class="np-muted">已载入 {{ logs.length }} 条（最多 300）· 自动刷新</span>
          <el-button size="small" :loading="logsLoading" @click="loadLogs">刷新</el-button>
        </div>
      </div>
      <div class="np-log np-log-full" v-loading="logsLoading">
        <div v-for="log in logs" :key="log.id">
          <span class="np-muted">{{ formatTs(log.ts) }}</span>
          <span :class="`lv-${log.level}`"> [{{ logLevelLabel(log.level) }}]</span>
          <span class="np-muted">{{ logSourceLabel(log.source) }}</span>
          {{ log.message }}
        </div>
        <div v-if="!logs.length" class="np-empty">暂无日志</div>
      </div>
    </div>
  </div>
</template>
