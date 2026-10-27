<script setup>
/* 对话流头像：36px 圆，两态。
 * user = accent 底白字「你」，靠右列（对齐由容器行决定）；
 * bot  = --card-2 底 + 工具图标，靠左列。图标全局注册（main.js），无需 import。 */
defineProps({ who: { type: String, default: "bot" } });
</script>

<template>
  <div class="avatar" :class="who">
    <span v-if="who === 'user'" class="you">你</span>
    <el-icon v-else :size="17"><Tools /></el-icon>
  </div>
</template>

<style scoped>
.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.avatar.user { background: var(--accent); }
.avatar.user .you { color: #fff; font-size: var(--fs-13); font-weight: 600; line-height: 1; }
.avatar.bot { background: var(--card-2); border: 1px solid var(--line); color: var(--text-2); }
</style>
