<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const summary = ref(null)
const targets = ref([])
const items = ref([])
const total = ref(0)
const page = ref(1)
const perPage = 20
const loading = ref(false)
const filters = ref({ q: '', business: '', state: '' })
const detail = ref(null)
const drawer = ref(false)
const detailLoading = ref(false)
const targetDialog = ref(false)
const targetForm = ref({})
const targetBusy = ref(false)
const settings = ref(null)
const settingsDialog = ref(false)
const settingsForm = ref({ interval_minutes: 360, valid_hours: 24, concurrency: 10 })

const BUSINESS_LABELS = {
  gpt: 'GPT', youtube: 'YouTube', telegram: 'Telegram', github: 'GitHub', global: '国外综合'
}
const BUSINESS_HINTS = {
  gpt: 'ChatGPT / OpenAI 入口', youtube: 'YouTube 视频与 204 入口',
  telegram: 'Telegram 官网入口', github: 'GitHub 静态内容', global: 'Cloudflare 综合出口'
}
const STATE = {
  untested: { text: '未检测', type: 'info' },
  unknown: { text: 'Unknown', type: 'info' },
  pending: { text: '待确认', type: 'warning' },
  qualified: { text: '已获得资格', type: 'success' },
  suspended: { text: '暂停资格', type: 'danger' }
}
const CLASS = {
  success: { text: 'Success', type: 'success' },
  failure: { text: 'Failure', type: 'danger' },
  unknown: { text: 'Unknown', type: 'info' }
}
const BASE = {
  healthy: { text: '健康', type: 'success' },
  unhealthy: { text: '不健康', type: 'danger' },
  unknown: { text: '未知', type: 'info' }
}
const EVENT_TEXT = {
  qualification_granted: '获得资格', qualification_suspended: '暂停资格',
  qualification_recovered: '恢复资格', business_unknown: '结果 Unknown',
  check_discarded: '配置变更丢弃'
}
const REASON_TEXT = {
  business_http_mismatch: 'HTTP 状态不在期望范围', target_disabled: '业务目标已停用',
engine_unavailable: '代理内核未就绪',
  probe_result_missing: '缺少检测结果',
  batch_aborted: '检测批次中断，已释放队列', config_changed: '检测目标配置已变更',
  timeout: '请求超时，责任方不明', dns_timeout: 'DNS 超时，责任方不明',
  tcp_timeout: 'TCP 超时，责任方不明', tls_timeout: 'TLS 超时，责任方不明',
  https_timeout: 'HTTPS 超时，责任方不明', dns_failed: 'DNS 解析失败',
  tcp_refused: 'TCP 连接被拒绝', tcp_failed: 'TCP 连接失败',
  tls_failed: 'TLS 握手失败', https_failed: 'HTTPS 请求失败',
  'success': '业务访问成功', 'pending': '等待下一次有效结果',
  'suspended': '连续 3 次有效失败'
}
const shortTs = (ts) => { const t = formatTs(ts); return t === '—' ? t : t.slice(5, 16) }
const longTs = (ts) => formatTs(ts)
const stateOf = (v) => STATE[v] || { text: v || '—', type: 'info' }
const classOf = (v) => CLASS[v] || { text: v || '—', type: 'info' }
const baseOf = (v) => BASE[v] || { text: v || '—', type: 'info' }
const reasonText = (v) => REASON_TEXT[v] || v || '—'
const expectedText = (v) => (v || '200-399').replace(/,/g, '、')

const bizList = computed(() => summary.value?.businesses || [])
const queueTotal = computed(() => summary.value?.queue?.total || 0)
const targetEnabled = computed(() => targets.value.filter(t => t.enabled).length)

function bizStatus(st) {
  if (!st) return { text: '未检测', type: 'info' }
  if (st.target_enabled === false) return { text: '目标停用', type: 'info' }
  if (st.state === 'qualified' && st.expired) return { text: '资格过期', type: 'warning' }
  if (st.state === 'qualified' && st.qualification_active) return { text: '已获得资格', type: 'success' }
  if (st.state === 'untested' && st.checking) return { text: '检测中', type: 'warning' }
  return stateOf(st.state)
}
function lastRunOf(row) {
  const times = Object.values(row.businesses || {}).map(b => b.last_run_at).filter(Boolean)
  return times.length ? shortTs(times.sort().slice(-1)[0]) : '—'
}
function setBusinessFilter(code) {
  filters.value.business = filters.value.business === code ? '' : code
  page.value = 1
  loadNodes()
}
function targetName(code) {
  const t = targets.value.find(x => x.business === code)
  return t?.name || BUSINESS_LABELS[code]
}

