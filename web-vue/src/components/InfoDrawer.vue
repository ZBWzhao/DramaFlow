<script setup>
/* 信息抽屉：会话统计 / 技能 / 子 Agent / 工具。
 * 默认收起 —— 它们是"查得到但不必一直占屏"的东西（Toonflow 也是把设置塞进弹层）。
 */
import { computed } from "vue";
import { state } from "../store.js";

const stats = computed(() => state.stats || {});
const main = computed(() => stats.value.main || {});
const subUsage = computed(() => Object.entries(stats.value.sub_agents || {}));
</script>

<template>
  <aside class="info-drawer">
    <div class="info-section">
      <h3>
        会话统计
        <span class="dim">{{ state.sessionId || "未开始" }}</span>
      </h3>
      <div class="info-row"><span>轮次</span><b>{{ stats.runs ?? 0 }}</b></div>
      <div class="info-row"><span>压缩次数</span><b>{{ stats.compactions ?? 0 }}</b></div>
      <div class="info-row"><span>上下文</span><b>{{ stats.context_tokens ?? 0 }} / {{ stats.context_window ?? "?" }} tokens</b></div>
      <div class="info-row"><span>窗口占用</span><b>{{ stats.context_percent ?? 0 }}%</b></div>
      <div class="info-row"><span>主线 tokens</span><b>{{ main.total_tokens ?? 0 }}</b></div>
      <div v-for="[name, value] in subUsage" :key="name" class="info-row">
        <span>{{ name }}</span>
        <b>{{ value.last?.total_tokens ?? 0 }} / 累计 {{ value.cumulative?.total_tokens ?? 0 }}</b>
      </div>
    </div>

    <div class="info-section">
      <h3>
        可用技能
        <span class="dim">{{ state.skillChars.prompt }} / {{ state.skillChars.body }} 字</span>
      </h3>
      <ul class="info-list">
        <li v-for="skill in state.skills" :key="skill.name">
          <div class="name">{{ skill.name }}</div>
          <div class="desc">{{ skill.description }}（{{ skill.body_chars }} 字，按需读）</div>
        </li>
      </ul>
    </div>

    <div class="info-section">
      <h3>子 Agent</h3>
      <ul class="info-list">
        <li v-if="!state.subagents.length" class="dim">（本次还没有委派）</li>
        <li v-for="sub in state.subagents" :key="sub.name">
          <div class="name">{{ sub.name }} · {{ sub.interrupted ? `${sub.status} · 已中断` : sub.status }}</div>
          <div class="desc">
            增量 {{ sub.last?.total_tokens ?? 0 }} / 累计 {{ sub.cumulative?.total_tokens ?? 0 }} tokens
            <template v-if="sub.reports?.length">｜上报 {{ sub.reports.length }} 条</template>
          </div>
          <div v-if="sub.error" class="desc">错误：{{ sub.error }}</div>
        </li>
      </ul>
    </div>

    <div class="info-section">
      <h3>
        已装载工具
        <span class="dim">{{ state.workspace }}</span>
      </h3>
      <ul class="info-list">
        <li v-for="tool in state.tools" :key="tool.name">
          <div class="name">{{ tool.name }} · v{{ tool.version }}</div>
          <div class="desc">{{ tool.description || tool.load_error }}</div>
        </li>
      </ul>
    </div>
  </aside>
</template>
