<script setup>
/* 视频大图（P0-5）：卡太小看不清表演和运镜，双击卡片/点播放键进这里。
 * Esc 或点背景关闭；打开时锁住 body 滚动，避免画布跟着滑。
 */
import { onBeforeUnmount, onMounted } from "vue";
import { formatDuration } from "./video.js";

const props = defineProps({
  // { src, title, badge, duration, download, filename }
  video: { type: Object, required: true },
});
const emit = defineEmits(["close"]);

function onKey(event) {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  }
}

onMounted(() => {
  window.addEventListener("keydown", onKey);
  document.body.style.overflow = "hidden";
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKey);
  document.body.style.overflow = "";
});
</script>

<template>
  <Teleport to="body">
    <div
      class="cv2-lightbox"
      role="dialog"
      aria-modal="true"
      :aria-label="video.title || '视频预览'"
      @click.self="emit('close')"
    >
      <div class="lb-panel">
        <header class="lb-head">
          <span class="lb-title">{{ video.title || "视频" }}</span>
          <span v-if="video.badge" class="lb-badge">{{ video.badge }}</span>
          <span v-if="formatDuration(video.duration)" class="lb-time">{{ formatDuration(video.duration) }}</span>
          <span class="lb-spacer" />
          <a v-if="video.download" class="lb-download" :href="video.download" :download="video.filename || ''">
            下载
          </a>
          <button type="button" class="lb-close" aria-label="关闭" @click="emit('close')">✕</button>
        </header>
        <video class="lb-video" :src="video.src" controls autoplay playsinline @click.stop></video>
      </div>
    </div>
  </Teleport>
</template>
