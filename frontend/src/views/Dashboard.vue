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
  const published = Math.min(d.nodes_qualified || 0, total)
  const availableOnly = Math.max(0, available - published)
  const unavailable = Math.max(0, total - published - availableOnly)
  const raw = [
    { key: 'published', label: '已下发', short: '下发', count: published, color: '#d6a41d' },
    { key: 'available', label: '可用', short: '可用', count: availableOnly, color: '#3f665c' },
    { key: 'unavailable', label: '不可用', short: '不可用', count: unavailable, color: '#c8755c' },
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

function gradeTone(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'is-empty'
  const shown = Number(value)
  if (shown >= 80) return 'is-good'
  if (shown >= 60) return 'is-ok'
  if (shown >= 40) return 'is-mid'
  if (shown >= 20) return 'is-low'
  return 'is-bad'
}
function scoreTone(row) {
  if (!row.enabled) return 'is-off'
  if (row.score === null || row.score === undefined) return 'is-empty'
  return gradeTone(row.score_exact ?? row.score)
}
function maturityTone(row) {
  if (!row.enabled) return 'is-off'
  return gradeTone(row.maturity_score)
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
  const rawLine = `可用率 ${health}%（×${scoreWeights.value.available}%）· 下发率 ${publish}%（×${scoreWeights.value.publish}%）· 标签合格率 ${tag}%（×${scoreWeights.value.tag}%）→ 实际评分 ${row.score} 分（${row.score_grade}）`
  return `${basis} · ${rawLine}。评分直接按实际得分展示，不再追加分`
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
function unavailableRate(row) {
  if (!row.enabled || !row.pool_count || row.available_rate === null || row.available_rate === undefined) return null
  return Math.max(0, Math.min(100, 100 - Number(row.available_rate)))
}
function unavailableText(row) {
  const value = unavailableRate(row)
  if (value === null) return '—'
  return `${value % 1 === 0 ? value.toFixed(0) : value.toFixed(1)}%`
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
    key: 'available',
    title: '可用节点走势',
    label: '可用节点',
    current: data.value?.nodes_available || 0,
    field: 'available_count',
    rows: nodeChartRows.value,
    points: trendChartPoints(availableTrendPoints.value, 'available_count', nodeChartRows.value),
  },
  {
    key: 'publish',
    title: '下发节点走势',
    label: '下发节点',
    current: publishedTotal.value,
    field: 'published_count',
    rows: publishChartRows.value,
    points: trendChartPoints(historyPoints.value, 'published_count', publishChartRows.value),
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

/* ------------------------------------------------ v2.010 合并评分拆解卡 */
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
    { key: 'excellent', label: '优 80–100', color: '#16A34A', count: rows.filter((row) => Number(row.score) >= 80).length },
    { key: 'good', label: '良 60–79', color: '#2563EB', count: rows.filter((row) => Number(row.score) >= 60 && Number(row.score) < 80).length },
    { key: 'mid', label: '中 40–59', color: '#EAB308', count: rows.filter((row) => Number(row.score) >= 40 && Number(row.score) < 60).length },
    { key: 'low', label: '差 20–39', color: '#EA580C', count: rows.filter((row) => Number(row.score) >= 20 && Number(row.score) < 40).length },
    { key: 'bad', label: '劣 0–19', color: '#DC2626', count: rows.filter((row) => Number(row.score) < 20).length },
  ]
  return bands.map((band) => ({ ...band, pct: Math.round(band.count / total * 100) }))
})
const donutArcs = computed(() => {
  const segments = statusSegments.value
  if (!segments.length) return []
  const cx = 60
  const cy = 60
  const inner = 33
  const outer = 55
  const lift = 1.4
  const gap = 4.5
  let cursor = -90
  return segments.map((segment) => {
    const sweep = segment.percent * 3.6
    const start = cursor + gap / 2
    const end = cursor + sweep - gap / 2
    cursor += sweep
    if (end <= start) return null
    const point = (radius, angle) => {
      const rad = angle * Math.PI / 180
      return [cx + radius * Math.cos(rad), cy + radius * Math.sin(rad)]
    }
    const [x0, y0] = point(outer + lift, start)
    const [x1, y1] = point(outer + lift, end)
    const [x2, y2] = point(inner + lift, end)
    const [x3, y3] = point(inner + lift, start)
    const large = end - start > 180 ? 1 : 0
    return {
      key: segment.key,
      color: segment.color,
      d: `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${(outer + lift).toFixed(2)} ${(outer + lift).toFixed(2)} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)} L ${x2.toFixed(2)} ${y2.toFixed(2)} A ${(inner + lift).toFixed(2)} ${(inner + lift).toFixed(2)} 0 ${large} 0 ${x3.toFixed(2)} ${y3.toFixed(2)} Z`,
    }
  }).filter(Boolean)
})
const batchSpectrumAll = computed(() => {
  const source = allHistoryPoints.value.filter((point) => !point.live)
  const byBatch = new Map()
  for (const point of source) {
    const key = point.batch_id ?? point.ts
    const previous = byBatch.get(key)
    if (!previous || String(point.ts || '') > String(previous.ts || '')) byBatch.set(key, point)
  }
  let points = [...byBatch.values()].sort((a, b) => String(a.ts || '').localeCompare(String(b.ts || '')))
  // 环图显示最近一次结果，下方频谱明确排除同一批次
  const currentBatch = data.value?.batch?.id
  points = points.filter((point) => currentBatch == null || String(point.batch_id ?? '') !== String(currentBatch))
  return points.map((point, index) => {
    const total = Number(point.total_nodes) || Object.values(point.airports || {})
      .reduce((sum, item) => sum + Number(item.pool_count || 0), 0)
    if (!total) return null
    const published = Math.min(total, Math.max(0, Number(point.total_published) || 0))
    const available = Math.min(total, Math.max(0, Number(point.total_available) || 0))
    const availableOnly = Math.max(0, available - published)
    const unavailable = Math.max(0, total - published - availableOnly)
    const publishedPct = published / total * 100
    const availablePct = availableOnly / total * 100
    const unavailablePct = unavailable / total * 100
    const date = String(point.ts || '').slice(5, 10).replace('-', '/')
    const stamp = String(point.ts || '').slice(5, 16).replace('-', '/')
    return {
      key: `${point.batch_id ?? 'batch'}-${point.ts ?? index}`,
      batchId: point.batch_id ?? '—',
      date,
      stamp,
      published,
      availableOnly,
      unavailable,
      publishedPct: Math.round(publishedPct),
      availablePct: Math.round(availablePct),
      unavailablePct: Math.round(unavailablePct),
      title: `批次 #${point.batch_id ?? '—'} · ${stamp} · 已下发 ${published} (${Math.round(publishedPct)}%) · 可用 ${availableOnly} (${Math.round(availablePct)}%) · 不可用 ${unavailable} (${Math.round(unavailablePct)}%)`,
    }
  }).filter(Boolean)
})
const spectrumEl = ref(null)
const spectrumWidth = ref(0)
let spectrumObserver = null
watch(spectrumEl, (el) => {
  spectrumObserver?.disconnect()
  spectrumObserver = null
  if (!el) return
  spectrumWidth.value = el.getBoundingClientRect().width
  if (typeof ResizeObserver === 'undefined') return
  spectrumObserver = new ResizeObserver((entries) => {
    spectrumWidth.value = entries[0]?.contentRect?.width || el.getBoundingClientRect().width
  })
  spectrumObserver.observe(el)
}, { flush: 'post' })
const spectrumPointCount = computed(() => {
  if (!spectrumWidth.value) return 36
  return Math.max(10, Math.min(60, Math.floor(spectrumWidth.value / 15)))
})
const batchSpectrum = computed(() => batchSpectrumAll.value.slice(-spectrumPointCount.value))
const smoothTopPath = (values, total = 100) => {
  const n = values.length
  if (!n) return ''
  const y = (value) => total - Math.max(0, Math.min(100, value))
  if (n === 1) return `M 0 ${y(values[0]).toFixed(2)} L 1000 ${y(values[0]).toFixed(2)}`
  const step = 1000 / (n - 1)
  let d = `M 0 ${y(values[0]).toFixed(2)}`
  for (let i = 1; i < n; i += 1) {
    const x0 = (i - 1) * step
    const x1 = i * step
    const y0 = y(values[i - 1])
    const y1 = y(values[i])
    d += ` C ${(x0 + step / 3).toFixed(2)} ${y0.toFixed(2)}, ${(x1 - step / 3).toFixed(2)} ${y1.toFixed(2)}, ${x1.toFixed(2)} ${y1.toFixed(2)}`
  }
  return d
}
const spectrumPaths = computed(() => {
  const bars = batchSpectrum.value
  if (!bars.length) return null
  const topPath = (values) => smoothTopPath(values)
  const area = (top) => `${top} L 1000 100 L 0 100 Z`
  return {
    available: area(topPath(bars.map((bar) => Math.min(100, bar.publishedPct + bar.availablePct)))),
    published: area(topPath(bars.map((bar) => bar.publishedPct))),
  }
})
const spectrumHits = computed(() => {
  const bars = batchSpectrum.value
  const n = bars.length
  if (!n) return []
  return bars.map((bar, index) => {
    const left = n === 1 ? 0 : Math.max(0, (index - 0.5) / (n - 1) * 1000)
    const right = n === 1 ? 1000 : Math.min(1000, (index + 0.5) / (n - 1) * 1000)
    return { key: bar.key, x: left, width: Math.max(0.2, right - left), title: bar.title }
  })
})
const batchSpectrumSummary = computed(() => {
  const bars = batchSpectrum.value
  if (!bars.length) return '暂无历史批次'
  const last = bars[bars.length - 1]
  return `${bars.length} 个历史批次 · 最近 ${last.publishedPct}% 已下发`
})
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
onUnmounted(() => { clearInterval(timer.value); clearInterval(liveTimer.value); spectrumObserver?.disconnect() })
</script>

