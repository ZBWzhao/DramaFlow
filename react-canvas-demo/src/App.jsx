import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EDGES, NODES, SIDEBAR_ITEMS } from "./data.js";

const CARD_W = 248;
const CAPTION_H = 24;

/* ---------------- 小工具 ---------------- */
const clamp = (v, min, max) => Math.min(max, Math.max(min, v));

function cardHeight(node) {
  if (node.plain) return 132;
  return CAPTION_H + Math.round(CARD_W * (9 / 16));
}

function nodeAnchor(node, side) {
  const h = cardHeight(node);
  const x = side === "left" ? node.x : node.x + CARD_W;
  return { x, y: node.y + h / 2 };
}

function edgePath(from, to) {
  const c = Math.max(40, Math.abs(to.x - from.x) * 0.45);
  return `M ${from.x} ${from.y} C ${from.x + c} ${from.y}, ${to.x - c} ${to.y}, ${to.x} ${to.y}`;
}

/* ---------------- 顶部栏 ---------------- */
function TopBar({ credits, onToggleEmpty }) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true" />
        <span className="brand-word">DramaFlow</span>
      </div>
      <div className="crumbs">
        <span>未命名工作区</span>
        <i>/</i>
        <b>画布 1</b>
        <span className="crumb-caret">▾</span>
      </div>
      <div className="topbar-tools">
        <button className="icon-btn" title="模板">
          ⧉
        </button>
        <button className="icon-btn" title="面板">
          ▤
        </button>
      </div>
      <div className="topbar-right">
        <button className="icon-btn" title="分享">
          ⇪
        </button>
        <button className="icon-btn" title="帮助">
          ?
        </button>
        <button className="vip-pill" title="会员中心">
          <span className="vip-dot">◆</span> 会员中心
          <em>限时 4.5 折</em>
        </button>
        <div className="credits" title="积分">
          <span className="bolt">⚡</span> {credits}
        </div>
        <button className="avatar" title="账户">
          Z
        </button>
        <button className="ghost-btn" onClick={onToggleEmpty} title="切换空画布起始页（演示用）">
          起始页
        </button>
      </div>
    </header>
  );
}

/* ---------------- 左侧目录 ---------------- */
function Sidebar({ tab, setTab, keyword, setKeyword, selectedId, onPick, total, hidden }) {
  const items = hidden ? [] : SIDEBAR_ITEMS.filter((item) => {
    if (tab === "assets" && item.tag !== "资产") return false;
    if (!keyword.trim()) return true;
    return (item.title + item.tag).toLowerCase().includes(keyword.trim().toLowerCase());
  });
  return (
    <aside className="sidebar">
      <div className="seg">
        <button className={tab === "canvas" ? "on" : ""} onClick={() => setTab("canvas")}>
          画布
        </button>
        <button className={tab === "assets" ? "on" : ""} onClick={() => setTab("assets")}>
          资产
        </button>
      </div>
      <div className="side-search">
        <span>⌕</span>
        <input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索节点" />
      </div>
      <div className="side-filter">
        <span>⚗</span>
        <span>所有评级</span>
        <span className="grow" />
        <span>☰</span>
      </div>
      <div className="side-list">
        {items.map((item) => (
          <button
            key={item.id}
            className={"side-item" + (item.id === selectedId ? " on" : "")}
            onClick={() => onPick(item.id)}
          >
            <span className="side-icon">{item.icon}</span>
            <span className="side-title">{item.title}</span>
            <span className="side-tag">{item.tag}</span>
          </button>
        ))}
        {!items.length && <p className="side-empty">画布暂无节点</p>}
      </div>
      <div className="side-foot">共 {total} 节点</div>
    </aside>
  );
}

