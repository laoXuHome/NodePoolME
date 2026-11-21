<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const items = ref([])
const total = ref(0)
const loading = ref(false)
const groups = ref([])
const airports = ref([])
const filters = ref({ q: '', airport_id: null, protocol: '', region: '', group_id: null,
                     tags: [], passed: '', publishable: '', sort: 'latency', limit: 50 })
const page = ref(1)
const detail = ref(null)
const detailOpen = ref(false)
const history = ref([])
const testing = ref(false)

const protocolOptions = computed(() => ['ss', 'vmess', 'vless', 'trojan', 'hysteria2', 'tuic', 'socks5', 'http'])

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
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function loadMeta() {
  groups.value = await api.get('/api/groups')
  airports.value = await api.get('/api/airports')
}

function search() { page.value = 1; load() }
function reset() {
  filters.value = { q: '', airport_id: null, protocol: '', region: '', group_id: null,
                    tags: [], passed: '', publishable: '', sort: 'latency', limit: 50 }
  page.value = 1
  load()
}

async function openDetail(row) {
  try {
    detail.value = await api.get(`/api/nodes/${row.id}`)
    history.value = detail.value.history
    detailOpen.value = true
  } catch (e) { ElMessage.error(e.message) }
}

async function testNode() {
  testing.value = true
  try {
    const res = await api.post(`/api/nodes/${detail.value.id}/test`, {})
    const ok = res.results.filter((r) => r.ok).length
    ElMessage.success(`检测完成：${ok}/${res.results.length} 项通过`)
    await openDetail(detail.value)
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { testing.value = false }
}

function latencyText(n) { return n ? `${n} ms` : '—' }
function latencyType(n) { if (!n) return 'info'; if (n < 200) return 'success'; if (n < 500) return 'warning'; return 'danger' }
function groupCell(row, gid) {
  const res = row.results[String(gid)]
  if (!res) return null
  return res
}

onMounted(async () => { await loadMeta(); await load() })
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">节点池</h2>
    <div class="np-page-desc">展示去重后的缓存节点、最近一次各检测组结果与动态生成的最终名称。</div>

    <div class="np-toolbar">
      <el-input v-model="filters.q" placeholder="搜索名称 / 服务器" style="width:220px" clearable
                @keyup.enter="search" @clear="search" />
      <el-select v-model="filters.airport_id" placeholder="机场" clearable style="width:150px" @change="search">
        <el-option v-for="a in airports" :key="a.id" :label="a.name" :value="a.id" />
      </el-select>
      <el-select v-model="filters.protocol" placeholder="协议" clearable style="width:130px" @change="search">
        <el-option v-for="p in protocolOptions" :key="p" :label="p" :value="p" />
      </el-select>
      <el-select v-model="filters.group_id" placeholder="检测组" clearable style="width:140px" @change="search">
        <el-option v-for="g in groups.filter(x => x.enabled)" :key="g.id" :label="`${g.ident} · ${g.name}`" :value="g.id" />
      </el-select>
      <el-select v-model="filters.tags" placeholder="标签（须同时命中）" multiple collapse-tags clearable style="width:190px" @change="search">
        <el-option v-for="g in groups.filter(x => x.enabled)" :key="g.ident" :label="g.ident" :value="g.ident" />
      </el-select>
      <el-select v-model="filters.publishable" placeholder="下发结果" clearable style="width:130px" @change="search">
        <el-option label="仅下发节点" value="1" />
        <el-option label="仅不下发" value="0" />
      </el-select>
      <el-select v-model="filters.passed" placeholder="组结果/标签" clearable style="width:150px" @change="search">
        <el-option label="未命中所选组" value="miss" />
        <el-option label="至少有一个标签" value="qualified" />
        <el-option label="没有任何标签" value="untagged" />
      </el-select>
      <el-select v-model="filters.sort" style="width:130px" @change="search">
        <el-option label="按延迟" value="latency" />
        <el-option label="按名称" value="name" />
        <el-option label="按检测时间" value="tested" />
      </el-select>
      <el-button @click="search"><el-icon><Search /></el-icon>查询</el-button>
      <el-button @click="reset">重置</el-button>
      <div class="np-flex" />
      <span class="np-muted">共 {{ total }} 个节点</span>
    </div>

    <div class="np-card">
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
            <el-tag v-if="groupCell(row, g.id)" :type="groupCell(row, g.id).ok ? 'success' : 'danger'"
                    size="small" effect="plain">
              {{ groupCell(row, g.id).ok ? (groupCell(row, g.id).latency_ms + '') : '✕' }}
            </el-tag>
            <span v-else class="np-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="延迟" width="96">
          <template #default="{ row }">
            <el-tag :type="latencyType(row.latency)" size="small" effect="plain">{{ latencyText(row.latency) }}</el-tag>
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
      <div v-if="!items.length" class="np-empty">没有符合条件的节点</div>
      <div style="display:flex;justify-content:flex-end;padding:12px">
        <el-pagination v-model:current-page="page" :page-size="filters.limit" :total="total"
                       layout="prev, pager, next" @current-change="load" />
      </div>
    </div>

    <el-dialog v-model="detailOpen" :title="detail?.display_name || '节点详情'" width="720px">
      <el-descriptions v-if="detail" :column="2" border size="small">
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
      <div class="np-section-title" style="margin-top:16px">延迟检测（下发依据）</div>
      <el-table :data="detail?.latency_history || []" size="small" max-height="180">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatTs(row.ts) }}</template>
        </el-table-column>
        <el-table-column label="结果" width="80">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '通过' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="延迟(ms)" width="95" />
        <el-table-column label="错误">
          <template #default="{ row }">
            <span class="np-muted">{{ row.error_type || '—' }} {{ row.message || '' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="np-section-title" style="margin-top:16px">
        标签历史
        <div class="np-flex" />
        <el-button size="small" type="primary" :loading="testing" @click="testNode">立即检测此节点</el-button>
      </div>
      <el-table :data="history" size="small" max-height="260">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatTs(row.ts) }}</template>
        </el-table-column>
        <el-table-column label="检测组" width="120">
          <template #default="{ row }">{{ row.group_name }} ({{ row.ident }})</template>
        </el-table-column>
        <el-table-column label="结果" width="80">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '通过' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="延迟(ms)" width="95" />
        <el-table-column label="错误">
          <template #default="{ row }">
            <span class="np-muted">{{ row.error_type || '—' }} {{ row.message || '' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>
