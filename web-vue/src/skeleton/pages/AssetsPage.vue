<script setup>
const groups = [
  { label: "角色", count: 10 },
  { label: "场景", count: 10 },
  { label: "道具", count: 11 },
  { label: "风格", count: 1 },
  { label: "声音", count: 0 },
];
const assets = [
  { name: "约书亚", kind: "角色", ready: true, refs: 3 },
  { name: "黑衣卫 A", kind: "角色", ready: true, refs: 1 },
  { name: "阶梯教室 · 无人版", kind: "场景", ready: true, refs: 2 },
  { name: "桌游店", kind: "场景", ready: false, refs: 0 },
  { name: "口琴", kind: "道具", ready: true, refs: 1 },
  { name: "全片美术风格", kind: "风格", ready: true, refs: 12 },
];
</script>

<template>
  <div class="page page-assets">
    <aside class="asset-nav" aria-label="资产类型">
      <div class="outline-head"><span>资产库</span><span class="outline-count">32</span></div>
      <ul class="outline-list">
        <li v-for="g in groups" :key="g.label" class="outline-item asset-nav-item" :class="{ active: g.label === '角色' }">
          <span class="outline-title">{{ g.label }}</span>
          <span class="muted">{{ g.count }}</span>
        </li>
      </ul>
      <div class="outline-foot">跨集共享 · 画布通过引用计数使用</div>
    </aside>

    <section class="asset-grid-wrap">
      <header class="asset-head">
        <h1>角色 <span class="muted">10</span></h1>
        <p class="muted">缩略图 + 名称 + 被引用的镜头数；「引用到画布」是显式动作。</p>
      </header>
      <div class="asset-grid">
        <article v-for="a in assets" :key="a.name" class="asset-card">
          <div class="asset-thumb" :data-ready="a.ready">
            <span class="asset-thumb-label">{{ a.ready ? "已就绪" : "缺图" }}</span>
          </div>
          <div class="asset-card-foot">
            <strong>{{ a.name }}</strong>
            <span class="muted">{{ a.kind }} · 引用 {{ a.refs }}</span>
          </div>
        </article>
      </div>
    </section>

    <aside class="asset-detail" aria-label="资产详情">
      <div class="drawer-head"><strong>资产详情</strong><span class="muted">选中后展示</span></div>
      <div class="asset-detail-empty">
        <p>形象 / 提示词 / 被引用的镜头列表</p>
        <p class="muted">引用到画布、重新生成</p>
      </div>
    </aside>
  </div>
</template>
