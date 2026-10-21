<script setup>
/* 卡片检查器（V2）：屏幕坐标系的浮层，宽度固定 620px，不随画布缩放变形。
 *
 * 数据全部来自已有画布数据（node / 上游节点 / 项目 meta），不新增接口：
 *  - 参考缩略图 = 直接上游的图片/视频节点
 *  - 素材职责 = 上游节点的标题 + 描述，按「图片 N 用于 …」排
 *  - 提示词 = node.prompt（保存仍走 /api/canvas/nodes/{id}/text）
 */
import { computed, ref, watch } from "vue";

const props = defineProps({
  node: { type: Object, required: true },
  refs: { type: Array, default: () => [] },
  project: { type: Object, default: () => ({}) },
  providerLabel: { type: String, default: "" },
  modelLabel: { type: String, default: "" },
});
const emit = defineEmits(["close", "focus", "send", "upload", "history", "detail", "retry"]);

const TABS = ["参考", "标记", "特效", "角色库", "运镜"];
const activeTab = ref("参考");

const text = ref(props.node.prompt || "");
watch(
  () => [props.node.id, props.node.prompt],
  () => { text.value = props.node.prompt || ""; },
);

const canSend = computed(() => text.value.trim().length > 0 || props.node.canGenerate);
const placeholder = computed(() => {
  if (props.node.canGenerate) return "可以直接上传，或在这里写一句镜头描述……";
  if (props.node.plain) return "写这段文字（Markdown 也支持）……";
  return "可以直接上传，或在这里写一句要求……";
});

const duties = computed(() => {
  const list = props.refs || [];
  if (!list.length) {
    return [{
      k: "",
      text: props.node.canGenerate
        ? "还没有参考素材。可以从上游图片/视频卡连入，或直接上传一张参考图。"
        : "这张卡没有上游素材。",
    }];
  }
  return list.slice(0, 5).map((ref, index) => {
    const label = ref.kind === "video" ? "视频" : ref.kind === "image" ? "图片" : "文本";
    const detail = ref.desc || "";
    const short = detail.length > 70 ? `${detail.slice(0, 70)}…` : detail;
    return {
      k: `${label} ${index + 1}`,
      text: short ? `用于${ref.title}。${short}` : `用于${ref.title}。`,
    };
  });
});

const paramsText = computed(() => {
  const ratio = props.project?.aspect_ratio || "16:9";
  const quality = String(props.project?.quality || "720P").toUpperCase();
  const seconds = props.node.seconds || 15;
  return `${ratio} · ${quality} · ${seconds}s · 1个`;
});

function send() {
  const value = text.value.trim();
  if (!canSend.value) return;
  emit("send", { node: props.node, text: value });
}
</script>

<template>
  <div class="cv2-inspector" @pointerdown.stop @click.stop @dblclick.stop>
    <div class="ins-head">
      <div
        v-for="tab in TABS"
        :key="tab"
        class="ins-tab"
        :class="{ on: activeTab === tab }"
        @click="activeTab = tab"
      >{{ tab }}</div>
      <div class="ins-tools">
        <button type="button" class="ins-ic" title="定位到这张卡" @click="emit('focus', node)">⤢</button>
        <button type="button" class="ins-ic" title="关闭（Esc）" @click="emit('close')">✕</button>
      </div>
    </div>

    <div class="ins-body">
      <template v-if="activeTab === '参考'">
        <div class="thumbs">
          <div v-for="(ref, index) in (refs || []).slice(0, 5)" :key="ref.id" class="thumb" :class="{ 'is-text': !ref.src }">
            <img v-if="ref.src" :src="ref.src" alt="" draggable="false" />
            <span v-else class="glyph">{{ ref.kind === "video" ? "▶" : "文" }}</span>
            <span class="no">{{ index + 1 }}</span>
            <span v-if="ref.duration" class="p">{{ ref.duration }}</span>
          </div>
          <button type="button" class="thumb add" title="上传参考" @click="emit('upload', node)">+</button>
        </div>

        <div class="ins-sec">
          <h4>【素材职责】</h4>
          <p v-for="(item, index) in duties" :key="index">
            <span v-if="item.k" class="k">{{ item.k }}</span>{{ item.text }}
          </p>
        </div>
      </template>

      <div v-else class="ins-sec ins-placeholder">
        <p>「{{ activeTab }}」还没有接入后端能力，先在「参考」里编辑这张卡的素材与提示词。</p>
      </div>

      <div class="promptbox">
        <textarea
          v-model="text"
          rows="3"
          spellcheck="false"
          :placeholder="placeholder"
          @keydown.enter.exact.prevent="send"
        ></textarea>
      </div>
    </div>

    <div class="ins-foot">
      <div class="chip" :title="providerLabel">{{ providerLabel || "占位" }}</div>
      <div class="chip">{{ modelLabel || "默认模型" }} <span class="car">▾</span></div>
      <div class="chip">{{ paramsText }} <span class="car">▾</span></div>
      <div class="foot-tools">
        <button type="button" class="ins-ic" title="详情" @click="emit('detail', node)">A</button>
        <button type="button" class="ins-ic" title="历史版本" @click="emit('history', node)">◈</button>
        <button type="button" class="ins-ic" title="上传素材" @click="emit('upload', node)">⇲</button>
        <button
          v-if="node.failed"
          type="button"
          class="ins-ic is-retry"
          title="重试"
          @click="emit('retry', node)"
        >!</button>
      </div>
      <button type="button" class="send" :disabled="!canSend" title="发送（Enter）" @click="send">↑</button>
    </div>
  </div>
</template>
