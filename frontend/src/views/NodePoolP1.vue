<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const summary = ref(null)
const items = ref([])
const total = ref(0)
const page = ref(1)
const perPage = 20
const loading = ref(false)
const airports = ref([])
const detail = ref(null)
const drawer = ref(false)
const busy = ref(false)
const newQualification = ref('')
const filters = ref({ q: '', airport_id: null, layer: '', health: '', competition: '',
                      data_status: '', source_active: null })

const LAYER = {
  raw: { text: '原始', type: 'info' },
  healthy: { text: '健康池', type: 'success' },
  observation: { text: '观察池', type: 'warning' }
}
const HEALTH = {
  healthy: { text: '健康', type: 'success' },
  unhealthy: { text: '不健康', type: 'danger' },
  unknown: { text: '未知', type: 'info' }
}
const COMPETITION = {
  candidate: { text: '候选', type: 'primary' },
  preferred: { text: '优选', type: 'success' },
  observation: { text: '观察', type: 'warning' },
  eliminated: { text: '淘汰', type: 'info' }
}
const DATA = {
  sufficient: { text: '充足', type: 'success' },
  insufficient: { text: '不足', type: 'warning' },
  unknown: { text: '未知', type: 'info' }
}
const EVENT_LABELS = {
  node_ingested: '采集入库', node_backfilled: '历史回填', name_changed: '名称变化',
  source_missing: '来源消失', source_returned: '来源恢复', health_update: '健康状态更新',
  layer_transition: '层级转换', competition_update: '竞争状态更新',
  qualification_granted: '授予资格', qualification_revoked: '撤销资格'
}
const layerOf = (v) => LAYER[v] || { text: v || '—', type: 'info' }
const healthOf = (v) => HEALTH[v] || { text: v || '—', type: 'info' }
const competitionOf = (v) => COMPETITION[v] || { text: v || '—', type: 'info' }
const dataOf = (v) => DATA[v] || { text: v || '—', type: 'info' }
const eventLabel = (v) => EVENT_LABELS[v] || v
const REASON_TEXT = {
  new: '新采集', source_missing: '来源消失', source_returned: '来源恢复',
  insufficient_data: '数据不足', health_unhealthy: '基础异常', health_unknown: '状态未知',
  backfill: '历史回填', filtered: '关键词过滤', manual_observation: '人工观察'
}
const reasonText = (v) => REASON_TEXT[v] || v || '—' 
const shortTs = (ts) => { const t = formatTs(ts); return t === '—' ? t : t.slice(5, 16) }

const canPromote = computed(() => detail.value
  && detail.value.base_health === 'healthy'
  && detail.value.data_status === 'sufficient'
  && detail.value.source_active
  && detail.value.management_layer === 'healthy')
const activeQualifications = computed(() => (detail.value?.qualifications || []).filter(q => q.active))

async function loadSummary() {
  summary.value = await api.get('/api/nodepool/summary')
}
async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    Object.entries(filters.value).forEach(([k, v]) => {
      if (v === '' || v === null) return
      params.set(k, String(v))
    })
    params.set('page', String(page.value))
    params.set('per_page', String(perPage))
    const res = await api.get(`/api/nodepool/nodes?${params.toString()}`)
    items.value = res.items
    total.value = res.total
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}
async function loadAirports() {
  try { airports.value = await api.get('/api/airports') } catch { airports.value = [] }
}
function search() { page.value = 1; load() }
function reset() {
  filters.value = { q: '', airport_id: null, layer: '', health: '', competition: '',
                    data_status: '', source_active: null }
  page.value = 1
  load()
}
async function openDetail(row) {
  try {
    detail.value = await api.get(`/api/nodepool/nodes/${row.id}`)
    newQualification.value = ''
    drawer.value = true
  } catch (e) { ElMessage.error(e.message) }
}
async function refreshAll() {
  await Promise.all([load(), loadSummary()])
  if (detail.value) detail.value = await api.get(`/api/nodepool/nodes/${detail.value.id}`)
}
async function act(label, request) {
  busy.value = true
  try {
    await request()
    ElMessage.success(`${label}完成`)
    await refreshAll()
  } catch (e) { ElMessage.error(e.message) }
  finally { busy.value = false }
}
const setHealth = (health, dataStatus, reason) => act('状态更新',
  () => api.post(`/api/nodepool/nodes/${detail.value.id}/health`,
    { health, data_status: dataStatus, reason, observation_reason: reason }))
const setLayer = (layer, reason) => act('层级更新',
  () => api.post(`/api/nodepool/nodes/${detail.value.id}/layer`, { layer, reason }))
