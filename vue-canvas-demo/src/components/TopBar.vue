<script setup>
defineProps({ credits: { type: Number, default: 215 } });
defineEmits(["toggle-empty"]);
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true"></span>
      <span class="brand-word">DramaFlow</span>
    </div>
    <div class="crumbs">
      <span>未命名工作区</span>
      <i>/</i>
      <b>画布 1</b>
      <span class="crumb-caret">▾</span>
    </div>
    <div class="topbar-tools">
      <button class="icon-btn" title="模板">⧉</button>
      <button class="icon-btn" title="面板">▤</button>
    </div>
    <div class="topbar-right">
      <button class="icon-btn" title="分享">⇪</button>
      <button class="icon-btn" title="帮助">?</button>
      <button class="vip-pill" title="会员中心">
        <span class="vip-dot">◆</span> 会员中心
        <em>限时 4.5 折</em>
      </button>
      <div class="credits" title="积分"><span class="bolt">⚡</span> {{ credits }}</div>
      <button class="avatar" title="账户">Z</button>
      <button class="ghost-btn" title="切换空画布起始页（演示用）" @click="$emit('toggle-empty')">
        起始页
      </button>
    </div>
  </header>
</template>
