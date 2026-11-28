<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const summary = ref(null)
const items = ref([])
const total = ref(0)
const page = ref(1)
const perPage = 20
const loading = ref(false)
const busy = ref(false)
const filters = ref({ q: '', quality_state: '', speed_grade: '' })
const detail = ref(null)
const drawer = ref(false)
const detailLoading = ref(false)
const settings = ref(null)
const settingsDialog = ref(false)
const settingsForm = ref({})
const saving = ref(false)

const BANDS = [
  { key: 'peak', label: '晚高峰', time: '17:00–01:00' },
  { key: 'night', label: '深夜', time: '01:00–08:00' },
  { key: 'day', label: '日间', time: '08:00–17:00' }
]
const QUALITY_STATE = {
  pass: { text: '质量合格', type: 'success' },
  fail: { text: '质量不合格', type: 'danger' },
  unknown: { text: '观察中', type: 'info' }
}
const SPEED = {
  excellent: { text: '优', emoji: '🟢', color: '#10B981' },
  good: { text: '良', emoji: '🔵', color: '#4682B4' },
  poor: { text: '差', emoji: '🟠', color: '#F59E0B' },
  danger: { text: '危', emoji: '🔴', color: '#EF4444' },
  empty: { text: '无数据', emoji: '⚪', color: '#AAB7B1' },
  excluded: { text: '机场排除', emoji: '⚪', color: '#AAB7B1' }
}
const REASON_TEXT = {
  no_valid_snapshots: '七天内无有效快照，进入观察，不按 0 分',
  not_enough_qualified_days: '达标评价天数不足',
  low_maturity: '数据成熟度未达线',
  low_time_coverage: '三时段覆盖不足',
  severe_day: '存在严重故障日，质量分已封顶 60',
  consecutive_failures: '存在连续失败 ≥3 次，质量分已封顶 60',
  no_latency_samples: '缺少延迟样本，延迟项不参与计分',
  airport_excluded: '机场已排除测速，速度显示灰色无数据',
  no_data: '没有有效测速结果，不能判为低速',
  pending: '测速任务仍在排队或执行',
  node_failure: '节点自身下载失败',
  source_failure: '测速目标故障',
  detector_error: '检测服务异常',
  timeout_unknown: '超时但无法确认责任方',
  interrupted: '任务取消或中断'
}
const ROW_STATUS = {
  ok: '成功', partial: '部分成功', node_error: '节点失败', source_unavailable: '目标故障',
  engine_error: '服务异常', failed: '失败', canceled: '已取消', excluded: '已排除',
  success: '成功', failure: '失败', unknown: 'Unknown'
}
const LAYER_TEXT = { raw: '原始池', healthy: '健康池', observation: '观察池' }
const HEALTH_TEXT = { healthy: '健康', unhealthy: '不健康', unknown: '未知' }
const ATTR_TEXT = {
  success: '测速成功', node_failure: '节点下载失败', source_failure: '测速目标故障',
  detector_error: '检测服务异常', ambiguous: '超时责任不明', interrupted: '任务取消/中断',
  excluded: '机场排除', pending: '任务排队'
}

