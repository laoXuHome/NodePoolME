<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  points: { type: Array, default: () => [] },
  rows: { type: Array, default: () => [] },
  field: { type: String, required: true },
  range: { type: String, default: '1d' },
  title: { type: String, default: '' },
  totalLabel: { type: String, default: '' },
  totalCount: { type: [Number, String], default: 0 },
  color: { type: Function, required: true },
})

const box = ref(null)
const w = ref(560)
const h = ref(170)
const uiScale = ref(1)
let ro = null
let measure = null

// 手机「电脑模式」下整页被缩小，按 视觉缩放比 放大内部设计尺寸，保证字号/间距在屏幕上仍可读
function detectScale() {
  if (typeof window === 'undefined') return 1
  if (window.__npChartScale) return Math.min(3, Math.max(1, Number(window.__npChartScale) || 1))
  const coarse = window.matchMedia?.('(pointer: coarse)').matches
  const screenW = window.screen?.width || 0
  const innerW = window.innerWidth || 0
  if (!coarse || !screenW || !innerW) return 1
  const ratio = innerW / screenW
  if (ratio < 1.35) return 1
  return Math.min(2.8, Math.round(ratio * 100) / 100)
}

onMounted(() => {
  const el = box.value
  if (!el) return
  measure = () => {
    const rect = el.getBoundingClientRect()
    const scale = detectScale()
    uiScale.value = scale
    w.value = Math.max(240, Math.round(rect.width / scale))
    h.value = Math.max(120, Math.round(rect.height / scale))
    narrow.value = w.value < 480 || window.matchMedia('(max-width: 760px)').matches
  }
  measure()
  ro = new ResizeObserver(measure)
  ro.observe(el)
  window.addEventListener('resize', measure)
  window.addEventListener('orientationchange', measure)
  window.visualViewport?.addEventListener('resize', measure)
})
onBeforeUnmount(() => {
  ro?.disconnect(); ro = null
  window.removeEventListener('resize', measure)
  window.removeEventListener('orientationchange', measure)
  window.visualViewport?.removeEventListener('resize', measure)
})

const PAD = { l: 34, r: 3, t: 12, b: 22 }
const plotW = computed(() => Math.max(80, w.value - PAD.l - PAD.r))
const plotH = computed(() => Math.max(50, h.value - PAD.t - PAD.b))

function pointTime(point) {
  if (point?.live) return Date.now()
  const t = new Date(String(point?.ts || '').replace(' ', 'T')).getTime()
  return Number.isNaN(t) ? 0 : t
}
function countOf(point, id) {
  const v = point?.airports?.[String(id)]?.[props.field]
  return Number(v) || 0
}

const domain = computed(() => {
  const pts0 = plotted.value
  const end = (pts0.length && pointTime(pts0[pts0.length - 1])) || Date.now()
  const days = props.range === '7d' ? 7 : 1
  const floor = end - days * 86400000
  const pts = pts0
  if (!pts.length) return { start: floor, end }
  const first = pointTime(pts[0])
  const span = end - first
  let start = Math.max(floor, first)
  if (props.range === '1d' && span > days * 86400000) start = floor
  if (start >= end) start = end - 3600000
  return { start, end }
})

const plotted = computed(() => props.points.filter((point) => !point.live))
const series = computed(() => props.rows
  .filter((row) => row && row.enabled)
  .map((row) => ({ id: row.id, name: row.name || `#${row.id}`, color: props.color(row),
    values: plotted.value.map((p) => countOf(p, row.id)) }))
  .filter((s) => s.values.some((v) => v > 0)))

function niceStep(raw) {
  const n = Math.max(1, raw)
  const pow = 10 ** Math.floor(Math.log10(n))
  for (const mult of [1, 2, 2.5, 5, 10]) {
    const cand = mult * pow
    if (cand >= n) return cand
  }
  return 10 * pow
}

const yMax = computed(() => {
  let max = 0
  for (const s of series.value) for (const v of s.values) if (v > max) max = v
  if (!max) return 1
  return niceStep(max / 4) * 4
})

const yTicks = computed(() => {
  const steps = 4
  const step = yMax.value / steps
  return Array.from({ length: steps + 1 }, (_, i) => ({
    v: step * i,
    label: Number.isInteger(step) ? String(step * i) : (step * i).toFixed(1),
    y: PAD.t + (1 - i / steps) * plotH.value,
  }))
})

