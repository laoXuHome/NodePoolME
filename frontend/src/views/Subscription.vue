<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const info = ref(null)
const form = ref({ sub_name: '', sub_token: '', regenerate_token: false })
const preview = ref('')
const previewFmt = ref('clash')
const loading = ref(false)
const saving = ref(false)
const isMobile = ref(false)
let mq = null
function syncMobile(e) { isMobile.value = e.matches }

const fullLinks = computed(() => {
  if (!info.value) return {}
  return pick(info.value.links)
})

const querylessLinks = computed(() => {
  if (!info.value) return {}
  return pick(info.value.queryless || {})
})

function pick(paths) {
  const base = info.value.base_url
  const out = {}
  Object.entries(paths).forEach(([k, v]) => { out[k] = base + v })
  return out
}

const shownLinks = ref('queryless')

async function load() {
  loading.value = true
  try {
    info.value = await api.get('/api/subscription')
    form.value = { ...info.value.settings, regenerate_token: false }
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function prettyLink(url) {
  if (!url) return ''
  const max = isMobile.value ? 38 : 78
  if (url.length <= max) return url
  const head = Math.ceil((max - 1) / 2)
  const tail = max - 1 - head
  return url.slice(0, head) + '…' + url.slice(-tail)
}

async function save() {
  saving.value = true
  try {
    await api.patch('/api/subscription', {
      sub_name: form.value.sub_name,
      sub_token: form.value.sub_token,
      regenerate_token: form.value.regenerate_token,
      sub_filter_keywords: form.value.sub_filter_keywords || '',
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

onMounted(async () => {
  mq = window.matchMedia('(max-width: 768px)')
  isMobile.value = mq.matches
  mq.addEventListener('change', syncMobile)
  await load()
  await loadPreview()
})
onUnmounted(() => { if (mq) mq.removeEventListener('change', syncMobile) })
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
            <el-radio-group v-model="shownLinks" size="small" style="margin-bottom:10px">
              <el-radio-button value="queryless">无参数（反代推荐）</el-radio-button>
              <el-radio-button value="query">带 format 参数</el-radio-button>
            </el-radio-group>
            <div class="link-row" v-for="(label, key) in { clash: 'Clash / Mihomo', singbox: 'sing-box', base64: '通用 Base64' }" :key="key">
              <div class="link-label">{{ label }}</div>
              <div class="link-control">
                <el-input :model-value="prettyLink((shownLinks === 'queryless' ? querylessLinks : fullLinks)[key])" readonly size="small"
                          :title="(shownLinks === 'queryless' ? querylessLinks : fullLinks)[key]"
                          :type="isMobile ? 'textarea' : 'text'" :autosize="{ minRows: 1, maxRows: 2 }" />
                <el-button class="link-copy" @click="copy((shownLinks === 'queryless' ? querylessLinks : fullLinks)[key])">复制</el-button>
              </div>
            </div>
            <div class="np-muted" style="margin-top:10px">
              反代转发会把 <span class="np-mono">&amp;</span> 转义坏，优先用「无参数」地址 <span class="np-mono">/sub/all/&lt;token&gt;/clash</span>；带 format 参数时按客户端 UA 自动选择。
            </div>
          </div>
          <el-divider />
          <div class="np-section-title">当前订阅状态</div>
          <el-descriptions v-if="info" :column="isMobile ? 1 : 3" size="small" border>
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
            <el-form-item label="下发关键词过滤">
              <el-input v-model="form.sub_filter_keywords" type="textarea" :rows="4" resize="vertical"
                        class="np-filter-box"
                        placeholder="留空 = 不做过滤，检测合格就正常下发&#10;每行一个关键词，命中即屏蔽（不区分大小写），例如：&#10;剩余流量&#10;官网&#10;过期&#10;某机场" />
            </el-form-item>
          </el-form>
          <div class="np-muted">
            这是订阅输出这一层的唯一入口：只对<strong>已经检测筛选出来</strong>的合格节点再做一次屏蔽，命中机场名 / 节点名 / 服务器地址的节点不会出现在 <span class="np-mono">/sub/all</span> 里（三种格式与预览同步生效），手动追加节点同样拦截。<br />
            过滤不影响检测与评分：机场的可用率、下发率、标签合格率、综合评分仍按未过滤的结果与快照计算；留空时与之前完全一致。保存后点「立即重新生成」或等下次定时下发生效。
          </div>
          <div class="np-muted">下发依据是「系统设置 → 延迟检测」里的双目标上限（Google / Wikipedia，可分别自定义链接与阈值）；节点标签只用于生成节点名代号，节点数不设上限。</div>
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
