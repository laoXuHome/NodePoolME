<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs } from '../api'

const info = ref(null)
const form = ref({ sub_name: '', sub_token: '', regenerate_token: false })
const loading = ref(false)
const saving = ref(false)
const isMobile = ref(false)
const listTab = ref('published')
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
const speedExcludeLevels = ref([])
const SPEED_OPTIONS = [
  { value: 'excellent', label: '优', color: '#10B981' },
  { value: 'good', label: '良', color: '#4682B4' },
  { value: 'poor', label: '差', color: '#E9C46A' },
  { value: 'danger', label: '劣', color: '#EF4444' },
  { value: 'error', label: '异常', color: '#F59E0B', symbol: '!' }
]
const LOON_REASON = {
  protocol: '协议不支持',
  cipher: '加密方式不支持',
  transport: '传输方式不支持',
  port: '端口小于 3',
  plugin: 'SS 插件不支持',
}
const loonHint = computed(() => {
  const l = info.value && info.value.filter && info.value.filter.loon
  if (!l) return null
  const reasons = Object.entries(l.reasons || {})
    .map(([k, v]) => `${LOON_REASON[k] || k} ${v}`)
    .join('、')
  return { available: l.available || 0, excluded: l.excluded || 0, reasons }
})

const filterHint = computed(() => {
  const f = info.value && info.value.filter
  if (!f) return null
  return {
    active: !!f.active,
    keywords: f.keywords || [],
    speedLevels: f.speed_levels || [],
    eligible: f.eligible || 0,
    kept: f.kept || 0,
    dropped: f.dropped || 0,
    keywordDropped: f.keyword_dropped || 0,
    speedDropped: f.speed_dropped || 0,
    sample: f.sample || [],
    nodes: f.nodes || [],
    filtered: f.filtered || [],
  }
})

const publishedNodes = computed(() => (filterHint.value ? filterHint.value.nodes : []))
const filteredNodes = computed(() => (filterHint.value ? filterHint.value.filtered : []))

