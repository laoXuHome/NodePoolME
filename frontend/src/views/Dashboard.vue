<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const data = ref(null)
const loading = ref(false)
const running = ref(false)
const timer = ref(null)

async function load() {
  loading.value = true
  try {
    data.value = await api.get('/api/dashboard')
    running.value = data.value.runtime.running
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    loading.value = false
  }
}

async function runNow() {
  try {
    await api.post('/api/run', { force: true })
    ElMessage.success('已启动：延迟检测 + 全部标签检测，完成后自动替换订阅')
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

const publishText = computed(() => {
  const ps = data.value?.publish_schedule
  if (!ps || !ps.enabled) return '未启用'
  if (ps.mode === 'interval') {
    return ps.next_at ? `每 ${ps.interval_minutes} 分钟（下次 ${ps.next_at.slice(11, 16)}）` : `每 ${ps.interval_minutes} 分钟`
  }
  return `每天 ${ps.time}`
})

function roundDone(name) {
  const item = (data.value?.runtime?.group_progress || []).find((g) => g.name === name)
  return item ? `${item.done}/${item.total}` : '—'
}
function roundPercent(name) {
  const item = (data.value?.runtime?.group_progress || []).find((g) => g.name === name)
  if (!item || !item.total) return 0
  return Math.round(item.done / item.total * 100)
}

onMounted(() => {
  load()
  timer.value = setInterval(() => { if (!document.hidden) load() }, 15000)
})
onUnmounted(() => clearInterval(timer.value))
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">仪表盘</h2>
    <div class="np-page-desc">机场拉取、节点缓存与检测批次总览。检测只读取本地缓存，不会触发机场重新拉取。</div>

    <div class="np-stat-grid" v-if="data">
      <div class="np-stat">
        <div class="label">机场</div>
        <div class="value">{{ data.airports }}</div>
        <div class="hint">启用 {{ data.airports_enabled }} 个</div>
      </div>
      <div class="np-stat">
        <div class="label">缓存节点</div>
        <div class="value">{{ data.nodes_total }}</div>
        <div class="hint">可用 {{ data.nodes_available }} · 降级 {{ data.nodes_degraded }} · 不可用 {{ data.nodes_unavailable }}</div>
      </div>
      <div class="np-stat">
        <div class="label">可下发节点</div>
        <div class="value">{{ data.nodes_qualified }}</div>
        <div class="hint">延迟 ≤ {{ data.latency_probe?.ceiling || '不限' }}ms 才进入 /sub/all</div>
      </div>
      <div class="np-stat">
        <div class="label">统一订阅</div>
        <div class="value">{{ data.subscription.nodes || 0 }}</div>
        <div class="hint">{{ formatTs(data.subscription.updated_at) }}</div>
      </div>
    </div>

    <el-row :gutter="16" style="margin-top:16px">
      <el-col :md="14" :xs="24">
        <div class="np-card" style="padding:18px 20px">
          <div class="np-section-title">
            检测任务
            <el-tag v-if="data?.runtime.running" type="warning" size="small">运行中</el-tag>
            <el-tag v-else type="success" size="small">空闲</el-tag>
            <div class="np-flex" />
            <el-button size="small" type="primary" :disabled="data?.runtime.running" @click="runNow">立即检测并下发</el-button>
          </div>
          <div v-if="data">
            <el-progress v-if="data.runtime.running" :percentage="data.runtime.progress"
                         :stroke-width="12" striped striped-flow />
            <div class="np-muted" style="margin:10px 0 14px">
              引擎 {{ data.runtime.engine }} · 批次 #{{ data.runtime.batch_id || '—' }} ·
              进度 {{ data.runtime.done }}/{{ data.runtime.total }} ·
              块 {{ data.runtime.block_index || 0 }}/{{ data.runtime.blocks || 0 }}（{{ data.runtime.block_size || 0 }} 节点）·
              {{ data.runtime.phase }} · 上次启动 {{ formatTs(data.runtime.last_cycle_at) }}
            </div>
            <div class="np-muted" style="margin:0 0 10px">
              延迟检测 {{ data.latency_probe?.enabled ? '启用' : '停用' }} ·
              <span class="np-mono">{{ data.latency_probe?.url }}</span> ·
              期望 {{ data.latency_probe?.status }} · 上限 {{ data.latency_probe?.ceiling || '不限' }}ms ·
              通过 {{ data.latency_probe?.passed }} 个节点 ·
              定时下发 {{ publishText }}
            </div>
            <el-table :data="data.groups" size="small">
              <el-table-column prop="name" label="检测组" />
              <el-table-column prop="ident" label="标识" width="80" />
              <el-table-column prop="url" label="目标" show-overflow-tooltip />
              <el-table-column label="本轮进度" width="190">
                <template #default="{ row }">
                  <div class="np-flex" style="gap:8px">
                    <el-progress :percentage="roundPercent(row.name)" :stroke-width="8" style="flex:1" />
                    <span class="np-muted">{{ roundDone(row.name) }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="passed" label="累计通过" width="100" />
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                    {{ row.enabled ? '启用' : '停用' }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>

        <div class="np-card" style="padding:18px 20px;margin-top:16px">
          <div class="np-section-title">机场状态</div>
          <el-table :data="data?.airport_list || []" size="small">
            <el-table-column prop="name" label="机场" />
            <el-table-column prop="node_count" label="节点" width="80" />
            <el-table-column label="最近成功" width="170">
              <template #default="{ row }">{{ formatTs(row.last_success_at) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.last_status === 'ok'" type="success" size="small">正常</el-tag>
                <el-tag v-else-if="row.last_status === 'error'" type="danger" size="small">失败</el-tag>
                <el-tag v-else type="info" size="small">未拉取</el-tag>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="!data?.airport_list?.length" class="np-empty">还没有机场，先到「机场管理」添加订阅。</div>
        </div>
      </el-col>

      <el-col :md="10" :xs="24">
        <div class="np-card" style="padding:18px 20px">
          <div class="np-section-title">最近日志</div>
          <div class="np-log">
            <div v-for="log in data?.logs || []" :key="log.id">
              <span class="np-muted">{{ formatTs(log.ts) }}</span>
              <span :class="`lv-${log.level}`"> [{{ log.level }}]</span>
              <span class="np-muted">{{ log.source }}</span>
              {{ log.message }}
            </div>
            <div v-if="!data?.logs?.length" class="np-empty">暂无日志</div>
          </div>
        </div>

        <div class="np-card" style="padding:18px 20px;margin-top:16px">
          <div class="np-section-title">最近批次</div>
          <div v-if="data?.batch">
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="批次号">#{{ data.batch.id }}</el-descriptions-item>
              <el-descriptions-item label="状态">{{ data.batch.status }}</el-descriptions-item>
              <el-descriptions-item label="开始">{{ formatTs(data.batch.started_at) }}</el-descriptions-item>
              <el-descriptions-item label="结束">{{ formatTs(data.batch.finished_at) }}</el-descriptions-item>
              <el-descriptions-item label="处理">
                {{ data.batch.processed_count }}/{{ data.batch.target_count }}
              </el-descriptions-item>
              <el-descriptions-item label="已发布">
                {{ data.batch.published ? '是' : '否' }}
              </el-descriptions-item>
            </el-descriptions>
          </div>
          <div v-else class="np-empty">尚未运行过检测批次</div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>
