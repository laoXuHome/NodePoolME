<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const info = ref(null)
const form = ref({ sub_name: '', sub_max_latency: 500, sub_max_nodes: 0,
                   sub_include_unavailable: false, sub_token: '', regenerate_token: false })
const preview = ref('')
const previewFmt = ref('clash')
const loading = ref(false)
const saving = ref(false)

const fullLinks = computed(() => {
  if (!info.value) return {}
  const base = info.value.base_url
  const out = {}
  Object.entries(info.value.links).forEach(([k, v]) => { out[k] = base + v })
  return out
})

async function load() {
  loading.value = true
  try {
    info.value = await api.get('/api/subscription')
    form.value = { ...info.value.settings, regenerate_token: false }
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function save() {
  saving.value = true
  try {
    await api.patch('/api/subscription', {
      sub_name: form.value.sub_name,
      sub_token: form.value.sub_token,
      regenerate_token: form.value.regenerate_token,
      sub_max_latency: form.value.sub_max_latency,
      sub_max_nodes: form.value.sub_max_nodes,
      sub_include_unavailable: form.value.sub_include_unavailable
    })
    ElMessage.success('订阅设置已保存')
    form.value.regenerate_token = false
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

async function rebuild() {
  try {
    const meta = await api.post('/api/subscription/rebuild', {})
    ElMessage.success(`已重新生成：${meta.nodes} 个节点`)
    await load()
  } catch (e) { ElMessage.error(e.message) }
}

async function loadPreview() {
  try {
    preview.value = await api.get(`/api/subscription/preview?fmt=${previewFmt.value}`)
  } catch (e) { ElMessage.error(e.message) }
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器拒绝剪贴板访问，请手动复制')
  }
}

function regen() { form.value.regenerate_token = true; ElMessage.info('保存后生效') }

onMounted(async () => { await load(); await loadPreview() })
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">订阅输出</h2>
    <div class="np-page-desc">
      系统只生成一个统一订阅 <span class="np-mono">/sub/all</span>，包含所有合格节点；访问需要 Token，内容不暴露机场原始 URL。
    </div>

    <el-row :gutter="16">
      <el-col :md="15" :xs="24">
        <div class="np-card" style="padding:20px">
          <div class="np-section-title">订阅地址</div>
          <div v-if="info">
            <div class="link-row" v-for="(label, key) in { clash: 'Clash / Mihomo', singbox: 'sing-box', base64: '通用 Base64' }" :key="key">
              <div class="link-label">{{ label }}</div>
              <el-input :model-value="fullLinks[key]" readonly size="small">
                <template #append>
                  <el-button @click="copy(fullLinks[key])">复制</el-button>
                </template>
              </el-input>
            </div>
            <div class="np-muted" style="margin-top:10px">
              不带 format 参数时按客户端 UA 自动选择格式；也可使用 <span class="np-mono">/sub/all/&lt;token&gt;</span>。
            </div>
          </div>
          <el-divider />
          <div class="np-section-title">当前订阅状态</div>
          <el-descriptions v-if="info" :column="3" size="small" border>
            <el-descriptions-item label="节点数">{{ info.meta.nodes || 0 }}</el-descriptions-item>
            <el-descriptions-item label="最近生成">{{ formatTs(info.meta.updated_at) }}</el-descriptions-item>
            <el-descriptions-item label="名称">{{ info.meta.name }}</el-descriptions-item>
          </el-descriptions>
          <div style="margin-top:14px;display:flex;gap:10px">
            <el-button type="primary" @click="rebuild"><el-icon><Refresh /></el-icon>立即重新生成</el-button>
            <el-button @click="loadPreview">刷新预览</el-button>
          </div>
        </div>

        <div class="np-card" style="padding:20px;margin-top:16px">
          <div class="np-section-title">
            订阅输出设置
            <div class="np-flex" />
            <el-button type="primary" size="small" :loading="saving" @click="save">保存</el-button>
          </div>
          <el-form label-width="130px">
            <el-form-item label="订阅名称">
              <el-input v-model="form.sub_name" maxlength="40" />
            </el-form-item>
            <el-form-item label="访问 Token">
              <el-input v-model="form.sub_token" />
              <el-button link type="warning" style="margin-left:8px" @click="regen">重新生成</el-button>
            </el-form-item>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="下发延迟上限(ms)">
                  <el-input-number v-model="form.sub_max_latency" :min="0" :max="60000" style="width:100%" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="最大节点数">
                  <el-input-number v-model="form.sub_max_nodes" :min="0" style="width:100%" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="12">
              <el-col :span="12">
              </el-col>
              <el-col :span="12">
                <el-form-item label="包含不可用">
                  <el-switch v-model="form.sub_include_unavailable" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <div class="np-muted">下发依据是延迟检测：延迟不超过上限的节点才进入订阅；检测组只用于生成节点名代号。0 表示不限制。</div>
        </div>
      </el-col>

      <el-col :md="9" :xs="24">
        <div class="np-card" style="padding:20px">
          <div class="np-section-title">
            输出预览
            <div class="np-flex" />
            <el-select v-model="previewFmt" size="small" style="width:120px" @change="loadPreview">
              <el-option label="Clash" value="clash" />
              <el-option label="sing-box" value="singbox" />
              <el-option label="Base64" value="base64" />
            </el-select>
          </div>
          <div class="np-code">{{ preview }}</div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.link-row { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.link-label { width: 112px; font-size: 13px; color: #4b5563; }
</style>
