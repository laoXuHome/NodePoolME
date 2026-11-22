<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs, prettyBytes } from '../api'

const items = ref([])
const loading = ref(false)
const dialog = ref(false)
const editing = ref(null)
const form = ref({ name: '', url: '', interval_minutes: 60, daily_time: '' })
const fetchUa = ref('')
async function loadSystem() {
  try { fetchUa.value = (await api.get('/api/settings')).system?.fetch_ua || '' } catch (e) { /* ignore */ }
}
const logsOpen = ref(false)
const logs = ref([])
const logsTitle = ref('')
const filterOpen = ref(false)
const filterLoading = ref(false)
const filterSaving = ref(false)
const filterAirport = ref(null)
const filterKeywords = ref([])
const filterNodes = ref([])
const filterDefaultKeywords = ref([])
const filterNewKeyword = ref('')
const isMobile = ref(false)
let mq = null
function syncMobile(e) { isMobile.value = e.matches }

async function load() {
  loading.value = true
  try { items.value = await api.get('/api/airports') } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function openCreate() {
  editing.value = null
  form.value = { name: '', url: '', interval_minutes: 60, daily_time: '' }
  dialog.value = true
}

function openEdit(row) {
  editing.value = row
  form.value = { name: row.name, url: row.url, interval_minutes: row.interval_minutes,
                 daily_time: row.daily_time || '' }
  dialog.value = true
}

async function submit() {
  if (!form.value.name || !form.value.url) return ElMessage.warning('名称和订阅地址必填')
  const payload = { ...form.value, daily_time: form.value.daily_time || null }
  try {
    if (editing.value) {
      await api.patch(`/api/airports/${editing.value.id}`, payload)
      ElMessage.success('已更新')
    } else {
      await api.post('/api/airports', payload)
      ElMessage.success('已添加机场')
    }
    dialog.value = false
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

async function toggle(row, enabled) {
  try {
    await api.patch(`/api/airports/${row.id}`, { enabled })
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

async function fetchNow(row, confirmed = false) {
  if (!confirmed) {
    try {
      await ElMessageBox.confirm(
        `部分机场只允许首次订阅，频繁拉取可能触发风控，甚至导致该机场全部节点失效。确定要立即拉取「${row.name}」吗？`,
        '立即拉取有风险',
        { type: 'warning', confirmButtonText: '继续拉取', cancelButtonText: '取消' }
      )
    } catch {
      return
    }
  }
  try {
    const res = await api.post(`/api/airports/${row.id}/fetch`)
    const parse = res.parse || {}
    const detail = parse.total ? `，源里 ${parse.total} 条，解析 ${parse.parsed} 条` : ''
    const skipped = parse.unsupported && Object.keys(parse.unsupported).length
      ? `；跳过 ${Object.entries(parse.unsupported).map(([k, v]) => `${k}×${v}`).join('、')}`
      : ''
    ElMessage.success(`拉取成功：${res.total_count ?? res.total} 个节点（新增 ${res.inserted}${detail}${skipped}）`)
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

function trafficColor(row) {
  const p = Number(row.traffic_percent || 0)
  if (p >= 90) return '#ff3b30'
  if (p >= 70) return '#ff9500'
  return '#0071e3'
}

function expireDate(row) {
  return row.traffic_expire ? new Date(row.traffic_expire * 1000) : null
}

function pad2(n) { return String(n).padStart(2, '0') }

function expireText(row) {
  if (!row.traffic_available) return '未提供'
  const d = expireDate(row)
  if (!d) return '不限'
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

function expireHint(row) {
  const d = expireDate(row)
  if (!d) return ''
  const days = Math.ceil((d.getTime() - Date.now()) / 86400000)
  if (days < 0) return '已到期'
  if (days === 0) return '今天到期'
  return `剩 ${days} 天`
}

function expireClass(row) {
  const d = expireDate(row)
  if (!d) return ''
  const days = Math.ceil((d.getTime() - Date.now()) / 86400000)
  if (days <= 7) return 'np-expire-danger'
  if (days <= 30) return 'np-expire-warn'
  return ''
}

async function reloadFilter() {
  if (!filterAirport.value) return
  filterLoading.value = true
  try {
    const res = await api.get(`/api/airports/${filterAirport.value.id}/filter`)
    filterAirport.value = res.airport
    filterKeywords.value = res.keywords || []
    filterNodes.value = res.nodes || []
    filterDefaultKeywords.value = res.default_keywords || []
  } catch (e) { ElMessage.error(e.message) }
  finally { filterLoading.value = false }
}

async function openFilter(row) {
  filterAirport.value = row
  filterOpen.value = true
  filterNewKeyword.value = ''
  await reloadFilter()
}

function addKeyword() {
  const added = String(filterNewKeyword.value || '').split(/[,，、\n]/)
  for (const raw of added) {
    const word = raw.trim()
    if (!word || word.length > 24) continue
    if (!filterKeywords.value.some(k => k.toLowerCase() === word.toLowerCase())) {
      filterKeywords.value.push(word)
    }
  }
  filterNewKeyword.value = ''
}

function removeKeyword(word) {
  filterKeywords.value = filterKeywords.value.filter(k => k !== word)
}

function resetKeywords() {
  filterKeywords.value = [...filterDefaultKeywords.value]
}

async function saveFilter() {
  if (!filterAirport.value) return
  filterSaving.value = true
  try {
    await api.patch(`/api/airports/${filterAirport.value.id}`, { filter_keywords: filterKeywords.value })
    ElMessage.success('关键词已保存，现有缓存已重新归类')
    await load()
    await reloadFilter()
  } catch (e) { ElMessage.error(e.message) }
  finally { filterSaving.value = false }
}

async function admitNode(node) {
  if (!filterAirport.value) return
  try {
    const res = await api.post(`/api/airports/${filterAirport.value.id}/filter/admit`, { fingerprint: node.fingerprint })
    ElMessage.success(`已加入节点池，当前 ${res.total_count ?? res.node_count} 个节点`)
    await load()
    await reloadFilter()
  } catch (e) { ElMessage.error(e.message) }
}

async function showLogs(row) {
  logsTitle.value = `${row.name} · 拉取历史`
  logs.value = await api.get(`/api/airports/${row.id}/logs`)
  logsOpen.value = true
}

async function remove(row) {
  await ElMessageBox.confirm(
    `删除机场「${row.name}」后，其 ${row.node_count} 个缓存节点与拉取历史会被移除，检测历史保留。检测批次需重新运行以获得新订阅。`,
    '确认删除', { type: 'warning' })
  try {
    const res = await api.del(`/api/airports/${row.id}`)
    ElMessage.success(`已删除，移除 ${res.removed_nodes} 个节点`)
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

onMounted(() => {
  mq = window.matchMedia('(max-width: 768px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', syncMobile)
  load()
  loadSystem()
})
onUnmounted(() => { if (mq) mq.removeEventListener('change', syncMobile) })
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">机场管理</h2>
    <div class="np-page-desc">
      每个机场独立拉取、独立周期；拉取或解析失败时保留旧缓存，只有完整解析成功才事务性更新。
      拉取统一使用固定 UA <span class="np-mono">{{ fetchUa || 'clash-verge/v1.19.6' }}</span>（不轮换客户端身份，避免触发机场风控），每次拉取的历史记录里都会写明。
    </div>

    <div class="np-toolbar">
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon>添加机场</el-button>
      <el-button @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
      <div class="np-flex" />
      <span class="np-muted">共 {{ items.length }} 个机场</span>
    </div>

    <div v-if="isMobile" class="np-mobile-list">
      <div v-for="row in items" :key="row.id" class="np-mobile-card">
        <div class="np-mobile-card-head">
          <div class="np-mobile-title">{{ row.name }}</div>
          <el-tag v-if="row.last_status === 'error'" type="danger" size="small" style="margin-top:2px">拉取失败</el-tag>
          <el-tag v-else-if="row.last_status === 'ok'" type="success" size="small" style="margin-top:2px">正常</el-tag>
          <el-tag v-else type="info" size="small" style="margin-top:2px">未拉取</el-tag>
          <el-switch :model-value="row.enabled" size="small" @change="(v) => toggle(row, v)" />
        </div>
        <div class="np-mobile-meta">
          <span>
            {{ row.node_count }} 个节点
            <span v-if="row.filtered_count" class="np-muted">（含过滤 {{ row.filtered_count }}）</span>
          </span>
          <span v-if="row.daily_time">每天 {{ row.daily_time }}</span>
          <span v-else-if="row.interval_minutes > 0">每 {{ row.interval_minutes }} 分钟</span>
          <span v-else>仅手动</span>
          <span>{{ formatTs(row.last_success_at) }}</span>
        </div>
        <div v-if="row.traffic_available" class="np-mobile-traffic">
          <div class="np-mobile-traffic-head">
            <span>已用 {{ prettyBytes(row.traffic_used) }} / {{ row.traffic_total ? prettyBytes(row.traffic_total) : '不限' }}</span>
            <span v-if="row.traffic_total">{{ row.traffic_percent }}%</span>
          </div>
          <el-progress :percentage="row.traffic_percent" :stroke-width="5" :show-text="false" :color="trafficColor(row)" />
          <div class="np-mobile-traffic-foot">
            <span>到期 {{ expireText(row) }}</span>
            <span v-if="expireHint(row)" :class="expireClass(row)">{{ expireHint(row) }}</span>
          </div>
        </div>
        <div v-else class="np-mobile-traffic-empty">流量与到期：机场未返回订阅信息</div>
        <div class="np-mobile-actions">
          <div class="np-flex" />
          <el-button link type="primary" size="small" @click="fetchNow(row)">拉取</el-button>
          <el-button link size="small" @click="openFilter(row)">过滤{{ row.filtered_count ? ' ' + row.filtered_count : '' }}</el-button>
          <el-button link size="small" @click="showLogs(row)">历史</el-button>
          <el-button link size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" size="small" @click="remove(row)">删除</el-button>
        </div>
      </div>
      <div v-if="!items.length" class="np-empty">暂无机场</div>
    </div>

    <div v-if="!isMobile" class="np-card">
      <el-table :data="items" size="default">
        <el-table-column prop="name" label="机场名称" min-width="120" />
        <el-table-column label="节点" width="72">
          <template #default="{ row }">
            <el-tooltip :content="`总数 = 节点池 ${row.pool_count} + 已过滤 ${row.filtered_count}`" placement="top">
              <span>{{ row.node_count }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="流量" min-width="200">
          <template #default="{ row }">
            <div v-if="row.traffic_available" class="np-traffic-cell">
              <div class="np-traffic-text">
                <span><b>{{ prettyBytes(row.traffic_used) }}</b><span class="np-muted"> / {{ row.traffic_total ? prettyBytes(row.traffic_total) : '不限' }}</span></span>
                <span v-if="row.traffic_total" class="np-traffic-pct">{{ row.traffic_percent }}%</span>
              </div>
              <el-progress :percentage="row.traffic_percent" :stroke-width="6" :show-text="false" :color="trafficColor(row)" />
            </div>
            <el-tooltip v-else content="机场订阅响应未返回 Subscription-Userinfo 头；下一次成功拉取后会自动读取">
              <span class="np-muted">未提供</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="到期" width="120">
          <template #default="{ row }">
            <template v-if="row.traffic_available">
              <div :class="expireClass(row)">{{ expireText(row) }}</div>
              <div v-if="expireHint(row)" class="np-muted" style="font-size:11px;line-height:1.4">{{ expireHint(row) }}</div>
            </template>
            <span v-else class="np-muted">未提供</span>
          </template>
        </el-table-column>
        <el-table-column label="拉取" width="150">
          <template #default="{ row }">
            <div>{{ formatTs(row.last_success_at) }}</div>
            <div class="np-muted" style="font-size:11px;line-height:1.4">
              <span v-if="row.daily_time">每天 {{ row.daily_time }}</span>
              <span v-else-if="row.interval_minutes > 0">每 {{ row.interval_minutes }} 分钟</span>
              <span v-else>仅手动</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="76">
          <template #default="{ row }">
            <el-tooltip v-if="row.last_status === 'error'" :content="row.last_error || ''">
              <el-tag type="danger" size="small">拉取失败</el-tag>
            </el-tooltip>
            <el-tag v-else-if="row.last_status === 'ok'" type="success" size="small">正常</el-tag>
            <el-tag v-else type="info" size="small">未拉取</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="76">
          <template #default="{ row }">
            <div class="np-flex" style="align-items:center;gap:8px">
              <el-switch :model-value="row.enabled" @change="(v) => toggle(row, v)" />
              <el-tooltip v-if="!row.enabled" content="已停用：该机场的缓存节点不进入节点池、不参与检测与下发；重新启用会直接恢复缓存，不会重新拉取">
                <el-tag type="info" size="small">节点不入池</el-tag>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="286">
          <template #default="{ row }">
            <el-tooltip content="部分机场只允许首次订阅；点击后会先弹出风险确认" placement="top">
              <el-button link type="primary" @click="fetchNow(row)">立即拉取</el-button>
            </el-tooltip>
            <el-button link type="primary" @click="openFilter(row)">过滤{{ row.filtered_count ? ' ' + row.filtered_count : '' }}</el-button>
            <el-button link @click="showLogs(row)">历史</el-button>
            <el-button link @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!items.length" class="np-empty">暂无机场</div>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑机场' : '添加机场'" width="560px">
      <el-form label-width="96px">
        <el-form-item label="机场名称">
          <el-input v-model="form.name" placeholder="例如：机场A" maxlength="60" />
        </el-form-item>
        <el-form-item label="订阅地址">
          <el-input v-model="form.url" type="textarea" :rows="3" placeholder="https://example.com/api/v1/client/subscribe?token=..." />
        </el-form-item>
        <el-form-item label="间隔拉取">
          <el-input-number v-model="form.interval_minutes" :min="0" :max="1440" />
          <span class="np-muted" style="margin-left:8px">分钟；填 0 表示不按间隔拉取</span>
        </el-form-item>
        <el-form-item label="定时拉取">
          <el-input v-model="form.daily_time" placeholder="如 03:30，留空则不按时刻拉取" style="width:220px" />
          <span class="np-muted" style="margin-left:8px">每天该时刻拉取一次</span>
        </el-form-item>
        <div class="np-muted" style="margin:-6px 0 14px 96px">
          两者可以同时生效（谁先到谁先拉）；<b>间隔填 0 且定时留空</b> 时该机场完全不会自动拉取，只在你点「立即拉取」（或首次配置后手动触发）时才拉。检测批次不会触发机场拉取，只读本地缓存。
        </div>
        <div class="np-muted" style="margin:-6px 0 0 96px">启用/停用请在列表中的开关操作。</div>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="filterOpen" :title="filterAirport ? filterAirport.name + ' · 过滤节点' : '过滤节点'"
               width="760px" :fullscreen="isMobile">
      <div v-loading="filterLoading">
        <div class="np-filter-tip">
          原始节点名包含任一关键词时，整条不导入节点池，<b>从不检测、从不下发</b>；清空关键词则不过滤。
          手动加入节点池的节点会记住准入，之后同名节点即使仍命中关键词也会保留。
        </div>

        <div class="np-filter-head">
          <b>过滤关键词</b>
          <el-tag type="info" size="small">{{ filterKeywords.length }} 个</el-tag>
          <div class="np-flex" />
          <el-button link type="primary" @click="resetKeywords">恢复默认</el-button>
        </div>
        <div class="np-keyword-list">
          <el-tag v-for="word in filterKeywords" :key="word" closable type="info" @close="removeKeyword(word)">
            {{ word }}
          </el-tag>
          <span v-if="!filterKeywords.length" class="np-muted">未设置关键词，当前不过滤任何节点</span>
        </div>
        <div class="np-keyword-add">
          <el-input v-model="filterNewKeyword" size="small" maxlength="24"
                    placeholder="输入关键词后回车，支持逗号分隔" @keyup.enter="addKeyword" />
          <el-button size="small" @click="addKeyword">添加</el-button>
        </div>
        <div class="np-filter-save">
          <span class="np-muted">保存后立即对现有缓存重新归类，无需重新拉取；清空后保存表示不过滤。</span>
        </div>

        <div class="np-filter-head" style="margin-top:22px">
          <b>被过滤节点</b>
          <el-tag type="info" size="small">{{ filterNodes.length }} 个</el-tag>
          <div class="np-flex" />
          <span class="np-muted">最近一次拉取中命中的条目</span>
        </div>

        <div v-if="isMobile" class="np-filter-list">
          <div v-for="node in filterNodes" :key="node.fingerprint" class="np-filter-item">
            <div class="np-filter-name">{{ node.raw_name }}</div>
            <div class="np-filter-meta">
              <el-tag type="warning" size="small">{{ node.keyword }}</el-tag>
              <span class="np-mono">{{ node.server }}:{{ node.port }}</span>
            </div>
            <div class="np-filter-item-actions">
              <el-button size="small" type="primary" plain @click="admitNode(node)">加入节点池</el-button>
            </div>
          </div>
          <div v-if="!filterNodes.length" class="np-empty">没有被过滤的节点</div>
        </div>

        <el-table v-else :data="filterNodes" size="small" max-height="340">
          <el-table-column prop="raw_name" label="原始节点名" min-width="190" show-overflow-tooltip />
          <el-table-column label="命中关键词" width="96">
            <template #default="{ row }"><el-tag type="warning" size="small">{{ row.keyword }}</el-tag></template>
          </el-table-column>
          <el-table-column label="服务器" min-width="130" show-overflow-tooltip>
            <template #default="{ row }"><span class="np-mono">{{ row.server }}:{{ row.port }}</span></template>
          </el-table-column>
          <el-table-column label="更新时间" width="138">
            <template #default="{ row }">{{ formatTs(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="96">
            <template #default="{ row }">
              <el-button link type="primary" @click="admitNode(row)">加入节点池</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!isMobile && !filterNodes.length" class="np-empty">没有被过滤的节点</div>
      </div>
      <template #footer>
        <el-button @click="filterOpen = false">关闭</el-button>
        <el-button type="primary" :loading="filterSaving" @click="saveFilter">保存关键词</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="logsOpen" :title="logsTitle" size="520px">
      <el-table :data="logs" size="small">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatTs(row.ts) }}</template>
        </el-table-column>
        <el-table-column label="结果" width="70">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '成功' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="node_count" label="节点" width="70" />
        <el-table-column prop="message" label="信息" show-overflow-tooltip />
      </el-table>
      <div v-if="!logs.length" class="np-empty">暂无拉取记录</div>
    </el-drawer>
  </div>
</template>
