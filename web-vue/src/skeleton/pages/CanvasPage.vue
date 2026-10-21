<script setup>
import { ref } from "vue";

/** 骨架：左侧导轨默认收起 56px，hover 展开到 220px（不挤压画布，覆盖式） */
const railOpen = ref(false);
const inspectorOpen = ref(false);
const cards = [
  { id: "n1", kind: "prompt", title: "第 1 镜 · 提示词", x: 120, y: 96, w: 248 },
  { id: "n2", kind: "image", title: "苏晚 · 定妆", x: 470, y: 150, w: 248 },
  { id: "n3", kind: "video_gen", title: "第 1 镜 · 生成", x: 300, y: 380, w: 248 },
];
</script>

<template>
  <div class="page page-canvas">
    <div class="cv-dot" aria-hidden="true" />

    <!-- 左导轨：默认 56px 图标栏，hover 展开，覆盖画布不改变画布宽度 -->
    <aside
      class="cv-rail"
      :class="{ 'is-open': railOpen }"
      aria-label="节点与资产"
      @mouseenter="railOpen = true"
      @mouseleave="railOpen = false"
      @click="railOpen = true"
    >
      <ul class="cv-rail-list">
        <li class="cv-rail-item is-active"><span class="cv-rail-ico">◈</span><span class="cv-rail-label">节点</span></li>
        <li class="cv-rail-item"><span class="cv-rail-ico">⬡</span><span class="cv-rail-label">资产</span></li>
        <li class="cv-rail-item"><span class="cv-rail-ico">▤</span><span class="cv-rail-label">分集</span></li>
      </ul>
      <div class="cv-rail-foot">
        <button class="cv-rail-tool" title="搜索节点（⌘K）">⌘K</button>
      </div>
    </aside>

    <!-- 画布本体：全幅 -->
    <div class="cv-stage">
      <article
        v-for="c in cards"
        :key="c.id"
        class="cv-card"
        :data-kind="c.kind"
        :style="{ left: c.x + 'px', top: c.y + 'px', width: c.w + 'px' }"
        @click="inspectorOpen = true"
      >
        <span class="cv-card-strip" aria-hidden="true" />
        <header class="cv-card-cap">
          <span class="cv-card-glyph" aria-hidden="true">{{ c.kind === 'prompt' ? '✎' : c.kind === 'image' ? '▣' : '◉' }}</span>
          <span class="cv-card-title">{{ c.title }}</span>
        </header>
        <div class="cv-card-body">
          <span class="cv-card-empty">骨架占位</span>
        </div>
      </article>

      <svg class="cv-edges" width="100%" height="100%" aria-hidden="true">
        <path d="M 368 178 C 420 178 420 228 470 228" />
        <path d="M 594 232 C 650 232 650 420 548 420" />
      </svg>

      <!-- 右侧检查器：留白抽屉位，选中卡片或点顶部按钮时滑出 -->
      <aside class="cv-inspector" :class="{ 'is-open': inspectorOpen }" aria-label="检查器">
        <header class="cv-inspector-head">
          <span>检查器</span>
          <button class="btn-icon" aria-label="收起检查器" @click="inspectorOpen = false">✕</button>
        </header>
        <div class="cv-inspector-empty">
          <p>选中卡片后在这里看提示词、参数与历史版本。</p>
          <p class="muted">骨架阶段：右侧只保留 348px 抽屉位，不挤压画布。</p>
        </div>
      </aside>
    </div>

    <div class="cv-status" aria-label="状态栏">
      <span>节点 12</span>
      <span>选中 1</span>
      <span>缩放 95%</span>
      <span class="cv-status-grow" />
      <span>⌘K 搜索 · F 适应 · Space 拖动 · Del 删除</span>
    </div>

    <div class="cv-zoom">
      <button aria-label="缩小">−</button>
      <span>95%</span>
      <button aria-label="放大">＋</button>
      <button class="cv-fit" @click="inspectorOpen = !inspectorOpen">检查器</button>
    </div>
  </div>
</template>
