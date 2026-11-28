<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const summary = ref(null)
const items = ref([])
const total = ref(0)
const page = ref(1)
const perPage = 20
const category = ref('standard')
const filters = ref({ q: '', state: '' })
const loading = ref(false)
const busy = ref(false)
const detail = ref(null)
const drawer = ref(false)
const detailLoading = ref(false)
const detailCategory = ref('standard')
const settings = ref(null)
const settingsDialog = ref(false)
const settingsForm = ref({})
const saving = ref(false)

const STATE = {
  observation: { text: '观察', type: 'info', color: '#83928a' },
  candidate: { text: '候选', type: 'warning', color: '#b07d26' },
  preferred: { text: '优选', type: 'success', color: '#2f8f62' },
  eliminated: { text: '淘汰', type: 'danger', color: '#a34f4f' }
}
const SPEED = {
  excellent: { text: '优', emoji: '🟢', color: '#10B981' },
  good: { text: '良', emoji: '🔵', color: '#4682B4' },
  poor: { text: '差', emoji: '🟠', color: '#F59E0B' },
  danger: { text: '危', emoji: '🔴', color: '#EF4444' },
  empty: { text: '无数据', emoji: '⚪', color: '#AAB7B1' },
  excluded: { text: '机场排除', emoji: '⚪', color: '#AAB7B1' }
}
const DELAY = {
  excellent: { text: '优秀', type: 'success' }, good: { text: '良好', type: 'primary' },
  normal: { text: '正常', type: 'info' }, fair: { text: '较差', type: 'warning' },
  severe: { text: '严重', type: 'danger' }, fail: { text: '不合格', type: 'danger' },
  none: { text: '无样本', type: 'info' }
}
const REASON = {
  initial_observation: '首次进入竞争，进入观察',
  observation_period: '观察期未结束，继续积累数据',
  observation_not_mature: '观察期结束但数据未成熟，继续观察',
  observation_complete: '观察期完成且满足准入，进入候选',
  candidate_confirming: '候选达标，等待连续有效确认',
  below_candidate_score: '候选未达到晋级分数',
  mature_priority: '存在成熟优选，分差未达替换线，保留成熟节点',
  replacement_confirming: '已达到分差，等待第二次有效确认',
  candidate_promoted: '候选达到晋级线并完成连续确认，进入优选',
  candidate_replaced_mature: '新节点领先成熟节点达到分差并连续确认，替换进入优选',
  retained: '优选保留线达标，继续保留',
  retain_watch: '单次低于保留线，计入一次不达标但不降级',
  retain_failed: '连续不达标达到门槛，降级回观察并保留历史',
  replaced_by_new_node: '被领先达到分差的新节点替换，让出优选位置',
  speed_danger: '测速等级为危，禁止进入优选，退出当前竞争',
  business_expired: '业务资格过期，等待重新检测；不计失败',
  recovery_confirming: '恢复期连续有效成功确认中',
  recovery_confirmed: '恢复连续成功达到门槛，重新进入候选',
  source_inactive: 'P1 来源失活，退出当前竞争但不删除原始节点',
  source_readmitted: '来源恢复，回到观察等待重新准入',
  health_unknown: 'P2 健康未知，不按失败处理',
  health_unhealthy: 'P2 基础健康不健康',
  p80_missing: '缺少 TCP P80 样本',
  p80_over_500: 'TCP P80 超过 500ms',
  quality_missing: '缺少 P4 质量结果',
  quality_below_line: 'P4 质量分低于准入线',
  maturity_missing: '缺少数据成熟度',
  maturity_below_70: '数据成熟度低于 70%',
  stability_failed: '七天稳定性不合格',
  business_unknown: '业务资格状态未知',
  business_not_qualified: '业务资格不合格',
  business_suspended: '业务资格已暂停',
  no_speed_data: '无测速数据，允许竞争但未伪装速度合格',
  speed_below_minimum: '下载速度低于最低实用速度线',
  speed_ok: '下载速度达到最低实用速度线',
  duplicate_evidence: '同一份证据重复运行，不重复计数'
}
const CHECK = {
  source_inactive: 'P1 来源有效', airport_disabled: '机场启用',
  health_ok: 'P2 基础健康', health_unknown: 'P2 健康已知', health_unhealthy: 'P2 基础健康',
  p80_missing: 'TCP P80 有样本', p80_over_500: 'TCP P80 ≤ 500ms',
  quality_missing: 'P4 质量有结果', quality_below_line: 'P4 质量达到准入线',
  maturity_missing: '数据成熟度已知', maturity_below_70: '数据成熟度 ≥ 70%',
  stability_failed: '七天稳定性合格', speed_danger: '速度等级非危',
  no_speed_data: '无测速数据标记', speed_ok: '下载速度准入',
  speed_below_minimum: '速度不低于最低实用线',
  business_expired: '业务资格未过期', business_qualified: '业务资格有效',
  business_unknown: '业务资格已知', business_not_qualified: '业务资格有效',
  business_not_required: '标准综合不看业务资格'
}

