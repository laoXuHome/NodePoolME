<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const loading = ref(false)
const busy = ref(false)
const summary = ref(null)
const history = ref([])
const format = ref('clash')
const content = ref('')
const contentLoading = ref(false)
const cache = reactive({})
const showFiltered = ref(true)

const CATS = [
  { code: 'standard', label: '标准综合' },
  { code: 'gpt', label: 'GPT' },
  { code: 'youtube', label: 'YouTube' },
  { code: 'telegram', label: 'Telegram' },
  { code: 'github', label: 'GitHub' },
  { code: 'global', label: '国外综合' }
]
const FORMATS = [
  { key: 'clash', label: 'Clash / Mihomo', extension: 'YAML' },
  { key: 'singbox', label: 'sing-box', extension: 'JSON' },
  { key: 'base64', label: '通用 Base64', extension: 'TXT' },
  { key: 'loon', label: 'Loon', extension: 'TXT' }
]
const STATUS_TEXT = {
  not_generated: '尚未生成',
  success: '生成成功',
  partial: '部分格式失败',
  failed: '生成失败',
  empty: '空订阅',
  stale: '需重新生成'
}
const NODE_STATUS = { allowed: '已下发', filtered: '关键词过滤', unsupported: '格式不兼容' }

const s = computed(() => summary.value || {})
const batch = computed(() => s.value.batch || null)
const current = computed(() => s.value.current || null)
const stats = computed(() => batch.value || current.value || {
  category_counts: {}, merged_count: 0, unique_count: 0, duplicate_count: 0,
  filtered_count: 0, candidate_count: 0, compatibility_excluded_count: 0,
  output_count: 0, labels: { gpt: 0, yt: 0, tg: 0, gh: 0, multi: 0 }, p6: {}, p621: {}
})
const isStale = computed(() => s.value.status === 'stale')
const statusKey = computed(() => s.value.status || 'not_generated')
const statusText = computed(() => STATUS_TEXT[statusKey.value] || statusKey.value)
const outputs = computed(() => s.value.formats || [])
const outputMap = computed(() => Object.fromEntries(outputs.value.map((o) => [o.format, o])))
const activeOutput = computed(() => outputMap.value[format.value] || null)
const canDownload = computed(() => Boolean(
  activeOutput.value && activeOutput.value.status === 'success' && !isStale.value
))
const nodes = computed(() => s.value.nodes || [])
const visibleNodes = computed(() => showFiltered.value
  ? nodes.value
  : nodes.value.filter((n) => n.status === 'allowed'))
const filteredCount = computed(() => nodes.value.filter((n) => n.status === 'filtered').length)
const categoryCounts = computed(() => stats.value.category_counts || {})
const labels = computed(() => stats.value.labels || { gpt: 0, yt: 0, tg: 0, gh: 0, multi: 0 })
const labelsTotal = computed(() => ['gpt', 'yt', 'tg', 'gh']
  .reduce((sum, key) => sum + Number(labels.value[key] || 0), 0))

const timeText = (v) => (v ? formatTs(v) : '—')
const protocolText = (v) => (v || '未知').toUpperCase()
const speedText = (v) => ({ excellent: '优', good: '良', poor: '差', danger: '劣' }[v] || v || '—')
const shortHash = (v) => (v ? String(v).slice(0, 12) : '—')
const nodeLabel = (n) => (n.labels || []).join(' · ') || '—'
const outputStatus = (key) => outputMap.value[key]?.status || 'not_generated'
const outputStatusText = (key) => STATUS_TEXT[outputStatus(key)] || outputStatus(key)

function formatLabel(key) {
  return FORMATS.find((item) => item.key === key)?.label || key
}

