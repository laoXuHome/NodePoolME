<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs, logLevelLabel, logSourceLabel } from '../api'
import TrendChart from '../components/TrendChart.vue'

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

function scoreTone(row) {
  if (!row.enabled) return 'is-off'
  if (row.score === null || row.score === undefined) return 'is-empty'
  if (row.score >= 80) return 'is-good'
  if (row.score >= 60) return 'is-ok'
  if (row.score >= 40) return 'is-mid'
  if (row.score >= 20) return 'is-low'
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
  const share = row.share_rate ?? Math.round(pool / (data.value?.nodes_total || 1) * 100)
  const tag = row.tag_rate ?? Math.round((row.tag_ok_count || 0) / pool * 100)
  const basis = row.score_samples ? `近 7 天 ${row.score_samples} 个完整检测快照` : '无完整检测快照，按当前值'
  return `${basis} · 可用率 ${health}%（×${scoreWeights.value.available}%）· 下发率 ${publish}%（×${scoreWeights.value.publish}%）· 池占比 ${share}%（×${scoreWeights.value.share}%）· 标签合格率 ${tag}%（×${scoreWeights.value.tag}%）→ 综合 ${row.score} 分`
}
function scoreSampleText(row) {
  if (!row.enabled) return '未启用'
  if (!row.pool_count) return '空池'
  if (row.sampled_points) return `${row.score_samples}/${row.sampled_points} 点命中`
  return row.score_samples ? `${row.score_samples} 个完整快照` : '按当前值'
}
function rateText(row, key) {
  if (!row.enabled || row[key] === null || row[key] === undefined) return '—'
  return `${Number(row[key]).toFixed(1)}%`
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
              <col class="np-score-col-status" />
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
                <th class="is-status">数据成熟度</th>
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
                  <small>{{ row.enabled ? '加权' : '—' }}</small>
                </td>
                <td class="np-score-cell">
                  <b>{{ rateText(row, 'publish_rate') }}</b>
                  <small>{{ row.enabled ? '加权' : '—' }}</small>
                </td>
                <td class="np-score-cell">
                  <b>{{ rateText(row, 'share_rate') }}</b>
                  <small>{{ row.enabled ? '加权' : '—' }}</small>
                </td>
                <td class="np-score-cell">
                  <b>{{ rateText(row, 'tag_rate') }}</b>
                  <small>{{ row.enabled ? `${row.tag_ok_count || 0}/${row.pool_count}` : '—' }}</small>
                </td>
                <td class="np-score-cell is-status" :title="statusHint(row)">
                  <div class="np-data-status" :class="statusTone(row)">
                    <b>{{ row.data_status_text || '—' }}</b>
                    <small>{{ row.data_status_detail || '' }}</small>
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
        </div>
        <div v-if="data.airport_list?.length" class="np-score-foot">
          <span>评分 = 可用率 {{ scoreWeights.available }}% + 下发率 {{ scoreWeights.publish }}% + 池占比 {{ scoreWeights.share }}% + 标签合格率 {{ scoreWeights.tag }}%；前 3 项取近 {{ data.score_window_days || 7 }} 天里按检测计划生成、且被快照命中的采样点，再做时段加权（17:00–01:00 权重 50%、01:00–08:00 15%、08:00–17:00 35%）</span>
          <span>覆盖率 = 有效采样点 / 计划采样点，只反映数据完整度，不参与评分；每天不足 4 份或 60 分钟内超过 2 份的日期整日淘汰，暂停、中断和单节点测试不计入</span>
        </div>
        </div>

        <div v-if="data.airport_list?.length" class="np-pool-charts">
            <div class="np-trend-toolbar">
              <span>节点趋势 <em>{{ trendRangeText }} · {{ trendSampleText }}</em></span>
              <div class="np-trend-range" role="group" aria-label="趋势区间">
                <button type="button" :class="{ 'is-active': trendRange === '1d' }" @click="trendRange = '1d'">近 1 天</button>
                <button type="button" :class="{ 'is-active': trendRange === '7d' }" @click="trendRange = '7d'">近 7 天</button>
                <button type="button" :class="{ 'is-active': trendRange === '30d' }" @click="trendRange = '30d'">近 30 天</button>
              </div>
            </div>
            <div v-for="chart in trendCharts" :key="chart.key" class="np-pool-chart">
              <TrendChart
                :points="chart.points"
                :rows="chart.rows"
                :field="chart.field"
                :range="trendRange"
                :title="chart.title"
                :palette="AIRPORT_COLORS"
              />
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
            <el-tag v-if="runtime.running" :type="runTagType" size="small">{{ runStateText }}</el-tag>
            <el-tag v-else type="success" size="small">空闲</el-tag>
          </div>
          <div class="np-detect-inline">
            <span>引擎 <b>{{ runtime.engine }}</b></span>
            <span>上次启动 {{ shortTs(runtime.last_cycle_at || data.batch?.started_at) }}</span>
          </div>
          <div class="np-detect-actions">
            <el-button v-if="!runtime.running" size="small" type="primary" @click="runNow">立即检测</el-button>
            <el-button v-if="runtime.running && !runtime.paused"
                       size="small" :disabled="runtime.stop_requested" @click="pauseCycle">暂停</el-button>
            <el-button v-if="runtime.running && runtime.paused"
                       size="small" type="success" :disabled="runtime.stop_requested" @click="resumeCycle">继续</el-button>
            <el-button v-if="runtime.running" size="small" type="danger" plain
                       :disabled="runtime.stop_requested" @click="stopCycle">
              {{ runtime.stop_requested ? '正在停止' : '停止检测' }}
            </el-button>
          </div>
        </div>

        <div class="np-detect-rings">
          <div class="np-ring-item">
            <div class="np-batch-gauge is-ring" :class="`is-${runTone}`">
              <svg viewBox="0 0 160 160" aria-hidden="true">
                <circle class="np-batch-gauge-track" cx="80" cy="80" r="65" pathLength="100" />
                <circle class="np-batch-gauge-value" cx="80" cy="80" r="65" pathLength="100"
                        :style="{ strokeDasharray: `${latencyPct} 100` }" />
              </svg>
              <div class="np-batch-gauge-label"><strong>{{ latencyPct }}</strong><span>%</span></div>
            </div>
            <div class="np-ring-meta">
              <b>延迟检测</b>
              <span>{{ runtime.latency_done || 0 }} / {{ runtime.latency_total || 0 }} 节点</span>
            </div>
          </div>
          <div class="np-ring-item">
            <div class="np-batch-gauge is-ring is-tag" :class="`is-${runTone}`">
              <svg viewBox="0 0 160 160" aria-hidden="true">
                <circle class="np-batch-gauge-track" cx="80" cy="80" r="65" pathLength="100" />
                <circle class="np-batch-gauge-value" cx="80" cy="80" r="65" pathLength="100"
                        :style="{ strokeDasharray: `${tagPct} 100` }" />
              </svg>
              <div class="np-batch-gauge-label"><strong>{{ tagPct }}</strong><span>%</span></div>
            </div>
            <div class="np-ring-meta">
              <b>标签检测</b>
              <span>{{ runtime.tag_done || 0 }} / {{ runtime.tag_total || 0 }} 项</span>
            </div>
          </div>
        </div>

        <div class="np-current" :class="{ 'is-idle': !runtime.running }">
          <template v-if="runtime.running && runtime.current">
            <div class="np-current-head">
              <i class="np-current-dot" />
              <span>正在检测</span>
              <span class="np-flex" />
              <span class="np-current-stage">{{ runtime.current.stage }}</span>
            </div>
            <div class="np-current-node">
              <b :title="runtime.current.node">{{ runtime.current.node }}</b>
            </div>
            <div class="np-current-meta">
              <span>{{ runtime.current.airport || '—' }}</span>
              <span v-if="runtime.current.protocol">{{ runtime.current.protocol }}</span>
              <span v-if="runtime.current.g || runtime.current.w" class="np-current-lat">
                G {{ runtime.current.g ?? '—' }}ms · W {{ runtime.current.w ?? '—' }}ms
              </span>
              <span v-else-if="runtime.current.latency" class="np-current-lat">{{ runtime.current.latency }}ms</span>
              <span class="np-current-result" :class="{ 'is-bad': ['未达标','未通过'].includes(runtime.current.result) }">
                {{ runtime.current.result }}
              </span>
            </div>
          </template>
          <template v-else>
            <div class="np-current-head">
              <i class="np-current-dot is-idle" />
              <span>{{ runtime.running ? '等待节点数据' : '当前空闲' }}</span>
              <span class="np-flex" />
              <span v-if="runtime.phase && runtime.running" class="np-current-stage">{{ runtime.phase }}</span>
            </div>
            <div class="np-current-meta">
              <span>批次 #{{ runtime.batch_id || '—' }}</span>
              <span>已处理 {{ runtime.done || 0 }} / {{ runtime.total || 0 }} 项</span>
              <span v-if="runtime.last_cycle_at">上次启动 {{ shortTs(runtime.last_cycle_at) }}</span>
            </div>
          </template>
        </div>

        <div class="np-detect-batch-meta">
          <span>批次 #{{ data.batch?.id || '—' }}</span>
          <span>{{ batchStatusLabel }}</span>
          <span>耗时 {{ batchDuration }}</span>
          <span>剩余 {{ batchRemaining }}</span>
        </div>

        <div class="np-detect-sumstats">
          <div><span>启用标签</span><b>{{ data.group_summary?.count || 0 }}</b></div>
          <div><span>命中节点</span><b>{{ data.group_summary?.hits || 0 }}</b></div>
          <div><span>累计通过</span><b>{{ data.group_summary?.passed || 0 }}</b></div>
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
