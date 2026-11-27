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

const batch = computed(() => props.data?.batch || {})
const batchId = computed(() => props.runtime?.batch_id || batch.value.id || '—')
const processed = computed(() => Number(props.runtime?.done ?? batch.value.processed_count ?? 0))
const target = computed(() => Number(props.runtime?.total ?? batch.value.target_count ?? nodesTotal.value))
const progressPct = computed(() => target.value ? Math.min(100, Math.round(processed.value / target.value * 100)) : 0)
const durationText = computed(() => {
  const b = batch.value
  if (!b?.started_at) return '—'
  if (!b.finished_at) return running.value ? '进行中' : '—'
  const seconds = Math.max(0, Math.round((new Date(b.finished_at) - new Date(b.started_at)) / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
})
const currentText = computed(() => {
  const c = props.runtime?.current
  if (running.value && c?.node) {
    return `${c.airport || '当前机场'} · ${c.node}`
  }
  return target.value ? `${processed.value} / ${target.value} 项已处理` : '等待下一批次'
})
const liveNote = computed(() => running.value ? currentText.value : `批次 #${batchId.value} · ${durationText.value}`)

const topAirport = computed(() => {
  const rows = airports.value.filter((a) => a.enabled)
  return [...rows].sort((a, b) => Number(b.score ?? -1) - Number(a.score ?? -1))[0] || null
})
const topScore = computed(() => topAirport.value?.score ?? '—')
const topName = computed(() => topAirport.value?.name || '暂无可用评分')
const maturityValue = computed(() => Number(topAirport.value?.maturity_score || 0))
const maturityText = computed(() => topAirport.value?.data_status_text || '待积累')
const scoreWindow = computed(() => props.data?.score_window_days || 7)

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
const syncPct = computed(() => airportTotal.value ? Math.round(airportOk.value / airportTotal.value * 100) : 0)
const detectedPct = computed(() => nodesTotal.value ? Math.round((nodesTotal.value - nodesUnknown.value) / nodesTotal.value * 100) : 0)
const publishPct = computed(() => nodesTotal.value ? Math.round(published.value / nodesTotal.value * 100) : 0)

const nodes = computed(() => [
  {
    key: 'airports', step: '01', title: '机场接入', icon: 'OfficeBuilding',
    value: `${enabledAirports.value} / ${airportTotal.value}`, unit: '启用机场',
    sub: `${airportOk.value} 个拉取正常 · ${fetchState.value}`,
    flag: fetchTone.value === 'bad' ? '同步异常' : fetchTone.value === 'warn' ? '同步中' : '已同步',
    color: fetchTone.value === 'bad' ? '#c8755c' : fetchTone.value === 'warn' ? '#c08a12' : '#3f665c',
    tone: fetchTone.value, x: 12, y: 42,
    meter: [{ label: '同步', value: syncPct.value, color: '#3f665c' }],
  },
  {
    key: 'cache', step: '02', title: '全量缓存', icon: 'Files',
    value: String(nodesTotal.value), unit: '缓存节点',
    sub: `${nodesUnknown.value} 个待检测 · 不改写原始订阅`,
    flag: nodesUnknown.value ? '待检测' : '已就绪',
    color: '#587d70', tone: nodesUnknown.value ? 'warn' : 'good', x: 50, y: 14,
    meter: [{ label: '已检测', value: detectedPct.value, color: '#587d70' }],
  },
  {
    key: 'detect', step: '03', title: '双阶段检测', icon: 'Aim',
    value: `${latencyPct.value}%`, unit: '延迟进度',
    sub: `标签检测 ${tagPct.value}% · ${runState.value}`,
    flag: running.value ? '检测中' : batch.value.status === 'complete' ? '已完成' : '待命',
    color: running.value ? '#c08a12' : '#3f665c', tone: running.value ? 'active' : 'good', x: 87, y: 42,
    active: running.value,
    meter: [
      { label: '延迟', value: latencyPct.value, color: '#3f665c' },
      { label: '标签', value: tagPct.value, color: '#d6a41d' },
    ],
  },
  {
    key: 'score', step: '04', title: '评分与成熟度', icon: 'DataAnalysis',
    value: String(topScore.value), unit: '最高展示分',
    sub: `${topName.value} · ${maturityText.value} · ${scoreWindow.value} 天窗口`,
    flag: topScore.value === '—' ? '待积累' : '已评分',
    color: '#8a6a2f', tone: topScore.value === '—' ? 'muted' : 'good', x: 74, y: 84,
    meter: [{ label: '成熟度', value: maturityValue.value, color: '#8a6a2f' }],
  },
  {
    key: 'output', step: '05', title: '统一订阅发布', icon: 'Link',
    value: String(published.value), unit: '下发节点',
    sub: `${publishPct.value}% 下发率 · 原子替换`,
    flag: published.value ? '已发布' : '待发布',
    color: '#2f6053', tone: published.value ? 'good' : 'muted', x: 26, y: 84,
    meter: [{ label: '下发率', value: publishPct.value, color: '#2f6053' }],
  },
])

const paths = computed(() => nodes.value.map((node) => {
  const p = { x: node.x * 10, y: node.y * 6 }
  const dx = p.x - 500
  const dy = p.y - 336
  const c1x = 500 + dx * 0.34
  const c1y = 336 + dy * 0.10
  const c2x = p.x - dx * 0.24
  const c2y = p.y - dy * 0.16
  return {
    key: node.key,
    d: `M 500 336 C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p.x} ${p.y}`,
    color: node.color,
    active: node.active,
  }
}))
</script>

<template>
  <section class="om" :class="{ 'is-running': running }" :style="{ '--core-progress': progressPct + '%' }">
    <div class="om-grid" aria-hidden="true"></div>
    <div class="om-aura is-mint" aria-hidden="true"></div>
    <div class="om-aura is-gold" aria-hidden="true"></div>

    <header class="om-head">
      <div class="om-copy">
        <span class="om-kicker"><i></i> NODE PIPELINE · LIVE</span>
        <h2>节点池<em>运营全景</em></h2>
        <p>机场接入 → 全量缓存 → 双阶段检测 → 评分成熟度 → 统一发布。</p>
      </div>
      <div class="om-headside">
        <span class="om-live" :class="{ 'is-running': running }"><i></i>{{ runState }}</span>
        <span v-if="version" class="om-version">v{{ version }}</span>
      </div>
    </header>

    <div class="om-telemetry">
      <div class="om-run">
        <span class="om-k">RUN STATE</span>
        <div class="om-run-main"><i :class="{ 'is-live': running }"></i><b>{{ runState }}</b></div>
        <small>{{ liveNote }}</small>
      </div>
      <div class="om-batch">
        <span class="om-k">BATCH</span>
        <b>#{{ batchId }}</b>
        <small>{{ durationText }} · {{ processed }} / {{ target }}</small>
      </div>
      <div class="om-progress">
        <div class="om-progress-head">
          <span class="om-k">PROGRESS</span>
          <b>{{ progressPct }}<small>%</small></b>
        </div>
        <div class="om-track"><i :style="{ width: progressPct + '%' }"></i></div>
        <small>{{ processed }} / {{ target }} 项</small>
      </div>
      <div class="om-vitals">
        <div><span>机场</span><b>{{ airportOk }}<small>/{{ airportTotal }}</small></b></div>
        <div><span>缓存</span><b>{{ nodesTotal }}</b></div>
        <div><span>下发</span><b>{{ published }}</b></div>
      </div>
    </div>

    <div class="om-canvas" aria-label="节点池五段运营思维导图">
      <svg class="om-lines" viewBox="0 0 1000 600" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <filter id="omSparkGlow"><feGaussianBlur stdDeviation="2.4" result="blur" /><feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
        </defs>
        <g v-for="p in paths" :key="`line-${p.key}`">
          <path class="om-link-base" :d="p.d" />
          <path class="om-link" :class="{ 'is-active': p.active }" :d="p.d" :style="{ stroke: p.color }" />
          <circle class="om-spark" r="3.1" :fill="p.color">
            <animateMotion :dur="`${4.2 + (p.key.length % 3) * .7}s`" :begin="`${(p.key.length % 4) * .35}s`" repeatCount="indefinite" :path="p.d" />
          </circle>
        </g>
      </svg>

      <div class="om-core">
        <div class="om-core-halo"></div>
        <div class="om-core-orbit is-outer"></div>
        <div class="om-core-orbit"></div>
        <div class="om-core-progress"></div>
        <div class="om-core-disc">
          <span class="om-core-kicker">CORE · 调度核心</span>
          <div class="om-core-word"><b>NodePool</b><em>ME</em></div>
          <span class="om-core-state" :class="{ 'is-live': running }"><i></i>{{ runState }}</span>
          <small>{{ airportTotal }} 机场 · {{ nodesTotal }} 节点</small>
        </div>
      </div>

      <article v-for="(node, idx) in nodes" :key="node.key" class="om-node"
               :class="[`tone-${node.tone}`, { 'is-active': node.active }]"
               :style="{ '--ox': node.x + '%', '--oy': node.y + '%', '--tone': node.color, '--i': idx }">
        <span class="om-node-accent" aria-hidden="true"></span>
        <div class="om-node-top">
          <span class="om-node-num">{{ node.step }}</span>
          <span class="om-node-flag"><i></i>{{ node.flag }}</span>
        </div>
        <div class="om-node-icon"><el-icon><component :is="node.icon" /></el-icon></div>
        <h3>{{ node.title }}</h3>
        <div class="om-node-value">{{ node.value }}<small>{{ node.unit }}</small></div>
        <p class="om-node-sub">{{ node.sub }}</p>
        <div class="om-node-meters" v-if="node.meter?.length">
          <div v-for="meter in node.meter" :key="meter.label" class="om-node-meter">
            <div class="om-node-meter-head"><span>{{ meter.label }}</span><b>{{ Math.round(meter.value) }}%</b></div>
            <div class="om-node-meter-track"><i :style="{ width: Math.max(0, Math.min(100, meter.value)) + '%', background: meter.color }"></i></div>
          </div>
        </div>
      </article>
    </div>

    <div class="om-mobile">
      <div v-for="(node, idx) in nodes" :key="`m-${node.key}`" class="om-mobile-row" :style="{ '--tone': node.color }">
        <span class="om-mobile-index">{{ node.step }}</span>
        <span class="om-mobile-icon"><el-icon><component :is="node.icon" /></el-icon></span>
        <div class="om-mobile-body">
          <b>{{ node.title }}</b>
          <small>{{ node.sub }}</small>
        </div>
        <div class="om-mobile-side">
          <strong>{{ node.value }}<small>{{ node.unit }}</small></strong>
          <em><i></i>{{ node.flag }}</em>
        </div>
      </div>
    </div>

    <footer class="om-foot">
      <span>五段闭环</span>
      <i>机场接入</i><i>全量缓存</i><i>双阶段检测</i><i>评分成熟度</i><i>统一发布</i>
      <span class="om-foot-note">检测只读本地缓存，不触发机场重新拉取</span>
    </footer>
  </section>
</template>

<style scoped>
.om {
  position: relative;
  overflow: hidden;
  margin: 18px 0;
  padding: 28px 30px 18px;
  border-radius: 28px;
  color: #233a33;
  background:
    radial-gradient(680px 390px at 8% 0%, rgba(63, 102, 92, .13), transparent 64%),
    radial-gradient(620px 380px at 94% 8%, rgba(214, 164, 29, .10), transparent 62%),
    linear-gradient(146deg, #fcfdfa 0%, #f2f6f0 58%, #f8f5eb 100%);
  border: 1px solid #dde7dd;
  box-shadow: 0 24px 56px rgba(29, 57, 47, .09), inset 0 1px 0 rgba(255, 255, 255, .90);
}
.om-grid {
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: .48;
  background-image: radial-gradient(rgba(63, 102, 92, .22) .8px, transparent .8px);
  background-size: 22px 22px;
  mask-image: radial-gradient(ellipse at 50% 44%, #000 8%, transparent 78%);
}
.om-aura { position: absolute; border-radius: 50%; pointer-events: none; filter: blur(4px); }
.om-aura.is-mint { width: 440px; height: 440px; left: -170px; top: -190px; background: radial-gradient(circle, rgba(63, 102, 92, .15), transparent 70%); }
.om-aura.is-gold { width: 360px; height: 360px; right: -120px; top: 60px; background: radial-gradient(circle, rgba(214, 164, 29, .10), transparent 70%); }

.om-head { position: relative; z-index: 2; display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.om-copy { max-width: 650px; }
.om-kicker { display: inline-flex; align-items: center; gap: 8px; font-size: 10px; font-weight: 750; letter-spacing: .18em; color: #6f8379; }
.om-kicker i { width: 20px; height: 1px; background: #3f665c; }
.om-copy h2 { margin: 11px 0 9px; font-size: clamp(30px, 3.2vw, 43px); line-height: 1.08; letter-spacing: -.048em; font-weight: 770; color: #203a32; }
.om-copy h2 em { position: relative; font-style: normal; color: #3f665c; }
.om-copy h2 em::after { content: ""; position: absolute; left: 0; right: 2px; bottom: 2px; height: 9px; background: rgba(214, 164, 29, .20); border-radius: 2px; z-index: -1; }
.om-copy p { margin: 0; max-width: 560px; font-size: 13px; line-height: 1.75; color: #667a70; }
.om-headside { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; flex: none; }
.om-live { display: inline-flex; align-items: center; gap: 7px; height: 28px; padding: 0 12px; border: 1px solid #dbe6dc; border-radius: 999px; background: rgba(255, 255, 255, .72); color: #52675e; font-size: 10.5px; font-weight: 650; box-shadow: 0 4px 14px rgba(35, 65, 53, .05); }
.om-live i { width: 7px; height: 7px; border-radius: 50%; background: #8ca199; box-shadow: 0 0 0 4px rgba(140, 161, 153, .12); }
.om-live.is-running { color: #84651c; border-color: #e8dbb2; background: rgba(255, 250, 236, .88); }
.om-live.is-running i { background: #d6a41d; box-shadow: 0 0 0 4px rgba(214, 164, 29, .15); animation: om-pulse 1.5s ease-in-out infinite; }
.om-version { font-size: 9.5px; letter-spacing: .10em; color: #93a49b; }

.om-telemetry {
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: 1.05fr .95fr 1.35fr auto;
  align-items: stretch;
  gap: 0;
  margin-top: 19px;
  padding: 12px 4px;
  border: 1px solid #e0e9df;
  border-radius: 16px;
  background: rgba(255, 255, 255, .62);
  backdrop-filter: blur(12px);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .92);
}
.om-run, .om-batch, .om-progress { padding: 1px 16px; border-right: 1px solid #e6ede5; }
.om-run { display: flex; flex-direction: column; gap: 3px; }
.om-k { display: block; margin-bottom: 4px; font-size: 8px; font-weight: 750; letter-spacing: .13em; color: #91a29a; }
.om-run-main { display: flex; align-items: center; gap: 7px; font-size: 14px; font-weight: 700; color: #2d4a40; }
.om-run-main i { width: 7px; height: 7px; border-radius: 50%; background: #7b9188; }
.om-run-main i.is-live { background: #d6a41d; box-shadow: 0 0 0 4px rgba(214, 164, 29, .13); animation: om-pulse 1.5s infinite; }
.om-run small, .om-batch small, .om-progress small { display: block; font-size: 8.5px; color: #8a9c93; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.om-batch b { display: block; font-size: 17px; letter-spacing: -.03em; color: #2d4a40; font-variant-numeric: tabular-nums; }
.om-progress-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.om-progress-head b { font-size: 17px; letter-spacing: -.03em; color: #2d4a40; font-variant-numeric: tabular-nums; }
.om-progress-head b small { display: inline; font-size: 9px; color: #7e9187; }
.om-track { height: 6px; margin: 5px 0 4px; border-radius: 999px; background: #e7eee7; overflow: hidden; }
.om-track i { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #3f665c, #7ca393 72%, #d6a41d); transition: width .5s ease; }
.om-vitals { display: flex; align-items: center; gap: 16px; padding: 0 16px; }
.om-vitals > div { min-width: 40px; }
.om-vitals span { display: block; margin-bottom: 3px; font-size: 8px; color: #91a29a; }
.om-vitals b { font-size: 17px; line-height: 1; letter-spacing: -.03em; color: #2d4a40; font-variant-numeric: tabular-nums; }
.om-vitals small { font-size: 8.5px; color: #8a9c93; }

.om-canvas { position: relative; z-index: 2; height: 600px; margin: 4px -10px -10px; }
.om-lines { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.om-link-base { fill: none; stroke: rgba(63, 102, 92, .20); stroke-width: 1.2; stroke-linecap: round; }
.om-link { fill: none; stroke-width: 1.9; stroke-linecap: round; stroke-dasharray: 4 7; opacity: .72; animation: om-dash 4.4s linear infinite; }
.om-link.is-active { stroke-width: 2.2; opacity: .88; animation-duration: 2.2s; }
.om-spark { opacity: .74; filter: url(#omSparkGlow); }

.om-core { position: absolute; left: 50%; top: 56%; width: 218px; height: 218px; transform: translate(-50%, -50%); display: flex; align-items: center; justify-content: center; }
.om-core-halo { position: absolute; inset: -46px; border-radius: 50%; background: radial-gradient(circle, rgba(63, 102, 92, .20), rgba(63, 102, 92, .04) 54%, transparent 70%); animation: om-breathe 4.8s ease-in-out infinite; }
.om-core-orbit, .om-core-orbit.is-outer { position: absolute; border-radius: 50%; border: 1px dashed rgba(63, 102, 92, .23); pointer-events: none; }
.om-core-orbit { inset: -13px; animation: om-spin 34s linear infinite; }
.om-core-orbit.is-outer { inset: -25px; border-color: rgba(214, 164, 29, .19); animation: om-spin-rev 46s linear infinite; }
.om-core-progress { position: absolute; inset: -6px; border-radius: 50%; background: conic-gradient(from -90deg, #d6a41d 0 var(--core-progress, 0%), rgba(63, 102, 92, .08) var(--core-progress, 0%) 100%); -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1px)); mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1px)); }
.om-core-disc { position: relative; z-index: 1; width: 100%; height: 100%; border-radius: 50%; display: flex; flex-direction: column; align-items: center; justify-content: center; background: radial-gradient(circle at 34% 22%, #4d7768 0%, #2e5146 42%, #1b352d 77%, #12251f 100%); border: 1px solid rgba(255, 255, 255, .15); box-shadow: 0 18px 46px rgba(24, 54, 44, .28), inset 0 1px 0 rgba(255, 255, 255, .16); color: #f5faf7; overflow: hidden; }
.om-core-disc::before { content: ""; position: absolute; width: 150px; height: 150px; left: -54px; top: -74px; border-radius: 50%; background: radial-gradient(circle, rgba(255, 255, 255, .14), transparent 66%); }
.om-core-disc::after { content: ""; position: absolute; inset: 0; border-radius: inherit; background: linear-gradient(112deg, transparent 34%, rgba(255, 255, 255, .08) 47%, transparent 56%); background-size: 220% 220%; animation: om-sheen 7s ease-in-out infinite; }
.om-core-kicker { position: relative; z-index: 1; font-size: 7.8px; letter-spacing: .15em; color: rgba(223, 242, 234, .62); }
.om-core-word { position: relative; z-index: 1; display: flex; align-items: baseline; margin-top: 9px; font-weight: 770; font-size: 20px; letter-spacing: -.05em; }
.om-core-word em { font-style: normal; margin-left: 1px; color: #e2bd57; font-size: 21px; }
.om-core-state { position: relative; z-index: 1; display: inline-flex; align-items: center; gap: 6px; margin-top: 11px; height: 23px; padding: 0 9px; border-radius: 999px; border: 1px solid rgba(198, 229, 216, .18); background: rgba(255, 255, 255, .07); font-size: 9px; color: rgba(237, 249, 243, .82); }
.om-core-state i { width: 6px; height: 6px; border-radius: 50%; background: #8fcfb9; }
.om-core-state.is-live { color: #f6e3b0; border-color: rgba(226, 189, 87, .30); background: rgba(214, 164, 29, .10); }
.om-core-state.is-live i { background: #e2bd57; animation: om-pulse 1.5s infinite; }
.om-core-disc small { position: relative; z-index: 1; margin-top: 8px; font-size: 9px; color: rgba(220, 238, 231, .62); font-variant-numeric: tabular-nums; }

.om-node {
  position: absolute; left: var(--ox); top: var(--oy); width: 196px; transform: translate(-50%, -50%);
  padding: 11px 13px 12px; border-radius: 16px; color: #2a4038;
  background: linear-gradient(150deg, rgba(255, 255, 255, .90), rgba(250, 252, 248, .78));
  border: 1px solid rgba(63, 102, 92, .16);
  box-shadow: 0 14px 30px rgba(30, 58, 47, .08), inset 0 1px 0 rgba(255, 255, 255, .95);
  backdrop-filter: blur(10px);
  animation: om-rise .7s cubic-bezier(.18,.86,.25,1) both;
  animation-delay: calc(var(--i) * 80ms + 120ms);
  transition: transform .25s ease, box-shadow .25s ease, border-color .25s ease;
}
.om-node:hover { transform: translate(-50%, calc(-50% - 3px)); box-shadow: 0 20px 36px rgba(30, 58, 47, .12), inset 0 1px 0 #fff; }
.om-node-accent { position: absolute; left: 13px; right: 13px; top: 0; height: 3px; border-radius: 0 0 4px 4px; background: linear-gradient(90deg, var(--tone), transparent 78%); opacity: .78; }
.om-node.is-active { border-color: rgba(214, 164, 29, .42); box-shadow: 0 16px 38px rgba(168, 124, 20, .13), 0 0 0 3px rgba(214, 164, 29, .06), inset 0 1px 0 #fff; }
.om-node-top { display: flex; align-items: center; justify-content: space-between; }
.om-node-num { font-size: 8.5px; letter-spacing: .14em; color: #94a59c; font-variant-numeric: tabular-nums; }
.om-node-flag { display: inline-flex; align-items: center; gap: 4px; font-size: 8px; color: var(--tone); font-weight: 650; }
.om-node-flag i { width: 5px; height: 5px; border-radius: 50%; background: var(--tone); opacity: .85; }
.om-node.is-active .om-node-flag i { animation: om-pulse 1.6s infinite; }
.om-node-icon { width: 26px; height: 26px; margin-top: 8px; display: flex; align-items: center; justify-content: center; border-radius: 8px; color: var(--tone); background: color-mix(in srgb, var(--tone) 10%, transparent); border: 1px solid color-mix(in srgb, var(--tone) 20%, transparent); font-size: 15px; }
.om-node h3 { margin: 7px 0 0; font-size: 11px; font-weight: 680; letter-spacing: .01em; color: #263f37; }
.om-node-value { display: flex; align-items: baseline; gap: 5px; margin: 4px 0 4px; font-size: 27px; line-height: 1; letter-spacing: -.045em; font-weight: 770; color: #203a32; font-variant-numeric: tabular-nums; }
.om-node-value small { font-size: 8.5px; font-weight: 520; color: #84968d; letter-spacing: 0; white-space: nowrap; }
.om-node-sub { margin: 0; min-height: 26px; font-size: 9px; line-height: 1.45; color: #71837a; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.om-node-meters { margin-top: 7px; display: flex; flex-direction: column; gap: 5px; }
.om-node-meter-head { display: flex; justify-content: space-between; font-size: 8px; color: #82958b; }
.om-node-meter-head b { color: var(--tone); font-weight: 650; font-variant-numeric: tabular-nums; }
.om-node-meter-track { height: 3px; margin-top: 2px; border-radius: 999px; background: #e7eee7; overflow: hidden; }
.om-node-meter-track i { display: block; height: 100%; border-radius: inherit; opacity: .86; transition: width .5s ease; }

.om-mobile { display: none; }
.om-foot { position: relative; z-index: 2; display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 2px; padding-top: 12px; border-top: 1px solid #e2eae2; font-size: 9.5px; color: #8a9b92; }
.om-foot > span:first-child { margin-right: 4px; color: #53675e; font-weight: 650; }
.om-foot i { font-style: normal; padding: 3px 8px; border-radius: 999px; background: rgba(255, 255, 255, .62); border: 1px solid #dfe8df; color: #71847a; }
.om-foot-note { margin-left: auto; color: #9caba3; }

@keyframes om-pulse { 0%, 100% { opacity: .68; transform: scale(.92); } 50% { opacity: 1; transform: scale(1.12); } }
@keyframes om-spin { to { transform: rotate(360deg); } }
@keyframes om-spin-rev { to { transform: rotate(-360deg); } }
@keyframes om-breathe { 0%, 100% { transform: scale(.96); opacity: .72; } 50% { transform: scale(1.04); opacity: 1; } }
@keyframes om-dash { to { stroke-dashoffset: -48; } }
@keyframes om-sheen { 0%, 100% { background-position: 120% 0%; } 50% { background-position: 0% 100%; } }
@keyframes om-rise { from { opacity: 0; transform: translate(-50%, calc(-50% + 12px)) scale(.97); } to { opacity: 1; transform: translate(-50%, -50%) scale(1); } }

@media (max-width: 1180px) {
  .om { padding: 24px 22px 16px; }
  .om-canvas { display: none; }
  .om-mobile { position: relative; z-index: 2; display: flex; flex-direction: column; margin: 16px 0 6px; padding-left: 13px; }
  .om-mobile::before { content: ""; position: absolute; left: 27px; top: 12px; bottom: 12px; width: 1px; background: linear-gradient(#cfded4, #e7dcc0); }
  .om-mobile-row { position: relative; display: grid; grid-template-columns: 30px 34px minmax(0, 1fr) auto; align-items: center; gap: 10px; min-height: 68px; padding: 8px 0 8px 4px; }
  .om-mobile-index { position: relative; z-index: 1; width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; border-radius: 50%; background: #fff; border: 1px solid #dbe6dd; font-size: 8px; color: #788b81; font-variant-numeric: tabular-nums; }
  .om-mobile-icon { width: 31px; height: 31px; display: flex; align-items: center; justify-content: center; border-radius: 10px; color: var(--tone); background: color-mix(in srgb, var(--tone) 9%, #fff); border: 1px solid color-mix(in srgb, var(--tone) 18%, #fff); font-size: 16px; }
  .om-mobile-body { min-width: 0; }
  .om-mobile-body b { display: block; font-size: 12px; color: #29433a; }
  .om-mobile-body small { display: block; margin-top: 2px; font-size: 9px; line-height: 1.4; color: #7e9187; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .om-mobile-side { text-align: right; }
  .om-mobile-side strong { display: flex; align-items: baseline; justify-content: flex-end; gap: 3px; font-size: 19px; line-height: 1; letter-spacing: -.04em; color: #233c34; font-variant-numeric: tabular-nums; }
  .om-mobile-side strong small { font-size: 8px; font-weight: 520; color: #8b9c93; }
  .om-mobile-side em { display: block; margin-top: 4px; font-size: 8px; font-style: normal; color: var(--tone); }
  .om-mobile-side em i { display: inline-block; width: 4px; height: 4px; margin-right: 3px; border-radius: 50%; background: var(--tone); vertical-align: 1px; }
  .om-head { flex-direction: column; gap: 12px; }
  .om-headside { flex-direction: row; align-items: center; }
  .om-copy h2 { font-size: 30px; }
  .om-telemetry { grid-template-columns: 1fr 1fr; gap: 12px 0; padding: 12px; }
  .om-run, .om-batch { border-right: 0; }
  .om-progress { grid-column: 1 / 2; border-right: 0; }
  .om-vitals { grid-column: 1 / -1; border-top: 1px solid #e6ede5; padding-top: 10px; padding-left: 16px; }
  .om-foot-note { display: none; }
}
@media (max-width: 640px) {
  .om { margin: 0 0 14px; padding: 22px 18px 14px; border-radius: 22px; }
  .om-foot { gap: 4px; }
  .om-foot > span:first-child { display: none; }
  .om-copy h2 { font-size: 26px; }
  .om-telemetry { grid-template-columns: 1fr 1fr; }
  .om-progress { grid-column: 1 / -1; border-top: 1px solid #e6ede5; padding-top: 8px; }
  .om-vitals { border-top: 0; padding-top: 0; }
  .om-vitals { gap: 12px; }
  .om-foot i { font-size: 8.5px; padding: 2px 6px; }
}
@media (prefers-reduced-motion: reduce) {
  .om-core-orbit, .om-core-orbit.is-outer, .om-core-halo, .om-core-disc::after, .om-link, .om-spark, .om-live i, .om-node { animation: none; }
  .om-spark { display: none; }
}
</style>
