<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs, logLevelLabel, logSourceLabel, state } from '../api'
import TrendChart from '../components/TrendChart.vue'
import OperationsMap from '../components/OperationsMap.vue'

const data = ref(null)
// 趋势图配色：前 5 个与评分等级同色（紫/绿/蓝/橙/红），其后按色相差距大者补充
const AIRPORT_COLORS = ['#8b5cf6', '#16a34a', '#2563eb', '#ea580c', '#dc2626',
  '#0e9596', '#ca8a04', '#db2777', '#475569', '#65a30d', '#0891b2', '#b45309']
const loading = ref(false)
const running = ref(false)
const logs = ref([])
const logLevel = ref('ALL')
const logsLoading = ref(false)
const timer = ref(null)
const rt = ref(null)
const runtime = computed(() => rt.value || data.value?.runtime || {})
let rtBusy = false
const liveTimer = ref(null)

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

const trendFetchDays = computed(() => trendRange.value === '30d' ? 30 : 7)

async function load() {
  loading.value = true
  try {
    data.value = await api.get(`/api/dashboard?days=${trendFetchDays.value}`)
    running.value = data.value.runtime.running
    rt.value = null
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    loading.value = false
  }
  syncLiveTimer()
  await loadLogs()
}

/** 只拉检测状态：仅在检测进行中轮询，且只更新检测卡片，不重绘曲线。 */
async function pollRuntime() {
  if (rtBusy) return
  rtBusy = true
  try {
    const next = await api.get('/api/runtime')
    rt.value = next
    if (!next.running) {
      rt.value = null
      syncLiveTimer()
      await load()
    }
  } catch { /* 轮询失败保留上一帧 */ }
  finally { rtBusy = false }
}

