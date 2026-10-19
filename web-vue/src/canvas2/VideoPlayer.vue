<script setup>
/* 卡内视频播放器（P0-5）：不是「能播」就行，重点是几十个视频节点也不卡。
 *
 * 三层懒加载：
 *  1. 不在视口（外扩 160px）不挂 <video>，用占位骨架；
 *  2. 画布缩得太远（enabled=false）也不挂，只留一点点视觉提示；
 *  3. 挂了也只 preload="metadata"，用 #t=0.1 让浏览器取一小段画首帧，不整段下载。
 *
 * 交互：悬停静音循环预览（像剪辑软件里的素材卡），双击/点播放键打开大图；
 * prefers-reduced-motion 的用户不自动播放，避免眩晕。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { formatDuration, videoFrameUrl } from "./video.js";

const props = defineProps({
  src: { type: String, required: true },
  title: { type: String, default: "" },
  duration: { type: Number, default: 0 },
  enabled: { type: Boolean, default: true },
});

const emit = defineEmits(["open"]);

const rootEl = ref(null);
const videoEl = ref(null);
const inViewport = ref(false);
const ready = ref(false);
const failed = ref(false);
const hovering = ref(false);
const playing = ref(false);
const progress = ref(0);
const metaDuration = ref(0);
const attempt = ref(0);

let observer = 0;
let seekTimer = 0;
let readyTimer = 0;

const reducedMotion = (() => {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
})();

const shouldMount = computed(() => props.enabled && inViewport.value && !failed.value);
const timeLabel = computed(() => formatDuration(metaDuration.value || props.duration));
const frameUrl = computed(() => videoFrameUrl(props.src));

onMounted(() => {
  // 老浏览器/测试环境没有 IntersectionObserver 时退化成「进来就加载」，不算致命
  if (typeof IntersectionObserver !== "function") {
    inViewport.value = true;
    return;
  }
  observer = new IntersectionObserver(
    (entries) => {
      inViewport.value = Boolean(entries[0]?.isIntersecting);
    },
    { rootMargin: "160px" },
  );
  if (rootEl.value) observer.observe(rootEl.value);
});

onBeforeUnmount(() => {
  if (observer) observer.disconnect();
  window.clearTimeout(seekTimer);
  window.clearTimeout(readyTimer);
  stopPreview();
});

// 同一张卡切版本时复用组件：重置状态，让 <video> 重新挂载拿新素材
watch(
  () => props.src,
  () => {
    ready.value = false;
    failed.value = false;
    playing.value = false;
    progress.value = 0;
    metaDuration.value = 0;
    attempt.value = 0;
  },
);

/* 首帧：metadata 到了以后把播放头挪到 0.1s，触发一次很小的 range 请求 */
function onMeta() {
  const video = videoEl.value;
  if (!video) return;
  metaDuration.value = Number.isFinite(video.duration) ? Math.round(video.duration) : 0;
  window.clearTimeout(seekTimer);
  seekTimer = window.setTimeout(() => {
    try {
      if (video.readyState >= 1 && video.currentTime < 0.05) {
        const end = Number.isFinite(video.duration) ? video.duration : 1;
        video.currentTime = Math.min(0.1, Math.max(0.01, end / 10));
      }
    } catch {
      /* 个别编码不支持 seek，退回骨架也不影响使用 */
    }
  }, 60);
  // 兜底：有些浏览器只给 metadata 不给帧，4 秒后也要让播放键可用
  window.clearTimeout(readyTimer);
  readyTimer = window.setTimeout(() => {
    if (!failed.value) ready.value = true;
  }, 4000);
}

function onData() {
  ready.value = true;
  window.clearTimeout(readyTimer);
}

function onError() {
  failed.value = true;
  ready.value = false;
}

function startPreview() {
  if (!ready.value || reducedMotion) return;
  const video = videoEl.value;
  if (!video) return;
  try {
    video.muted = true;
    const played = video.play();
    if (played && typeof played.then === "function") {
      played.then(() => { playing.value = true; }).catch(() => { playing.value = false; });
    } else {
      playing.value = true;
    }
  } catch {
    playing.value = false;
  }
}

function stopPreview() {
  playing.value = false;
  progress.value = 0;
  const video = videoEl.value;
  if (!video) return;
  try {
    video.pause();
    if (video.readyState >= 1) video.currentTime = 0;
  } catch {
    /* 忽略：停止预览失败不影响卡片 */
  }
}

function onTime() {
  const video = videoEl.value;
  progress.value = video && video.duration ? Math.min(1, video.currentTime / video.duration) : 0;
}

function onEnter() {
  hovering.value = true;
  startPreview();
}

function onLeave() {
  hovering.value = false;
  stopPreview();
}

// 悬停时首帧往往还没 decode 完：ready 一到就补播，否则「鼠标已经放上去了却没动」
// 会让人以为播放器坏了（真浏览器验收时踩到过）。
watch(ready, (value) => {
  if (value && hovering.value && !playing.value) startPreview();
});

function retry() {
  failed.value = false;
  ready.value = false;
  attempt.value += 1;
}

function open() {
  emit("open");
}
</script>

<template>
  <div
    ref="rootEl"
    class="video-player"
    :class="{ ready, failed, playing }"
    @pointerenter="onEnter"
    @pointerleave="onLeave"
    @dblclick.stop="open"
  >
    <video
      v-if="shouldMount"
      :key="attempt"
      ref="videoEl"
      class="video-el"
      :src="frameUrl"
      preload="metadata"
      muted
      loop
      playsinline
      disablepictureinpicture
      @loadedmetadata="onMeta"
      @loadeddata="onData"
      @canplay="onData"
      @seeked="onData"
      @timeupdate="onTime"
      @error="onError"
    ></video>

    <!-- 还没到 / 还没加载出来：静态骨架，不做动画，避免一堆卡同时闪 -->
    <div v-if="!ready && !failed" class="video-skeleton" aria-hidden="true">
      <span class="video-glyph">▷</span>
    </div>

    <div v-if="failed" class="video-failed">
      <span>首帧加载失败</span>
      <button type="button" class="video-retry" @click.stop="retry">重试</button>
    </div>

    <button
      v-if="ready"
      type="button"
      class="video-play"
      :aria-label="title ? `播放 ${title}` : '播放视频'"
      @click.stop="open"
    >
      ▶
    </button>

    <span v-if="timeLabel" class="video-time">{{ timeLabel }}</span>
    <span v-if="playing" class="video-progress" aria-hidden="true">
      <i :style="{ width: `${Math.round(progress * 100)}%` }"></i>
    </span>
  </div>
</template>
