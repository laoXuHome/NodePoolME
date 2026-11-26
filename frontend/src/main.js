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
  { path: '/dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue'),
    meta: { title: '仪表盘', eyebrow: 'OPERATIONS', side: 'LIVE PULSE',
      desc: '机场拉取、节点缓存与检测批次总览。检测只读取本地缓存，不会触发机场重新拉取。' } },
  { path: '/airports', name: 'airports', component: () => import('./views/Airports.vue'),
    meta: { title: '机场管理', eyebrow: 'SOURCES', side: 'SUBSCRIPTION IN',
      desc: '每个机场独立拉取、独立周期；失败保留旧缓存，完整解析成功后才事务性更新。拉取统一使用固定 UA，避免触发机场风控。' } },
  { path: '/nodes', name: 'nodes', component: () => import('./views/Nodes.vue'),
    meta: { title: '缓存池', eyebrow: 'NODE POOL', side: 'CACHE LAYER',
      desc: '查看机场节点进入缓存后的真实状态：可用性、延迟、标签、评分与下发资格，均以最近一次检测结果为准。' } },
  { path: '/groups', name: 'groups', component: () => import('./views/Groups.vue'),
    meta: { title: '检测标签', eyebrow: 'DETECTION', side: 'TWO-STAGE TEST',
      desc: '每个标签对应一个检测目标与延迟上限，超时由系统设置统一控制，所有启用标签随全局检测一起执行。标签命中只影响命名，是否下发由延迟上限决定。' } },
  { path: '/subscription', name: 'subscription', component: () => import('./views/Subscription.vue'),
    meta: { title: '订阅输出', eyebrow: 'DELIVERY', side: 'CLIENT FORMATS',
      desc: '只生成一个统一订阅 /sub/all，Token 访问，不暴露机场原始 URL。输出层可做关键词过滤与测速标签排除，按 Clash、sing-box、Base64、Loon 四种格式输出。' } },
  { path: '/settings', name: 'settings', component: () => import('./views/Settings.vue'),
    meta: { title: '系统设置', eyebrow: 'SYSTEM', side: 'POLICY & LIMITS',
      desc: '集中配置检测并发、超时、发布策略与下发门槛、历史保留和备份。这里决定节点池的运行节奏。' } }
]

const router = createRouter({ history: createWebHashHistory(), routes })

const app = createApp(App)
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, comp)
}
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
