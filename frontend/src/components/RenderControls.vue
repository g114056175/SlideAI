<template>
  <div class="render-controls">
    <div class="preview-actions-row">
      <button class="btn btn-success" :disabled="!hasSelectedSlide" @click="$emit('render-current')">
        {{ rendering ? '渲染中...' : (hasRenderedVideo ? '重新渲染' : '渲染此頁') }}
      </button>
      <button class="btn btn-dark render-all-btn" :disabled="!slidesCount || renderingAll" @click="$emit('render-all')">
        {{ renderingAll ? '全部渲染中...' : '渲染全部' }}
      </button>
      <button v-if="showStopAll" class="btn btn-danger" @click="$emit('stop-all')">
        終止渲染全部
      </button>
      <button
        v-for="pageIdx in cancellableSinglePages"
        :key="`cancel-page-${pageIdx}`"
        class="btn btn-outline-danger"
        @click="$emit('stop-page', pageIdx)"
      >
        終止: 第{{ pageIdx + 1 }}頁
      </button>
      <div ref="mergeRoot" class="merge-action">
        <button
          class="btn merge-btn"
          :disabled="!renderedCount"
          :aria-expanded="showMergeOptions"
          @click="toggleMerge"
        >合併匯出 <span class="merge-caret">▾</span></button>
        <div v-if="showMergeOptions" class="merge-options" role="menu">
          <button type="button" role="menuitem" @click="chooseMerge(false)">
            直接合併
          </button>
          <button type="button" role="menuitem" @click="chooseMerge(true)">
            自動轉場
          </button>
        </div>
      </div>
      <DownloadMenu
        label="下載本頁"
        :video-url="downloadVideoUrl"
        :srt-url="downloadSrtUrl"
        :bundle-url="downloadBundleUrl"
      />
    </div>
    <div class="alert py-2 mb-0 render-status-bar" :class="message ? 'alert-info' : 'alert-secondary'">
      {{ message || '待命：尚未開始渲染。' }}
    </div>
  </div>
</template>

<script setup>
import { computed, watch } from 'vue'
import { useExclusiveMenu } from '../composables/useExclusiveMenu.js'
import DownloadMenu from './DownloadMenu.vue'

const props = defineProps({
  hasSelectedSlide: { type: Boolean, default: false },
  hasRenderedVideo: { type: Boolean, default: false },
  renderedCount: { type: Number, default: 0 },
  slidesCount: { type: Number, default: 0 },
  rendering: { type: Boolean, default: false },
  renderingAll: { type: Boolean, default: false },
  queueLength: { type: Number, default: 0 },
  cancellableSinglePages: { type: Array, default: () => [] },
  message: { type: String, default: '' },
  downloadVideoUrl: { type: String, default: '' },
  downloadSrtUrl: { type: String, default: '' },
  downloadBundleUrl: { type: String, default: '' },
})

const emit = defineEmits(['render-current', 'render-all', 'stop-all', 'stop-page', 'merge'])

const showStopAll = computed(() => props.rendering || props.renderingAll || props.queueLength > 0)
const { root: mergeRoot, open: showMergeOptions, toggle: toggleMerge, close: closeMerge } = useExclusiveMenu()
watch(() => [props.renderedCount, props.downloadVideoUrl], closeMerge)

const chooseMerge = (transitionsEnabled) => {
  closeMerge()
  emit('merge', !!transitionsEnabled)
}
</script>

<style scoped>
.render-controls {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.preview-actions-row {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  padding-left: 14px;
  padding-right: 14px;
  flex-wrap: wrap;
  row-gap: 8px;
}

.render-all-btn {
  background: #0ea5e9;
  border-color: #0284c7;
  color: #ffffff;
}

.render-all-btn:hover:not(:disabled) {
  background: #0284c7;
  border-color: #0369a1;
  color: #ffffff;
}

.merge-action {
  position: relative;
  margin-left: auto;
  z-index: 15;
}

.merge-btn {
  min-width: 112px;
  color: #eff6ff;
  border: 1px solid #3b82f6;
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  box-shadow: 0 5px 14px rgba(37, 99, 235, 0.2);
}

.merge-btn:hover:not(:disabled) {
  color: #ffffff;
  border-color: #60a5fa;
  background: linear-gradient(135deg, #3b82f6, #2563eb);
}

.merge-caret {
  margin-left: 5px;
  font-size: 11px;
  opacity: 0.8;
}

.merge-options {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  width: 152px;
  padding: 6px;
  border: 1px solid #334155;
  border-radius: 10px;
  background: #0b1220;
  box-shadow: 0 18px 40px rgba(2, 6, 23, 0.48);
}

.merge-options::after {
  content: '';
  position: absolute;
  right: 26px;
  top: 100%;
  border: 7px solid transparent;
  border-top-color: #334155;
}

.merge-options button {
  width: 100%;
  display: flex;
  align-items: center;
  padding: 9px 10px;
  border: 0;
  border-radius: 7px;
  color: #e2e8f0;
  background: transparent;
  font-size: 13px;
  font-weight: 500;
  text-align: left;
}

.merge-options button:hover {
  background: #172033;
}

.preview-actions-row > .btn-success {
  color: #dbeafe;
  border-color: #475569;
  background: #1e293b;
}

.preview-actions-row > .btn-success:hover:not(:disabled) {
  border-color: #64748b;
  background: #334155;
}

.render-status-bar {
  min-height: 42px;
  display: flex;
  align-items: center;
  margin-left: 10px;
  margin-right: 10px;
}

@media (max-width: 900px) {
  .preview-actions-row {
    padding-left: 8px;
    padding-right: 8px;
  }

  .preview-actions-row .btn {
    flex: 0 1 auto;
  }

  .merge-action {
    margin-left: 0;
  }
}
</style>
