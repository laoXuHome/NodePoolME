<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs, logLevelLabel, logSourceLabel } from '../api'

const data = ref(null)
const AIRPORT_COLORS = ['#2563eb', '#ea580c', '#16a34a', '#db2777', '#7c3aed', '#0891b2', '#ca8a04', '#be185d']
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

const publishTotal = computed(() => chartTotal(data.value?.airport_list || [], 'published_count'))
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
    available: weights.available ?? 45,
    publish: weights.publish ?? 45,
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

function scoreTone(row) {
  if (!row.enabled) return 'is-off'
  if (row.score === null || row.score === undefined) return 'is-empty'
  if (row.score >= 80) return 'is-good'
  if (row.score >= 65) return 'is-ok'
  if (row.score >= 50) return 'is-mid'
  return 'is-low'
}
function scoreHint(row) {
  const pool = row.pool_count || 0
  if (!row.enabled) return '机场未启用，不参与评分'
  if (!pool) return '缓存池为空，暂不评分'
  const health = row.available_rate ?? Math.round((row.available_count || 0) / pool * 100)
  const publish = row.publish_rate ?? Math.round((row.published_count || 0) / pool * 100)
  const share = row.share_rate ?? Math.round(pool / (data.value?.nodes_total || 1) * 100)
  const tag = row.tag_rate ?? Math.round((row.tag_ok_count || 0) / pool * 100)
  const basis = row.score_samples ? `近 7 天 ${row.score_samples} 个完整检测快照` : '无完整检测快照，按当前值'
  return `${basis} · 可用率 ${health}%（×${scoreWeights.value.available}%）· 下发率 ${publish}%（×${scoreWeights.value.publish}%）· 池占比 ${share}%（×${scoreWeights.value.share}%）· 标签合格率 ${tag}%（×${scoreWeights.value.tag}%）→ 综合 ${row.score} 分`
}
function scoreSampleText(row) {
  if (!row.enabled) return '未启用'
  if (!row.pool_count) return '空池'
  return row.score_samples ? `${row.score_samples} 个完整快照` : '按当前值'
}
function rateText(row, key) {
  if (!row.enabled || row[key] === null || row[key] === undefined) return '—'
  return `${Number(row[key]).toFixed(1)}%`
}
const rankedAirports = computed(() => {
  return [...(data.value?.airport_list || [])].sort((a, b) => {
    if (!!a.enabled !== !!b.enabled) return a.enabled ? -1 : 1
    const sa = Number.isFinite(a.score) ? a.score : -1
    const sb = Number.isFinite(b.score) ? b.score : -1
    if (sb !== sa) return sb - sa
    return (b.pool_count || 0) - (a.pool_count || 0)
  })
})

