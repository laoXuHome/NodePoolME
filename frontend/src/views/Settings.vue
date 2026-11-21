<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatTs, prettyBytes } from '../api'

const form = ref({})
const system = ref({})
const loading = ref(false)
const saving = ref(false)
const logs = ref([])
const password = ref({ old_password: '', new_password: '' })
const file = ref(null)
const uploading = ref(false)
const backupBusy = ref(false)
const groups = ref([])
const requiredTags = ref([])

async function load() {
  loading.value = true
  try {
    const res = await api.get('/api/settings')
    form.value = res.settings
    system.value = res.system
    requiredTags.value = String(res.settings.sub_required_tags || '').split(',').filter(Boolean)
    groups.value = await api.get('/api/groups')
    logs.value = await api.get('/api/logs?limit=120')
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

async function save() {
  saving.value = true
  try {
    await api.patch('/api/settings', {
      global_concurrency: Number(form.value.global_concurrency),
      group_concurrency: Number(form.value.group_concurrency),
      ip_concurrency: Number(form.value.ip_concurrency),
      subnet_concurrency: Number(form.value.subnet_concurrency),
      request_interval_min_ms: Number(form.value.request_interval_min_ms),
      request_interval_max_ms: Number(form.value.request_interval_max_ms),
      test_interval_minutes: Number(form.value.test_interval_minutes),
      fail_threshold: Number(form.value.fail_threshold),
      history_days: Number(form.value.history_days),
      dispersion_mask: Number(form.value.dispersion_mask),
      latency_test_url: form.value.latency_test_url,
      latency_test_name: form.value.latency_test_name,
      latency_test_name2: form.value.latency_test_name2,
      latency_test_status: form.value.latency_test_status,
      latency_test_timeout: Number(form.value.latency_test_timeout),
      sub_max_latency: Number(form.value.sub_max_latency),
      sub_max_latency2: Number(form.value.sub_max_latency2),
      latency_test_url2: form.value.latency_test_url2,
      sub_publish_enabled: !!Number(form.value.sub_publish_enabled),
      sub_publish_time: form.value.sub_publish_time,
      sub_publish_mode: form.value.sub_publish_mode,
      sub_publish_interval_minutes: Number(form.value.sub_publish_interval_minutes),
      sub_required_tags: requiredTags.value
    })
    ElMessage.success('设置已保存')
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { saving.value = false }
}

async function changePassword() {
  if (!password.value.new_password || password.value.new_password.length < 6) {
    return ElMessage.warning('新密码至少 6 位')
  }
  try {
    await api.post('/api/auth/password', password.value)
    ElMessage.success('密码已修改')
    password.value = { old_password: '', new_password: '' }
  } catch (e) { ElMessage.error(e.message) }
}

async function backup() {
  backupBusy.value = true
  try {
    const res = await api.post('/api/system/backup', {})
    ElMessage.success(`备份完成：${res.file}（${prettyBytes(res.size)}）`)
  } catch (e) { ElMessage.error(e.message) }
  finally { backupBusy.value = false }
}

async function restore() {
  if (!file.value) return ElMessage.warning('请选择备份文件')
  uploading.value = true
  try {
    const res = await api.upload('/api/system/restore', file.value)
    ElMessage.success(`恢复完成，旧库已保存为 ${res.safety_copy}`)
    await load()
  } catch (e) { ElMessage.error(e.message) }
  finally { uploading.value = false }
}

function onFile(e) { file.value = e.target.files[0] || null }

function downloadBackup() { window.open('/api/system/backup/download', '_blank') }

onMounted(load)
</script>

<template>
  <div class="np-page" v-loading="loading">
    <h2 class="np-page-title">系统设置</h2>
    <div class="np-page-desc">全局并发优先于单组并发；请求间隔由统一调度器管理，多组同时运行不会突破全局上限。</div>

    <el-row :gutter="16">
      <el-col :md="14" :xs="24">
        <div class="np-card" style="padding:20px;margin-bottom:16px">
          <div class="np-section-title">
            延迟检测（决定是否下发订阅）
            <div class="np-flex" />
            <el-button type="primary" size="small" :loading="saving" @click="save">保存设置</el-button>
          </div>
          <el-form label-width="150px" size="default">
            <el-form-item label="目标 1（名称/链接/上限ms）">
              <div class="np-target-row">
                <el-input v-model="form.latency_test_name" class="np-target-name" maxlength="16"
                          placeholder="名称（如 Google）" />
                <el-input v-model="form.latency_test_url" class="np-target-url"
                          placeholder="检测链接 http://www.google.com/favicon.ico" />
                <el-input-number v-model="form.sub_max_latency" :min="0" :max="60000"
                                 class="np-target-cap" controls-position="right" />
              </div>
            </el-form-item>
            <el-form-item label="目标 2（名称/链接/上限ms）">
              <div class="np-target-row">
                <el-input v-model="form.latency_test_name2" class="np-target-name" maxlength="16"
                          placeholder="名称（如 Wikipedia）" />
                <el-input v-model="form.latency_test_url2" class="np-target-url"
                          placeholder="检测链接 http://www.wikipedia.org/favicon.ico" />
                <el-input-number v-model="form.sub_max_latency2" :min="0" :max="60000"
                                 class="np-target-cap" controls-position="right" />
              </div>
            </el-form-item>
            <el-row :gutter="12">
              <el-col :span="12" :xs="24">
                <el-form-item label="期望状态码">
                  <el-input v-model="form.latency_test_status" placeholder="200-399" />
                </el-form-item>
              </el-col>
              <el-col :span="12" :xs="24">
                <el-form-item label="检测超时(全局, s)">
                  <el-input-number v-model="form.latency_test_timeout" :min="1" :max="60" style="width:100%" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="必带标签">
              <el-checkbox-group v-model="requiredTags">
                <el-checkbox v-for="g in groups.filter(x => x.enabled)" :key="g.ident" :value="g.ident">
                  {{ g.ident }}
                </el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <div class="np-muted">
              上面两个链接与两个上限都是自定义配置，可改成任意可访问的 URL / 阈值（清空链接则回退内置默认）。<br />
              不勾选标签 = 只按延迟下发；勾选多个 = 必须同时命中全部勾选标签（AND）才下发。<br />
              两个链接各测 2 轮（并发），每个链接只要有一轮 ≤ 自己的上限即算达标；两个链接都达标才下发。
            </div>
            <div class="np-muted">每次检测都会对全部节点执行一次（无开关）；延迟不超过上限的节点才进入 <span class="np-mono">/sub/all</span>。检测组（GPT/TG/YT 及自定义）只决定节点名里的代号，不决定是否下发。上限填 0 表示不限制。<br />检测超时为全局值：延迟探测与所有标签检测共用这一个超时。</div>
            <el-divider />
            <el-form-item label="定时下发">
              <el-switch v-model="form.sub_publish_enabled" active-value="1" inactive-value="0" />
            </el-form-item>
            <el-form-item label="下发方式">
              <el-radio-group v-model="form.sub_publish_mode">
                <el-radio value="time">每天固定时刻</el-radio>
                <el-radio value="interval">固定间隔</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="form.sub_publish_mode === 'interval'" label="下发间隔(分钟)">
              <el-input-number v-model="form.sub_publish_interval_minutes" :min="5" :max="1440"
                               :step="30" style="width:200px" />
            </el-form-item>
            <el-form-item label="下发时间" v-if="form.sub_publish_mode === 'time'">
              <el-time-picker v-model="form.sub_publish_time" format="HH:mm" value-format="HH:mm"
                              placeholder="04:30" style="width:200px" />
            </el-form-item>
            <div class="np-muted">
              定时/间隔下发与仪表盘「立即检测并下发」走同一条流程：对全部节点跑延迟检测 + 全部检测组标签，检测期间暂停机场拉取，完成后才原子替换订阅。
              <template v-if="form.sub_publish_mode === 'time'">固定时刻模式下 15 分钟内未成功会在下一分钟重试。</template>
              <template v-else>间隔模式从保存设置（或上一次下发完成）开始计时，失败会在下一分钟重试，成功后才重新计时。</template>
            </div>
          </el-form>
        </div>

        <div class="np-card" style="padding:20px">
          <div class="np-section-title">
            调度与检测
            <div class="np-flex" />
            <el-button type="primary" size="small" :loading="saving" @click="save">保存设置</el-button>
          </div>
          <el-form label-width="150px" size="default">
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="全局并发"><el-input-number v-model="form.global_concurrency" :min="1" :max="300" style="width:100%" /></el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="单检测组并发"><el-input-number v-model="form.group_concurrency" :min="1" :max="100" style="width:100%" /></el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="同 IP 并发"><el-input-number v-model="form.ip_concurrency" :min="1" :max="10" style="width:100%" /></el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="同网段并发"><el-input-number v-model="form.subnet_concurrency" :min="1" :max="20" style="width:100%" /></el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="随机间隔最小(ms)"><el-input-number v-model="form.request_interval_min_ms" :min="0" :max="10000" style="width:100%" /></el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="随机间隔最大(ms)"><el-input-number v-model="form.request_interval_max_ms" :min="0" :max="10000" style="width:100%" /></el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="检测批次间隔(分)"><el-input-number v-model="form.test_interval_minutes" :min="1" :max="1440" style="width:100%" /></el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="连续失败阈值"><el-input-number v-model="form.fail_threshold" :min="1" :max="20" style="width:100%" /></el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="历史保留(天)"><el-input-number v-model="form.history_days" :min="7" :max="365" style="width:100%" /></el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="相似节点分散粒度">
              <el-select v-model="form.dispersion_mask" style="width:220px">
                <el-option label="相同 IP（/32）" :value="32" />
                <el-option label="相同 /24（默认）" :value="24" />
                <el-option label="相同 /16" :value="16" />
                <el-option label="相同 /8" :value="8" />
              </el-select>
            </el-form-item>
          </el-form>
        </div>

        <div class="np-card" style="padding:20px;margin-top:16px">
          <div class="np-section-title">系统日志</div>
          <div class="np-log" style="max-height:320px;overflow:auto">
            <div v-for="log in logs" :key="log.id">
              <span class="np-muted">{{ formatTs(log.ts) }}</span>
              <span :class="`lv-${log.level}`"> [{{ log.level }}]</span>
              <span class="np-muted">{{ log.source }}</span>
              {{ log.message }}
            </div>
            <div v-if="!logs.length" class="np-empty">暂无日志</div>
          </div>
        </div>
      </el-col>

      <el-col :md="10" :xs="24">
        <div class="np-card" style="padding:20px">
          <div class="np-section-title">运行信息</div>
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="版本">
              {{ system.version }} ·
              <a href="https://github.com/laoXuHome/NodePoolME" target="_blank" rel="noopener">GitHub 仓库 ↗</a>
            </el-descriptions-item>
            <el-descriptions-item label="检测引擎">{{ system.engine }}</el-descriptions-item>
            <el-descriptions-item label="调度器">{{ system.scheduler ? '运行中' : '已停止' }}</el-descriptions-item>
            <el-descriptions-item label="拉取 UA">
              <span class="np-mono">{{ system.fetch_ua }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="数据目录"><span class="np-mono">{{ system.data_dir }}</span></el-descriptions-item>
            <el-descriptions-item label="数据库大小">{{ prettyBytes(system.db_size) }}</el-descriptions-item>
            <el-descriptions-item label="服务器时间">{{ system.time }}</el-descriptions-item>
          </el-descriptions>
          <div v-if="system.engine_error" class="np-muted" style="margin-top:10px">
            引擎降级原因：{{ system.engine_error }}
          </div>
        </div>

        <div class="np-card" style="padding:20px;margin-top:16px">
          <div class="np-section-title">备份与恢复</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <el-button :loading="backupBusy" @click="backup">创建备份</el-button>
            <el-button @click="downloadBackup">下载数据库</el-button>
          </div>
          <el-divider />
          <div class="np-muted" style="margin-bottom:10px">
            恢复会校验 SQLite 结构，覆盖前自动保存一份旧库到 backup 目录，并重建订阅。
          </div>
          <input type="file" accept=".db,.sqlite,.sqlite3" @change="onFile" />
          <div style="margin-top:12px">
            <el-button type="warning" :loading="uploading" @click="restore">上传并恢复</el-button>
          </div>
        </div>

        <div class="np-card" style="padding:20px;margin-top:16px">
          <div class="np-section-title">修改管理密码</div>
          <el-form label-width="90px">
            <el-form-item label="当前密码"><el-input v-model="password.old_password" type="password" show-password /></el-form-item>
            <el-form-item label="新密码"><el-input v-model="password.new_password" type="password" show-password /></el-form-item>
          </el-form>
          <el-button type="primary" @click="changePassword">更新密码</el-button>
        </div>
      </el-col>
    </el-row>
  </div>
</template>