/* ---------------- 卡片 ---------------- */
function Toolbar() {
  const tools = [
    ["人像质感调节", "NEW"],
    ["全景"],
    ["多角度"],
    ["打光"],
    ["九宫格", "▾"],
    ["高清", "▾"],
    ["元素编辑"],
    ["图层分离"],
    ["宫格切分"],
  ];
  return (
    <div className="node-toolbar">
      {tools.map(([label, badge]) => (
        <button key={label} className="tool-btn">
          {label}
          {badge && <em className={badge === "NEW" ? "badge-new" : "badge-caret"}>{badge}</em>}
        </button>
      ))}
      <span className="tool-divider" />
      <button className="tool-btn icon">↕</button>
      <button className="tool-btn icon">⤓</button>
      <button className="tool-btn icon">⤢</button>
    </div>
  );
}

function PromptBox({ node, onSend }) {
  const [text, setText] = useState(node.prompt || "");
  useEffect(() => setText(node.prompt || ""), [node.id, node.prompt]);
  return (
    <div className="prompt-box nodrag" onClick={(e) => e.stopPropagation()}>
      <div className="prompt-chips">
        <button className="chip">＋ 参考</button>
        <button className="chip">◷ 标记</button>
        <button className="chip">❖ 风格</button>
        <span className="grow" />
        <button className="chip ghost">⤢</button>
      </div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} placeholder="描述这一张要什么，或用 {{node:x}} 引用其它卡片" />
      <div className="prompt-foot">
        <span className="model-chip">❖ {node.model || "Lib Image 2.5 Pro"} ▾</span>
        <span className="sep" />
        <span className="size-chip">{node.size || "16:9 · 标准画质 · 2K · 1张"} ▾</span>
        <span className="grow" />
        <span className="prompt-icons">
          <b>A</b>
          <b>⌗</b>
          <b>15</b>
        </span>
        <button className="send" title="发送" onClick={() => onSend(node)}>
          ↑
        </button>
      </div>
    </div>
  );
}

function NodeCard({ node, selected, onSelect, onSend }) {
  const glyph = node.kind === "asset" ? "◈" : node.kind === "video" ? "▷" : node.kind === "text" ? "✎" : "▣";
  return (
    <div
      className={"node" + (selected ? " selected" : "") + (node.plain ? " plain" : "")}
      style={{ left: node.x, top: node.y, width: CARD_W }}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(node.id);
      }}
    >
      {selected && <Toolbar />}
      <div className="node-caption">
        <span className="cap-glyph">▣</span>
        <span className="cap-title">{node.title}</span>
        {node.dims ? <span className="cap-dims">{node.dims}</span> : null}
      </div>

      <div className={"node-body" + (node.plain ? " is-text" : "") + (node.video ? " is-video" : "")}>
        {node.plain ? (
          <p className="node-text">{node.prompt}</p>
        ) : (
          <>
            {node.img ? (
              <img src={node.img} alt={node.title} draggable="false" />
            ) : (
              <span className="node-empty-icon">▣</span>
            )}
            {node.img ? <span className="ai-tag">AI生成</span> : null}
            {node.video && (
              <>
                <button className="play-btn">▶</button>
                <span className="video-time">{node.duration}</span>
              </>
            )}
            {!node.video && node.attempts && (
              <div className="attempts">
                <span className="attempts-title">尝试：</span>
                {node.attempts.map((label) => (
                  <button key={label} className="attempt" onClick={(e) => e.stopPropagation()}>
                    ⬆ {label}
                  </button>
                ))}
              </div>
            )}
            <span className="handle left" title="连接">
              ＋
            </span>
            <span className="handle right" title="连接">
              ＋
            </span>
            <span className="node-menu" title="更多">
              ⋯
            </span>
          </>
        )}
      </div>

      {selected && <PromptBox node={node} onSend={onSend} />}
    </div>
  );
}