function px(time) {
  const span = domain.value.end - domain.value.start || 1
  const ratio = Math.min(1, Math.max(0, (time - domain.value.start) / span))
  return PAD.l + ratio * plotW.value
}
function py(value) {
  return PAD.t + (1 - Math.min(value, yMax.value) / yMax.value) * plotH.value
}

const xTicks = computed(() => {
  const span = domain.value.end - domain.value.start
  const count = w.value < 420 ? 3 : 4
  return Array.from({ length: count }, (_, i) => {
    const time = domain.value.start + span * i / (count - 1)
    const d = new Date(time)
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
    let text = `${d.getMonth() + 1}/${d.getDate()}`
    if (props.range === '1d') text = i === count - 1 ? '当前' : i === 0 ? `昨天 ${hm}` : hm
    else if (props.range === '7d' && span < 3 * 86400000) text = `${d.getMonth() + 1}/${d.getDate()} ${hm}`
    if (props.range === '7d' && i === count - 1) text = '当前'
    return { x: PAD.l + plotW.value * i / (count - 1), text,
      anchor: i === 0 ? 'start' : i === count - 1 ? 'end' : 'middle' }
  })
})

// Monotone cubic interpolation: smooth through every snapshot without overshoot.
function pathFor(values) {
  const pts = values.map((v, i) => ({ x: px(pointTime(plotted.value[i])), y: py(v) }))
  if (!pts.length) return ''
  if (pts.length === 1) return ''
  const spans = []
  const slopes = []
  for (let i = 0; i < pts.length - 1; i += 1) {
    const dx = pts[i + 1].x - pts[i].x || 1
    spans.push(dx)
    slopes.push((pts[i + 1].y - pts[i].y) / dx)
  }
  const tangents = pts.map((_, i) => {
    if (i === 0) return slopes[0]
    if (i === pts.length - 1) return slopes[slopes.length - 1]
    const prev = slopes[i - 1]
    const next = slopes[i]
    if (prev * next <= 0) return 0
    const hp = spans[i - 1]
    const hn = spans[i]
    const w1 = 2 * hn + hp
    const w2 = hn + 2 * hp
    return (w1 + w2) / (w1 / prev + w2 / next)
  })
  let d = `M${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`
  for (let i = 0; i < pts.length - 1; i += 1) {
    const dx = spans[i]
    const c1x = pts[i].x + dx / 3
    const c1y = pts[i].y + tangents[i] * dx / 3
    const c2x = pts[i + 1].x - dx / 3
    const c2y = pts[i + 1].y - tangents[i + 1] * dx / 3
    d += ` C${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${pts[i + 1].x.toFixed(1)} ${pts[i + 1].y.toFixed(1)}`
  }
  return d
}

const paths = computed(() => series.value.map((s) => ({ id: s.id, color: s.color, d: pathFor(s.values) })))

const endDots = computed(() => series.value.map((s) => {
  const last = s.values.length - 1
  return { id: s.id, color: s.color, cx: px(pointTime(plotted.value[last])), cy: py(s.values[last]) }
}))

const lastIndex = computed(() => Math.max(0, plotted.value.length - 1))
const lastTotal = computed(() => {
  const last = plotted.value[lastIndex.value]
  if (!last) return 0
  return series.value.reduce((acc, row) => acc + (row.values[lastIndex.value] || 0), 0)
})
const liveDiff = computed(() => {
  const live = props.points.find((point) => point.live)
  const n = Number(props.totalCount)
  if (!live || !Number.isFinite(n)) return false
  return n !== lastTotal.value
})
const narrow = ref(false)

