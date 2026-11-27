<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs, logLevelLabel, logSourceLabel, state } from '../api'
import TrendChart from '../components/TrendChart.vue'
import OperationsMap from '../components/OperationsMap.vue'

const data = ref(null)
// 趋势图配色：深金、深绿、灰绿、铜棕、橄榄，和整页的磨砂评分室同一色域
const AIRPORT_COLORS = ['#a8751c', '#2f6555', '#5d7f70', '#9a5a37', '#7c8b60',
  '#b58a3a', '#45685d', '#8a6a2f', '#3f6d68', '#b06a32', '#6f8078', '#c0a15a']
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
    window.dispatchEvent(new CustomEvent('np:dashboard', { detail: data.value }))
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
    { key: 'qualified', label: '已下发', short: '下发', count: qualified, color: '#d6a41d', side: '#a67d16', light: '#efdc9c', ink: '#ffffff' },
    { key: 'available', label: '未达标', short: '未达标', count: availableOnly, color: '#3f665c', side: '#31524a', light: '#9bc3b4', ink: '#ffffff' },
    { key: 'unknown', label: '未检测', short: '未检', count: unknown, color: '#c7d7cf', side: '#a9bdb3', light: '#e3eee8', ink: '#40564d' },
    { key: 'unavailable', label: '不可用', short: '不可用', count: unavailable, color: '#c8755c', side: '#a9543e', light: '#e6ad9c', ink: '#ffffff' },
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
  if (shown >= 70) return 'is-ok'
  if (shown >= 60) return 'is-mid'
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

/* ------------------------------------------------ v2.005 C×E 评分主卡 */
const enabledAirports = computed(() => (data.value?.airport_list || []).filter((row) => row.enabled))
const scoredAirports = computed(() => enabledAirports.value.filter((row) => Number.isFinite(Number(row.score))))
const overallScoreValue = computed(() => {
  let weight = 0
  let sum = 0
  for (const row of scoredAirports.value) {
    const w = Math.max(1, Number(row.pool_count) || 1)
    weight += w
    sum += Number(row.score) * w
  }
  return weight ? Math.round((sum / weight) * 10) / 10 : null
})
const overallScoreText = computed(() => overallScoreValue.value === null ? '—' : overallScoreValue.value.toFixed(1))
const overallGrade = computed(() => {
  const value = overallScoreValue.value
  if (value === null) return 'N/A'
  if (value >= 90) return 'A+'
  if (value >= 80) return 'A'
  if (value >= 70) return 'B'
  if (value >= 60) return 'C'
  return 'D'
})
const scorePct = computed(() => Math.max(0, Math.min(100, Number(overallScoreValue.value) || 0)))
const scoreGaugeStyle = computed(() => ({
  background: `conic-gradient(#efc44c 0 ${scorePct.value}%, rgba(255,255,255,.12) ${scorePct.value}% 100%)`
}))
const orbitRows = computed(() => enabledAirports.value
  .filter((row) => Number.isFinite(Number(row.score)))
  .sort((a, b) => Number(b.score) - Number(a.score))
  .slice(0, 5))