async function loadSummary() {
  summary.value = await api.get('/api/business/summary')
  settings.value = summary.value.settings
}
async function loadTargets() {
  targets.value = (await api.get('/api/business/targets')).items || []
}
async function loadNodes() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    Object.entries(filters.value).forEach(([k, v]) => {
      if (v !== '' && v != null) params.set(k, v)
    })
    params.set('page', String(page.value))
    params.set('per_page', String(perPage))
    const res = await api.get(`/api/business/nodes?${params.toString()}`)
    items.value = res.items || []
    total.value = res.total || 0
  } finally { loading.value = false }
}
async function reload() {
  await Promise.all([loadSummary(), loadTargets(), loadNodes()])
}
async function openDetail(row) {
  drawer.value = true
  detailLoading.value = true
  detail.value = null
  try { detail.value = await api.get(`/api/business/nodes/${row.id}`) }
  catch (e) { ElMessage.error(e.message) }
  finally { detailLoading.value = false }
}
async function checkNode(node, business = '') {
  try {
    const payload = business ? { business } : {}
    const res = await api.post(`/api/business/nodes/${node.id}/check`, payload)
    ElMessage.success(res.queued
      ? `已加入 ${business ? BUSINESS_LABELS[business] : '五类业务'} 队列`
      : '该节点对应业务已在队列中，不会重复排队')
    await loadSummary()
    await loadNodes()
    if (detail.value?.node?.id === node.id) detail.value = await api.get(`/api/business/nodes/${node.id}`)
  } catch (e) { ElMessage.error(e.message) }
}
async function checkAll() {
  try {
    const res = await api.post('/api/business/check', {})
    ElMessage.success(`已入队 ${res.queued} 个业务任务；同一节点同一业务不会重复排队`)
    await reload()
  } catch (e) { ElMessage.error(e.message) }
}
function openTarget(t) {
  targetForm.value = {
    business: t.business, name: t.name, url: t.url,
    expected_status: t.expected_status || '200-399',
    interval_minutes: t.interval_minutes || 0, enabled: t.enabled
  }
  targetDialog.value = true
}
async function saveTarget() {
  if (!targetForm.value.name?.trim() || !targetForm.value.url?.trim()) {
    ElMessage.warning('名称和地址不能为空'); return
  }
  targetBusy.value = true
  try {
    await api.patch(`/api/business/targets/${targetForm.value.business}`, {
      name: targetForm.value.name.trim(), url: targetForm.value.url.trim(),
      expected_status: targetForm.value.expected_status,
      interval_minutes: Number(targetForm.value.interval_minutes || 0),
      enabled: targetForm.value.enabled,
    })
    targetDialog.value = false
    ElMessage.success('业务目标已保存；技术配置变更只会重置该业务结论与资格')
    await reload()
  } catch (e) { ElMessage.error(e.message) } finally { targetBusy.value = false }
}
async function toggleTarget(t, value) {
  try {
    await api.patch(`/api/business/targets/${t.business}`, { enabled: value })
    ElMessage.success(value ? `${BUSINESS_LABELS[t.business]} 目标已启用` : `${BUSINESS_LABELS[t.business]} 目标已停用`)
    await reload()
  } catch (e) { ElMessage.error(e.message); await loadTargets() }
}
async function restoreDefaults() {
  try {
    await ElMessageBox.confirm(
      '恢复 GPT / YouTube / Telegram / GitHub / 国外综合 五个默认目标？自定义地址会被覆盖，仅重置对应业务结论。',
      '恢复默认目标', { type: 'warning' })
    await api.post('/api/business/targets/defaults', {})
    ElMessage.success('已恢复五类业务默认目标')
    await reload()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}
async function openSettings() {
  settings.value = await api.get('/api/business/settings')
  settingsForm.value = {
    interval_minutes: settings.value.interval_minutes,
    valid_hours: settings.value.valid_hours,
    concurrency: settings.value.concurrency
  }
  settingsDialog.value = true
}
async function saveSettings() {
  try {
    settings.value = await api.patch('/api/business/settings', settingsForm.value)
    settingsDialog.value = false
    ElMessage.success('业务检测节奏已保存')
    await loadSummary()
  } catch (e) { ElMessage.error(e.message) }
}
onMounted(reload)
</script>

<template>
  <div v-loading="loading" class="np-page p3-page">
    <div class="p3-head">
      <div>
        <h2 class="np-page-title">业务专项检测</h2>
      </div>
      <div class="p3-head-actions">
        <el-button size="small" @click="openSettings">检测设置</el-button>
        <el-button size="small" type="primary" @click="checkAll">检测全部业务</el-button>
        <el-button size="small" @click="reload">刷新</el-button>
      </div>
    </div>

    <div class="p3-biz-stats">
      <button v-for="b in bizList" :key="b.code" class="p3-stat" :class="[`is-${b.code}`, { active: filters.business === b.code }]"
              @click="setBusinessFilter(b.code)">
        <span>{{ b.label }}<i>{{ b.short }}</i></span>
        <b>{{ b.stats.qualified }}</b>
        <small>有资格节点 · 待确认 {{ b.stats.pending }} · Unknown {{ b.stats.unknown }}</small>
        <em>{{ b.target?.enabled ? '目标已启用' : '目标已停用' }}</em>
      </button>
    </div>

    <div class="p3-mini-stats">
      <div><span>五类合计有资格</span><b>{{ summary?.qualified_nodes || 0 }}</b><small>至少一门业务有效</small></div>
      <div><span>待确认</span><b>{{ summary?.pending_nodes || 0 }}</b><small>有效成功或失败不足阈值</small></div>
      <div><span>Unknown</span><b>{{ summary?.unknown_nodes || 0 }}</b><small>检测异常或责任不明，不计失败</small></div>
      <div><span>检测队列</span><b>{{ queueTotal }}</b><small>排队 {{ summary?.queue?.pending || 0 }} · 执行 {{ summary?.queue?.running || 0 }}</small></div>
    </div>

    <div class="np-card p3-target-card">
      <div class="p3-section-head">
        <div>
          <h3>业务检测目标</h3>
          <p class="np-muted">启用 {{ targetEnabled }} / 5 · 每个业务只保留一个目标；地址、期望状态码、启用状态或单业务周期变化只重置该业务。</p>
        </div>
        <el-button size="small" @click="restoreDefaults">恢复默认</el-button>
      </div>
      <div class="p3-target-grid">
        <div v-for="t in targets" :key="t.business" class="p3-target" :class="{ off: !t.enabled }">
          <div class="p3-target-head">
            <b>{{ BUSINESS_LABELS[t.business] || t.name }}</b>
            <el-tag size="small" :type="t.enabled ? 'success' : 'info'" effect="plain">
              {{ t.enabled ? '启用' : '停用' }}
            </el-tag>
            <span class="p3-version">配置 v{{ t.config_version }}</span>
          </div>
          <span class="p3-target-url">{{ t.url }}</span>
          <div class="p3-target-meta">
            <span>期望 {{ expectedText(t.expected_status) }}</span>
            <span>{{ t.interval_minutes ? t.interval_minutes + ' 分钟' : '跟随全局' }}</span>
          </div>
          <div class="p3-target-foot">
            <span class="p3-target-hint">仅按节点代理出口真实 HTTPS 结果判定，不做服务器直连检查</span>
            <div class="p3-target-actions">
              <el-switch :model-value="t.enabled" size="small" @change="v => toggleTarget(t, v)" />
              <el-button link size="small" @click="openTarget(t)">编辑</el-button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="p3-toolbar">
      <el-input v-model="filters.q" placeholder="节点名 / 服务器 / 机场" clearable style="width: 230px"
                @keyup.enter="page = 1; loadNodes()" />
      <el-select v-model="filters.business" placeholder="业务" clearable style="width: 130px" @change="page = 1; loadNodes()">
        <el-option v-for="(label, code) in BUSINESS_LABELS" :key="code" :label="label" :value="code" />
      </el-select>
      <el-select v-model="filters.state" placeholder="资格状态" clearable style="width: 140px" @change="page = 1; loadNodes()">
        <el-option v-for="(v, k) in STATE" :key="k" :label="v.text" :value="k" />
      </el-select>
      <el-button size="small" @click="page = 1; loadNodes()">筛选</el-button>
      <el-button v-if="filters.business" link size="small" @click="filters.business = ''; loadNodes()">
        取消业务筛选
      </el-button>
      <span class="np-muted p3-count">共 {{ total }} 个有效来源节点 · 五类业务均来自 P1 原始节点池</span>
    </div>

    <div class="np-card p3-table-card">
      <el-table :data="items" size="small" row-key="id" @row-click="openDetail">
        <el-table-column label="节点" min-width="205" fixed>
          <template #default="{ row }">
            <div class="p3-node-name">{{ row.current_name }}</div>
            <div class="np-muted p3-node-sub">{{ row.server }}:{{ row.port }} · {{ row.source_name }}</div>
            <div class="p3-node-tags">
              <el-tag size="small" :type="baseOf(row.base_health).type" effect="plain">P2 {{ baseOf(row.base_health).text }}</el-tag>
              <span class="p3-layer">{{ row.management_layer || '应届' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-for="(label, code) in BUSINESS_LABELS" :key="code" :label="label" width="132">
          <template #default="{ row }">
            <div class="p3-bcell">
              <el-tag size="small" :type="bizStatus(row.businesses[code]).type" effect="light">
                {{ bizStatus(row.businesses[code]).text }}
              </el-tag>
              <span class="p3-bcount">{{ row.businesses[code]?.consecutive_successes || 0 }} 成 /
                {{ row.businesses[code]?.consecutive_failures || 0 }} 败</span>
              <small v-if="row.businesses[code]?.checking" class="p3-running">检测中</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="资格数" width="78" align="center">
          <template #default="{ row }"><b class="p3-qcount">{{ row.qualified_count }}</b> / 5</template>
        </el-table-column>
        <el-table-column label="最近检测" width="118">
          <template #default="{ row }">{{ lastRunOf(row) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="88" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="checkNode(row)">检测五项</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="p3-pager">
        <el-pagination v-model:current-page="page" :page-size="perPage" :total="total"
                       layout="prev, pager, next" small @current-change="loadNodes" />
      </div>
    </div>

    <div class="p3-mobile-list">
      <div v-for="row in items" :key="row.id" class="np-card p3-mcard" @click="openDetail(row)">
        <div class="p3-mcard-head">
          <div>
            <b>{{ row.current_name }}</b>
            <div class="np-muted p3-node-sub">{{ row.server }}:{{ row.port }} · {{ row.source_name }}</div>
          </div>
          <span class="p3-mqualified">{{ row.qualified_count }}/5</span>
        </div>
        <div class="p3-mbiz-grid">
          <div v-for="(label, code) in BUSINESS_LABELS" :key="code" class="p3-mbiz">
            <span>{{ label }}</span>
            <el-tag size="small" :type="bizStatus(row.businesses[code]).type" effect="light">
              {{ bizStatus(row.businesses[code]).text }}
            </el-tag>
            <small>{{ row.businesses[code]?.consecutive_successes || 0 }} 成 / {{ row.businesses[code]?.consecutive_failures || 0 }} 败</small>
          </div>
        </div>
        <div class="p3-mcard-foot">
          <span class="np-muted">P2 {{ baseOf(row.base_health).text }} · 最近 {{ lastRunOf(row) }}</span>
          <el-button link type="primary" size="small" @click.stop="checkNode(row)">检测五项</el-button>
        </div>
      </div>
      <el-pagination v-model:current-page="page" :page-size="perPage" :total="total"
                     layout="prev, pager, next" small @current-change="loadNodes" />
    </div>

    <el-dialog v-model="targetDialog" title="编辑业务检测目标" width="540px">
      <el-form label-width="104px" size="default">
        <el-form-item label="业务"><el-input :model-value="BUSINESS_LABELS[targetForm.business]" disabled /></el-form-item>
        <el-form-item label="名称"><el-input v-model="targetForm.name" maxlength="40" /></el-form-item>
        <el-form-item label="检测地址"><el-input v-model="targetForm.url" placeholder="https://example.com/" /></el-form-item>
        <el-form-item label="期望状态码">
          <el-input v-model="targetForm.expected_status" placeholder="200-399 或 200,204,301" />
        </el-form-item>
        <el-form-item label="单业务周期">
          <el-input-number v-model="targetForm.interval_minutes" :min="0" :max="1440" />
          <span class="np-muted p3-unit">分钟 · 0 跟随全局</span>
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="targetForm.enabled" /></el-form-item>
      </el-form>
      <p class="np-muted p3-dialog-note">
        名称修改不影响历史；地址、期望状态码、启用状态或周期变化会提升配置版本，只清空该业务的旧结论与资格，其他四类业务不受影响。
      </p>
      <template #footer>
        <el-button @click="targetDialog = false">取消</el-button>
        <el-button type="primary" :loading="targetBusy" @click="saveTarget">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="settingsDialog" title="业务专项检测设置" width="520px">
      <el-form label-width="150px">
        <el-form-item label="全局检测周期">
          <el-input-number v-model="settingsForm.interval_minutes" :min="30" :max="1440" />
          <span class="np-muted p3-unit">分钟</span>
        </el-form-item>
        <el-form-item label="结果有效期">
          <el-input-number v-model="settingsForm.valid_hours" :min="1" :max="72" />
          <span class="np-muted p3-unit">小时</span>
        </el-form-item>
        <el-form-item label="统一检测并发">
          <el-input-number v-model="settingsForm.concurrency" :min="1" :max="50" />
          <span class="np-muted p3-unit">业务任务</span>
        </el-form-item>
      </el-form>
      <p class="np-muted">
        连续 2 次有效成功获得资格，连续 3 次有效失败撤销资格；Unknown 不计入失败。
        有效期默认 24 小时，过期后保留资格历史但不计为当前有资格，并强制重检。
      </p>
      <template #footer>
        <el-button @click="settingsDialog = false">取消</el-button>
        <el-button type="primary" @click="saveSettings">保存</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="drawer" size="min(820px, 96vw)" title="业务专项检测详情">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="p3-detail-head">
            <div>
              <h3>{{ detail.node.current_name }}</h3>
              <p class="np-muted">{{ detail.node.server }}:{{ detail.node.port }} · {{ detail.node.source_name }}</p>
            </div>
            <el-button size="small" type="primary" @click="checkNode(detail.node)">检测五项</el-button>
          </div>
          <div class="p3-detail-tags">
            <el-tag :type="baseOf(detail.node.base_health).type" effect="plain">P2 {{ baseOf(detail.node.base_health).text }}</el-tag>
            <el-tag type="info" effect="plain">{{ detail.node.management_layer }}</el-tag>
            <el-tag type="info" effect="plain">竞争 {{ detail.node.competition_state }}</el-tag>
            <el-tag type="success" effect="plain">五类业务资格 {{ Object.values(detail.businesses).filter(b => b.qualification_active).length }} / 5</el-tag>
          </div>
          <p class="p3-detail-note">业务资格只代表该业务可访问，不等于已进入优选；P3 不修改 P2 基础健康和 P1 竞争状态。</p>

          <div v-for="(label, code) in BUSINESS_LABELS" :key="code" class="p3-biz-detail">
            <div class="p3-biz-detail-head">
              <div>
                <h4>{{ label }} <small>{{ BUSINESS_HINTS[code] }}</small></h4>
                <span class="np-muted">{{ detail.businesses[code].target?.name }} · {{ detail.businesses[code].target?.url }}</span>
              </div>
              <div class="p3-biz-detail-right">
                <el-tag :type="bizStatus(detail.businesses[code]).type">{{ bizStatus(detail.businesses[code]).text }}</el-tag>
                <el-button link size="small" type="primary" @click="checkNode(detail.node, code)">检测</el-button>
              </div>
            </div>
            <div class="p3-biz-meta">
              <div><span>连续成功</span><b>{{ detail.businesses[code].consecutive_successes || 0 }}</b></div>
              <div><span>连续失败</span><b>{{ detail.businesses[code].consecutive_failures || 0 }}</b></div>
              <div><span>最近有效</span><b>{{ shortTs(detail.businesses[code].last_valid_at) }}</b></div>
              <div><span>有效期至</span><b>{{ shortTs(detail.businesses[code].valid_until) }}</b></div>
              <div><span>最近检测</span><b>{{ shortTs(detail.businesses[code].last_run_at) }}</b></div>
              <div><span>下次检测</span><b>{{ shortTs(detail.businesses[code].next_due_at) }}</b></div>
            </div>
            <div v-if="detail.businesses[code].latest" class="p3-latest">
              <div class="p3-latest-main">
                <span :class="`p3-class is-${detail.businesses[code].latest.status}`">
                  {{ classOf(detail.businesses[code].latest.status).text }}
                </span>
                <b>HTTP {{ detail.businesses[code].latest.http_status ?? '—' }}</b>
                <span>{{ detail.businesses[code].latest.http_ms ?? '—' }} ms</span>
                <span>总耗时 {{ detail.businesses[code].latest.duration_ms || 0 }} ms</span>
                <span>{{ longTs(detail.businesses[code].latest.checked_at || detail.businesses[code].latest.started_at) }}</span>
              </div>
              <p v-if="detail.businesses[code].latest.reason" class="p3-reason">
                {{ reasonText(detail.businesses[code].latest.reason) }}
              </p>
              <div class="p3-stages">
                <div v-for="s in (detail.businesses[code].latest.stages || [])" :key="s.stage" class="p3-stage">
                  <span>{{ s.stage.toUpperCase() }}</span>
                  <b :class="`is-${s.status}`">{{ s.executed ? (s.ok ? '成功' : s.status) : '未执行' }}</b>
                  <small>{{ s.latency_ms == null ? '—' : s.latency_ms + 'ms' }}</small>
                </div>
              </div>
            </div>
            <div v-else class="np-muted p3-empty">该业务尚无检测记录。</div>
            <div v-if="detail.businesses[code].recent_checks?.length" class="p3-recent">
              <span class="p3-subhead">最近检测</span>
              <div v-for="c in detail.businesses[code].recent_checks.slice(0, 5)" :key="c.id" class="p3-history">
                <span>{{ shortTs(c.checked_at || c.started_at) }}</span>
                <span class="p3-class" :class="`is-${c.status}`">{{ classOf(c.status).text }}</span>
                <span>HTTP {{ c.http_status ?? '—' }}</span>
                <span>{{ c.http_ms == null ? '—' : c.http_ms + 'ms' }}</span>
                <span>{{ reasonText(c.reason) }}</span>
              </div>
            </div>
            <div v-if="detail.businesses[code].events?.length" class="p3-recent">
              <span class="p3-subhead">资格历史</span>
              <div v-for="e in detail.businesses[code].events.slice(0, 5)" :key="e.id" class="p3-event">
                <span>{{ shortTs(e.at) }}</span>
                <em>{{ EVENT_TEXT[e.event] || e.event }}</em>
                <span>{{ e.reason }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.p3-page { max-width: 1560px; }
.p3-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 14px; }
.p3-head .np-page-desc { margin: 5px 0 0; max-width: 780px; }
.p3-head-actions { display: flex; gap: 7px; flex-wrap: wrap; justify-content: flex-end; flex: 0 0 auto; }
.p3-biz-stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 10px; }
.p3-stat { text-align: left; background: var(--np-panel-solid); border: 1px solid rgba(31,61,51,.09); border-radius: 14px; padding: 13px 14px 11px; border-top: 3px solid #9bb0a6; cursor: pointer; transition: box-shadow .18s, transform .18s; }
.p3-stat:hover { transform: translateY(-1px); box-shadow: 0 8px 20px rgba(31,61,51,.08); }
.p3-stat.active { box-shadow: 0 0 0 2px rgba(53,111,85,.28), 0 8px 20px rgba(31,61,51,.08); }
.p3-stat.is-gpt { border-top-color: #4f8c6c; }
.p3-stat.is-youtube { border-top-color: #c05252; }
.p3-stat.is-telegram { border-top-color: #4b88b7; }
.p3-stat.is-github { border-top-color: #7b6ca8; }
.p3-stat.is-global { border-top-color: #d6a53c; }
.p3-stat span { display: flex; justify-content: space-between; font-size: 12px; color: #74857c; }
.p3-stat span i { font-style: normal; font-size: 9px; letter-spacing: .05em; color: #9aa79e; }
.p3-stat b { display: block; font-size: 29px; color: #1c332b; line-height: 1.2; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.p3-stat small { display: block; font-size: 11px; color: #8a998f; line-height: 1.35; }
.p3-stat em { display: block; font-style: normal; font-size: 10px; margin-top: 4px; color: #6f8578; }
.p3-mini-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-bottom: 16px; }
.p3-mini-stats > div { display: flex; align-items: baseline; gap: 8px; padding: 8px 12px; border-radius: 10px; background: rgba(240,246,242,.72); border: 1px solid rgba(31,61,51,.06); flex-wrap: wrap; }
.p3-mini-stats span { font-size: 11px; color: #74857c; }
.p3-mini-stats b { font-size: 18px; color: #29433a; font-variant-numeric: tabular-nums; }
.p3-mini-stats small { font-size: 10px; color: #93a198; }
.p3-target-card { padding: 16px 18px; margin-bottom: 16px; }
.p3-section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.p3-section-head h3 { margin: 0; font-size: 15px; color: #203730; }
.p3-section-head p { margin: 3px 0 0; font-size: 12px; }
.p3-target-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.p3-target { padding: 10px 12px; border: 1px solid rgba(31,61,51,.08); border-radius: 10px; background: #fbfdfc; min-width: 0; }
.p3-target.off { opacity: .62; background: #f7f7f6; }
.p3-target-head { display: flex; align-items: center; gap: 7px; }
.p3-target-head b { font-size: 13px; color: #203730; }
.p3-version { margin-left: auto; font-size: 9px; color: #9aa79e; }
.p3-target-url { display: block; font-size: 11px; color: #5f746a; overflow-wrap: anywhere; margin-top: 3px; }
.p3-target-meta { display: flex; flex-wrap: wrap; gap: 8px; font-size: 10px; color: #87958d; margin-top: 4px; }
.p3-target-foot { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.p3-target-actions { display: flex; align-items: center; gap: 2px; margin-left: auto; }
.p3-target-actions :deep(.el-button) { padding: 0 2px; }
.p3-target-hint { font-size: 9px; color: #93a198; line-height: 1.4; }
.p3-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.p3-count { margin-left: auto; font-size: 12px; }
.p3-table-card { padding: 4px 8px 6px; }
.p3-table-card :deep(.el-table) { min-width: 1260px; }
.p3-node-name { font-weight: 600; color: #213b32; }
.p3-node-sub { font-size: 11px; margin-top: 1px; overflow-wrap: anywhere; }
.p3-node-tags { display: flex; gap: 5px; align-items: center; margin-top: 4px; }
.p3-layer { font-size: 10px; color: #86958c; }
.p3-bcell { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; }
.p3-bcount { font-size: 10px; color: #7e8f85; font-variant-numeric: tabular-nums; }
.p3-running { font-size: 9px; color: #b07a19; }
.p3-qcount { font-size: 14px; color: #2f7d5a; }
.p3-class { font-size: 11px; font-weight: 700; }
.p3-class.is-success { color: #2f7d5a; }
.p3-class.is-failure { color: #a84646; }
.p3-class.is-unknown { color: #7c8792; }
.p3-pager { display: flex; justify-content: flex-end; padding: 8px 4px 2px; }
.p3-mobile-list { display: none; }
.p3-mcard { padding: 13px 14px; margin-bottom: 10px; }
.p3-mcard-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
.p3-mqualified { font-size: 13px; font-weight: 700; color: #2f7d5a; }
.p3-mbiz-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 9px; }
.p3-mbiz { background: #f4f8f5; border-radius: 8px; padding: 7px 8px; min-width: 0; }
.p3-mbiz span { display: block; font-size: 10px; color: #74857c; margin-bottom: 3px; }
.p3-mbiz small { display: block; font-size: 9px; color: #93a198; margin-top: 2px; }
.p3-mcard-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
.p3-dialog-note { font-size: 12px; line-height: 1.6; margin: 4px 0 0; }
.p3-unit { margin-left: 8px; }
.p3-detail-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.p3-detail-head h3 { margin: 0; font-size: 17px; color: #203730; }
.p3-detail-head p { margin: 3px 0 0; font-size: 12px; }
.p3-detail-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 6px; }
.p3-detail-note { font-size: 11px; color: #86958c; margin: 0 0 12px; }
.p3-biz-detail { border: 1px solid rgba(31,61,51,.09); border-radius: 12px; padding: 12px 13px; margin-bottom: 10px; background: #fcfdfd; }
.p3-biz-detail-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
.p3-biz-detail-head h4 { margin: 0; font-size: 14px; color: #203730; }
.p3-biz-detail-head h4 small { font-weight: 400; font-size: 10px; color: #93a198; margin-left: 5px; }
.p3-biz-detail-head .np-muted { display: block; font-size: 10px; margin-top: 2px; overflow-wrap: anywhere; }
.p3-biz-detail-right { display: flex; align-items: center; gap: 4px; flex: 0 0 auto; }
.p3-biz-meta { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 6px; margin: 9px 0; }
.p3-biz-meta > div { background: #f4f8f5; border-radius: 7px; padding: 6px 8px; min-width: 0; }
.p3-biz-meta span { display: block; font-size: 9px; color: #7a8c82; }
.p3-biz-meta b { display: block; font-size: 11px; color: #29433a; margin-top: 2px; overflow-wrap: anywhere; }
.p3-latest { border-top: 1px dashed rgba(31,61,51,.1); padding-top: 7px; }
.p3-latest-main { display: flex; gap: 9px; flex-wrap: wrap; align-items: baseline; font-size: 11px; color: #5c6f65; }
.p3-latest-main b { color: #29433a; }
.p3-reason { font-size: 10px; color: #9a6d17; margin: 3px 0 4px; }
.p3-stages { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; margin-top: 5px; }
.p3-stage { background: #f5f8f6; border-radius: 7px; padding: 6px 7px; }
.p3-stage span { display: block; font-size: 9px; color: #7a8c82; }
.p3-stage b { display: block; font-size: 10px; color: #596d63; margin: 1px 0; }
.p3-stage b.is-success { color: #2f7d5a; }
.p3-stage b.is-failure { color: #a84646; }
.p3-stage b.is-unknown, .p3-stage b.is-unexecuted { color: #9ca7a1; font-weight: 400; }
.p3-stage small { font-size: 9px; color: #29433a; }
.p3-empty { padding: 8px 0; font-size: 11px; }
.p3-recent { margin-top: 7px; }
.p3-subhead { display: block; font-size: 10px; color: #7a8c82; margin-bottom: 2px; }
.p3-history { display: grid; grid-template-columns: 76px 66px 68px 58px 1fr; gap: 6px; padding: 3px 0; border-bottom: 1px dashed rgba(31,61,51,.07); font-size: 10px; color: #5c6f65; }
.p3-event { display: grid; grid-template-columns: 76px 76px 1fr; gap: 6px; padding: 3px 0; font-size: 10px; color: #5c6f65; }
.p3-event em { font-style: normal; font-weight: 700; color: #2f7d5a; }
@media (max-width: 1100px) {
  .p3-biz-stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 860px) {
  .p3-head { flex-direction: column; }
  .p3-head-actions { justify-content: flex-start; width: 100%; }
  .p3-biz-stats { grid-template-columns: 1fr 1fr; }
  .p3-stat b { font-size: 24px; }
  .p3-mini-stats { grid-template-columns: 1fr 1fr; }
  .p3-target-grid { grid-template-columns: 1fr; }
  .p3-section-head { flex-direction: column; }
  .p3-table-card { display: none; }
  .p3-mobile-list { display: block; }
  .p3-count { width: 100%; margin-left: 0; }
  .p3-biz-meta { grid-template-columns: 1fr 1fr 1fr; }
  .p3-stages { grid-template-columns: 1fr 1fr; }
  .p3-history { grid-template-columns: 1fr 1fr; }
  .p3-event { grid-template-columns: 1fr 1fr; }
}
</style>
