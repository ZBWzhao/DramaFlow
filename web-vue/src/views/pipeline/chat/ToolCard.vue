<script setup>
/* 工具调用折叠卡（stage 消息内原子件）。复刻 components/ToolPart.vue 视觉模式，
 * 但 props 驱动、不碰 store：part = {kind:"tool", name, argsSummary, preview, done}。
 * pipeline 事件只有摘要/预览，没有全量参数，所以展开区标题是「回执」。
 * 折叠头 = ▸ 旋转三角 + 图标 + 工具名 + argsSummary + 已完成/调用中；
 * part.done=false 显示旋转 Loading。展开 = preview <pre> + 复制按钮（区内滚防撑爆）。 */
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";

const props = defineProps({ part: { type: Object, required: true } });

const expanded = ref(false);
const previewText = computed(() => props.part.preview || "（无返回内容）");

async function copy() {
  try {
    await navigator.clipboard.writeText(props.part.preview || "");
    ElMessage.success("已复制回执");
  } catch {
    ElMessage.warning("复制失败，请手动选中");
  }
}
</script>

<template>
  <div class="tcard">
    <button class="tc-head" type="button" @click="expanded = !expanded">
      <span class="chev" :class="{ open: expanded }">▸</span>
      <el-icon class="icon" :class="{ spin: !part.done }">
        <Loading v-if="!part.done" />
        <Tools v-else />
      </el-icon>
      <b class="name">{{ part.name }}</b>
      <span class="args">{{ part.argsSummary }}</span>
      <span class="status" :class="part.done ? 'ok' : 'run'">{{ part.done ? "已完成" : "调用中" }}</span>
    </button>
    <div v-if="expanded" class="tc-body">
      <div class="io-head">
        <span>回执</span>
        <el-button link size="small" @click="copy">复制</el-button>
      </div>
      <pre class="io-block">{{ previewText }}</pre>
    </div>
  </div>
</template>

<style scoped>
/* 折叠卡骨架与 GateMarkers/CastPanel 折叠块同款：发丝边 + --card-2 底 + ▸ 三角 */
.tcard {
  border: 1px solid var(--line);
  border-radius: var(--r-1);
  background: var(--card-2);
  overflow: hidden;
}
.tc-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px var(--sp-2);
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
  color: var(--text-3);
  font-size: var(--fs-12);
  font-family: inherit;
}
.tc-head:hover { color: var(--text-2); }
.chev { display: inline-block; flex-shrink: 0; font-size: 10px; transition: transform 160ms var(--ease, ease); }
.chev.open { transform: rotate(90deg); }
.icon { color: var(--text-3); flex-shrink: 0; }
.icon.spin { animation: tc-spin 1s linear infinite; }
@keyframes tc-spin { to { transform: rotate(360deg); } }
.name { color: var(--text-2); font-weight: 500; flex-shrink: 0; }
.args {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
}
.status { flex-shrink: 0; font-size: var(--fs-11); }
.status.ok { color: var(--ok); }
.status.run { color: var(--warn); }

/* 展开体：回执 pre，--mono + word-break 防撑爆，区内滚（spec §5 超长内容） */
.tc-body { padding: 0 var(--sp-2) var(--sp-2) 24px; }
.io-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--text-3);
  font-size: var(--fs-11);
  padding: 2px 0 4px;
}
.io-block {
  margin: 0;
  padding: var(--sp-2);
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 6px;
  font-family: var(--mono);
  font-size: var(--fs-11);
  line-height: 1.6;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 240px;
  overflow-y: auto;
}
</style>
