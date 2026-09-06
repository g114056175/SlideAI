<template>
  <div ref="root" class="download-menu">
    <button
      type="button"
      class="btn download-menu-trigger"
      :disabled="!videoUrl"
      :aria-expanded="open"
      @click="toggle"
    >{{ label }} <span class="download-caret">▾</span></button>
    <div v-if="open" class="download-options" role="menu">
      <a :href="videoUrl" download role="menuitem" @click="close">下載影片</a>
      <a v-if="srtUrl" :href="srtUrl" download role="menuitem" @click="close">下載 SRT</a>
      <span v-else class="disabled-option">下載 SRT</span>
      <a v-if="bundleUrl" :href="bundleUrl" download role="menuitem" @click="close">下載全部</a>
      <span v-else class="disabled-option">下載全部</span>
    </div>
  </div>
</template>

<script setup>
import { watch } from 'vue'
import { useExclusiveMenu } from '../composables/useExclusiveMenu.js'

const props = defineProps({
  label: { type: String, default: '下載' },
  videoUrl: { type: String, default: '' },
  srtUrl: { type: String, default: '' },
  bundleUrl: { type: String, default: '' },
})

const { root, open, toggle, close } = useExclusiveMenu()
watch(() => props.videoUrl, close)
</script>

<style scoped>
.download-menu {
  position: relative;
  z-index: 14;
}

.download-menu-trigger {
  min-width: 110px;
  color: #bae6fd;
  border: 1px solid #0369a1;
  background: #0c2438;
  font-weight: 700;
}

.download-menu-trigger:hover:not(:disabled) {
  color: #ffffff;
  border-color: #38bdf8;
  background: #0c4a6e;
}

.download-caret {
  margin-left: 5px;
  font-size: 11px;
  opacity: 0.8;
}

.download-options {
  position: absolute;
  right: 0;
  bottom: calc(100% + 7px);
  width: 146px;
  padding: 5px;
  border: 1px solid #334155;
  border-radius: 9px;
  background: #0b1220;
  box-shadow: 0 14px 32px rgba(2, 6, 23, 0.45);
}

.download-options a,
.disabled-option {
  display: block;
  width: 100%;
  padding: 8px 10px;
  border-radius: 6px;
  color: #e2e8f0;
  text-decoration: none;
  font-size: 13px;
  font-weight: 500;
  text-align: left;
}

.download-options a:hover {
  color: #e2e8f0;
  background: #172033;
}

.disabled-option {
  color: #64748b;
  cursor: not-allowed;
}
</style>