async function load() {
  loading.value = true
  try {
    info.value = await api.get('/api/subscription')
    form.value = { ...info.value.settings, regenerate_token: false }
    const levels = (form.value.sub_filter_speed_levels || '').split(',').filter(Boolean)
    speedExcludeLevels.value = levels
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
      sub_filter_speed_levels: speedExcludeLevels.value.join(','),
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
})
onUnmounted(() => { if (mq) mq.removeEventListener('change', syncMobile) })
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">订阅输出</h2>
    <div class="np-page-desc">
      系统只生成一个统一订阅 <span class="np-mono">/sub/all</span>；访问需要 Token，内容不暴露机场原始 URL。下发关键词与测速标签过滤是输出层的手动口子；测速标签勾选即排除。
    </div>

    <el-row :gutter="16">
      <el-col :md="14" :xs="24">
        <!-- 订阅地址 + 订阅输出设置（合并） -->
        <div class="np-card" style="padding:20px" v-if="info">
          <div class="np-section-title">
            订阅地址
            <div class="np-flex" />
            <el-button type="primary" size="small" :loading="saving" @click="save">保存</el-button>
          </div>

          <el-form label-width="96px" size="default" class="np-sub-form">
            <el-form-item label="订阅名称">
              <el-input v-model="form.sub_name" maxlength="40" />
            </el-form-item>
            <el-form-item label="访问 Token">
              <el-input v-model="form.sub_token" />
              <el-button link type="warning" style="margin-left:8px" @click="regen">重新生成</el-button>
            </el-form-item>
          </el-form>

          <el-radio-group v-model="shownLinks" size="small" style="margin-bottom:10px;margin-top:4px">
            <el-radio-button value="queryless">无参数（反代推荐）</el-radio-button>
            <el-radio-button value="query">带 format 参数</el-radio-button>
          </el-radio-group>
          <div class="link-row" v-for="(label, key) in { loon: 'Loon（iOS 专用）', clash: 'Clash / Mihomo', singbox: 'sing-box', base64: '通用 Base64' }" :key="key">
            <div class="link-label">{{ label }}</div>
            <div class="link-control">
              <el-input :model-value="prettyLink((shownLinks === 'queryless' ? querylessLinks : fullLinks)[key])" readonly size="small"
                        :title="(shownLinks === 'queryless' ? querylessLinks : fullLinks)[key]"
                        :type="isMobile ? 'textarea' : 'text'" :autosize="{ minRows: 1, maxRows: 2 }" />
              <el-button class="link-copy" @click="copy((shownLinks === 'queryless' ? querylessLinks : fullLinks)[key])">复制</el-button>
            </div>
          </div>
          <div v-if="loonHint" class="np-muted" style="margin-top:10px">
            Loon 专用订阅：可用 {{ loonHint.available }} 个节点<template v-if="loonHint.excluded">，自动排除 {{ loonHint.excluded }} 个（{{ loonHint.reasons }}）</template>。
            只输出 Loon 支持的协议、加密方式与传输方式，并对密码和节点名做转义。
          </div>
          <div class="np-muted" style="margin-top:10px">
            反代转发会把 <span class="np-mono">&amp;</span> 转义坏，优先用「无参数」地址 <span class="np-mono">/sub/all/&lt;token&gt;/clash</span>；带 format 参数时按客户端 UA 自动选择。
          </div>
        </div>

        <!-- 当前订阅状态 + 下发过滤（合并成一个板块） -->
        <div class="np-card" style="padding:20px;margin-top:16px">
          <div class="np-section-title">当前订阅状态与下发过滤</div>
          <el-descriptions v-if="info" :column="isMobile ? 1 : 3" size="small" border>
            <el-descriptions-item label="节点数">{{ info.meta.nodes || 0 }}</el-descriptions-item>
            <el-descriptions-item label="最近生成">{{ formatTs(info.meta.updated_at) }}</el-descriptions-item>
            <el-descriptions-item label="名称">{{ info.meta.name }}</el-descriptions-item>
            <el-descriptions-item v-if="filterHint" label="下发过滤">
              <span v-if="!filterHint.active" class="np-muted">未设置（不过滤，合格节点全部下发）</span>
              <span v-else>
                排除 <b>{{ filterHint.dropped }}</b> 个 / 合格 {{ filterHint.eligible }} 个，实际下发 {{ filterHint.kept }} 个
                <span class="np-muted" style="margin-left:6px">
                  关键词 {{ filterHint.keywordDropped }} · 标签 {{ filterHint.speedDropped }}
                </span>
              </span>
            </el-descriptions-item>
          </el-descriptions>

          <div class="np-filter-row">
            <div class="np-filter-head">
              <b>测速标签排除</b>
              <span class="np-muted">勾选即排除带该标签的节点；未勾选不排除。可选优、良、差、劣、异常。</span>
            </div>
            <el-checkbox-group v-model="speedExcludeLevels" class="speed-filter-grid">
              <el-checkbox v-for="opt in SPEED_OPTIONS" :key="opt.value" :value="opt.value">
                <span class="np-speed-dot is-tag" :class="{ 'is-warn': opt.symbol }"
                      :style="{ background: opt.color }">{{ opt.symbol || '' }}</span>{{ opt.label }}
              </el-checkbox>
            </el-checkbox-group>
          </div>

          <div class="np-filter-row">
            <div class="np-filter-head">
              <b>下发关键词过滤</b>
              <span class="np-muted">留空 = 不过滤；关键词之间用<strong>空格</strong>或<strong>逗号</strong>分隔，命中即屏蔽（不区分大小写）</span>
            </div>
            <el-input v-model="form.sub_filter_keywords" type="textarea" :rows="2" resize="none"
                      class="np-filter-box np-filter-compact"
                      placeholder="例如：剩余流量 官网，过期 某机场" />
            <div class="np-filter-foot">
              <span class="np-muted">命中机场名 / 节点名 / 服务器地址的节点，或勾选标签对应的节点，不会进入订阅；不影响机场检测与评分。保存后点「立即重新生成」或等下次定时下发生效。</span>
              <div class="np-flex" />
              <el-button size="small" :loading="saving" @click="save">保存过滤规则</el-button>
            </div>
          </div>

          <div style="margin-top:14px;display:flex;gap:10px">
            <el-button type="primary" @click="rebuild"><el-icon><Refresh /></el-icon>立即重新生成</el-button>
            <span class="np-muted" style="font-size:12px;align-self:center">
              按当前已保存的配置重新下发，并同时刷新右侧两个清单（未保存的输入不参与）
            </span>
          </div>
        </div>
      </el-col>

      <el-col :md="10" :xs="24">
        <!-- 列表：实际下发节点 / 关键词过滤命中 -->
        <div class="np-card" style="padding:20px">
          <div class="np-section-title">
            节点清单
            <div class="np-flex" />
            <el-radio-group v-model="listTab" size="small">
              <el-radio-button value="published">实际下发 {{ publishedNodes.length }}</el-radio-button>
              <el-radio-button value="filtered">被过滤 {{ filteredNodes.length }}</el-radio-button>
            </el-radio-group>
          </div>

          <div v-if="listTab === 'published'">
            <div class="np-node-list" v-if="publishedNodes.length">
              <div class="np-node-item" v-for="(name, i) in publishedNodes" :key="'p' + i">
                <span class="np-node-idx">{{ i + 1 }}</span>
                <span class="np-node-name" :title="name">{{ name }}</span>
              </div>
            </div>
            <div v-else class="np-empty">暂无已下发节点</div>
          </div>

          <div v-else>
            <div class="np-node-list" v-if="filteredNodes.length">
              <div class="np-node-item is-hit" v-for="(item, i) in filteredNodes" :key="'f' + i">
                <span class="np-node-idx">{{ i + 1 }}</span>
                <span class="np-node-name" :title="item.name">{{ item.name }}</span>
                <span class="np-node-kw" :title="(item.reason_type === 'speed' ? '标签：' : '命中关键词：') + item.reason">
                  {{ item.reason_type === 'speed' ? '标签 ' + item.reason : '关键词 ' + item.reason }}
                </span>
              </div>
            </div>
            <div v-else class="np-empty">
              {{ filterHint && filterHint.active ? '当前没有节点被下发过滤排除' : '未设置下发过滤' }}
            </div>
          </div>

          <div class="np-muted" style="margin-top:10px">
            列表按实时检测结果与当前过滤规则计算：被过滤项显示触发的速度等级或关键词，方便校正。
          </div>
        </div>
      </el-col>
    </el-row>
  </div>
</template>