// 末端标签：按每条曲线的最后一个数值定位，纵向贴住线尾；数值相同就落在同一条高度上，
// 太近时按最小间距微调，并画一小段引导线指回数据点。
const labels = computed(() => {
  if (!plotted.value.length) return []
  const last = lastIndex.value
  const total = lastTotal.value || 1
  const items = series.value.map((row) => {
    const value = row.values[last] || 0
    const y = py(value)
    return {
      id: row.id,
      name: row.name,
      color: row.color,
      value,
      pct: total ? Math.round((value / total) * 100) : 0,
      dotY: y,
      y,
      col: 0,
    }
  })
  items.sort((a, b) => a.dotY - b.dotY || a.id - b.id)
  const top = PAD.t + 4
  const bottom = PAD.t + plotH.value - 2
  const gap = 11
  // 单列贴线尾；末端数值过近的一组上下居中均分，其余按线尾 Y 精确落位
  let ys = items.map(it => Math.max(top, Math.min(bottom, it.dotY)))
  const runs = []
  let start = 0
  for (let i = 1; i <= ys.length; i++) {
    if (i === ys.length || ys[i] - ys[i - 1] >= gap) {
      runs.push([start, i - 1])
      start = i
    }
  }
  for (const [a, b] of runs) {
    const k = b - a + 1
    if (k < 2) continue
    const need = (k - 1) * gap
    if (ys[b] - ys[a] >= need) continue
    let mid = (ys[a] + ys[b]) / 2
    const lo = ys[ys.length - 1] > bottom ? mid : Math.min(mid, ys[a] + need / 2)
    mid = Math.max(top + need / 2, Math.min(lo, top + (bottom - top) - need / 2))
    for (let i = a; i <= b; i++) ys[i] = mid - need / 2 + (i - a) * gap
  }
  if (ys[ys.length - 1] > bottom) ys = ys.map(y => y - (ys[ys.length - 1] - bottom))
  for (let i = ys.length - 1; i > 0; i--) if (ys[i] - ys[i - 1] < gap) ys[i - 1] = ys[i] - gap
  if (ys[0] < top) ys = ys.map(y => y + (top - ys[0]))
  items.forEach((it, i) => { it.y = ys[i]; it.shifted = Math.abs(ys[i] - it.dotY) > 1 })
  return items
})
const hover = ref(null)
function onMove(event) {
  if (!plotted.value.length) return
  const rect = event.currentTarget.getBoundingClientRect()
  const relX = Math.max(0, Math.min(w.value, (event.clientX - rect.left) / uiScale.value))
  const span = domain.value.end - domain.value.start || 1
  const time = domain.value.start + (relX - PAD.l) / plotW.value * span
  let best = 0
  let bestDist = Infinity
  plotted.value.forEach((p, i) => {
    const dist = Math.abs(pointTime(p) - time)
    if (dist < bestDist) { bestDist = dist; best = i }
  })
  const point = plotted.value[best]
  const d = new Date(pointTime(point))
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const stamp = `${d.getMonth() + 1}/${d.getDate()} ${hm}`
  const pointTotal = series.value.reduce((acc, s) => acc + (s.values[best] || 0), 0)
  const values = series.value
    .map((s) => ({ name: s.name, color: s.color, v: s.values[best] || 0,
      pct: pointTotal ? Math.round((s.values[best] || 0) / pointTotal * 100) : 0 }))
    .sort((a, b) => b.v - a.v)
  const dots = values.slice(0, 6).map((item) => item.v)
  const anchorX = px(pointTime(point))
  const tipLeft = Math.min(Math.max(anchorX + 10, 8), Math.max(8, w.value - 172))
  hover.value = { x: anchorX, stamp, values: values.slice(0, 5), more: Math.max(0, values.length - 5),
    dots: series.value.map((s) => ({ color: s.color, cx: anchorX, cy: py(s.values[best] || 0) })),
    tip: { left: tipLeft, top: 4 }, count: dots.length }
}
function onLeave() { hover.value = null }
const hoverTotal = computed(() => hover.value ? hover.value.values.reduce((a, b) => a + b.v, 0) : 0)
</script>

