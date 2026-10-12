<script setup>
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { actions } from "../store.js";

const props = defineProps({ item: { type: Object, required: true } });
const custom = ref("");
const submitting = ref(false);

async function submit(answer) {
  const value = String(answer || "").trim();
  if (!value || submitting.value || props.item.status !== "pending") return;
  submitting.value = true;
  try {
    await actions.answerQuestion(props.item.questionId, value);
  } catch (err) {
    ElMessage.error(`提交回答失败：${err.message || err}`);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <section class="question-card" :class="{ answered: item.status === 'answered' }">
    <div class="question-head">
      <span class="question-badge">需要确认</span>
      <b>{{ item.title || "需要你的选择" }}</b>
      <span v-if="item.agent && item.agent !== 'main'" class="dim">来自子 Agent · {{ item.agent }}</span>
    </div>
    <p>{{ item.question }}</p>
    <div v-if="item.status === 'pending'" class="question-options">
      <el-button
        v-for="option in item.options"
        :key="option"
        size="small"
        :loading="submitting"
        @click="submit(option)"
      >
        {{ option }}
      </el-button>
    </div>
    <div v-if="item.status === 'pending' && item.allowCustom" class="question-custom">
      <el-input
        v-model="custom"
        size="small"
        placeholder="输入自定义回答"
        aria-label="自定义回答"
        @keyup.enter="submit(custom)"
      />
      <el-button size="small" type="primary" :disabled="!custom.trim()" :loading="submitting" @click="submit(custom)">
        回答
      </el-button>
    </div>
    <div v-else-if="item.status === 'answered'" class="question-answer">
      已回答：{{ item.answer }}
    </div>
    <div v-else class="dim">该问题已取消</div>
  </section>
</template>
