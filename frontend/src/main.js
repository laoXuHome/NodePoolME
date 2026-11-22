import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import './styles.css'

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
  { path: '/dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue'), meta: { title: '仪表盘' } },
  { path: '/airports', name: 'airports', component: () => import('./views/Airports.vue'), meta: { title: '机场管理' } },
  { path: '/nodes', name: 'nodes', component: () => import('./views/Nodes.vue'), meta: { title: '缓存池' } },
  { path: '/groups', name: 'groups', component: () => import('./views/Groups.vue'), meta: { title: '检测管理' } },
  { path: '/subscription', name: 'subscription', component: () => import('./views/Subscription.vue'), meta: { title: '订阅输出' } },
  { path: '/settings', name: 'settings', component: () => import('./views/Settings.vue'), meta: { title: '系统设置' } }
]

const router = createRouter({ history: createWebHashHistory(), routes })

const app = createApp(App)
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, comp)
}
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
