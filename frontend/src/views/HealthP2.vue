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
const filters = ref({ q: '', state: '', classification: '' })
const detail = ref(null)
const drawer = ref(false)
const detailLoading = ref(false)
const targets = ref([])
const targetDialog = ref(false)
const targetForm = ref({ id: null, name: '', url: '', enabled: true })
const targetBusy = ref(false)
const testBusy = ref(false)
const testResult = ref(null)
const testDialog = ref(false)
const settings = ref(null)
const settingsDialog = ref(false)
const settingsForm = ref({ interval_minutes: 30, concurrency: 10 })

const STATE = {
  healthy: { text: '健康', type: 'success' },
  new: { text: '新节点', type: 'info' },
  exception: { text: '异常待确认', type: 'warning' },
  delay: { text: '延迟观察', type: 'warning' },
  recovering: { text: '恢复中', type: 'warning' },
  unhealthy: { text: '不健康', type: 'danger' },
  unknown: { text: '未知', type: 'info' }
}
const CLASS = {
  success: { text: 'Success', type: 'success' },
  failure: { text: 'Failure', type: 'danger' },
  unknown: { text: 'Unknown', type: 'info' }
}
const LAYER = {
  raw: { text: '原始池', type: 'info' },
  healthy: { text: '健康池', type: 'success' },
  observation: { text: '观察池', type: 'warning' }
}
const BASE = {
  healthy: { text: '健康', type: 'success' },
  unhealthy: { text: '不健康', type: 'danger' },
  unknown: { text: '未知', type: 'info' }
}
const STAGE_TEXT = { dns: 'DNS', tcp: 'TCP×5', tls: 'TLS', https: 'HTTPS' }
const TRIGGER_TEXT = {
  normal: '正常周期', exception_recheck: '快速复查', observation: '观察复查',
  recovery: '恢复检查', manual: '手动检测', target: '目标测试', config: '配置重检'
}
const REASON_TEXT = {
  exception_pending: '异常待确认', recovery_pending: '恢复确认中', health_unhealthy: '连续失败',
  tcp_p80_observe: 'TCP P80 进入观察区间', tcp_p80_exceeded: 'TCP P80 超 800ms',
  tcp_all_failed: 'TCP 五次连接全部失败', probe_target_unavailable: '检测目标自身不可用',
  engine_unavailable: '检测内核不可用', probe_unknown: '检测结果 Unknown',
  probe_config_changed: '检测目标配置变更', new: '新采集', insufficient_data: '数据不足'
}
const stateOf = (v) => STATE[v] || { text: v || '—', type: 'info' }
const classOf = (v) => CLASS[v] || { text: v || '—', type: 'info' }
const layerOf = (v) => LAYER[v] || { text: v || '—', type: 'info' }
const baseOf = (v) => BASE[v] || { text: v || '—', type: 'info' }
const reasonText = (v) => REASON_TEXT[v] || v || '—'
const shortTs = (ts) => { const t = formatTs(ts); return t === '—' ? t : t.slice(5, 16) }

const metrics = computed(() => summary.value?.states || {})
const targetOk = computed(() => targets.value.filter(t => t.enabled).length)

