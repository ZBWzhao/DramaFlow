<script setup>
/* 生成前审批面板：把「这一次要花什么」摊开给用户看，而不是弹一句"要开始吗"。
 * 数据全部来自 GET /api/canvas/generation-plan（后端解析后的 prompt/params/refs/ready_reason），
 * 这里只做展示，不改任何业务状态；确认动作交给父组件（store.runCanvas 里做 approvals 握手）。 */
import { computed } from "vue";
import { assetUrl } from "../utils.js";

const props = defineProps({
  review: { type: Object, default: null },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(["approve", "cancel"]);

const jobs = computed(() => {
  const list = Array.isArray(props.review?.jobs) ? props.review.jobs : [];
  const only = Array.isArray(props.review?.only) ? props.review.only : [];
  return only.length ? list.filter((job) => only.includes(job.node_id)) : list;
});
const readyCount = computed(() => jobs.value.filter((job) => job.ready).length);
const canApprove = computed(() => !jobs.value.length || readyCount.value > 0);
const isMock = computed(() => Boolean(props.review?.mock));

const SOURCE_LABEL = {
  prompt_node: "提示词节点",
  text_input: "文本输入",
  node: "卡片自身",
};

function sourceLabel(source) {
  return SOURCE_LABEL[source] || "";
}

/* 「6s / 9:16 / 720p / 1280x720」——契约要求生成前必须能读到这四个参数 */
function paramText(job) {
  const params = job?.params || {};
  const parts = [];
  if (params.duration) parts.push(`${params.duration}s`);
  if (params.ratio) parts.push(params.ratio);
  if (params.resolution) parts.push(String(params.resolution).toLowerCase());
  if (params.size) parts.push(params.size);
  return parts.join(" · ");
}

function refLabel(ref) {
  return ref?.label || ref?.node_id || "";
}
</script>

<template>
  <Teleport to="body">
    <div v-if="review" class="ap-veil" @click.self="emit('cancel')">
      <section class="ap-panel" role="dialog" aria-modal="true" aria-label="生成前确认">
        <header class="ap-head">
          <h2>生成前确认</h2>
          <span class="ap-model" :title="review.model || ''">{{ review.model || "默认模型" }}</span>
          <span class="ap-provider" :class="isMock ? 'is-mock' : 'is-cost'">
            {{ isMock ? "Mock · 不扣费" : "真实 Provider · 会产生费用" }}
          </span>
          <button class="ap-x" type="button" aria-label="关闭" title="关闭" @click="emit('cancel')">✕</button>
        </header>

        <div v-if="jobs.length" class="ap-summary">
          <b>{{ readyCount }}</b> / {{ jobs.length }} 张可生成
          <span class="ap-sep">·</span>
          参数由「节点数据 › 分镜镜头 › 项目设置」依次决定
        </div>

        <ul class="ap-jobs">
          <li v-for="job in jobs" :key="job.node_id" class="ap-job" :class="{ blocked: !job.ready }">
            <div class="ap-job-head">
              <span class="ap-kind">{{ job.kind === "video" ? "▷" : "◈" }}</span>
              <b class="ap-label">{{ job.label || job.node_id }}</b>
              <span v-if="sourceLabel(job.prompt_source)" class="ap-src">{{ sourceLabel(job.prompt_source) }}</span>
              <span class="ap-chars">{{ job.prompt_chars || 0 }} 字</span>
            </div>

            <p v-if="!job.ready" class="ap-reason">{{ job.ready_reason || "暂不可生成" }}</p>
            <p v-else-if="paramText(job)" class="ap-params">{{ paramText(job) }}</p>

            <div v-if="job.refs && job.refs.length" class="ap-refs">
              <span class="ap-refs-label">参考 {{ job.refs.length }}</span>
              <span v-for="ref in job.refs.slice(0, 8)" :key="`${ref.node_id}:${ref.path}`" class="ap-thumb" :title="refLabel(ref)">
                <img v-if="ref.path && String(ref.mime_type || '').startsWith('image')" :src="assetUrl(ref.path)" :alt="refLabel(ref)" />
                <template v-else>{{ ref.type === "VIDEO" ? "▷" : "◈" }}</template>
              </span>
            </div>
          </li>

          <li v-if="!jobs.length" class="ap-job">
            <div class="ap-job-head"><b class="ap-label">按当前项目参数提交</b></div>
            <p class="ap-params">该节点不进入批量生成计划，画幅 / 画质 / 时长以项目设置与节点数据为准。</p>
          </li>
        </ul>

        <footer class="ap-foot">
          <span class="ap-hint">确认后提交 1 次生成请求，结果异步回填</span>
          <button class="ap-btn" type="button" @click="emit('cancel')">取消</button>
          <button class="ap-btn ap-primary" type="button" :disabled="busy || !canApprove" @click="emit('approve')">
            {{ busy ? "提交中…" : "批准并开始生成" }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.ap-veil {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgb(0 0 0 / 55%);
  backdrop-filter: blur(2px);
}
.ap-panel {
  width: min(560px, calc(100vw - 48px));
  max-height: min(640px, calc(100vh - 64px));
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding: var(--sp-5);
  color: var(--text);
  background: var(--panel);
  border: 1px solid var(--line-2);
  border-radius: var(--r-card);
  box-shadow: var(--elev-3);
  font: var(--fs-13) / 1.5 var(--font-sans);
}
.ap-head {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}
.ap-head h2 {
  margin: 0;
  font-size: var(--fs-15);
  font-weight: 600;
  letter-spacing: -0.01em;
}
.ap-model,
.ap-provider {
  padding: var(--sp-1) var(--sp-3);
  border-radius: var(--r-pill);
  font: var(--fs-11) / 1.6 var(--font-mono);
  white-space: nowrap;
}
.ap-model {
  color: var(--text-2);
  background: var(--card-2);
  border: 1px solid var(--line);
}
.ap-provider.is-mock {
  color: var(--ok);
  background: rgb(69 192 138 / 10%);
  border: 1px solid rgb(69 192 138 / 22%);
}
.ap-provider.is-cost {
  color: var(--warn);
  background: rgb(255 138 92 / 10%);
  border: 1px solid rgb(255 138 92 / 24%);
}
.ap-x {
  margin-left: auto;
  width: 22px;
  height: 22px;
  color: var(--text-3);
  background: transparent;
  border: 0;
  border-radius: var(--r-btn);
  cursor: pointer;
}
.ap-x:hover { color: var(--text); background: var(--card-2); }
.ap-summary { color: var(--text-2); font-size: var(--fs-12); }
.ap-summary b { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
.ap-sep { margin: 0 var(--sp-1); color: var(--text-3); }
.ap-jobs {
  margin: 0;
  padding: 0;
  list-style: none;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: var(--r-nest);
}
.ap-job {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-4);
  background: var(--card);
}
.ap-job.blocked { box-shadow: inset 2px 0 0 var(--warn); }
.ap-job-head { display: flex; align-items: center; gap: var(--sp-3); min-width: 0; }
.ap-kind { color: var(--fam-image); font-size: var(--fs-12); }
.ap-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-12);
  font-weight: 500;
}
.ap-src,
.ap-chars {
  flex: none;
  color: var(--text-3);
  font: var(--fs-11) / 1.4 var(--font-mono);
}
.ap-chars { margin-left: auto; }
.ap-params {
  margin: 0;
  color: var(--text-2);
  font: var(--fs-11) / 1.5 var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.ap-reason {
  margin: 0;
  color: var(--warn);
  font: var(--fs-11) / 1.5 var(--font-sans);
  white-space: pre-wrap;
}
.ap-reason { color: var(--warn); white-space: pre-wrap; }
.ap-reason::before { content: "! "; color: var(--warn); }
.ap-refs { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.ap-refs-label { color: var(--text-3); font-size: var(--fs-11); }
.ap-thumb {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  overflow: hidden;
  color: var(--text-2);
  background: var(--card-2);
  border: 1px solid var(--line-2);
  border-radius: var(--r-btn);
  font-size: var(--fs-11);
}
.ap-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.ap-foot {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding-top: var(--sp-4);
  border-top: 1px solid var(--line);
}
.ap-hint { color: var(--text-3); font-size: var(--fs-11); margin-right: auto; }
.ap-btn {
  height: 30px;
  padding: 0 var(--sp-4);
  color: var(--text);
  background: var(--card-2);
  border: 1px solid var(--line-2);
  border-radius: var(--r-btn);
  font: inherit;
  font-size: var(--fs-12);
  cursor: pointer;
}
.ap-btn:hover:not(:disabled) { background: var(--card); border-color: var(--text-3); }
.ap-btn:disabled { color: var(--text-3); cursor: not-allowed; }
.ap-primary {
  color: var(--text-on-accent);
  background: var(--accent);
  border-color: var(--accent);
  font-weight: 600;
}
.ap-primary:hover:not(:disabled) { background: var(--accent); filter: brightness(1.08); }
.ap-primary:disabled { color: var(--text-3); background: var(--card-2); border-color: var(--line-2); }
</style>
