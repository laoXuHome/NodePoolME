<script setup>
defineProps({
  size: { type: Number, default: 34 },
  compact: { type: Boolean, default: false },
  light: { type: Boolean, default: false }
})
</script>

<template>
  <div class="np-brand" :class="{ 'is-compact': compact, 'is-light': light }">
    <svg class="np-mark" :width="size" :height="size" viewBox="0 0 40 40" role="img" aria-label="NodePoolME">
      <defs>
        <linearGradient id="npBlueGrad" x1="0" y1="0" x2="0.9" y2="1">
          <stop offset="0%" stop-color="#5d8b7c" />
          <stop offset="52%" stop-color="#39665a" />
          <stop offset="100%" stop-color="#23463d" />
        </linearGradient>
        <linearGradient id="npGloss" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#ffffff" stop-opacity=".34" />
          <stop offset="46%" stop-color="#ffffff" stop-opacity=".06" />
          <stop offset="100%" stop-color="#ffffff" stop-opacity="0" />
        </linearGradient>
        <radialGradient id="npInner" cx="0.32" cy="0.26" r="0.72">
          <stop offset="0%" stop-color="#ffffff" stop-opacity=".18" />
          <stop offset="100%" stop-color="#ffffff" stop-opacity="0" />
        </radialGradient>
      </defs>

      <!-- 底板 -->
      <rect x="1" y="1" width="38" height="38" rx="11.5" fill="url(#npBlueGrad)" />
      <rect x="1" y="1" width="38" height="38" rx="11.5" fill="url(#npInner)" />
      <rect x="1" y="1" width="38" height="38" rx="11.5" fill="url(#npGloss)" />
      <rect x="1.6" y="1.6" width="36.8" height="36.8" rx="11" fill="none"
            stroke="#ffffff" stroke-opacity=".22" stroke-width="1.2" />

      <!-- 汇聚结构：多机场 → 节点池 → 下发 -->
      <g stroke="#ffffff" stroke-linecap="round" fill="none">
        <path d="M12.9 12.5 L17.0 16.4" stroke-width="2.1" stroke-opacity=".92" />
        <path d="M27.1 12.5 L23.0 16.4" stroke-width="2.1" stroke-opacity=".92" />
        <path d="M20 25.6 L20 22.6" stroke-width="2.1" stroke-opacity=".92" />
      </g>
      <g fill="#ffffff">
        <circle cx="11.6" cy="11.2" r="3.2" />
        <circle cx="28.4" cy="11.2" r="3.2" />
        <circle cx="20" cy="28" r="3.2" />
      </g>
      <circle cx="20" cy="19.2" r="6.1" fill="none" stroke="#ffffff" stroke-width="1.1" stroke-opacity=".42" />
      <circle cx="20" cy="19.2" r="3.9" fill="#ffffff" />
      <circle cx="20" cy="19.2" r="1.55" fill="#23463d" fill-opacity=".55" />
    </svg>

    <span class="np-word">
      <span class="np-word-main">NodePool</span><span class="np-word-accent">ME</span>
    </span>
  </div>
</template>

<style scoped>
.np-brand { display: inline-flex; align-items: center; gap: 12px; flex: none; }
.np-mark {
  display: block;
  flex: none;
  filter: drop-shadow(0 2px 6px rgba(24, 52, 44, .28));
}
.np-word {
  display: inline-flex;
  align-items: baseline;
  font-weight: 750;
  letter-spacing: -.033em;
  white-space: nowrap;
}
.np-word-main { font-size: 19px; color: #0d1420; letter-spacing: -.035em; }
.np-word-accent {
  font-size: 19.5px;
  font-weight: 800;
  color: #a8751c;
  letter-spacing: -.01em;
}
.np-brand.is-light .np-word-main { color: #f4f8ff; }
.np-brand.is-light .np-word-accent { color: #ecd29a; }
.np-brand.is-compact .np-word-main,
.np-brand.is-compact .np-word-accent { font-size: 13.5px; }
</style>
