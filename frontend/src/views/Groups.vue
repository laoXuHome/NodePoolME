<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatTs } from '../api'

const items = ref([])
const loading = ref(false)
const dialog = ref(false)
const editing = ref(null)
const blank = () => ({ name: '', ident: '', url: '', method: 'GET', expect_status: '200-399',
                       expect_body: '', max_latency: 0, enabled: true, sort_order: 10 })
const form = ref(blank())

async function load() {
  loading.value = true
  try { items.value = await api.get('/api/groups') } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function openCreate() { editing.value = null; form.value = blank(); dialog.value = true }
function openEdit(row) { editing.value = row; form.value = { ...row }; dialog.value = true }

async function submit() {
  if (!form.value.name || !form.value.ident || !form.value.url) return ElMessage.warning('名称、标识、检测 URL 必填')
  const payload = {
    name: form.value.name, ident: form.value.ident, url: form.value.url, method: form.value.method,
    expect_status: form.value.expect_status,
    expect_body: form.value.expect_body, max_latency: form.value.max_latency,
    enabled: form.value.enabled,
    sort_order: form.value.sort_order
  }
  try {
    if (editing.value) {
      await api.patch(`/api/groups/${editing.value.id}`, payload)
      ElMessage.success('已更新检测组')
    } else {
      await api.post('/api/groups', payload)
      ElMessage.success('已新增检测组')
    }
    dialog.value = false
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

async function toggle(row, enabled) {
  try { await api.patch(`/api/groups/${row.id}`, { enabled }); await load() }
  catch (e) { ElMessage.error(e.message) }
}

async function remove(row) {
  await ElMessageBox.confirm(`删除检测组「${row.name}」会同时删除其检测结果，节点名的中间段将不再包含 ${row.name}。`,
                             '确认删除', { type: 'warning' })
  try { await api.del(`/api/groups/${row.id}`); ElMessage.success('已删除'); await load() }
  catch (e) { ElMessage.error(e.message) }
}

async function runGroup(row) {
  try {
    await api.post('/api/run', { force: true, group_ids: [row.id] })
    ElMessage.success('已启动该检测组的批次，进度见仪表盘')
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

onMounted(load)
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">节点标签</h2>
    <div class="np-page-desc">
      每个标签对应一个检测目标与一个延迟上限（命中即打标）。超时统一走系统设置里的全局检测超时，标签本身不再单独配置超时和周期。「名称」只在本页面内部使用；真正进入节点名中间段的是「标识」（如「机场｜GPT·TG｜节点名」）。命中标签不影响是否下发，下发只看「系统设置 → 延迟检测」的延迟上限。
    </div>

    <div class="np-toolbar">
      <el-button type="primary" @click="openCreate"><el-icon><Plus /></el-icon>新增检测组</el-button>
      <el-button @click="load"><el-icon><Refresh /></el-icon>刷新</el-button>
      <div class="np-flex" />
      <span class="np-muted">顺序决定节点名中间段的排列：{{ items.filter(i => i.enabled).map(i => i.ident).join(' · ') || '（无启用标签）' }}</span>
    </div>

    <div class="np-card">
      <el-table :data="items">
        <el-table-column prop="name" label="名称（内部）" width="150" />
        <el-table-column label="标识（节点名）" width="120">
          <template #default="{ row }"><el-tag effect="dark" size="small">{{ row.ident }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="url" label="检测 URL" min-width="220" show-overflow-tooltip />
        <el-table-column prop="method" label="方法" width="80" />
        <el-table-column label="成功条件" min-width="170">
          <template #default="{ row }">
            <div class="np-mono">状态: {{ row.expect_status }}</div>
            <div class="np-mono" v-if="row.expect_body">包含: {{ row.expect_body }}</div>
            <div class="np-mono" v-if="row.max_latency">延迟 ≤ {{ row.max_latency }}ms</div>
          </template>
        </el-table-column>
        <el-table-column prop="passed" label="通过" width="80" />
        <el-table-column label="最近运行" width="160">
          <template #default="{ row }">{{ formatTs(row.last_run_at) }}</template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" @change="(v) => toggle(row, v)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="runGroup(row)">运行</el-button>
            <el-button link @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" :disabled="row.is_default" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑检测组' : '新增检测组'" width="620px">
      <el-form label-width="96px">
        <el-row :gutter="12">
          <el-col :span="14">
            <el-form-item label="名称">
              <el-input v-model="form.name" placeholder="内部名称，例如 ChatGPT" />
            </el-form-item>
          </el-col>
          <el-col :span="10">
            <el-form-item label="标识">
              <el-input v-model="form.ident" placeholder="进入节点名，如 GPT" maxlength="12" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="检测 URL">
          <el-input v-model="form.url" placeholder="https://example.com/generate_204" />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="HTTP 方法">
              <el-select v-model="form.method" style="width:100%">
                <el-option label="GET" value="GET" />
                <el-option label="HEAD" value="HEAD" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">

          </el-col>
          <el-col :span="8">

          </el-col>
        </el-row>
        <el-form-item label="预期状态码">
          <el-input v-model="form.expect_status" placeholder="例如 200 / 200-399 / 204,302" />
        </el-form-item>
        <el-form-item label="响应包含">
          <el-input v-model="form.expect_body" placeholder="留空则不校验；例如某个页面标题片段" />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="最大延迟">
              <el-input-number v-model="form.max_latency" :min="0" :max="60000" style="width:100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="排序">
              <el-input-number v-model="form.sort_order" :min="0" :max="999" style="width:100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <div class="np-muted">延迟 0 表示不限制。检测组只决定节点名里的代号，不决定是否下发；是否下发由「系统设置 → 延迟检测」的延迟上限决定。</div>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
