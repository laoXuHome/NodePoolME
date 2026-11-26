<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: Object, default: null },
  runtime: { type: Object, default: () => ({}) },
  version: { type: String, default: '' },
})

const airports = computed(() => props.data?.airport_list || [])
const airportTotal = computed(() => props.data?.fetch?.total || airports.value.length)
const airportOk = computed(() => props.data?.fetch?.ok || 0)
const enabledAirports = computed(() => airports.value.filter((a) => a.enabled).length)
const nodesTotal = computed(() => Number(props.data?.nodes_total || 0))
const nodesUnknown = computed(() => Number(props.data?.nodes_unknown || 0))
const published = computed(() => airports.value.reduce((sum, a) => sum + Number(a.published_count || 0), 0))
const publishRate = computed(() => {
  const total = nodesTotal.value || 1
  return `${Math.round((published.value / total) * 100)}%`
})
const fetchState = computed(() => {
  const f = props.data?.fetch
  if (!f) return '等待数据'
  if (f.fetching) return `拉取中 ${f.fetching}`
  if (f.error) return `${f.error} 个失败`
  if (!f.total) return '未配置机场'
  return '订阅同步正常'
})
const fetchTone = computed(() => {
  const f = props.data?.fetch
  if (!f || f.fetching) return 'warn'
  if (f.error) return 'bad'
  if (!f.total) return 'muted'
  return 'good'
})
const running = computed(() => !!props.runtime?.running)
const runState = computed(() => {
  const r = props.runtime
  if (!r?.running) return '调度空闲'
  if (r.stop_requested) return '正在停止'
  if (r.paused) return '已暂停'
  return '实时检测中'
})
const latencyPct = computed(() => Math.round(props.runtime?.latency_pct || 0))
const tagPct = computed(() => Math.round(props.runtime?.tag_pct || 0))
const topAirport = computed(() => {
  const rows = airports.value.filter((a) => a.enabled)
  return [...rows].sort((a, b) => Number(b.score ?? -1) - Number(a.score ?? -1))[0] || null
})
const topScore = computed(() => topAirport.value?.score ?? '—')
const topName = computed(() => topAirport.value?.name || '暂无可用评分')
const maturity = computed(() => topAirport.value?.data_status_text || '待积累')
const scoreWindow = computed(() => props.data?.score_window_days || 7)

const nodes = computed(() => [
  {
    key: 'airports', step: '01', title: '机场接入', icon: 'OfficeBuilding',
    value: `${enabledAirports.value} / ${airportTotal.value}`,
    unit: '启用机场',
    sub: `${airportOk.value} 个拉取正常 · ${fetchState.value}`,
    tone: fetchTone.value, x: 12, y: 24,
  },
  {
    key: 'cache', step: '02', title: '全量缓存', icon: 'Files',
    value: String(nodesTotal.value),
    unit: '缓存节点',
    sub: `${nodesUnknown.value} 个待检测 · 不改写原始订阅`,
    tone: 'blue', x: 50, y: 10,
  },
  {
    key: 'detect', step: '03', title: '双阶段检测', icon: 'Aim',
    value: `${latencyPct.value}%`,
    unit: '延迟进度',
    sub: `标签检测 ${tagPct.value}% · ${runState.value}`,
    tone: running.value ? 'cyan' : 'muted', x: 88, y: 24,
  },
  {
    key: 'score', step: '04', title: '评分与成熟度', icon: 'DataAnalysis',
    value: String(topScore.value),
    unit: '最高展示分',
    sub: `${topName.value} · ${maturity.value} · ${scoreWindow.value} 天窗口`,
    tone: 'violet', x: 88, y: 74,
  },
  {
    key: 'output', step: '05', title: '统一订阅发布', icon: 'Link',
    value: String(published.value),
    unit: '下发节点',
    sub: `${publishRate.value} 下发率 · 原子替换`,
    tone: 'green', x: 50, y: 90,
  },
  {
    key: 'clients', step: '06', title: '客户端消费', icon: 'Monitor',
    value: '4',
    unit: '种输出格式',
    sub: 'Clash · sing-box · Base64 · Loon',
    tone: 'amber', x: 12, y: 74,
  },
])

function point(node) {
  return { x: node.x * 10, y: node.y * 6 }
}

const paths = computed(() => nodes.value.map((node) => {
  const p = point(node)
  const dx = p.x - 500
  const dy = p.y - 300
  const c1x = 500 + dx * 0.36
  const c1y = 300 + dy * 0.08
  const c2x = p.x - dx * 0.22
  const c2y = p.y - dy * 0.18
  return {
    key: node.key,
    d: `M 500 300 C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p.x} ${p.y}`,
    tone: node.tone,
  }
}))
</script>