const q = computed(() => summary.value || {})
const categories = computed(() => q.value.categories || [])
const activeCategory = computed(() => categories.value.find(c => c.code === category.value) || categories.value[0] || {})
const lastAt = computed(() => q.value.last_competition_at)
const stateOf = (v) => STATE[v] || STATE.observation
const speedOf = (v) => SPEED[v] || SPEED.empty
const delayOf = (v) => DELAY[v] || DELAY.none
const reasonText = (v) => REASON[v] || CHECK[v] || v || '—'
const fmt1 = (v) => (v === null || v === undefined || v === '' ? '—' : Number(v).toFixed(1))
const fmtSpeed = (v) => (v === null || v === undefined ? '—' : Number(v) >= 1 ? `${Number(v).toFixed(2)} MB/s` : `${Math.round(Number(v) * 1024)} KB/s`)
const shortTs = (ts) => { const t = formatTs(ts); return t === '—' ? t : t.slice(5, 16) }
const count = (c, state) => Number(c?.states?.[state] || 0)
const stateHistory = (row) => { const t = stateOf(row.state); return t.text }
const hasSpeed = (row) => row.has_speed_data !== false && row.speed_grade !== 'empty' && row.speed_grade !== 'excluded'

async function loadSummary() { summary.value = await api.get('/api/competition/summary') }
async function loadNodes() {
  const params = new URLSearchParams({ category: category.value, page: String(page.value), per_page: String(perPage) })
  if (filters.value.q) params.set('q', filters.value.q)
  if (filters.value.state) params.set('state', filters.value.state)
  const data = await api.get(`/api/competition/nodes?${params}`)
  items.value = data.items || []
  total.value = data.total || 0
}
async function loadAll() {
  loading.value = true
  try { await Promise.all([loadSummary(), loadNodes()]) }
  catch (e) { ElMessage.error(e.message || '竞争数据加载失败') }
  finally { loading.value = false }
}
async function setCategory(code) {
  category.value = code
  page.value = 1
  filters.value.state = ''
  await loadNodes()
}
function setFilter(key, value) {
  filters.value[key] = value
  page.value = 1
  loadNodes()
}
function pager(next) { page.value = next; loadNodes() }
async function runNow() {
  busy.value = true
  try {
    await ElMessageBox.confirm('将基于现有 P1–P4 数据执行一次六类竞争；同一份证据不会重复计数。是否继续？', '执行智能节点竞争', { type: 'warning' })
    const res = await api.post('/api/competition/run', { trigger: 'manual', refresh_p4: true })
    if (res.duplicate) ElMessage.warning('已有一轮竞争正在执行，本次已跳过')
    else ElMessage.success(`竞争完成：${res.nodes_scanned || 0} 个节点/类别，状态变化 ${res.changes || 0}`)
    await loadAll()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.message || '竞争执行失败')
  } finally { busy.value = false }
}
async function openDetail(row) {
  detailCategory.value = category.value
  drawer.value = true
  detailLoading.value = true
  detail.value = null
  try { detail.value = await api.get(`/api/competition/nodes/${row.node_id}`) }
  catch (e) { ElMessage.error(e.message || '节点竞争详情加载失败') }
  finally { detailLoading.value = false }
}
const detailState = computed(() => detail.value?.states?.[detailCategory.value] || null)
const detailCategoryTitle = computed(() => categories.value.find(c => c.code === detailCategory.value)?.title || detailCategory.value)
const detailHistory = computed(() => (detail.value?.history || []).filter(h => h.category === detailCategory.value))
async function openSettings() {
  settings.value = await api.get('/api/competition/settings')
  const cur = settings.value.current
  settingsForm.value = { ...cur }
  settingsDialog.value = true
}
async function saveSettings() {
  saving.value = true
  try {
    await api.patch('/api/competition/settings', settingsForm.value)
    settings.value = await api.get('/api/competition/settings')
    settingsForm.value = { ...settings.value.current }
    await loadAll()
    ElMessage.success('竞争参数已保存并通过范围校验')
  } catch (e) { ElMessage.error(e.message || '参数校验未通过') }
  finally { saving.value = false }
}
async function restoreDefaults() {
  try {
    await ElMessageBox.confirm('将六项竞争参数恢复默认值，是否继续？', '恢复竞争默认参数', { type: 'warning' })
    const res = await api.post('/api/competition/settings/defaults', {})
    settings.value = res
    settingsForm.value = { ...res.current }
    await loadAll()
    ElMessage.success('已恢复默认竞争参数')
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message || '恢复默认失败') }
}
onMounted(loadAll)
</script>

