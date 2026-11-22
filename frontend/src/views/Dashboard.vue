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
    { key: 'available', label: '可用', short: '可用', count: availableOnly, color: '#3f8cff', side: '#2563c9', light: '#7db4ff', ink: '#ffffff' },
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
  const health = Math.round((row.available_count || 0) / pool * 100)
  const publish = Math.round((row.published_count || 0) / pool * 100)
  const share = Math.round(pool / (data.value?.nodes_total || 1) * 100)
  const unknown = row.unknown_count || 0
  return `可用率 ${health}% · 下发率 ${publish}% · 池占比 ${share}%${unknown ? ` · 未检测 ${unknown}` : ''} → 综合 ${row.score} 分`
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

const historyPoints = computed(() => Array.isArray(data.value?.share_history) ? data.value.share_history : [])
const TREND = { w: 336, h: 148, left: 30, right: 72, top: 12, bottom: 18 }
function trendCount(point, row, key) {
  return point.airports?.[String(row.id)]?.[key] || 0
}
function trendShare(point, row, key) {
  const total = key === 'available_count' ? point.total_available
    : key === 'pool_count' ? point.total_nodes : point.total_published
  return total ? trendCount(point, row, key) / total * 100 : 0
}
function trendMax(key) {
  const rows = data.value?.airport_list || []
  let max = 0
  for (const point of historyPoints.value) {
    for (const row of rows) {
      if (!row.enabled) continue
      max = Math.max(max, trendShare(point, row, key))
    }
  }
  return Math.max(20, Math.ceil(max / 10) * 10)
}
const availableTrendMax = computed(() => trendMax('available_count'))
const publishTrendMax = computed(() => trendMax('published_count'))
function trendXY(key, row, index, max) {
  const n = historyPoints.value.length
  const plotW = TREND.w - TREND.left - TREND.right
  const plotH = TREND.h - TREND.top - TREND.bottom
  const x = n <= 1 ? TREND.left + plotW / 2 : TREND.left + index / (n - 1) * plotW
  const q = TREND.top + (1 - Math.min(100, trendShare(historyPoints.value[index], row, key)) / max) * plotH
  return { x, y: q }
}
function trendPath(key, row, max) {
  const points = historyPoints.value.map((_, index) => trendXY(key, row, index, max))
  if (!points.length) return ''
  if (points.length === 1) return `M${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`
  let d = `M${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`
  for (let i = 1; i < points.length; i += 1) {
    const prev = points[i - 1]
    const cur = points[i]
    const mid = (prev.x + cur.x) / 2
    d += ` C${mid.toFixed(1)} ${prev.y.toFixed(1)}, ${mid.toFixed(1)} ${cur.y.toFixed(1)}, ${cur.x.toFixed(1)} ${cur.y.toFixed(1)}`
  }
  return d
}
function trendArea(key, row, max) {
  const points = historyPoints.value.map((_, index) => trendXY(key, row, index, max))
  if (!points.length) return ''
  const base = TREND.h - TREND.bottom
  const first = points[0]
  const last = points[points.length - 1]
  const middle = points.map((p) => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ')
  return `M${first.x.toFixed(1)} ${base.toFixed(1)} L${first.x.toFixed(1)} ${first.y.toFixed(1)} ${middle} L${last.x.toFixed(1)} ${base.toFixed(1)} Z`
}
function trendDot(key, row, max) {
  const last = historyPoints.value.length - 1
  if (last < 0) return { x: 0, y: 0, v: 0 }
  const p = trendXY(key, row, last, max)
  return { ...p, v: trendCount(historyPoints.value[last], row, key) }
}
function trendLabelWidth(row, value) {
  const nameWidth = [...row.name].reduce((sum, ch) => sum + (ch.charCodeAt(0) > 127 ? 9 : 5), 0)
  const valueWidth = String(Math.round(value)).length * 4.8 + 8
  return Math.max(38, Math.min(64, Math.round(nameWidth + valueWidth + 7)))
}
function trendEndLabels(key, max) {
  const labels = (data.value?.airport_list || [])
    .filter((row) => row.enabled && trendHasValue(key, row))
    .map((row) => {
      const dot = trendDot(key, row, max)
      return { id: row.id, name: row.name, color: airportColor(row), y: dot.y,
               pct: Math.round(trendShare(historyPoints.value[historyPoints.value.length - 1], row, key)),
               width: trendLabelWidth(row, trendShare(historyPoints.value[historyPoints.value.length - 1], row, key)) }
    })
    .sort((a, b) => a.y - b.y)
  let cursor = TREND.top + 6
  for (const label of labels) {
    label.y = Math.max(label.y, cursor)
    cursor = label.y + 14
  }
  const bottom = TREND.h - TREND.bottom - 2
  const overflow = labels.length ? labels[labels.length - 1].y - bottom : 0
  if (overflow > 0) labels.forEach((label) => { label.y -= overflow })
  return labels
}
function trendXLabels() {
  const n = historyPoints.value.length
  if (!n) return []
  if (n === 1) return [{ x: TREND.left + (TREND.w - TREND.left - TREND.right) / 2, text: '当前', anchor: 'middle' }]
  const count = n >= 4 ? 4 : n
  const indexes = [...new Set(Array.from({ length: count }, (_, i) => Math.round(i * (n - 1) / (count - 1))) )]
  return indexes.map((index) => {
    const x = TREND.left + index / (n - 1) * (TREND.w - TREND.left - TREND.right)
    const last = index === n - 1
    return { x, text: last ? '当前' : trendPointLabel(historyPoints.value[index]), anchor: last ? 'end' : index === 0 ? 'start' : 'middle' }
  })
}
function trendHasValue(key, row) {
  return historyPoints.value.some((point) => trendCount(point, row, key) > 0)
}
function trendPointLabel(point) {
  if (point.live) return '当前'
  const d = new Date(String(point.ts).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return ''
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function trendTicks(max) {
  const plotH = TREND.h - TREND.top - TREND.bottom
  const step = max <= 20 ? 5 : max <= 50 ? 10 : 20
  const values = []
  for (let v = 0; v <= max; v += step) values.push(v)
  if (values[values.length - 1] !== max) values.push(max)
  return values.map((v) => ({ v, y: TREND.top + (1 - v / max) * plotH }))
}
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
            <div class="np-pool-status-sub" title="可用包含下发达标节点">
              <span>可用 {{ statusCounts.available }}</span><i>·</i><span>不可用 {{ statusCounts.unavailable }}</span>
              <template v-if="statusCounts.unknown"><i>·</i><span>未检测 {{ statusCounts.unknown }}</span></template>
            </div>
            <div class="np-pool-stack-wrap" :style="{ '--stack-top': statusTopColor }">
              <div class="np-pool-stack"
                   :title="`下发 ${statusCounts.qualified} · 可用（未达标）${statusCounts.qualified ? statusCounts.available - statusCounts.qualified : statusCounts.available} · 不可用 ${statusCounts.unavailable}`">
                <div v-for="segment in statusSegments" :key="segment.key"
                     class="np-pool-stack-seg" :class="{ 'is-thin': segment.height < 9 }"
                     :style="{ height: `${segment.height}%`, background: `linear-gradient(90deg, ${segment.side}, ${segment.color} 38%, ${segment.light} 70%, ${segment.color})` }">
                  <div class="np-pool-stack-label" :style="{ color: segment.ink, textShadow: segment.ink === '#ffffff' ? '0 1px 2px rgba(0,0,0,.3)' : 'none' }"
                       :title="`${segment.label} ${segment.count} 个 · ${segment.percent}%`">
                    <em>{{ segment.short }}</em><b>{{ segment.count }}</b><i>{{ segment.percent }}%</i>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div v-if="data.airport_list?.length" class="np-pool-charts">
            <div class="np-pool-chart">
              <div class="np-pool-chart-head">
                <b>可用占比趋势</b>
                <span>{{ data.nodes_available }} 可用</span>
              </div>
              <div class="np-pool-chart-body">
                <div class="np-trend">
                  <svg :viewBox="`0 0 ${TREND.w} ${TREND.h}`" class="np-trend-svg">
                    <line v-for="tick in trendTicks(availableTrendMax)" :key="`node-grid-${tick.v}`"
                          class="np-trend-grid" :x1="TREND.left" :x2="TREND.w - TREND.right"
                          :y1="tick.y" :y2="tick.y" />
                    <text v-for="tick in trendTicks(availableTrendMax)" :key="`node-tick-${tick.v}`"
                          class="np-trend-tick" :x="TREND.left - 5" :y="tick.y + 3" text-anchor="end">{{ tick.v }}</text>
                    <template v-for="row in nodeChartRows" :key="`node-line-${row.id}`">
                      <g v-if="row.enabled && trendHasValue('available_count', row)">
                        <path :d="trendArea('available_count', row, availableTrendMax)"
                              :fill="airportColor(row)" class="np-trend-area" />
                        <path :d="trendPath('available_count', row, availableTrendMax)"
                              :stroke="airportColor(row)" class="np-trend-line" />
                        <circle v-for="(point, index) in historyPoints" :key="`node-dot-${row.id}-${index}`"
                                :cx="trendXY('available_count', row, index, availableTrendMax).x"
                                :cy="trendXY('available_count', row, index, availableTrendMax).y"
                                r="2.4" :fill="airportColor(row)" class="np-trend-dot" />
                      </g>
                    </template>
                    <g v-for="label in trendEndLabels('available_count', availableTrendMax)" :key="`node-label-${label.id}`">
                      <rect :x="TREND.w - TREND.right + 4" :y="label.y - 6.25" :width="label.width" height="12.5" rx="3" :fill="label.color" />
                      <text class="np-trend-label" :x="TREND.w - TREND.right + 8" :y="label.y + 3.4">{{ label.name }} {{ label.pct }}%</text>
                    </g>
                    <text v-for="label in trendXLabels()" :key="`node-x-${label.x}`"
                          class="np-trend-axis" :x="label.x" :y="TREND.h - 3" :text-anchor="label.anchor">{{ label.text }}</text>
                  </svg>
                </div>
              </div>
            </div>
            <div class="np-pool-chart">
              <div class="np-pool-chart-head">
                <b>下发占比趋势</b>
                <span>{{ publishTotal }} 已下发</span>
              </div>
              <div class="np-pool-chart-body">
                <div class="np-trend">
                  <svg :viewBox="`0 0 ${TREND.w} ${TREND.h}`" class="np-trend-svg">
                    <line v-for="tick in trendTicks(publishTrendMax)" :key="`pub-grid-${tick.v}`"
                          class="np-trend-grid" :x1="TREND.left" :x2="TREND.w - TREND.right"
                          :y1="tick.y" :y2="tick.y" />
                    <text v-for="tick in trendTicks(publishTrendMax)" :key="`pub-tick-${tick.v}`"
                          class="np-trend-tick" :x="TREND.left - 5" :y="tick.y + 3" text-anchor="end">{{ tick.v }}</text>
                    <template v-for="row in publishChartRows" :key="`pub-line-${row.id}`">
                      <g v-if="row.enabled && trendHasValue('published_count', row)">
                        <path :d="trendArea('published_count', row, publishTrendMax)"
                              :fill="airportColor(row)" class="np-trend-area" />
                        <path :d="trendPath('published_count', row, publishTrendMax)"
                              :stroke="airportColor(row)" class="np-trend-line" />
                        <circle v-for="(point, index) in historyPoints" :key="`pub-dot-${row.id}-${index}`"
                                :cx="trendXY('published_count', row, index, publishTrendMax).x"
                                :cy="trendXY('published_count', row, index, publishTrendMax).y"
                                r="2.4" :fill="airportColor(row)" class="np-trend-dot" />
                      </g>
                    </template>
                    <g v-for="label in trendEndLabels('published_count', publishTrendMax)" :key="`pub-label-${label.id}`">
                      <rect :x="TREND.w - TREND.right + 4" :y="label.y - 6.25" :width="label.width" height="12.5" rx="3" :fill="label.color" />
                      <text class="np-trend-label" :x="TREND.w - TREND.right + 8" :y="label.y + 3.4">{{ label.name }} {{ label.pct }}%</text>
                    </g>
                    <text v-for="label in trendXLabels()" :key="`pub-x-${label.x}`"
                          class="np-trend-axis" :x="label.x" :y="TREND.h - 3" :text-anchor="label.anchor">{{ label.text }}</text>
                  </svg>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="np-pool-subhead">
          <span title="综合评分 = 可用率 45% + 下发率 45% + 池占比 10%">机场明细</span>
          <span>最近 {{ historyPoints.length }} 个批次 · 按综合评分降序</span>
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
              <div class="np-pool-airport-meta">
                <span>可用 <b>{{ row.enabled ? `${row.available_count || 0}/${row.pool_count}` : '—' }}</b></span>
                <span>下发 <b>{{ row.enabled ? `${row.published_count || 0}/${row.pool_count}` : '—' }}</b></span>
              </div>
            </div>
            <div class="np-pool-score" :class="scoreTone(row)" :title="scoreHint(row)">
              <span>综合评分</span><b>{{ row.enabled && row.score !== null ? row.score : '—' }}</b><em>{{ row.score_grade }}</em>
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