<template>
  <div class="np-tc" :style="{ '--tcs': String(uiScale) }">
    <div class="np-tc-head">
      <b class="np-tc-title">{{ title }}</b>
      <i v-if="lastTotal !== null" class="np-tc-live">实时 {{ lastTotal }}</i>
    </div>
    <div class="np-tc-body">
      <div ref="box" class="np-tc-plot">
        <svg v-if="plotted.length" class="np-tc-svg" :viewBox="`0 0 ${w} ${h}`"
             @pointermove="onMove" @pointerleave="onLeave">
          <line v-for="t in yTicks" :key="`gy-${t.v}`" class="np-tc-grid"
                :x1="PAD.l" :x2="PAD.l + plotW" :y1="t.y" :y2="t.y" />
          <text v-for="t in yTicks" :key="`ty-${t.v}`" class="np-tc-tick"
                :x="PAD.l - 6" :y="t.y + 3.5" text-anchor="end">{{ t.label }}</text>
          <line class="np-tc-axis" :x1="PAD.l" :x2="PAD.l + plotW" :y1="PAD.t + plotH" :y2="PAD.t + plotH" />
          <line class="np-tc-axis" :x1="PAD.l" :x2="PAD.l" :y1="PAD.t" :y2="PAD.t + plotH" />
          <path v-for="p in paths" :key="`ln-${p.id}`" :d="p.d" :stroke="p.color" class="np-tc-line" />
          <circle v-for="d in endDots" :key="`dot-${d.id}`" :cx="d.cx" :cy="d.cy" r="2.6"
                  :fill="d.color" class="np-tc-dot" />
          <g v-if="hover" class="np-tc-hover">
            <line :x1="hover.x" :x2="hover.x" :y1="PAD.t" :y2="PAD.t + plotH" class="np-tc-cross" />
            <circle v-for="(d, i) in hover.dots" :key="`hd-${i}`" :cx="d.cx" :cy="d.cy" r="2.4"
                    :fill="d.color" class="np-tc-hdot" />
          </g>
          <line v-for="l in labels.filter((i) => i.shifted)" :key="`ld-${l.id}`"
                :x1="l.dotX" :x2="l.dotX + 7" :y1="l.dotY" :y2="l.y" :stroke="l.color" class="np-tc-leader" />
          <text v-for="t in xTicks" :key="`tx-${t.x}`" class="np-tc-axis-text"
                :x="t.x" :y="h - 6" :text-anchor="t.anchor">{{ t.text }}</text>
        </svg>
        <div v-if="hover" class="np-tc-tip" :style="{ left: `${hover.tip.left}px`, top: `${hover.tip.top}px` }">
          <span class="np-tc-tip-time">{{ hover.stamp }}</span>
          <div v-for="v in hover.values" :key="v.name" class="np-tc-tip-row">
            <i :style="{ background: v.color }" />
            <em>{{ v.name }}</em>
            <b>{{ v.v }}</b>
            <small class="np-tc-tip-pct">{{ v.pct }}%</small>
          </div>
          <div v-if="hover.more" class="np-tc-tip-more">另有 {{ hover.more }} 个机场</div>
          <div class="np-tc-tip-total">合计 <b>{{ hoverTotal }}</b></div>
        </div>
        <div v-if="!plotted.length" class="np-tc-empty">暂无历史快照</div>
      </div>
      <div class="np-tc-legend">
        <div v-for="l in labels" :key="l.id" class="np-tc-lab" 
             :title="`${l.name} · ${l.pct}% · ${l.value} 节点`"
             :style="{ top: `${(l.y - 6.5) * uiScale}px`, background: l.color }">
          <em>{{ l.name }}</em>
          <b>{{ l.pct }}%</b>
          <small>{{ l.value }}</small>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.np-tc { flex: 1; width: 100%; display: flex; flex-direction: column; height: 100%; min-height: 0; min-width: 0; }
