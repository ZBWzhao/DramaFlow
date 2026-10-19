<script setup>
/* 历史版本抽屉（P0，仿 AICON 的 CanvasGenerationHistoryDrawer，Apache-2.0，重写而非照搬）。
 *
 * 数据来源是节点上的 `data.takes`（服务端 studio 模块的既有结构），
 * 不是我们另造的一套版本表 —— 候选版本早就存在，只是画布这层从来没给入口。
 * 「设为当前版」调 /api/studio/segments/{id}/take，它会同步 outputs 指针。
 */
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { actions } from "../store.js";
import { assetUrl } from "../utils.js";

const props = defineProps({
  visible: { type: Boolean, default: false },
  node: { type: Object, default: null },
});
const emit = defineEmits(["close"]);

const takes = computed(() => {
  const list = props.node?.data?.takes;
  return Array.isArray(list) ? [...list].reverse() : [];
});
const currentTakeId = computed(() => props.node?.data?.current_take_id || "");
const busyId = ref("");

function isImage(take) {
  const mime = String(take?.mime || "");
  if (mime.startsWith("image")) return true;
  return /\.(png|jpe?g|webp|gif)$/i.test(String(take?.path || ""));
}

function timeLabel(take) {
  const stamp = Number(take?.created_at || 0);
  if (!stamp) return "";
  const date = new Date(stamp * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const modeLabel = { first_frame: "首帧", references: "参考图", canvas: "画布生成" };

async function choose(take) {
  if (!props.node || !take?.take_id || take.take_id === currentTakeId.value) return;
  busyId.value = take.take_id;
  try {
    await actions.selectTake(props.node.id, take.take_id);
    ElMessage.success("已切换为这一版");
    // 不自动关抽屉：切版之后人往往还要继续跟下一版对比，关掉反而要多点一次。
  } catch (err) {
    ElMessage.error(`切换失败：${err.message || err}`);
  } finally {
    busyId.value = "";
  }
}
</script>

<template>
  <aside v-if="visible && node" class="studio-history" aria-label="历史版本">
    <header class="studio-history-head">
      <div>
        <b>历史版本</b>
        <span>{{ takes.length }} 版 · {{ node.label }}</span>
      </div>
      <button type="button" class="studio-close" title="关闭（Esc）" aria-label="关闭历史版本" @click="emit('close')">×</button>
    </header>

    <p v-if="!takes.length" class="studio-history-empty">
      这张卡还没有历史版本。点「生成」跑一次之后，每一版都会留在这里，可以随时切回去。
    </p>

    <ul v-else class="studio-history-list">
      <li v-for="take in takes" :key="take.take_id" :class="{ current: take.take_id === currentTakeId }">
        <div class="take-thumb">
          <img v-if="isImage(take)" :src="assetUrl(take.path)" :alt="take.note || '历史版本'" loading="lazy" />
          <video v-else :src="assetUrl(take.path)" muted preload="metadata" />
          <span v-if="take.take_id === currentTakeId" class="take-badge">当前</span>
        </div>
        <div class="take-meta">
          <div class="take-line">
            <span class="take-model">{{ take.model || "未知模型" }}</span>
            <span class="take-time">{{ timeLabel(take) }}</span>
          </div>
          <div class="take-sub">
            <span v-if="modeLabel[take.mode]">{{ modeLabel[take.mode] }}</span>
            <span v-if="take.rating" class="take-rating">{{ { good: "可用", great: "更好" }[take.rating] || take.rating }}</span>
            <span v-if="take.note" class="take-note">{{ take.note }}</span>
          </div>
          <el-button
            v-if="take.take_id !== currentTakeId"
            size="small"
            text
            :loading="busyId === take.take_id"
            @click="choose(take)"
          >设为当前版</el-button>
        </div>
      </li>
    </ul>
  </aside>
</template>

<style scoped>
.studio-history {
  position: absolute;
  top: 58px;
  /* 与单卡工作台同处右侧：它是工作台的上一层，切版时盖在工作台上 */
  right: 14px;
  bottom: 78px;
  width: 348px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: linear-gradient(180deg, rgba(23, 28, 37, 0.98), rgba(15, 19, 26, 0.99));
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(14px);
  overflow-y: auto;
  z-index: 8;
}
.studio-history-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
.studio-history-head b { display: block; color: var(--text); font-size: 13px; }
.studio-history-head span { color: var(--text-3); font-size: 10.5px; }
.studio-close {
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--text-2);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
}
.studio-close:hover { background: rgba(255, 255, 255, 0.12); color: var(--text); }
.studio-history-empty { margin: 0; color: var(--text-3); font-size: 11px; line-height: 1.7; }
.studio-history-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 8px; }
.studio-history-list li {
  display: flex;
  gap: 9px;
  padding: 7px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.025);
}
.studio-history-list li.current { border-color: rgba(124, 196, 255, 0.5); background: rgba(124, 196, 255, 0.08); }
.take-thumb { position: relative; flex: none; width: 88px; height: 52px; border-radius: 7px; overflow: hidden; background: #0a0d13; }
.take-thumb img, .take-thumb video { width: 100%; height: 100%; object-fit: cover; display: block; }
.take-badge {
  position: absolute;
  left: 4px;
  bottom: 4px;
  padding: 0 5px;
  border-radius: 999px;
  background: rgba(8, 11, 16, 0.78);
  color: #7cc4ff;
  font-size: 9px;
}
.take-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.take-line { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; }
.take-model { color: var(--text); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.take-time { flex: none; color: var(--text-3); font-size: 10px; }
.take-sub { display: flex; flex-wrap: wrap; gap: 6px; color: var(--text-3); font-size: 10px; }
.take-rating { color: #58c9a0; }
.take-note { color: var(--text-2); }
</style>
