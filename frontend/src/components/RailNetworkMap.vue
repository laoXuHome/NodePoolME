<script setup>
import { computed } from 'vue'

const props = defineProps({
  regions: { type: Array, default: () => [] },
  score: { type: String, default: '—' },
  available: { type: Number, default: 0 },
  total: { type: Number, default: 0 },
  compact: { type: Boolean, default: false }
})

const VIEW_W = 300
const VIEW_H = 150
const REGION_COORDS = {
  HK: [114.2, 22.3], MO: [113.5, 22.2], TW: [121.0, 23.7], CN: [105.0, 35.0],
  SG: [103.8, 1.35], MY: [102.0, 4.0], TH: [101.0, 15.0], VN: [106.0, 16.0],
  ID: [118.0, -2.0], PH: [122.0, 12.0], JP: [138.0, 36.2], KR: [127.8, 36.5],
  IN: [78.0, 22.0], PK: [69.0, 30.0], AE: [54.0, 24.0], SA: [45.0, 24.0],
  IL: [35.0, 31.0], TR: [35.0, 39.0], RU: [90.0, 61.0], KZ: [67.0, 48.0],
  US: [-98.5, 39.8], CA: [-106.0, 56.0], MX: [-102.0, 23.0], BR: [-51.0, -10.0],
  AR: [-64.0, -34.0], CL: [-71.0, -33.0], CO: [-74.0, 4.0], PE: [-75.0, -10.0],
  GB: [-2.5, 54.0], IE: [-8.0, 53.0], DE: [10.4, 51.1], FR: [2.4, 46.2],
  NL: [5.3, 52.1], BE: [4.5, 50.8], CH: [8.2, 46.8], AT: [14.5, 47.5],
  IT: [12.5, 42.8], ES: [-3.7, 40.4], PT: [-8.0, 39.5], PL: [19.0, 52.0],
  CZ: [15.0, 50.0], RO: [25.0, 46.0], UA: [31.0, 49.0], SE: [15.0, 60.0],
  NO: [8.0, 61.0], FI: [25.0, 64.0], DK: [10.0, 56.0], AU: [134.0, -25.0],
  NZ: [172.0, -41.0], ZA: [24.0, -29.0], EG: [30.0, 27.0], NG: [8.0, 9.0],
  KE: [38.0, 0.0]
}

function project(coord) {
  return [
    (coord[0] + 180) / 360 * VIEW_W,
    (90 - coord[1]) / 180 * VIEW_H
  ]
}

function fallbackCoord(region) {
  let hash = 0
  for (const letter of region || '??') hash = (hash * 31 + letter.charCodeAt(0)) >>> 0
  const lon = -150 + (hash % 300)
  const lat = -25 + ((hash >>> 8) % 80)
  return project([lon, lat])
}

const points = computed(() => props.regions.map((region) => {
  const code = String(region.region || 'UNKNOWN').toUpperCase()
  const rawCoord = REGION_COORDS[code]
  const coord = rawCoord ? project(rawCoord) : fallbackCoord(code)
  const total = Number(region.total) || 0
  const available = Number(region.available) || 0
  const status = available > 0 ? 'up' : (Number(region.unavailable) > 0 ? 'down' : 'idle')
  return {
    ...region,
    code,
    x: coord[0],
    y: coord[1],
    status,
    total,
    available,
    radius: 1.7 + Math.min(3.4, Math.sqrt(Math.max(total, 1)) * 0.85)
  }
}).filter((point) => point.code !== 'UNKNOWN'))

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

const links = computed(() => {
  const pointsNow = points.value
  if (pointsNow.length < 2) return []
  const pairs = []
  const seen = new Set()
  for (const a of pointsNow) {
    const nearest = pointsNow
      .filter((b) => b !== a)
      .sort((b, c) => distance(a, b) - distance(a, c))
      .slice(0, 2)
    for (const b of nearest) {
      const key = [a.code, b.code].sort().join('-')
      if (seen.has(key)) continue
      seen.add(key)
      pairs.push([a, b])
    }
  }
  return pairs.slice(0, 10).map(([a, b], index) => {
    const dx = b.x - a.x
    const dy = b.y - a.y
    const span = Math.max(1, Math.hypot(dx, dy))
    const bend = Math.min(24, span * 0.24) * (index % 2 === 0 ? 1 : -1)
    const cx = (a.x + b.x) / 2 - dy / span * bend
    const cy = (a.y + b.y) / 2 + dx / span * bend
    return {
      key: `${a.code}-${b.code}-${index}`,
      d: `M${a.x.toFixed(2)} ${a.y.toFixed(2)} Q${cx.toFixed(2)} ${cy.toFixed(2)} ${b.x.toFixed(2)} ${b.y.toFixed(2)}`,
      live: a.status === 'up' && b.status === 'up',
      delay: `${(index * 0.35).toFixed(2)}s`,
      duration: `${(4.2 + (span / VIEW_W) * 4).toFixed(2)}s`
    }
  })
})

