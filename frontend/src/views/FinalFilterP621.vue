<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const loading = ref(false)
const busy = ref(false)
const summary = ref(null)
const preview = ref(null)
const selectedCategory = ref('standard')
const expanded = ref({})
const dialog = reactive({ open: false, mode: 'create', id: null, keyword: '', enabled: true })

const s = computed(() => summary.value || {})
const totals = computed(() => s.value.totals || { internal: 0, filtered: 0, allowed: 0 })
const ruleStats = computed(() => s.value.rule_stats || { total: 0, enabled: 0, disabled: 0, matched: 0 })
const rules = computed(() => s.value.rules || [])
const cats = computed(() => preview.value?.categories || [])
const active = computed(() => cats.value.find((c) => c.code === selectedCategory.value) || cats.value[0] || {
  code: 'standard', title: '标准综合优选', internal: 0, filtered: 0, allowed: 0,
  filtered_nodes: [], allowed_nodes: []
})
const catLabel = (code) => (cats.value.find((c) => c.code === code)?.label) || code
const categoryLine = (codes) => (codes || []).map(catLabel).join('、') || '—'

const hitMap = computed(() => {
  const map = {}
  for (const cat of cats.value) {
    for (const node of cat.filtered_nodes || []) {
      for (const hit of node.hits || []) {
        if (!map[hit.rule_id]) map[hit.rule_id] = { nodes: [], seen: {} }
        const key = `${cat.code}-${node.node_id}`
        if (map[hit.rule_id].seen[key]) continue
        map[hit.rule_id].seen[key] = true
        map[hit.rule_id].nodes.push({
          key,
          node_id: node.node_id,
          node_name: node.node_name,
          airport_name: node.airport_name,
          server: node.server,
          category: cat.label,
          hits: (node.hits || []).filter((h) => h.rule_id === hit.rule_id)
        })
      }
    }
  }
  return map
})

const ruleHits = (rule) => hitMap.value[rule.id]?.nodes || []

function toggleHits(rule) {
  expanded.value = { ...expanded.value, [rule.id]: !expanded.value[rule.id] }
}

function openCreate() {
  Object.assign(dialog, { open: true, mode: 'create', id: null, keyword: '', enabled: true })
}

function openEdit(rule) {
  Object.assign(dialog, { open: true, mode: 'edit', id: rule.id, keyword: rule.keyword, enabled: rule.enabled })
}

async function loadPreview() {
  preview.value = await api.get('/api/final-filter/preview')
  if (!preview.value.categories.some((c) => c.code === selectedCategory.value)) {
    selectedCategory.value = preview.value.categories[0]?.code || 'standard'
  }
}

async function loadAll() {
  loading.value = true
  try {
    const [sum, prev] = await Promise.all([
      api.get('/api/final-filter/summary'),
      api.get('/api/final-filter/preview')
    ])
    summary.value = sum
    preview.value = prev
  } catch (e) {
    ElMessage.error(e.message || '关键词过滤数据加载失败')
  } finally {
    loading.value = false
  }
}

async function afterMutation(res, message) {
  summary.value = res.summary
  await loadPreview()
  ElMessage.success(message)
}

async function saveRule() {
  const keyword = (dialog.keyword || '').trim()
  if (!keyword) {
    ElMessage.warning('请输入关键词')
    return
  }
  busy.value = true
  try {
    const res = dialog.mode === 'edit'
      ? await api.patch(`/api/final-filter/rules/${dialog.id}`, { keyword, enabled: dialog.enabled })
      : await api.post('/api/final-filter/rules', { keyword, enabled: dialog.enabled })
    dialog.open = false
    await afterMutation(res, dialog.mode === 'edit' ? '关键词已更新，过滤预览已重算' : '关键词已新增，过滤预览已重算')
  } catch (e) {
    ElMessage.error(e.message || '关键词保存失败')
  } finally {
    busy.value = false
  }
}

async function toggleRule(rule) {
  busy.value = true
  try {
    const res = await api.patch(`/api/final-filter/rules/${rule.id}`, { enabled: !rule.enabled })
    await afterMutation(res, !rule.enabled ? '关键词已启用，过滤预览已重算' : '关键词已停用，过滤预览已重算')
  } catch (e) {
    ElMessage.error(e.message || '关键词状态切换失败')
    await loadAll()
  } finally {
    busy.value = false
  }
}