/* ---------------- 底部工具条 ---------------- */
function Dock({ onAdd, empty, onToggleEmpty }) {
  return (
    <div className="dock">
      <button className="dock-add" onClick={onAdd} title="添加节点">
        ＋
      </button>
      <button className="dock-btn on" title="选择">
        ⌖
      </button>
      <button className="dock-btn" title="组件">
        ⧉
      </button>
      <button className="dock-btn" title="表情">
        ☺
      </button>
      <button className="dock-btn" title="历史">
        ◷
      </button>
      <button className="dock-btn" title="命令">
        ⌨
      </button>
      <button className="dock-btn" onClick={onToggleEmpty} title={empty ? "回到画布" : "空画布演示"}>
        ?
      </button>
    </div>
  );
}

/* ---------------- 空画布起始页 ---------------- */
function Hero({ onPick }) {
  const pills = [
    ["image", "▣", "图片生成"],
    ["video", "▷", "视频生成"],
    ["audio", "♪", "音频生成"],
    ["text", "✎", "剧本生成"],
    ["timeline", "⧗", "智能剪辑"],
  ];
  return (
    <div className="hero">
      <p className="hero-hint">
        <span className="hero-cursor">➤</span> 双击画布 自由生成节点
      </p>
      <div className="hero-pills">
        {pills.map(([kind, glyph, label]) => (
          <button key={kind} className="hero-pill" onClick={() => onPick(kind)}>
            <span>{glyph}</span>
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------------- 主应用 ---------------- */
export default function App() {
  const [nodes, setNodes] = useState(NODES);
  const [selectedId, setSelectedId] = useState(null);
  const [tab, setTab] = useState("canvas");
  const [keyword, setKeyword] = useState("");
  const [empty, setEmpty] = useState(false);
  const [view, setView] = useState({ x: 0, y: 0, z: 0.62 });
  const [toast, setToast] = useState("");
  const [credits, setCredits] = useState(215);
  const stageRef = useRef(null);
  const panRef = useRef(null);

  const showToast = useCallback((text) => {
    setToast(text);
    window.setTimeout(() => setToast(""), 2200);
  }, []);

  /* 首次进入：把内容放进可视区 */
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const maxX = Math.max(...nodes.map((n) => n.x + CARD_W));
    const maxY = Math.max(...nodes.map((n) => n.y + cardHeight(n)));
    const z = clamp(Math.min(rect.width / (maxX + 160), rect.height / (maxY + 170)), 0.35, 1.05);
    setView({
      x: (rect.width - maxX * z) / 2,
      y: (rect.height - maxY * z) / 2,
      z,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toFlow = useCallback(
    (clientX, clientY) => {
      const rect = stageRef.current.getBoundingClientRect();
      return {
        x: (clientX - rect.left - view.x) / view.z,
        y: (clientY - rect.top - view.y) / view.z,
      };
    },
    [view],
  );

  /* 平移 */
  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    // 点在卡片上不启动平移：否则 pointer capture 会把 click 抢到 stage，
    // 卡片永远选不中（实测点击后 .node.selected 一直是 0）。
    if (e.target.closest?.(".node")) return;
    panRef.current = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e) => {
    if (!panRef.current) return;
    const { sx, sy, vx, vy } = panRef.current;
    setView((v) => ({ ...v, x: vx + (e.clientX - sx), y: vy + (e.clientY - sy) }));
  };
  const onPointerUp = () => {
    panRef.current = null;
  };

  /* 缩放（滚轮以指针为中心） */
  const onWheel = (e) => {
    e.preventDefault();
    const rect = stageRef.current.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    setView((v) => {
      const z = clamp(v.z * Math.exp(-e.deltaY * 0.0016), 0.15, 2.2);
      const k = z / v.z;
      return { z, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
    });
  };

  const addNode = useCallback(
    (clientX, clientY) => {
      const rect = stageRef.current.getBoundingClientRect();
      const p = toFlow(clientX ?? rect.left + rect.width / 2, clientY ?? rect.top + rect.height / 2);
      const id = "n" + Math.random().toString(36).slice(2, 7);
      const node = {
        id,
        kind: "gen",
        typeLabel: "图片生成",
        title: "图片节点 " + (nodes.filter((n) => n.kind === "gen").length + 1),
        dims: "2048 × 1152",
        x: Math.round(p.x - CARD_W / 2),
        y: Math.round(p.y - 100),
        attempts: ["图生图", "图片高清"],
        prompt: "",
        model: "Lib Image 2.5 Pro",
        size: "16:9 · 标准画质 · 2K · 1张",
      };
      setNodes((list) => [...list, node]);
      setSelectedId(id);
      setEmpty(false);
    },
    [nodes, toFlow],
  );

  const onPickSide = (id) => {
    setSelectedId(id);
    const node = nodes.find((n) => n.id === id);
    if (!node) return;
    const rect = stageRef.current.getBoundingClientRect();
    setView((v) => ({
      ...v,
      x: rect.width / 2 - (node.x + CARD_W / 2) * v.z,
      y: rect.height / 2 - (node.y + cardHeight(node) / 2) * v.z,
    }));
  };

  const onSend = (node) => {
    setCredits((c) => Math.max(0, c - 1));
    showToast(`已提交「${node.title}」`);
  };

  const edges = useMemo(() => {
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    return EDGES.filter(([a, b]) => byId[a] && byId[b]).map(([a, b], index) => (
      <path key={index} d={edgePath(nodeAnchor(byId[a], "right"), nodeAnchor(byId[b], "left"))} />
    ));
  }, [nodes]);

  return (
    <div className="app">
      <TopBar credits={credits} onToggleEmpty={() => setEmpty((v) => !v)} />
      <Sidebar
        tab={tab}
        setTab={setTab}
        keyword={keyword}
        setKeyword={setKeyword}
        selectedId={selectedId}
        onPick={onPickSide}
        total={empty ? 0 : nodes.length}
        hidden={empty}
      />

      <main
        ref={stageRef}
        className="stage"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
        onClick={(e) => {
          if (e.target.closest?.(".node")) return;
          setSelectedId(null);
        }}
        onDoubleClick={(e) => {
          if (e.target.closest(".node")) return;
          addNode(e.clientX, e.clientY);
        }}
      >
        <div className="grid-bg" style={{ backgroundSize: `${22 * view.z}px ${22 * view.z}px`, backgroundPosition: `${view.x}px ${view.y}px` }} />
        {!empty && (
          <div className="world" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }}>
            <svg className="edges" width={3600} height={2400}>
              {edges}
            </svg>
            {nodes.map((node) => (
              <NodeCard key={node.id} node={node} selected={node.id === selectedId} onSelect={setSelectedId} onSend={onSend} />
            ))}
          </div>
        )}

        {empty && (
          <Hero
            onPick={(kind) => {
              setEmpty(false);
              addNode();
            }}
          />
        )}

        <div className="zoom-pill">
          <button onClick={() => setView((v) => ({ ...v, z: clamp(v.z * 1.2, 0.15, 2.2) }))}>＋</button>
          <button onClick={() => setView((v) => ({ ...v, z: clamp(v.z / 1.2, 0.15, 2.2) }))}>−</button>
          <span>{Math.round(view.z * 100)}%</span>
          <button
            className="fit"
            title="适应窗口"
            onClick={() => {
              const rect = stageRef.current.getBoundingClientRect();
              const maxX = Math.max(...nodes.map((n) => n.x + CARD_W));
              const maxY = Math.max(...nodes.map((n) => n.y + cardHeight(n)));
              const z = clamp(Math.min(rect.width / (maxX + 160), rect.height / (maxY + 170)), 0.35, 1.05);
              setView({ x: (rect.width - maxX * z) / 2, y: (rect.height - maxY * z) / 2, z });
            }}
          >
            ⤢
          </button>
        </div>

        <Dock onAdd={() => addNode()} empty={empty} onToggleEmpty={() => setEmpty((v) => !v)} />
        <div className="count-pill">共 {empty ? 0 : nodes.length} 节点</div>
      </main>

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
