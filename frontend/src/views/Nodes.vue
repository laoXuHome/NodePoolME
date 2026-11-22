<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const items = ref([])
const total = ref(0)
const loading = ref(false)
const groups = ref([])
const enabledGroups = computed(() => (groups.value || []).filter(g => g && g.enabled))
const airports = ref([])
const filters = ref({ q: '', airport_id: null, protocol: '', region: '', status: '',
                     group_id: null, tags: [], passed: '', publishable: '',
                     sort: 'latency', limit: 50 })
const page = ref(1)
const detail = ref(null)
const detailOpen = ref(false)
const history = ref([])
const latencyLabels = ref({ G: { short: 'G', name: 'Google' }, W: { short: 'W', name: 'Wikipedia' } })
const testing = ref('')   // '' | 'latency' | 'tags'
const isMobile = ref(false)
const filtersOpen = ref(false)
const detailCols = computed(() => (isMobile.value ? 1 : 2))
let mq = null
function syncMobile(e) { isMobile.value = e.matches }

const protocols = ref([])
const regions = ref([])
const protocolOptions = computed(() => protocols.value.length
  ? protocols.value
  : ['ss', 'ssr', 'vmess', 'vless', 'trojan', 'hysteria', 'hysteria2', 'tuic', 'socks5', 'http'])


async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    Object.entries(filters.value).forEach(([k, v]) => {
      if (v === '' || v === null || (Array.isArray(v) && !v.length)) return
      params.set(k, Array.isArray(v) ? v.join(',') : v)
    })
    params.set('offset', String((page.value - 1) * filters.value.limit))
    const res = await api.get(`/api/nodes?${params.toString()}`)
    items.value = res.items
    total.value = res.total
    if (res.latency_labels) latencyLabels.value = res.latency_labels
    if (res.protocols) protocols.value = res.protocols
    if (res.regions) regions.value = res.regions
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function loadMeta() {
  groups.value = await api.get('/api/groups')
  airports.value = await api.get('/api/airports')
}

function search() {
  if (!filters.value.group_id) filters.value.passed = filters.value.passed === 'miss' ? '' : filters.value.passed
  page.value = 1
  load()
}
function reset() {
  filters.value = { q: '', airport_id: null, protocol: '', region: '', status: '',
                     group_id: null, tags: [], passed: '', publishable: '',
                     sort: 'latency', limit: 50 }
  page.value = 1
  load()
}

async function openDetail(row) {
  try {
    detail.value = await api.get(`/api/nodes/${row.id}`)
    history.value = detail.value.history
    if (detail.value.latency_labels) latencyLabels.value = detail.value.latency_labels
    detailOpen.value = true
  } catch (e) { ElMessage.error(e.message) }
}