const regionCount = computed(() => points.value.length)
const liveLabel = computed(() => props.total ? `${props.available}/${props.total}` : '0/0')
</script>

<template>
  <div class="rail-network" :class="{ 'is-compact': compact }" aria-label="可用节点地区实时网络">
    <div class="rail-network-head">
      <span>LIVE NODE MESH</span>
      <b><i></i>{{ liveLabel }}</b>
    </div>
    <div class="rail-network-map">
      <svg :viewBox="`0 0 ${VIEW_W} ${VIEW_H}`" role="img" aria-hidden="true">
        <defs>
          <filter id="rail-node-glow" x="-80%" y="-80%" width="260%" height="260%">
            <feGaussianBlur stdDeviation="2.6" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <filter id="rail-line-glow" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="1.1" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <pattern id="rail-land-dots" width="5" height="5" patternUnits="userSpaceOnUse">
            <circle cx="1.6" cy="1.6" r=".62" fill="#dbece1" fill-opacity=".25" />
          </pattern>
          <radialGradient id="rail-node-core">
            <stop offset="0" stop-color="#fff8d9" stop-opacity="1" />
            <stop offset=".55" stop-color="#efc44c" stop-opacity=".95" />
            <stop offset="1" stop-color="#efc44c" stop-opacity="0" />
          </radialGradient>
        </defs>

        <g class="rail-land">
          <path d="M28 38 L54 25 84 23 111 29 132 40 126 54 113 62 102 76 79 80 58 72 44 58 33 49Z" />
          <path d="M94 82 L111 79 124 89 128 106 118 125 105 134 96 121 92 104 89 93Z" />
          <path d="M148 24 L164 19 179 21 186 30 180 41 168 47 156 44 148 35Z" />
          <path d="M151 51 L166 47 181 54 188 67 184 87 175 104 161 116 150 108 145 91 145 71Z" />
          <path d="M184 22 L207 16 237 17 260 22 279 31 286 42 278 55 264 64 246 73 226 83 206 86 190 78 181 65 179 50 181 34Z" />
          <path d="M248 88 L260 84 273 87 280 95 276 106 264 110 252 105 246 96Z" />
          <path d="M121 21 L131 17 139 22 136 29 127 32Z" />
        </g>

        <g class="rail-links">
          <path v-for="link in links" :key="link.key" :d="link.d"
                :class="{ 'is-live': link.live, 'is-down': !link.live }"
                :style="link.live ? { animationDelay: link.delay, animationDuration: link.duration } : null" />
        </g>

        <g class="rail-points">
          <g v-for="point in points" :key="point.code" :class="['rail-point', 'is-' + point.status]">
            <title>{{ point.code }} · 可用 {{ point.available }} / {{ point.total }}</title>
            <circle v-if="point.status === 'up'" :cx="point.x" :cy="point.y"
                    :r="point.radius + 3.5" class="rail-point-halo" />
            <circle :cx="point.x" :cy="point.y" :r="point.radius" class="rail-point-core" />
            <circle v-if="point.status === 'up'" :cx="point.x" :cy="point.y"
                    :r="Math.max(1.1, point.radius * .36)" class="rail-point-hot" />
          </g>
        </g>
      </svg>
    </div>
    <div class="rail-network-foot">
      <span>{{ regionCount }} REGIONS</span>
      <b>SCORE {{ score }}</b>
    </div>
  </div>
</template>