const setCompetition = (state, reason) => act('竞争状态更新',
  () => api.post(`/api/nodepool/nodes/${detail.value.id}/competition`, { state, reason }))
async function grantQualification() {
  const q = newQualification.value.trim()
  if (!q) return
  await act('授予资格', () => api.post(`/api/nodepool/nodes/${detail.value.id}/qualifications`,
    { qualification: q, reason: '管理台手工授予' }))
  newQualification.value = ''
}
const revokeQualification = (q) => act('撤销资格',
  () => api.del(`/api/nodepool/nodes/${detail.value.id}/qualifications/${encodeURIComponent(q)}`))

onMounted(async () => { await Promise.all([loadSummary(), loadAirports()]); await load() })
</script>

<template>
  <div v-loading="loading" class="np-page p1-page">
    <div class="p1-head">
      <div>
        <h2 class="np-page-title">三层节点池</h2>
      </div>
      <el-button class="p1-refresh" @click="refreshAll"><el-icon><Refresh /></el-icon>刷新</el-button>
    </div>

    <div v-if="summary" class="p1-stats">
      <div class="p1-stat p1-stat-raw">
        <span class="p1-stat-k">原始节点池 · 全部</span>
        <b class="p1-stat-v">{{ summary.raw_total }}</b>
        <small>所有标准化节点，含历史失效</small>
        <div class="p1-stat-foot"><span>来源有效 {{ summary.source_active }}</span><span>历史 {{ summary.historical }}</span></div>
      </div>
      <div class="p1-stat p1-stat-healthy">
        <span class="p1-stat-k">健康节点池</span>
        <b class="p1-stat-v">{{ summary.healthy }}</b>
        <small>基础健康合格 · 不等于智能优选</small>
        <div class="p1-stat-foot"><span>健康 {{ summary.base_health.healthy }}</span><span>候选 {{ summary.competition.candidate }}</span></div>
      </div>
      <div class="p1-stat p1-stat-observe">
        <span class="p1-stat-k">观察节点池</span>
        <b class="p1-stat-v">{{ summary.observation }}</b>
        <small>新采集 / 数据不足 / 波动 / 待恢复</small>
        <div class="p1-stat-foot"><span>数据不足 {{ summary.data_status.insufficient }}</span><span>不健康 {{ summary.base_health.unhealthy }}</span></div>
      </div>
      <div class="p1-stat p1-stat-rawonly">
        <span class="p1-stat-k">仅原始层</span>
        <b class="p1-stat-v">{{ summary.raw_only }}</b>
        <small>未进入健康或观察管理层，记录仍保留</small>
        <div class="p1-stat-foot"><span>淘汰 {{ summary.competition.eliminated }}</span><span>资格 {{ summary.active_qualifications }}</span></div>
      </div>
    </div>

    <div class="np-toolbar p1-toolbar">
      <el-input v-model="filters.q" placeholder="搜索名称 / 服务器 / 历史名" clearable style="width:220px"
                @keyup.enter="search" @clear="search" />
      <el-select v-model="filters.airport_id" placeholder="机场" clearable style="width:150px" @change="search">
        <el-option v-for="a in airports" :key="a.id" :label="a.name" :value="a.id" />
      </el-select>
      <el-select v-model="filters.layer" placeholder="管理层" clearable style="width:125px" @change="search">
        <el-option label="健康池" value="healthy" />
        <el-option label="观察池" value="observation" />
        <el-option label="仅原始" value="raw" />
      </el-select>
      <el-select v-model="filters.health" placeholder="基础健康" clearable style="width:120px" @change="search">
        <el-option label="健康" value="healthy" />
        <el-option label="不健康" value="unhealthy" />
        <el-option label="未知" value="unknown" />
      </el-select>
      <el-select v-model="filters.competition" placeholder="竞争状态" clearable style="width:120px" @change="search">
        <el-option label="候选" value="candidate" />
        <el-option label="优选" value="preferred" />
        <el-option label="观察" value="observation" />
        <el-option label="淘汰" value="eliminated" />
      </el-select>
      <el-select v-model="filters.data_status" placeholder="数据" clearable style="width:110px" @change="search">
        <el-option label="充足" value="sufficient" />
        <el-option label="不足" value="insufficient" />
        <el-option label="未知" value="unknown" />
      </el-select>
      <el-select v-model="filters.source_active" placeholder="来源" clearable style="width:120px" @change="search">
        <el-option label="来源有效" :value="1" />
        <el-option label="历史失效" :value="0" />
      </el-select>
      <el-button class="np-query-btn" type="primary" @click="search"><el-icon><Search /></el-icon>查询</el-button>
      <el-button class="np-reset-btn" @click="reset">重置</el-button>
      <div class="np-flex np-spacer" />
      <span class="np-muted">共 {{ total }} 条</span>
    </div>

    <div class="p1-mobile-list">
      <div v-for="row in items" :key="row.id" class="p1-mcard" role="button" tabindex="0"
           @click="openDetail(row)" @keyup.enter="openDetail(row)">
        <div class="p1-mcard-head">
          <div>
            <div class="p1-name">{{ row.current_name }}</div>
            <div class="np-muted p1-sub">{{ row.airport_name || row.source_name }} · {{ row.server }}:{{ row.port }}</div>
          </div>
          <el-tag size="small" effect="plain" :type="layerOf(row.management_layer).type">{{ layerOf(row.management_layer).text }}</el-tag>
        </div>
        <div class="p1-mchips">
          <span :class="['p1-chip', 'is-' + row.base_health]">健康 {{ healthOf(row.base_health).text }}</span>
          <span :class="['p1-chip', 'is-' + row.competition_state]">竞争 {{ competitionOf(row.competition_state).text }}</span>
          <span class="p1-chip">数据 {{ dataOf(row.data_status).text }}</span>
          <span :class="['p1-chip', { 'is-off': !row.source_active }]">{{ row.source_active ? '来源有效' : '历史失效' }}</span>
        </div>
        <div class="p1-mcard-foot">
          <span class="np-muted">{{ reasonText(row.observation_reason) }}</span>
          <span class="np-muted">{{ shortTs(row.last_seen_at) }}</span>
        </div>
        <div v-if="row.qualifications.length" class="p1-mcard-qual">
          <el-tag v-for="q in row.qualifications" :key="q" size="small" effect="plain" class="p1-q">{{ q }}</el-tag>
        </div>
      </div>
      <div v-if="!items.length" class="np-empty">当前筛选条件下没有节点</div>
      <div v-if="total > perPage" class="p1-pager">
        <el-pagination v-model:current-page="page" :page-size="perPage" :total="total"
                       layout="prev, pager, next" small background @current-change="load" />
      </div>
    </div>

    <div class="np-card p1-table-card">
      <el-table :data="items" size="small" table-layout="fixed" @row-click="openDetail">
        <el-table-column label="节点" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="p1-name">{{ row.current_name }}</div>
            <div class="np-muted p1-sub">{{ row.airport_name || row.source_name }} · {{ row.server }}:{{ row.port }}</div>
          </template>
        </el-table-column>
        <el-table-column label="管理层" width="92">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="layerOf(row.management_layer).type">{{ layerOf(row.management_layer).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="基础健康" width="88">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="healthOf(row.base_health).type">{{ healthOf(row.base_health).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="竞争状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="competitionOf(row.competition_state).type">{{ competitionOf(row.competition_state).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="数据" width="72">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="dataOf(row.data_status).type">{{ dataOf(row.data_status).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源 / 最近出现" width="135">
          <template #default="{ row }">
            <div :class="['p1-source', { off: !row.source_active }]">{{ row.source_active ? '有效' : '历史失效' }}</div>
            <div class="np-muted p1-sub">{{ shortTs(row.last_seen_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="观察原因" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="np-muted">{{ reasonText(row.observation_reason) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="资格" min-width="145">
          <template #default="{ row }">
            <el-tag v-for="q in row.qualifications" :key="q" size="small" effect="plain" class="p1-q">{{ q }}</el-tag>
            <span v-if="!row.qualifications.length" class="np-muted">—</span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!items.length" class="np-empty">当前筛选条件下没有节点</div>
      <div class="p1-pager">
        <el-pagination v-model:current-page="page" :page-size="perPage" :total="total"
                       layout="prev, pager, next" background @current-change="load" />
      </div>
    </div>

    <el-drawer v-model="drawer" size="min(560px, 94vw)" :title="detail ? `节点 · ${detail.current_name}` : '节点详情'">
      <template v-if="detail">
        <div class="p1-detail-tags">
          <el-tag effect="plain" :type="layerOf(detail.management_layer).type">{{ layerOf(detail.management_layer).text }}</el-tag>
          <el-tag effect="plain" :type="healthOf(detail.base_health).type">健康 {{ healthOf(detail.base_health).text }}</el-tag>
          <el-tag effect="plain" :type="competitionOf(detail.competition_state).type">竞争 {{ competitionOf(detail.competition_state).text }}</el-tag>
          <el-tag effect="plain" :type="dataOf(detail.data_status).type">数据 {{ dataOf(detail.data_status).text }}</el-tag>
        </div>
        <dl class="p1-meta">
          <div><dt>稳定身份</dt><dd>{{ detail.stable_uid }}</dd></div>
          <div><dt>机场 / 来源</dt><dd>{{ detail.airport_name || detail.source_name }} · {{ detail.source_url || '—' }}</dd></div>
          <div><dt>端点</dt><dd>{{ detail.protocol }} · {{ detail.server }}:{{ detail.port }}</dd></div>
          <div><dt>首名 / 现名</dt><dd>{{ detail.first_name }} → {{ detail.current_name }}</dd></div>
          <div><dt>来源状态</dt><dd>{{ detail.source_active ? '有效' : '历史失效（未删除）' }}</dd></div>
          <div><dt>观察原因</dt><dd>{{ reasonText(detail.observation_reason) }}</dd></div>
        </dl>

        <div class="p1-block">
          <h4>管理层与状态操作</h4>
          <div class="p1-actions">
            <el-button size="small" :disabled="busy" @click="setHealth('healthy', 'sufficient', '人工确认健康')">标记健康</el-button>
            <el-button size="small" :disabled="busy" @click="setHealth('unhealthy', 'sufficient', '人工标记异常')">标记不健康</el-button>
            <el-button size="small" :disabled="busy" @click="setHealth('unknown', 'insufficient', '数据不足')">数据不足</el-button>
            <el-button size="small" :disabled="busy" @click="setLayer('observation', '人工转观察')">转观察</el-button>
            <el-button size="small" :disabled="busy" @click="setLayer('raw', '历史归档')">归档到原始</el-button>
          </div>
          <div class="p1-actions">
            <el-button size="small" :disabled="busy || !canPromote" @click="setCompetition('candidate', '恢复候选')">恢复候选</el-button>
            <el-button size="small" :disabled="busy || !canPromote" @click="setCompetition('preferred', '人工优选')">设为优选</el-button>
            <el-button size="small" :disabled="busy" @click="setCompetition('eliminated', '智能淘汰')">标记淘汰</el-button>
          </div>
          <p class="np-muted p1-hint">健康/数据状态由 P2 检测结果写入；本页按钮用于 P1 管理能力验收，不发起任何网络探测。</p>
        </div>

        <div class="p1-block">
          <h4>业务资格</h4>
          <div class="p1-qual-form">
            <el-input v-model="newQualification" size="small" placeholder="资格名，如 streaming" @keyup.enter="grantQualification" />
            <el-button size="small" type="primary" :disabled="busy || !newQualification.trim()" @click="grantQualification">授予</el-button>
          </div>
          <div class="p1-qual-list">
            <el-tag v-for="q in activeQualifications" :key="q.qualification" closable effect="plain"
                    @close="revokeQualification(q.qualification)">{{ q.qualification }}</el-tag>
            <span v-if="!activeQualifications.length" class="np-muted">暂无有效资格</span>
          </div>
        </div>

        <div class="p1-block">
          <h4>名称历史</h4>
          <div v-for="n in detail.names" :key="n.name" class="p1-history-row">
            <b>{{ n.name }}</b>
            <span class="np-muted">{{ shortTs(n.first_seen_at) }} → {{ shortTs(n.last_seen_at) }} · {{ n.seen_count }} 次</span>
          </div>
        </div>

        <div class="p1-block">
          <h4>状态审计</h4>
          <div v-for="e in detail.events" :key="e.id" class="p1-event">
            <div class="p1-event-head">
              <b>{{ eventLabel(e.kind) }}</b>
              <span class="np-muted">{{ shortTs(e.ts) }}</span>
            </div>
            <div class="p1-event-body">
              <span v-if="e.kind === 'name_changed'">{{ e.detail.old }} → {{ e.detail.new }}</span>
              <span v-else>{{ e.from_layer || '—' }} → {{ e.to_layer || '—' }} ·
                {{ e.from_health || '—' }} → {{ e.to_health || '—' }} ·
                {{ e.from_competition || '—' }} → {{ e.to_competition || '—' }}</span>
            </div>
            <div v-if="e.reason" class="np-muted p1-event-reason">{{ e.reason }}</div>
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.p1-page { max-width: 1500px; }
.p1-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 16px; }
.p1-head .np-page-desc { margin: 4px 0 0; max-width: 860px; }
.p1-refresh { flex: 0 0 auto; }
.p1-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
.p1-stat { background: var(--np-panel-solid); border: 1px solid rgba(31, 61, 51, .09); border-radius: 14px; padding: 15px 16px 12px; box-shadow: 0 1px 2px rgba(0,0,0,.03); border-top: 3px solid #9bb0a6; }
.p1-stat-raw { border-top-color: #7b8f86; }
.p1-stat-healthy { border-top-color: #3d9a72; }
.p1-stat-observe { border-top-color: #d6a53c; }
.p1-stat-rawonly { border-top-color: #8d7b98; }
.p1-stat-k { display: block; font-size: 12px; color: #74857c; letter-spacing: .02em; }
.p1-stat-v { display: block; font-size: 30px; line-height: 1.15; margin: 5px 0 2px; color: #1c332b; font-variant-numeric: tabular-nums; }
.p1-stat small { display: block; font-size: 11px; color: #8a998f; min-height: 28px; line-height: 1.35; }
.p1-stat-foot { display: flex; justify-content: space-between; gap: 8px; padding-top: 8px; margin-top: 8px; border-top: 1px dashed rgba(31,61,51,.1); font-size: 11px; color: #65766c; }
.p1-toolbar { margin-bottom: 12px; }
.p1-table-card { padding: 6px 10px 4px; overflow-x: auto; }
.p1-table-card :deep(.el-table) { min-width: 980px; }
.p1-mobile-list { display: none; }
.p1-mcard { background: var(--np-panel-solid); border: 1px solid rgba(31,61,51,.09); border-radius: 14px; padding: 13px 14px; margin-bottom: 10px; box-shadow: 0 1px 2px rgba(0,0,0,.03); }
.p1-mcard-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.p1-mchips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }
.p1-chip { font-size: 11px; line-height: 20px; padding: 0 7px; border-radius: 6px; background: #f2f5f3; color: #5b6f64; }
.p1-chip.is-healthy { background: #e9f5ef; color: #2f7d5a; }
.p1-chip.is-unhealthy { background: #fbeaea; color: #a84646; }
.p1-chip.is-preferred { background: #e9f5ef; color: #2f7d5a; }
.p1-chip.is-candidate { background: #eef3fa; color: #416a9a; }
.p1-chip.is-observation { background: #fbf2df; color: #9a6d17; }
.p1-chip.is-eliminated, .p1-chip.is-off { background: #f1efef; color: #7b6969; }
.p1-mcard-foot { display: flex; justify-content: space-between; gap: 10px; margin-top: 8px; }
.p1-mcard-qual { margin-top: 7px; }
.p1-name { font-weight: 600; color: #203730; }
.p1-sub { font-size: 11px; margin-top: 1px; }
.p1-source { font-size: 12px; color: #347a5b; }
.p1-source.off { color: #a06b3a; }
.p1-q { margin: 2px 3px 2px 0; }
.p1-pager { display: flex; justify-content: flex-end; padding: 10px 4px 6px; }
.p1-detail-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.p1-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; margin: 0 0 14px; }
.p1-meta div { min-width: 0; }
.p1-meta dt { font-size: 11px; color: #86978f; margin-bottom: 2px; }
.p1-meta dd { margin: 0; font-size: 12px; color: #29433a; overflow-wrap: anywhere; }
.p1-block { border-top: 1px solid rgba(31,61,51,.08); padding-top: 12px; margin-top: 12px; }
.p1-block h4 { margin: 0 0 8px; font-size: 13px; color: #29433a; }
.p1-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 7px; }
.p1-hint { margin: 4px 0 0; }
.p1-qual-form { display: flex; gap: 6px; margin-bottom: 8px; }
.p1-qual-list { display: flex; flex-wrap: wrap; gap: 6px; }
.p1-history-row { display: flex; justify-content: space-between; gap: 10px; padding: 4px 0; border-bottom: 1px dashed rgba(31,61,51,.07); font-size: 12px; }
.p1-event { padding: 7px 0; border-bottom: 1px dashed rgba(31,61,51,.07); }
.p1-event-head { display: flex; justify-content: space-between; gap: 10px; font-size: 12px; }
.p1-event-body { font-size: 11px; color: #5c6f65; margin-top: 2px; }
.p1-event-reason { font-size: 11px; }
@media (max-width: 860px) {
  .p1-stats { grid-template-columns: 1fr 1fr; }
  .p1-table-card { display: none; }
  .p1-mobile-list { display: block; }
  .p1-head { align-items: center; }
  .p1-head .np-page-desc { font-size: 12px; }
  .p1-meta { grid-template-columns: 1fr; }
}
</style>