const trendRange = ref('1d')
const allHistoryPoints = computed(() => Array.isArray(data.value?.share_history) ? data.value.share_history : [])
function trendPointTime(point) {
  if (point?.live) return Date.now()
  const time = new Date(String(point?.ts || '').replace(' ', 'T')).getTime()
  return Number.isNaN(time) ? 0 : time
}
const trendWindowPoints = computed(() => {
  const all = allHistoryPoints.value
  if (!all.length) return []
  const days = trendRange.value === '7d' ? 7 : 1
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
const trendRangeText = computed(() => trendRange.value === '7d' ? '近 7 天' : '近 1 天')
const TREND = { w: 560, h: 148, left: 26, right: 52, top: 12, bottom: 18 }
function trendDomain(points = trendWindowPoints.value) {
  const days = trendRange.value === '7d' ? 7 : 1
  const end = Date.now()
  let start = end - days * 86400000
  if (days === 7) {
    const first = points[0]
    const firstTime = first ? trendPointTime(first) : 0
    if (firstTime && firstTime > start) start = firstTime
  }
  return { start, end }
}
function trendChartPoints(points, key) {
  if (trendRange.value !== '7d' || !points.length) return points
  const totalKey = key === 'available_count' ? 'total_available' : 'total_published'
  const first = points.findIndex((point) => Number(point[totalKey] || 0) > 0)
  return first > 0 ? points.slice(first) : points
}
function trendPointX(point, domain) {
  const plotW = TREND.w - TREND.left - TREND.right
  if (point?.live) return TREND.left + plotW
  const t = trendPointTime(point)
  const ratio = Math.min(1, Math.max(0, (t - domain.start) / (domain.end - domain.start)))
  return TREND.left + ratio * plotW
}
function trendCount(point, row, key) {
  return point.airports?.[String(row.id)]?.[key] || 0
}
function trendShare(point, row, key) {
  const total = key === 'available_count' ? point.total_available
    : key === 'pool_count' ? point.total_nodes : point.total_published
  return total ? trendCount(point, row, key) / total * 100 : 0
}
function trendMax(key, points = historyPoints.value) {
  const rows = data.value?.airport_list || []
  let max = 0
  for (const point of points) {
    for (const row of rows) {
      if (!row.enabled) continue
      max = Math.max(max, trendShare(point, row, key))
    }
  }
  return Math.max(20, Math.ceil(max / 10) * 10)
}
const availableTrendMax = computed(() => trendMax('available_count', availableTrendPoints.value))
const publishTrendMax = computed(() => trendMax('published_count'))
function trendXY(key, row, index, max, points = historyPoints.value) {
  const plotH = TREND.h - TREND.top - TREND.bottom
  const x = trendPointX(points[index], trendDomain(points))
  const q = TREND.top + (1 - Math.min(max, trendCount(points[index], row, key)) / Math.max(1, max)) * plotH
  return { x, y: q }
}
function trendPath(key, row, max, source = historyPoints.value) {
  const points = source.map((_, index) => trendXY(key, row, index, max, source))
  if (!points.length) return ''
  if (points.length === 1) return `M${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`
  // Monotone cubic Hermite: smooth through every snapshot without overshooting sharp peaks.
  const spans = []
  const slopes = []
  for (let i = 0; i < points.length - 1; i += 1) {
    const h = points[i + 1].x - points[i].x || 1
    spans.push(h)
    slopes.push((points[i + 1].y - points[i].y) / h)
  }
  const tangents = points.map((_, i) => {
    if (i === 0) return slopes[0]
    if (i === points.length - 1) return slopes[slopes.length - 1]
    const prev = slopes[i - 1]
    const next = slopes[i]
    if (prev * next <= 0) return 0
    const hPrev = spans[i - 1]
    const hNext = spans[i]
    const w1 = 2 * hNext + hPrev
    const w2 = hNext + 2 * hPrev
    return (w1 + w2) / (w1 / prev + w2 / next)
  })
  let d = `M${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`
  for (let i = 0; i < points.length - 1; i += 1) {
    const h = spans[i]
    const c1x = points[i].x + h / 3
    const c1y = points[i].y + tangents[i] * h / 3
    const c2x = points[i + 1].x - h / 3
    const c2y = points[i + 1].y - tangents[i + 1] * h / 3
    d += ` C${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${points[i + 1].x.toFixed(1)} ${points[i + 1].y.toFixed(1)}`
  }
  return d
}
function trendArea(key, row, max, source = historyPoints.value) {
  const points = source.map((_, index) => trendXY(key, row, index, max, source))
  if (!points.length) return ''
  const base = TREND.h - TREND.bottom
  const first = points[0]
  const last = points[points.length - 1]
  const middle = points.map((p) => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')
  return `M${first.x.toFixed(1)} ${base.toFixed(1)} L${first.x.toFixed(1)} ${first.y.toFixed(1)} ${middle} L${last.x.toFixed(1)} ${base.toFixed(1)} Z`
}
function trendDot(key, row, max, source = historyPoints.value) {
  const last = source.length - 1
  if (last < 0) return { x: 0, y: 0, v: 0 }
  const p = trendXY(key, row, last, max, source)
  return { ...p, v: trendCount(source[last], row, key) }
}
function trendEndLabels(key, max, source = historyPoints.value) {
  const labels = (data.value?.airport_list || [])
    .filter((row) => row.enabled && trendHasValue(key, row, source))
    .map((row) => {
      const dot = trendDot(key, row, max, source)
      const current = source[source.length - 1]
      const count = trendCount(current, row, key)
      const value = trendShare(current, row, key)
      return { id: row.id, name: row.name, color: airportColor(row), y: dot.y,
               dotY: dot.y, count, value,
               pct: Math.round(value) }
    })
    .sort((a, b) => a.dotY - b.dotY)
  const ranked = [...labels].sort((a, b) => b.value - a.value || b.count - a.count || a.id - b.id)
  ranked.forEach((label, index) => { label.rank = index + 1 })
  const gap = 9.5
  const top = TREND.top + 4.6
  const bottom = TREND.h - TREND.bottom - 4
  const groups = []
  for (const label of labels) {
    const group = groups[groups.length - 1]
    if (group && label.dotY - group[group.length - 1].dotY < gap) group.push(label)
    else groups.push([label])
  }
  for (const group of groups) {
    if (group.length < 2) continue
    const span = (group.length - 1) * gap
    const center = (group[0].dotY + group[group.length - 1].dotY) / 2
    const start = Math.max(top, Math.min(bottom - span, center - span / 2))
    group.forEach((label, index) => { label.y = start + index * gap })
  }
  // Final collision pass: even unrelated groups must never overlap vertically.
  let cursor = top
  for (const label of labels) {
    label.y = Math.max(label.y, cursor)
    cursor = label.y + gap
  }
  const overflow = labels.length && labels[labels.length - 1].y > bottom
    ? labels[labels.length - 1].y - bottom : 0
  if (overflow > 0) {
    cursor = top
    for (const label of labels) {
      label.y = Math.max(top, label.y - overflow)
      label.y = Math.max(label.y, cursor)
      cursor = label.y + gap
    }
  }
  for (const label of labels) label.shifted = Math.abs(label.y - label.dotY) > .8
  return labels
}
function trendXLabels(points = historyPoints.value, key = 'published_count') {
  const domain = trendDomain(points)
  const count = 4
  const plotW = TREND.w - TREND.left - TREND.right
  return Array.from({ length: count }, (_, index) => {
    const last = index === count - 1
    const time = domain.start + (domain.end - domain.start) * index / (count - 1)
    const d = new Date(time)
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    let text = `${d.getMonth() + 1}/${d.getDate()}`
    if (!last && trendRange.value !== '7d') text = index === 0 ? `昨天 ${hm}` : hm
    if (!last && trendRange.value === '7d' && (domain.end - domain.start) < 3 * 86400000) text = `${text} ${hm}`
    return { x: TREND.left + plotW * index / (count - 1), text: last ? '当前' : text,
             anchor: last ? 'end' : index === 0 ? 'start' : 'middle' }
  })
}
function trendHasValue(key, row, source = historyPoints.value) {
  return source.some((point) => trendCount(point, row, key) > 0)
}
function trendPointLabel(point) {
  if (point.live) return '当前'
  const d = new Date(String(point.ts).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return ''
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (trendRange.value === '7d') return `${d.getMonth() + 1}/${d.getDate()}`
  return hm
}
function trendTicks(max) {
  const plotH = TREND.h - TREND.top - TREND.bottom
  const step = max <= 20 ? 5 : max <= 50 ? 10 : 20
  const values = []
  for (let v = 0; v <= max; v += step) values.push(v)
  if (values[values.length - 1] !== max) values.push(max)
  return values.map((v) => ({ v, y: TREND.top + (1 - v / Math.max(1, max)) * plotH }))
}
const trendCharts = computed(() => [
  {
    key: 'publish',
    title: '下发节点趋势',
    totalLabel: '已下发',
    totalCount: publishTotal.value,
    field: 'published_count',
    rows: publishChartRows.value,
    max: Math.max(1, publishTotal.value),
    points: trendChartPoints(historyPoints.value, 'published_count'),
  },
  {
    key: 'available',
    title: '可用节点趋势',
    totalLabel: '可用',
    totalCount: data.value?.nodes_available || 0,
    field: 'available_count',
    rows: nodeChartRows.value,
    max: Math.max(1, data.value?.nodes_available || 0),
    points: trendChartPoints(availableTrendPoints.value, 'available_count'),
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
function rankRows(rows, key) {
  return [...(rows || [])].sort((a, b) => {
    if (!!a.enabled !== !!b.enabled) return a.enabled ? -1 : 1
    const diff = chartAmount(b, key) - chartAmount(a, key)
    if (diff) return diff
    return (b.pool_count || 0) - (a.pool_count || 0)
  })
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
      <!-- 缓存池 + 机场拉取：合并成主统计卡 -->
      <div class="np-card np-dash-pool">
        <div class="np-section-title">
          <span class="np-dot" :class="(data.fetch?.error || data.fetch?.fetching) ? 'is-warning' : 'is-success'" />
          缓存池
          <el-tag :type="fetchState.type" size="small" effect="plain">
            {{ data.fetch?.fetching ? fetchState.text : data.fetch?.error ? `${data.fetch.error} 个机场失败` : `${data.fetch?.ok || 0}/${data.fetch?.total || 0} 机场正常` }}
          </el-tag>
        </div>

        <div class="np-pool-overview">
          <div class="np-pool-status">
            <div class="np-pool-status-title">缓存池状态</div>
            <div class="np-pool-status-total"><strong>{{ data.nodes_total }}</strong><span>节点</span></div>
            <div class="np-pool-stack-wrap" :style="{ '--stack-top': statusTopColor }">
              <div class="np-pool-stack"
                   :title="`下发 ${statusCounts.qualified} · 未达标 ${statusCounts.qualified ? statusCounts.available - statusCounts.qualified : statusCounts.available} · 不可用 ${statusCounts.unavailable}`">
                <div v-for="segment in statusSegments" :key="segment.key"
                     class="np-pool-stack-seg" :class="{ 'is-thin': segment.height < 15 }"
                     :style="{ height: `${segment.height}%`, background: `linear-gradient(90deg, ${segment.side}, ${segment.color} 38%, ${segment.light} 70%, ${segment.color})` }">
                  <div class="np-pool-stack-label" :style="{ color: segment.ink, textShadow: segment.ink === '#ffffff' ? '0 1px 2px rgba(0,0,0,.3)' : 'none' }"
                       :title="`${segment.label} ${segment.count} 个 · ${segment.percent}%`">
                    <em>{{ segment.short }}</em><b>{{ segment.count }}</b><i>{{ segment.percent }}%</i>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="np-pool-score-panel">
        <div class="np-score-head">
          <div>
            <b>机场评价</b>
            <span>近 {{ data.score_window_days || 7 }} 天数据</span>
          </div>
          <span>评分降序</span>
        </div>

        <div v-if="data.airport_list?.length" class="np-score-table-wrap">
          <table class="np-score-table">
            <colgroup>
              <col class="np-score-col-rank" />
              <col class="np-score-col-name" />
              <col class="np-score-col-rate" />
              <col class="np-score-col-rate" />
              <col class="np-score-col-rate" />
              <col class="np-score-col-rate" />
              <col class="np-score-col-score" />
            </colgroup>
            <thead>
              <tr>
                <th>#</th>
                <th class="is-left">机场</th>
                <th>可用率 <i>{{ scoreWeights.available }}%</i></th>
                <th>下发率 <i>{{ scoreWeights.publish }}%</i></th>
                <th>池占比 <i>{{ scoreWeights.share }}%</i></th>
                <th>标签合格率 <i>{{ scoreWeights.tag }}%</i></th>
                <th class="is-score">评分</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, idx) in rankedAirports" :key="row.id" :class="{ 'is-off': !row.enabled }">
                <td class="np-score-rank" :class="{ 'is-off': !row.enabled }">{{ idx + 1 }}</td>
                <td class="np-score-airport">
                  <div class="np-score-airport-name">
                    <span class="np-airport-dot" :class="!row.enabled ? 'is-off' : `is-${row.last_status || 'none'}`" :title="airportStateText(row)" />
                    <b>{{ row.name }}</b>
                    <span v-if="!row.enabled" class="np-pool-off">未启用</span>
                  </div>
                  <small>{{ row.enabled ? `${row.pool_count} 节点` : `${row.node_count} 缓存` }} · {{ scoreSampleText(row) }}</small>
                </td>
                <td class="np-score-cell">
                  <b>{{ rateText(row, 'available_rate') }}</b>
                  <small>{{ row.enabled ? '均值' : '—' }}</small>
                </td>
                <td class="np-score-cell">
                  <b>{{ rateText(row, 'publish_rate') }}</b>
                  <small>{{ row.enabled ? '均值' : '—' }}</small>
                </td>
                <td class="np-score-cell">
                  <b>{{ rateText(row, 'share_rate') }}</b>
                  <small>{{ row.enabled ? '均值' : '—' }}</small>
                </td>
                <td class="np-score-cell">
                  <b>{{ rateText(row, 'tag_rate') }}</b>
                  <small>{{ row.enabled ? `${row.tag_ok_count || 0}/${row.pool_count}` : '—' }}</small>
                </td>
                <td class="np-score-cell is-score" :title="scoreHint(row)">
                  <div class="np-pool-score" :class="scoreTone(row)">
                    <strong>{{ row.enabled && row.score !== null ? row.score : '—' }}</strong><em>{{ row.score_grade }}</em>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="data.airport_list?.length" class="np-score-foot">
          <span>评分 = 可用率 {{ scoreWeights.available }}% + 下发率 {{ scoreWeights.publish }}% + 池占比 {{ scoreWeights.share }}% + 标签合格率 {{ scoreWeights.tag }}%；前 3 项取近 {{ data.score_window_days || 7 }} 天完整检测快照均值</span>
          <span>标签合格率 = 近 7 天任一启用标签通过 / 缓存节点；暂停、中断和单节点测试不计入</span>
        </div>
        </div>

        <div v-if="data.airport_list?.length" class="np-pool-charts">
            <div class="np-trend-toolbar">
              <span>节点趋势 <em>{{ trendRangeText }} · {{ trendSampleText }}</em></span>
              <div class="np-trend-range" role="group" aria-label="趋势区间">
                <button type="button" :class="{ 'is-active': trendRange === '1d' }" @click="trendRange = '1d'">近 1 天</button>
                <button type="button" :class="{ 'is-active': trendRange === '7d' }" @click="trendRange = '7d'">近 7 天</button>
              </div>
            </div>
            <div v-for="chart in trendCharts" :key="chart.key" class="np-pool-chart">
              <div class="np-pool-chart-head">
                <b>{{ chart.title }}</b>
                <span class="np-pool-chart-total">当前{{ chart.totalLabel }} <b>{{ chart.totalCount }}</b></span>
              </div>
              <div class="np-pool-chart-body">
                <div class="np-trend">
                  <svg :viewBox="`0 0 ${TREND.w} ${TREND.h}`" class="np-trend-svg">
                    <line v-for="tick in trendTicks(chart.max)" :key="`${chart.key}-grid-${tick.v}`"
                          class="np-trend-grid" :x1="TREND.left" :x2="TREND.w - TREND.right"
                          :y1="tick.y" :y2="tick.y" />
                    <text v-for="tick in trendTicks(chart.max)" :key="`${chart.key}-tick-${tick.v}`"
                          class="np-trend-tick" x="2" :y="tick.y + 3" text-anchor="start">{{ tick.v }}</text>
                    <template v-for="row in chart.rows" :key="`${chart.key}-line-${row.id}`">
                      <g v-if="row.enabled && trendHasValue(chart.field, row, chart.points)">
                        <path :d="trendArea(chart.field, row, chart.max, chart.points)"
                              :fill="airportColor(row)" class="np-trend-area" />
                        <path :d="trendPath(chart.field, row, chart.max, chart.points)"
                              :stroke="airportColor(row)" class="np-trend-line" />
                        <circle v-if="chart.points.length"
                                :cx="trendXY(chart.field, row, chart.points.length - 1, chart.max, chart.points).x"
                                :cy="trendXY(chart.field, row, chart.points.length - 1, chart.max, chart.points).y"
                                r="2.8" :fill="airportColor(row)" class="np-trend-dot" />
                      </g>
                    </template>
                    <g v-for="label in trendEndLabels(chart.field, chart.max, chart.points)" :key="`${chart.key}-label-${label.id}`">
                      <line v-if="label.shifted" :x1="TREND.w - TREND.right" :y1="label.dotY" :x2="TREND.w - TREND.right + 6" :y2="label.y" :stroke="label.color" class="np-trend-leader" />
                      <foreignObject :x="TREND.w - TREND.right + 2" :y="label.y - 4.5" :width="TREND.right - 2" height="9">
                        <span xmlns="http://www.w3.org/1999/xhtml" class="np-trend-pill" :style="{ background: label.color }"><span class="np-trend-rank">{{ label.rank <= 3 ? ['🏆','🥈','🥉'][label.rank - 1] : String(label.rank).padStart(2, '0') }}</span><span>{{ label.name }}</span><span class="np-trend-pct">{{ label.pct }}%</span><span>ˑ{{ label.count }}</span></span>
                      </foreignObject>
                    </g>
                    <text v-for="label in trendXLabels(chart.points, chart.field)" :key="`${chart.key}-x-${label.x}`"
                          class="np-trend-axis" :x="label.x" :y="TREND.h - 3" :text-anchor="label.anchor">{{ label.text }}</text>
                  </svg>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="!data.airport_list?.length" class="np-empty">缓存池为空，先到「机场管理」添加并拉取订阅。</div>

      </div>

      <div class="np-dash-side">
      <!-- 检测任务 + 最近批次：合并成一块实时状态 -->
      <div class="np-card np-dash-detect">
        <div class="np-detect-head">
          <div class="np-detect-title">
            检测任务
            <el-tag v-if="data?.runtime.running" :type="runTagType" size="small">{{ runStateText }}</el-tag>
            <el-tag v-else type="success" size="small">空闲</el-tag>
          </div>
          <div class="np-detect-inline">
            <span>引擎 <b>{{ data.runtime.engine }}</b></span>
            <span>上次启动 {{ shortTs(data.runtime.last_cycle_at || data.batch?.started_at) }}</span>
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

      <div class="np-card np-log-card">
        <div class="np-section-title np-log-head">
          <span class="np-log-title">系统日志</span>
          <div class="np-log-tools">
            <el-select v-model="logLevel" size="small" style="width:92px" @change="loadLogs">
              <el-option label="全部级别" value="ALL" />
              <el-option label="信息" value="INFO" />
              <el-option label="警告" value="WARN" />
              <el-option label="错误" value="ERROR" />
              <el-option label="调试" value="DEBUG" />
            </el-select>
            <span class="np-muted">{{ logs.length }}/300 · 自动刷新</span>
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
    </div>
  </div>
</template>