<template>
  <div class="np-page frostdash-page" v-loading="loading">
    <header v-if="data" class="dash-headline">
      <div class="dash-headline-copy">
        <div class="dash-eyebrow"><b>SCORE ROOM</b><i></i><span>V2.024 · FROST</span></div>
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
      <article class="frostcard scorecard theme-dark span12 scorecard-merged">
        <div class="card-head scorecard-head">
          <div>
            <div class="card-kicker"><i></i>SCORE DECOMPOSITION</div>
            <h2>评分拆解</h2>
            <p class="muted">可用率、下发率与标签合格率的加权展示分；轨道中保留机场分布。</p>
          </div>
          <span class="card-chip">{{ scoredAirports.length }} / {{ enabledAirports.length }} 机场计分</span>
        </div>

        <div class="score-merged-body">
          <div class="score-total">
            <span class="score-total-kicker">OVERALL</span>
            <div class="score-total-num">
              <b>{{ overallScoreText }}</b>
              <em>{{ overallGrade }}</em>
            </div>
            <small>加权展示分</small>
            <div class="score-total-line"></div>
            <div class="score-total-foot">
              <b>{{ qualifiedPct }}%</b>
              <span>合格</span>
              <i>·</i>
              <span>{{ data.score_window_days || 7 }} 天窗口</span>
            </div>
          </div>

          <div class="orbit-wrap score-orbit">
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

          <div class="score-metrics">
            <div class="score-metrics-head"><span>评分构成</span><span>加权结果</span></div>
            <div v-for="item in breakdownRows" :key="item.key" class="score-metric-row" :class="`is-${item.key}`">
              <div class="score-metric-top">
                <b>{{ item.label }}</b>
                <span>{{ item.sub }}</span>
                <em>{{ item.value.toFixed(1) }}%</em>
              </div>
              <div class="score-metric-track">
                <i :style="{ width: `${Math.max(0, Math.min(100, item.value))}%` }" />
              </div>
            </div>
            <div class="score-metrics-note">
              <span>权重来自后端 <b>score_weights</b></span>
              <span>{{ data.score_window_days || 7 }} 天动态采样</span>
            </div>
          </div>
        </div>

        <div class="score-lower">
          <div class="scoreband scoreband-merged">
            <div><span>ENABLED AIRPORTS</span><b>{{ enabledAirports.length }}<small>座</small></b><i>{{ airportOk }}/{{ airportTotal }} 拉取正常</i></div>
            <div><span>POOL TOTAL</span><b>{{ data.nodes_total }}<small>节点</small></b><i>{{ statusCounts.unknown }} 待检测 · {{ statusCounts.unavailable }} 不可用</i></div>
            <div><span>QUALIFIED</span><b>{{ data.nodes_qualified }}<small>合格</small></b><i>{{ qualifiedPct }}% 通过延迟门槛</i></div>
            <div><span>DELIVERED</span><b>{{ publishedTotal }}<small>下发</small></b><i>{{ publishRatio }}% 下发率</i></div>
          </div>

          <div class="grade-summary">
            <div class="grade-summary-head"><span>评分档位分布</span><b>{{ scoredAirports.length }} 座机场</b></div>
            <div v-for="band in gradeBands" :key="band.key" class="grade-summary-row">
              <i :style="{ background: band.color }" />
              <span>{{ band.label }}</span>
              <b>{{ band.count }}</b>
              <em>{{ band.pct }}%</em>
            </div>
          </div>
        </div>

        <div class="score-merged-foot">
          <b>{{ data.nodes_qualified }}</b> / {{ data.nodes_total }} 节点已通过延迟门槛；
          <b>{{ publishedTotal }}</b> 个节点进入统一订阅。
        </div>
      </article>

      <article v-for="chart in trendCharts" :key="chart.key" class="frostcard trendbig span6 trend-split">
        <div class="trend-head">
          <div class="trend-title">
            <div class="k"><i></i>POOL TREND · {{ trendRangeText }}</div>
            <h2>{{ chart.title }}</h2>
            <div class="num"><b>{{ chart.current }}</b><small>{{ chart.label }} · {{ chart.points.length }} 个采样点</small></div>
          </div>
          <div class="trend-tools">
            <div class="seg" role="group" aria-label="趋势区间">
              <button :class="{ active: trendRange === '1d' }" @click="trendRange = '1d'">1D</button>
              <button :class="{ active: trendRange === '7d' }" @click="trendRange = '7d'">7D</button>
              <button :class="{ active: trendRange === '30d' }" @click="trendRange = '30d'">30D</button>
            </div>
          </div>
        </div>
        <div class="trend-chart">
          <TrendChart :points="chart.points" :rows="chart.rows" :field="chart.field"
                      :range="trendRange" :title="''" :palette="AIRPORT_COLORS" />
        </div>
        <div class="trend-foot"><span>{{ trendRangeText }} · {{ trendSampleText }}</span><b>{{ chart.label }} {{ chart.current }} / {{ data.nodes_total }}</b></div>
      </article>

      <article class="frostcard statuscard span7">
        <div class="card-head status-head">
          <div>
            <div class="card-kicker"><i></i>POOL STATUS</div>
            <h2>节点池状态</h2>
            <p class="muted">环图取最近一次检测结果，下方波浪图只统计更早的历史批次。</p>
          </div>
          <span class="card-chip">{{ qualifiedPct }}% 合格</span>
        </div>

        <div class="status-body">
          <section class="status-pane">
            <div class="status-pane-head">
              <span>COMPOSITION</span>
              <b>检测构成</b>
              <em>最近一次 · {{ data.nodes_qualified }} / {{ data.nodes_total }}</em>
            </div>
            <div class="status-comp">
              <div class="status-donut" :class="{ 'is-empty': !donutArcs.length }">
                <svg class="status-donut-svg" viewBox="0 0 120 120" aria-hidden="true">
                  <path v-for="arc in donutArcs" :key="arc.key" :d="arc.d" :fill="arc.color" />
                </svg>
                <div class="status-donut-hole">
                  <b>{{ qualifiedPct }}</b>
                  <span>QUALIFIED</span>
                  <small>{{ data.nodes_qualified }} / {{ data.nodes_total }}</small>
                </div>
              </div>
              <div class="status-legend">
                <div v-for="segment in statusSegments" :key="segment.key" class="status-legend-row">
                  <i :style="{ background: segment.color }" />
                  <span>{{ segment.label }}</span>
                  <b>{{ segment.count }}</b>
                  <em>{{ segment.percent }}%</em>
                </div>
                <div v-if="!statusSegments.length" class="status-legend-row">
                  <i style="background:#c7d7cf" /><span>暂无节点</span><b>0</b><em>0%</em>
                </div>
                <div class="status-legend-foot">砖红 不可用 / 未检测 · 深绿 可用未下发 · 金 已下发</div>
              </div>
            </div>
          </section>

          <section class="status-pane">
            <div class="status-pane-head">
              <span>LIVE TASK</span>
              <b>检测任务</b>
              <em>{{ runStateText }}</em>
            </div>
            <div class="status-gauges">
              <div class="status-gauge">
                <div class="gauge-ring status-gauge-ring" :style="{ background: `conic-gradient(#3f665c 0 ${latencyPct}%, #edf3ef ${latencyPct}% 100%)` }"><b>{{ latencyPct }}<small>%</small></b></div>
                <span>延迟检测</span>
              </div>
              <div class="status-gauge">
                <div class="gauge-ring status-gauge-ring" :style="{ background: `conic-gradient(#d6a41d 0 ${tagPct}%, #edf3ef ${tagPct}% 100%)` }"><b>{{ tagPct }}<small>%</small></b></div>
                <span>标签检测</span>
              </div>
            </div>
            <div class="status-current">
              <template v-if="runtime.running && runtime.current">
                <b :title="runtime.current.node">{{ runtime.current.node }}</b>
                <span>{{ runtime.current.airport || '—' }} · G {{ runtime.current.g ?? '—' }}ms · W {{ runtime.current.w ?? '—' }}ms · {{ runtime.current.result }}</span>
              </template>
              <template v-else>
                <b>调度空闲</b>
                <span>已处理 {{ runtime.done || 0 }} / {{ runtime.total || 0 }} 项 · 批次 #{{ runtime.batch_id || data.batch?.id || '—' }}</span>
              </template>
            </div>
            <div class="status-actions">
              <el-button v-if="!runtime.running" size="small" type="primary" @click="runNow">立即检测</el-button>
              <el-button v-if="runtime.running && !runtime.paused" size="small" :disabled="runtime.stop_requested" @click="pauseCycle">暂停</el-button>
              <el-button v-if="runtime.running && runtime.paused" size="small" type="success" :disabled="runtime.stop_requested" @click="resumeCycle">继续</el-button>
              <el-button v-if="runtime.running" size="small" type="danger" plain :disabled="runtime.stop_requested" @click="stopCycle">{{ runtime.stop_requested ? '正在停止' : '停止检测' }}</el-button>
            </div>
          </section>
        </div>

        <div class="status-batch">
          <div class="status-batch-head">
            <div class="status-batch-id"><b>#{{ data.batch?.id || '—' }}</b><span>{{ data.batch?.processed_count || runtime.done || 0 }} / {{ data.batch?.target_count || data.nodes_total || 0 }} 处理</span><em>最近一次</em></div>
            <div class="status-batch-side"><em>{{ batchRemaining }} 剩余</em><span>{{ batchStatusLabel }}</span></div>
          </div>
          <div class="batch-spectrum-head">
            <div><span>RECENT BATCHES</span><b>历史批次合格率</b><em>{{ batchSpectrumSummary }}</em></div>
            <div class="batch-spectrum-legend">
              <span><i class="is-unavailable" />不可用</span>
              <span><i class="is-available" />可用</span>
              <span><i class="is-published" />已下发</span>
            </div>
          </div>
          <div v-if="batchSpectrum.length && spectrumPaths" class="batch-spectrum">
            <div class="batch-spectrum-axis"><span>100%</span><span>50%</span><span>0</span></div>
            <div ref="spectrumEl" class="batch-spectrum-main">
              <svg class="batch-spectrum-svg" viewBox="0 0 1000 100" preserveAspectRatio="none" role="img" aria-label="历史批次合格率波浪图">
                <defs>
                  <linearGradient id="np-wave-published" x1="0" y1="0" x2="0.12" y2="1">
                    <stop offset="0" stop-color="#f8edcd" />
                    <stop offset=".5" stop-color="#f2dfae" />
                    <stop offset="1" stop-color="#e8cd8b" />
                  </linearGradient>
                  <linearGradient id="np-wave-available" x1="0" y1="0" x2="0.12" y2="1">
                    <stop offset="0" stop-color="#dcebe4" />
                    <stop offset=".5" stop-color="#c0d8cd" />
                    <stop offset="1" stop-color="#a5c4b5" />
                  </linearGradient>
                  <linearGradient id="np-wave-unavailable" x1="0" y1="0" x2="0.12" y2="1">
                    <stop offset="0" stop-color="#f6e8e3" />
                    <stop offset=".5" stop-color="#edcfc4" />
                    <stop offset="1" stop-color="#dfb6a5" />
                  </linearGradient>
                  <linearGradient id="np-wave-sheen" x1="0" y1="0" x2="0.18" y2="1">
                    <stop offset="0" stop-color="#ffffff" stop-opacity=".1" />
                    <stop offset=".3" stop-color="#ffffff" stop-opacity=".03" />
                    <stop offset=".72" stop-color="#ffffff" stop-opacity="0" />
                    <stop offset="1" stop-color="#ffffff" stop-opacity=".03" />
                  </linearGradient>
                </defs>
                <rect x="0" y="0" width="1000" height="100" fill="url(#np-wave-unavailable)" />
                <g class="batch-spectrum-grid">
                  <line x1="0" x2="1000" y1="25" y2="25" />
                  <line x1="0" x2="1000" y1="50" y2="50" />
                  <line x1="0" x2="1000" y1="75" y2="75" />
                </g>
                <path class="is-available" :d="spectrumPaths.available" fill="url(#np-wave-available)" />
                <path class="is-published" :d="spectrumPaths.published" fill="url(#np-wave-published)" />
                <path class="batch-spectrum-glint" :d="spectrumPaths.available" />
                <path class="batch-spectrum-glint" :d="spectrumPaths.published" />
                <rect x="0" y="0" width="1000" height="100" fill="url(#np-wave-sheen)" pointer-events="none" />
                <g class="batch-spectrum-hit">
                  <rect v-for="hit in spectrumHits" :key="hit.key" :x="hit.x" :width="hit.width" y="0" height="100" fill="transparent" pointer-events="all">
                    <title>{{ hit.title }}</title>
                  </rect>
                </g>
              </svg>
              <div class="batch-spectrum-x"><span>{{ batchSpectrum[0].date }}</span><span>{{ batchSpectrum[Math.floor(batchSpectrum.length / 2)].date }}</span><span>{{ batchSpectrum[batchSpectrum.length - 1].date }}</span></div>
            </div>
          </div>
          <div v-else class="batch-empty status-bars-empty">暂无历史批次</div>
        </div>

        <div class="status-foot">
          <span>批次 #{{ data.batch?.id || '—' }} · 耗时 {{ batchDuration }} · 剩余 {{ batchRemaining }}</span>
          <span>{{ trendRangeText }} · {{ trendSampleText }}</span>
        </div>
      </article>

      <article class="frostcard modecard span5">
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
            <p>最后一次检测的可用、下发、标签与不可用；成熟度和评分在右侧。</p>
          </div>
          <div class="chips">
            <span class="card-chip">评分 = {{ scoreWeights.available }} / {{ scoreWeights.publish }} / {{ scoreWeights.tag }}</span>
            <span class="card-chip">近 {{ data.score_window_days || 7 }} 天</span>
          </div>
        </div>

        <div v-if="data.airport_list?.length" class="airport-table-wrap">
          <table class="np-score-table console-score-table status-score-table">
            <colgroup>
              <col class="np-score-col-rank" />
              <col class="np-score-col-airport" />
              <col />
              <col class="np-score-col-status" />
              <col class="np-score-col-score" />
            </colgroup>
            <thead>
              <tr>
                <th>#</th>
                <th class="is-left">机场资产</th>
                <th class="is-state">状态评分 <i>最后一次检测 · 绿可用 / 金下发 / 红不可用</i></th>
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
                <td class="np-score-cell is-state">
                  <div v-if="row.enabled && row.pool_count" class="state-score">
                    <div class="state-rail" :title="`可用 ${rateText(row, 'available_rate')} · 下发 ${rateText(row, 'publish_rate')} · 不可用 ${unavailableText(row)}`">
                      <div class="state-track">
                        <i class="state-fill is-available" :style="{ width: metricWidth(row, 'available_rate') + '%' }" />
                        <i class="state-fill is-unavailable" :style="{ left: metricWidth(row, 'available_rate') + '%', width: (unavailableRate(row) || 0) + '%' }" />
                        <i class="state-fill is-publish" :style="{ width: metricWidth(row, 'publish_rate') + '%' }" />
                      </div>
                      <i class="state-pin is-publish" :style="{ left: metricWidth(row, 'publish_rate') + '%' }" />
                    </div>
                    <div class="state-metrics">
                      <span class="state-metric is-available"><i /><em>可用</em><b>{{ rateText(row, 'available_rate') }}</b></span>
                      <span class="state-metric is-publish"><i /><em>下发</em><b>{{ rateText(row, 'publish_rate') }}</b></span>
                      <span class="state-metric is-tag"><i /><em>标签</em><b>{{ rateText(row, 'tag_rate') }}</b></span>
                      <span class="state-metric is-unavailable"><i /><em>不可用</em><b>{{ unavailableText(row) }}</b></span>
                    </div>
                  </div>
                  <div v-else class="state-empty">未启用或缓存池为空</div>
                </td>
                <td class="np-score-cell is-status" :title="statusHint(row)">
                  <div class="np-data-status" :class="[statusTone(row), maturityTone(row)]"><b>{{ row.data_status_text || '—' }}<em v-if="row.data_status_detail">{{ row.data_status_detail }}</em></b></div>
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
                <span class="amc-status" :class="[statusTone(row), maturityTone(row)]">{{ row.data_status_text || '—' }}</span>
              </div>
              <div class="np-pool-score amc-score" :class="scoreTone(row)">
                <strong>{{ row.enabled && row.score !== null ? row.score : '—' }}</strong>
                <em>{{ row.score_grade }}</em>
              </div>
            </div>
            <div class="amc-sub">{{ row.enabled ? `${row.pool_count} 节点` : `${row.node_count} 缓存` }} · {{ scoreSampleText(row) }}</div>
            <div class="amc-state">
              <div v-if="row.enabled && row.pool_count" class="state-rail" :title="`可用 ${rateText(row, 'available_rate')} · 下发 ${rateText(row, 'publish_rate')} · 不可用 ${unavailableText(row)}`">
                <div class="state-track">
                  <i class="state-fill is-available" :style="{ width: metricWidth(row, 'available_rate') + '%' }" />
                  <i class="state-fill is-unavailable" :style="{ left: metricWidth(row, 'available_rate') + '%', width: (unavailableRate(row) || 0) + '%' }" />
                  <i class="state-fill is-publish" :style="{ width: metricWidth(row, 'publish_rate') + '%' }" />
                </div>
                <i class="state-pin is-publish" :style="{ left: metricWidth(row, 'publish_rate') + '%' }" />
              </div>
              <div v-if="row.enabled && row.pool_count" class="state-metrics is-compact">
                <span class="state-metric is-available"><i /><em>可用</em><b>{{ rateText(row, 'available_rate') }}</b></span>
                <span class="state-metric is-publish"><i /><em>下发</em><b>{{ rateText(row, 'publish_rate') }}</b></span>
                <span class="state-metric is-tag"><i /><em>标签</em><b>{{ rateText(row, 'tag_rate') }}</b></span>
                <span class="state-metric is-unavailable"><i /><em>不可用</em><b>{{ unavailableText(row) }}</b></span>
              </div>
              <div v-else class="state-empty">未启用或缓存池为空</div>
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
