<template>
  <div class="home">
    <header class="home-top">
      <div class="home-brand">
        <span class="home-mark">TF</span>
        <div class="home-name">
          <b>DramaFlow</b>
          <small>AI 短剧制片台</small>
        </div>
      </div>
      <div class="home-top-right">
        <span v-if="shell.projects.length" class="home-hint">
          {{ shell.projects.length }} 个项目 · 累计 {{ totalSegments }} 个片段 · {{ totalDone }} 个已出片
        </span>
        <button class="btn primary" type="button" @click="shell.newOpen = true">＋ 新建项目</button>
      </div>
    </header>

    <main class="home-main">
      <section class="home-hero">
        <h1>从一本剧本，到一个能播的成片。</h1>
        <p>
          剧本自动分集 → 资产形象（角色三视图 / 场景 / 道具）→ 片段抽卡挑片 → 整集拼接导出。
          <br />每一步都为「这一集能不能交付」服务，而不是为「节点能不能跑通」服务。
        </p>
        <div class="home-hero-row">
          <span class="home-pill">9:16 竖屏</span>
          <span class="home-pill">Mock 兜底 · 真模型可切</span>
          <span class="home-pill">片段级成本护栏</span>
        </div>
      </section>

      <section class="home-block">
        <div class="home-block-head">
          <span>全部项目</span>
          <span class="sub">{{ shell.projects.length }}</span>
        </div>

        <div v-if="shell.error" class="home-error">
          {{ shell.error }}
          <button class="btn sm" type="button" @click="reload">重试</button>
        </div>

        <div v-else class="proj-grid">
          <article v-for="p in shell.projects" :key="p.canvas" class="proj-card" @click="enter(p)">
            <div class="proj-cover" :class="{ empty: !p.cover }">
              <img v-if="p.cover" :src="mediaUrl(p.cover)" :alt="p.title" loading="lazy" />
              <span v-else class="proj-cover-ph">暂无形象图</span>
              <span class="proj-badge">{{ p.aspect_ratio }} · {{ p.quality }}</span>
            </div>
            <div class="proj-body">
              <h3 :title="p.title">{{ p.title }}</h3>
              <div class="proj-sub">
                <span>{{ p.episodes }} 集</span>
                <span v-if="p.episode_titles?.length" class="proj-ep">{{ p.episode_titles[0] }}</span>
              </div>
              <div class="proj-bar" :title="`片段 ${p.segments_done}/${p.segments_total}`">
                <i :style="{ width: percent(p) + '%' }" />
              </div>
              <div class="proj-foot">
                <span>片段 {{ p.segments_done }}/{{ p.segments_total }}</span>
                <span>资产 {{ p.assets_ready }}/{{ p.assets_total }}</span>
              </div>
            </div>
            <button class="proj-del" type="button" title="删除项目" @click.stop="remove(p)">×</button>
          </article>

          <button class="proj-card proj-new" type="button" @click="shell.newOpen = true">
            <span class="proj-new-plus">＋</span>
            <span class="proj-new-label">新建项目</span>
            <span class="proj-new-hint">粘贴剧本，自动分集</span>
          </button>
        </div>

        <div v-if="shell.loading" class="home-loading">正在读取项目…</div>
      </section>
    </main>

    <NewProjectDialog v-if="shell.newOpen" @close="shell.newOpen = false" />
  </div>
</template>

<script setup>
import { computed, onMounted } from "vue";
import { shell, loadProjects, openProject } from "./shellStore.js";
import NewProjectDialog from "./NewProjectDialog.vue";
import "./shell.css";

const totalSegments = computed(() => shell.projects.reduce((n, p) => n + (p.segments_total || 0), 0));
const totalDone = computed(() => shell.projects.reduce((n, p) => n + (p.segments_done || 0), 0));

function mediaUrl(path) {
  const value = String(path || "");
  if (!value) return "";
  if (/^(https?:|data:)/.test(value)) return value;
  if (value.startsWith("assets/")) return `/${value}`;
  return `/assets/${value}`;
}

function percent(p) {
  const total = Number(p.segments_total || 0);
  if (!total) return 0;
  return Math.round(((Number(p.segments_done) || 0) / total) * 100);
}

function enter(p) {
  openProject(p.canvas);
}

async function remove(p) {
  if (!window.confirm(`删除项目「${p.title}」？画面数据会一起删掉。`)) return;
  await fetch(`/api/studio/projects/${encodeURIComponent(p.canvas)}`, { method: "DELETE" });
  await loadProjects();
}

function reload() {
  loadProjects();
}

onMounted(loadProjects);
</script>
