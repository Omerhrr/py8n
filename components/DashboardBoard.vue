<script setup lang="ts">
// Shared dashboard board renderer (v31) - used by the builder's live preview
// and the public /d/{slug} page. Input: the RENDERED component payload from
// POST /dashboards/{ref}/preview or GET /dashboards/{slug}/runtime.
import { computed, useAttrs, onMounted, ref } from 'vue'

const props = defineProps<{
  components: any[]
  accent?: string // tailwind gradient classes for bars
  // v47 cross-filtering - only the public /d/{slug} page passes these (and a
  // @segment-click listener); the builder passes neither, so rendering there
  // stays byte-identical to v46.
  groupBys?: Record<string, string> // chart id -> group_by column, learned from the board config
  activeFilters?: Record<string, string[]> // active cross-filters, for the active-segment highlight
  // v54 drilldown target: when set (from ?c= on the runtime), that component
  // card gets a highlight ring and the page scrolls to it.
  highlightId?: string
}>()

const HIGHLIGHT_CLASS = 'ring-2 ring-sky-400/70 ring-offset-2 ring-offset-zinc-950'
const didScroll = ref(false)
onMounted(() => {
  if (props.highlightId && !didScroll.value) {
    didScroll.value = true
    setTimeout(() => {
      document.getElementById(`comp-${props.highlightId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 350)
  }
})

function compClass(id: string): string {
  return props.highlightId && props.highlightId === id ? HIGHLIGHT_CLASS : ''
}

// Optional parent callback: @segment-click="..." on the component tag. Read
// from attrs (not defineEmits) so the handler stays optional - without it no
// segment is clickable and every branch renders exactly as before.
const attrs = useAttrs() as Record<string, any>

function segCol(comp: any): string | null {
  return comp.group_by || props.groupBys?.[comp.id] || null
}

function segClickable(comp: any): boolean {
  return typeof attrs.onSegmentClick === 'function' && comp.type === 'chart' && !!segCol(comp)
}

function segActive(comp: any, label: string): boolean {
  const col = segCol(comp)
  return !!col && (props.activeFilters?.[col] || []).includes(label)
}

function onSeg(comp: any, label: string) {
  if (!label || !segClickable(comp)) return
  ;(attrs.onSegmentClick as (chart: any, label: string) => void)(comp, label)
}

const ACCENT = computed(() => props.accent || 'from-cyan-500/80 to-cyan-400/50')

// ---------- helpers ----------
function statDisplay(v: any) {
  if (v === null || v === undefined) return '-'
  const n = Number(v)
  if (Number.isFinite(n)) {
    if (Math.abs(n) >= 1000000) return `${(n / 1000000).toFixed(1)}M`
    if (Math.abs(n) >= 10000) return `${(n / 1000).toFixed(1)}k`
    if (!Number.isInteger(n)) return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
  }
  return String(v)
}

const chartMaxes = computed(() => {
  const m: Record<string, number> = {}
  for (const c of props.components) {
    if (c.type === 'chart') m[c.id] = Math.max(1, ...(c.values || [1]))
  }
  return m
})

// pie slices → conic-gradient stops
function pieStyle(c: any) {
  const total = (c.values || []).reduce((a: number, b: number) => a + b, 0) || 1
  const palette = ['#06b6d4', '#8b5cf6', '#f59e0b', '#10b981', '#ef4444', '#3b82f6', '#ec4899', '#84cc16']
  let acc = 0
  const stops: string[] = []
  c.values.forEach((v: number, i: number) => {
    const from = (acc / total) * 100
    acc += v
    const to = (acc / total) * 100
    stops.push(`${palette[i % palette.length]} ${from}% ${to}%`)
  })
  return { background: `conic-gradient(${stops.join(', ')})`, total }
}

// line chart → per-point svg coordinates (shared by the polyline and the
// v47 cross-filter click targets)
function linePointList(c: any, w = 320, h = 120, pad = 6) {
  const vals = c.values || []
  const n = vals.length
  const max = chartMaxes.value[c.id] || 1
  if (!n) return []
  const x = (i: number) => pad + (i * (w - pad * 2)) / Math.max(1, n - 1)
  const y = (v: number) => h - pad - (v / max) * (h - pad * 2)
  return vals.map((v: number, i: number) => ({ x: x(i).toFixed(1), y: y(v).toFixed(1) }))
}

// line chart → svg polyline points
function linePoints(c: any, w = 320, h = 120, pad = 6) {
  const list = linePointList(c, w, h, pad)
  if (!list.length) return { pts: '', area: '' }
  const pts = list.map((p) => `${p.x},${p.y}`).join(' ')
  const area = `${pad},${h - pad} ${pts} ${list[list.length - 1].x},${h - pad}`
  return { pts, area }
}

// v46: scatter points → svg circle coordinates
function scatterPoints(c: any, w = 320, h = 140, padL = 24, padB = 12, padT = 8) {
  const points = c.points || []
  if (!points.length) return []
  const xs = points.map((p: any) => Number(p.x)).filter(Number.isFinite)
  const ys = points.map((p: any) => Number(p.y)).filter(Number.isFinite)
  const xMin = Math.min(...xs), xMax = Math.max(...xs)
  const yMin = Math.min(...ys), yMax = Math.max(...ys)
  const xRange = xMax - xMin || 1
  const yRange = yMax - yMin || 1
  return points.map((p: any) => ({
    cx: (padL + ((Number(p.x) - xMin) / xRange) * (w - padL - 4)).toFixed(1),
    cy: (padT + (1 - (Number(p.y) - yMin) / yRange) * (h - padT - padB)).toFixed(1),
  }))
}
</script>

<template>
  <div>
    <template v-for="comp in components" :key="comp.id">
      <!-- stat card -->
      <div v-if="comp.type === 'stat'" :id="`comp-${comp.id}`" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4" :class="compClass(comp.id)">
        <p class="truncate text-[11px] font-medium uppercase tracking-wide text-zinc-500">{{ comp.label }}</p>
        <p class="mt-1.5 text-2xl font-bold tabular-nums text-zinc-50">{{ statDisplay(comp.value) }}</p>
      </div>

      <!-- text / narrative -->
      <div v-else-if="comp.type === 'text'" :id="`comp-${comp.id}`" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4" :class="compClass(comp.id)">
        <p v-if="comp.title" class="text-sm font-semibold text-zinc-200">{{ comp.title }}</p>
        <p v-if="comp.body" class="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-zinc-400">{{ comp.body }}</p>
      </div>

      <!-- chart -->
      <div v-else-if="comp.type === 'chart'" :id="`comp-${comp.id}`" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4" :class="compClass(comp.id)">
        <div class="flex items-center justify-between gap-2">
          <p class="truncate text-sm font-semibold text-zinc-200">{{ comp.title || 'Chart' }}</p>
          <span class="shrink-0 rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-medium uppercase text-zinc-400">{{ comp.chart_type }}</span>
        </div>

        <!-- bar -->
        <div v-if="comp.chart_type === 'bar' && comp.labels.length" class="mt-3">
          <div class="space-y-2">
            <div
              v-for="(label, i) in comp.labels"
              :key="label"
              class="flex items-center gap-2"
              :class="segClickable(comp) ? (segActive(comp, label) ? 'cursor-pointer rounded-lg bg-cyan-500/10 ring-1 ring-cyan-500/40' : 'cursor-pointer rounded-lg hover:bg-zinc-800/40') : ''"
              :title="segClickable(comp) ? `filter by ${segCol(comp)} = ${label}` : undefined"
              @click="onSeg(comp, label)"
            >
              <span class="w-24 shrink-0 truncate text-[11px] text-zinc-400">{{ label }}</span>
              <div class="h-4 flex-1 overflow-hidden rounded-md bg-zinc-800/60">
                <div class="h-full rounded-md bg-gradient-to-r" :class="ACCENT" :style="{ width: `${Math.max(4, (comp.values[i] / (chartMaxes[comp.id] || 1)) * 100)}%` }" />
              </div>
              <span class="w-12 text-right text-[11px] tabular-nums text-zinc-400">{{ comp.values[i] }}</span>
            </div>
          </div>
        </div>

        <!-- line / area (v46: area joins the line renderer) -->
        <div v-else-if="(comp.chart_type === 'line' || comp.chart_type === 'area') && comp.labels.length" class="mt-3">
          <svg viewBox="0 0 320 120" class="h-32 w-full" preserveAspectRatio="none">
            <polygon :points="linePoints(comp).area" fill="url(#lg)" opacity="0.25" />
            <polyline :points="linePoints(comp).pts" fill="none" stroke="#06b6d4" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
            <!-- v47: per-point click targets when the chart is cross-filterable -->
            <template v-if="segClickable(comp)">
              <circle
                v-for="(p, i) in linePointList(comp)"
                :key="`pt${i}`"
                :cx="p.x" :cy="p.y" r="4.5"
                :fill="segActive(comp, comp.labels[i]) ? '#22d3ee' : '#06b6d4'"
                fill-opacity="0.9"
                class="cursor-pointer"
                @click="onSeg(comp, comp.labels[i])"
              />
            </template>
            <defs>
              <linearGradient id="lg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#06b6d4" />
                <stop offset="100%" stop-color="#06b6d4" stop-opacity="0" />
              </linearGradient>
            </defs>
          </svg>
          <div class="mt-1 flex justify-between text-[10px] text-zinc-500">
            <span>{{ comp.labels[0] }}</span>
            <span>{{ comp.labels[comp.labels.length - 1] }}</span>
          </div>
        </div>

        <!-- pie / donut (v46: donut = pie with a deeper hole) -->
        <div v-else-if="(comp.chart_type === 'pie' || comp.chart_type === 'donut') && comp.labels.length" class="mt-3 flex items-center gap-4">
          <div class="relative h-24 w-24 shrink-0 rounded-full" :style="pieStyle(comp)">
            <div class="absolute rounded-full bg-zinc-900" :class="comp.chart_type === 'donut' ? 'inset-[18px]' : 'inset-[10px]'" />
          </div>
          <div class="min-w-0 flex-1 space-y-1">
            <div
              v-for="(label, i) in comp.labels"
              :key="label"
              class="flex items-center gap-1.5 text-[11px]"
              :class="segClickable(comp) ? (segActive(comp, label) ? 'cursor-pointer rounded-md bg-cyan-500/10 px-1 ring-1 ring-cyan-500/40' : 'cursor-pointer rounded-md px-1 hover:bg-zinc-800/40') : ''"
              :title="segClickable(comp) ? `filter by ${segCol(comp)} = ${label}` : undefined"
              @click="onSeg(comp, label)"
            >
              <span class="h-2 w-2 shrink-0 rounded-full" :style="{ background: ['#06b6d4', '#8b5cf6', '#f59e0b', '#10b981', '#ef4444', '#3b82f6', '#ec4899', '#84cc16'][i % 8] }" />
              <span class="min-w-0 flex-1 truncate text-zinc-400">{{ label }}</span>
              <span class="tabular-nums text-zinc-500">{{ comp.values[i] }} ({{ Math.round((comp.values[i] / (pieStyle(comp).total || 1)) * 100) }}%)</span>
            </div>
          </div>
        </div>

        <!-- scatter (v46): x/y points as SVG circles -->
        <div v-else-if="comp.chart_type === 'scatter' && comp.points?.length" class="mt-3">
          <svg viewBox="0 0 320 140" class="h-36 w-full" preserveAspectRatio="none">
            <line x1="24" y1="128" x2="316" y2="128" stroke="#3f3f46" stroke-width="1" />
            <line x1="24" y1="8" x2="24" y2="128" stroke="#3f3f46" stroke-width="1" />
            <circle
              v-for="(pt, i) in scatterPoints(comp)"
              :key="i"
              :cx="pt.cx" :cy="pt.cy" r="3.5"
              fill="#06b6d4" fill-opacity="0.75"
            />
          </svg>
          <div class="mt-1 flex justify-between text-[10px] text-zinc-500">
            <span>{{ comp.x }}</span>
            <span>{{ comp.y }}</span>
          </div>
        </div>

        <p v-else class="mt-2 text-[11px] text-zinc-600">No data to chart yet.</p>
      </div>

      <!-- table -->
      <div v-else-if="comp.type === 'table'" :id="`comp-${comp.id}`" class="overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40" :class="compClass(comp.id)">
        <div class="flex items-center justify-between border-b border-zinc-800/80 px-4 py-2.5">
          <p class="truncate text-sm font-semibold text-zinc-200">{{ comp.title || 'Table' }}</p>
          <span class="shrink-0 text-[10px] text-zinc-500">{{ comp.row_count }} rows</span>
        </div>
        <div v-if="comp.rows.length" class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead>
              <tr class="border-b border-zinc-800/60 text-[10px] uppercase tracking-wide text-zinc-500">
                <th v-for="col in comp.columns" :key="col" class="px-4 py-2 font-medium">{{ col }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, ri) in comp.rows" :key="ri" class="border-b border-zinc-800/40 last:border-0">
                <td v-for="col in comp.columns" :key="col" class="max-w-[220px] truncate px-4 py-2 text-zinc-300">
                  {{ row[col] === null || row[col] === undefined || row[col] === '' ? '-' : row[col] }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="px-4 py-4 text-[11px] text-zinc-600">No rows yet.</p>
      </div>
    </template>
  </div>
</template>
