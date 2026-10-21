<script setup>
import { ref } from "vue";
import { RouterLink } from "vue-router";

const settingsOpen = ref(false);
const episodes = [
  { id: "EP01", title: "第 1 集 · 退婚", state: "已拆解", tone: "ok" },
  { id: "EP02", title: "第 2 集 · 我嫁给了他的小叔", state: "待拆解", tone: "muted" },
  { id: "EP03", title: "第 3 集 · 宴席上，他喊我小婶", state: "待拆解", tone: "muted" },
  { id: "EP04", title: "第 4 集 · 她的嫁妆，是我欠她的", state: "待拆解", tone: "muted" },
  { id: "EP05", title: "第 5 集 · 她不是苏家的女儿", state: "待拆解", tone: "muted" },
];
const paragraphs = [
  "苏晚（打断）：只是想要我的未婚夫，我的嫁妆，还有我苏家大小姐的位置。野心挺大，可惜演技太差。",
  "陆铭（护住林薇薇）：苏晚！薇薇比你有良心。你除了那张脸，还有什么？",
  "苏晚（笑，极冷）：还有脑子。你们忘了吗，陆氏上周的救命资金，用的是谁的账户？",
  "陆铭脸色骤变。",
  "苏晚（一步步逼近）：陆铭，退婚可以，今天走出这个门，明天开始，陆氏股价自己扛。",
];
</script>

<template>
  <div class="page page-script">
    <aside class="outline" aria-label="分集大纲">
      <div class="outline-head">
        <span>分集</span>
        <span class="outline-count">5</span>
      </div>
      <ul class="outline-list">
        <li v-for="ep in episodes" :key="ep.id" class="outline-item" :class="{ active: ep.id === 'EP01' }">
          <div class="outline-id">{{ ep.id }}</div>
          <div class="outline-main">
            <div class="outline-title">{{ ep.title }}</div>
            <span class="tag" :data-tone="ep.tone">{{ ep.state }}</span>
          </div>
        </li>
      </ul>
      <div class="outline-foot">字数 741 · 5 集</div>
    </aside>

    <section class="script-main">
      <div class="script-head">
        <div>
          <h1>剧本正文</h1>
          <p class="script-meta">741 字 · 5 集 · 只读正文，不做资产与分镜</p>
        </div>
        <button class="btn-ghost" @click="settingsOpen = true">项目设置</button>
      </div>

      <article class="script-body">
        <p v-for="(p, i) in paragraphs" :key="i">{{ p }}</p>
        <p class="script-note">— 正文区只有正文：资产、台词表、校验都已移出本页 —</p>
      </article>

      <div class="script-cta">
        <div>
          <strong>下一步：让 Agent 拆成本集分镜</strong>
          <p>不生成任何画面、不花钱；拆完进入画布逐镜调整。</p>
        </div>
        <RouterLink class="btn-cta" :to="{ name: 'canvas' }">开始制作<span aria-hidden="true">→</span></RouterLink>
      </div>
    </section>

    <transition name="fade">
      <div v-if="settingsOpen" class="drawer-mask" @click.self="settingsOpen = false">
        <aside class="drawer" aria-label="项目设置">
          <header class="drawer-head">
            <strong>项目设置</strong>
            <button class="btn-icon" aria-label="关闭" @click="settingsOpen = false">✕</button>
          </header>
          <div class="field"><label>画面比例</label><div class="field-box">9:16 竖屏</div></div>
          <div class="field"><label>画质</label><div class="field-box">480P</div></div>
          <div class="field"><label>地区 / 题材</label><div class="field-box">国内 · 职场</div></div>
          <div class="field"><label>全剧画风</label><div class="field-box">电影质感</div></div>
          <div class="field"><label>视频模型</label><div class="field-box">mock-video-v1</div></div>
          <p class="drawer-note">抽屉覆盖正文，不挤压阅读宽度；默认关闭。</p>
        </aside>
      </div>
    </transition>
  </div>
</template>
