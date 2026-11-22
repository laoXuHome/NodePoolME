import { reactive } from 'vue'

const STATUS_TEXT = {
  400: '请求参数有误，请检查输入',
  401: '请先登录',
  403: '没有权限执行此操作',
  404: '请求的内容不存在',
  409: '操作冲突，请刷新后重试',
  405: '请求方式不允许',
  413: '上传的文件过大',
  422: '提交内容不符合要求，请检查输入',
  429: '请求过于频繁，请稍后再试',
  500: '服务器内部错误',
  502: '服务暂时不可用，请稍后重试',
  503: '服务暂时不可用，请稍后重试',
  504: '网关超时，请稍后重试'
}

const DEFAULT_ENGLISH_ERROR = {
  'bad request': 400,
  unauthorized: 401,
  forbidden: 403,
  'not found': 404,
  'method not allowed': 405,
  'request entity too large': 413,
  'unprocessable entity': 422,
  'too many requests': 429,
  'internal server error': 500,
  'bad gateway': 502,
  'service unavailable': 503,
  'gateway timeout': 504
}

function responseMessage(status, data) {
  const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : data
  if (typeof detail === 'string' && detail.trim()) {
    const text = detail.trim()
    if (text.startsWith('<')) return STATUS_TEXT[status] || '请求失败，请稍后重试'
    const mapped = DEFAULT_ENGLISH_ERROR[text.toLowerCase()]
    if (mapped) return STATUS_TEXT[mapped] || STATUS_TEXT[status]
    return text
  }
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message
  }
  if (Array.isArray(detail)) return STATUS_TEXT[422] || '提交内容不符合要求，请检查输入'
  return STATUS_TEXT[status] || `请求失败（HTTP ${status}）`
}


async function request(method, url, body) {
  const options = { method, credentials: 'same-origin', headers: {} }
  if (body !== undefined) {
    if (body instanceof FormData) {
      options.body = body
    } else {
      options.headers['Content-Type'] = 'application/json'
      options.body = JSON.stringify(body)
    }
  }
  let res
  try {
    res = await fetch(url, options)
  } catch {
    throw new Error('网络请求失败，请检查网络连接或服务是否正在运行')
  }
  if (res.status === 401 && !url.includes('/auth/login')) {
    window.location.hash = '#/login'
    throw new Error('登录已失效，请重新登录')
  }
  const text = await res.text()
  const data = text ? (() => { try { return JSON.parse(text) } catch { return text } })() : null
  if (!res.ok) {
    throw new Error(responseMessage(res.status, data))
  }
  return data
}

export const state = reactive({ loggedIn: false, version: '' })

export const api = {
  state,
  get: (url) => request('GET', url),
  post: (url, body) => request('POST', url, body),
  patch: (url, body) => request('PATCH', url, body),
  del: (url) => request('DELETE', url),
  upload: (url, file) => {
    const form = new FormData()
    form.append('file', file)
    return request('POST', url, form)
  }
}

export async function login(password) {
  await api.post('/api/auth/login', { password })
  state.loggedIn = true
  // App.vue skips checkAuth() on the login route, so load the real version here too.
  try {
    const me = await api.get('/api/auth/me')
    if (me && me.version) state.version = me.version
  } catch { /* version stays at fallback until next reload */ }
}

export async function logout() {
  await api.post('/api/auth/logout')
  state.loggedIn = false
}

export async function checkAuth() {
  try {
    const me = await api.get('/api/auth/me')
    state.loggedIn = true
    if (me && me.version) state.version = me.version
    return true
  } catch {
    state.loggedIn = false
    return false
  }
}

export function logLevelLabel(level) {
  const map = { INFO: '信息', WARN: '警告', ERROR: '错误', DEBUG: '调试' }
  return map[String(level || '').toUpperCase()] || level
}

export function logSourceLabel(source) {
  const map = {
    auth: '认证', airport: '机场', fetch: '拉取', publish: '下发', group: '检测组',
    subscription: '订阅', settings: '设置', backup: '备份', restore: '恢复',
    system: '系统', api: '接口', latency: '延迟', test: '检测',
    maintenance: '维护', scheduler: '调度', tester: '测试内核'
  }
  return map[String(source || '').trim()] || source
}

export function formatTs(value) {
  if (!value) return '—'
  return String(value).replace('T', ' ').slice(0, 19)
}

export function prettyBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let n = bytes
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(1)} ${units[i]}`
}