function syncLiveTimer() {
  clearInterval(liveTimer.value)
  liveTimer.value = null
  if (runtime.value?.running) {
    liveTimer.value = setInterval(() => { if (!document.hidden) pollRuntime() }, 2000)
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

const nodeChartRows = computed(() => rankRows(data.value?.airport_list || [], 'available_count'))
const publishChartRows = computed(() => rankRows(data.value?.airport_list || [], 'published_count'))
const statusSegments = computed(() => {
  const d = data.value
  if (!d?.nodes_total) return []
  const total = d.nodes_total
  const available = Math.min(d.nodes_available || 0, total)
  const qualified = Math.min(d.nodes_qualified || 0, available)
  const unavailable = Math.min(d.nodes_unavailable || 0, total)
  const unknown = Math.max(0, total - available - unavailable)
  const availableOnly = Math.max(0, available - qualified)
  const raw = [
    { key: 'qualified', label: '下发', short: '下发', count: qualified, color: '#34c759', side: '#1f8f45', light: '#7bdf95', ink: '#ffffff' },
    { key: 'available', label: '未达标', short: '未达标', count: availableOnly, color: '#3f8cff', side: '#2563c9', light: '#7db4ff', ink: '#ffffff' },
    { key: 'unknown', label: '未检测', short: '未检', count: unknown, color: '#c7c7cc', side: '#97979d', light: '#e2e2e6', ink: '#59595f' },
    { key: 'unavailable', label: '不可用', short: '不可用', count: unavailable, color: '#ee7b76', side: '#c94b46', light: '#f6a6a2', ink: '#ffffff' },
  ].filter((item) => item.count > 0)
  if (!raw.length) return []
  const sum = raw.reduce((acc, item) => acc + item.count, 0)
  const exact = raw.map((item) => item.count / sum * 100)
  const floors = exact.map((value) => Math.floor(value))
  let assigned = floors.reduce((acc, value) => acc + value, 0)
  const queue = exact.map((value, index) => ({ index, fraction: value - floors[index] }))
    .sort((a, b) => b.fraction - a.fraction)
  for (let i = 0; assigned < 100 && i < queue.length; i += 1) {
    floors[queue[i].index] += 1
    assigned += 1
  }
  return raw.map((item, index) => ({
    ...item, percent: floors[index], height: item.count / sum * 100,
  }))
})
const statusTopColor = computed(() => statusSegments.value[0]?.color || '#c7c7cc')
const scoreWeights = computed(() => {
  const weights = data.value?.score_weights || {}
  return {
    available: weights.available ?? 50,
    publish: weights.publish ?? 40,
    share: weights.share ?? 5,
    tag: weights.tag ?? 5,
  }
})
const statusCounts = computed(() => ({
  qualified: data.value?.nodes_qualified || 0,
  available: data.value?.nodes_available || 0,
  unavailable: data.value?.nodes_unavailable || 0,
  unknown: data.value?.nodes_unknown || 0,
}))

const publishedTotal = computed(() => (data.value?.airport_list || []).reduce((sum, row) => sum + Number(row.published_count || 0), 0))
const publishRatio = computed(() => {
  const total = data.value?.nodes_total || 0
  return total ? Math.round((publishedTotal.value / total) * 100) : 0
})
const airportOk = computed(() => data.value?.fetch?.ok || 0)
const airportTotal = computed(() => data.value?.fetch?.total || data.value?.airport_list?.length || 0)
const airportFailed = computed(() => data.value?.fetch?.error || 0)
const qualifiedPct = computed(() => {
  const total = data.value?.nodes_total || 0
  return total ? Math.round(((data.value?.nodes_qualified || 0) / total) * 100) : 0
})

const rateMetrics = [
  { key: 'available_rate' },
  { key: 'publish_rate' },
  { key: 'tag_rate' },
]
function scoreTone(row) {
  if (!row.enabled) return 'is-off'
  if (row.score === null || row.score === undefined) return 'is-empty'
  const shown = row.score_exact ?? row.score
  if (shown >= 80) return 'is-good'
  if (shown >= 60) return 'is-ok'
  if (shown >= 40) return 'is-mid'
  if (shown >= 20) return 'is-low'
  return 'is-bad'
}
function dayHint(row) {
  if (row.maturity_score === undefined || row.maturity_score === null) return ''
  return `成熟度 ${row.maturity_score} 分：完整数据日 ${row.full_days}/7（≥12 份）、合格 ${row.qualified_days}/7（≥4 份）、覆盖率 ${row.coverage ?? '—'}%`
}
function statusHint(row) {
  const days = dayHint(row)
  if (!row.enabled) return '机场未启用，不参与评分'
  if (row.data_status === 'insufficient') return `数据成熟度不足，节点评分留空${days ? '；' + days : ''}`
  return `${days}。成熟度只说明这份 7 天评分的可信度，不参与节点评分计算`
}
function scoreHint(row) {
  const pool = row.pool_count || 0
  if (!row.enabled) return '机场未启用，不参与评分'
  if (!pool) return '缓存池为空，暂不评分'
  const health = row.available_rate ?? Math.round((row.available_count || 0) / pool * 100)
  const publish = row.publish_rate ?? Math.round((row.published_count || 0) / pool * 100)
  const tag = row.tag_rate ?? Math.round((row.tag_ok_count || 0) / pool * 100)
  const basis = row.score_samples ? `近 7 天 ${row.score_samples} 个完整检测快照` : '无完整检测快照，按当前值'
  const rawLine = `可用率 ${health}%（×${scoreWeights.value.available}%）· 下发率 ${publish}%（×${scoreWeights.value.publish}%）· 标签合格率 ${tag}%（×${scoreWeights.value.tag}%）→ 原始 ${row.raw_score} 分 · 追加 ${row.bonus_score} 分 → 展示 ${row.score} 分（${row.score_grade}）`
  return `${basis} · ${rawLine}。追加分按原始分线性计算，只加一次，封顶 100`
}
function scoreSampleText(row) {
  if (!row.enabled) return '未启用'
  if (!row.pool_count) return '空池'
  if (row.sampled_points) return `${row.score_samples}/${row.sampled_points} 点命中`
  return row.score_samples ? `${row.score_samples} 个完整快照` : '按当前值'
}
function rateText(row, key) {
  if (!row.enabled || !row.pool_count || row[key] === null || row[key] === undefined) return '—'
  const value = Number(row[key])
  return `${value % 1 === 0 ? value.toFixed(0) : value.toFixed(1)}%`
}
function metricWidth(row, key) {
  if (!row.enabled || row[key] === null || row[key] === undefined) return 0
  return Math.max(0, Math.min(100, Number(row[key])))
}
const rankedAirports = computed(() => {
  return [...(data.value?.airport_list || [])].sort((a, b) => {
    if (!!a.enabled !== !!b.enabled) return a.enabled ? -1 : 1
    const ta = a.rank_tier ?? 0
    const tb = b.rank_tier ?? 0
    if (ta !== tb) return ta - tb
    const sa = Number.isFinite(a.score) ? a.score : -1
    const sb = Number.isFinite(b.score) ? b.score : -1
    if (sb !== sa) return sb - sa
    return (b.pool_count || 0) - (a.pool_count || 0)
  })
})
const statusTone = (row) => `is-${row.data_status || 'unknown'}`

const trendRange = ref('1d')
const trendDayCount = computed(() => trendRange.value === '30d' ? 30 : trendRange.value === '7d' ? 7 : 1)
watch(trendRange, () => { if (data.value) load() })
const allHistoryPoints = computed(() => Array.isArray(data.value?.share_history) ? data.value.share_history : [])
function trendPointTime(point) {
  if (point?.live) return Date.now()
  const time = new Date(String(point?.ts || '').replace(' ', 'T')).getTime()
  return Number.isNaN(time) ? 0 : time
}
const trendWindowPoints = computed(() => {
  const all = allHistoryPoints.value
  if (!all.length) return []
  const days = trendDayCount.value
  const cutoff = Date.now() - days * 86400000
  const rows = all.filter((point) => point.live || trendPointTime(point) >= cutoff)
  return rows.length ? rows : all.slice(-1)
})
const historyPoints = computed(() => trendWindowPoints.value)
const availableTrendPoints = computed(() => historyPoints.value.filter((point) => point.available_known !== 0 && point.available_known !== false))
const trendSampleText = computed(() => {
  const raw = trendWindowPoints.value.length
  if (!raw) return '暂无数据'
  const live = trendWindowPoints.value.some((point) => point.live)
  const measured = Math.max(0, raw - (live ? 1 : 0))
  return live ? `${measured} 次检测 + 当前` : `${measured} 次检测`
})
const trendRangeText = computed(() => `近 ${trendDayCount.value} 天`)
function trendChartPoints(points, key, rows) {
  if (trendDayCount.value === 1 || !points.length) return points
  const first = points.findIndex((point) => rows?.some((row) => row.enabled && trendCount(point, row, key) > 0))
  return first > 0 ? points.slice(first) : points
}
function trendCount(point, row, key) {
  return Number(point?.airports?.[String(row.id)]?.[key]) || 0
}
const trendCharts = computed(() => [
  {
    key: 'publish',
    title: '下发节点趋势',
    field: 'published_count',
    rows: publishChartRows.value,
    points: trendChartPoints(historyPoints.value, 'published_count', publishChartRows.value),
  },
  {
    key: 'available',
    title: '可用节点趋势',
    field: 'available_count',
    rows: nodeChartRows.value,
    points: trendChartPoints(availableTrendPoints.value, 'available_count', nodeChartRows.value),
  },
])
const fetchState = computed(() => {
  const f = data.value?.fetch
  if (!f) return { text: '—', type: 'info' }
  if (f.fetching) return { text: `拉取中 ${f.fetching}`, type: 'warning' }
  if (f.error) return { text: `${f.error} 个失败`, type: 'danger' }
  if (!f.total) return { text: '未配置', type: 'info' }
  return { text: '正常', type: 'success' }
})
const runStateText = computed(() => {
  const r = runtime.value
  if (!r?.running) return '空闲'
  if (r.stop_requested) return '正在停止'
  if (r.paused) return '已暂停'
  return '检测中'
})
const runTagType = computed(() => {
  const r = runtime.value
  if (!r?.running) return 'success'
  return r.stop_requested ? 'danger' : r.paused ? 'info' : 'warning'
})
const batchStatusLabel = computed(() => {
  const map = { running: '检测中', complete: '已完成', stopped: '已停止', failed: '失败', aborted: '已中止' }
  return map[data.value?.batch?.status] || data.value?.batch?.status || '—'
})
const latencyPct = computed(() => Math.round(runtime.value?.latency_pct || 0))
const tagPct = computed(() => Math.round(runtime.value?.tag_pct || 0))
const runTone = computed(() => {
  const r = runtime.value
  if (!r?.running) return 'done'
  return r.stop_requested ? 'failed' : 'running'
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
function chartAmount(row, key) {
  return row.enabled ? (row[key] || 0) : 0
}
function rankRows(rows, key) {
  return [...(rows || [])].sort((a, b) => {
    if (!!a.enabled !== !!b.enabled) return a.enabled ? -1 : 1
    const diff = chartAmount(b, key) - chartAmount(a, key)
    if (diff) return diff
    return (b.pool_count || 0) - (a.pool_count || 0)
  })
}
function airportStateText(row) {
  if (!row.enabled) return '未启用'
  if (row.last_status === 'ok') return '正常'
  if (row.last_status === 'error') return '失败'
  return '未拉取'
}
function shortTs(ts) {
  if (!ts) return '—'
  return String(ts).replace(/^\d{4}-/, '').replace('T', ' ').slice(0, 11)
}

onMounted(() => {
  // 只在检测进行中才轮询，空闲时不做任何定时刷新
  load()
})
onUnmounted(() => { clearInterval(timer.value); clearInterval(liveTimer.value) })
</script>

<template>
  <div class="np-page np-dash-page" v-loading="loading">
    <header v-if="data" class="dash-headline">
      <div class="dash-headline-copy">
        <span class="dash-eyebrow"><i></i> NODE POOL OPERATIONS · LIVE</span>
        <h1>节点池<span>控制总览</span></h1>
        <p>机场订阅在本地汇聚成节点资产，经过双阶段检测、评分与成熟度筛选后，统一交付给客户端。</p>
      </div>
      <div class="dash-headline-actions">
        <span class="dash-live" :class="{ 'is-running': runtime.running }"><i></i>{{ runStateText }}</span>
        <el-button class="dash-run-btn" type="primary" :disabled="runtime.running" @click="runNow">
          <el-icon><VideoPlay /></el-icon>{{ runtime.running ? '检测进行中' : '启动全量检测' }}
        </el-button>
      </div>
    </header>

    <section v-if="data" class="kpi-grid">
      <article class="kpi-card kpi-mint">
        <div class="kpi-top"><span>NODE INVENTORY</span><el-icon><Connection /></el-icon></div>
        <div class="kpi-value">{{ data.nodes_total }}<small>缓存节点</small></div>
        <p>{{ data.nodes_unknown || 0 }} 个待检测 · {{ data.nodes_unavailable || 0 }} 个不可用</p>
        <div class="kpi-track"><i :style="{ width: `${qualifiedPct}%` }"></i></div>
      </article>
      <article class="kpi-card kpi-violet">
        <div class="kpi-top"><span>DISTRIBUTION</span><el-icon><Link /></el-icon></div>
        <div class="kpi-value">{{ publishedTotal }}<small>下发节点</small></div>
        <p>占缓存池 {{ publishRatio }}% · 原子替换统一订阅</p>
        <div class="kpi-track"><i :style="{ width: `${publishRatio}%` }"></i></div>
      </article>
      <article class="kpi-card kpi-blue">
        <div class="kpi-top"><span>SOURCE HEALTH</span><el-icon><OfficeBuilding /></el-icon></div>
        <div class="kpi-value">{{ airportOk }}<small>/ {{ airportTotal }} 机场在线</small></div>
        <p>{{ airportFailed ? `${airportFailed} 个拉取失败` : '全部机场订阅同步正常' }} · 失败保留旧缓存</p>
        <div class="kpi-airports">
          <i v-for="row in (data.airport_list || [])" :key="row.id"
             :class="!row.enabled ? 'is-off' : row.last_status === 'ok' ? 'is-ok' : row.last_status === 'error' ? 'is-bad' : 'is-wait'"
             :title="row.name" />
        </div>
      </article>
      <article class="kpi-card kpi-amber">
        <div class="kpi-top"><span>TEST PULSE</span><el-icon><Aim /></el-icon></div>
        <div class="kpi-pulse-row">
          <div class="kpi-value">{{ latencyPct }}<small>% 延迟检测</small></div>
          <div class="kpi-ring" :style="{ background: `conic-gradient(#2ee6a8 ${latencyPct}%, rgba(255,255,255,.08) 0)` }">
            <span>{{ latencyPct }}<small>%</small></span>
          </div>
        </div>
        <p>标签检测 {{ tagPct }}% · 批次 {{ data.batch?.id ? `#${data.batch.id}` : '—' }} · {{ batchStatusLabel }}</p>
        <div class="kpi-track"><i :style="{ width: `${tagPct}%` }"></i></div>
      </article>
    </section>

    <div v-if="data" class="dash-workbench">
      <section class="console-panel pool-panel">
        <div class="panel-head">
          <div>
            <span class="panel-kicker">POOL STRUCTURE</span>
            <h2>节点资产与机场评分</h2>
          </div>
          <div class="pool-status-pill" :class="{ 'is-warn': data.fetch?.error || data.fetch?.fetching }">
            <i></i>{{ data.fetch?.fetching ? fetchState.text : data.fetch?.error ? `${data.fetch.error} 个机场失败` : `${data.fetch?.ok || 0}/${data.fetch?.total || 0} 机场正常` }}
          </div>
        </div>

        <div class="pool-summary">
          <div class="pool-total">
            <span>POOL TOTAL</span>
            <strong>{{ data.nodes_total }}</strong>
            <small>{{ qualifiedPct }}% 已通过延迟门槛</small>
          </div>
          <div class="pool-mix-wrap">
            <div class="pool-mix" aria-label="节点池分布">
              <span v-for="segment in statusSegments" :key="segment.key"
                    :style="{ width: `${segment.percent}%`, background: segment.color }"
                    :title="`${segment.label} ${segment.count} 个 · ${segment.percent}%`" />
            </div>
            <div class="pool-legend">
              <span v-for="segment in statusSegments" :key="`lg-${segment.key}`">
                <i :style="{ background: segment.color }"></i>{{ segment.label }} <b>{{ segment.count }}</b>
              </span>
              <span v-if="!statusSegments.length" class="np-muted">暂无节点状态</span>
            </div>
          </div>
          <div class="pool-side-stats">
            <div><span>待检测</span><b>{{ statusCounts.unknown }}</b></div>
            <div><span>不可用</span><b>{{ statusCounts.unavailable }}</b></div>
            <div><span>已下发</span><b>{{ statusCounts.qualified }}</b></div>
          </div>
        </div>

        <div v-if="data.airport_list?.length" class="score-desktop">
          <table class="np-score-table console-score-table">
            <colgroup>
              <col class="np-score-col-rank" />
              <col class="np-score-col-name" />
              <col class="np-score-col-rate" />
              <col class="np-score-col-rate" />
              <col class="np-score-col-rate" />
              <col class="np-score-col-status" />
              <col class="np-score-col-score" />
            </colgroup>
            <thead>
              <tr>
                <th>#</th>
                <th class="is-left">机场资产</th>
                <th>可用率 <i>{{ scoreWeights.available }}%</i></th>
                <th>下发率 <i>{{ scoreWeights.publish }}%</i></th>
                <th>标签率 <i>{{ scoreWeights.tag }}%</i></th>
                <th class="is-status">成熟度</th>
                <th class="is-score">评分</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, idx) in rankedAirports" :key="row.id" :class="{ 'is-off': !row.enabled }">
                <td class="np-score-rank" :class="{ 'is-off': !row.enabled }">{{ String(idx + 1).padStart(2, '0') }}</td>
                <td class="np-score-airport">
                  <div class="np-score-airport-name">
                    <span class="np-airport-dot" :class="!row.enabled ? 'is-off' : `is-${row.last_status || 'none'}`" :title="airportStateText(row)" />
                    <b>{{ row.name }}</b>
                    <span v-if="!row.enabled" class="np-pool-off">未启用</span>
                  </div>
                  <small>{{ row.enabled ? `${row.pool_count} 节点` : `${row.node_count} 缓存` }} · {{ scoreSampleText(row) }}</small>
                </td>
                <td v-for="metric in rateMetrics" :key="metric.key" class="np-score-cell">
                  <div class="np-score-metric">
                    <b>{{ rateText(row, metric.key) }}</b>
                    <i v-if="row.enabled" class="np-meter"><s :style="{ width: metricWidth(row, metric.key) + '%' }" /></i>
                  </div>
                </td>
                <td class="np-score-cell is-status" :title="statusHint(row)">
                  <div class="np-data-status" :class="statusTone(row)">
                    <b>{{ row.data_status_text || '—' }}<em v-if="row.data_status_detail">{{ row.data_status_detail }}</em></b>
                  </div>
                </td>
                <td class="np-score-cell is-score" :title="scoreHint(row)">
                  <div class="np-pool-score" :class="scoreTone(row)">
                    <strong>{{ row.enabled && row.score !== null ? row.score : '—' }}</strong><em>{{ row.score_grade }}</em>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <div class="score-footline">
            <span>评分 = 可用率 {{ scoreWeights.available }}% + 下发率 {{ scoreWeights.publish }}% + 标签 {{ scoreWeights.tag }}%；近 {{ data.score_window_days || 7 }} 天动态采样。</span>
            <span>成熟度只表示数据可信度，不参与评分。</span>
          </div>
        </div>

        <div v-if="data.airport_list?.length" class="score-mobile">
          <article v-for="(row, idx) in rankedAirports" :key="`m-${row.id}`" class="score-mobile-card" :class="{ 'is-off': !row.enabled }">
            <div class="score-mobile-head">
              <span class="score-mobile-rank">{{ String(idx + 1).padStart(2, '0') }}</span>
              <div>
                <b>{{ row.name }}</b>
                <small>{{ row.enabled ? `${row.pool_count} 节点` : '未启用' }} · {{ row.data_status_text || '—' }}</small>
              </div>
              <div class="np-pool-score" :class="scoreTone(row)">
                <strong>{{ row.enabled && row.score !== null ? row.score : '—' }}</strong><em>{{ row.score_grade }}</em>
              </div>
            </div>
            <div class="score-mobile-metrics">
              <span>可用 <b>{{ rateText(row, 'available_rate') }}</b></span>
              <span>下发 <b>{{ rateText(row, 'publish_rate') }}</b></span>
              <span>标签 <b>{{ rateText(row, 'tag_rate') }}</b></span>
            </div>
          </article>
        </div>

        <div v-if="!data.airport_list?.length" class="np-empty">缓存池为空，先到「机场接入」添加并拉取订阅。</div>
      </section>

      <aside class="dash-side">
        <section class="console-panel detect-panel">
          <div class="panel-head compact">
            <div>
              <span class="panel-kicker">LIVE BATCH</span>
              <h2>检测任务</h2>
            </div>
            <el-tag v-if="runtime.running" :type="runTagType" size="small">{{ runStateText }}</el-tag>
            <el-tag v-else type="success" size="small">空闲</el-tag>
          </div>

          <div class="detect-rings">
            <div class="ring-item">
              <div class="np-batch-gauge is-ring" :class="`is-${runTone}`">
                <svg viewBox="0 0 120 120" aria-hidden="true">
                  <circle class="np-batch-gauge-track" cx="60" cy="60" r="48" pathLength="100" />
                  <circle class="np-batch-gauge-value" cx="60" cy="60" r="48" pathLength="100"
                          :style="{ strokeDasharray: `${latencyPct} 100` }" />
                </svg>
                <div class="np-batch-gauge-label"><strong>{{ latencyPct }}</strong><span>%</span></div>
              </div>
              <b>延迟检测</b>
              <small>{{ runtime.latency_done || 0 }} / {{ runtime.latency_total || 0 }}</small>
            </div>
            <div class="ring-item">
              <div class="np-batch-gauge is-ring is-tag" :class="`is-${runTone}`">
                <svg viewBox="0 0 120 120" aria-hidden="true">
                  <circle class="np-batch-gauge-track" cx="60" cy="60" r="48" pathLength="100" />
                  <circle class="np-batch-gauge-value" cx="60" cy="60" r="48" pathLength="100"
                          :style="{ strokeDasharray: `${tagPct} 100` }" />
                </svg>
                <div class="np-batch-gauge-label"><strong>{{ tagPct }}</strong><span>%</span></div>
              </div>
              <b>标签检测</b>
              <small>{{ runtime.tag_done || 0 }} / {{ runtime.tag_total || 0 }}</small>
            </div>
          </div>

          <div class="current-node" :class="{ 'is-idle': !runtime.running }">
            <template v-if="runtime.running && runtime.current">
              <div class="current-head"><i class="np-current-dot" /><span>正在检测</span><span class="np-flex" /><em>{{ runtime.current.stage }}</em></div>
              <b class="current-name" :title="runtime.current.node">{{ runtime.current.node }}</b>
              <div class="current-meta">
                <span>{{ runtime.current.airport || '—' }}</span>
                <span v-if="runtime.current.g || runtime.current.w">G {{ runtime.current.g ?? '—' }}ms · W {{ runtime.current.w ?? '—' }}ms</span>
                <span class="np-current-result" :class="{ 'is-bad': ['未达标','未通过'].includes(runtime.current.result) }">{{ runtime.current.result }}</span>
              </div>
            </template>
            <template v-else>
              <div class="current-head"><i class="np-current-dot is-idle" /><span>调度空闲</span><span class="np-flex" /><em>{{ runtime.engine || 'mihomo' }}</em></div>
              <div class="current-meta"><span>批次 #{{ runtime.batch_id || data.batch?.id || '—' }}</span><span>已处理 {{ runtime.done || 0 }} / {{ runtime.total || 0 }} 项</span></div>
            </template>
          </div>

          <div class="detect-actions">
            <el-button v-if="!runtime.running" size="small" type="primary" @click="runNow">立即检测</el-button>
            <el-button v-if="runtime.running && !runtime.paused" size="small" :disabled="runtime.stop_requested" @click="pauseCycle">暂停</el-button>
            <el-button v-if="runtime.running && runtime.paused" size="small" type="success" :disabled="runtime.stop_requested" @click="resumeCycle">继续</el-button>
            <el-button v-if="runtime.running" size="small" type="danger" plain :disabled="runtime.stop_requested" @click="stopCycle">
              {{ runtime.stop_requested ? '正在停止' : '停止检测' }}
            </el-button>
          </div>

          <div class="detect-stats">
            <div><span>批次</span><b>#{{ data.batch?.id || '—' }}</b></div>
            <div><span>耗时</span><b>{{ batchDuration }}</b></div>
            <div><span>剩余</span><b>{{ batchRemaining }}</b></div>
          </div>
        </section>

        <section class="console-panel logs-panel">
          <div class="panel-head compact">
            <div>
              <span class="panel-kicker">SYSTEM LOG</span>
              <h2>运行日志</h2>
            </div>
            <div class="log-tools">
              <el-select v-model="logLevel" size="small" style="width:88px" @change="loadLogs">
                <el-option label="全部" value="ALL" />
                <el-option label="信息" value="INFO" />
                <el-option label="警告" value="WARN" />
                <el-option label="错误" value="ERROR" />
                <el-option label="调试" value="DEBUG" />
              </el-select>
              <el-button size="small" :loading="logsLoading" @click="loadLogs">刷新</el-button>
            </div>
          </div>
          <div class="console-log" v-loading="logsLoading">
            <div v-for="log in logs" :key="log.id">
              <time>{{ formatTs(log.ts) }}</time>
              <span :class="`lv-${log.level}`">{{ logLevelLabel(log.level) }}</span>
              <span class="src">{{ logSourceLabel(log.source) }}</span>
              <p>{{ log.message }}</p>
            </div>
            <div v-if="!logs.length" class="np-empty">暂无日志</div>
          </div>
          <span class="log-count">{{ logs.length }}/300 · 自动刷新</span>
        </section>
      </aside>
    </div>

    <OperationsMap v-if="data" :data="data" :runtime="runtime" :version="state.version" />

    <section v-if="data?.airport_list?.length" class="console-panel trend-panel">
      <div class="panel-head">
        <div>
          <span class="panel-kicker">POOL HISTORY</span>
          <h2>节点趋势</h2>
          <p>{{ trendRangeText }} · {{ trendSampleText }}</p>
        </div>
        <div class="trend-range" role="group" aria-label="趋势区间">
          <button type="button" :class="{ 'is-active': trendRange === '1d' }" @click="trendRange = '1d'">1D</button>
          <button type="button" :class="{ 'is-active': trendRange === '7d' }" @click="trendRange = '7d'">7D</button>
          <button type="button" :class="{ 'is-active': trendRange === '30d' }" @click="trendRange = '30d'">30D</button>
        </div>
      </div>
      <div class="trend-grid">
        <div v-for="chart in trendCharts" :key="chart.key" class="trend-card">
          <TrendChart :points="chart.points" :rows="chart.rows" :field="chart.field"
                      :range="trendRange" :title="chart.title" :palette="AIRPORT_COLORS" />
        </div>
      </div>
    </section>

    <div v-if="data" class="dash-footer">
      <span>ALL SYSTEMS LOCAL</span>
      <p>检测只读取本地缓存 · 机场失败保留旧版本 · 订阅发布使用原子替换</p>
      <span>v{{ state.version || '—' }}</span>
    </div>
  </div>
</template>