<style scoped>
.rail-network {
  position: relative;
  padding: 10px 11px 9px;
  border-radius: 18px;
  overflow: hidden;
  background:
    radial-gradient(circle at 32% 18%, rgba(73, 110, 96, .42), transparent 56%),
    linear-gradient(145deg, #26463b, #1c3730 68%, #1a332d);
  border: 1px solid rgba(255, 255, 255, .07);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .05), 0 10px 24px rgba(0, 0, 0, .13);
}
.rail-network::after {
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(circle at 82% 80%, rgba(239, 196, 76, .06), transparent 44%),
    repeating-linear-gradient(0deg, rgba(255,255,255,.018) 0 1px, transparent 1px 3px);
  mix-blend-mode: screen;
  opacity: .55;
}
.rail-network-head, .rail-network-foot {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.rail-network-head { font-size: 7.5px; letter-spacing: .15em; font-weight: 700; color: rgba(239, 196, 76, .78); }
.rail-network-head b { display: inline-flex; align-items: center; gap: 4px; font-size: 8px; letter-spacing: .05em; color: #eff5ef; }
.rail-network-head b i { width: 4px; height: 4px; border-radius: 50%; background: #e8c36d; box-shadow: 0 0 0 3px rgba(232, 195, 109, .12); animation: rail-pulse 2.4s ease-in-out infinite; }
.rail-network-map { position: relative; z-index: 1; margin: 4px -3px 1px; }
.rail-network-map svg { display: block; width: 100%; height: 108px; overflow: visible; }
.rail-land path {
  fill: url(#rail-land-dots);
  stroke: rgba(218, 236, 225, .10);
  stroke-width: .55;
}
.rail-links path {
  fill: none;
  stroke: #e8c36d;
  stroke-width: .9;
  stroke-linecap: round;
  stroke-dasharray: 2 3;
}
.rail-links path.is-live {
  opacity: .46;
  filter: url(#rail-line-glow);
  animation: rail-flow 5s linear infinite;
}
.rail-links path.is-down {
  stroke: #b36e5d;
  opacity: .22;
  stroke-dasharray: 1 4;
}
.rail-point-core {
  fill: #c77763;
  opacity: .62;
}
.rail-point-halo {
  fill: url(#rail-node-core);
  opacity: .38;
  filter: url(#rail-node-glow);
  animation: rail-breathe 3.2s ease-in-out infinite;
}
.rail-point-hot {
  fill: #fff9dc;
  filter: url(#rail-node-glow);
}
.rail-point.is-down .rail-point-core { fill: #c77763; opacity: .64; }
.rail-point.is-idle .rail-point-core { fill: #8da49a; opacity: .5; }
.rail-point.is-up .rail-point-core { fill: #efc44c; opacity: .95; }
.rail-network-foot { margin-top: 3px; font-size: 7px; font-weight: 600; letter-spacing: .08em; color: rgba(223, 234, 227, .46); }
.rail-network-foot b { font-size: 7.5px; color: rgba(239, 196, 76, .72); }
@keyframes rail-flow { to { stroke-dashoffset: -20; } }
@keyframes rail-breathe { 0%, 100% { opacity: .28; } 50% { opacity: .58; } }
@keyframes rail-pulse { 0%, 100% { opacity: .65; } 50% { opacity: 1; } }
@media (max-width: 1280px) {
  .rail-network-map svg { height: 100px; }
}
.rail-network.is-compact {
  width: 290px; height: 108px; padding: 8px 10px; border-radius: 16px;
  display: grid;
  grid-template-columns: 148px minmax(0, 1fr);
  grid-template-rows: 1fr 1fr;
  column-gap: 10px; row-gap: 2px;
  align-content: center;
}
.is-compact .rail-network-head {
  grid-column: 2; grid-row: 1;
  flex-direction: column; align-items: flex-start; justify-content: flex-start;
  gap: 3px; font-size: 9.5px; letter-spacing: .08em;
}
.is-compact .rail-network-head b { font-size: 14px; gap: 5px; }
.is-compact .rail-network-map {
  grid-column: 1; grid-row: 1 / span 2;
  margin: 0; align-self: center;
}
.is-compact .rail-network-map svg { height: 88px; }
.is-compact .rail-network-foot {
  grid-column: 2; grid-row: 2;
  flex-direction: column; align-items: flex-start; justify-content: flex-start;
  gap: 3px; margin-top: 0; font-size: 9.5px; color: rgba(230, 241, 234, .62);
}
.is-compact .rail-network-foot b { font-size: 10.5px; color: rgba(239, 196, 76, .90); }
</style>