<template>
  <section class="opsmap">
    <div class="opsmap-glow" aria-hidden="true"></div>

    <div class="opsmap-head">
      <div class="opsmap-copy">
        <span class="opsmap-kicker"><i></i> LIVE OPERATIONS MAP</span>
        <h1>节点池<em>运营全景</em></h1>
        <p>机场接入 → 全量缓存 → 双阶段检测 → 评分成熟度 → 统一发布 → 客户端消费。</p>
      </div>
      <div class="opsmap-state">
        <span class="opsmap-live" :class="{ 'is-running': running }"><i></i>{{ runState }}</span>
        <span class="opsmap-version" v-if="version">v{{ version }}</span>
      </div>
    </div>

    <div class="opsmap-metrics">
      <div><span>机场在线</span><strong>{{ airportOk }}<small>/{{ airportTotal }}</small></strong></div>
      <div><span>缓存节点</span><strong>{{ nodesTotal }}</strong></div>
      <div><span>本次下发</span><strong>{{ published }}</strong></div>
      <div><span>最高评分</span><strong>{{ topScore }}</strong></div>
    </div>

    <div class="opsmap-canvas" aria-label="节点池运营流程思维导图">
      <svg class="opsmap-lines" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="opsLineBlue" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#4fd9ff" stop-opacity=".14" />
            <stop offset="55%" stop-color="#66f0c4" stop-opacity=".58" />
            <stop offset="100%" stop-color="#9b7cff" stop-opacity=".92" />
          </linearGradient>
          <linearGradient id="opsLineCyan" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0%" stop-color="#2ee6a8" stop-opacity=".14" />
            <stop offset="60%" stop-color="#2ee6a8" stop-opacity=".66" />
            <stop offset="100%" stop-color="#7c9cff" stop-opacity=".92" />
          </linearGradient>
          <radialGradient id="opsCoreGlow" cx=".5" cy=".5" r=".5">
            <stop offset="0%" stop-color="#2ee6a8" stop-opacity=".34" />
            <stop offset="60%" stop-color="#7c3aed" stop-opacity=".14" />
            <stop offset="100%" stop-color="#7c3aed" stop-opacity="0" />
          </radialGradient>
          <filter id="opsBlur"><feGaussianBlur stdDeviation="4" /></filter>
        </defs>
        <circle cx="500" cy="300" r="170" fill="url(#opsCoreGlow)" filter="url(#opsBlur)" />
        <path v-for="p in paths" :key="p.key" class="opsmap-line" :class="`tone-${p.tone}`" :d="p.d" />
        <circle v-for="p in paths" :key="`dot-${p.key}`" class="opsmap-spark" r="3.2" fill="#bde7ff">
          <animateMotion :dur="`${4 + (Number(p.key.length) % 3)}s`" repeatCount="indefinite" :path="p.d" />
        </circle>
      </svg>

      <div class="ops-core">
        <div class="ops-core-ring"></div>
        <span class="ops-core-orb"><i></i></span>
        <div class="ops-core-word"><b>NodePool</b><em>ME</em></div>
        <span class="ops-core-label">调度核心</span>
        <small>{{ airportTotal }} 机场 → {{ nodesTotal }} 节点</small>
      </div>

      <article v-for="node in nodes" :key="node.key" class="ops-node" :class="`tone-${node.tone}`"
               :style="{ '--ox': `${node.x}%`, '--oy': `${node.y}%` }">
        <div class="ops-node-top">
          <span class="ops-node-num">{{ node.step }}</span>
          <el-icon class="ops-node-icon"><component :is="node.icon" /></el-icon>
        </div>
        <b class="ops-node-title">{{ node.title }}</b>
        <div class="ops-node-value">{{ node.value }}<small>{{ node.unit }}</small></div>
        <span class="ops-node-sub">{{ node.sub }}</span>
      </article>
    </div>

    <div class="opsmap-mobile">
      <div v-for="node in nodes" :key="`m-${node.key}`" class="ops-mobile-row" :class="`tone-${node.tone}`">
        <span class="ops-mobile-num">{{ node.step }}</span>
        <span class="ops-mobile-icon"><el-icon><component :is="node.icon" /></el-icon></span>
        <div class="ops-mobile-body">
          <b>{{ node.title }}</b>
          <small>{{ node.sub }}</small>
        </div>
        <div class="ops-mobile-val">{{ node.value }}<small>{{ node.unit }}</small></div>
      </div>
    </div>

    <footer class="opsmap-foot">
      <span>六段闭环</span>
      <i>机场接入</i><i>全量缓存</i><i>双阶段检测</i><i>评分成熟度</i><i>统一发布</i><i>客户端消费</i>
      <span class="opsmap-foot-note">检测只读本地缓存，不触发机场重新拉取</span>
    </footer>
  </section>
