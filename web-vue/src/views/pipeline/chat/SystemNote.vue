<script setup>
/* 居中系统行：hello 接入 / retry 重试 / F5 恢复提示等非对话消息。
 * 规格（spec §3）：居中、--fs-11、--text-3。 */
defineProps({ text: { type: String, default: "" } });
</script>

<template>
  <div class="note">{{ text }}</div>
</template>

<style scoped>
.note {
  text-align: center;
  color: var(--text-3);
  font-size: var(--fs-11);
  line-height: 1.6;
  padding: 2px 0;
}
</style>