async function removeRule(rule) {
  try {
    await ElMessageBox.confirm(`确定删除关键词「${rule.keyword}」吗？`, '删除关键词', { type: 'warning' })
  } catch { return }
  busy.value = true
  try {
    const res = await api.del(`/api/final-filter/rules/${rule.id}`)
    await afterMutation(res, '关键词已删除，过滤预览已重算')
  } catch (e) {
    ElMessage.error(e.message || '关键词删除失败')
  } finally {
    busy.value = false
  }
}

async function restoreDefaults() {
  try {
    await ElMessageBox.confirm('恢复后当前全部自定义关键词会被默认规则替换，确定继续吗？', '恢复默认规则', { type: 'warning' })
  } catch { return }
  busy.value = true
  try {
    const res = await api.post('/api/final-filter/defaults')
    await afterMutation(res, '默认关键词已恢复，过滤预览已重算')
  } catch (e) {
    ElMessage.error(e.message || '恢复默认失败')
  } finally {
    busy.value = false
  }
}

async function recompute() {
  busy.value = true
  try {
    const res = await api.post('/api/final-filter/recompute')
    summary.value = res
    await loadPreview()
    ElMessage.success('已按当前关键词重算过滤预览')
  } catch (e) {
    ElMessage.error(e.message || '重算失败')
  } finally {
    busy.value = false
  }
}

const timeText = (v) => formatTs(v)
const scoreText = (v) => (v === null || v === undefined || v === '' ? '—' : Number(v).toFixed(1))
const msText = (v) => (v === null || v === undefined || v === '' ? '—' : `${Math.round(Number(v))} ms`)

onMounted(loadAll)
</script>

