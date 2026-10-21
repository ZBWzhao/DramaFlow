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
/* 历史版本：和单卡工作台同尺寸同层级，切版时盖在它上面（z 高一档）。
 * 当前版本是「选中态」而不是「另一块颜色」：左边 accent 竖条 + 缩略图角标。 */
.studio-history {
  position: absolute;
  top: var(--sp-7);
  right: var(--sp-4);
  bottom: calc(var(--sp-7) + var(--sp-5));
  width: 348px;
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-4);
  color: var(--text);
  background: var(--panel);
  border: 1px solid var(--line-2);
  border-radius: var(--r-card);
  box-shadow: var(--elev-3);
  overflow-y: auto;
  z-index: calc(var(--z-node-ui) + 10);
  font: var(--fs-12) / var(--lh-base) var(--font-sans);
}
.studio-history-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-3); }
.studio-history-head b { display: block; color: var(--text); font-size: var(--fs-13); font-weight: 600; letter-spacing: -0.01em; }
.studio-history-head span {
  display: block;
  margin-top: 2px;
  color: var(--text-3);
  font: var(--fs-11) / 1.5 var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.studio-close {
  width: 24px;
  height: 24px;
  border: 1px solid transparent;
  border-radius: var(--r-btn);
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-13);
  line-height: 1;
  cursor: pointer;
}
.studio-close:hover { background: var(--card-2); border-color: var(--line); color: var(--text); }
.studio-history-empty {
  margin: 0;
  color: var(--text-3);
  font-size: var(--fs-11);
  line-height: 1.7;
}
.studio-history-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: var(--sp-2); }
.studio-history-list li {
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-2);
  border: 1px solid var(--line);
  border-radius: var(--r-btn);
  background: var(--card);
}
.studio-history-list li.current {
  border-color: var(--accent-line);
  background: color-mix(in srgb, var(--accent) 7%, var(--card));
  box-shadow: inset 2px 0 0 var(--accent);
}
.take-thumb {
  position: relative;
  flex: none;
  width: 88px;
  height: 52px;
  overflow: hidden;
  background: var(--bg);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.take-thumb img, .take-thumb video { width: 100%; height: 100%; object-fit: cover; display: block; }
.take-badge {
  position: absolute;
  left: var(--sp-1);
  bottom: var(--sp-1);
  padding: 0 var(--sp-1);
  border-radius: var(--r-pill);
  background: color-mix(in srgb, var(--bg) 82%, transparent);
  color: var(--accent);
  font: var(--fs-11) / 1.5 var(--font-mono);
}
.take-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: var(--sp-1); }
.take-line { display: flex; align-items: baseline; justify-content: space-between; gap: var(--sp-2); }
.take-model {
  color: var(--text);
  font: var(--fs-11) / 1.5 var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.take-time { flex: none; color: var(--text-3); font: var(--fs-11) / 1.5 var(--font-mono); }
.take-sub { display: flex; flex-wrap: wrap; gap: var(--sp-2); color: var(--text-3); font-size: var(--fs-11); }
.take-rating { color: var(--ok); }
.take-note { color: var(--text-2); }
</style>