const orbitPoints = computed(() => {
  const rows = orbitRows.value
  if (!rows.length) return []
  const maxPool = Math.max(1, ...rows.map((row) => Number(row.pool_count) || 0))
  return rows.map((row, index) => {
    const angle = -Math.PI / 2 + index * (Math.PI * 2 / 5)
    const radius = 48 + ((Number(row.pool_count) || 0) / maxPool) * 34
    const x = 150 + Math.cos(angle) * radius
    const y = 108 + Math.sin(angle) * radius
    const right = x >= 150
    return {
      id: row.id,
      x,
      y,
      lx: x + (right ? 9 : -9),
      ly: y + (y < 108 ? -3 : 13),
      anchor: right ? 'start' : 'end',
      name: String(row.name || '').slice(0, 7),
      score: row.score,
      color: Number(row.score) >= 85 ? '#efc44c' : Number(row.score) >= 70 ? '#9bd0ba' : Number(row.score) >= 55 ? '#c6dbd1' : '#df9a83',
    }
  })
})
const orbitPolyline = computed(() => orbitPoints.value.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '))
const orbitPoolText = computed(() => orbitRows.value.reduce((sum, row) => sum + Number(row.pool_count || 0), 0))
const weightedRate = (key) => {
  const rows = scoredAirports.value.filter((row) => row[key] !== null && row[key] !== undefined)
  let weight = 0
  let sum = 0
  for (const row of rows) {
    const w = Math.max(1, Number(row.pool_count) || 1)
    weight += w
    sum += Number(row[key]) * w
  }
  return weight ? Math.round((sum / weight) * 10) / 10 : 0
}
const breakdownRows = computed(() => [
  { key: 'available', label: '可用率', sub: `${scoreWeights.value.available}% 权重`, value: weightedRate('available_rate') },
  { key: 'publish', label: '下发率', sub: `${scoreWeights.value.publish}% 权重`, value: weightedRate('publish_rate') },
  { key: 'tag', label: '标签合格率', sub: `${scoreWeights.value.tag}% 权重`, value: weightedRate('tag_rate') },
])
const gradeBands = computed(() => {
  const rows = scoredAirports.value
  const total = rows.length || 1
  const bands = [
    { key: 'high', label: '85+ 优先', color: '#d6a41d', count: rows.filter((row) => Number(row.score) >= 85).length },
    { key: 'mid', label: '70–84 稳定', color: '#3f665c', count: rows.filter((row) => Number(row.score) >= 70 && Number(row.score) < 85).length },
    { key: 'low', label: '低于 70 复核', color: '#c8755c', count: rows.filter((row) => Number(row.score) < 70).length },
  ]
  return bands.map((band) => ({ ...band, pct: Math.round(band.count / total * 100) }))
})
const ringOuterStyle = computed(() => ({
  background: `conic-gradient(#3f665c 0 ${Math.min(100, publishRatio.value)}%, #dbe9e2 ${Math.min(100, publishRatio.value)}% 100%)`
}))
const ringInnerStyle = computed(() => ({
  background: `conic-gradient(#d6a41d 0 ${Math.min(100, qualifiedPct.value)}%, #eee7d4 ${Math.min(100, qualifiedPct.value)}% 100%)`
}))
const trendMode = ref('available')
const activeTrend = computed(() => trendCharts.value.find((chart) => chart.key === trendMode.value) || trendCharts.value[1])
const activeTrendCurrent = computed(() => trendMode.value === 'publish' ? publishedTotal.value : (data.value?.nodes_available || 0))
const activeTrendLabel = computed(() => trendMode.value === 'publish' ? '下发节点' : '可用节点')
const batchBars = computed(() => {
  const points = allHistoryPoints.value.filter((point) => !point.live).slice(-6)
  return points.map((point) => {
    const total = Object.values(point.airports || {}).reduce((sum, item) => sum + Number(item.available_count || 0), 0)
    const ts = String(point.ts || '')
    return {
      key: ts || String(Math.random()),
      value: total,
      label: ts ? ts.slice(5, 10).replace('-', '/') : '—',
      height: 0,
    }
  })
})
const batchBarMax = computed(() => Math.max(1, ...batchBars.value.map((bar) => bar.value)))
const modeRows = computed(() => [
  { icon: 'Files', title: '本地缓存优先', desc: '检测不触发机场拉取', on: true },
  { icon: 'RefreshLeft', title: '失败保留旧缓存', desc: '拉取失败不清空节点', on: true },
  { icon: 'Switch', title: '原子替换订阅', desc: '发布时整包切换', on: true },
  { icon: 'Timer', title: '自动发布', desc: data.value?.publish_schedule?.enabled ? `每天 ${data.value.publish_schedule.time || '04:30'}` : '当前为手动发布', on: !!data.value?.publish_schedule?.enabled, gold: true },
])

onMounted(() => {
  // 只在检测进行中才轮询，空闲时不做任何定时刷新
  load()
})
onUnmounted(() => { clearInterval(timer.value); clearInterval(liveTimer.value) })
</script>

<template>
  <div class="np-page frostdash-page" v-loading="loading">
    <header v-if="data" class="dash-headline">
      <div class="dash-headline-copy">
        <div class="dash-eyebrow"><b>SCORE ROOM</b><i></i><span>V2.005 · FROST</span></div>
        <h1>节点池 <span>评分室</span></h1>
        <p>机场订阅在本地汇聚成节点资产，经过双阶段检测、评分与成熟度筛选后，统一交付给客户端。</p>
      </div>
      <div class="dash-headline-actions">
        <span class="dash-pill" :class="{ 'is-gold': !runtime.running }"><i></i>{{ runStateText }}</span>
        <span class="dash-pill">批次 #{{ data.batch?.id || '—' }}</span>
        <button class="dash-runbtn" :disabled="runtime.running" @click="runNow">
          <el-icon><VideoPlay /></el-icon>{{ runtime.running ? '检测进行中' : '启动全量检测' }}
        </button>
      </div>
    </header>

    <section v-if="data" class="dash-grid">
      <article class="frostcard scorecard theme-dark span8">
        <div class="card-head">
          <div>
            <div class="card-kicker"><i></i>OVERALL AIRPORT SCORE</div>
            <h2>整体评分</h2>
            <p class="muted">按缓存池规模加权的机场展示分</p>
          </div>
          <span class="card-chip">{{ scoredAirports.length }} / {{ enabledAirports.length }} 机场计分</span>
        </div>

        <div class="score-body">
          <div class="score-gauge-wrap">
            <div class="score-gauge" :style="scoreGaugeStyle">
              <div class="score-gauge-hole">
                <b>{{ overallScoreText }}</b>
                <em>OVERALL</em>
                <small>{{ overallGrade }}</small>
              </div>
              <span class="score-gauge-cap">{{ qualifiedPct }}% 合格</span>
            </div>
          </div>

          <div class="orbit-wrap">
            <svg viewBox="0 0 300 216" role="img" aria-label="机场评分轨道">
              <circle cx="150" cy="108" r="82" fill="none" stroke="rgba(255,255,255,.10)" stroke-width="1" />
              <circle cx="150" cy="108" r="62" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="1" />
              <circle cx="150" cy="108" r="42" fill="none" stroke="rgba(255,255,255,.055)" stroke-width="1" />
              <line x1="68" y1="108" x2="232" y2="108" stroke="rgba(255,255,255,.07)" stroke-width="1" />
              <line x1="150" y1="26" x2="150" y2="190" stroke="rgba(255,255,255,.07)" stroke-width="1" />
              <polyline v-if="orbitPolyline" :points="orbitPolyline" fill="none" stroke="rgba(239,196,76,.24)" stroke-width="1.4" />
              <g v-for="point in orbitPoints" :key="point.id">
                <circle :cx="point.x" :cy="point.y" r="5.3" :fill="point.color" stroke="rgba(255,255,255,.22)" stroke-width="1" />
                <circle :cx="point.x" :cy="point.y" r="9" fill="none" :stroke="point.color" stroke-opacity=".28" stroke-width="1" />
                <text :x="point.lx" :y="point.ly" :text-anchor="point.anchor" fill="rgba(246,250,244,.66)" font-size="7.4" font-weight="600">{{ point.name }}</text>
                <text :x="point.lx" :y="point.ly + 9" :text-anchor="point.anchor" :fill="point.color" font-size="7.2" font-weight="700">{{ point.score }}</text>
              </g>
            </svg>
            <div class="orbit-center">
              <b>{{ orbitRows.length }}</b>
              <span>AIRPORTS IN ORBIT</span>
              <i>{{ orbitPoolText }} 缓存节点</i>
            </div>
          </div>

          <div class="score-rank">
            <div class="score-rank-head"><span>TOP AIRPORTS</span><span>SCORE</span></div>
            <div v-for="(row, idx) in rankedAirports.filter((r) => r.enabled).slice(0, 5)" :key="`rank-${row.id}`" class="rankrow">
              <em>{{ String(idx + 1).padStart(2, '0') }}</em>
              <span class="nm" :title="row.name">{{ row.name }}</span>
              <b :class="{ low: Number(row.score) < 85, bad: Number(row.score) < 70 }">{{ Number.isFinite(Number(row.score)) ? row.score : '—' }}</b>
            </div>
            <div v-if="!scoredAirports.length" class="rankrow"><em>--</em><span class="nm">暂无评分</span><b class="bad">—</b></div>
          </div>
        </div>

        <div class="scoreband">
          <div><span>ENABLED AIRPORTS</span><b>{{ enabledAirports.length }}<small>座</small></b><i>{{ airportOk }}/{{ airportTotal }} 拉取正常</i></div>
          <div><span>POOL TOTAL</span><b>{{ data.nodes_total }}<small>节点</small></b><i>{{ statusCounts.unknown }} 待检测 · {{ statusCounts.unavailable }} 不可用</i></div>
          <div><span>QUALIFIED</span><b>{{ data.nodes_qualified }}<small>合格</small></b><i>{{ qualifiedPct }}% 通过延迟门槛</i></div>
          <div><span>DELIVERED</span><b>{{ publishedTotal }}<small>下发</small></b><i>{{ publishRatio }}% 下发率</i></div>
        </div>

        <div class="score-legend">
          <span><i class="is-gold"></i>80+ 优先</span>
          <span><i class="is-mint"></i>60–79 观察</span>
          <span><i class="is-red"></i>低于 60 复核</span>
          <span class="np-flex"></span>
          <span>评分窗口 {{ data.score_window_days || 7 }} 天</span>
        </div>
      </article>

      <article class="frostcard scorebreak span4">
        <div class="break-head">
          <div class="card-kicker"><i></i>SCORE DECOMPOSITION</div>
          <h2>评分拆解</h2>
          <div class="break-main">
            <b>{{ overallScoreText }}</b><em>{{ overallGrade }}</em><span>加权展示分</span>
          </div>
        </div>
        <div class="break-rows">
          <div v-for="item in breakdownRows" :key="item.key" class="break-row">
            <div class="break-row-top">
              <b>{{ item.label }}</b><span>{{ item.sub }}</span><em>{{ item.value.toFixed(1) }}%</em>
            </div>
            <div class="break-track"><i :style="{ width: `${Math.max(0, Math.min(100, item.value))}%` }" :class="{ 'is-gold': item.key === 'publish' }" /></div>
          </div>
        </div>
        <div class="break-note"><span>权重来自后端 <b>score_weights</b></span><span>{{ data.score_window_days || 7 }} 天动态采样</span></div>
        <div class="grade-band">
          <div class="grade-band-head"><span>评分档位分布</span><b>{{ scoredAirports.length }} 座机场</b></div>
          <div v-for="band in gradeBands" :key="band.key" class="grade-band-row">
            <i :style="{ background: band.color }" />
            <span>{{ band.label }}</span>
            <b>{{ band.count }}</b>
            <em>{{ band.pct }}%</em>
          </div>
        </div>
        <div class="break-spacer" />
        <div class="break-foot">
          <b>{{ data.nodes_qualified }}</b> / {{ data.nodes_total }} 节点已通过延迟门槛；<b>{{ publishedTotal }}</b> 个节点进入统一订阅。
        </div>
      </article>

      <article class="frostcard trendbig span7">
        <div class="trend-head">
          <div class="trend-title">
            <div class="k"><i></i>POOL TREND · {{ trendRangeText }}</div>
            <h2>{{ activeTrendLabel }}走势</h2>
            <div class="num"><b>{{ activeTrendCurrent }}</b><small>{{ activeTrendLabel }} · {{ activeTrend.points.length }} 个采样点</small></div>
          </div>
          <div class="trend-tools">
            <div class="seg" role="group" aria-label="趋势指标">
              <button :class="{ active: trendMode === 'available' }" @click="trendMode = 'available'">可用</button>
              <button :class="{ active: trendMode === 'publish' }" @click="trendMode = 'publish'">下发</button>
            </div>
            <div class="seg" role="group" aria-label="趋势区间">
              <button :class="{ active: trendRange === '1d' }" @click="trendRange = '1d'">1D</button>
              <button :class="{ active: trendRange === '7d' }" @click="trendRange = '7d'">7D</button>
              <button :class="{ active: trendRange === '30d' }" @click="trendRange = '30d'">30D</button>
            </div>
          </div>
        </div>
        <div class="trend-chart">
          <TrendChart :points="activeTrend.points" :rows="activeTrend.rows" :field="activeTrend.field"
                      :range="trendRange" :title="''" :palette="AIRPORT_COLORS" />
        </div>
        <div class="trend-foot"><span>{{ trendRangeText }} · {{ trendSampleText }}</span><b>{{ activeTrendLabel }} {{ activeTrendCurrent }} / {{ data.nodes_total }}</b></div>
      </article>

      <article class="frostcard ringcard span5">
        <div class="card-head">
          <div>
            <div class="card-kicker"><i></i>DETECTION COMPOSITION</div>
            <h2>检测构成</h2>
            <p class="muted">外环下发占比，内环合格节点</p>
          </div>
          <span class="card-chip">{{ qualifiedPct }}% 合格</span>
        </div>
        <div class="ring-body">
          <div class="double-ring" :style="ringOuterStyle">
            <div class="double-ring-inner" :style="ringInnerStyle">
              <div class="double-ring-hole">
                <b>{{ qualifiedPct }}</b>
                <span>QUALIFIED</span>
                <small>{{ data.nodes_qualified }} / {{ data.nodes_total }}</small>
              </div>
            </div>
          </div>
          <div class="ring-legend">
            <div v-for="segment in statusSegments" :key="segment.key" class="ring-row">
              <i :style="{ background: segment.color }" />
              <span>{{ segment.label }}</span>
              <b>{{ segment.count }}</b>
              <em>{{ segment.percent }}%</em>
            </div>
            <div v-if="!statusSegments.length" class="ring-row"><i style="background:#c7d7cf" /><span>暂无节点</span><b>0</b><em>0%</em></div>
          </div>
        </div>
        <div class="ring-foot"><span>外环 下发 {{ publishRatio }}%</span><b>{{ publishedTotal }} DELIVERED</b></div>
      </article>

      <article class="frostcard detectcard span4">
        <div class="card-head">
          <div>
            <div class="card-kicker"><i></i>LIVE BATCH</div>
            <h2>检测任务</h2>
            <p class="muted">{{ runtime.engine || 'mihomo' }} · {{ runtime.batch_id ? `#${runtime.batch_id}` : '等待批次' }}</p>
          </div>
          <span class="card-chip">{{ runStateText }}</span>
        </div>
        <div class="detect-rings">
          <div class="detect-ring">
            <div class="gauge-ring" :style="{ background: `conic-gradient(#3f665c 0 ${latencyPct}%, #edf3ef ${latencyPct}% 100%)` }"><b>{{ latencyPct }}<small>%</small></b></div>
            <span>延迟检测</span>
          </div>
          <div class="detect-ring">
            <div class="gauge-ring" :style="{ background: `conic-gradient(#d6a41d 0 ${tagPct}%, #edf3ef ${tagPct}% 100%)` }"><b>{{ tagPct }}<small>%</small></b></div>
            <span>标签检测</span>
          </div>
        </div>
        <div class="detect-current">
          <template v-if="runtime.running && runtime.current">
            <b :title="runtime.current.node">{{ runtime.current.node }}</b>
            <span>{{ runtime.current.airport || '—' }} · G {{ runtime.current.g ?? '—' }}ms · W {{ runtime.current.w ?? '—' }}ms · {{ runtime.current.result }}</span>
          </template>
          <template v-else>
            <b>调度空闲</b>
            <span>已处理 {{ runtime.done || 0 }} / {{ runtime.total || 0 }} 项 · 批次 #{{ runtime.batch_id || data.batch?.id || '—' }}</span>
          </template>
        </div>
        <div class="detect-actions">
          <el-button v-if="!runtime.running" size="small" type="primary" @click="runNow">立即检测</el-button>
          <el-button v-if="runtime.running && !runtime.paused" size="small" :disabled="runtime.stop_requested" @click="pauseCycle">暂停</el-button>
          <el-button v-if="runtime.running && runtime.paused" size="small" type="success" :disabled="runtime.stop_requested" @click="resumeCycle">继续</el-button>
          <el-button v-if="runtime.running" size="small" type="danger" plain :disabled="runtime.stop_requested" @click="stopCycle">{{ runtime.stop_requested ? '正在停止' : '停止检测' }}</el-button>
        </div>
        <div class="detect-stats">
          <div><span>批次</span><b>#{{ data.batch?.id || '—' }}</b></div>
          <div><span>耗时</span><b>{{ batchDuration }}</b></div>
          <div><span>剩余</span><b>{{ batchRemaining }}</b></div>
        </div>
      </article>

      <article class="frostcard batchcard span4">
        <div class="card-head">
          <div>
            <div class="card-kicker"><i></i>BATCH ANALYSIS</div>
            <h2>批次分析</h2>
            <p class="muted">近 6 次检测快照的可用节点</p>
          </div>
          <span class="card-chip">{{ batchStatusLabel }}</span>
        </div>
        <div class="batch-top">
          <b>#{{ data.batch?.id || '—' }}</b>
          <span>{{ data.batch?.processed_count || runtime.done || 0 }} / {{ data.batch?.target_count || data.nodes_total || 0 }} 处理</span>
          <em>{{ batchRemaining }} 剩余</em>
        </div>
        <div v-if="batchBars.length" class="chart-cols">
          <div v-for="bar in batchBars" :key="bar.key" class="col">
            <b>{{ bar.value }}</b>
            <div class="col-bar" :class="{ 'is-mutet': bar.value === 0 }" :style="{ height: `${Math.max(7, Math.round(bar.value / batchBarMax * 100))}%` }" />
            <span>{{ bar.label }}</span>
          </div>
        </div>
        <div v-else class="batch-empty">暂无检测快照</div>
        <div class="batch-foot"><span>{{ trendRangeText }} · {{ trendSampleText }}</span><b>{{ data.batch?.status || 'idle' }}</b></div>
      </article>

      <article class="frostcard modecard span4">
        <div class="card-head">
          <div>
            <div class="card-kicker"><i></i>RUN POLICY</div>
            <h2>运行策略</h2>
            <p class="muted">状态来自后端运行与发布计划</p>
          </div>
          <span class="card-chip">POLICY</span>
        </div>
        <div class="mode-list">
          <div v-for="row in modeRows" :key="row.title" class="mode-row">
            <span class="mode-icon"><el-icon><component :is="row.icon" /></el-icon></span>
            <div class="mode-copy"><b>{{ row.title }}</b><span>{{ row.desc }}</span></div>
            <span class="ios-switch" :class="{ on: row.on, 'is-gold': row.gold }" role="img" :aria-label="`${row.title}${row.on ? '开启' : '关闭'}`" />
          </div>
        </div>
        <div class="mode-note">开关呈现当前策略状态；自动发布由发布计划控制，不在页面内直接改写。</div>
      </article>

      <article class="frostcard tablecard span12">
        <div class="table-head">
          <div>
            <div class="card-kicker"><i></i>AIRPORT LEDGER</div>
            <h2>机场资产与评分</h2>
            <p>可用率、下发率、标签率与成熟度；评分按后端权重计算。</p>
          </div>
          <div class="chips">
            <span class="card-chip">评分 = {{ scoreWeights.available }} / {{ scoreWeights.publish }} / {{ scoreWeights.tag }}</span>
            <span class="card-chip">近 {{ data.score_window_days || 7 }} 天</span>
          </div>
        </div>

        <div v-if="data.airport_list?.length" class="airport-table-wrap">
          <table class="np-score-table console-score-table">
            <colgroup>
              <col class="np-score-col-rank" />
              <col />
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
                  <div class="np-data-status" :class="statusTone(row)"><b>{{ row.data_status_text || '—' }}<em v-if="row.data_status_detail">{{ row.data_status_detail }}</em></b></div>
                </td>
                <td class="np-score-cell is-score" :title="scoreHint(row)">
                  <div class="np-pool-score" :class="scoreTone(row)"><strong>{{ row.enabled && row.score !== null ? row.score : '—' }}</strong><em>{{ row.score_grade }}</em></div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="data.airport_list?.length" class="airport-mcards">
          <div v-for="(row, idx) in rankedAirports" :key="`m-${row.id}`" class="amc" :class="{ 'is-off': !row.enabled }">
            <div class="amc-top">
              <div class="amc-name">
                <span class="amc-rank">{{ String(idx + 1).padStart(2, '0') }}</span>
                <span class="np-airport-dot" :class="!row.enabled ? 'is-off' : `is-${row.last_status || 'none'}`" />
                <b>{{ row.name }}</b>
                <span class="amc-status" :class="statusTone(row)">{{ row.data_status_text || '—' }}</span>
              </div>
              <div class="np-pool-score amc-score" :class="scoreTone(row)">
                <strong>{{ row.enabled && row.score !== null ? row.score : '—' }}</strong>
                <em>{{ row.score_grade }}</em>
              </div>
            </div>
            <div class="amc-sub">{{ row.enabled ? `${row.pool_count} 节点` : `${row.node_count} 缓存` }} · {{ scoreSampleText(row) }}</div>
            <div class="amc-metrics">
              <div v-for="metric in rateMetrics" :key="`m-${row.id}-${metric.key}`" class="amc-metric">
                <label>{{ metric.key === 'available_rate' ? '可用' : metric.key === 'publish_rate' ? '下发' : '标签' }}</label>
                <b>{{ rateText(row, metric.key) }}</b>
                <i class="np-meter"><s :style="{ width: metricWidth(row, metric.key) + '%' }" /></i>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="np-empty">缓存池为空，先到「机场接入」添加并拉取订阅。</div>

        <div class="table-foot">
          <span>评分 = 可用率 {{ scoreWeights.available }}% + 下发率 {{ scoreWeights.publish }}% + 标签 {{ scoreWeights.tag }}%；近 {{ data.score_window_days || 7 }} 天动态采样。</span>
          <span>成熟度只表示数据可信度，不参与评分。</span>
        </div>
      </article>

      <div class="opsmap-wrap span12">
        <OperationsMap :data="data" :runtime="runtime" :version="state.version" />
      </div>

      <article class="frostcard logcard span12">
        <div class="log-head">
          <div>
            <div class="card-kicker"><i></i>SYSTEM LOG</div>
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
            <span class="np-muted">{{ logs.length }}/300</span>
          </div>
        </div>
        <div v-if="logs.length" class="log-body" v-loading="logsLoading">
          <div v-for="log in logs" :key="log.id" class="log-line" :class="`lv-${log.level}`">
            <time>{{ formatTs(log.ts) }}</time>
            <b>{{ logLevelLabel(log.level) }}</b>
            <span>{{ logSourceLabel(log.source) }} · {{ log.message }}</span>
          </div>
        </div>
        <div v-else class="log-empty" v-loading="logsLoading">暂无日志</div>
      </article>
    </section>

    <div v-if="data" class="dash-footer">
      <span>ALL SYSTEMS LOCAL · 检测只读本地缓存 · 失败保留旧版本 · 原子替换订阅</span>
      <p>NodePoolME · FROST SCORE · v{{ state.version || '—' }}</p>
    </div>
  </div>
</template>