<template>
  <div v-loading="loading || busy" class="np-page p621-page">
    <header class="p621-head">
      <div class="p621-head-note">
        <b>规则即改即算</b>
        <span>只重算本页过滤预览，不触发检测、测速、竞争或新版本</span>
      </div>
      <div class="p621-head-actions">
        <el-tag type="info" effect="plain" size="small">过滤预览 · 未生成订阅</el-tag>
        <el-tag v-if="s.p6?.version_no" type="success" effect="plain" size="small">来源 {{ s.p6.version_no }}</el-tag>
        <el-button size="small" @click="loadAll">刷新读取</el-button>
        <el-button size="small" :disabled="busy" @click="recompute">重算预览</el-button>
      </div>
    </header>

    <section class="p621-boundary">
      <div class="p621-boundary-mark">SAFE<br>GATE</div>
      <div>
        <b>关键词过滤不参与检测、评分和竞争。</b>
        <p>规则变化只重算本页过滤结果：不拉取机场，不触发 P2/P3/P4 检测或测速，不运行 P5 竞争，不新建或修改 P6.1 版本历史。</p>
      </div>
      <span class="p621-boundary-chip">允许下发 = 预览</span>
    </section>

    <section class="p621-kpis">
      <article>
        <span>内部智能优选</span>
        <b>{{ totals.internal }}</b>
        <small>六类累计条目，来自 P6.1</small>
      </article>
      <article class="is-filtered">
        <span>关键词过滤</span>
        <b>{{ totals.filtered }}</b>
        <small>命中任一启用规则</small>
      </article>
      <article class="is-allowed">
        <span>最终允许下发</span>
        <b>{{ totals.allowed }}</b>
        <small>仅过滤预览，不代表已发布</small>
      </article>
      <article class="is-rules">
        <span>启用关键词</span>
        <b>{{ ruleStats.enabled }}<em>/ {{ ruleStats.total }}</em></b>
        <small>{{ ruleStats.matched }} 条规则有命中</small>
      </article>
    </section>

    <section class="p621-card">
      <div class="p621-card-head">
        <div>
          <h3>关键词规则</h3>
          <p>大小写不敏感的包含匹配，覆盖节点名称、机场名称与服务器地址；同一节点可保留多条命中原因。</p>
        </div>
        <div class="p621-card-actions">
          <el-button type="primary" size="small" @click="openCreate">新增关键词</el-button>
          <el-button size="small" :disabled="busy" @click="restoreDefaults">恢复默认</el-button>
        </div>
      </div>

      <div class="p621-rule-list">
        <article v-for="rule in rules" :key="rule.id" class="p621-rule" :class="{ 'is-off': !rule.enabled }">
          <div class="p621-rule-main">
            <div class="p621-rule-title">
              <span class="p621-rule-id">R{{ rule.id }}</span>
              <b>{{ rule.keyword }}</b>
              <span class="p621-state" :class="rule.enabled ? 'is-on' : 'is-off'">{{ rule.enabled ? '启用' : '停用' }}</span>
            </div>
            <div class="p621-rule-meta">
              <span>命中 <b>{{ rule.hit_count }}</b> 个节点</span>
              <span><b>{{ rule.hit_record_count }}</b> 条原因</span>
              <span>类别 {{ categoryLine(rule.matched_categories) }}</span>
              <span>建 {{ timeText(rule.created_at) }}</span>
              <span>改 {{ timeText(rule.updated_at) }}</span>
            </div>
          </div>
          <div class="p621-rule-actions">
            <el-switch :model-value="rule.enabled" size="small" :disabled="busy" @change="toggleRule(rule)" />
            <el-button link size="small" :disabled="!rule.hit_count" @click="toggleHits(rule)">
              {{ expanded[rule.id] ? '收起命中' : '命中明细' }}
            </el-button>
            <el-button link size="small" @click="openEdit(rule)">编辑</el-button>
            <el-button link type="danger" size="small" @click="removeRule(rule)">删除</el-button>
          </div>
          <div v-if="expanded[rule.id]" class="p621-rule-hits">
            <div v-if="!ruleHits(rule).length" class="p621-muted">当前没有命中节点。</div>
            <div v-for="hit in ruleHits(rule)" :key="hit.key" class="p621-hit-line">
              <span class="p621-hit-cat">{{ hit.category }}</span>
              <b>{{ hit.node_name }}</b>
              <small>{{ hit.airport_name }} · {{ hit.server }}</small>
              <span class="p621-hit-reasons">
                <i v-for="reason in hit.hits" :key="reason.field">· {{ reason.field_label }}</i>
              </span>
            </div>
          </div>
        </article>
        <div v-if="!rules.length" class="p621-empty">暂无关键词，全部内部优选节点都会进入允许下发预览。</div>
      </div>
    </section>

    <section class="p621-card">
      <div class="p621-card-head">
        <div>
          <h3>六类最终过滤结果</h3>
          <p>六类分别计算内部优选、允许下发与关键词过滤；同一节点跨类命中时，只影响命中的类别。</p>
        </div>
        <div class="p621-legend">
          <span><i class="ok"></i>允许</span>
          <span><i class="no"></i>过滤</span>
        </div>
      </div>

      <div class="p621-cat-grid">
        <button v-for="cat in cats" :key="cat.code" class="p621-cat" :class="{ active: cat.code === selectedCategory }" @click="selectedCategory = cat.code">
          <span class="p621-cat-name">{{ cat.title }}</span>
          <div class="p621-cat-nums">
            <span><b class="ok">{{ cat.allowed }}</b><small>允许</small></span>
            <span><b class="no">{{ cat.filtered }}</b><small>过滤</small></span>
            <span><b>{{ cat.internal }}</b><small>内部</small></span>
          </div>
          <span class="p621-cat-code">{{ cat.short }} · {{ cat.label }}</span>
        </button>
      </div>
    </section>

    <section class="p621-card">
      <div class="p621-card-head">
        <div>
          <h3>{{ active.title }} · 过滤明细</h3>
          <p>内部 {{ active.internal }} · 允许 {{ active.allowed }} · 过滤 {{ active.filtered }}；被过滤节点仍保留在 P6.1 内部结果。</p>
        </div>
        <el-tag size="small" effect="plain" type="warning">仅预览</el-tag>
      </div>

      <div class="p621-detail-grid">
        <div class="p621-panel">
          <div class="p621-panel-head">
            <b>被过滤节点</b>
            <span>{{ active.filtered_nodes.length }} 条</span>
          </div>
          <div v-for="node in active.filtered_nodes" :key="node.node_id" class="p621-node is-filtered">
            <div class="p621-node-main">
              <b>{{ node.node_name }}</b>
              <small>{{ node.airport_name }} · {{ node.server }}:{{ node.port }}</small>
            </div>
            <div class="p621-node-hits">
              <span v-for="hit in node.hits" :key="`${hit.rule_id}-${hit.field}`" class="p621-hit-chip">
                {{ hit.keyword }}<i>· {{ hit.field_label }}</i>
              </span>
            </div>
            <div class="p621-node-side">
              <span>{{ timeText(node.filtered_at) }}</span>
            </div>
          </div>
          <div v-if="!active.filtered_nodes.length" class="p621-empty">当前类别没有节点命中关键词。</div>
        </div>

        <div class="p621-panel is-ok">
          <div class="p621-panel-head">
            <b>允许下发预览</b>
            <span>{{ active.allowed_nodes.length }} 条 · 未生成订阅</span>
          </div>
          <div v-for="node in active.allowed_nodes" :key="node.node_id" class="p621-node">
            <div class="p621-node-main">
              <b>{{ node.node_name }}</b>
              <small>{{ node.airport_name }} · {{ node.server }}:{{ node.port }}</small>
            </div>
            <div class="p621-node-side">
              <span>{{ scoreText(node.score) }} 分</span>
              <small>{{ msText(node.p80_ms) }} · {{ node.speed_grade || '无测速' }}</small>
            </div>
          </div>
          <div v-if="!active.allowed_nodes.length" class="p621-empty">当前类别没有允许下发节点。</div>
        </div>
      </div>
    </section>

    <footer class="p621-foot">
      规则重算时间 {{ timeText(preview?.computed_at) }} · 规则修订 {{ s.revision || 0 }} · 来源版本 {{ s.p6?.version_no || '尚未生成' }}
    </footer>

    <el-dialog v-model="dialog.open" :title="dialog.mode === 'edit' ? '编辑关键词' : '新增关键词'" width="min(440px, 92vw)">
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="关键词">
          <el-input v-model="dialog.keyword" maxlength="80" show-word-limit placeholder="中文、英文、数字或机场常用名称片段" @keyup.enter="saveRule" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="dialog.enabled" />
          <span class="p621-dialog-note">停用后保留规则，但不参与匹配。</span>
        </el-form-item>
        <p class="p621-dialog-scope">匹配范围：节点名称、机场名称、服务器地址；英文不区分大小写。</p>
      </el-form>
      <template #footer>
        <el-button @click="dialog.open = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="saveRule">保存并重算</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.p621-page { max-width: 1500px; }
