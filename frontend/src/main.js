import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import './styles.css'
import './console.css'

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
  { path: '/dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue'),
    meta: { title: '控制总览', eyebrow: 'OPERATIONS', side: 'LIVE PULSE',
      desc: '机场订阅、节点资产与检测批次的总览。检测只读取本地缓存，不会触发机场重新拉取。' } },
  { path: '/airports', name: 'airports', component: () => import('./views/Airports.vue'),
    meta: { title: '机场接入', eyebrow: 'SOURCES', side: 'SUBSCRIPTION IN',
      desc: '每个机场独立拉取、独立周期；失败保留旧缓存，完整解析成功后才事务性更新。拉取统一使用固定 UA，避免触发机场风控。' } },
  { path: '/nodes', name: 'nodes', component: () => import('./views/Nodes.vue'),
    meta: { title: '节点池', eyebrow: 'NODE POOL', side: 'CACHE LAYER',
      desc: '查看机场节点进入缓存后的真实状态：可用性、延迟、标签、评分与下发资格，均以最近一次检测结果为准。' } },
  { path: '/nodepool', name: 'nodepool', component: () => import('./views/NodePoolP1.vue'),
    meta: { title: '三层节点池', eyebrow: 'INVENTORY', side: 'P1 LAYERS',
      desc: '原始池保存全部标准化节点；健康池与观察池共享同一条节点记录，只按基础健康与数据充分度进入管理层。' } },
  { path: '/health', name: 'health', component: () => import('./views/HealthP2.vue'),
    meta: { title: '基础健康', eyebrow: 'P2 PROBES', side: 'DNS · TCP · TLS · HTTPS',
      desc: '按目标独立执行 DNS → TCP 五次采样 → TLS → HTTPS；P80 取最近秩第 4 个值，Unknown 不计失败，连续 3 次有效失败才判不健康。' } },
  { path: '/business', name: 'business', component: () => import('./views/BusinessP3.vue'),
    meta: { title: '业务专项检测', eyebrow: 'P3 BUSINESS', side: 'GPT · YOUTUBE · TELEGRAM · GITHUB · GLOBAL',
      desc: '五类业务独立检测、独立资格；只走节点自身代理出口的真实 HTTPS 访问，Unknown 不计失败，连续 2 次成功获得资格。' } },
  { path: '/quality', name: 'quality', component: () => import('./views/QualityP4.vue'),
    meta: { title: '质量评价与下载速度', eyebrow: 'P4 EVALUATION', side: '7-DAY QUALITY · SPEED GRADE',
      desc: '七天连续滚动质量、独立数据成熟度与独立下载速度评价；只读 P1/P2/P3 证据，不生成优选名单。' } },
  { path: '/competition', name: 'competition', component: () => import('./views/CompetitionP5.vue'),
    meta: { title: '智能节点竞争', eyebrow: 'P5 COMPETITION', side: 'STANDARD · GPT · YOUTUBE · TELEGRAM · GITHUB · GLOBAL',
      desc: '标准综合与五类业务各自独立竞争；新节点先观察，成熟优选按连续证据保留，只有达到分差并连续确认才替换。' } },
  { path: '/final', name: 'final', component: () => import('./views/FinalP6.vue'),
    meta: { title: '最终优选结果', eyebrow: 'P6.1 FINAL RESULTS', side: 'IMMUTABLE SNAPSHOT · SIX CATEGORIES',
      desc: '汇总 P5 六类当前优选节点，形成不可篡改的版本快照与新增/移除/保留差异；本批只做内部结果，不做关键词过滤、不生成订阅。' } },
  { path: '/final-filter', name: 'final-filter', component: () => import('./views/FinalFilterP621.vue'),
    meta: { title: '最终关键词过滤', eyebrow: 'P6.2.1 DELIVERY GATE', side: 'KEYWORDS · PREVIEW ONLY',
      desc: '关键词匹配节点名称、机场名称与服务器地址，只决定最终下发预览是否放行；命中节点仍保留在 P5 与 P6.1 内部结果，本批不生成、不发布订阅。' } },
  { path: '/unified-subscription', name: 'unified-subscription', component: () => import('./views/UnifiedSubscriptionP622.vue'),
    meta: { title: '统一订阅生成', eyebrow: 'P6.2.2 UNIFIED SUBSCRIPTION', side: 'ONE POOL · FOUR FORMATS',
      desc: 'P6.1 六类最终优选合并去重，经 P6.2.1 关键词过滤后生成一份统一节点集合；只提供后台预览与管理员下载，不开放正式客户端订阅入口。' } },
  { path: '/groups', name: 'groups', component: () => import('./views/Groups.vue'),
    meta: { title: '检测标签', eyebrow: 'DETECTION', side: 'TWO-STAGE TEST',
      desc: '每个标签对应一个检测目标与延迟上限，超时由系统设置统一控制，所有启用标签随全局检测一起执行。标签命中只影响命名，是否下发由延迟上限决定。' } },
  { path: '/subscription', name: 'subscription', component: () => import('./views/Subscription.vue'),
    meta: { title: '订阅分发', eyebrow: 'DELIVERY', side: 'CLIENT FORMATS',
      desc: '只生成一个统一订阅 /sub/all，Token 访问，不暴露机场原始 URL。分发层可做关键词过滤与测速标签排除，按 Clash、sing-box、Base64、Loon 四种格式输出。' } },
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