<template>
  <div v-loading="loading" class="np-page p5-page">
    <div class="p5-head">
      <div>
        <h2 class="np-page-title">智能节点竞争</h2>
      </div>
      <div class="p5-head-actions">
        <el-button size="small" @click="openSettings">竞争参数</el-button>
        <el-button size="small" type="primary" :loading="busy" @click="runNow">执行竞争</el-button>
      </div>
    </div>

    <div class="p5-overview">
      <button class="p5-stat is-good"><span>标准综合优选<i>PREFERRED</i></span><b>{{ q.preferred_standard || 0 }}</b><small>成熟保留 + 新节点替换后的标准名单</small></button>
      <button class="p5-stat" @click="setCategory('gpt')"><span>五类业务优选<i>BUSINESS</i></span><b>{{ Object.values(q.preferred_business || {}).reduce((a, b) => a + Number(b || 0), 0) }}</b><small>GPT · YouTube · Telegram · GitHub · 国外综合</small></button>
      <button class="p5-stat is-warn" @click="setFilter('state', 'candidate')"><span>候选总数<i>CANDIDATE</i></span><b>{{ q.candidate_total || 0 }}</b><small>跨六类累计的候选状态数</small></button>
      <button class="p5-stat" @click="setFilter('state', 'observation')"><span>观察总数<i>OBSERVATION</i></span><b>{{ q.observation_total || 0 }}</b><small>含新节点、数据不足与恢复确认</small></button>
      <button class="p5-stat is-bad" @click="setFilter('state', 'eliminated')"><span>淘汰总数<i>ELIMINATED</i></span><b>{{ q.eliminated_total || 0 }}</b><small>只退出该类别，P1 原始节点仍保留</small></button>
      <button class="p5-stat is-note"><span>最近竞争<i>LAST RUN</i></span><b class="p5-last">{{ shortTs(lastAt) }}</b><small>{{ q.last_run ? `触发 ${q.last_run.trigger === 'auto' ? '自动' : '手动'} · 变化 ${q.last_run.changes || 0}` : '尚未执行竞争' }}</small></button>
    </div>

    <div class="p5-cat-strip">
      <button v-for="c in categories" :key="c.code" :class="['p5-cat', { active: c.code === category }]" @click="setCategory(c.code)">
        <div class="p5-cat-top"><b>{{ c.title }}</b><i>{{ c.short }}</i></div>
        <div class="p5-cat-counts">
          <span class="obs">观察 <b>{{ count(c, 'observation') }}</b></span>
          <span class="cand">候选 <b>{{ count(c, 'candidate') }}</b></span>
          <span class="pref">优选 <b>{{ count(c, 'preferred') }}</b></span>
          <span class="elim">淘汰 <b>{{ count(c, 'eliminated') }}</b></span>
        </div>
      </button>
    </div>

    <div class="np-card p5-rule-line">
      <div><span>当前类别</span><b>{{ activeCategory.title }}</b></div>
      <div><span>准入</span><b>P2 健康 · P80 ≤500ms · P4 ≥75 · 成熟度 ≥70%</b></div>
      <div><span>优选保留</span><b>≥{{ q.settings?.retain_score || 70 }} 分；连续 {{ q.settings?.failure_limit || 3 }} 次不达标才降级</b></div>
      <div><span>替换</span><b>新节点须领先 ≥{{ q.settings?.replace_gap || 8 }} 分并连续两次确认</b></div>
    </div>

    <div class="p5-toolbar">
      <el-input v-model="filters.q" size="small" clearable placeholder="搜索节点、机场或原因" style="width: 240px" @keyup.enter="setFilter('q', filters.q)" @clear="setFilter('q', '')" />
      <el-select v-model="filters.state" size="small" clearable placeholder="全部状态" style="width: 130px" @change="setFilter('state', filters.state)">
        <el-option label="观察" value="observation" />
        <el-option label="候选" value="candidate" />
        <el-option label="优选" value="preferred" />
        <el-option label="淘汰" value="eliminated" />
      </el-select>
      <span class="np-muted p5-count">当前类别 {{ total }} 个节点/状态 · 点击行查看六类详情与竞争历史</span>
    </div>

    <div class="np-card p5-table-card">
      <el-table :data="items" size="small" row-key="node_id" @row-click="openDetail">
        <el-table-column label="节点 / 机场" min-width="190">
          <template #default="{ row }">
            <div class="p5-node-name">{{ row.node_name }}</div>
            <div class="np-muted p5-tiny">{{ row.airport_name || '未归属机场' }} · {{ row.server }}:{{ row.port }}</div>
            <div class="p5-node-tags"><span class="p5-layer">{{ row.management_layer || 'raw' }}</span></div>
          </template>
        </el-table-column>
        <el-table-column label="质量分" width="90">
          <template #default="{ row }"><b class="p5-score">{{ fmt1(row.score) }}</b><div class="np-muted p5-tiny">P4 七天</div></template>
        </el-table-column>
        <el-table-column label="成熟度" width="92">
          <template #default="{ row }"><b>{{ fmt1(row.maturity) }}%</b><div class="np-muted p5-tiny">数据成熟</div></template>
        </el-table-column>
        <el-table-column label="延迟" width="100">
          <template #default="{ row }"><el-tag size="small" :type="delayOf(row.delay_grade).type" effect="plain">{{ delayOf(row.delay_grade).text }}</el-tag><div class="np-muted p5-tiny">{{ row.p80_ms === null ? '—' : Math.round(row.p80_ms) }} ms · P80</div></template>
        </el-table-column>
        <el-table-column label="下载速度" width="126">
          <template #default="{ row }">
            <div v-if="hasSpeed(row)" class="p5-speed" :style="{ color: speedOf(row.speed_grade).color }"><b>{{ speedOf(row.speed_grade).emoji }}</b><b>{{ fmtSpeed(row.speed_mb_s) }}</b></div>
            <el-tag v-else size="small" type="info" effect="plain">无数据</el-tag>
            <div class="np-muted p5-tiny">{{ speedOf(row.speed_grade).text }} · {{ hasSpeed(row) ? '已测速' : '允许竞争' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="108">
          <template #default="{ row }"><el-tag size="small" :type="stateOf(row.state).type" effect="light">{{ stateOf(row.state).text }}</el-tag><div v-if="row.consecutive_failures" class="np-muted p5-tiny">不达标 {{ row.consecutive_failures }}</div><div v-else-if="row.recovery_required" class="np-muted p5-tiny">恢复确认中</div></template>
        </el-table-column>
        <el-table-column label="最近竞争" width="116">
          <template #default="{ row }"><span class="p5-tiny">{{ shortTs(row.last_evaluated_at || row.last_change_at) }}</span><div class="np-muted p5-tiny">变化 {{ shortTs(row.last_change_at) }}</div></template>
        </el-table-column>
        <el-table-column label="原因" min-width="190">
          <template #default="{ row }"><span class="p5-reason">{{ reasonText(row.last_reason) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="70">
          <template #default="{ row }"><el-button link type="primary" size="small" @click.stop="openDetail(row)">详情</el-button></template>
        </el-table-column>
      </el-table>
      <div class="p5-pager"><el-pagination :current-page="page" :page-size="perPage" :total="total" layout="prev, pager, next" small @current-change="pager" /></div>
    </div>

    <div class="p5-mobile-list">
      <div v-for="row in items" :key="row.node_id" class="np-card p5-mcard" @click="openDetail(row)">
        <div class="p5-mcard-head">
          <div><b>{{ row.node_name }}</b><div class="np-muted p5-tiny">{{ row.airport_name }} · {{ row.server }}:{{ row.port }}</div></div>
          <el-tag size="small" :type="stateOf(row.state).type" effect="light">{{ stateOf(row.state).text }}</el-tag>
        </div>
        <div class="p5-mcard-main">
          <div><span>质量分</span><b>{{ fmt1(row.score) }}</b></div>
          <div><span>成熟度</span><b>{{ fmt1(row.maturity) }}%</b></div>
          <div><span>延迟</span><b>{{ delayOf(row.delay_grade).text }}</b></div>
          <div><span>速度</span><b :style="{ color: speedOf(row.speed_grade).color }">{{ hasSpeed(row) ? speedOf(row.speed_grade).emoji + ' ' + fmtSpeed(row.speed_mb_s) : '无数据' }}</b></div>
        </div>
        <div class="p5-mcard-reason">{{ reasonText(row.last_reason) }}</div>
        <div class="p5-mcard-foot"><span>{{ shortTs(row.last_evaluated_at) }}</span><span>{{ row.p80_ms === null ? '—' : Math.round(row.p80_ms) }} ms</span></div>
      </div>
      <el-pagination :current-page="page" :page-size="perPage" :total="total" layout="prev, pager, next" small @current-change="pager" />
    </div>

    <el-dialog v-model="settingsDialog" title="智能节点竞争参数" width="min(720px, 94vw)">
      <div v-if="settings" class="p5-settings">
        <div class="p5-setting-note">参数范围由后端校验，保存后配置版本提升；不会改变 P1–P4 已验收规则。</div>
        <div class="p5-setting-grid">
          <div v-for="f in settings.fields" :key="f.key" class="p5-field">
            <label>{{ f.label }}<i>{{ f.default }} 默认 · 范围 {{ f.min }}–{{ f.max }}{{ f.unit }}</i></label>
            <el-input-number v-model="settingsForm[f.key]" :min="f.min" :max="f.max" size="small" controls-position="right" />
            <small>{{ f.description }}</small>
          </div>
        </div>
        <div class="p5-setting-actions"><el-button size="small" @click="restoreDefaults">恢复默认</el-button><span class="np-muted">配置版本 {{ settings.current.config_version }}</span></div>
        <div class="p5-setting-boundary">
          <b>本批边界</b>：只做竞争状态与历史；不生成 P6 订阅结果，不做关键词过滤，不进入 P7 设置中心，不增加参考节点或目标投票。
        </div>
      </div>
      <template #footer><el-button @click="settingsDialog = false">关闭</el-button><el-button type="primary" :loading="saving" @click="saveSettings">保存并校验</el-button></template>
    </el-dialog>

    <el-drawer v-model="drawer" class="p5-drawer" size="min(960px, 96vw)" title="节点竞争详情">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="p5-detail-head">
            <div><h3>{{ detail.node.name || detail.node.current_name }}</h3><p class="np-muted">{{ detail.node.airport_name || '未归属机场' }} · {{ detail.node.server }}:{{ detail.node.port }}</p></div>
            <el-tag effect="plain" :type="detail.node.source_active ? 'success' : 'danger'">{{ detail.node.source_active ? 'P1 原始节点保留' : 'P1 来源失活' }}</el-tag>
          </div>
          <div class="p5-detail-tags">
            <el-tag effect="plain">P2 {{ detail.node.base_health || 'unknown' }}</el-tag>
            <el-tag v-for="c in categories" :key="c.code" effect="plain" :type="stateOf((detail.states[c.code] || {}).state).type">
              {{ c.short }} {{ stateOf((detail.states[c.code] || {}).state).text }}
            </el-tag>
          </div>
          <div class="p5-cat-strip is-detail">
            <button v-for="c in categories" :key="c.code" :class="['p5-cat', { active: c.code === detailCategory }]" @click="detailCategory = c.code">
              <div class="p5-cat-top"><b>{{ c.short }}</b><i>{{ stateOf((detail.states[c.code] || {}).state).text }}</i></div>
              <div class="p5-cat-counts"><span>质量 {{ fmt1((detail.states[c.code] || {}).score) }}</span><span>成熟度 {{ fmt1((detail.states[c.code] || {}).maturity) }}%</span></div>
            </button>
          </div>
          <div v-if="detailState" class="p5-detail-grid">
            <div class="np-card p5-detail-state">
              <span>当前状态</span><b :style="{ color: stateOf(detailState.state).color }">{{ stateOf(detailState.state).text }}</b>
              <small>{{ reasonText(detailState.last_reason) }}</small>
              <div class="p5-detail-facts">
                <div><span>质量分</span><b>{{ fmt1(detailState.score) }}</b></div>
                <div><span>成熟度</span><b>{{ fmt1(detailState.maturity) }}%</b></div>
                <div><span>P80</span><b>{{ detailState.p80_ms === null ? '—' : Math.round(detailState.p80_ms) }} ms</b></div>
                <div><span>速度</span><b :style="{ color: speedOf(detailState.speed_grade).color }">{{ speedOf(detailState.speed_grade).text }}</b></div>
                <div><span>连续不达标</span><b>{{ detailState.consecutive_failures || 0 }} / {{ settings?.current?.failure_limit || 3 }}</b></div>
                <div><span>连续有效成功</span><b>{{ detailState.consecutive_successes || 0 }}</b></div>
              </div>
              <div class="p5-reason-box"><b>最近原因</b><p>{{ reasonText(detailState.last_reason) }}</p></div>
            </div>
            <div class="np-card p5-detail-admission">
              <span>准入检查</span>
              <div v-for="chk in (detailState.admission?.checks || [])" :key="chk.code" class="p5-check-row" :class="{ fail: !chk.ok, pending: !chk.ok && chk.severity === 'pending' }">
                <i>{{ chk.ok ? '✓' : chk.severity === 'pending' ? '…' : '×' }}</i><b>{{ CHECK[chk.code] || chk.code }}</b><small>{{ chk.ok ? (chk.severity === 'warning' ? '允许 · 已标记' : chk.severity === 'info' ? '不适用' : '通过') : reasonText(chk.code) }}</small>
              </div>
              <div v-if="!(detailState.admission?.checks || []).length" class="np-muted p5-empty">暂无准入检查快照。</div>
            </div>
          </div>
          <div class="p5-detail-section">
            <h4>竞争历史 · {{ detailCategoryTitle }}</h4>
            <div class="p5-history">
              <div class="p5-hrow is-head"><span>时间</span><span>变化</span><span>分数</span><span>原因</span><span>替换</span></div>
              <div v-for="h in detailHistory" :key="h.id" class="p5-hrow">
                <span>{{ shortTs(h.at) }}</span>
                <span>{{ stateOf(h.from_state).text }} → {{ stateOf(h.to_state).text }}</span>
                <span>{{ fmt1(h.before_score) }} → {{ fmt1(h.after_score) }}</span>
                <span>{{ reasonText(h.reason) }}</span>
                <span>{{ h.replaced_node_id ? `#${h.replaced_node_id}` : '—' }}</span>
              </div>
              <div v-if="!detailHistory.length" class="np-muted p5-empty">当前类别暂无状态变化历史。</div>
            </div>
          </div>
          <div class="p5-detail-section">
            <h4>边界说明</h4>
            <div class="p5-boundary">竞争只读写 P5 独立状态与历史；P1 原始节点、P2 健康、P3 业务资格、P4 质量/成熟度/速度均只读。淘汰只退出当前类别，不删除 P1 节点。</div>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.p5-page { max-width: 1600px; }
.p5-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 14px; }
.p5-head .np-page-desc { margin: 5px 0 0; max-width: 850px; }
.p5-head-actions { display: flex; gap: 7px; flex-wrap: wrap; justify-content: flex-end; }
.p5-overview { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 9px; margin-bottom: 10px; }
.p5-stat { text-align: left; background: var(--np-panel-solid); border: 1px solid rgba(31,61,51,.09); border-radius: 13px; padding: 12px 13px 10px; border-top: 3px solid #7e9c8d; cursor: pointer; }
.p5-stat:hover { box-shadow: 0 8px 20px rgba(31,61,51,.08); transform: translateY(-1px); }
.p5-stat.is-good { border-top-color: #2f8f62; }
.p5-stat.is-warn { border-top-color: #c79a35; }
.p5-stat.is-bad { border-top-color: #b04b4b; }
.p5-stat.is-note { border-top-color: #748aa0; cursor: default; }
.p5-stat span { display: flex; justify-content: space-between; font-size: 12px; color: #6f8378; }
.p5-stat span i { font-style: normal; font-size: 9px; letter-spacing: .04em; color: #9aa79e; }
.p5-stat b { display: block; font-size: 27px; color: #1c332b; line-height: 1.15; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.p5-stat small { font-size: 10px; color: #8a998f; }
.p5-stat .p5-last { font-size: 18px; margin-top: 8px; }
.p5-cat-strip { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; margin-bottom: 12px; }
.p5-cat { text-align: left; background: rgba(240,246,242,.72); border: 1px solid rgba(31,61,51,.09); border-radius: 11px; padding: 9px 10px; cursor: pointer; transition: .15s; }
.p5-cat.active { background: #eaf3ec; border-color: rgba(47,143,98,.42); box-shadow: 0 4px 14px rgba(47,143,98,.10); }
.p5-cat-top { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
.p5-cat-top b { font-size: 12px; color: #29433a; }
.p5-cat-top i { font-style: normal; font-size: 8px; color: #8a998f; letter-spacing: .03em; }
.p5-cat-counts { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 5px; font-size: 9px; color: #75877d; }
.p5-cat-counts b { color: #29433a; font-size: 11px; }
.p5-cat-counts .pref b { color: #2f8f62; }
.p5-cat-counts .cand b { color: #b07d26; }
.p5-rule-line { display: grid; grid-template-columns: 1.15fr 2.3fr 2.1fr 2.2fr; gap: 10px; padding: 9px 12px; margin-bottom: 10px; }
.p5-rule-line > div { border-left: 2px solid rgba(47,143,98,.35); padding-left: 8px; }
.p5-rule-line span { display: block; font-size: 9px; color: #8a998f; }
.p5-rule-line b { font-size: 10px; color: #385046; font-weight: 600; }
.p5-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.p5-count { margin-left: auto; font-size: 11px; }
.p5-table-card { padding: 0 0 6px; }
.p5-node-name { font-size: 12px; font-weight: 700; color: #263f35; }
.p5-tiny { font-size: 9px; }
.p5-node-tags { display: flex; gap: 5px; margin-top: 3px; }
.p5-layer { font-size: 9px; color: #72877b; background: #f0f5f1; border-radius: 5px; padding: 1px 5px; }
.p5-score { font-size: 22px; color: #1f3d33; font-variant-numeric: tabular-nums; }
.p5-speed { display: flex; align-items: center; gap: 4px; font-size: 13px; font-weight: 700; }
.p5-reason { font-size: 10px; color: #60756a; line-height: 1.35; }
.p5-pager { display: flex; justify-content: flex-end; padding: 8px 12px 2px; }
.p5-mobile-list { display: none; }
.p5-settings { max-height: 66vh; overflow: auto; padding-right: 3px; }
.p5-setting-note { font-size: 11px; color: #71857a; background: #f4f8f5; border-radius: 8px; padding: 8px 10px; margin-bottom: 11px; }
.p5-setting-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.p5-field label { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; color: #5e7368; margin-bottom: 3px; }
.p5-field label i { font-style: normal; font-size: 9px; color: #9aa79e; text-align: right; }
.p5-field small { display: block; font-size: 9px; color: #8c9b92; line-height: 1.4; margin-top: 2px; }
.p5-field :deep(.el-input-number) { width: 100%; }
.p5-setting-actions { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
.p5-setting-actions span { margin-left: auto; font-size: 10px; }
.p5-setting-boundary { margin-top: 12px; padding: 9px 10px; border-radius: 8px; background: #fbf7ef; font-size: 10px; color: #7c6a45; line-height: 1.5; }
.p5-detail-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.p5-detail-head h3 { margin: 0; font-size: 18px; color: #203730; }
.p5-detail-head p { margin: 3px 0 0; font-size: 11px; }
.p5-detail-tags { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 12px; }
.p5-cat-strip.is-detail { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-bottom: 12px; }
.p5-cat-strip.is-detail .p5-cat { padding: 7px 8px; }
.p5-cat-strip.is-detail .p5-cat-counts { font-size: 8px; gap: 4px; }
.p5-detail-grid { display: grid; grid-template-columns: 1.1fr 1fr; gap: 9px; }
.p5-detail-state, .p5-detail-admission { padding: 12px 14px; }
.p5-detail-state > span, .p5-detail-admission > span { display: block; font-size: 10px; color: #809188; }
.p5-detail-state > b { display: block; font-size: 28px; line-height: 1.15; margin: 3px 0 4px; }
.p5-detail-state > small { display: block; font-size: 10px; color: #72877b; line-height: 1.4; }
.p5-detail-facts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; margin-top: 10px; }
.p5-detail-facts > div { background: #f6f9f7; border-radius: 7px; padding: 5px 7px; }
.p5-detail-facts span { display: block; font-size: 8px; color: #87958d; }
.p5-detail-facts b { font-size: 12px; color: #29433a; }
.p5-reason-box { margin-top: 10px; background: #fbfdfc; border: 1px dashed rgba(31,61,51,.08); border-radius: 7px; padding: 6px 8px; }
.p5-reason-box b { font-size: 9px; color: #87958d; }
.p5-reason-box p { margin: 2px 0 0; font-size: 10px; color: #50685b; line-height: 1.4; }
.p5-check-row { display: grid; grid-template-columns: 16px 1.2fr 1.1fr; align-items: center; gap: 5px; padding: 5px 0; border-bottom: 1px dashed rgba(31,61,51,.06); font-size: 10px; color: #52695d; }
.p5-check-row i { font-style: normal; color: #2f8f62; font-weight: 700; }
.p5-check-row.fail i { color: #b04b4b; }
.p5-check-row.pending i { color: #b07d26; }
.p5-check-row b { font-size: 10px; color: #334c40; }
.p5-check-row small { font-size: 9px; color: #8a998f; }
.p5-empty { font-size: 11px; padding: 8px 0; }
.p5-detail-section { margin-top: 14px; }
.p5-detail-section h4 { margin: 0 0 7px; font-size: 13px; color: #29433a; }
.p5-history { background: #fbfdfc; border: 1px solid rgba(31,61,51,.07); border-radius: 9px; overflow: hidden; }
.p5-hrow { display: grid; grid-template-columns: .9fr 1.15fr .9fr 1.8fr .6fr; gap: 5px; padding: 5px 8px; font-size: 9px; color: #5f746a; border-bottom: 1px dashed rgba(31,61,51,.06); }
.p5-hrow.is-head { background: #f1f6f2; font-size: 8px; color: #7d8e84; font-weight: 700; }
.p5-boundary { padding: 9px 10px; border-radius: 8px; background: #f4f8f5; font-size: 10px; color: #61776b; line-height: 1.5; }
.p5-mcard { padding: 10px 12px; margin-bottom: 8px; }
.p5-mcard-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
.p5-mcard-head b { font-size: 13px; color: #263f35; }
.p5-mcard-main { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 4px; margin: 7px 0; }
.p5-mcard-main > div { background: #f6f9f7; border-radius: 6px; padding: 4px 6px; }
.p5-mcard-main span { display: block; font-size: 8px; color: #8a998f; }
.p5-mcard-main b { font-size: 12px; color: #29433a; }
.p5-mcard-reason { font-size: 10px; color: #61776b; line-height: 1.4; margin: 4px 0; }
.p5-mcard-foot { display: flex; justify-content: space-between; font-size: 9px; color: #8a998f; }
@media (max-width: 1400px) {
  .p5-overview { grid-template-columns: repeat(3, 1fr); }
  .p5-cat-strip { grid-template-columns: repeat(3, 1fr); }
  .p5-rule-line { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 860px) {
  .p5-head { flex-direction: column; }
  .p5-head-actions { justify-content: flex-start; width: 100%; }
  .p5-overview { grid-template-columns: 1fr 1fr; }
  .p5-stat b { font-size: 24px; }
  .p5-cat-strip, .p5-cat-strip.is-detail { grid-template-columns: 1fr 1fr; }
  .p5-rule-line { grid-template-columns: 1fr; }
  .p5-table-card { display: none; }
  .p5-mobile-list { display: block; }
  .p5-count { width: 100%; margin-left: 0; }
  .p5-detail-grid { grid-template-columns: 1fr; }
  .p5-setting-grid { grid-template-columns: 1fr; }
  .p5-hrow { grid-template-columns: .8fr 1fr 1.3fr 1.5fr; }
  .p5-hrow span:nth-child(5) { display: none; }
  .p5-cat-strip.is-detail { grid-template-columns: 1fr 1fr; gap: 6px; }
}
</style>
