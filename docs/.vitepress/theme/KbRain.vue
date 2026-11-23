<script setup lang="ts">
// The home page's background animation. Every 1.5-2.8 s an invisible wave starts somewhere and runs
// across the whole screen; what it passes lights up, easing in, bright near the start and weaker far
// away. One wave lights a code graph (nodes and edges), the next a field of vectors (embeddings). Idle
// elements are at 2.5% opacity, lit ones at most ~50%. Canvas, no library; colors come from the theme's
// CSS variables (--kbbg-*). With "reduce motion" on, or without JS, the static SVG (hero-bg.svg) is shown.
import { onBeforeUnmount, onMounted, ref } from 'vue'
import heroBg from './hero-bg.svg?raw'

const canvas = ref<HTMLCanvasElement | null>(null)
const still = ref(true)
let raf = 0
let stop = () => {}

type Node = { x: number; y: number; r: number; hue: number; glow: number; to: number }
type Edge = { a: number; b: number; glow: number }
type Vec = { x: number; y: number; a: number; len: number; glow: number; to: number; turn: number }
type Ring = { x: number; y: number; r: number; max: number; mode: 0 | 1; life: number }

function rng(seed: number) {
  return () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296)
}

onMounted(() => {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  const cv = canvas.value
  if (!cv) return
  const ctx = cv.getContext('2d')
  if (!ctx) return
  still.value = false

  let W = 0, H = 0, dpr = 1
  let nodes: Node[] = [], edges: Edge[] = [], vecs: Vec[] = []
  const rings: Ring[] = []
  let colors = { node: '#2563eb', node2: '#7c3aed', edge: '#94a3b8', vec: '#0ea5e9', dot: '#cbd5e1' }
  let nextMode: 0 | 1 = 0
  let lastWave = 0
  let visible = true

  const readColors = () => {
    const s = getComputedStyle(document.documentElement)
    const v = (n: string, d: string) => s.getPropertyValue(n).trim() || d
    colors = { node: v('--kbbg-node', colors.node), node2: v('--kbbg-node2', colors.node2),
               edge: v('--kbbg-edge', colors.edge), vec: v('--kbbg-vec', colors.vec), dot: v('--kbbg-dot', colors.dot) }
  }

  const build = () => {
    const r = rng(7)
    const x0 = 0
    nodes = []
    // a few clusters, like projects: dense inside, a couple of links between them
    const centers = [[0.08, 0.2], [0.3, 0.12], [0.58, 0.3], [0.78, 0.22], [0.7, 0.62], [0.9, 0.55],
                     [0.52, 0.72], [0.15, 0.7], [0.35, 0.88], [0.88, 0.88]]
    for (const [cx, cy] of centers) {
      const n = 12 + Math.floor(r() * 6)
      for (let i = 0; i < n; i++) {
        const ang = r() * Math.PI * 2, d = 20 + r() * 95
        nodes.push({ x: cx * W + Math.cos(ang) * d * 1.3, y: cy * H + Math.sin(ang) * d, r: 2 + r() * 2.6,
                     hue: r() < 0.3 ? 1 : 0, glow: 0, to: 0 })
      }
    }
    nodes = nodes.filter(n => n.x > x0 - 40 && n.x < W + 20 && n.y > 10 && n.y < H - 10)
    edges = []
    nodes.forEach((n, i) => {
      const near = nodes.map((m, j) => [j, (m.x - n.x) ** 2 + (m.y - n.y) ** 2] as [number, number])
        .filter(([j]) => j !== i).sort((a, b) => a[1] - b[1]).slice(0, 2 + Math.floor(r() * 2))
      for (const [j, d2] of near) if (d2 < 140 ** 2 && i < j) edges.push({ a: i, b: j, glow: 0 })
    })
    for (let k = 0; k < 5; k++) {          // cross-project links
      const a = Math.floor(r() * nodes.length), b = Math.floor(r() * nodes.length)
      if (a !== b) edges.push({ a, b, glow: 0 })
    }
    vecs = []
    const step = 54
    for (let y = step / 2; y < H; y += step) {
      for (let x = x0; x < W; x += step) {
        const a = Math.sin(x * 0.006) * 1.2 + Math.cos(y * 0.008) * 0.9 - 0.6
        vecs.push({ x: x + (r() - 0.5) * 10, y: y + (r() - 0.5) * 10, a, len: 9 + r() * 9, glow: 0, to: 0, turn: 0 })
      }
    }
  }

  const resize = () => {
    const rect = cv.getBoundingClientRect()
    dpr = Math.min(window.devicePixelRatio || 1, 2)
    W = rect.width; H = rect.height
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    build()
  }

  // under the hero's text (left half, top 70%) a lit element reaches only half brightness
  const calm = (x: number, y: number) => (x < W * 0.5 && y < Math.min(H * 0.7, 620) ? 0.5 : 1)
  const alpha = (hex: string, a: number) => {
    if (!hex.startsWith('#')) return hex
    const h = hex.length === 4 ? hex.slice(1).split('').map(c => c + c).join('') : hex.slice(1, 7)
    const n = parseInt(h, 16)
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`
  }

  const spawn = (t: number) => {
    if (t - lastWave < 1500 + Math.random() * 1300) return
    lastWave = t
    // a wave starts by itself somewhere on the page (no falling drop)
    rings.push({ x: W * (0.04 + Math.random() * 0.92), y: H * (0.08 + Math.random() * 0.84), r: 2,
                 max: Math.hypot(W, H), mode: nextMode, life: 1 })
    nextMode = nextMode === 0 ? 1 : 0
  }

  let lastT = 0
  const frame = (t: number) => {
    raf = requestAnimationFrame(frame)
    if (!visible) { lastT = t; return }
    // time-based: a slow or throttled tab moves the same distance per second, just in fewer steps
    const k = lastT ? Math.min(4, (t - lastT) / 16.7) : 1
    lastT = t
    spawn(t)
    ctx.clearRect(0, 0, W, H)

    // rings light up what they cross
    for (const g of rings) {
      g.r += 1.6 * k; g.life = Math.pow(1 - g.r / g.max, 1.6)   // bright at first, fading as it spreads
      const band = 30 + 1.6 * k
      if (g.mode === 0) {
        nodes.forEach(n => {
          const d = Math.hypot(n.x - g.x, n.y - g.y)
          if (Math.abs(d - g.r) < band) n.to = Math.max(n.to, g.life * calm(n.x, n.y))
        })
      } else {
        vecs.forEach(v => {
          const d = Math.hypot(v.x - g.x, v.y - g.y)
          if (Math.abs(d - g.r) < band) {
            v.to = Math.max(v.to, g.life * calm(v.x, v.y))
            v.turn = Math.atan2(v.y - g.y, v.x - g.x)   // the wave pushes the vector outwards
          }
        })
      }
    }
    for (let i = rings.length - 1; i >= 0; i--) if (rings[i].r > rings[i].max) rings.splice(i, 1)
    edges.forEach(e => { e.glow = Math.min(nodes[e.a].glow, nodes[e.b].glow) })

    // vectors: faint field, bright where the wave passed
    for (const v of vecs) {
      v.to *= Math.pow(0.992, k); v.glow += (v.to - v.glow) * Math.min(1, 0.06 * k)   // eases in, then fades
      const a = v.glow > 0.02 ? v.a + (v.turn - v.a) * v.glow * 0.6 : v.a
      const l = v.len * (1 + v.glow * 0.8)
      const x2 = v.x + Math.cos(a) * l, y2 = v.y + Math.sin(a) * l
      ctx.strokeStyle = alpha(colors.vec, 0.025 + v.glow * 0.45)
      ctx.lineWidth = 1 + v.glow * 1.2
      ctx.beginPath(); ctx.moveTo(v.x, v.y); ctx.lineTo(x2, y2); ctx.stroke()
      const h = 3 + v.glow * 3
      ctx.beginPath()
      ctx.moveTo(x2, y2)
      ctx.lineTo(x2 - Math.cos(a - 0.5) * h, y2 - Math.sin(a - 0.5) * h)
      ctx.moveTo(x2, y2)
      ctx.lineTo(x2 - Math.cos(a + 0.5) * h, y2 - Math.sin(a + 0.5) * h)
      ctx.stroke()
    }

    // graph: edges then nodes
    for (const e of edges) {
      const A = nodes[e.a], B = nodes[e.b]
      ctx.strokeStyle = alpha(colors.edge, 0.025 + e.glow * 0.45)
      ctx.lineWidth = 1 + e.glow * 1.3
      ctx.beginPath(); ctx.moveTo(A.x, A.y); ctx.lineTo(B.x, B.y); ctx.stroke()
    }
    for (const n of nodes) {
      n.to *= Math.pow(0.992, k); n.glow += (n.to - n.glow) * Math.min(1, 0.06 * k)
      const c = n.hue ? colors.node2 : colors.node
      if (n.glow > 0.05) {
        ctx.fillStyle = alpha(c, n.glow * 0.12)
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 8 * n.glow, 0, Math.PI * 2); ctx.fill()
      }
      ctx.fillStyle = alpha(c, 0.025 + n.glow * 0.47)
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r * (1 + n.glow * 0.6), 0, Math.PI * 2); ctx.fill()
    }

  }

  readColors(); resize()
  const ro = new ResizeObserver(resize); ro.observe(cv)
  const mo = new MutationObserver(readColors)
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting })
  io.observe(cv)
  raf = requestAnimationFrame(frame)
  stop = () => { cancelAnimationFrame(raf); ro.disconnect(); mo.disconnect(); io.disconnect() }
})

onBeforeUnmount(() => stop())
</script>

<template>
  <div class="kb-hero-bg" aria-hidden="true">
    <div v-if="still" class="kb-hero-still" v-html="heroBg" />
    <canvas ref="canvas" class="kb-hero-canvas" :class="{ on: !still }" />
  </div>
</template>
