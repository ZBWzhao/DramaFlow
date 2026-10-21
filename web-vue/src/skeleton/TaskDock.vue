<script setup>
import { dock, elapsed, fmtElapsed, toggleDock } from "./appStore";
</script>

<template>
  <section class="dock" :class="{ 'is-open': !dock.collapsed }">
    <!-- 收起态：一颗胶囊，只留“在跑 + 已用时长” -->
    <button v-if="dock.collapsed" class="dock-pill" @click="toggleDock(false)">
      <span class="dock-ring" aria-hidden="true" />
      <span class="dock-pill-text">拆解中</span>
      <span class="dock-pill-time">{{ fmtElapsed(elapsed.s) }}</span>
      <span class="dock-caret" aria-hidden="true">▴</span>
    </button>

    <!-- 展开态：面板，普通浮层不锁页面 -->
    <div v-else class="dock-panel" role="dialog" aria-label="任务坞">
      <header class="dock-head">
        <strong>任务</strong>
        <span class="dock-note">跨页面保持运行 · 已切换 {{ dock.switched }} 次</span>
        <button class="dock-close" aria-label="收起任务坞" @click="toggleDock(true)">✕</button>
      </header>

      <ul class="dock-list">
        <li v-for="t in dock.tasks" :key="t.id" class="dock-task" :data-status="t.status">
          <div class="dock-task-top">
            <span class="dock-task-title">{{ t.title }}</span>
            <span class="dock-task-pct">
              {{ t.status === "queued" ? "排队" : Math.round(t.progress) + "%" }}
            </span>
          </div>
          <div class="dock-bar">
            <i :style="{ width: t.status === 'queued' ? '6%' : t.progress + '%' }" />
          </div>
          <div class="dock-task-foot">
            <span>{{ t.step }}</span>
            <span>{{ t.status === "running" ? "已用 " + fmtElapsed(elapsed.s) : "等待中" }}</span>
          </div>
        </li>
      </ul>

      <footer class="dock-foot">
        <span>切到「画布」看结果，计时与进度都不会中断</span>
        <button class="dock-link">去画布 →</button>
      </footer>
    </div>
  </section>
</template>