const q = computed(() => summary.value || {})
const sGrades = computed(() => q.value.speed_grades || {})
const settingsCurrent = computed(() => settings.value?.current || q.value.settings || {})
const reasonText = (v) => REASON_TEXT[v] || ATTR_TEXT[v] || v || '—'
const layerText = (v) => LAYER_TEXT[v] || v || '原始池'
const healthText = (v) => HEALTH_TEXT[v] || v || '未知'
const shortTs = (ts) => { const t = formatTs(ts); return t === '—' ? t : t.slice(5, 16) }
const stateOf = (v) => QUALITY_STATE[v] || QUALITY_STATE.unknown
const speedOf = (v) => SPEED[v] || SPEED.empty
const fmt1 = (v) => (v === null || v === undefined || v === '' ? '—' : Number(v).toFixed(1))
const fmtSpeed = (v) => (v === null || v === undefined ? '—' : Number(v) >= 1 ? `${Number(v).toFixed(2)} MB/s` : `${Math.round(Number(v) * 1024)} KB/s`)
const fmtBytes = (v) => {
  const n = Number(v || 0)
  if (n <= 0) return '0 B'
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${Math.round(n / 1024)} KB`
}
const bandOf = (row, key) => (row?.band_stats?.[key]) || {}

function qualityTag(row) {
  if (!row || row.quality_score === null) return { text: '观察中', type: 'info' }
  if (!row.mature) return { text: '数据不足', type: 'warning' }
  return row.quality_pass ? { text: '合格', type: 'success' } : { text: '不合格', type: 'danger' }
}

async function loadSummary() {
  summary.value = await api.get('/api/quality/summary')
}
async function loadNodes() {
  const params = new URLSearchParams({ page: String(page.value), per_page: String(perPage) })
  if (filters.value.q) params.set('q', filters.value.q)
  if (filters.value.quality_state) params.set('quality_state', filters.value.quality_state)
  if (filters.value.speed_grade) params.set('speed_grade', filters.value.speed_grade)
  const data = await api.get(`/api/quality/nodes?${params}`)
  items.value = data.items || []
  total.value = data.total || 0
}
async function loadAll() {
  loading.value = true
  try { await Promise.all([loadSummary(), loadNodes()]) }
  catch (e) { ElMessage.error(e.message || '质量评价数据加载失败') }
  finally { loading.value = false }
}
async function refreshEvaluation() {
  busy.value = true
  try {
    await api.post('/api/quality/evaluate', { force: true })
    await loadAll()
    ElMessage.success('七天质量评价已刷新')
  } catch (e) { ElMessage.error(e.message || '评价刷新失败') }
  finally { busy.value = false }
}
async function openDetail(row) {
  drawer.value = true
  detailLoading.value = true
  detail.value = null
  try { detail.value = await api.get(`/api/quality/nodes/${row.id}`) }
  catch (e) { ElMessage.error(e.message || '节点详情加载失败') }
  finally { detailLoading.value = false }
}
async function speedTest(row, mode = 'retest') {
  try {
    const res = await api.post('/api/quality/speed/test', { mode, node_ids: [row.id] })
    ElMessage.success(`已提交${mode === 'retest' ? '重新测速' : '手动测速'}：新建 ${res.created}，跳过 ${res.duplicate}`)
    setTimeout(loadAll, 1200)
  } catch (e) { ElMessage.error(e.message || '测速任务创建失败') }
}
async function testNew() {
  const res = await api.post('/api/quality/speed/test', { mode: 'new', node_ids: [] })
  ElMessage.success(`新节点测速已入队：${res.created} 个`)
}
async function retestAll() {
  await ElMessageBox.confirm('将对所有未排除机场的节点重新测速，是否继续？', '全部重新测试', { type: 'warning' })
  const res = await api.post('/api/quality/speed/test', { mode: 'all', node_ids: [] })
  ElMessage.success(`全部重新测试已入队：${res.created} 个，跳过 ${res.duplicate}`)
}
async function toggleAirport(airport, val) {
  try {
    await api.patch(`/api/quality/airports/${airport.id}`, { excluded: val })
    ElMessage.success(`机场「${airport.name}」${val ? '已排除测速' : '已恢复测速'}`)
    await loadAll()
  } catch (e) { ElMessage.error(e.message || '机场测速状态保存失败') }
}
async function openSettings() {
  settings.value = await api.get('/api/quality/settings')
  const cur = settings.value.current
  settingsForm.value = {
    min_daily_snapshots: cur.min_daily_snapshots,
    min_quality_days: cur.min_quality_days,
    min_maturity: cur.min_maturity,
    quality_pass: cur.quality_pass,
    speed_excellent: cur.speed_excellent,
    speed_good: cur.speed_good,
    speed_minimum: cur.speed_minimum,
    speed_ttl_days: cur.speed_ttl_days,
    speed_limit_mb: cur.speed_limit_mb,
    speed_window_start: cur.speed_window_start,
    speed_window_end: cur.speed_window_end,
    speed_schedule_enabled: cur.speed_schedule_enabled,
    speed_every_days: cur.speed_every_days,
    speed_concurrency: cur.speed_concurrency,
    weights: { ...cur.weights }
  }
  settingsDialog.value = true
}
async function saveSettings() {
  saving.value = true
  try {
    const payload = { ...settingsForm.value }
    await api.patch('/api/quality/settings', payload)
    settings.value = await api.get('/api/quality/settings')
    await loadAll()
    ElMessage.success('评价参数已保存，配置版本已提升')
  } catch (e) { ElMessage.error(e.message || '参数校验未通过，请检查速度等级区间') }
  finally { saving.value = false }
}
function setFilter(key, value) {
  filters.value[key] = value
  page.value = 1
  loadNodes()
}
function pager(next) { page.value = next; loadNodes() }

onMounted(loadAll)
</script>

<template>
  <div v-loading="loading" class="np-page p4-page">
    <div class="p4-head">
      <div>
        <h2 class="np-page-title">质量评价与下载速度</h2>
      </div>
      <div class="p4-head-actions">
        <el-button size="small" @click="openSettings">评价设置</el-button>
        <el-button size="small" @click="testNew">测试新节点</el-button>
        <el-button size="small" @click="retestAll">全部重新测试</el-button>
        <el-button size="small" type="primary" :loading="busy" @click="refreshEvaluation">刷新评价</el-button>
      </div>
    </div>

    <div class="p4-quality-grid">
      <button class="p4-stat" @click="setFilter('quality_state', '')">
        <span>节点总数<i>P1 RAW</i></span><b>{{ q.nodes_total || 0 }}</b>
        <small>原始节点池评价对象</small>
      </button>
      <button class="p4-stat" @click="setFilter('quality_state', '')">
        <span>已评价<i>SCORED</i></span><b>{{ q.quality_evaluated || 0 }}</b>
        <small>有确定性快照证据</small>
      </button>
      <button class="p4-stat is-good" @click="setFilter('quality_state', 'pass')">
        <span>质量合格<i>PASS</i></span><b>{{ q.quality_pass || 0 }}</b>
        <small>成熟且 ≥ {{ q.settings?.quality_pass || 75 }} 分</small>
      </button>
      <button class="p4-stat is-bad" @click="setFilter('quality_state', 'fail')">
        <span>质量不合格<i>FAIL</i></span><b>{{ q.quality_fail || 0 }}</b>
        <small>成熟但未达合格线</small>
      </button>
      <button class="p4-stat" @click="setFilter('quality_state', '')">
        <span>数据成熟<i>MATURE</i></span><b>{{ q.quality_mature || 0 }}</b>
        <small>可进入正式评价资格</small>
      </button>
      <button class="p4-stat is-warn" @click="setFilter('quality_state', '')">
        <span>数据不足<i>OBSERVE</i></span><b>{{ q.quality_insufficient || 0 }}</b>
        <small>观察中，不按 0 分处理</small>
      </button>
    </div>

    <div class="p4-band-strip">
      <div v-for="b in BANDS" :key="b.key" class="p4-band">
        <span>{{ b.label }} <i>{{ b.time }}</i> · 权重 {{ q.settings?.weights?.[b.key] || (b.key === 'peak' ? 50 : b.key === 'night' ? 15 : 35) }}%</span>
        <b>{{ q.band_coverage?.[b.key] || 0 }} <small>节点有时段数据</small></b>
      </div>
      <div class="p4-band is-note">
        <span>计算口径</span>
        <b>P80 延迟 <small>可用率 50 · 延迟 30 · 稳定 20</small></b>
      </div>
    </div>

    <div class="p4-speed-card np-card">
      <div class="p4-speed-head">
        <div><h3>下载速度评价</h3><p class="np-muted">有效结果中位；7 天有效期；单次最高速不代表长期表现。</p></div>
        <div class="p4-plan">
          <span>计划 <b>{{ q.schedule?.enabled ? '定时开启' : '已停用' }}</b></span>
          <span>窗口 <b>{{ settingsCurrent.speed_window_start || q.schedule?.window_start }}–{{ settingsCurrent.speed_window_end || q.schedule?.window_end }}</b></span>
          <span>并发 <b>{{ settingsCurrent.speed_concurrency || 4 }}</b></span>
          <span>上限 <b>{{ settingsCurrent.speed_limit_mb || 50 }} MB</b></span>
          <span>最近测速 <b>{{ shortTs(q.speed_latest_at) }}</b></span>
        </div>
      </div>
      <div class="p4-speed-grid">
        <div><span>已测速</span><b>{{ q.speed_tested || 0 }}</b><small>有效速度结果</small></div>
        <div class="excellent"><span>🟢 优</span><b>{{ sGrades.excellent || 0 }}</b><small>≥ 5 MB/s</small></div>
        <div class="good"><span>🔵 良</span><b>{{ sGrades.good || 0 }}</b><small>2–5 MB/s</small></div>
        <div class="poor"><span>🟠 差</span><b>{{ sGrades.poor || 0 }}</b><small>512 KB/s–2 MB/s</small></div>
        <div class="danger"><span>🔴 危</span><b>{{ sGrades.danger || 0 }}</b><small>&lt; 512 KB/s</small></div>
        <div><span>⚪ 无数据</span><b>{{ q.speed_no_data || 0 }}</b><small>不判低速</small></div>
      </div>
      <div class="p4-airports">
        <div class="p4-airport-head"><b>机场测速排除</b><span class="np-muted">灰色无数据，可随时恢复；不影响其他检测与质量评价</span></div>
        <div class="p4-airport-list">
          <div v-for="a in (summary?.excluded_airports || [])" :key="a.id" class="p4-airport is-off">
            <span>{{ a.name }}</span><el-tag size="small" type="info" effect="plain">已排除</el-tag>
            <el-switch :model-value="true" size="small" @change="v => toggleAirport(a, v)" />
          </div>
          <div v-for="a in (summary?.all_airports || [])" :key="'x' + a.id" v-show="false" />
          <span v-if="!(summary?.excluded_airports || []).length" class="np-muted">当前没有排除测速的机场</span>
        </div>
        <div class="p4-airport-add">
          <el-select placeholder="选择机场加入排除" size="small" style="width: 220px" @change="id => { const a = (summary?.all_airports || []).find(x => x.id === id); if (a) toggleAirport(a, true) }">
            <el-option v-for="a in (summary?.all_airports || []).filter(x => !x.speed_excluded)" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
        </div>
      </div>
    </div>

    <div class="p4-toolbar">
      <el-input v-model="filters.q" placeholder="节点名 / 服务器 / 机场" clearable style="width: 240px"
                @keyup.enter="setFilter('q', filters.q)" />
      <el-select v-model="filters.quality_state" placeholder="质量状态" clearable style="width: 140px" @change="v => setFilter('quality_state', v)">
        <el-option label="质量合格" value="pass" /><el-option label="质量不合格" value="fail" />
        <el-option label="观察中" value="unknown" />
      </el-select>
      <el-select v-model="filters.speed_grade" placeholder="速度等级" clearable style="width: 130px" @change="v => setFilter('speed_grade', v)">
        <el-option label="🟢 优" value="excellent" /><el-option label="🔵 良" value="good" />
        <el-option label="🟠 差" value="poor" /><el-option label="🔴 危" value="danger" />
        <el-option label="⚪ 无数据" value="empty" /><el-option label="机场排除" value="excluded" />
      </el-select>
      <el-button size="small" @click="loadAll">查询</el-button>
      <span class="np-muted p4-count">共 {{ total }} 个节点 · 七天内 P80 延迟 · Unknown 不扣分</span>
    </div>

    <div class="np-card p4-table-card">
      <el-table :data="items" size="small" row-key="id" @row-click="openDetail">
        <el-table-column label="节点" min-width="190">
          <template #default="{ row }">
            <div class="p4-node-name">{{ row.current_name }}</div>
            <div class="np-muted p4-node-sub">{{ row.server }}:{{ row.port }} · {{ row.airport_name }}</div>
            <div class="p4-node-tags">
              <span class="p4-layer">{{ layerText(row.management_layer) }}</span>
              <el-tag size="small" effect="plain" :type="row.base_health === 'healthy' ? 'success' : row.base_health === 'unhealthy' ? 'danger' : 'info'">
                P2 {{ healthText(row.base_health) }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="七天质量" width="108">
          <template #default="{ row }">
            <div class="p4-score"><b>{{ row.quality_score === null ? '—' : fmt1(row.quality_score) }}</b><small>/100</small></div>
            <el-tag size="small" :type="qualityTag(row).type" effect="light">{{ qualityTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="数据成熟度" width="104">
          <template #default="{ row }">
            <div class="p4-mature"><b>{{ row.maturity_score === null ? '—' : fmt1(row.maturity_score) }}%</b>
              <el-tag size="small" :type="row.mature ? 'success' : 'info'" effect="plain">{{ row.mature ? '成熟' : '观察' }}</el-tag></div>
            <small class="np-muted">达标 {{ row.full_days || 0 }} / 7 天</small>
          </template>
        </el-table-column>
        <el-table-column label="有效快照" width="74" align="center">
          <template #default="{ row }"><b>{{ row.valid_snapshots || 0 }}</b><div class="np-muted p4-tiny">有数据 {{ row.valid_days || 0 }} 天</div></template>
        </el-table-column>
        <el-table-column label="三时段覆盖" width="164">
          <template #default="{ row }">
            <div class="p4-bands">
              <div v-for="b in BANDS" :key="b.key" :class="{ zero: !(bandOf(row, b.key).valid) }">
                <span>{{ b.key === 'peak' ? '峰' : b.key === 'night' ? '夜' : '日' }}</span>
                <b>{{ bandOf(row, b.key).valid || 0 }}</b>
                <small>{{ bandOf(row, b.key).availability === null || bandOf(row, b.key).availability === undefined ? '—' : Math.round(bandOf(row, b.key).availability) + '%' }}</small>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="延迟表现" width="88">
          <template #default="{ row }"><b>{{ row.p80_ms === null ? '—' : Math.round(row.p80_ms) }} <small>ms</small></b><div class="np-muted p4-tiny">得分 {{ fmt1(row.delay_score) }}</div></template>
        </el-table-column>
        <el-table-column label="延迟稳定" width="88">
          <template #default="{ row }"><b>{{ row.p80_stddev_ms === null ? '—' : Math.round(row.p80_stddev_ms) }} <small>ms</small></b><div class="np-muted p4-tiny">得分 {{ fmt1(row.stability_score) }}</div></template>
        </el-table-column>
        <el-table-column label="异常记录" width="88">
          <template #default="{ row }">
            <span class="np-muted p4-tiny">严重日 {{ row.severe_days || 0 }} · 连败 {{ row.max_consecutive_failures || 0 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="下载速度" width="120">
          <template #default="{ row }">
            <div class="p4-speed-grade" :style="{ color: speedOf(row.speed_grade).color }">
              <b>{{ speedOf(row.speed_grade).emoji }}</b><b>{{ fmtSpeed(row.speed_mb_s) }}</b>
            </div>
            <div class="np-muted p4-tiny">{{ speedOf(row.speed_grade).text }} · {{ row.speed_valid_runs || 0 }} 次有效</div>
            <div class="np-muted p4-tiny">{{ shortTs(row.speed_at) }} · {{ fmtBytes(row.speed_bytes) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="116">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="openDetail(row)">详情</el-button>
            <el-button link size="small" :disabled="row.excluded" @click.stop="speedTest(row, 'manual')">测速</el-button>
            <el-button link size="small" :disabled="row.excluded" @click.stop="speedTest(row, 'retest')">重测</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="p4-pager"><el-pagination :current-page="page" :page-size="perPage" :total="total" layout="prev, pager, next" small @current-change="pager" /></div>
    </div>

    <div class="p4-mobile-list">
      <div v-for="row in items" :key="row.id" class="np-card p4-mcard" @click="openDetail(row)">
        <div class="p4-mcard-head">
          <div><b>{{ row.current_name }}</b><div class="np-muted p4-node-sub">{{ row.airport_name }} · {{ row.server }}:{{ row.port }}</div></div>
          <el-tag size="small" :type="qualityTag(row).type" effect="light">{{ qualityTag(row).text }}</el-tag>
        </div>
        <div class="p4-mcard-main">
          <div><span>质量分</span><b>{{ row.quality_score === null ? '—' : fmt1(row.quality_score) }}</b></div>
          <div><span>成熟度</span><b>{{ row.maturity_score === null ? '—' : fmt1(row.maturity_score) }}%</b></div>
          <div><span>有效快照</span><b>{{ row.valid_snapshots || 0 }}</b></div>
          <div><span>下载速度</span><b :style="{ color: speedOf(row.speed_grade).color }">{{ speedOf(row.speed_grade).emoji }} {{ fmtSpeed(row.speed_mb_s) }}</b></div>
        </div>
        <div class="p4-mbands">
          <span v-for="b in BANDS" :key="b.key">{{ b.key === 'peak' ? '峰' : b.key === 'night' ? '夜' : '日' }} {{ bandOf(row, b.key).valid || 0 }} 次</span>
          <span>达标 {{ row.full_days || 0 }}/7 天</span>
          <span v-if="row.severe_days">严重故障日 {{ row.severe_days }}</span>
          <span v-if="row.max_consecutive_failures >= 3">连败 {{ row.max_consecutive_failures }}</span>
        </div>
        <div class="p4-mcard-foot">
          <span class="np-muted">P2 {{ healthText(row.base_health) }} · 最近测速 {{ shortTs(row.speed_at) }}</span>
          <el-button link type="primary" size="small" @click.stop="speedTest(row, 'manual')">测速</el-button>
          <el-button link size="small" @click.stop="speedTest(row, 'retest')">重测</el-button>
        </div>
      </div>
      <el-pagination :current-page="page" :page-size="perPage" :total="total" layout="prev, pager, next" small @current-change="pager" />
    </div>

    <el-dialog v-model="settingsDialog" title="质量评价与下载速度参数" width="720px">
      <div v-if="settings" class="p4-settings">
        <div class="p4-setting-section">
          <h4>七天质量评价</h4>
          <div class="p4-setting-grid">
            <div v-for="f in settings.fields.filter(x => ['min_daily_snapshots','min_quality_days','min_maturity','quality_pass'].includes(x.key))" :key="f.key" class="p4-field">
              <label>{{ f.label }}<i>{{ f.default }} 默认</i></label>
              <el-input-number v-model="settingsForm[f.key]" :min="f.min" :max="f.max" size="small" controls-position="right" />
              <small>{{ f.description }} · 范围 {{ f.min }}–{{ f.max }}{{ f.unit }}</small>
            </div>
          </div>
          <div class="p4-fixed-note">七天评价周期固定 7 天连续滚动（不是自然周）；数据成熟度独立于质量分。</div>
          <div class="p4-weight-grid">
            <div v-for="b in BANDS" :key="b.key" class="p4-field">
              <label>{{ b.label }}权重<i>{{ b.time }}</i></label>
              <el-input-number v-model="settingsForm.weights[b.key]" :min="0" :max="100" size="small" controls-position="right" />
            </div>
          </div>
          <small class="np-muted">质量分 = 有效可用率 50 + 延迟表现 30 + 延迟稳定性 20；三项时段权重合计必须为 100。某时段无数据时只在有时段的时段间重归一化，不判失败。</small>
        </div>
        <div class="p4-setting-section">
          <h4>下载速度评价</h4>
          <div class="p4-setting-grid">
            <div v-for="f in settings.fields.filter(x => ['speed_excellent','speed_good','speed_minimum','speed_ttl_days','speed_limit_mb','speed_concurrency','speed_every_days'].includes(x.key))" :key="f.key" class="p4-field">
              <label>{{ f.label }}<i>{{ f.default }} 默认</i></label>
              <el-input-number v-model="settingsForm[f.key]" :min="f.min" :max="f.max" size="small" controls-position="right" />
              <small>{{ f.description }} · 范围 {{ f.min }}–{{ f.max }}{{ f.unit }}</small>
            </div>
            <div class="p4-field">
              <label>测速窗口<i>默认 17:00–24:00</i></label>
              <div class="p4-time-range">
                <el-input v-model="settingsForm.speed_window_start" size="small" placeholder="17:00" />
                <span>–</span>
                <el-input v-model="settingsForm.speed_window_end" size="small" placeholder="24:00" />
              </div>
            </div>
            <div class="p4-field">
              <label>定时测速<i>固定频率，非随机</i></label>
              <el-switch v-model="settingsForm.speed_schedule_enabled" />
            </div>
          </div>
          <small class="np-muted">优 ≥ 5 MB/s、良 ≥ 2 MB/s、差 ≥ 512 KB/s、危 &lt; 512 KB/s；保存时校验区间组合。</small>
        </div>
      </div>
      <template #footer><el-button @click="settingsDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveSettings">保存并校验</el-button></template>
    </el-dialog>

    <el-drawer v-model="drawer" size="min(900px, 96vw)" title="节点评价详情">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="p4-detail-head">
            <div><h3>{{ detail.node.current_name }}</h3><p class="np-muted">{{ detail.node.airport_name }} · {{ detail.node.server }}:{{ detail.node.port }}</p></div>
            <div class="p4-detail-actions">
              <el-button size="small" :disabled="detail.node.speed_excluded" @click="speedTest(detail.node, 'manual')">手动测速</el-button>
              <el-button size="small" @click="speedTest(detail.node, 'retest')">重新测速</el-button>
            </div>
          </div>
          <div class="p4-detail-tags">
            <el-tag effect="plain">{{ layerText(detail.node.management_layer) }}</el-tag>
            <el-tag effect="plain" :type="detail.node.base_health === 'healthy' ? 'success' : 'info'">P2 {{ healthText(detail.node.base_health) }}</el-tag>
            <el-tag effect="plain" :type="qualityTag(detail.quality).type">{{ qualityTag(detail.quality).text }}</el-tag>
            <el-tag effect="plain" :type="detail.quality.mature ? 'success' : 'info'">{{ detail.quality.mature ? '数据成熟' : '观察中' }}</el-tag>
          </div>

          <div class="p4-detail-grid">
            <div class="p4-detail-score np-card">
              <span>七天质量分数</span><b>{{ detail.quality.quality_score === null ? '—' : fmt1(detail.quality.quality_score) }}</b>
              <small>合格线 {{ detail.quality.quality_line }} 分 · 与成熟度独立</small>
              <div class="p4-bars">
                <div><label>有效可用率 · 50%</label><b>{{ fmt1(detail.quality.available_score) }}</b><el-progress :percentage="Math.max(0, Math.min(100, Number(detail.quality.available_score || 0)))" :show-text="false" :stroke-width="5" /></div>
                <div><label>延迟表现 · 30%</label><b>{{ fmt1(detail.quality.delay_score) }}</b><el-progress :percentage="Math.max(0, Math.min(100, Number(detail.quality.delay_score || 0)))" :show-text="false" :stroke-width="5" /></div>
                <div><label>延迟稳定性 · 20%</label><b>{{ fmt1(detail.quality.stability_score) }}</b><el-progress :percentage="Math.max(0, Math.min(100, Number(detail.quality.stability_score || 0)))" :show-text="false" :stroke-width="5" /></div>
              </div>
            </div>
            <div class="p4-detail-maturity np-card">
              <span>数据成熟度 · 独立</span><b>{{ fmt1(detail.quality.maturity_score) }}%</b>
              <small>{{ detail.quality.mature ? '数据成熟，可参与正式评价资格' : '数据不足，只观察，不按 0 分' }}</small>
              <div class="p4-maturity-grid">
                <div><span>每日有效快照</span><b>{{ detail.quality.valid_snapshots }}</b></div>
                <div><span>达标天数</span><b>{{ detail.quality.full_days }} / 7</b></div>
                <div><span>有数据天数</span><b>{{ detail.quality.valid_days }}</b></div>
                <div><span>三时段覆盖</span><b>{{ fmt1(detail.quality.coverage) }}%</b></div>
              </div>
            </div>
          </div>

          <div class="p4-detail-facts">
            <div><span>有效可用率</span><b>{{ fmt1(detail.quality.availability) }}%</b></div>
            <div><span>TCP P80</span><b>{{ detail.quality.p80_ms === null ? '—' : Math.round(detail.quality.p80_ms) }} ms</b></div>
            <div><span>延迟标准差</span><b>{{ detail.quality.p80_stddev_ms === null ? '—' : Math.round(detail.quality.p80_stddev_ms) }} ms</b></div>
            <div><span>有效快照</span><b>{{ detail.quality.valid_snapshots }}</b></div>
            <div><span>严重故障日</span><b>{{ detail.quality.severe_days || 0 }}</b></div>
            <div><span>最长连续失败</span><b>{{ detail.quality.max_consecutive_failures || 0 }}</b></div>
          </div>

          <div v-if="detail.quality.reasons?.length" class="p4-reasons">
            <div v-for="r in detail.quality.reasons" :key="r"><el-tag size="small" type="warning" effect="plain">{{ reasonText(r) }}</el-tag></div>
          </div>

          <div class="p4-detail-section">
            <h4>三时段质量表现</h4>
            <div class="p4-band-table">
              <div class="p4-band-row is-head"><span>时段</span><span>权重</span><span>有效</span><span>成功</span><span>可用率</span><span>P80</span></div>
              <div v-for="b in BANDS" :key="b.key" class="p4-band-row">
                <span>{{ b.label }}<i>{{ b.time }}</i></span>
                <span>{{ bandOf(detail.quality, b.key).weight || 0 }}%</span>
                <span>{{ bandOf(detail.quality, b.key).valid || 0 }}</span>
                <span>{{ bandOf(detail.quality, b.key).success || 0 }}</span>
                <span>{{ bandOf(detail.quality, b.key).availability === null || bandOf(detail.quality, b.key).availability === undefined ? '无数据' : fmt1(bandOf(detail.quality, b.key).availability) + '%' }}</span>
                <span>{{ bandOf(detail.quality, b.key).p80_ms === null || bandOf(detail.quality, b.key).p80_ms === undefined ? '—' : Math.round(bandOf(detail.quality, b.key).p80_ms) + ' ms' }}</span>
              </div>
            </div>
          </div>

          <div class="p4-detail-section">
            <h4>每日质量变化</h4>
            <div class="p4-daily-list">
              <div v-for="d in (detail.quality.daily || [])" :key="d.date" class="p4-daily-row">
                <span>{{ d.date }}</span><b>{{ d.availability }}%</b>
                <small>{{ d.success }} 成 / {{ d.failure }} 败 · {{ d.valid }} 快照</small>
                <el-tag v-if="d.severe" size="small" type="danger" effect="plain">严重故障日</el-tag>
                <span class="p4-daily-bands">{{ (d.bands || []).join(' · ') }}</span>
              </div>
              <div v-if="!(detail.quality.daily || []).length" class="np-muted p4-empty">七天内没有确定性 Success/Failure 快照，节点处于观察状态。</div>
            </div>
          </div>

          <div class="p4-detail-section">
            <h4>下载速度</h4>
            <div class="p4-speed-detail">
              <div class="p4-speed-big" :style="{ color: speedOf(detail.quality.speed_grade).color }">
                <span>{{ speedOf(detail.quality.speed_grade).emoji }}</span><b>{{ fmtSpeed(detail.quality.speed_mb_s) }}</b>
                <small>{{ speedOf(detail.quality.speed_grade).text }} · 最近 {{ detail.quality.speed_valid_runs || 0 }} 次有效结果中位</small>
              </div>
              <div class="p4-speed-facts">
                <div><span>最近有效速度</span><b>{{ fmtSpeed(detail.quality.speed_latest_mb_s) }}</b></div>
                <div><span>测速时间</span><b>{{ shortTs(detail.quality.speed_at) }}</b></div>
                <div><span>本次流量</span><b>{{ fmtBytes(detail.quality.speed_bytes) }}</b></div>
                <div><span>单次上限</span><b>{{ Math.round((detail.quality.speed_limit_bytes || 0) / 1024 / 1024) }} MB</b></div>
              </div>
            </div>
            <div class="p4-speed-history">
              <div class="p4-speed-row is-head"><span>时间</span><span>状态</span><span>速度</span><span>流量</span><span>归因</span></div>
              <div v-for="r in (detail.speed_runs || [])" :key="r.id" class="p4-speed-row">
                <span>{{ shortTs(r.finished_at || r.started_at) }}</span>
                <span>{{ ROW_STATUS[r.status] || r.status }}</span>
                <span>{{ fmtSpeed(r.mb_per_s) }}</span>
                <span>{{ fmtBytes(r.bytes_total) }}</span>
                <span>{{ reasonText(r.attribution || r.error_type) }}</span>
              </div>
              <div v-if="!(detail.speed_runs || []).length" class="np-muted p4-empty">暂无测速历史。</div>
            </div>
          </div>

          <div class="p4-detail-section">
            <h4>与 P1 / P2 / P3 的关系</h4>
            <div class="p4-relation">
              <div><span>P1 三层归属</span><b>{{ layerText(detail.node.management_layer) }}</b><small>只读，不修改节点身份</small></div>
              <div><span>P2 基础健康</span><b>{{ healthText(detail.node.base_health) }}</b><small>只读，不改检测结论</small></div>
              <div><span>P3 业务资格</span><b>{{ (detail.p3_states || []).filter(x => x.state === 'qualified').length }} 项</b><small>只读，不改业务资格</small></div>
            </div>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.p4-page { max-width: 1600px; }
.p4-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 14px; }
.p4-head .np-page-desc { margin: 5px 0 0; max-width: 780px; }
.p4-head-actions { display: flex; gap: 7px; flex-wrap: wrap; justify-content: flex-end; }
.p4-quality-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 9px; margin-bottom: 10px; }
.p4-stat { text-align: left; background: var(--np-panel-solid); border: 1px solid rgba(31,61,51,.09); border-radius: 13px; padding: 12px 13px 10px; border-top: 3px solid #7e9c8d; cursor: pointer; }
.p4-stat:hover { box-shadow: 0 8px 20px rgba(31,61,51,.08); transform: translateY(-1px); }
.p4-stat.is-good { border-top-color: #2f8f62; }
.p4-stat.is-bad { border-top-color: #b04b4b; }
.p4-stat.is-warn { border-top-color: #c79a35; }
.p4-stat span { display: flex; justify-content: space-between; font-size: 12px; color: #6f8378; }
.p4-stat span i { font-style: normal; font-size: 9px; letter-spacing: .04em; color: #9aa79e; }
.p4-stat b { display: block; font-size: 27px; color: #1c332b; line-height: 1.15; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.p4-stat small { font-size: 10px; color: #8a998f; }
.p4-band-strip { display: grid; grid-template-columns: 1.1fr 1fr 1fr 1.25fr; gap: 8px; margin-bottom: 12px; }
.p4-band { background: rgba(240,246,242,.72); border: 1px solid rgba(31,61,51,.06); border-radius: 10px; padding: 8px 12px; }
.p4-band span { font-size: 11px; color: #74857c; }
.p4-band span i { font-style: normal; color: #9aa79e; }
.p4-band b { display: block; font-size: 18px; color: #29433a; margin-top: 2px; }
.p4-band b small { font-size: 10px; font-weight: 400; color: #8a998f; }
.p4-band.is-note b { font-size: 14px; margin-top: 6px; }
.p4-speed-card { padding: 14px 17px 13px; margin-bottom: 14px; }
.p4-speed-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.p4-speed-head h3 { margin: 0; font-size: 15px; color: #203730; }
.p4-speed-head p { margin: 3px 0 0; font-size: 11px; }
.p4-plan { display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; font-size: 10px; color: #83928a; }
.p4-plan b { color: #3b554a; }
.p4-speed-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.p4-speed-grid > div { background: #f8fbf9; border: 1px solid rgba(31,61,51,.06); border-radius: 9px; padding: 7px 10px; }
.p4-speed-grid span { font-size: 11px; color: #74857c; }
.p4-speed-grid b { display: block; font-size: 21px; color: #264139; margin: 1px 0; }
.p4-speed-grid small { font-size: 9px; color: #93a198; }
.p4-speed-grid .excellent b { color: #10B981; } .p4-speed-grid .good b { color: #4682B4; }
.p4-speed-grid .poor b { color: #F59E0B; } .p4-speed-grid .danger b { color: #EF4444; }
.p4-airports { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 10px; padding-top: 9px; border-top: 1px dashed rgba(31,61,51,.09); }
.p4-airport-head { font-size: 11px; color: #74857c; }
.p4-airport-head b { color: #385046; margin-right: 8px; }
.p4-airport-list { display: flex; gap: 6px; flex-wrap: wrap; flex: 1; }
.p4-airport { display: flex; align-items: center; gap: 6px; background: #f5f7f4; border-radius: 8px; padding: 4px 8px; font-size: 11px; color: #5d7267; }
.p4-airport-add { margin-left: auto; }
.p4-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.p4-count { margin-left: auto; font-size: 11px; }
.p4-table-card { padding: 0 0 6px; }
.p4-node-name { font-size: 12px; font-weight: 700; color: #263f35; }
.p4-node-sub { font-size: 10px; margin-top: 1px; }
.p4-node-tags { display: flex; gap: 5px; align-items: center; margin-top: 4px; }
.p4-layer { font-size: 9px; color: #72877b; background: #f0f5f1; border-radius: 5px; padding: 1px 5px; }
.p4-score b { font-size: 24px; color: #1f3d33; font-variant-numeric: tabular-nums; }
.p4-score small { color: #91a097; }
.p4-mature { display: flex; align-items: center; gap: 4px; }
.p4-mature b { font-size: 15px; color: #29433a; }
.p4-tiny { font-size: 9px; }
.p4-bands { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px; }
.p4-bands > div { background: #f5f8f6; border-radius: 5px; padding: 2px 4px; text-align: center; }
.p4-bands > div.zero { background: #fafaf9; opacity: .65; }
.p4-bands span { font-size: 9px; color: #8a998f; }
.p4-bands b { font-size: 11px; color: #365046; margin: 0 2px; }
.p4-bands small { font-size: 8px; color: #93a198; }
.p4-speed-grade { display: flex; align-items: center; gap: 3px; font-size: 13px; font-weight: 700; }
.p4-speed-grade b:first-child { font-size: 12px; }
.p4-pager { display: flex; justify-content: flex-end; padding: 8px 12px 2px; }
.p4-mobile-list { display: none; }
.p4-settings { max-height: 62vh; overflow: auto; padding-right: 4px; }
.p4-setting-section { margin-bottom: 16px; }
.p4-setting-section h4 { margin: 0 0 8px; font-size: 13px; color: #29433a; }
.p4-setting-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 12px; }
.p4-field label { display: flex; justify-content: space-between; font-size: 11px; color: #5e7368; margin-bottom: 3px; }
.p4-field label i { font-style: normal; font-size: 9px; color: #9aa79e; }
.p4-field small { display: block; font-size: 9px; color: #8c9b92; line-height: 1.35; margin-top: 2px; }
.p4-field :deep(.el-input-number) { width: 100%; }
.p4-weight-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 10px; padding-top: 9px; border-top: 1px dashed rgba(31,61,51,.08); }
.p4-time-range { display: flex; align-items: center; gap: 5px; }
.p4-time-range :deep(.el-date-editor) { width: 100%; }
.p4-detail-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.p4-detail-head h3 { margin: 0; font-size: 18px; color: #203730; }
.p4-detail-head p { margin: 3px 0 0; font-size: 11px; }
.p4-detail-actions { display: flex; gap: 6px; }
.p4-detail-tags { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 12px; }
.p4-detail-grid { display: grid; grid-template-columns: 1.15fr 1fr; gap: 9px; }
.p4-detail-score, .p4-detail-maturity { padding: 12px 14px; }
.p4-detail-score > span, .p4-detail-maturity > span { font-size: 11px; color: #74857c; display: block; }
.p4-detail-score > b { font-size: 34px; color: #1f3d33; line-height: 1.1; }
.p4-detail-maturity > b { font-size: 28px; color: #29433a; line-height: 1.1; }
.p4-detail-score small, .p4-detail-maturity small { display: block; font-size: 10px; color: #8a998f; margin: 4px 0 8px; }
.p4-bars > div { margin-top: 6px; }
.p4-bars label { font-size: 9px; color: #7a8c82; }
.p4-bars b { font-size: 11px; color: #365046; float: right; }
.p4-maturity-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.p4-maturity-grid > div { background: #f6f9f7; border-radius: 7px; padding: 5px 7px; }
.p4-maturity-grid span { display: block; font-size: 9px; color: #87958d; }
.p4-maturity-grid b { font-size: 12px; color: #29433a; }
.p4-detail-facts { display: grid; grid-template-columns: repeat(6, 1fr); gap: 6px; margin: 10px 0; }
.p4-detail-facts > div { background: rgba(240,246,242,.72); border-radius: 8px; padding: 6px 8px; }
.p4-detail-facts span { display: block; font-size: 9px; color: #87958d; }
.p4-detail-facts b { font-size: 13px; color: #29433a; }
.p4-reasons { display: flex; flex-wrap: wrap; gap: 5px; margin: 6px 0 10px; }
.p4-detail-section { margin-top: 14px; }
.p4-detail-section h4 { margin: 0 0 7px; font-size: 13px; color: #29433a; }
.p4-band-table, .p4-speed-history { background: #fbfdfc; border: 1px solid rgba(31,61,51,.07); border-radius: 9px; overflow: hidden; }
.p4-band-row { display: grid; grid-template-columns: 1.5fr .6fr .6fr .6fr .8fr .8fr; gap: 6px; padding: 5px 9px; font-size: 10px; color: #5f746a; border-bottom: 1px dashed rgba(31,61,51,.06); }
.p4-band-row.is-head, .p4-speed-row.is-head { background: #f1f6f2; font-size: 9px; color: #7d8e84; font-weight: 700; }
.p4-band-row span i { font-style: normal; color: #a0ada5; font-size: 8px; margin-left: 4px; }
.p4-daily-list { max-height: 210px; overflow: auto; }
.p4-daily-row { display: grid; grid-template-columns: 80px 56px 1.4fr auto 80px; gap: 6px; align-items: center; padding: 4px 2px; font-size: 10px; color: #5f746a; border-bottom: 1px dashed rgba(31,61,51,.06); }
.p4-daily-row b { color: #2f5a48; }
.p4-daily-row small { color: #8a998f; }
.p4-daily-bands { color: #9aa79e; font-size: 8px; }
.p4-empty { padding: 8px 0; font-size: 11px; }
.p4-speed-detail { display: grid; grid-template-columns: 1fr 1.3fr; gap: 10px; align-items: stretch; }
.p4-speed-big { background: #fbfdfc; border-radius: 9px; padding: 10px 12px; border: 1px solid rgba(31,61,51,.07); }
.p4-speed-big span { font-size: 22px; }
.p4-speed-big b { font-size: 26px; margin-left: 5px; }
.p4-speed-big small { display: block; font-size: 10px; color: #87958d; margin-top: 4px; }
.p4-speed-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }
.p4-speed-facts > div { background: #f6f9f7; border-radius: 7px; padding: 5px 7px; }
.p4-speed-facts span { display: block; font-size: 9px; color: #87958d; }
.p4-speed-facts b { font-size: 12px; color: #29433a; }
.p4-speed-row { display: grid; grid-template-columns: 1.1fr .7fr .9fr .7fr 1.4fr; gap: 5px; padding: 4px 8px; font-size: 10px; color: #5f746a; border-bottom: 1px dashed rgba(31,61,51,.06); }
.p4-relation { display: grid; grid-template-columns: repeat(3, 1fr); gap: 7px; }
.p4-relation > div { background: #f6f9f7; border-radius: 8px; padding: 7px 9px; }
.p4-relation span { display: block; font-size: 9px; color: #87958d; }
.p4-relation b { font-size: 13px; color: #29433a; }
.p4-relation small { display: block; font-size: 9px; color: #9aa79e; margin-top: 2px; }
@media (max-width: 1400px) {
  .p4-quality-grid { grid-template-columns: repeat(3, 1fr); }
  .p4-band-strip { grid-template-columns: 1fr 1fr; }
  .p4-speed-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 860px) {
  .p4-head { flex-direction: column; }
  .p4-head-actions { justify-content: flex-start; width: 100%; }
  .p4-quality-grid { grid-template-columns: 1fr 1fr; }
  .p4-stat b { font-size: 24px; }
  .p4-band-strip { grid-template-columns: 1fr 1fr; }
  .p4-speed-grid { grid-template-columns: 1fr 1fr 1fr; }
  .p4-table-card { display: none; }
  .p4-mobile-list { display: block; }
  .p4-count { width: 100%; margin-left: 0; }
  .p4-detail-grid { grid-template-columns: 1fr; }
  .p4-detail-facts { grid-template-columns: 1fr 1fr 1fr; }
  .p4-speed-detail { grid-template-columns: 1fr; }
  .p4-relation { grid-template-columns: 1fr; }
  .p4-setting-grid { grid-template-columns: 1fr; }
  .p4-weight-grid { grid-template-columns: 1fr 1fr 1fr; }
  .p4-band-row { grid-template-columns: 1.4fr .5fr .5fr .7fr .8fr; }
  .p4-band-row span:nth-child(4) { display: none; }
  .p4-speed-row { grid-template-columns: 1fr .7fr 1fr 1.2fr; }
  .p4-speed-row span:nth-child(4) { display: none; }
  .p4-daily-row { grid-template-columns: 1fr 1fr; }
  .p4-airport-add { margin-left: 0; }
}
.p4-mcard { padding: 10px 12px; margin-bottom: 8px; }
.p4-mcard-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
.p4-mcard-head b { font-size: 13px; color: #263f35; }
.p4-mcard-main { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 4px; margin: 7px 0; }
.p4-mcard-main > div { background: #f6f9f7; border-radius: 6px; padding: 4px 6px; }
.p4-mcard-main span { display: block; font-size: 8px; color: #8a998f; }
.p4-mcard-main b { font-size: 13px; color: #29433a; }
.p4-mbands { display: flex; gap: 8px; flex-wrap: wrap; font-size: 9px; color: #7d8e84; }
.p4-mcard-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 6px; font-size: 9px; }
</style>
