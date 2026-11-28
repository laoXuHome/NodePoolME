<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const loading = ref(false)
const saving = ref(false)
const summary = ref(null)
const current = ref(null)
const history = ref([])
const viewDetail = ref(null)
const selectedCategory = ref('standard')

const REASON = {
  admission_ok: '准入与质量达标',
  initial_observation: '首次进入竞争，进入观察',
  observation_period: '观察期未结束，继续积累数据',
  observation_not_mature: '观察期结束但数据未成熟，继续观察',
  observation_complete: '观察期完成且满足准入，进入候选',
  candidate_confirming: '候选达标，等待连续有效确认',
  below_candidate_score: '候选未达到晋级分数',
  replacement_confirming: '已达到分差，等待第二次有效确认',
  candidate_promoted: '候选达到晋级线并完成连续确认，进入优选',
  candidate_replaced_mature: '新节点领先成熟节点达到分差并连续确认，替换进入优选',
  retained: '优选保留线达标，继续保留',
  retain_watch: '单次低于保留线，计入一次不达标但不降级',
  retain_failed: '连续不达标达到门槛，降级回观察并保留历史',
  replaced_by_new_node: '被领先达到分差的新节点替换，让出优选位置',
  speed_danger: '测速等级为危，退出当前竞争',
  business_expired: '业务资格过期，等待重新检测',
  recovery_confirming: '恢复期连续有效成功确认中',
  recovery_confirmed: '恢复连续成功达到门槛，重新进入候选',
  source_inactive: 'P1 来源失活，退出当前类别但不删除原始节点',
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
const SPEED = {
  excellent: { text: '优', color: '#10B981' },
  good: { text: '良', color: '#4682B4' },
  poor: { text: '差', color: '#F59E0B' },
  danger: { text: '危', color: '#EF4444' },
  empty: { text: '无数据', color: '#8a998f' },
  excluded: { text: '机场排除', color: '#8a998f' }
}
const TRIGGER = { manual: '手动', auto: '自动', startup: '启动', cli: '命令行' }

const s = computed(() => summary.value || {})
const displayVersion = computed(() => viewDetail.value?.version || current.value?.version || s.value.current || null)
const displayCategories = computed(() => {
  const source = viewDetail.value?.categories || current.value?.categories
  if (source && source.length) return source
  return (s.value.categories || []).map(item => ({ ...item, count: 0, nodes: [] }))
})
const displayDiff = computed(() => viewDetail.value?.diff || current.value?.diff || { categories: [] })
const historyCount = computed(() => Number(s.value.history_count || 0))
const viewedVersionId = computed(() => viewDetail.value?.version?.id || null)
const isHistoryView = computed(() => Boolean(viewDetail.value))
const activeCategory = computed(() => displayCategories.value.find(c => c.code === selectedCategory.value) || displayCategories.value[0] || { nodes: [], title: '标准综合优选' })
const activeNodes = computed(() => activeCategory.value.nodes || [])
const sourceStatus = computed(() => s.value.source?.status || null)
const sourceText = computed(() => ({
  success: 'P5 最近一轮成功', running: 'P5 正在执行', partial: 'P5 最近一轮部分完成', null: 'P5 尚未执行'
}[sourceStatus.value] || sourceStatus.value))
const canSync = computed(() => sourceStatus.value === 'success')
const pending = computed(() => Boolean(s.value.pending))
const totalChanges = computed(() => {
  const c = displayVersion.value?.change_counts || {}
  return ['added', 'removed', 'retained'].reduce((acc, k) => {
    acc[k] = Object.values(c).reduce((sum, item) => sum + Number(item?.[k] || 0), 0)
    return acc
  }, { added: 0, removed: 0, retained: 0 })
})
const diffCategories = computed(() => displayDiff.value?.categories || [])
const previousNo = computed(() => displayVersion.value?.previous_version_no || '—')

const fmt1 = (v) => (v === null || v === undefined || v === '' ? '—' : Number(v).toFixed(1))
const fmtMs = (v) => (v === null || v === undefined ? '—' : `${Math.round(Number(v))} ms`)
const fmtSpeed = (v) => (v === null || v === undefined ? '—' : Number(v) >= 1 ? `${Number(v).toFixed(2)} MB/s` : `${Math.round(Number(v) * 1024)} KB/s`)
const timeText = (v) => formatTs(v)
const shortTime = (v) => { const t = formatTs(v); return t === '—' ? t : t.slice(5, 16) }
const reasonText = (v) => REASON[v] || v || '—'
const speedOf = (v) => SPEED[v] || SPEED.empty
const countOf = (c) => Number(c?.count || 0)
const triggerText = (v) => TRIGGER[v] || v || '—'
const versionLabel = (v) => v?.version_no || '尚未生成'

async function loadAll() {
  loading.value = true
  try {
    const [sum, cur, list] = await Promise.all([
      api.get('/api/final/summary'),
      api.get('/api/final/current'),
      api.get('/api/final/versions?limit=30')
    ])
    summary.value = sum
    current.value = cur
    history.value = Array.isArray(list) ? list : []
    if (viewedVersionId.value) {
      try { viewDetail.value = await api.get(`/api/final/versions/${viewedVersionId.value}`) }
      catch { viewDetail.value = null }
    }
  } catch (e) {
    ElMessage.error(e.message || '最终优选结果加载失败')
  } finally {
    loading.value = false
  }
}

async function selectVersion(row) {
  if (!row?.id) return
  try {
    viewDetail.value = await api.get(`/api/final/versions/${row.id}`)
    selectedCategory.value = 'standard'
  } catch (e) {
    ElMessage.error(e.message || '历史版本加载失败')
  }
}

function backCurrent() {
  viewDetail.value = null
  selectedCategory.value = 'standard'
}

async function syncNow() {
  if (!canSync.value) {
    ElMessage.warning('P5 最近一轮未成功，不能生成有效优选版本')
    return
  }
  saving.value = true
  try {
    const res = await api.post('/api/final/snapshot', {})
    if (res.created) ElMessage.success(`已生成 ${res.version?.version_no || '新版本'}：共 ${res.version?.total_nodes || 0} 个优选节点`)
    else ElMessage.info('六类优选名单未发生变化，继续沿用当前版本')
    viewDetail.value = null
    await loadAll()
  } catch (e) {
    ElMessage.error(e.message || '同步优选结果失败')
  } finally {
    saving.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <div v-loading="loading" class="np-page p6-page">
    <div class="p6-head">
      <div>
        <h2 class="np-page-title">最终优选结果</h2>
      </div>
      <div class="p6-head-actions">
        <el-tag v-if="pending" type="warning" effect="plain" size="small">P5 有新变化，待同步</el-tag>
        <el-tag v-else-if="displayVersion" type="success" effect="plain" size="small">当前名单已固化</el-tag>
        <el-button size="small" @click="loadAll">刷新读取</el-button>
        <el-button size="small" type="primary" :loading="saving" :disabled="!canSync" @click="syncNow">同步当前优选</el-button>
      </div>
    </div>

    <div class="p6-overview">
      <div class="p6-stat is-total">
        <span>最终优选总量<i>TOTAL NODE ENTRIES</i></span>
        <b>{{ displayVersion?.total_nodes || 0 }}</b>
        <small>六类累计；同一节点可跨类出现</small>
      </div>
      <div class="p6-stat is-version">
        <span>当前有效版本<i>VERSION</i></span>
        <b class="p6-version">{{ versionLabel(displayVersion) }}</b>
        <small>{{ isHistoryView ? '正在查看历史快照' : '客户端读取不会生成新版本' }}</small>
      </div>
      <div class="p6-stat">
        <span>生成时间<i>GENERATED</i></span>
        <b class="p6-time">{{ timeText(displayVersion?.created_at) }}</b>
        <small>触发 {{ triggerText(displayVersion?.trigger) }} · 来源 P5 #{{ displayVersion?.source_run_id || '—' }}</small>
      </div>
      <div class="p6-stat is-added">
        <span>本次新增<i>ADDED</i></span>
        <b>+{{ totalChanges.added }}</b>
        <small>进入优选名单的节点/类别</small>
      </div>
      <div class="p6-stat is-removed">
        <span>本次移除<i>REMOVED</i></span>
        <b>−{{ totalChanges.removed }}</b>
        <small>退出优选，不删除 P1 节点</small>
      </div>
      <div class="p6-stat is-retained">
        <span>本次保留<i>RETAINED</i></span>
        <b>{{ totalChanges.retained }}</b>
        <small>继续保留的优选节点/类别</small>
      </div>
    </div>

    <div class="p6-statusline">
      <span class="p6-source" :class="sourceStatus || 'none'"><i></i>{{ sourceText }}</span>
      <span>历史版本 <b>{{ historyCount }}</b> 个</span>
      <span v-if="displayVersion?.is_empty" class="p6-empty-flag">当前为有效空版本，不沿用旧名单</span>
      <button v-if="isHistoryView" class="p6-back" @click="backCurrent">← 返回当前版本</button>
    </div>

    <div class="p6-cat-grid">
      <section v-for="c in displayCategories" :key="c.code" :class="['np-card', 'p6-cat', `is-${c.code}`, { active: c.code === selectedCategory }]">
        <button class="p6-cat-head" @click="selectedCategory = c.code">
          <div><span>{{ c.title }}</span><i>{{ c.short }}</i></div>
          <b>{{ countOf(c) }}</b>
        </button>
        <div class="p6-cat-list">
          <button v-for="n in c.nodes" :key="`${c.code}-${n.node_id}`" class="p6-cat-node" @click="selectedCategory = c.code">
            <div>
              <span>{{ n.node_name }}</span>
              <small>{{ n.airport_name || '未归属机场' }}</small>
            </div>
            <div class="p6-cat-meta"><b>{{ fmt1(n.score) }}</b><i>{{ fmtMs(n.p80_ms) }}</i></div>
          </button>
          <div v-if="!c.nodes.length" class="p6-cat-empty">本版本该类优选为空</div>
        </div>
      </section>
    </div>

    <div class="np-card p6-detail-card">
      <div class="p6-section-head">
        <div>
          <h3>{{ activeCategory.title }} · 完整名单</h3>
          <p>只读展示 P5 当前优选快照；分数、成熟度、延迟和原因均为版本写入时的证据。</p>
        </div>
        <el-tag effect="plain" type="info">{{ activeNodes.length }} 个节点</el-tag>
      </div>

      <el-table v-if="activeNodes.length" class="p6-table" :data="activeNodes" size="small" row-key="node_id">
        <el-table-column label="节点 / 机场" min-width="210">
          <template #default="{ row }">
            <div class="p6-node-name">{{ row.node_name }}</div>
            <div class="p6-tiny">{{ row.airport_name || '未归属机场' }} · {{ row.server }}:{{ row.port }}</div>
          </template>
        </el-table-column>
        <el-table-column label="优选分数" width="95">
          <template #default="{ row }"><b class="p6-score">{{ fmt1(row.score) }}</b><div class="p6-tiny">P4 七天质量</div></template>
        </el-table-column>
        <el-table-column label="成熟度" width="100">
          <template #default="{ row }"><b>{{ fmt1(row.maturity) }}%</b><div class="p6-tiny">数据成熟</div></template>
        </el-table-column>
        <el-table-column label="P80 延迟" width="105">
          <template #default="{ row }"><b>{{ fmtMs(row.p80_ms) }}</b><div class="p6-tiny">{{ row.delay_grade || '无样本' }}</div></template>
        </el-table-column>
        <el-table-column label="下载速度" width="128">
          <template #default="{ row }">
            <b :style="{ color: speedOf(row.speed_grade).color }">{{ speedOf(row.speed_grade).text }}</b>
            <div class="p6-tiny">{{ fmtSpeed(row.speed_mb_s) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="优选起点" width="118">
          <template #default="{ row }">{{ shortTime(row.preferred_since) }}</template>
        </el-table-column>
        <el-table-column label="最近原因" min-width="190">
          <template #default="{ row }"><span class="p6-reason">{{ reasonText(row.last_reason) }}</span></template>
        </el-table-column>
      </el-table>
      <div v-else class="p6-empty-state">该类别在当前版本中没有优选节点。空结果仍是有效结果，不继承旧版本节点。</div>

      <div class="p6-mobile-list">
        <div v-for="row in activeNodes" :key="row.node_id" class="p6-mcard">
          <div class="p6-mcard-head">
            <div><b>{{ row.node_name }}</b><small>{{ row.airport_name || '未归属机场' }} · {{ row.server }}:{{ row.port }}</small></div>
            <strong>{{ fmt1(row.score) }}</strong>
          </div>
          <div class="p6-mcard-grid">
            <div><span>成熟度</span><b>{{ fmt1(row.maturity) }}%</b></div>
            <div><span>P80</span><b>{{ fmtMs(row.p80_ms) }}</b></div>
            <div><span>速度</span><b :style="{ color: speedOf(row.speed_grade).color }">{{ speedOf(row.speed_grade).text }} · {{ fmtSpeed(row.speed_mb_s) }}</b></div>
            <div><span>优选起点</span><b>{{ shortTime(row.preferred_since) }}</b></div>
          </div>
          <p>{{ reasonText(row.last_reason) }}</p>
        </div>
        <div v-if="!activeNodes.length" class="p6-empty-state">该类别在当前版本中没有优选节点。</div>
      </div>
    </div>

    <div class="np-card p6-diff-card">
      <div class="p6-section-head">
        <div>
          <h3>版本差异 · {{ versionLabel(displayVersion) }} ↔ {{ previousNo }}</h3>
          <p>按类别独立计算新增、移除和保留，节点跨类别变化不会被合并或互相覆盖。</p>
        </div>
        <div class="p6-diff-totals">
          <span class="is-added">+{{ totalChanges.added }} 新增</span>
          <span class="is-removed">−{{ totalChanges.removed }} 移除</span>
          <span class="is-retained">{{ totalChanges.retained }} 保留</span>
        </div>
      </div>
      <div class="p6-diff-grid">
        <div v-for="c in diffCategories" :key="c.code" class="p6-diff-cat">
          <div class="p6-diff-head"><b>{{ c.title }}</b><span>+{{ c.added.length }} / −{{ c.removed.length }} / ={{ c.retained.length }}</span></div>
          <div class="p6-diff-row">
            <i class="is-added">新增</i>
            <div>
              <span v-for="n in c.added" :key="`a-${n.node_id}`">+ {{ n.node_name }} <small>{{ n.airport_name }}</small></span>
              <em v-if="!c.added.length">无</em>
            </div>
          </div>
          <div class="p6-diff-row">
            <i class="is-removed">移除</i>
            <div>
              <span v-for="n in c.removed" :key="`r-${n.node_id}`">− {{ n.node_name }} <small>{{ n.airport_name }}</small></span>
              <em v-if="!c.removed.length">无</em>
            </div>
          </div>
          <div class="p6-diff-row">
            <i class="is-retained">保留</i>
            <div>
              <span v-for="n in c.retained" :key="`k-${n.node_id}`">= {{ n.node_name }} <small>{{ n.airport_name }}</small></span>
              <em v-if="!c.retained.length">无</em>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="np-card p6-history-card">
      <div class="p6-section-head">
        <div>
          <h3>历史版本</h3>
          <p>快照只追加不覆盖；点击版本查看六类完整名单与该版本对上一有效版本的差异。</p>
        </div>
        <el-tag effect="plain" type="info">最多展示 30 个</el-tag>
      </div>
      <el-table v-if="history.length" class="p6-history-table" :data="history" size="small" row-key="id" @row-click="selectVersion">
        <el-table-column label="版本号" width="120">
          <template #default="{ row }"><b :class="{ 'p6-current-version': row.id === displayVersion?.id }">{{ row.version_no }}</b><div v-if="row.id === displayVersion?.id" class="p6-tiny">正在查看</div></template>
        </el-table-column>
        <el-table-column label="生成时间" width="165">
          <template #default="{ row }">{{ timeText(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="触发" width="80">
          <template #default="{ row }">{{ triggerText(row.trigger) }}</template>
        </el-table-column>
        <el-table-column label="总量" width="70">
          <template #default="{ row }"><b>{{ row.total_nodes }}</b></template>
        </el-table-column>
        <el-table-column label="六类计数" min-width="220">
          <template #default="{ row }">
            <div class="p6-count-chips">
              <span v-for="code in ['standard','gpt','youtube','telegram','github','global']" :key="code">{{ code.slice(0, 2).toUpperCase() }} {{ row.category_counts?.[code] || 0 }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="新增 / 移除 / 保留" width="160">
          <template #default="{ row }">
            <span class="p6-delta">+{{ Object.values(row.change_counts || {}).reduce((s, c) => s + Number(c?.added || 0), 0) }}</span>
            <span class="p6-delta is-removed">−{{ Object.values(row.change_counts || {}).reduce((s, c) => s + Number(c?.removed || 0), 0) }}</span>
            <span class="p6-delta is-retained">{{ Object.values(row.change_counts || {}).reduce((s, c) => s + Number(c?.retained || 0), 0) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_empty ? 'warning' : 'success'" effect="plain">{{ row.is_empty ? '有效空版本' : '有效' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="" width="70">
          <template #default="{ row }"><el-button link type="primary" size="small" @click.stop="selectVersion(row)">查看</el-button></template>
        </el-table-column>
      </el-table>
      <div v-else class="p6-empty-state">尚无历史版本。P5 产生成功竞争结果后，点击“同步当前优选”生成第一个有效快照。</div>

      <div class="p6-history-mobile">
        <button v-for="row in history" :key="row.id" :class="['p6-hcard', { active: row.id === displayVersion?.id }]" @click="selectVersion(row)">
          <div class="p6-hcard-top"><b>{{ row.version_no }}</b><span>{{ timeText(row.created_at) }}</span></div>
          <div class="p6-hcard-mid"><strong>{{ row.total_nodes }}</strong><span>节点/类别</span><i>+{{ Object.values(row.change_counts || {}).reduce((s, c) => s + Number(c?.added || 0), 0) }} / −{{ Object.values(row.change_counts || {}).reduce((s, c) => s + Number(c?.removed || 0), 0) }} / ={{ Object.values(row.change_counts || {}).reduce((s, c) => s + Number(c?.retained || 0), 0) }}</i></div>
          <div class="p6-hcard-bot"><el-tag size="small" :type="row.is_empty ? 'warning' : 'success'" effect="plain">{{ row.is_empty ? '有效空版本' : '有效' }}</el-tag><span>{{ triggerText(row.trigger) }}</span></div>
        </button>
        <div v-if="!history.length" class="p6-empty-state">尚无历史版本。</div>
      </div>
    </div>

    <div class="p6-boundary">
      <b>P6.1 边界</b>
      <span>只读 P1–P5，不修改竞争状态、评分、资格和规则。</span>
      <span>待定订阅池仅承载内部优选结果，不等于健康池，也不等于实际发布订阅。</span>
      <span>关键词过滤、订阅生成、发布入口和 P6.2/P7 均未在本批实施。</span>
    </div>
  </div>
</template>

<style scoped>
.p6-page { max-width: 1600px; }
.p6-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 14px; }
.p6-head-actions { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; justify-content: flex-end; }
.p6-overview { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 9px; margin-bottom: 9px; }
.p6-stat { text-align: left; background: var(--np-panel-solid); border: 1px solid rgba(31,61,51,.09); border-radius: 13px; padding: 11px 13px 9px; border-top: 3px solid #7e9c8d; }
.p6-stat.is-total { border-top-color: #2f8f62; }
.p6-stat.is-version { border-top-color: #b07d26; }
.p6-stat.is-added { border-top-color: #3f7fb0; }
.p6-stat.is-removed { border-top-color: #a34f4f; }
.p6-stat.is-retained { border-top-color: #748aa0; }
.p6-stat span { display: flex; justify-content: space-between; gap: 6px; font-size: 12px; color: #6f8378; }
.p6-stat span i { font-style: normal; font-size: 8px; letter-spacing: .04em; color: #9aa79e; white-space: nowrap; }
.p6-stat b { display: block; font-size: 27px; color: #1c332b; line-height: 1.15; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.p6-stat b.p6-version { font-size: 20px; margin-top: 6px; }
.p6-stat b.p6-time { font-size: 15px; margin-top: 8px; }
.p6-stat small { font-size: 10px; color: #8a998f; }
.p6-statusline { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 0 1px 11px; font-size: 11px; color: #768a7f; }
.p6-source { display: inline-flex; align-items: center; gap: 5px; }
.p6-source i { width: 6px; height: 6px; border-radius: 50%; background: #9aa79e; }
.p6-source.success i { background: #2f8f62; }
.p6-source.running i, .p6-source.partial i { background: #b07d26; }
.p6-empty-flag { padding: 1px 7px; border-radius: 5px; background: rgba(176,125,38,.1); color: #8a611d; }
.p6-back { margin-left: auto; border: 0; background: none; color: #2f6f5b; font-size: 11px; cursor: pointer; padding: 2px 0; }
.p6-cat-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.p6-cat { padding: 0; overflow: hidden; border-top: 3px solid #7e9c8d; }
.p6-cat.is-standard { border-top-color: #2f8f62; }
.p6-cat.is-gpt { border-top-color: #b07d26; }
.p6-cat.is-youtube { border-top-color: #a34f4f; }
.p6-cat.is-telegram { border-top-color: #3f7fb0; }
.p6-cat.is-github { border-top-color: #5d7f70; }
.p6-cat.is-global { border-top-color: #7c6fa8; }
.p6-cat.active { box-shadow: 0 6px 20px rgba(47,143,98,.13); border-color: rgba(47,143,98,.32); }
.p6-cat-head { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 13px 8px; background: none; border: 0; cursor: pointer; text-align: left; }
.p6-cat-head div { display: flex; align-items: baseline; gap: 7px; }
.p6-cat-head span { font-size: 12px; font-weight: 700; color: #29433a; }
.p6-cat-head i { font-style: normal; font-size: 8px; letter-spacing: .04em; color: #9aa79e; }
.p6-cat-head > b { font-size: 23px; color: #1c332b; font-variant-numeric: tabular-nums; }
.p6-cat-list { max-height: 210px; overflow: auto; padding: 0 7px 7px; }
.p6-cat-node { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 5px 6px; border: 0; background: none; text-align: left; cursor: pointer; border-radius: 7px; }
.p6-cat-node:hover { background: rgba(47,143,98,.07); }
.p6-cat-node div:first-child { min-width: 0; }
.p6-cat-node span { display: block; font-size: 11px; color: #334d42; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.p6-cat-node small { display: block; font-size: 9px; color: #8a998f; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.p6-cat-meta { text-align: right; flex: none; }
.p6-cat-meta b { display: block; font-size: 11px; color: #2f6f5b; }
.p6-cat-meta i { display: block; font-style: normal; font-size: 9px; color: #9aa79e; }
.p6-cat-empty { padding: 16px 8px; text-align: center; font-size: 10px; color: #9aa79e; }
.p6-detail-card, .p6-diff-card, .p6-history-card { padding: 13px 14px 6px; margin-bottom: 12px; }
.p6-section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; flex-wrap: wrap; padding: 0 1px 10px; }
.p6-section-head h3 { margin: 0; font-size: 14px; color: #203730; }
.p6-section-head p { margin: 3px 0 0; max-width: 880px; font-size: 10px; line-height: 1.6; color: #7c8d84; }
.p6-node-name { font-size: 12px; font-weight: 700; color: #263f35; }
.p6-tiny { font-size: 9px; color: #8a998f; }
.p6-score { color: #2f6f5b; font-size: 14px; font-variant-numeric: tabular-nums; }
.p6-reason { font-size: 10px; color: #5d776a; }
.p6-empty-state { padding: 28px 12px; text-align: center; font-size: 11px; color: #8a998f; }
.p6-mobile-list, .p6-history-mobile { display: none; }
.p6-mcard { border: 1px solid rgba(31,61,51,.08); border-radius: 10px; padding: 9px 10px; margin-bottom: 7px; background: rgba(240,246,242,.36); }
.p6-mcard-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
.p6-mcard-head b { display: block; font-size: 12px; color: #29433a; }
.p6-mcard-head small { display: block; font-size: 9px; color: #8a998f; }
.p6-mcard-head strong { font-size: 17px; color: #2f6f5b; }
.p6-mcard-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 5px 10px; margin: 8px 0 5px; }
.p6-mcard-grid span { display: block; font-size: 8px; color: #8a998f; }
.p6-mcard-grid b { font-size: 10px; color: #385046; }
.p6-mcard p { margin: 0; font-size: 9px; color: #6f8378; }
.p6-diff-totals { display: flex; gap: 8px; flex-wrap: wrap; font-size: 10px; }
.p6-diff-totals span { padding: 2px 7px; border-radius: 5px; background: rgba(47,143,98,.09); color: #2f6f5b; }
.p6-diff-totals .is-removed { background: rgba(163,79,79,.09); color: #8f4545; }
.p6-diff-totals .is-retained { background: rgba(95,120,140,.1); color: #526a7c; }
.p6-diff-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.p6-diff-cat { border: 1px solid rgba(31,61,51,.08); border-radius: 9px; padding: 8px 9px; background: rgba(240,246,242,.36); }
.p6-diff-head { display: flex; justify-content: space-between; align-items: baseline; gap: 6px; margin-bottom: 6px; }
.p6-diff-head b { font-size: 11px; color: #29433a; }
.p6-diff-head span { font-size: 9px; color: #7c8d84; }
.p6-diff-row { display: grid; grid-template-columns: 34px 1fr; gap: 5px; padding: 4px 0; border-top: 1px dashed rgba(31,61,51,.08); }
.p6-diff-row i { font-style: normal; font-size: 9px; text-align: center; border-radius: 3px; height: 16px; line-height: 16px; }
.p6-diff-row i.is-added { background: rgba(47,143,98,.12); color: #2f6f5b; }
.p6-diff-row i.is-removed { background: rgba(163,79,79,.12); color: #8f4545; }
.p6-diff-row i.is-retained { background: rgba(95,120,140,.12); color: #526a7c; }
.p6-diff-row div { min-width: 0; }
.p6-diff-row span { display: block; font-size: 9px; color: #385046; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.p6-diff-row small { color: #9aa79e; }
.p6-diff-row em { font-style: normal; font-size: 9px; color: #a3aea7; }
.p6-count-chips { display: flex; gap: 3px; flex-wrap: wrap; }
.p6-count-chips span { font-size: 8px; padding: 1px 3px; border-radius: 3px; background: rgba(47,143,98,.08); color: #526a5e; }
.p6-delta { display: inline-block; font-size: 10px; margin-right: 4px; color: #2f6f5b; }
.p6-delta.is-removed { color: #8f4545; }
.p6-delta.is-retained { color: #526a7c; }
.p6-current-version { color: #2f8f62; }
.p6-hcard { width: 100%; text-align: left; border: 1px solid rgba(31,61,51,.09); border-radius: 10px; background: rgba(240,246,242,.38); padding: 9px 10px; margin-bottom: 7px; }
.p6-hcard.active { border-color: rgba(47,143,98,.42); background: #eef6f0; }
.p6-hcard-top { display: flex; justify-content: space-between; gap: 6px; }
.p6-hcard-top b { font-size: 11px; color: #29433a; }
.p6-hcard-top span { font-size: 9px; color: #8a998f; }
.p6-hcard-mid { display: flex; align-items: baseline; gap: 6px; margin: 6px 0; }
.p6-hcard-mid strong { font-size: 19px; color: #1c332b; }
.p6-hcard-mid span { font-size: 9px; color: #8a998f; }
.p6-hcard-mid i { margin-left: auto; font-style: normal; font-size: 9px; color: #6f8378; }
.p6-hcard-bot { display: flex; justify-content: space-between; align-items: center; }
.p6-hcard-bot span { font-size: 9px; color: #8a998f; }
.p6-boundary { display: flex; gap: 10px; flex-wrap: wrap; align-items: baseline; padding: 2px 2px 6px; font-size: 10px; line-height: 1.6; color: #7c8d84; }
.p6-boundary b { color: #526a5e; }
@media (max-width: 1180px) {
  .p6-overview { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .p6-cat-grid, .p6-diff-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 860px) {
  .p6-head { flex-direction: column; gap: 9px; }
  .p6-head-actions { width: 100%; justify-content: flex-start; }
  .p6-overview { grid-template-columns: 1fr 1fr; gap: 7px; }
  .p6-stat { padding: 9px 10px 8px; }
  .p6-stat span i { display: none; }
  .p6-stat span { min-height: 17px; }
  .p6-stat b { font-size: 22px; }
  .p6-stat b.p6-version { font-size: 16px; }
  .p6-stat b.p6-time { font-size: 12px; }
  .p6-statusline { gap: 8px; }
  .p6-cat-grid, .p6-diff-grid { grid-template-columns: 1fr; }
  .p6-cat-list { max-height: 240px; }
  .p6-table, .p6-history-table { display: none; }
  .p6-mobile-list, .p6-history-mobile { display: block; }
  .p6-detail-card, .p6-diff-card, .p6-history-card { padding: 11px 10px 6px; }
  .p6-section-head h3 { font-size: 13px; }
  .p6-back { margin-left: 0; }
  .p6-boundary { display: block; }
  .p6-boundary span { display: block; margin-top: 2px; }
}
</style>
