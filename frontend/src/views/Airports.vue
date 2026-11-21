<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const items = ref([])
const loading = ref(false)
const dialog = ref(false)
const editing = ref(null)
const form = ref({ name: '', url: '', interval_minutes: 60, daily_time: '', enabled: true })
const fetchUa = ref('')
async function loadSystem() {
  try { fetchUa.value = (await api.get('/api/settings')).system?.fetch_ua || '' } catch (e) { /* ignore */ }
}
const logsOpen = ref(false)
const logs = ref([])
const logsTitle = ref('')

async function load() {
  loading.value = true
  try { items.value = await api.get('/api/airports') } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function openCreate() {
  editing.value = null
  form.value = { name: '', url: '', interval_minutes: 60, daily_time: '', enabled: true }
  dialog.value = true
}

function openEdit(row) {
  editing.value = row
  form.value = { name: row.name, url: row.url, interval_minutes: row.interval_minutes,
                 daily_time: row.daily_time || '', enabled: row.enabled }
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

async function fetchNow(row) {
  try {
    const res = await api.post(`/api/airports/${row.id}/fetch`)
    const parse = res.parse || {}
    const detail = parse.total ? `，源里 ${parse.total} 条，解析 ${parse.parsed} 条` : ''
    const skipped = parse.unsupported && Object.keys(parse.unsupported).length
      ? `；跳过 ${Object.entries(parse.unsupported).map(([k, v]) => `${k}×${v}`).join('、')}`
      : ''
    ElMessage.success(`拉取成功：${res.total} 个节点（新增 ${res.inserted}${detail}${skipped}）`)
    await load()
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

onMounted(() => { load(); loadSystem() })
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

    <div class="np-card">
      <el-table :data="items" size="default">
        <el-table-column prop="name" label="机场名称" min-width="150" />
        <el-table-column label="订阅地址" min-width="240">
          <template #default="{ row }"><span class="np-mono">{{ row.url_masked }}</span></template>
        </el-table-column>
        <el-table-column prop="node_count" label="节点" width="80" />
        <el-table-column label="拉取周期" width="150">
          <template #default="{ row }">
            <span v-if="row.daily_time">每天 {{ row.daily_time }}</span>
            <span v-else-if="row.interval_minutes > 0">每 {{ row.interval_minutes }} 分钟</span>
            <el-tooltip v-else content="定时与间隔都为 0：不会自动拉取，只能点「立即拉取」">
              <el-tag type="info" size="small">仅手动</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="最近成功" width="170">
          <template #default="{ row }">{{ formatTs(row.last_success_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tooltip v-if="row.last_status === 'error'" :content="row.last_error || ''">
              <el-tag type="danger" size="small">拉取失败</el-tag>
            </el-tooltip>
            <el-tag v-else-if="row.last_status === 'ok'" type="success" size="small">正常</el-tag>
            <el-tag v-else type="info" size="small">未拉取</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="(v) => toggle(row, v)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="fetchNow(row)">立即拉取</el-button>
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
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
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