async function testNode(mode) {
  testing.value = mode
  try {
    const res = await api.post(`/api/nodes/${detail.value.id}/test`, { mode })
    const addNote = res.subscription?.added ? '，已加入当前订阅' : ''
    if (mode === 'latency') {
      ElMessage.success((res.latency?.ok ? '延迟检测通过（两个目标都达标）' : '延迟检测未通过') + addNote)
    } else {
      const ok = res.results.filter((r) => r.ok).length
      ElMessage.success(`标签检测完成：${ok}/${res.results.length} 项通过${addNote}`)
    }
    await openDetail(detail.value)
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { testing.value = '' }
}

const hasActiveFilters = computed(() => Object.entries(filters.value).some(([k, v]) => {
  if (k === 'sort' || k === 'limit') return false
  return Array.isArray(v) ? v.length > 0 : v !== '' && v !== null
}))

function latencyText(n) { return n ? `${n} ms` : '—' }
function targetName(target) { return latencyLabels.value[target]?.name || target }
function targetShort(target) { return latencyLabels.value[target]?.short || target }
function shortTs(ts) { const full = formatTs(ts); return full === '—' ? full : full.slice(5, 16) }
function latencyType(n) { if (!n) return 'info'; if (n < 200) return 'success'; if (n < 500) return 'warning'; return 'danger' }
function latencyValue(row, target) {
  return row?.latency_targets?.[target]?.min ?? row?.latency_targets?.[target]?.best ?? null
}
function overCap(row, target) {
  const value = latencyValue(row, target)
  const cap = row?.latency_targets?.[target]?.cap
  return value != null && cap > 0 && value > cap
}
const ERROR_TYPE_LABELS = {
  timeout: '超时', connect: '连接失败', status: '状态码不符',
  content: '内容不符', slow: '延迟超限', engine: '引擎异常'
}
function errorTypeLabel(value) { return ERROR_TYPE_LABELS[value] || value || '未知原因' }
function groupCell(row, gid) {
  const res = row.results[String(gid)]
  if (!res) return null
  return res
}
function groupCellText(row, gid) {
  const res = groupCell(row, gid)
  if (!res) return ''
  if (res.ok) return res.latency_ms ?? ''
  // 只有“延迟超过标签上限”保留具体数值；超时和其他失败仍显示叉号
  if (res.error_type === 'slow' && res.latency_ms != null) return res.latency_ms
  return '✕'
}
function groupCellTip(row, gid) {
  const res = groupCell(row, gid)
  if (!res) return '—'
  if (res.ok) return `延迟 ${res.latency_ms}ms`
  return `${errorTypeLabel(res.error_type)}${res.message ? '：' + res.message : ''}`
}

onMounted(async () => {
  mq = window.matchMedia('(max-width: 768px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', syncMobile)
  await loadMeta()
  await load()
})
onUnmounted(() => { if (mq) mq.removeEventListener('change', syncMobile) })
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">缓存池</h2>
    <div class="np-page-desc">展示全部缓存节点、最近一次各节点标签结果与动态生成的最终名称。</div>

    <div class="np-toolbar np-node-toolbar">
      <el-input v-model="filters.q" class="np-search" placeholder="搜索名称 / 服务器" style="width:220px" clearable
                @keyup.enter="search" @clear="search" />
      <el-button v-if="isMobile" class="np-filter-toggle" @click="filtersOpen = !filtersOpen">
        <el-icon><Operation /></el-icon>{{ filtersOpen ? '收起筛选' : '筛选' }}
      </el-button>
      <div v-show="!isMobile || filtersOpen" class="np-filters">
      <el-select v-model="filters.airport_id" placeholder="机场" clearable style="width:150px" @change="search">
        <el-option v-for="a in airports" :key="a.id" :label="a.name" :value="a.id" />
      </el-select>
      <el-select v-model="filters.protocol" placeholder="协议" clearable style="width:130px" @change="search">
        <el-option v-for="p in protocolOptions" :key="p" :label="p" :value="p" />
      </el-select>
      <el-select v-model="filters.region" placeholder="地区" clearable style="width:120px" @change="search">
        <el-option v-for="r in regions" :key="r" :label="r" :value="r" />
      </el-select>
      <el-select v-model="filters.status" placeholder="状态" clearable style="width:120px" @change="search">
        <el-option label="可用" value="available" />
        <el-option label="不可用" value="unavailable" />
      </el-select>
      <el-select v-model="filters.group_id" placeholder="节点标签" clearable style="width:140px" @change="search">
        <el-option v-for="g in enabledGroups" :key="g.id" :label="`${g.ident} · ${g.name}`" :value="g.ident" />
      </el-select>
      <el-select v-model="filters.tags" placeholder="标签（须同时命中）" multiple collapse-tags clearable style="width:190px" @change="search">
        <el-option v-for="g in enabledGroups" :key="g.ident" :label="g.ident" :value="g.ident" />
      </el-select>
      <el-select v-model="filters.publishable" placeholder="下发结果" clearable style="width:130px" @change="search">
        <el-option label="仅下发节点" value="1" />
        <el-option label="仅不下发" value="0" />
      </el-select>
      <el-select v-model="filters.passed" placeholder="组结果 / 标签情况" clearable style="width:170px" @change="search">
        <el-option label="未命中所选组" value="miss" :disabled="!filters.group_id" />
        <el-option label="至少有一个标签" value="qualified" />
        <el-option label="没有任何标签" value="untagged" />
      </el-select>
      <el-select v-model="filters.sort" style="width:130px" @change="search">
        <el-option label="按延迟" value="latency" />
        <el-option label="按名称" value="name" />
        <el-option label="按检测时间" value="tested" />
        <el-option label="按标签数量" value="tags" />
      </el-select>
      </div>
      <el-button class="np-query-btn" type="primary" @click="search"><el-icon><Search /></el-icon>查询</el-button>
      <el-button class="np-reset-btn" @click="reset">重置</el-button>
      <div class="np-flex np-spacer" />
      <span class="np-muted np-node-total">共 {{ total }} 个节点</span>
    </div>

    <div v-if="isMobile" class="np-mobile-list">
      <div v-for="row in items" :key="row.id" class="np-mobile-card" role="button" tabindex="0"
           @click="openDetail(row)" @keyup.enter="openDetail(row)">
        <div class="np-mobile-card-head">
          <div class="np-mobile-title">
            {{ row.display_name }}
            <div class="np-mobile-sub">{{ row.airport_name }} · {{ row.server }}:{{ row.port }}</div>
          </div>
          <el-tag size="small" effect="plain" :type="row.publishable ? 'success' : 'info'">
            {{ row.publishable ? '下发' : '不下发' }}
          </el-tag>
        </div>
        <div class="np-metrics">
          <div class="np-metric">
            <span class="np-metric-label">{{ targetShort('G') }} ≤ {{ row.latency_targets?.G?.cap }}</span>
            <span class="np-metric-value" :class="{ 'np-latency-over': overCap(row, 'G') }">
              {{ latencyValue(row, 'G') ?? '—' }}
            </span>
          </div>
          <div class="np-metric">
            <span class="np-metric-label">{{ targetShort('W') }} ≤ {{ row.latency_targets?.W?.cap }}</span>
            <span class="np-metric-value" :class="{ 'np-latency-over': overCap(row, 'W') }">
              {{ latencyValue(row, 'W') ?? '—' }}
            </span>
          </div>
          <div class="np-metric">
            <span class="np-metric-label">标签</span>
            <span class="np-metric-value">{{ row.passed_idents_count || 0 }}</span>
          </div>
          <div class="np-metric">
            <span class="np-metric-label">最近检测</span>
            <span class="np-metric-value muted" style="font-size:11px">{{ shortTs(row.last_tested_at) }}</span>
          </div>
        </div>
        <div class="np-mobile-meta">
          <span>{{ row.protocol }}</span>
          <span>{{ row.region || '未知地区' }}</span>
          <span v-if="row.publish_reason && !row.publishable">{{ row.publish_reason }}</span>
        </div>
      </div>
      <div v-if="!items.length" class="np-empty">当前筛选条件下没有节点</div>
    </div>

    <div v-if="!isMobile" class="np-card">
      <el-table :data="items" size="small" @row-click="openDetail">
        <el-table-column label="节点名称" min-width="260">
          <template #default="{ row }">
            <div style="font-weight:500">{{ row.display_name }}</div>
            <div class="np-muted">{{ row.server }}:{{ row.port }}</div>
          </template>
        </el-table-column>
        <el-table-column prop="airport_name" label="机场" width="110" />
        <el-table-column prop="protocol" label="协议" width="92" />
        <el-table-column prop="region" label="地区" width="70" />
        <el-table-column v-for="g in groups" :key="g.id" :label="g.ident" width="76">
          <template #default="{ row }">
            <el-tooltip v-if="groupCell(row, g.id)" :content="groupCellTip(row, g.id)" placement="top">
              <el-tag :type="groupCell(row, g.id).ok ? 'success' : 'danger'"
                      size="small" effect="plain">
                {{ groupCellText(row, g.id) }}
              </el-tag>
            </el-tooltip>
            <span v-else class="np-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="`延迟 ${targetShort('G')} / ${targetShort('W')}`" width="132">
          <template #default="{ row }">
            <div class="np-mono" style="font-size:12px">
              {{ targetShort('G') }}
              <span :class="{ 'np-latency-over': overCap(row, 'G') }">{{ latencyValue(row, 'G') ?? '—' }}</span>
              /
              {{ targetShort('W') }}
              <span :class="{ 'np-latency-over': overCap(row, 'W') }">{{ latencyValue(row, 'W') ?? '—' }}</span>
            </div>
            <div class="np-muted" style="font-size:11px">上限 {{ row.latency_targets?.G?.cap }} / {{ row.latency_targets?.W?.cap }}</div>
          </template>
        </el-table-column>
        <el-table-column label="下发" width="110">
          <template #default="{ row }">
            <el-tooltip :content="row.publish_reason || '—'" placement="top">
              <el-tag size="small" effect="plain" :type="row.publishable ? 'success' : 'info'">
                {{ row.publishable ? '下发' : '不下发' }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="最近检测" width="160">
          <template #default="{ row }">{{ formatTs(row.last_tested_at) }}</template>
        </el-table-column>
      </el-table>
      <div v-if="!items.length" class="np-empty">
        {{ total ? '没有符合筛选条件的节点' : (hasActiveFilters ? '当前筛选条件下没有节点' : '缓存池为空，请先拉取机场订阅') }}
      </div>
      <div style="display:flex;justify-content:flex-end;padding:12px">
        <el-pagination v-model:current-page="page" :page-size="filters.limit" :total="total"
                       layout="prev, pager, next" @current-change="load" />
      </div>
    </div>

    <el-dialog v-model="detailOpen" :title="detail?.display_name || '节点详情'" width="720px"
               :fullscreen="isMobile" class="node-detail-dialog">
      <el-descriptions v-if="detail" :column="detailCols" border size="small">
        <el-descriptions-item label="原始名称">{{ detail.raw_name }}</el-descriptions-item>
        <el-descriptions-item label="机场">{{ detail.airport_name }}</el-descriptions-item>
        <el-descriptions-item label="协议">{{ detail.protocol }}</el-descriptions-item>
        <el-descriptions-item label="服务器">{{ detail.server }}:{{ detail.port }}</el-descriptions-item>
        <el-descriptions-item label="地区">{{ detail.region || '—' }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ { available: '可用', unavailable: '不可用' }[detail.status] || detail.status }}</el-descriptions-item>
        <el-descriptions-item label="是否下发">{{ detail.publishable ? '下发到 /sub/all' : `不下发（${detail.publish_reason || '—'}）` }}</el-descriptions-item>
        <el-descriptions-item label="连续失败">{{ detail.fail_count }}</el-descriptions-item>
        <el-descriptions-item label="最近检测">{{ formatTs(detail.last_tested_at) }}</el-descriptions-item>
        <el-descriptions-item label="指纹" :span="2"><span class="np-mono">{{ detail.fingerprint }}</span></el-descriptions-item>
        <el-descriptions-item label="参数" :span="2">
          <div class="np-code" style="max-height:150px">{{ JSON.stringify(detail.params, null, 2) }}</div>
        </el-descriptions-item>
      </el-descriptions>
      <div class="np-section-title" style="margin-top:16px">
        延迟检测（下发依据）
        <div class="np-flex" />
        <el-button size="small" type="primary" plain
                   :loading="testing === 'latency'" :disabled="!!testing && testing !== 'latency'"
                   @click="testNode('latency')">只测延迟</el-button>
      </div>
      <el-table :data="detail?.latency_history || []" size="small" max-height="180">
        <el-table-column label="时间" :width="isMobile ? 104 : 160">
          <template #default="{ row }">{{ isMobile ? shortTs(row.ts) : formatTs(row.ts) }}</template>
        </el-table-column>
        <el-table-column label="结果" :width="isMobile ? 64 : 80">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '通过' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target" label="目标" :width="isMobile ? 64 : 88">
          <template #default="{ row }">
            <span :title="targetName(row.target)">{{ isMobile ? targetShort(row.target) : targetName(row.target) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="延迟(ms)" :width="isMobile ? 76 : 95" />
        <el-table-column v-if="!isMobile" label="错误">
          <template #default="{ row }">
            <span v-if="row.ok" class="np-muted">—</span>
            <span v-else class="np-muted">{{ errorTypeLabel(row.error_type) }}{{ row.message ? '：' + row.message : '' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="np-section-title" style="margin-top:16px">标签历史</div>
      <el-table :data="history" size="small" max-height="260">
        <el-table-column label="时间" :width="isMobile ? 104 : 160">
          <template #default="{ row }">{{ isMobile ? shortTs(row.ts) : formatTs(row.ts) }}</template>
        </el-table-column>
        <el-table-column label="节点标签" :width="isMobile ? 78 : 120">
          <template #default="{ row }">
            <span :title="`${row.group_name} (${row.ident})`">{{ isMobile ? row.ident : `${row.group_name} (${row.ident})` }}</span>
          </template>
        </el-table-column>
        <el-table-column label="结果" :width="isMobile ? 64 : 80">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '通过' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="延迟(ms)" :width="isMobile ? 76 : 95" />
        <el-table-column v-if="!isMobile" label="错误">
          <template #default="{ row }">
            <span v-if="row.ok" class="np-muted">—</span>
            <span v-else class="np-muted">{{ errorTypeLabel(row.error_type) }}{{ row.message ? '：' + row.message : '' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.np-latency-over { color: var(--el-color-danger); font-weight: 600; }
</style>
