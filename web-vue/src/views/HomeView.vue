<script setup>
/* 首页 · 海报墙（demo v4 屏0）。
 *
 * 数据沿用 shellStore.loadProjects（/api/studio/projects）；
 * 进入项目 = openProject 的状态重置 + 路由跳转（旧 shell.view 字段留 Legacy 不再读）。
 */
import { computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { loadProjects, openProject, shell } from "../shell/shellStore.js";

const router = useRouter();

const FX = ["fx-a", "fx-b", "fx-c", "fx-d"];

const totalSegments = computed(() => shell.projects.reduce((n, p) => n + (p.segments_total || 0), 0));
const making = computed(() => shell.projects.filter((p) => (p.segments_done || 0) > 0).length);

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
  router.push(`/project/${p.canvas}/script`);
}

async function remove(p) {
  if (!window.confirm(`删除项目「${p.title}」？画面数据会一起删掉。`)) return;
  await fetch(`/api/studio/projects/${encodeURIComponent(p.canvas)}`, { method: "DELETE" });
  await loadProjects();
}

onMounted(loadProjects);
</script>

<template>
  <div class="home">
    <div class="home-in">
      <div class="home-head">
        <h1>项 目</h1>
        <span class="sub num">{{ shell.projects.length }} 部短剧 · {{ making }} 部制作中 · 累计 {{ totalSegments }} 段</span>
        <div class="grow"></div>
      </div>

      <div v-if="shell.error" class="home-error">
        {{ shell.error }}
        <button class="btn sm" type="button" @click="loadProjects">重试</button>
      </div>
      <div v-else-if="shell.loading" class="home-loading">正在读取项目…</div>

      <div v-else class="poster-grid">
        <article v-for="(p, i) in shell.projects" :key="p.canvas" class="poster" @click="enter(p)">
          <div class="im">
            <img v-if="p.cover" :src="mediaUrl(p.cover)" :alt="p.title" loading="lazy" />
            <div v-else class="fx" :class="FX[i % FX.length]"></div>
            <div class="veil"></div>
            <div class="ttl">{{ p.title }}</div>
            <div class="meta num">
              <span>{{ p.episodes }} 集</span>
              <span>{{ p.segments_done }}/{{ p.segments_total }} 段</span>
              <span>{{ p.assets_ready }}/{{ p.assets_total }} 资产</span>
            </div>
            <button class="del" type="button" title="删除项目" @click.stop="remove(p)">×</button>
          </div>
          <div class="bar"><i :style="{ width: percent(p) + '%' }"></i></div>
          <div class="foot">
            <span class="num">{{ p.aspect_ratio }} · {{ p.quality }}</span>
            <span class="st" :class="p.segments_done > 0 ? 'ok' : 'mute'"><i></i>{{ p.segments_done > 0 ? "制作中" : "草稿" }}</span>
          </div>
        </article>

        <button class="poster new" type="button" @click="shell.newOpen = true">＋ 新建短剧</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.home { overflow-y: auto; flex: 1; height: 100%; }
.home-in { max-width: 1200px; margin: 0 auto; padding: var(--sp-6) var(--sp-6) 96px; }
.home-head { display: flex; align-items: baseline; gap: var(--sp-4); padding-bottom: var(--sp-4); }
.home-head h1 { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; letter-spacing: .04em; }
.home-head .sub { color: var(--text-3); font-size: var(--fs-12); }
.home-head .grow { flex: 1; }
.home-loading, .home-error { padding: var(--sp-6) 0; color: var(--text-3); font-size: var(--fs-13); }
.home-error { color: var(--bad); display: flex; align-items: center; gap: var(--sp-3); }

.poster-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: var(--sp-4); }
.poster { cursor: pointer; }
.poster .im { aspect-ratio: 9/13; border-radius: var(--r-2); position: relative; overflow: hidden; border: 1px solid var(--line); transition: transform var(--dur) var(--ease), border-color var(--dur) var(--ease); }
.poster:hover .im { transform: translateY(-2px); border-color: var(--line-2); }
.poster .im img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.poster .im .fx { position: absolute; inset: 0; }
.fx-a { background: linear-gradient(165deg, #2A2622, #171310 55%, #1F1A14); }
.fx-b { background: linear-gradient(195deg, #241F2B, #131017 60%, #191420); }
.fx-c { background: linear-gradient(150deg, #1C2320, #0F1310 65%, #15201A); }
.fx-d { background: linear-gradient(170deg, #2B2320, #171210 50%, #241A16); }
.poster .im .veil { position: absolute; inset: 0; background: linear-gradient(180deg, transparent 40%, rgba(8, 7, 6, .88)); }
.poster .im .ttl { position: absolute; left: 12px; right: 12px; bottom: 26px; font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; line-height: 1.5; }
.poster .im .meta { position: absolute; left: 12px; right: 12px; bottom: 8px; font-size: var(--fs-11); color: var(--text-2); display: flex; gap: 8px; }
.poster .del {
  position: absolute; top: 6px; right: 6px; width: 22px; height: 22px; border-radius: 50%;
  background: rgba(8, 7, 6, .6); color: var(--text-3); font-size: 13px; line-height: 1;
  opacity: 0; transition: opacity var(--dur) var(--ease);
}
.poster:hover .del { opacity: 1; }
.poster .del:hover { color: var(--bad); }
.poster .bar { margin-top: 8px; height: 2px; background: var(--card-3); }
.poster .bar i { display: block; height: 100%; background: var(--ok); }
.poster .foot { display: flex; justify-content: space-between; margin-top: 6px; font-size: var(--fs-11); color: var(--text-3); }
.st { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-11); color: var(--text-3); }
.st i { width: 6px; height: 6px; border-radius: 50%; flex: none; }
.st.ok i { background: var(--ok); }
.st.ok { color: var(--ok); }
.st.mute i { background: var(--text-3); }
.poster.new {
  aspect-ratio: 9/13; border: 1px dashed var(--line-2); border-radius: var(--r-2);
  display: grid; place-items: center; color: var(--text-3); font-size: var(--fs-12); background: none;
}
.poster.new:hover { color: var(--text-2); border-color: var(--line-3); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }
</style>
