/*
 * 自研极简画布：DOM 节点 + SVG 贝塞尔连线 + transform 视口。
 *
 * 为什么不用 Vue Flow / React Flow（对应笔记里的决策 2）：
 *   1. 现状前端是零构建 vanilla JS，引库要带构建链，成本翻倍
 *   2. 画布的核心原理只有三件事：坐标、连线路径、视口变换 —— 自己写反而讲得清
 *
 * 坐标系统：
 *   节点位置 = 世界坐标（存在画布 JSON 里，服务端是唯一写入方）
 *   世界 → 屏幕 = 外层容器一个 transform: translate(vx,vy) scale(z)
 *   所以缩放/平移只需要改容器的 transform，节点自身不用重算
 */

/** 工作区相对路径（assets/...）→ 可访问 URL（/assets/...）。 */
function assetUrl(path) {
  return "/assets/" + String(path || "").replace(/^\/?assets\//, "").replace(/^\//, "");
}

const CanvasView = {
  NODE_W: 220, // Toonflow nodeSkeleton: 宽 220 固定
  NODE_H: 104, // 标题条 28 + 卡体最小 76
  HEAD_H: 28, // 卡体外上方的标题条高度
  PORT_TOP: 20, // 第一个端口相对卡体顶部的偏移
  PORT_GAP: 22, // 端口间距（in/out 各最多 2 个，够用）

  s: {
    name: "main",
    nodes: [],
    edges: [],
    nodeSpec: {},
    topo: [],
    problems: [],
    view: { x: 60, y: 60, zoom: 1 },
    selected: null,
    loaded: false,
  },

  init() {
    this.el = {
      stage: document.getElementById("canvas-stage"),
      world: document.getElementById("canvas-world"),
      svg: document.getElementById("canvas-edges"),
      nodes: document.getElementById("canvas-nodes"),
      detail: document.getElementById("canvas-detail"),
      title: document.getElementById("canvas-title"),
      meta: document.getElementById("canvas-meta"),
      problems: document.getElementById("canvas-problems"),
    };

    // 空白拖拽 = 平移视口
    this.el.stage.addEventListener("pointerdown", (e) => {
      if (e.target !== this.el.stage && e.target !== this.el.world) return;
      const start = { x: e.clientX, y: e.clientY, vx: this.s.view.x, vy: this.s.view.y };
      const move = (ev) => {
        this.s.view.x = start.vx + (ev.clientX - start.x);
        this.s.view.y = start.vy + (ev.clientY - start.y);
        this.applyTransform();
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });

    // 滚轮缩放：以鼠标位置为锚点（否则会往左上角跑）
    this.el.stage.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const rect = this.el.stage.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        const next = Math.min(2.5, Math.max(0.25, this.s.view.zoom * factor));
        const ratio = next / this.s.view.zoom;
        this.s.view.x = mx - (mx - this.s.view.x) * ratio;
        this.s.view.y = my - (my - this.s.view.y) * ratio;
        this.s.view.zoom = next;
        this.applyTransform();
      },
      { passive: false }
    );

    document.getElementById("canvas-picker").onchange = (e) => this.load(e.target.value);
    document.getElementById("canvas-run").onclick = () => this.runCanvas();
    document.getElementById("canvas-reload").onclick = () => this.load(this.s.name);
    document.getElementById("canvas-fit").onclick = () => this.fit();
    document.getElementById("canvas-arrange").onclick = () => this.arrange();
    document.getElementById("canvas-export").onclick = () => this.exportPrompts();
    document.getElementById("export-close").onclick = () =>
      document.getElementById("export-modal").classList.add("hidden");
    document.getElementById("export-copy").onclick = async (e) => {
      await navigator.clipboard.writeText(document.getElementById("export-body").textContent);
      e.target.textContent = "已复制";
      setTimeout(() => (e.target.textContent = "复制"), 1200);
    };
  },

  // 导出全片提示词：{{ref N}} 由服务端展开成参考声明，这里只负责展示
  async exportPrompts() {
    const modal = document.getElementById("export-modal");
    const body = document.getElementById("export-body");
    const meta = document.getElementById("export-path");
    modal.classList.remove("hidden");
    body.textContent = "导出中…";
    meta.textContent = "";
    const res = await fetch("/api/canvas/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canvas: this.s.name }),
    });
    const data = await res.json();
    if (!res.ok) {
      body.textContent = `导出失败：${data.detail || res.status}`;
      return;
    }
    meta.textContent = `${data.path}（${data.segments} 个片段，${data.chars} 字）`;
    body.textContent = data.markdown;
  },

  async initPicker() {
    await this.loadCanvases();
    const picker = document.getElementById("canvas-picker");
    if (picker.options.length && !picker.value) this.s.name = picker.options[0].value;
  },

  applyTransform() {
    const { x, y, zoom } = this.s.view;
    this.el.world.style.transform = `translate(${x}px, ${y}px) scale(${zoom})`;
  },

  async loadCanvases(current) {
    const res = await fetch("/api/canvases");
    const data = await res.json();
    const picker = document.getElementById("canvas-picker");
    picker.innerHTML = (data.canvases || []).map((n) => `<option value="${n}">${n}</option>`).join("");
    if (current) picker.value = current;
  },

  async load(name = "main") {
    const res = await fetch(`/api/canvas?name=${encodeURIComponent(name)}`);
    const data = await res.json();
    this.s.name = data.name;
    if (![...document.getElementById("canvas-picker").options].some((o) => o.value === data.name)) {
      await this.loadCanvases(data.name); // 新画布（比如 Agent 刚建的）自动进下拉框
    } else {
      document.getElementById("canvas-picker").value = data.name;
    }
    this.s.nodes = data.nodes || [];
    this.s.edges = data.edges || [];
    this.s.nodeSpec = data.node_spec || {};
    this.s.topo = data.topo_order || [];
    this.s.problems = data.problems || [];
    this.s.loaded = true;
    if (!data.exists || !this.s.nodes.length) {
      if (!this.s.nodes.length) this.s.view = { x: 60, y: 60, zoom: 1 };
    }
    this.render();
    this.fit();
  },

  // ------------------------------------------------------------------
  // 端口锚点：把「节点 + 端口名」换算成世界坐标
  // ------------------------------------------------------------------
  portsOf(node, side) {
    const spec = this.s.nodeSpec[node.type];
    if (!spec) return [];
    return Object.keys(side === "out" ? spec.out || {} : spec.in || {});
  },

  anchor(node, handle, side) {
    const ports = this.portsOf(node, side);
    const index = Math.max(ports.indexOf(handle), 0);
    return {
      x: side === "out" ? node.position.x + this.NODE_W : node.position.x,
      // 端口在卡体内部，卡体顶部要加上标题条高度
      y: node.position.y + this.HEAD_H + this.PORT_TOP + index * this.PORT_GAP,
    };
  },

  // ------------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------------
  render() {
    const { nodes, edges } = this.s;
    if (this.el.title) this.el.title.textContent = `画布「${this.s.name}」`;
    if (this.el.meta) this.el.meta.textContent = `${nodes.length} 节点 · ${edges.length} 连线`;

    // 连线：三次贝塞尔（水平方向控制点，越远越平缓）
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const paths = [];
    for (const e of edges) {
      const src = byId.get(e.source);
      const dst = byId.get(e.target);
      if (!src || !dst) continue;
      const a = this.anchor(src, e.source_handle, "out");
      const b = this.anchor(dst, e.target_handle, "in");
      const dx = Math.max(46, Math.abs(b.x - a.x) * 0.45);
      const d = `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
      const hot = this.s.selected && (e.source === this.s.selected || e.target === this.s.selected);
      paths.push(
        `<path class="edge${hot ? " hot" : ""}" d="${d}" marker-end="url(#arrow)"><title>${this.escape(
          (src.label || src.id) + "." + e.source_handle + " → " + (dst.label || dst.id) + "." + e.target_handle
        )}</title></path>`
      );
    }
    this.el.svg.innerHTML = `<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor"/></marker></defs>${paths.join("")}`;

    // 节点
    this.el.nodes.innerHTML = "";
    for (const node of nodes) {
      this.el.nodes.appendChild(this.buildNode(node));
    }
    this.showProblems();
  },

  buildNode(node) {
    const spec = this.s.nodeSpec[node.type] || { out: {}, in: {} };
    const el = document.createElement("div");
    el.className = `cnode type-${node.type} status-${node.status}${this.s.selected === node.id ? " selected" : ""}`;
    el.style.left = `${node.position.x}px`;
    el.style.top = `${node.position.y}px`;
    el.dataset.id = node.id;

    // 标题条在卡体**上方**（照 Toonflow nodeSkeleton：类型 + 状态，卡体独立）
    const head = document.createElement("div");
    head.className = "cnode-head";
    // 标题用**短名**（"资产（角色/场景/道具/风格）" → "资产"），全名放 title
    const fullType = spec.label || node.type;
    const shortType = fullType.split("（")[0];
    head.innerHTML =
      `<span class="cnode-type" title="${fullType}">${shortType}</span>` +
      `<span class="cnode-status">${node.status}</span>`;
    el.appendChild(head);

    // 卡体：标签 + 端口 + 产物都装在这里，端口相对它定位
    const body = document.createElement("div");
    body.className = "cnode-body";
    const label = document.createElement("div");
    label.className = "cnode-label";
    label.textContent = node.label;
    body.appendChild(label);

    // 有产物就显示缩略图（生成节点跑完后就有）
    const media = this.mediaOf(node);
    if (media) {
      el.classList.add("has-media");
      const wrap = document.createElement("div");
      wrap.className = "cnode-media";
      const img = document.createElement("img");
      // 产物路径存的是工作区相对路径（assets/generated/x.png），拼 /assets 时要剥掉前缀，
      // 否则会变成 /assets/assets/... 而 404 —— 这个 bug 曾经让所有缩略图一直加载失败
      img.src = assetUrl(media.path);
      img.alt = node.label; // 不加 loading="lazy"：首屏就要看到产物，否则像"图没了"
      wrap.appendChild(img);
      if (media.mock) {
        const badge = document.createElement("span");
        badge.className = "mock-badge";
        badge.textContent = "mock 占位";
        wrap.appendChild(badge);
      }
      body.appendChild(wrap);
    }

    // 端口：左边输入、右边输出
    const addPorts = (side) => {
      const ports = this.portsOf(node, side);
      ports.forEach((handle, index) => {
        const type = side === "out" ? spec.out[handle] : spec.in[handle];
        const dot = document.createElement("span");
        dot.className = `port port-${side} port-${type}`;
        dot.style.top = `${this.PORT_TOP + index * this.PORT_GAP - 5}px`;
        dot.title = `${handle} (${type})`;
        body.appendChild(dot);
        const label = document.createElement("span");
        label.className = `port-label port-label-${side}`;
        label.style.top = `${this.PORT_TOP + index * this.PORT_GAP - 7}px`;
        label.textContent = handle;
        body.appendChild(label);
      });
    };
    addPorts("in");
    addPorts("out");
    el.appendChild(body);

    el.addEventListener("pointerdown", (e) => this.dragNode(e, node, el));
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      this.select(node.id);
    });
    return el;
  },

  // 拖动节点：实时更新位置与连线，松手回写服务端（画布状态在服务端）
  dragNode(e, node, el) {
    if (e.target.classList.contains("port") || e.target.classList.contains("port-label")) return;
    e.stopPropagation();
    const start = { x: e.clientX, y: e.clientY, nx: node.position.x, ny: node.position.y };
    let moved = false;
    const move = (ev) => {
      const dx = (ev.clientX - start.x) / this.s.view.zoom;
      const dy = (ev.clientY - start.y) / this.s.view.zoom;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
      node.position = { x: Math.round(start.nx + dx), y: Math.round(start.ny + dy) };
      el.style.left = `${node.position.x}px`;
      el.style.top = `${node.position.y}px`;
      this.renderEdgesOnly();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (moved) this.savePosition(node);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  },

  async savePosition(node) {
    await fetch(`/api/canvas/nodes/${node.id}/position`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canvas: this.s.name, x: node.position.x, y: node.position.y }),
    });
  },

  renderEdgesOnly() {
    const byId = new Map(this.s.nodes.map((n) => [n.id, n]));
    const paths = [];
    for (const e of this.s.edges) {
      const src = byId.get(e.source);
      const dst = byId.get(e.target);
      if (!src || !dst) continue;
      const a = this.anchor(src, e.source_handle, "out");
      const b = this.anchor(dst, e.target_handle, "in");
      const dx = Math.max(46, Math.abs(b.x - a.x) * 0.45);
      paths.push(`<path class="edge" d="M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}"/>`);
    }
    const defs = this.el.svg.querySelector("defs");
    this.el.svg.innerHTML = defs ? defs.outerHTML : "";
    this.el.svg.insertAdjacentHTML("beforeend", paths.join(""));
  },

  /** 从节点 outputs 里找产物路径（video 节点存的是首帧图）。 */
  mediaOf(node) {
    const outputs = node.outputs || {};
    for (const port of ["image", "video", "audio"]) {
      const value = (outputs[port] || {}).value;
      if (value && value.path) return { port, path: value.path, mock: !!value.mock };
    }
    return null;
  },

  // 一键生成：按拓扑序把画布上的生成节点跑一遍（服务端负责顺序）
  async runCanvas() {
    const btn = document.getElementById("canvas-run");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "生成中…";
    try {
      const payload = {
        canvas: this.s.name,
        approved: true,
        provider: "mock",
        model: "mock-video-v1",
        duration: 5,
        resolution: "720p",
        ratio: "16:9",
        generate_audio: false,
      };
      const approvalRes = await fetch("/api/canvas/approvals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const approval = await approvalRes.json();
      if (!approvalRes.ok) {
        this.toast(`<span class="err">审批失败：${approval.detail || approvalRes.status}</span>`);
        return;
      }
      payload.approval_id = approval.approval_id;
      const res = await fetch("/api/canvas/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        this.toast(`<span class="err">生成失败：${data.detail || res.status}</span>`);
        return;
      }
      await this.load(this.s.name);
      const lines = data.results.map((r) =>
        r.ok ? `<span class="ok">✓</span> ${r.label} → ${r.path}` : `<span class="err">✗</span> ${r.label}：${r.error}`
      );
      this.toast(`生成完成 <b>${data.done}/${data.total}</b>\n${lines.join("\n")}`);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  },

  toast(html) {
    const box = document.getElementById("canvas-toast");
    box.classList.remove("hidden");
    box.innerHTML = `<div class="grow">${html}</div><button class="btn ghost" id="toast-close">关闭</button>`;
    document.getElementById("toast-close").onclick = () => box.classList.add("hidden");
  },

  // ------------------------------------------------------------------
  // 详情 / 体检 / 布局
  // ------------------------------------------------------------------
  select(nodeId) {
    this.s.selected = nodeId;
    const node = this.s.nodes.find((n) => n.id === nodeId);
    if (!node) return;
    const refs = this.s.edges
      .filter((e) => e.target === nodeId)
      .map((e) => this.s.nodes.find((n) => n.id === e.source))
      .filter(Boolean);
    const rows = [
      ["id", node.id],
      ["类型", `${node.type}（${(this.s.nodeSpec[node.type] || {}).label || ""}）`],
      ["状态", node.status],
      ["标签", node.label],
    ];
    const dataLines = Object.entries(node.data || {})
      .map(([k, v]) => `  ${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
      .join("\n");
    const media = this.mediaOf(node);
    const mediaBlock = media
      ? `<h4>产物${media.mock ? "（mock 占位）" : ""}</h4>
         <a href="${assetUrl(media.path)}" target="_blank"><img class="detail-media" src="${assetUrl(media.path)}" alt="${this.escape(node.label)}"></a>
         <p class="dim">${this.escape(media.path)}</p>`
      : "";
    const content = node.data && node.data.content_path
      ? `<h4>正文</h4><p class="dim">${this.escape(node.data.content_path)}（用 read_file 读）</p>`
      : "";
    this.el.detail.innerHTML = `
      <h3>${this.escape(node.label)}</h3>
      <table class="detail-table">${rows.map(([k, v]) => `<tr><td>${k}</td><td>${this.escape(String(v))}</td></tr>`).join("")}</table>
      ${mediaBlock}
      ${content}
      ${dataLines ? `<h4>data</h4><pre>${this.escape(dataLines)}</pre>` : ""}
      ${refs.length ? `<h4>上游依赖</h4><ul>${refs.map((n) => `<li>${this.escape(n.label)}</li>`).join("")}</ul>` : ""}
      <h4>在拓扑序中的位置</h4>
      <p class="dim">第 ${this.s.topo.indexOf(nodeId) + 1} / ${this.s.topo.length} 步</p>
    `;
    this.render();
  },

  showProblems() {
    const box = this.el.problems;
    if (!this.s.problems.length) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    box.innerHTML = `<b>体检发现 ${this.s.problems.length} 个问题</b><ul>${this.s.problems
      .slice(0, 5)
      .map((p) => `<li>${this.escape(p)}</li>`)
      .join("")}</ul>`;
  },

  /**
   * 整理成流程图：交给服务端按拓扑分层重排。
   *
   * 为什么不在前端算：位置是画布数据的一部分（存在 JSON 里，服务端权威），
   * 前端算完还得逐个 PATCH 回去 —— 一次请求搞定更省事，也避免两边算法不一致。
   */
  async arrange() {
    const btn = document.getElementById("canvas-arrange");
    btn.disabled = true;
    try {
      const res = await fetch("/api/canvas/arrange", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canvas: this.s.name }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        this.toast(`<span class="err">整理失败：${data.detail || res.status}</span>`);
        return;
      }
      await this.load(this.s.name);
    } finally {
      btn.disabled = false;
    }
  },

  /** 节点实际占的高度：带缩略图的生成节点明显更高（决定包围盒，不能用统一值）。 */
  nodeHeight(node) {
    const base = this.HEAD_H + 76;
    return this.mediaOf(node) ? base + 122 : base;
  },

  /** 适应窗口：把全部节点在当前可用区域里居中铺满。布局未稳定（rect 为 0）时等一帧重试。 */
  fit(retries = 3) {
    if (!this.s.nodes.length) {
      this.s.view = { x: 60, y: 60, zoom: 1 };
      this.applyTransform();
      return;
    }
    const rect = this.el.stage.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      if (retries > 0) requestAnimationFrame(() => this.fit(retries - 1));
      return;
    }
    const pad = 70;
    const safeTop = 76; // 顶部浮动条（品牌 + 视图切换）占的高度
    const safeBottom = 96; // 左下控件组 / 右下详情卡
    const minX = Math.min(...this.s.nodes.map((n) => n.position.x)) - pad;
    const maxX = Math.max(...this.s.nodes.map((n) => n.position.x + this.NODE_W)) + pad;
    const minY = Math.min(...this.s.nodes.map((n) => n.position.y)) - pad;
    const maxY = Math.max(...this.s.nodes.map((n) => n.position.y + this.nodeHeight(n))) + pad;

    const availH = Math.max(200, rect.height - safeTop - safeBottom);
    const zoom = Math.min(1.1, Math.max(0.25, Math.min(rect.width / (maxX - minX), availH / (maxY - minY))));
    this.s.view = {
      zoom,
      x: (rect.width - (maxX - minX) * zoom) / 2 - minX * zoom,
      y: safeTop + (availH - (maxY - minY) * zoom) / 2 - minY * zoom,
    };
    this.applyTransform();
  },

  escape(text) {
    return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  },
};