.p621-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 2px 2px 0; }
.p621-head-note { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.p621-head-note b { font-size: 11.5px; color: #2f6f5b; white-space: nowrap; }
.p621-head-note span { font-size: 10px; color: #8a998f; }
.p621-head-actions { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.p621-boundary { margin: 14px 0 12px; display: flex; align-items: center; gap: 12px; padding: 12px 14px; border: 1px solid rgba(47,111,91,.16); border-radius: 12px; background: linear-gradient(100deg, rgba(240,247,243,.94), rgba(250,247,238,.86)); }
.p621-boundary-mark { flex: 0 0 46px; height: 46px; border-radius: 10px; background: #29453c; color: #f1dfae; font-size: 9px; line-height: 1.2; display: flex; align-items: center; justify-content: center; text-align: center; letter-spacing: .1em; font-weight: 700; }
.p621-boundary b { font-size: 12.5px; color: #29433a; }
.p621-boundary p { margin: 3px 0 0; font-size: 11.5px; line-height: 1.55; color: #71827a; }
.p621-boundary-chip { margin-left: auto; white-space: nowrap; font-size: 10px; color: #8a651c; background: #faf1dc; border-radius: 999px; padding: 5px 9px; }
.p621-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; margin-bottom: 12px; }
.p621-kpis article { padding: 12px 14px; border-radius: 13px; background: #fff; border: 1px solid rgba(31,55,47,.1); box-shadow: 0 5px 15px rgba(31,55,47,.04); }
.p621-kpis span { display: block; font-size: 10.5px; color: #7b8d83; }
.p621-kpis b { display: block; margin: 3px 0 1px; font-size: 26px; line-height: 1.1; color: #203a31; font-variant-numeric: tabular-nums; }
.p621-kpis em { font-size: 12px; font-style: normal; color: #94a39a; margin-left: 3px; }
.p621-kpis small { font-size: 9.5px; color: #91a097; }
.p621-kpis .is-filtered b { color: #b7791f; }
.p621-kpis .is-allowed b { color: #2f8f62; }
.p621-kpis .is-rules b { color: #3d6f8e; }
.p621-card { margin-bottom: 12px; padding: 14px 16px; border-radius: 15px; background: #fff; border: 1px solid rgba(31,55,47,.1); box-shadow: 0 7px 20px rgba(31,55,47,.045); }
.p621-card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 10px; }
.p621-card-head h3 { margin: 0; font-size: 15px; color: #203a31; letter-spacing: -.01em; }
.p621-card-head p { margin: 3px 0 0; font-size: 10.5px; line-height: 1.55; color: #7c8d84; max-width: 760px; }
.p621-card-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.p621-legend { display: flex; gap: 10px; font-size: 10px; color: #7b8d84; white-space: nowrap; }
.p621-legend i { display: inline-block; width: 7px; height: 7px; border-radius: 3px; margin-right: 3px; background: #2f8f62; }
.p621-legend i.no { background: #c68a2e; }
.p621-rule-list { display: grid; gap: 7px; }
.p621-rule { border: 1px solid rgba(31,55,47,.09); border-radius: 10px; background: #fbfdfc; overflow: hidden; }
.p621-rule.is-off { background: #f8f8f6; }
.p621-rule-main { display: flex; align-items: center; gap: 12px; padding: 9px 11px 3px; min-width: 0; }
.p621-rule-title { display: flex; align-items: center; gap: 7px; min-width: 0; flex: 0 1 auto; }
.p621-rule-id { flex: none; font-size: 9px; color: #98a69d; letter-spacing: .06em; background: #f0f4f1; padding: 2px 5px; border-radius: 4px; }
.p621-rule-title b { font-size: 13.5px; color: #244036; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p621-state { flex: none; font-size: 9px; border-radius: 999px; padding: 2px 7px; }
.p621-state.is-on { color: #2f7c5b; background: #e9f5ee; }
.p621-state.is-off { color: #8b9690; background: #efefeb; }
.p621-rule-meta { display: flex; flex-wrap: wrap; gap: 4px 10px; font-size: 10px; color: #84948b; flex: 1 1 auto; min-width: 0; }
.p621-rule-meta b { color: #526b60; font-variant-numeric: tabular-nums; }
.p621-rule-actions { display: flex; align-items: center; gap: 2px; padding: 0 9px 7px; flex-wrap: wrap; justify-content: flex-end; }
.p621-rule-actions :deep(.el-button) { margin-left: 0; }
.p621-rule-hits { padding: 4px 11px 10px 28px; display: grid; gap: 5px; border-top: 1px dashed rgba(31,55,47,.08); background: #fff; }
.p621-hit-line { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; font-size: 10px; color: #64776d; }
.p621-hit-cat { color: #2f6f5b; background: #edf5f0; padding: 1px 5px; border-radius: 4px; font-size: 9px; }
.p621-hit-line b { font-size: 10.5px; color: #29433a; }
.p621-hit-line small { color: #91a097; }
.p621-hit-reasons { display: flex; gap: 3px; }
.p621-hit-reasons i { font-style: normal; font-size: 9px; color: #9a6b1d; background: #fbf3e3; border-radius: 4px; padding: 1px 5px; }
.p621-muted { font-size: 10px; color: #93a198; }
.p621-cat-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 7px; }
.p621-cat { text-align: left; border: 1px solid rgba(31,55,47,.1); border-radius: 10px; padding: 9px 11px; background: #fbfdfc; cursor: pointer; transition: all .16s ease; }
.p621-cat:hover { border-color: rgba(47,111,91,.28); }
.p621-cat.active { border-color: #2f6f5b; background: #f2f8f4; box-shadow: 0 0 0 2px rgba(47,111,91,.08) inset; }
.p621-cat-name { display: block; font-size: 10.5px; color: #64776d; }
.p621-cat-nums { display: flex; gap: 12px; margin: 4px 0 2px; }
.p621-cat-nums span { display: flex; align-items: baseline; gap: 3px; }
.p621-cat-nums b { font-size: 19px; color: #29433a; font-variant-numeric: tabular-nums; }
.p621-cat-nums b.ok { color: #2f8f62; }
.p621-cat-nums b.no { color: #b7791f; }
.p621-cat-nums small { font-size: 8.5px; color: #93a198; }
.p621-cat-code { font-size: 8px; letter-spacing: .05em; color: #a1ada5; }
.p621-detail-grid { display: grid; grid-template-columns: 1.35fr 1fr; gap: 10px; }
.p621-panel { border: 1px solid rgba(31,55,47,.08); border-radius: 11px; background: #fcfdfc; padding: 8px; min-width: 0; }
.p621-panel.is-ok { background: #fbfdf9; }
.p621-panel-head { display: flex; justify-content: space-between; align-items: baseline; padding: 2px 4px 7px; border-bottom: 1px solid rgba(31,55,47,.06); }
.p621-panel-head b { font-size: 11.5px; color: #29433a; }
.p621-panel-head span { font-size: 9px; color: #94a29a; }
.p621-node { display: flex; align-items: center; gap: 8px; padding: 7px 4px; border-bottom: 1px dashed rgba(31,55,47,.06); min-width: 0; }
.p621-node:last-child { border-bottom: 0; }
.p621-node-main { min-width: 0; flex: 1 1 auto; }
.p621-node-main b { display: block; font-size: 11px; color: #29433a; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p621-node-main small, .p621-node-side small { display: block; font-size: 8.5px; color: #94a199; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p621-node.is-filtered .p621-node-main b { color: #8a5a16; }
.p621-node-hits { display: flex; flex-wrap: wrap; gap: 3px; flex: 0 1 40%; justify-content: flex-end; }
.p621-hit-chip { font-size: 9px; color: #9a6512; background: #fcf2df; border: 1px solid #f3e2c2; padding: 1px 4px; border-radius: 4px; white-space: nowrap; }
.p621-hit-chip i { font-style: normal; color: #b28c4e; margin-left: 3px; }
.p621-node-side { flex: 0 0 auto; text-align: right; font-size: 9px; color: #71857a; font-variant-numeric: tabular-nums; }
.p621-empty { padding: 14px 6px; font-size: 10.5px; color: #93a198; text-align: center; }
.p621-foot { padding: 2px 4px 12px; font-size: 9.5px; color: #94a29a; }
.p621-dialog-note { margin-left: 9px; font-size: 10px; color: #8a998f; }
.p621-dialog-scope { margin: 2px 0 0; font-size: 10px; color: #7f9086; }

@media (max-width: 1200px) {
  .p621-cat-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .p621-detail-grid { grid-template-columns: 1fr; }
}
@media (max-width: 980px) {
  .p621-cat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  .p621-head { flex-direction: column; gap: 8px; }
  .p621-head-actions { justify-content: flex-start; }
  .p621-boundary { align-items: flex-start; }
  .p621-boundary-mark { flex-basis: 40px; height: 40px; }
  .p621-boundary-chip { display: none; }
  .p621-kpis { grid-template-columns: 1fr 1fr; gap: 7px; }
  .p621-kpis b { font-size: 23px; }
  .p621-card { padding: 11px 10px; }
  .p621-card-head { flex-direction: column; gap: 7px; }
  .p621-card-actions { width: 100%; }
  .p621-card-actions :deep(.el-button) { flex: 1; }
  .p621-rule-main { flex-direction: column; align-items: flex-start; gap: 4px; }
  .p621-rule-title { width: 100%; }
  .p621-rule-actions { justify-content: flex-start; padding-left: 11px; }
  .p621-rule-hits { padding-left: 11px; }
  .p621-cat-grid { grid-template-columns: 1fr 1fr; }
  .p621-cat { padding: 8px; }
  .p621-cat-nums { gap: 7px; }
  .p621-cat-nums b { font-size: 16px; }
  .p621-node { flex-wrap: wrap; }
  .p621-node-main { flex: 1 1 58%; }
  .p621-node-hits { flex: 1 1 100%; justify-content: flex-start; order: 3; }
  .p621-node-side { flex: 1 1 auto; text-align: right; }
}
</style>