async function loadAll() {
  loading.value = true
  try {
    summary.value = await api.get('/api/unified-subscription/summary')
    await loadHistory()
    if (!outputs.value.some((o) => o.format === format.value)) {
      format.value = outputs.value[0]?.format || 'clash'
    }
    await loadContent()
  } catch (e) {
    ElMessage.error(e.message || '统一订阅数据加载失败')
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  try {
    const data = await api.get('/api/unified-subscription/history?limit=12')
    history.value = data.items || []
  } catch {
    history.value = []
  }
}

async function loadContent(force = false) {
  if (!activeOutput.value) {
    content.value = ''
    return
  }
  if (!force && cache[format.value] !== undefined) {
    content.value = cache[format.value]
    return
  }
  contentLoading.value = true
  try {
    const data = await api.get(`/api/unified-subscription/formats/${format.value}`)
    content.value = data.content || ''
    cache[format.value] = content.value
  } catch (e) {
    content.value = ''
    ElMessage.error(e.message || '订阅内容读取失败')
  } finally {
    contentLoading.value = false
  }
}

async function selectFormat(key) {
  format.value = key
  await loadContent()
}

async function generate() {
  try {
    await ElMessageBox.confirm(
      '将按当前 P6.1 版本和 P6.2.1 规则重新合并、过滤并生成四种格式，创建一条新的追加记录；不会触发检测、测速或竞争。确定继续吗？',
      '重新生成统一订阅',
      { type: 'warning', confirmButtonText: '生成' }
    )
  } catch { return }
  busy.value = true
  try {
    Object.keys(cache).forEach((key) => delete cache[key])
    const result = await api.post('/api/unified-subscription/generate')
    ElMessage.success(`批次 ${result.batch.batch_no} 已生成`)
    await loadAll()
  } catch (e) {
    ElMessage.error(e.message || '统一订阅生成失败')
  } finally {
    busy.value = false
  }
}

async function download() {
  if (!canDownload.value) return
  try {
    const res = await fetch(`/api/unified-subscription/download/${format.value}`, { credentials: 'same-origin' })
    if (!res.ok) {
      let message = '下载失败'
      try {
        const data = await res.json()
        message = data.detail || message
      } catch { /* keep fallback */ }
      throw new Error(message)
    }
    const blob = await res.blob()
    const name = activeOutput.value?.format === 'clash' ? 'nodepool-unified.yaml'
      : activeOutput.value?.format === 'singbox' ? 'nodepool-unified.json'
        : activeOutput.value?.format === 'loon' ? 'nodepool-unified.loon.txt'
          : 'nodepool-unified.base64.txt'
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = name
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(link.href)
  } catch (e) {
    ElMessage.error(e.message || '下载失败')
  }
}

onMounted(loadAll)
</script>

<template>
  <div v-loading="loading || busy" class="np-page p622-page">
    <header class="p622-head">
      <div class="p622-head-note">
        <b>一份统一订阅，四种客户端表达</b>
        <span>六类 P6.1 优选合并去重 → P6.2.1 关键词过滤 → Clash / sing-box / Base64 / Loon</span>
      </div>
      <div class="p622-head-actions">
        <el-tag :type="isStale ? 'warning' : statusKey === 'success' ? 'success' : 'info'"
                effect="plain" size="small">{{ statusText }}</el-tag>
        <el-tag v-if="stats.p6?.version_no" type="info" effect="plain" size="small">{{ stats.p6.version_no }}</el-tag>
        <el-tag v-if="stats.p621?.revision !== undefined" type="info" effect="plain" size="small">
          规则 R{{ stats.p621.revision }}
        </el-tag>
        <el-button size="small" @click="loadAll">刷新读取</el-button>
        <el-button type="primary" size="small" :disabled="busy" @click="generate">重新生成</el-button>
      </div>
    </header>

    <section class="p622-boundary" :class="{ 'is-stale': isStale }">
      <div class="p622-boundary-mark">ONE<br>SUB</div>
      <div>
        <b>{{ isStale ? '版本或规则已变化，当前批次已过期。' : '只做后台预览与管理员下载，不开放客户端订阅入口。' }}</b>
        <p v-if="isStale">
          当前 P6.1 为 {{ current?.p6?.version_no || '—' }}、规则 R{{ current?.p621?.revision ?? '—' }}；
          本批次仍绑定旧版本。旧过滤结果不得用于新订阅，请重新生成。
        </p>
        <p v-else>
          节点选择只来自 P6.1，关键词只来自 P6.2.1；重新计算不会触发 P2/P3/P4/P5，不创建 P6.1 新版本，不写回过滤结果。
        </p>
      </div>
      <span class="p622-boundary-chip">PREVIEW · ADMIN ONLY</span>
    </section>

    <section class="p622-kpis">
      <article>
        <span>六类合并记录</span>
        <b>{{ stats.merged_count }}</b>
        <small>各业务类别可重复计入</small>
      </article>
      <article>
        <span>稳定 ID 去重</span>
        <b>{{ stats.unique_count }}</b>
        <small>重复 {{ stats.duplicate_count }} 条记录</small>
      </article>
      <article class="is-filtered">
        <span>关键词过滤</span>
        <b>{{ stats.filtered_count }}</b>
        <small>命中节点不进入客户端内容</small>
      </article>
      <article class="is-allowed">
        <span>最终输出节点</span>
        <b>{{ stats.output_count }}</b>
        <small>
          候选 {{ stats.candidate_count }}
          <template v-if="stats.compatibility_excluded_count">
            · 兼容排除 {{ stats.compatibility_excluded_count }}
          </template>
        </small>
      </article>
    </section>

    <section class="p622-strip">
      <div><span>当前 P6.1</span><b>{{ current?.p6?.version_no || '—' }}</b></div>
      <div><span>当前规则版本</span><b>R{{ current?.p621?.revision ?? '—' }}</b></div>
      <div><span>最近生成</span><b>{{ timeText(batch?.created_at) }}</b></div>
      <div><span>批次</span><b>{{ batch?.batch_no || '未生成' }}</b></div>
      <div><span>节点集合 SHA</span><b class="mono">{{ shortHash(stats.node_set_sha256) }}</b></div>
    </section>

    <section class="p622-card">
      <div class="p622-card-head">
        <div>
          <h3>六类最终优选合并</h3>
          <p>合并的是 P6.1 六类当前版本；同一稳定节点 ID 跨类出现时只输出一次，并合并业务资格。</p>
        </div>
        <span class="p622-hint">不固定数量 · 不补位 · 不改变 P5/P6.1</span>
      </div>
      <div class="p622-cats">
        <article v-for="cat in CATS" :key="cat.code">
          <span>{{ cat.label }}</span>
          <b>{{ categoryCounts[cat.code] || 0 }}</b>
          <small>{{ cat.code }}</small>
        </article>
      </div>
      <div class="p622-math">
        <span>{{ stats.merged_count }} 条合并记录</span>
        <i>−</i>
        <span>{{ stats.duplicate_count }} 条跨类重复</span>
        <i>=</i>
        <b>{{ stats.unique_count }} 个稳定节点</b>
        <i>→</i>
        <span>{{ stats.filtered_count }} 个过滤</span>
        <i>→</i>
        <b>{{ stats.output_count }} 个输出</b>
      </div>
    </section>

    <section class="p622-labels">
      <article class="is-gpt">
        <span>GPT 标签</span><b>{{ labels.gpt }}</b><small>ChatGPT 业务优选</small>
      </article>
      <article class="is-yt">
        <span>YT 标签</span><b>{{ labels.yt }}</b><small>YouTube 业务优选</small>
      </article>
      <article class="is-tg">
        <span>TG 标签</span><b>{{ labels.tg }}</b><small>Telegram 业务优选</small>
      </article>
      <article class="is-gh">
        <span>GH 标签</span><b>{{ labels.gh }}</b><small>GitHub 业务优选</small>
      </article>
      <article class="is-multi">
        <span>多业务标签</span><b>{{ labels.multi }}</b><small>标签合计 {{ labelsTotal }} · 可重复计数</small>
      </article>
    </section>

    <section class="p622-card">
      <div class="p622-card-head">
        <div>
          <h3>客户端格式</h3>
          <p>四种格式共用同一最终节点集合、数量与展示名称；切换只改变表达方式，不重新选择节点。</p>
        </div>
        <div class="p622-format-tabs">
          <button v-for="item in FORMATS" :key="item.key"
                  :class="{ active: format === item.key }"
                  @click="selectFormat(item.key)">
            <i :class="`is-${outputStatus(item.key)}`"></i>
            {{ item.label }}
          </button>
        </div>
      </div>

      <div v-if="!outputs.length" class="p622-empty">
        当前还没有生成批次。点击「重新生成」后，四种格式会关联同一批次并分别记录状态。
      </div>

      <div v-else class="p622-format-body">
        <div class="p622-format-meta">
          <div>
            <span>格式</span><b>{{ formatLabel(format) }} · {{ FORMATS.find(x => x.key === format)?.extension }}</b>
          </div>
          <div>
            <span>状态</span><b :class="`st-${outputStatus(format)}`">{{ outputStatusText(format) }}</b>
          </div>
          <div><span>节点数量</span><b>{{ activeOutput?.node_count ?? 0 }}</b></div>
          <div><span>生成时间</span><b>{{ timeText(activeOutput?.generated_at) }}</b></div>
          <div><span>P6.1 / 规则</span><b>{{ activeOutput?.p6?.version_no || '—' }} / R{{ activeOutput?.p621?.revision ?? '—' }}</b></div>
          <div><span>内容 SHA256</span><b class="mono">{{ shortHash(activeOutput?.content_sha256) }}</b></div>
          <div class="p622-format-actions">
            <el-button size="small" :disabled="!canDownload" @click="download">下载</el-button>
            <el-button size="small" :disabled="busy" @click="generate">重新生成四种格式</el-button>
          </div>
        </div>

        <div v-if="activeOutput?.status === 'failed'" class="p622-fail">
          <b>该格式生成失败，未伪造内容。</b>
          <span>{{ activeOutput.failure_reason || '未知失败原因' }}</span>
        </div>
        <div v-else class="p622-content" v-loading="contentLoading">
          <div class="p622-content-head">
            <span>后台预览</span>
            <small>{{ activeOutput?.content_bytes || 0 }} bytes · {{ activeOutput?.node_count || 0 }} 个节点</small>
          </div>
          <pre>{{ content || '（空订阅：当前没有允许下发节点）' }}</pre>
        </div>
      </div>
    </section>

    <section class="p622-card">
      <div class="p622-card-head">
        <div>
          <h3>节点预览</h3>
          <p>展示最终名称、机场、服务器、协议、速度、业务标签与过滤状态；被过滤节点仅在此管理预览出现。</p>
        </div>
        <el-checkbox v-model="showFiltered" size="small">显示被过滤节点</el-checkbox>
      </div>
      <div v-if="stats.unsupported?.count" class="p622-warn">
        有 {{ stats.unsupported.count }} 个候选节点无法被四种格式同时正确表达，已从统一输出集合排除并在下方标记，不伪造配置。
      </div>
      <div class="p622-node-head">
        <span>最终展示名称</span><span>机场 / 服务器</span><span>协议</span><span>速度</span><span>业务标签</span><span>状态</span>
      </div>
      <div v-for="node in visibleNodes" :key="node.node_id" class="p622-node" :class="`is-${node.status}`">
        <div class="p622-node-name">
          <b>{{ node.display_name }}</b>
          <small>#{{ node.node_id }} · 原始 {{ node.node_name }}</small>
        </div>
        <div class="p622-node-origin">
          <b>{{ node.airport_name || '—' }}</b>
          <small>{{ node.server }}:{{ node.port }}</small>
        </div>
        <div class="p622-node-proto"><b>{{ protocolText(node.protocol) }}</b><small>{{ node.categories?.join(' / ') || '—' }}</small></div>
        <div class="p622-node-speed"><b>{{ speedText(node.speed_grade) }}</b><small>{{ node.p80_ms ? `${Math.round(node.p80_ms)} ms` : '—' }}</small></div>
        <div class="p622-node-tags">
          <span v-for="label in node.labels" :key="label" :class="`tag-${label.toLowerCase()}`">{{ label }}</span>
          <small v-if="!node.labels?.length">无业务标签</small>
        </div>
        <div class="p622-node-status">
          <span :class="`status-${node.status}`">{{ NODE_STATUS[node.status] || node.status }}</span>
          <small v-if="node.status === 'filtered'">
            {{ (node.hits || []).map(h => h.keyword).filter((v, i, a) => a.indexOf(v) === i).join('、') }}
          </small>
          <small v-else-if="node.status === 'unsupported'">
            {{ Object.values(node.unsupported || {}).map(x => x.label).join('；') }}
          </small>
        </div>
      </div>
      <div v-if="!visibleNodes.length" class="p622-empty">当前没有可展示的节点。</div>
    </section>

    <section class="p622-card">
      <div class="p622-card-head">
        <div>
          <h3>生成记录</h3>
          <p>每批关联四种格式，状态独立记录；历史只追加，不覆盖。记录中不保存机场原始订阅地址或上游凭据。</p>
        </div>
      </div>
      <div v-for="item in history" :key="item.id" class="p622-history" :class="{ 'is-stale': item.stale }">
        <div class="p622-history-main">
          <b>{{ item.batch_no }}</b>
          <small>{{ timeText(item.created_at) }} · {{ item.p6?.version_no }} / R{{ item.p621?.revision }}</small>
        </div>
        <div class="p622-history-num">
          <span>输出 {{ item.output_count }}</span>
          <span>过滤 {{ item.filtered_count }}</span>
          <span>重复 {{ item.duplicate_count }}</span>
        </div>
        <div class="p622-history-formats">
          <span v-for="out in item.outputs" :key="out.format" :class="`st-${out.status}`">
            {{ formatLabel(out.format) }} · {{ out.status === 'success' ? out.node_count : out.status }}
          </span>
        </div>
        <el-tag v-if="item.stale" size="small" type="warning" effect="plain">版本已过期</el-tag>
        <el-tag v-else size="small" :type="item.status === 'success' ? 'success' : 'info'" effect="plain">
          {{ STATUS_TEXT[item.status] || item.status }}
        </el-tag>
      </div>
      <div v-if="!history.length" class="p622-empty">还没有生成历史。</div>
    </section>

    <footer class="p622-foot">
      来源 {{ stats.p6?.version_no || '—' }} · 规则 R{{ stats.p621?.revision ?? '—' }} ·
      规则预览 {{ timeText(stats.p621?.computed_at) }} · 仅后台预览与管理员下载
    </footer>
  </div>
</template>

<style scoped>
.p622-page { max-width: 1500px; }
.p622-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 2px 2px 0; }
.p622-head-note { display: flex; align-items: baseline; gap: 8px; min-width: 0; flex-wrap: wrap; }
.p622-head-note b { font-size: 11.5px; color: #2f6f5b; white-space: nowrap; }
.p622-head-note span { font-size: 10px; color: #8a998f; }
.p622-head-actions { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.p622-boundary { margin: 14px 0 12px; display: flex; align-items: center; gap: 12px; padding: 12px 14px; border: 1px solid rgba(47,111,91,.16); border-radius: 12px; background: linear-gradient(100deg, rgba(240,247,243,.94), rgba(250,247,238,.86)); }
.p622-boundary.is-stale { border-color: rgba(181,121,31,.28); background: linear-gradient(100deg, #fcf7ed, #fdf2e5); }
.p622-boundary-mark { flex: 0 0 46px; height: 46px; border-radius: 10px; background: #29453c; color: #f1dfae; font-size: 9px; line-height: 1.2; display: flex; align-items: center; justify-content: center; text-align: center; letter-spacing: .1em; font-weight: 700; }
.p622-boundary b { font-size: 12.5px; color: #29433a; }
.p622-boundary p { margin: 3px 0 0; font-size: 11.5px; line-height: 1.55; color: #71827a; }
.p622-boundary-chip { margin-left: auto; white-space: nowrap; font-size: 9.5px; color: #8a651c; background: #faf1dc; border-radius: 999px; padding: 5px 9px; letter-spacing: .06em; }
.p622-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; margin-bottom: 10px; }
.p622-kpis article { padding: 12px 14px; border-radius: 13px; background: #fff; border: 1px solid rgba(31,55,47,.1); box-shadow: 0 5px 15px rgba(31,55,47,.04); }
.p622-kpis span { display: block; font-size: 10.5px; color: #7b8d83; }
.p622-kpis b { display: block; margin: 3px 0 1px; font-size: 26px; line-height: 1.1; color: #203a31; font-variant-numeric: tabular-nums; }
.p622-kpis small { font-size: 9.5px; color: #91a097; }
.p622-kpis .is-filtered b { color: #b7791f; }
.p622-kpis .is-allowed b { color: #2f8f62; }
.p622-strip { display: flex; flex-wrap: wrap; gap: 7px 22px; margin-bottom: 12px; padding: 8px 13px; border-radius: 10px; background: #f8faf8; border: 1px solid rgba(31,55,47,.07); }
.p622-strip div { display: flex; align-items: baseline; gap: 6px; }
.p622-strip span { font-size: 9.5px; color: #91a097; }
.p622-strip b { font-size: 10.5px; color: #40584e; font-variant-numeric: tabular-nums; }
.p622-strip .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 9px; }
.p622-card { margin-bottom: 12px; padding: 14px 16px; border-radius: 15px; background: #fff; border: 1px solid rgba(31,55,47,.1); box-shadow: 0 7px 20px rgba(31,55,47,.045); }
.p622-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
.p622-card-head h3 { margin: 0; font-size: 15px; color: #203a31; letter-spacing: -.01em; }
.p622-card-head p { margin: 3px 0 0; font-size: 10.5px; line-height: 1.55; color: #7c8d84; max-width: 860px; }
.p622-hint { flex: none; font-size: 9px; color: #8b998f; border: 1px solid rgba(31,55,47,.08); border-radius: 999px; padding: 3px 7px; }
.p622-cats { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 7px; }
.p622-cats article { padding: 8px 10px; border: 1px solid rgba(31,55,47,.09); border-radius: 9px; background: #fbfdfc; }
.p622-cats span { display: block; font-size: 9.5px; color: #7b8d83; }
.p622-cats b { display: block; font-size: 20px; line-height: 1.15; color: #29433a; font-variant-numeric: tabular-nums; }
.p622-cats small { font-size: 8px; color: #a3aea7; letter-spacing: .05em; }
.p622-math { display: flex; flex-wrap: wrap; align-items: center; gap: 5px 7px; margin-top: 9px; font-size: 10.5px; color: #73867c; }
.p622-math b { color: #2f6f5b; }
.p622-math i { font-style: normal; color: #b4bdb7; }
.p622-labels { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 9px; margin-bottom: 12px; }
.p622-labels article { padding: 11px 14px; border-radius: 13px; background: #fff; border: 1px solid rgba(31,55,47,.1); }
.p622-labels span { display: block; font-size: 10px; color: #7b8d83; }
.p622-labels b { display: block; margin: 2px 0; font-size: 24px; line-height: 1.1; color: #203a31; font-variant-numeric: tabular-nums; }
.p622-labels small { font-size: 9px; color: #91a097; }
.p622-labels .is-gpt b { color: #2d7d5e; }
.p622-labels .is-yt b { color: #b24f3f; }
.p622-labels .is-tg b { color: #3c7b9b; }
.p622-labels .is-gh b { color: #6d5b9b; }
.p622-labels .is-multi b { color: #9a6b1d; }
.p622-format-tabs { display: flex; flex-wrap: wrap; gap: 5px; }
.p622-format-tabs button { display: flex; align-items: center; gap: 5px; border: 1px solid rgba(31,55,47,.12); background: #fbfdfc; border-radius: 999px; padding: 5px 10px; font-size: 10px; color: #667a6f; cursor: pointer; }
.p622-format-tabs button.active { border-color: #2f6f5b; background: #f0f7f3; color: #235443; box-shadow: 0 0 0 2px rgba(47,111,91,.06) inset; }
.p622-format-tabs i { width: 6px; height: 6px; border-radius: 50%; background: #b3bdb7; }
.p622-format-tabs i.is-success { background: #2f8f62; }
.p622-format-tabs i.is-failed { background: #c25243; }
.p622-format-tabs i.is-empty { background: #c68a2e; }
.p622-format-body { border-top: 1px solid rgba(31,55,47,.07); padding-top: 10px; }
.p622-format-meta { display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px 20px; margin-bottom: 9px; }
.p622-format-meta div { display: flex; align-items: baseline; gap: 5px; }
.p622-format-meta span { font-size: 9px; color: #91a097; }
.p622-format-meta b { font-size: 10.5px; color: #40584e; font-variant-numeric: tabular-nums; }
.p622-format-meta b.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 9px; }
.p622-format-meta .st-success { color: #2f8f62; }
.p622-format-meta .st-failed { color: #c25243; }
.p622-format-meta .st-empty { color: #a8741e; }
.p622-format-actions { margin-left: auto; display: flex; gap: 4px; }
.p622-content { border: 1px solid rgba(31,55,47,.1); border-radius: 10px; background: #1f2f29; overflow: hidden; }
.p622-content-head { display: flex; justify-content: space-between; align-items: baseline; padding: 7px 11px; border-bottom: 1px solid rgba(255,255,255,.08); color: #e8dfc8; }
.p622-content-head span { font-size: 10px; font-weight: 700; letter-spacing: .05em; }
.p622-content-head small { font-size: 9px; color: #9eb2a7; }
.p622-content pre { margin: 0; padding: 11px 13px; max-height: 330px; overflow: auto; color: #e8efe9; font: 10px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre; }
.p622-fail { padding: 11px 12px; border: 1px solid #f0cdc5; border-radius: 9px; background: #fdf3f0; }
.p622-fail b { display: block; font-size: 11px; color: #a94738; }
.p622-fail span { font-size: 10px; color: #8b6b63; }
.p622-warn { margin: 2px 0 8px; padding: 7px 9px; border-radius: 7px; background: #fcf6e8; border: 1px solid #f0dfba; font-size: 9.5px; color: #93651b; }
.p622-node-head { display: grid; grid-template-columns: minmax(210px, 2.2fr) minmax(120px, 1.4fr) 64px 58px minmax(90px, 1fr) 108px; gap: 8px; padding: 5px 7px; border-bottom: 1px solid rgba(31,55,47,.08); font-size: 8.5px; color: #97a59c; letter-spacing: .04em; }
.p622-node { display: grid; grid-template-columns: minmax(210px, 2.2fr) minmax(120px, 1.4fr) 64px 58px minmax(90px, 1fr) 108px; gap: 8px; align-items: center; padding: 7px; border-bottom: 1px dashed rgba(31,55,47,.06); }
.p622-node:last-child { border-bottom: 0; }
.p622-node.is-filtered { background: #fdfaf4; }
.p622-node.is-unsupported { background: #faf8f8; }
.p622-node > div { min-width: 0; }
.p622-node b { display: block; font-size: 10.5px; color: #29433a; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p622-node small { display: block; font-size: 8.5px; color: #93a198; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p622-node-name b { font-size: 11px; }
.p622-node-tags { display: flex; flex-wrap: wrap; gap: 3px; }
.p622-node-tags span { font-size: 8.5px; padding: 1px 4px; border-radius: 4px; background: #edf5f0; color: #2f6f5b; }
.p622-node-tags span.tag-yt { background: #fdf0ed; color: #b24f3f; }
.p622-node-tags span.tag-tg { background: #edf4f8; color: #3c7b9b; }
.p622-node-tags span.tag-gh { background: #f2eff9; color: #6d5b9b; }
.p622-node-status span { display: inline-block; font-size: 8.5px; padding: 2px 5px; border-radius: 4px; background: #edf5f0; color: #2f7c5b; }
.p622-node-status span.status-filtered { background: #fbf1df; color: #9a6512; }
.p622-node-status span.status-unsupported { background: #f5eeee; color: #9a5a51; }
.p622-history { display: flex; align-items: center; gap: 10px; padding: 8px 7px; border-bottom: 1px dashed rgba(31,55,47,.07); }
.p622-history:last-child { border-bottom: 0; }
.p622-history.is-stale { background: #fdfaf4; }
.p622-history-main { flex: 0 0 170px; }
.p622-history-main b { display: block; font-size: 11px; color: #29433a; }
.p622-history-main small { font-size: 8.5px; color: #93a198; }
.p622-history-num { display: flex; flex-wrap: wrap; gap: 4px 9px; flex: 1 1 auto; font-size: 9px; color: #7b8d83; }
.p622-history-formats { display: flex; flex-wrap: wrap; gap: 4px; }
.p622-history-formats span { font-size: 8.5px; padding: 2px 5px; border-radius: 4px; background: #f2f7f4; color: #2f7c5b; }
.p622-history-formats span.st-failed { background: #fbf0ed; color: #b24f3f; }
.p622-history-formats span.st-empty { background: #fcf6e8; color: #93651b; }
.p622-empty { padding: 18px 6px; font-size: 10.5px; color: #93a198; text-align: center; }
.p622-foot { padding: 2px 4px 12px; font-size: 9.5px; color: #94a29a; }

@media (max-width: 1200px) {
  .p622-cats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .p622-node-head, .p622-node { grid-template-columns: minmax(180px, 2fr) minmax(110px, 1.3fr) 58px 52px minmax(76px, 1fr) 96px; }
}
@media (max-width: 980px) {
  .p622-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .p622-labels { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .p622-cats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .p622-node-head { display: none; }
  .p622-node { grid-template-columns: 1fr 1fr; gap: 5px 10px; padding: 9px 6px; }
  .p622-node-name { grid-column: 1 / -1; }
  .p622-node-status { text-align: right; }
}
@media (max-width: 640px) {
  .p622-head { display: block; }
  .p622-head-note { display: block; }
  .p622-head-note span { display: block; margin-top: 2px; line-height: 1.5; }
  .p622-head-actions { margin-top: 8px; justify-content: flex-start; }
  .p622-boundary { align-items: flex-start; }
  .p622-boundary-mark { flex-basis: 40px; height: 40px; }
  .p622-boundary-chip { display: none; }
  .p622-strip { gap: 5px 12px; }
  .p622-strip div { flex: 1 1 42%; justify-content: space-between; }
  .p622-card { padding: 12px 11px; }
  .p622-card-head { display: block; }
  .p622-hint { display: inline-block; margin-top: 6px; }
  .p622-labels { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
  .p622-format-tabs { margin-top: 8px; }
  .p622-format-meta { gap: 5px 13px; }
  .p622-format-actions { margin-left: 0; width: 100%; }
  .p622-content pre { font-size: 9px; max-height: 290px; }
  .p622-node { grid-template-columns: 1fr 1fr; }
  .p622-history { display: grid; grid-template-columns: 1fr auto; gap: 5px 8px; }
  .p622-history-main { grid-column: 1; }
  .p622-history-num { grid-column: 1 / -1; order: 3; }
  .p622-history-formats { grid-column: 1 / -1; order: 4; }
}
</style>
