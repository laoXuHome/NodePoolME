import { reactive } from 'vue'

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
  const res = await fetch(url, options)
  if (res.status === 401 && !url.includes('/auth/login')) {
    window.location.hash = '#/login'
    throw new Error('登录已失效，请重新登录')
  }
  const text = await res.text()
  const data = text ? (() => { try { return JSON.parse(text) } catch { return text } })() : null
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${res.status}`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
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