</template>

<style scoped>
.opsmap {
  position: relative;
  overflow: hidden;
  margin: 4px 0 18px;
  padding: 28px 30px 20px;
  border-radius: 30px;
  color: #eaf2ff;
  background:
    radial-gradient(720px 430px at 14% 0%, rgba(46, 230, 168, .16), transparent 64%),
    radial-gradient(620px 430px at 92% 16%, rgba(124, 58, 237, .22), transparent 66%),
    radial-gradient(720px 460px at 50% 112%, rgba(45, 120, 255, .13), transparent 68%),
    linear-gradient(135deg, #07111a 0%, #0a1622 52%, #10132a 100%);
  border: 1px solid rgba(120, 190, 175, .19);
  box-shadow: 0 30px 80px rgba(11, 29, 61, .28), inset 0 1px 0 rgba(255, 255, 255, .10);
}
.opsmap::before {
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: .42;
  background-image:
    linear-gradient(rgba(120, 200, 180, .06) 1px, transparent 1px),
    linear-gradient(90deg, rgba(120, 200, 180, .06) 1px, transparent 1px);
  background-size: 34px 34px;
  mask-image: radial-gradient(ellipse at 50% 45%, #000 5%, transparent 78%);
}
.opsmap-glow {
  position: absolute;
  width: 520px;
  height: 520px;
  right: -120px;
  top: -230px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(124, 58, 237, .20), transparent 68%);
  pointer-events: none;
}
.opsmap-head { position: relative; z-index: 1; display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.opsmap-copy { max-width: 640px; }
.opsmap-kicker {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 10.5px; font-weight: 700; letter-spacing: .18em;
  color: rgba(191, 219, 254, .78);
}
.opsmap-kicker i { width: 18px; height: 1px; background: #2ee6a8; box-shadow: 0 0 8px #2ee6a8; }
.opsmap-copy h1 {
  margin: 12px 0 10px;
  font-size: clamp(30px, 3.2vw, 46px);
  line-height: 1.12;
  letter-spacing: -.045em;
  font-weight: 760;
  color: #f4f8ff;
}
.opsmap-copy h1 em {
  font-style: normal;
  background: linear-gradient(90deg, #6ff2c8, #d4f8ff 48%, #c4b5fd);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.opsmap-copy p {
  margin: 0;
  max-width: 560px;
  font-size: 13.5px;
  line-height: 1.75;
  color: rgba(203, 219, 244, .78);
}
.opsmap-state { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; flex: none; }
.opsmap-live {
  display: inline-flex; align-items: center; gap: 7px;
  height: 27px; padding: 0 12px;
  border: 1px solid rgba(160, 205, 255, .22);
  border-radius: 999px;
  background: rgba(110, 168, 255, .11);
  color: #cfe4ff; font-size: 11px; font-weight: 600;
  backdrop-filter: blur(12px);
}
.opsmap-live i { width: 7px; height: 7px; border-radius: 50%; background: #67e8f9; box-shadow: 0 0 0 4px rgba(103, 232, 249, .13); }
.opsmap-live.is-running i { animation: ops-pulse 1.6s ease-in-out infinite; background: #86efac; box-shadow: 0 0 0 4px rgba(134, 239, 172, .16); }
.opsmap-version { font-size: 10px; letter-spacing: .08em; color: rgba(191, 219, 254, .50); }
.opsmap-metrics { position: relative; z-index: 1; display: flex; flex-wrap: wrap; gap: 30px; margin-top: 22px; padding-top: 16px; border-top: 1px solid rgba(168, 204, 255, .13); }
.opsmap-metrics > div { display: flex; align-items: baseline; gap: 8px; }
.opsmap-metrics span { color: rgba(191, 219, 254, .60); font-size: 10.5px; letter-spacing: .04em; }
.opsmap-metrics strong { font-size: 24px; line-height: 1; letter-spacing: -.04em; font-weight: 700; color: #f2f7ff; font-variant-numeric: tabular-nums; }
.opsmap-metrics small { font-size: 10px; color: rgba(191, 219, 254, .52); font-weight: 500; }

.opsmap-canvas { position: relative; z-index: 1; height: 520px; margin: 2px -10px -8px; }
.opsmap-lines { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.opsmap-line { fill: none; stroke-width: 2.2; stroke-linecap: round; stroke-dasharray: 5 7; opacity: .62; }
.opsmap-line.tone-good, .opsmap-line.tone-green, .opsmap-line.tone-amber { stroke: url(#opsLineCyan); }
.opsmap-line.tone-blue, .opsmap-line.tone-violet, .opsmap-line.tone-cyan { stroke: url(#opsLineBlue); }
.opsmap-line.tone-warn, .opsmap-line.tone-bad { stroke: rgba(251, 191, 36, .66); }
.opsmap-line.tone-muted { stroke: rgba(148, 163, 184, .38); }
.opsmap-spark { opacity: .66; filter: drop-shadow(0 0 6px rgba(147, 197, 253, .95)); }
.ops-core {
  position: absolute; left: 50%; top: 50%; width: 190px; height: 190px;
  transform: translate(-50%, -50%);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 24%, rgba(255, 255, 255, .20), transparent 44%), linear-gradient(145deg, rgba(18, 82, 82, .62), rgba(24, 26, 66, .78));
  border: 1px solid rgba(96, 240, 194, .34);
  box-shadow: 0 0 0 9px rgba(46, 230, 168, .05), 0 0 52px rgba(46, 230, 168, .18), inset 0 1px 0 rgba(255, 255, 255, .16);
  backdrop-filter: blur(14px);
}
.ops-core-ring { position: absolute; inset: -17px; border-radius: 50%; border: 1px dashed rgba(96, 240, 194, .28); animation: ops-spin 26s linear infinite; }
.ops-core-orb { position: absolute; top: 30px; width: 27px; height: 27px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #fff, #7dffd9 38%, #19c99a 70%); box-shadow: 0 0 22px rgba(46, 230, 168, .65); }
.ops-core-orb i { position: absolute; inset: 9px; border-radius: 50%; background: #dff8ff; box-shadow: 0 0 14px #fff; }
.ops-core-word { display: flex; align-items: baseline; margin-top: 16px; letter-spacing: -.045em; font-weight: 760; font-size: 21px; }
.ops-core-word em { font-style: normal; font-size: 22.5px; color: #61f0c4; letter-spacing: -.01em; }
.ops-core-label { margin-top: 4px; font-size: 9.5px; letter-spacing: .16em; color: rgba(191, 219, 254, .67); }
.ops-core small { margin-top: 8px; font-size: 10px; color: rgba(191, 219, 254, .82); font-variant-numeric: tabular-nums; }
.ops-node {
  position: absolute; left: var(--ox); top: var(--oy); width: 190px;
  transform: translate(-50%, -50%);
  padding: 12px 13px 11px;
  border-radius: 16px;
  background: linear-gradient(145deg, rgba(18, 42, 52, .74), rgba(15, 25, 48, .68));
  border: 1px solid rgba(120, 200, 180, .16);
  box-shadow: 0 12px 30px rgba(1, 10, 28, .24), inset 0 1px 0 rgba(255, 255, 255, .07);
  backdrop-filter: blur(12px);
}
.ops-node::after { content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none; background: linear-gradient(120deg, rgba(255, 255, 255, .10), transparent 42%); }
.ops-node.tone-good, .ops-node.tone-green { border-color: rgba(134, 239, 172, .32); }
.ops-node.tone-warn, .ops-node.tone-amber { border-color: rgba(251, 191, 36, .34); }
.ops-node.tone-bad { border-color: rgba(251, 113, 133, .38); }
.ops-node.tone-violet { border-color: rgba(196, 181, 253, .34); }
.ops-node.tone-cyan { border-color: rgba(103, 232, 249, .34); }
.ops-node-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.ops-node-num { font-size: 9px; letter-spacing: .12em; color: rgba(191, 219, 254, .52); font-variant-numeric: tabular-nums; }
.ops-node-icon { width: 23px; height: 23px; border-radius: 7px; display: inline-flex; align-items: center; justify-content: center; color: #85f2cf; background: rgba(46, 230, 168, .09); border: 1px solid rgba(96, 240, 194, .16); font-size: 14px; }
.ops-node-title { display: block; font-size: 11.5px; font-weight: 650; color: #dfeeff; letter-spacing: .01em; }
.ops-node-value { display: flex; align-items: baseline; gap: 5px; margin: 5px 0 6px; font-size: 27px; line-height: 1; letter-spacing: -.045em; font-weight: 750; color: #fff; font-variant-numeric: tabular-nums; }
.ops-node-value small { font-size: 9px; color: rgba(191, 219, 254, .62); font-weight: 500; letter-spacing: 0; white-space: nowrap; }
.ops-node-sub { display: block; min-height: 27px; font-size: 9.5px; line-height: 1.45; color: rgba(191, 219, 254, .68); overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }

.opsmap-mobile { display: none; position: relative; z-index: 1; margin: 14px 0 6px; }
.opsmap-foot {
  position: relative; z-index: 1; display: flex; align-items: center; flex-wrap: wrap; gap: 7px;
  margin-top: 4px; padding-top: 13px; border-top: 1px solid rgba(168, 204, 255, .13);
  font-size: 10px; color: rgba(191, 219, 254, .52);
}
.opsmap-foot > span:first-child { margin-right: 5px; color: rgba(191, 219, 254, .85); font-weight: 650; }
.opsmap-foot i { font-style: normal; padding: 3px 8px; border-radius: 999px; background: rgba(46, 230, 168, .06); border: 1px solid rgba(96, 240, 194, .14); color: rgba(190, 235, 225, .68); }
.opsmap-foot-note { margin-left: auto; color: rgba(191, 219, 254, .40); }
@keyframes ops-pulse { 0%, 100% { opacity: .7; transform: scale(.92); } 50% { opacity: 1; transform: scale(1.12); } }
@keyframes ops-spin { to { transform: rotate(360deg); } }

@media (max-width: 1100px) {
  .opsmap { padding: 24px 22px 16px; }
  .opsmap-canvas { display: none; }
  .opsmap-mobile { display: flex; flex-direction: column; gap: 0; }
  .ops-mobile-row {
    position: relative; display: flex; align-items: center; gap: 10px; min-height: 62px;
    padding: 10px 12px; border-left: 2px solid rgba(147, 197, 253, .20);
    border-bottom: 1px solid rgba(147, 197, 253, .08);
  }
  .ops-mobile-row:last-child { border-bottom: 0; }
  .ops-mobile-num { width: 23px; flex: none; font-size: 9px; color: rgba(191, 219, 254, .52); font-variant-numeric: tabular-nums; }
  .ops-mobile-icon { width: 26px; height: 26px; flex: none; display: flex; align-items: center; justify-content: center; border-radius: 9px; color: #bde7ff; background: rgba(96, 165, 250, .14); border: 1px solid rgba(147, 197, 253, .16); font-size: 15px; }
  .ops-mobile-body { flex: 1; min-width: 0; }
  .ops-mobile-body b { display: block; font-size: 12px; color: #e5f0ff; }
  .ops-mobile-body small { display: block; margin-top: 2px; font-size: 9.5px; line-height: 1.4; color: rgba(191, 219, 254, .60); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ops-mobile-val { flex: none; text-align: right; font-size: 21px; line-height: 1; letter-spacing: -.04em; font-weight: 740; color: #fff; font-variant-numeric: tabular-nums; }
  .ops-mobile-val small { display: block; margin-top: 4px; font-size: 8.5px; font-weight: 500; color: rgba(191, 219, 254, .56); letter-spacing: 0; }
  .opsmap-head { flex-direction: column; gap: 14px; }
  .opsmap-state { flex-direction: row; align-items: center; }
  .opsmap-copy h1 { font-size: 29px; }
  .opsmap-copy p { font-size: 12.5px; }
  .opsmap-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
  .opsmap-metrics > div { display: block; }
  .opsmap-metrics span { display: block; margin-bottom: 4px; font-size: 9px; }
  .opsmap-metrics strong { font-size: 20px; }
  .opsmap-foot { margin-top: 10px; }
  .opsmap-foot-note { display: none; }
}
@media (max-width: 640px) {
  .opsmap { margin: 0 0 14px; padding: 22px 18px 14px; border-radius: 22px; }
  .opsmap-copy h1 { font-size: 26px; }
  .opsmap-metrics > div:nth-child(4) { display: none; }
  .opsmap-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .opsmap-foot i { font-size: 9px; padding: 2px 6px; }
}
@media (prefers-reduced-motion: reduce) {
  .ops-core-ring, .opsmap-spark, .opsmap-live.is-running i { animation: none; }
  .opsmap-spark { display: none; }
}
</style>