.np-tc-head { flex: none; display: flex; align-items: baseline; justify-content: space-between; gap: calc(10px * var(--tcs, 1)); padding: 0 calc(2px * var(--tcs, 1)) calc(4px * var(--tcs, 1)); }
.np-tc-title { font-size: calc(12.5px * var(--tcs, 1)); font-weight: 650; color: var(--np-text, #1c1c1e); white-space: nowrap; }
.np-tc-total { font-size: 10px; color: var(--np-tertiary, #8d939d); white-space: nowrap; }
.np-tc-live { padding: calc(1px * var(--tcs, 1)) calc(6px * var(--tcs, 1)); border-radius: calc(5px * var(--tcs, 1)); background: #eef3ff; color: #2f6bdd; font-style: normal; font-size: calc(10px * var(--tcs, 1)); line-height: 1.45; }
.np-tc-total b { font-size: 15px; font-weight: 700; color: var(--np-text, #1c1c1e); font-variant-numeric: tabular-nums; margin-left: 2px; }
.np-tc-body { flex: 1; display: flex; gap: 0; min-height: 0; min-width: 0; }
.np-tc-plot { position: relative; flex: 1; min-width: 0; min-height: 0; }
.np-tc-svg { display: block; width: 100%; height: 100%; }
.np-tc-grid { stroke: #e9eef5; stroke-width: 1; }
.np-tc-axis { stroke: #dcE2EA; stroke-width: 1; }
.np-tc-tick { fill: #9aa1ab; font-size: 9.5px; font-variant-numeric: tabular-nums; }
.np-tc-axis-text { fill: #8d939d; font-size: 9.5px; font-variant-numeric: tabular-nums; }
.np-tc-line { fill: none; stroke-width: 1.7; stroke-linecap: round; }
.np-tc-dot { stroke: #fff; stroke-width: 1; }
.np-tc-cross { stroke: #c3cbd6; stroke-width: 1; }
.np-tc-hdot { stroke: #fff; stroke-width: 1; }
.np-tc-legend { position: relative; flex: none; width: calc(60px * var(--tcs, 1)); align-self: stretch; }
.np-tc-lab {
  position: absolute;
  left: 0;
  right: 0;
  justify-content: flex-start;
  height: calc(12px * var(--tcs, 1));
  display: inline-flex;
  align-items: center;
  gap: calc(2.5px * var(--tcs, 1));
  padding: 0 calc(4px * var(--tcs, 1));
  border-radius: calc(3px * var(--tcs, 1));
  color: #fff;
  font-size: calc(9px * var(--tcs, 1));
  font-weight: 600;
  line-height: 1;
  letter-spacing: -.1px;
  white-space: nowrap;
  box-shadow: 0 0 0 1px #fff, 0 1px 2px rgba(20, 30, 50, .08);
}
.np-tc-lab em { font-style: normal; min-width: 0; max-width: calc(27px * var(--tcs, 1)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.np-tc-lab b { font-weight: 500; opacity: .82; font-variant-numeric: tabular-nums; }
.np-tc-lab small { font-weight: 700; font-variant-numeric: tabular-nums; }
.np-tc-leader { stroke-width: .9; opacity: .8; }
.np-tc-chips { display: flex; flex-wrap: wrap; gap: 2px 12px; max-height: 56px; overflow-y: auto; padding: 2px 0 0; flex: none; }
.np-tc-chips span { display: inline-flex; align-items: center; gap: 3px; height: 14px; padding: 0 4.5px; border-radius: 3px; font-size: 9.5px; font-weight: 600; color: #fff; white-space: nowrap; }
.np-tc-chips span b { font-weight: 500; opacity: .82; }
.np-tc-chips span small { font-weight: 700; }
.np-tc-empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 11px; color: #9aa1ab; }
.np-tc-tip { position: absolute; z-index: 3; min-width: calc(148px * var(--tcs, 1)); padding: calc(6px * var(--tcs, 1)) calc(8px * var(--tcs, 1)); border-radius: calc(8px * var(--tcs, 1)); background: rgba(255, 255, 255, .97); box-shadow: 0 6px 20px rgba(20, 30, 50, .16); border: 1px solid #e6eaf0; pointer-events: none; }
.np-tc-tip-time { display: block; font-size: calc(10px * var(--tcs, 1)); font-weight: 650; color: #6b7280; margin-bottom: 3px; }
.np-tc-tip-row { display: flex; align-items: center; gap: calc(5px * var(--tcs, 1)); font-size: calc(10.5px * var(--tcs, 1)); line-height: calc(15px * var(--tcs, 1)); color: #404653; }
.np-tc-tip-row i { width: 6px; height: 6px; border-radius: 50%; flex: none; }
.np-tc-tip-row em { flex: 1; min-width: 0; font-style: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.np-tc-tip-row b { font-weight: 650; font-variant-numeric: tabular-nums; }
.np-tc-tip-pct { flex: none; width: calc(26px * var(--tcs, 1)); text-align: right; color: #98a0ad; font-size: calc(9.5px * var(--tcs, 1)); font-variant-numeric: tabular-nums; }
.np-tc-tip-more { font-size: calc(9.5px * var(--tcs, 1)); color: #98a0ad; margin-top: 2px; }
.np-tc-tip-total { margin-top: calc(4px * var(--tcs, 1)); padding-top: calc(3px * var(--tcs, 1)); border-top: 1px solid #eceff4; font-size: calc(10px * var(--tcs, 1)); color: #8d939d; }
.np-tc-tip-total b { color: #2f3540; font-variant-numeric: tabular-nums; }
@media (max-width: 760px) {
  .np-tc-legend { width: calc(56px * var(--tcs, 1)); }
  .np-tc-lab { font-size: calc(8.5px * var(--tcs, 1)); gap: calc(2px * var(--tcs, 1)); padding: 0 calc(3px * var(--tcs, 1)); }
  .np-tc-lab em { max-width: calc(24px * var(--tcs, 1)); }
}
</style>