async function loadSummary() {
  summary.value = await api.get('/api/health/summary')
}
async function loadTargets() {
  targets.value = (await api.get('/api/health/targets')).items || []
}
async function loadNodes() {
  loading.value = true
  try {
    const params = new URLSearchParams()
    Object.entries(filters.value).forEach(([k, v]) => { if (v !== '' && v != null) params.set(k, v) })
    params.set('page', String(page.value))
    params.set('per_page', String(perPage))
    const res = await api.get(`/api/health/nodes?${params.toString()}`)
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
  try { detail.value = await api.get(`/api/health/nodes/${row.id}`) }
  catch (e) { ElMessage.error(e.message) }
  finally { detailLoading.value = false }
}
function openTarget(row) {
  targetForm.value = row
    ? { id: row.id, name: row.name, url: row.url, enabled: row.enabled }
    : { id: null, name: '', url: 'https://', enabled: true }
  targetDialog.value = true
}
async function saveTarget() {
  if (!targetForm.value.name.trim() || !targetForm.value.url.trim()) {
    ElMessage.warning('名称和地址不能为空'); return
  }
  targetBusy.value = true
  try {
    if (targetForm.value.id) {
      await api.patch(`/api/health/targets/${targetForm.value.id}`, {
        name: targetForm.value.name.trim(), url: targetForm.value.url.trim(),
        enabled: targetForm.value.enabled
      })
    } else {
      await api.post('/api/health/targets', {
        name: targetForm.value.name.trim(), url: targetForm.value.url.trim(),
        enabled: targetForm.value.enabled
      })
    }
    targetDialog.value = false
    ElMessage.success('检测目标已保存；技术配置变更会触发全量重检')
    await reload()
  } catch (e) { ElMessage.error(e.message) } finally { targetBusy.value = false }
}
async function toggleTarget(row, value) {
  try {
    await api.patch(`/api/health/targets/${row.id}`, { enabled: value })
    ElMessage.success(value ? '目标已启用' : '目标已停用')
    await reload()
  } catch (e) { ElMessage.error(e.message); await loadTargets() }
}
async function removeTarget(row) {
  try {
    await ElMessageBox.confirm(`删除检测目标「${row.name}」？历史记录会保留。`, '确认删除', { type: 'warning' })
    await api.delete(`/api/health/targets/${row.id}`)
    ElMessage.success('已删除，节点将按新目标重新检测')
    await reload()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}
async function testTarget(row) {
  testBusy.value = true
  try {
    testResult.value = await api.post(`/api/health/targets/${row.id}/test`, {})
    testDialog.value = true
    await loadTargets()
  } catch (e) { ElMessage.error(e.message) } finally { testBusy.value = false }
}
async function restoreDefaults() {
  try {
    await ElMessageBox.confirm('恢复 Google / Wikipedia 两个默认目标？自定义目标将被移除，并触发全量重检。',
      '恢复默认', { type: 'warning' })
    await api.post('/api/health/targets/defaults', {})
    ElMessage.success('已恢复默认目标')
    await reload()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message) }
}
async function checkNode(row) {
  try {
    const res = await api.post(`/api/health/nodes/${row.id}/check`, {})
    ElMessage.success(res.queued ? '已加入检测队列' : '该节点已在队列中，不会重复排队')
    await reload()
  } catch (e) { ElMessage.error(e.message) }
}
async function checkAll() {
  try {
    const res = await api.post('/api/health/check', {})
    ElMessage.success(`已入队 ${res.queued} 个节点；已在队列中的节点不会重复排队`)
    await reload()
  } catch (e) { ElMessage.error(e.message) }
}
async function openSettings() {
  settings.value = await api.get('/api/health/settings')
  settingsForm.value = {
    interval_minutes: settings.value.interval_minutes,
    concurrency: settings.value.concurrency
  }
  settingsDialog.value = true
}
async function saveSettings() {
  try {
    settings.value = await api.patch('/api/health/settings', settingsForm.value)
    settingsDialog.value = false
    ElMessage.success('检测节奏已保存')
    await loadSummary()
  } catch (e) { ElMessage.error(e.message) }
}
const activeTargets = computed(() => detail.value?.latest?.targets || [])
const stageOf = (target, name) => (target.stages || []).find(s => s.stage === name)

onMounted(reload)
</script>

<template>
  <div v-loading="loading" class="np-page p2-page">
    <div class="p2-head">
      <div>
        <h2 class="np-page-title">基础健康检测</h2>
      </div>
      <div class="p2-head-actions">
        <el-button size="small" @click="openSettings">检测设置</el-button>
        <el-button size="small" type="primary" @click="checkAll">立即检测全部</el-button>
        <el-button size="small" @click="reload">刷新</el-button>
      </div>
    </div>

    <div class="p2-stats">
      <div class="p2-stat is-healthy">
        <span>健康</span><b>{{ metrics.healthy || 0 }}</b><small>基础健康通过且数据充足</small>
      </div>
      <div class="p2-stat is-watch">
        <span>观察 / 复查</span><b>{{ (metrics.exception || 0) + (metrics.recovering || 0) + (metrics.delay || 0) }}</b>
        <small>异常待确认、延迟观察与恢复确认</small>
      </div>
      <div class="p2-stat is-bad">
        <span>不健康</span><b>{{ metrics.unhealthy || 0 }}</b><small>连续 3 轮有效失败</small>
      </div>
      <div class="p2-stat is-unknown">
        <span>未知 / 新节点</span><b>{{ (metrics.unknown || 0) + (metrics.new || 0) }}</b>
        <small>目标故障或数据尚未建立</small>
      </div>
    </div>

    <div class="np-card p2-target-card">
      <div class="p2-section-head">
        <div>
          <h3>检测目标</h3>
          <p class="np-muted">启用目标 {{ targetOk }} 个 · 至少保留 2 个；所有启用目标均参与判定。目标配置变更会清空旧结论并全量重检。</p>
        </div>
        <div class="p2-inline-actions">
          <el-button size="small" @click="restoreDefaults">恢复默认</el-button>
          <el-button size="small" type="primary" @click="openTarget(null)">新增目标</el-button>
        </div>
      </div>
      <div class="p2-target-list">
        <div v-for="t in targets" :key="t.id" class="p2-target" :class="{ off: !t.enabled }">
          <div class="p2-target-main">
            <div class="p2-target-title">
              <b>{{ t.name }}</b>
              <el-tag size="small" :type="t.enabled ? 'success' : 'info'" effect="plain">
                {{ t.enabled ? '启用' : '停用' }}
              </el-tag>
            </div>
            <span class="p2-target-url">{{ t.url }}</span>
            <div class="p2-target-meta">
              <span>最近测试 {{ shortTs(t.last_check_at) }}</span>
              <span v-if="t.last_check_status">·
                <em :class="`is-${t.last_check_status}`">{{ classOf(t.last_check_status).text }}</em>
              </span>
              <span v-if="t.last_check_error" class="p2-err">· {{ reasonText(t.last_check_error) }}</span>
            </div>
          </div>
          <div class="p2-target-actions">
            <el-switch :model-value="t.enabled" size="small" @change="v => toggleTarget(t, v)" />
            <el-button link size="small" :loading="testBusy" @click="testTarget(t)">测试</el-button>
            <el-button link size="small" @click="openTarget(t)">编辑</el-button>
            <el-button link type="danger" size="small" @click="removeTarget(t)">删除</el-button>
          </div>
        </div>
      </div>
    </div>

    <div class="p2-toolbar">
      <el-input v-model="filters.q" placeholder="节点名 / 服务器 / 机场" clearable style="width: 240px"
                @keyup.enter="page = 1; loadNodes()" />
      <el-select v-model="filters.state" placeholder="检测状态" clearable style="width: 140px" @change="page = 1; loadNodes()">
        <el-option v-for="(v, k) in STATE" :key="k" :label="v.text" :value="k" />
      </el-select>
      <el-select v-model="filters.classification" placeholder="最近分类" clearable style="width: 130px" @change="page = 1; loadNodes()">
        <el-option label="Success" value="success" />
        <el-option label="Failure" value="failure" />
        <el-option label="Unknown" value="unknown" />
      </el-select>
      <el-button size="small" @click="page = 1; loadNodes()">筛选</el-button>
      <span class="np-muted p2-count">共 {{ total }} 个有效来源节点</span>
    </div>

    <div class="np-card p2-table-card">
      <el-table :data="items" size="small" row-key="id" @row-click="openDetail">
        <el-table-column label="节点" min-width="190">
          <template #default="{ row }">
            <div class="p2-node-name">{{ row.current_name }}</div>
            <div class="np-muted p2-node-sub">{{ row.server }}:{{ row.port }} · {{ row.source_name }}</div>
          </template>
        </el-table-column>
        <el-table-column label="基础健康" width="105">
          <template #default="{ row }">
            <el-tag size="small" :type="baseOf(row.base_health).type" effect="plain">{{ baseOf(row.base_health).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="检测状态" width="125">
          <template #default="{ row }">
            <el-tag size="small" :type="stateOf(row.probe_state).type" effect="light">{{ stateOf(row.probe_state).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近分类" width="100">
          <template #default="{ row }">
            <span :class="`p2-class is-${row.last_status || 'none'}`">{{ classOf(row.last_status).text }}</span>
          </template>
        </el-table-column>
        <el-table-column label="TCP P80" width="105">
          <template #default="{ row }">
            <b v-if="row.p80_ms != null" class="p2-p80">{{ row.p80_ms }}<small>ms</small></b>
            <span v-else class="np-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="连续失败 / 成功" width="125">
          <template #default="{ row }">
            <span class="p2-counters"><em>{{ row.consecutive_failures || 0 }}</em> 失 /
              <em>{{ row.consecutive_successes || 0 }}</em> 成</span>
          </template>
        </el-table-column>
        <el-table-column label="下次检测" width="125">
          <template #default="{ row }">{{ shortTs(row.next_due_at) }}</template>
        </el-table-column>
        <el-table-column label="最近检测" width="125">
          <template #default="{ row }">{{ shortTs(row.last_run_at || row.check_finished_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="92" fixed="right">
          <template #default="{ row }">
            <el-button link size="small" type="primary" @click.stop="checkNode(row)">立即检测</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="p2-pager">
        <el-pagination v-model:current-page="page" :page-size="perPage" :total="total"
                       layout="prev, pager, next" small @current-change="loadNodes" />
      </div>
    </div>

    <div class="p2-mobile-list">
      <div v-for="row in items" :key="row.id" class="np-card p2-mcard" @click="openDetail(row)">
        <div class="p2-mcard-head">
          <div>
            <b>{{ row.current_name }}</b>
            <div class="np-muted p2-node-sub">{{ row.server }}:{{ row.port }} · {{ row.source_name }}</div>
          </div>
          <el-tag size="small" :type="stateOf(row.probe_state).type">{{ stateOf(row.probe_state).text }}</el-tag>
        </div>
        <div class="p2-mchips">
          <span class="p2-chip">基础 {{ baseOf(row.base_health).text }}</span>
          <span class="p2-chip">P80 {{ row.p80_ms == null ? '—' : row.p80_ms + 'ms' }}</span>
          <span class="p2-chip">{{ classOf(row.last_status).text }}</span>
          <span class="p2-chip">失败 {{ row.consecutive_failures || 0 }}</span>
        </div>
        <div class="p2-mcard-foot">
          <span class="np-muted">下次 {{ shortTs(row.next_due_at) }}</span>
          <el-button link type="primary" size="small" @click.stop="checkNode(row)">立即检测</el-button>
        </div>
      </div>
      <el-pagination v-model:current-page="page" :page-size="perPage" :total="total"
                     layout="prev, pager, next" small @current-change="loadNodes" />
    </div>

    <!-- target edit -->
    <el-dialog v-model="targetDialog" :title="targetForm.id ? '编辑检测目标' : '新增检测目标'" width="520px">
      <el-form label-width="78px" size="default">
        <el-form-item label="名称"><el-input v-model="targetForm.name" maxlength="40" placeholder="例如 Google" /></el-form-item>
        <el-form-item label="检测地址"><el-input v-model="targetForm.url" placeholder="https://www.google.com/generate_204" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="targetForm.enabled" /></el-form-item>
      </el-form>
      <p class="np-muted p2-dialog-note">
        名称修改不影响历史数据；地址、启用状态、新增或删除会提升配置版本，旧检测结论作废并在新配置下重新检测。
      </p>
      <template #footer>
        <el-button @click="targetDialog = false">取消</el-button>
        <el-button type="primary" :loading="targetBusy" @click="saveTarget">保存</el-button>
      </template>
    </el-dialog>

    <!-- target test -->
    <el-dialog v-model="testDialog" title="检测目标手动测试" width="640px">
      <template v-if="testResult">
        <el-alert :type="testResult.available ? 'success' : 'warning'" :closable="false"
                  :title="testResult.available ? '目标从控制端可达' : '目标暂不可用，节点结果将记为 Unknown'" />
        <div class="p2-test-meta">
          <span>{{ testResult.target.name }}</span>
          <span>{{ testResult.target.url }}</span>
        </div>
        <div class="p2-stage-grid">
          <div v-for="s in (testResult.result?.stages || [])" :key="s.stage" class="p2-stage">
            <span>{{ STAGE_TEXT[s.stage] || s.stage }}</span>
            <em :class="`is-${s.status}`">{{ s.executed ? (s.ok ? '成功' : s.status) : '未执行' }}</em>
            <b>{{ s.latency_ms == null ? '—' : s.latency_ms + 'ms' }}</b>
          </div>
        </div>
        <p class="np-muted">P80 {{ testResult.result?.p80_ms == null ? '—' : testResult.result.p80_ms + 'ms' }}
          · 状态 {{ classOf(testResult.result?.status).text }} · {{ reasonText(testResult.result?.reason) }}</p>
      </template>
    </el-dialog>

    <el-dialog v-model="settingsDialog" title="基础检测设置" width="500px">
      <el-form label-width="130px">
        <el-form-item label="正常检测周期">
          <el-input-number v-model="settingsForm.interval_minutes" :min="5" :max="1440" />
          <span class="np-muted p2-unit">分钟</span>
        </el-form-item>
        <el-form-item label="全局检测并发">
          <el-input-number v-model="settingsForm.concurrency" :min="1" :max="50" />
          <span class="np-muted p2-unit">节点</span>
        </el-form-item>
      </el-form>
      <p class="np-muted">异常后约 15 秒、30 秒快速复查；观察/不健康节点每 5 分钟复查。快速复查高于正常周期，同一节点始终只占一个队列位置。</p>
      <template #footer>
        <el-button @click="settingsDialog = false">取消</el-button>
        <el-button type="primary" @click="saveSettings">保存</el-button>
      </template>
    </el-dialog>

    <!-- node detail -->
    <el-drawer v-model="drawer" size="min(760px, 96vw)" title="基础健康详情">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="p2-detail-head">
            <div>
              <h3>{{ detail.node.current_name }}</h3>
              <p class="np-muted">{{ detail.node.server }}:{{ detail.node.port }} · {{ detail.node.source_name }}</p>
            </div>
            <el-button size="small" type="primary" @click="checkNode(detail.node)">立即检测</el-button>
          </div>
          <div class="p2-detail-tags">
            <el-tag :type="stateOf(detail.state.state).type">{{ stateOf(detail.state.state).text }}</el-tag>
            <el-tag :type="baseOf(detail.node.base_health).type" effect="plain">基础 {{ baseOf(detail.node.base_health).text }}</el-tag>
            <el-tag :type="layerOf(detail.node.management_layer).type" effect="plain">{{ layerOf(detail.node.management_layer).text }}</el-tag>
            <el-tag type="info" effect="plain">数据 {{ detail.node.data_status }}</el-tag>
            <el-tag type="info" effect="plain">竞争 {{ detail.node.competition_state }}</el-tag>
          </div>
          <dl class="p2-meta">
            <div><dt>最近检测</dt><dd>{{ shortTs(detail.state.last_run_at) }}</dd></div>
            <div><dt>下次检测</dt><dd>{{ shortTs(detail.state.next_due_at) }}</dd></div>
            <div><dt>连续有效失败</dt><dd>{{ detail.state.consecutive_failures || 0 }} 轮</dd></div>
            <div><dt>连续成功</dt><dd>{{ detail.state.consecutive_successes || 0 }} 轮</dd></div>
            <div><dt>观察池原因</dt><dd>{{ reasonText(detail.observation_reason) }}</dd></div>
            <div><dt>最近判定原因</dt><dd>{{ reasonText(detail.state.last_reason) }}</dd></div>
          </dl>

          <div v-if="detail.latest" class="p2-latest-head">
            <div>
              <h4>本轮结果 · {{ TRIGGER_TEXT[detail.latest.trigger] || detail.latest.trigger }}</h4>
              <p class="np-muted">{{ formatTs(detail.latest.started_at) }} · 耗时 {{ detail.latest.duration_ms || 0 }}ms
                · {{ detail.latest.success_count }} 成功 / {{ detail.latest.failure_count }} 失败 /
                {{ detail.latest.unknown_count }} 未知</p>
            </div>
            <span :class="`p2-class is-${detail.latest.status}`">{{ classOf(detail.latest.status).text }}</span>
          </div>

          <div v-if="!activeTargets.length" class="np-muted p2-empty">尚无检测记录。点击「立即检测」后将生成逐目标分阶段结果。</div>
          <div v-for="t in activeTargets" :key="t.id" class="p2-target-result">
            <div class="p2-target-result-head">
              <div>
                <b>{{ t.target_name }}</b>
                <div class="np-muted p2-node-sub">{{ t.target_url }}</div>
              </div>
              <div class="p2-target-result-right">
                <span :class="`p2-class is-${t.status}`">{{ classOf(t.status).text }}</span>
                <strong v-if="t.p80_ms != null">{{ t.p80_ms }} ms</strong>
              </div>
            </div>
            <p v-if="t.reason" class="p2-reason">{{ reasonText(t.reason) }}</p>

            <div class="p2-stage-grid">
              <div v-for="name in ['dns','tcp','tls','https']" :key="name" class="p2-stage">
                <span>{{ STAGE_TEXT[name] }}</span>
                <em :class="`is-${stageOf(t, name)?.status || 'none'}`">
                  {{ stageOf(t, name)?.executed ? (stageOf(t, name)?.ok ? '成功' : (stageOf(t, name)?.status || '失败')) : '未执行' }}
                </em>
                <b>{{ stageOf(t, name)?.latency_ms == null ? '—' : stageOf(t, name).latency_ms + 'ms' }}</b>
                <small v-if="stageOf(t, name)?.slow">偏慢</small>
              </div>
            </div>

            <div class="p2-subblock">
              <div class="p2-subhead">
                <span>DNS 解析</span>
                <b>{{ stageOf(t, 'dns')?.latency_ms == null ? '—' : stageOf(t, 'dns').latency_ms + ' ms' }}</b>
              </div>
              <p class="np-muted p2-stage-detail">
                地址：{{ stageOf(t, 'dns')?.detail?.address || '—' }}
                · 正常 ≤200ms，异常参考 >1000ms
              </p>
            </div>

            <div class="p2-subblock">
              <div class="p2-subhead">
                <span>TCP 五次原始探测</span>
                <b>P80 {{ t.p80_ms == null ? '—' : t.p80_ms + ' ms' }}</b>
              </div>
              <div class="p2-sample-row">
                <span v-for="s in t.tcp_samples" :key="s.sample_index" class="p2-sample"
                      :class="{ bad: !s.ok }">
                  #{{ s.sample_index + 1 }} {{ s.ok ? s.latency_ms + 'ms' : '失败' }}
                </span>
              </div>
              <p class="np-muted p2-stage-detail">
                最近秩法：5 个样本升序排列，取第 4 个值；不使用中位数、均值、最低或最高值。
                P80 等级 {{ t.p80_grade?.label || '—' }}。
              </p>
            </div>

            <div class="p2-subblock p2-two-col">
              <div>
                <div class="p2-subhead"><span>TLS 握手</span>
                  <b>{{ t.tls_ms == null ? '—' : t.tls_ms + 'ms' }}</b></div>
                <p class="np-muted p2-stage-detail">
                  证书：{{ stageOf(t, 'tls')?.detail?.certificate || '—' }}
                  <span v-if="stageOf(t, 'tls')?.detail?.not_after"> · 到期 {{ stageOf(t, 'tls').detail.not_after }}</span>
                </p>
              </div>
              <div>
                <div class="p2-subhead"><span>HTTPS 请求</span>
                  <b>{{ t.https_status || '—' }} · {{ t.https_ms == null ? '—' : t.https_ms + 'ms' }}</b></div>
                <p class="np-muted p2-stage-detail">正常响应 ≤3000ms；失败时后续阶段标记未执行。</p>
              </div>
            </div>
          </div>

          <div v-if="detail.recent_checks?.length" class="p2-subblock">
            <div class="p2-subhead"><span>异常复查与恢复记录</span><b>最近 {{ detail.recent_checks.length }} 轮</b></div>
            <div v-for="c in detail.recent_checks" :key="c.id" class="p2-history">
              <span>{{ shortTs(c.started_at) }}</span>
              <span>{{ TRIGGER_TEXT[c.trigger] || c.trigger }}</span>
              <em :class="`is-${c.status}`">{{ classOf(c.status).text }}</em>
              <span>{{ c.p80_ms == null ? '—' : c.p80_ms + 'ms' }} · {{ reasonText(c.reason) }}</span>
            </div>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.p2-page { max-width: 1500px; }
.p2-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 14px; }
.p2-head .np-page-desc { margin: 5px 0 0; max-width: 860px; }
.p2-head-actions { display: flex; gap: 7px; flex-wrap: wrap; justify-content: flex-end; flex: 0 0 auto; }
.p2-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
.p2-stat { background: var(--np-panel-solid); border: 1px solid rgba(31,61,51,.09); border-radius: 14px; padding: 14px 16px 12px; border-top: 3px solid #9bb0a6; }
.p2-stat.is-healthy { border-top-color: #3d9a72; }
.p2-stat.is-watch { border-top-color: #d6a53c; }
.p2-stat.is-bad { border-top-color: #c05252; }
.p2-stat.is-unknown { border-top-color: #8b9bb0; }
.p2-stat span { display: block; font-size: 12px; color: #74857c; }
.p2-stat b { display: block; font-size: 30px; color: #1c332b; line-height: 1.2; margin: 4px 0 2px; font-variant-numeric: tabular-nums; }
.p2-stat small { display: block; font-size: 11px; color: #8a998f; min-height: 28px; line-height: 1.35; }
.p2-target-card { padding: 16px 18px; margin-bottom: 16px; }
.p2-section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.p2-section-head h3 { margin: 0; font-size: 15px; color: #203730; }
.p2-section-head p { margin: 3px 0 0; font-size: 12px; }
.p2-inline-actions { display: flex; gap: 6px; flex: 0 0 auto; }
.p2-target-list { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.p2-target { display: flex; justify-content: space-between; gap: 10px; padding: 10px 12px; border: 1px solid rgba(31,61,51,.08); border-radius: 10px; background: #fbfdfc; }
.p2-target.off { opacity: .62; background: #f7f7f6; }
.p2-target-main { min-width: 0; }
.p2-target-title { display: flex; align-items: center; gap: 7px; }
.p2-target-url { display: block; font-size: 11px; color: #5f746a; overflow-wrap: anywhere; margin-top: 3px; }
.p2-target-meta { font-size: 11px; color: #87958d; margin-top: 5px; }
.p2-target-meta em { font-style: normal; font-weight: 600; }
.p2-target-actions { display: flex; align-items: center; gap: 2px; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; }
.p2-err { color: #a84646; }
.p2-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.p2-count { margin-left: auto; font-size: 12px; }
.p2-table-card { padding: 4px 8px 6px; overflow-x: auto; }
.p2-table-card :deep(.el-table) { min-width: 1100px; }
.p2-node-name { font-weight: 600; color: #213b32; }
.p2-node-sub { font-size: 11px; margin-top: 1px; overflow-wrap: anywhere; }
.p2-class { font-size: 11px; font-weight: 700; letter-spacing: .02em; }
.p2-class.is-success { color: #2f7d5a; }
.p2-class.is-failure { color: #a84646; }
.p2-class.is-unknown { color: #7c8792; }
.p2-class.is-none { color: #99a49d; }
.p2-p80 small { font-size: 9px; margin-left: 2px; font-weight: 400; color: #87958d; }
.p2-counters em { font-style: normal; font-weight: 600; }
.p2-pager { display: flex; justify-content: flex-end; padding: 8px 4px 2px; }
.p2-mobile-list { display: none; }
.p2-mcard { padding: 13px 14px; margin-bottom: 10px; }
.p2-mcard-head { display: flex; justify-content: space-between; gap: 10px; }
.p2-mchips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }
.p2-chip { font-size: 11px; line-height: 20px; padding: 0 7px; border-radius: 6px; background: #f2f5f3; color: #5b6f64; }
.p2-mcard-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; }
.p2-dialog-note { font-size: 12px; line-height: 1.6; margin: 4px 0 0; }
.p2-unit { margin-left: 8px; }
.p2-test-meta { display: flex; justify-content: space-between; gap: 10px; font-size: 12px; margin: 10px 0; color: #60746a; overflow-wrap: anywhere; }
.p2-detail-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.p2-detail-head h3 { margin: 0; font-size: 17px; color: #203730; }
.p2-detail-head p { margin: 3px 0 0; font-size: 12px; }
.p2-detail-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
.p2-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 9px 16px; margin: 0 0 15px; }
.p2-meta dt { font-size: 11px; color: #87958d; margin-bottom: 2px; }
.p2-meta dd { margin: 0; font-size: 12px; color: #29433a; overflow-wrap: anywhere; }
.p2-latest-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; padding-top: 12px; border-top: 1px solid rgba(31,61,51,.08); }
.p2-latest-head h4 { margin: 0; font-size: 13px; color: #29433a; }
.p2-latest-head p { margin: 3px 0 0; font-size: 11px; }
.p2-empty { padding: 20px 0; }
.p2-target-result { border: 1px solid rgba(31,61,51,.09); border-radius: 12px; padding: 12px 13px; margin-top: 12px; background: #fcfdfd; }
.p2-target-result-head { display: flex; justify-content: space-between; gap: 10px; }
.p2-target-result-head b { color: #203730; font-size: 13px; }
.p2-target-result-right { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
.p2-target-result-right strong { font-size: 13px; color: #29433a; }
.p2-reason { font-size: 11px; color: #9a6d17; margin: 4px 0 8px; }
.p2-stage-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; margin: 9px 0; }
.p2-stage { background: #f5f8f6; border-radius: 8px; padding: 7px 8px; min-width: 0; }
.p2-stage span { display: block; font-size: 10px; color: #7a8c82; }
.p2-stage em { display: block; font-style: normal; font-size: 11px; font-weight: 700; margin: 2px 0; color: #596d63; }
.p2-stage em.is-success { color: #2f7d5a; }
.p2-stage em.is-failure { color: #a84646; }
.p2-stage em.is-unknown { color: #7c8792; }
.p2-stage em.is-unexecuted { color: #9ca7a1; font-weight: 400; }
.p2-stage b { font-size: 11px; color: #29433a; font-variant-numeric: tabular-nums; }
.p2-stage small { font-size: 9px; color: #b07a19; margin-left: 2px; }
.p2-subblock { border-top: 1px dashed rgba(31,61,51,.09); padding-top: 8px; margin-top: 8px; }
.p2-subhead { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: #29433a; }
.p2-subhead b { font-variant-numeric: tabular-nums; }
.p2-stage-detail { font-size: 11px; line-height: 1.55; margin: 4px 0 0; }
.p2-sample-row { display: flex; flex-wrap: wrap; gap: 5px; margin: 6px 0 2px; }
.p2-sample { font-size: 11px; padding: 2px 6px; border-radius: 5px; background: #eef5f1; color: #356f55; font-variant-numeric: tabular-nums; }
.p2-sample.bad { background: #fbeaea; color: #a84646; }
.p2-two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.p2-history { display: grid; grid-template-columns: 92px 90px 80px 1fr; gap: 8px; padding: 5px 0; border-bottom: 1px dashed rgba(31,61,51,.07); font-size: 11px; color: #5c6f65; }
.p2-history em { font-style: normal; font-weight: 700; }
.p2-history em.is-success { color: #2f7d5a; }
.p2-history em.is-failure { color: #a84646; }
.p2-history em.is-unknown { color: #7c8792; }
@media (max-width: 860px) {
  .p2-head { flex-direction: column; }
  .p2-head-actions { justify-content: flex-start; width: 100%; }
  .p2-stats { grid-template-columns: 1fr 1fr; }
  .p2-target-list { grid-template-columns: 1fr; }
  .p2-section-head { flex-direction: column; }
  .p2-table-card { display: none; }
  .p2-mobile-list { display: block; }
  .p2-count { width: 100%; margin-left: 0; }
  .p2-meta, .p2-two-col { grid-template-columns: 1fr; }
  .p2-stage-grid { grid-template-columns: 1fr 1fr; }
  .p2-history { grid-template-columns: 1fr 1fr; }
}
</style>
