<script setup lang="ts">
import { ref, computed, onMounted, nextTick } from 'vue'
import {
  Bot, Send, Loader2, Wrench, Database, Code2, Globe, GitBranch, BookOpen,
  MessageSquare, ExternalLink, MemoryStick, AlertTriangle, CheckCircle2, ChevronDown, Brain,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

interface ToolRef { name: string; kind: string }
interface AgentSummary {
  id: string
  name: string
  description: string
  active: boolean
  agent_nodes: string[]
  tools: ToolRef[]
  tool_kinds: string[]
  memory_sessions: string[]
  node_count: number
}
// v36: one live trace step per SSE agent frame, rendered as it arrives
// v40: dataset-shaped tool results also carry a compact row preview
interface DataPreview {
  columns: string[]
  rows: string[][]
  total_rows: number
  rows_shown: number
  columns_shown: number
  columns_total: number
}
interface LiveStep {
  kind: 'iteration' | 'reply' | 'tool_call' | 'tool_result'
  iteration?: number
  reply?: string
  tool?: string
  arguments?: string
  status?: string
  preview?: string
  data?: DataPreview
}
interface Turn {
  role: 'user' | 'assistant'
  text: string
  steps: LiveStep[]
  streaming?: boolean
  error?: boolean
}

const KIND_ICON: Record<string, unknown> = {
  knowledge: BookOpen,
  http: Globe,
  workflow: GitBranch,
  dataset: Database,
  code: Code2,
}
function kindIcon(kind: string) {
  return KIND_ICON[kind] ?? Wrench
}
const KIND_CLASS: Record<string, string> = {
  knowledge: 'bg-sky-500/10 text-sky-300 border-sky-500/30',
  http: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
  workflow: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
  dataset: 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30',
  code: 'bg-fuchsia-500/10 text-fuchsia-300 border-fuchsia-500/30',
}
function kindClass(kind: string) {
  return KIND_CLASS[kind] ?? 'bg-zinc-500/10 text-zinc-300 border-zinc-500/30'
}

const agents = ref<AgentSummary[]>([])
const loading = ref(true)
const selected = ref<AgentSummary | null>(null)
const sessionId = ref('playground')
const draft = ref('')
const sending = ref(false)
const turns = ref<Turn[]>([])
const transcriptEl = ref<HTMLElement | null>(null)

async function loadAgents(keepSelection = true) {
  loading.value = true
  try {
    agents.value = await api.get<AgentSummary[]>('/agents')
    if (keepSelection && selected.value) {
      selected.value = agents.value.find((a) => a.id === selected.value?.id) ?? agents.value[0] ?? null
    } else if (!selected.value) {
      selected.value = agents.value[0] ?? null
    }
  } finally {
    loading.value = false
  }
}

function pick(a: AgentSummary) {
  if (selected.value?.id === a.id) return
  selected.value = a
  turns.value = []
}

function pretty(text?: string): string {
  if (!text) return ''
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

function scrollBottom() {
  nextTick(() => {
    transcriptEl.value?.scrollTo({ top: transcriptEl.value.scrollHeight, behavior: 'smooth' })
  })
}

function streamUrl(id: string): string {
  // gateway routes /api to the backend; raw fetch (no $fetch) so we can read
  // the body as a stream - same pattern as the editor ChatPanel
  const base = `/api/v1/chat/${id}/stream`
  return base
}

function handleFrame(turn: Turn, ev: string, data: Record<string, any>) {
  if (ev === 'agent') {
    const step = data.phase as LiveStep['kind']
    if (step === 'iteration') {
      turn.steps.push({ kind: 'iteration', iteration: data.iteration })
    } else if (step === 'reply') {
      turn.steps.push({ kind: 'reply', iteration: data.iteration, reply: data.reply })
    } else if (step === 'tool_call') {
      turn.steps.push({ kind: 'tool_call', iteration: data.iteration, tool: data.tool, arguments: data.arguments })
    } else if (step === 'tool_result') {
      turn.steps.push({ kind: 'tool_result', iteration: data.iteration, tool: data.tool, status: data.status, preview: data.preview, data: data.data || undefined })
    } else if (step === 'answer') {
      turn.text = typeof data.answer === 'string' ? data.answer : turn.text
    }
  } else if (ev === 'done') {
    if (!turn.text) turn.text = data.reply || '(empty reply)'
  } else if (ev === 'error') {
    turn.text = data.error || 'agent run failed'
    turn.error = true
  } else if (ev === 'timeout') {
    turn.text = 'stream timed out - the run keeps going in the background'
    turn.error = true
  }
  scrollBottom()
}

async function send() {
  const a = selected.value
  const message = draft.value.trim()
  if (!a || !message || sending.value) return
  draft.value = ''
  turns.value.push({ role: 'user', text: message, steps: [] })
  scrollBottom()
  sending.value = true
  const turn: Turn = { role: 'assistant', text: '', steps: [], streaming: true }
  turns.value.push(turn)
  scrollBottom()
  try {
    const res = await fetch(streamUrl(a.id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: sessionId.value.trim() || 'playground' }),
    })
    const ctype = res.headers.get('content-type') || ''
    if (!res.ok || !ctype.includes('text/event-stream')) {
      // guards (404/409/422) answer with plain JSON before any stream
      const body = await res.json().catch(() => ({}) as Record<string, unknown>)
      throw new Error((body as Record<string, string>).detail || `stream failed (${res.status})`)
    }
    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const lines = frame.split('\n')
        const evLine = lines.find((l) => l.startsWith('event: '))
        const dataLine = lines.find((l) => l.startsWith('data: '))
        if (!evLine || !dataLine) continue
        const ev = evLine.slice(7).trim()
        let data: Record<string, any> = {}
        try {
          data = JSON.parse(dataLine.slice(6))
        } catch {
          continue
        }
        handleFrame(turn, ev, data)
      }
    }
    if (!turn.text && !turn.error) turn.text = '(stream ended without an answer)'
  } catch (e: unknown) {
    turn.text = e instanceof Error ? e.message : 'Request failed - is the backend up?'
    turn.error = true
  } finally {
    turn.streaming = false
    sending.value = false
    scrollBottom()
  }
}

const toolChipTotal = computed(() =>
  turns.value.reduce((n, t) => n + t.steps.filter((s) => s.kind === 'tool_call').length, 0),
)

function turnIterations(t: Turn): number {
  return Math.max(0, ...t.steps.map((s) => s.iteration ?? 0))
}

onMounted(() => loadAgents(false))
</script>

<template>
  <div class="flex h-screen flex-col text-zinc-100">
    <!-- page header -->
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 to-fuchsia-500 shadow-lg shadow-violet-500/20">
            <Bot class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Agent Console</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">
              {{ agents.length }} agent{{ agents.length === 1 ? '' : 's' }} · live tool-calling streams
            </p>
          </div>
        </div>
        <div class="flex items-center gap-2 text-[11px] text-zinc-500">
          <Wrench class="h-3.5 w-3.5" />
          tool calls this session: <span class="font-mono text-zinc-300">{{ toolChipTotal }}</span>
        </div>
      </div>
    </header>

    <main class="mx-auto grid w-full max-w-7xl flex-1 grid-cols-1 gap-4 overflow-hidden px-4 py-4 sm:px-6 lg:grid-cols-[20rem_1fr]">
      <!-- agent list -->
      <aside class="flex min-h-0 flex-col gap-2 overflow-y-auto pr-1">
        <div v-if="loading" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 text-sm text-zinc-500">
          <Loader2 class="h-4 w-4 animate-spin" /> loading agents…
        </div>
        <template v-else-if="agents.length">
          <button
            v-for="a in agents"
            :key="a.id"
            class="group rounded-xl border p-3 text-left transition"
            :class="selected?.id === a.id
              ? 'border-violet-500/60 bg-violet-500/[0.07]'
              : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-700 hover:bg-zinc-900'"
            @click="pick(a)"
          >
            <div class="flex items-center justify-between gap-2">
              <span class="flex items-center gap-2 truncate text-sm font-semibold">
                <span
                  class="h-1.5 w-1.5 shrink-0 rounded-full"
                  :class="a.active ? 'bg-emerald-400' : 'bg-zinc-600'"
                  :title="a.active ? 'triggers active' : 'inactive'"
                />
                {{ a.name }}
              </span>
              <a
                :href="`/workflows/${a.id}`"
                class="shrink-0 rounded p-1 text-zinc-600 opacity-0 transition hover:text-violet-300 group-hover:opacity-100"
                title="Open in editor"
                @click.stop
              >
                <ExternalLink class="h-3.5 w-3.5" />
              </a>
            </div>
            <p class="mt-1 line-clamp-2 text-[11px] leading-snug text-zinc-500">{{ a.description || 'No description' }}</p>
            <div class="mt-2 flex flex-wrap gap-1">
              <span
                v-for="t in a.tools.slice(0, 4)"
                :key="t.name"
                class="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10px]"
                :class="kindClass(t.kind)"
              >
                <component :is="kindIcon(t.kind)" class="h-2.5 w-2.5" />{{ t.name }}
              </span>
              <span v-if="a.tools.length > 4" class="rounded-md border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400">
                +{{ a.tools.length - 4 }}
              </span>
              <span
                v-if="a.memory_sessions.length"
                class="inline-flex items-center gap-1 rounded-md border border-orange-500/30 bg-orange-500/10 px-1.5 py-0.5 text-[10px] text-orange-300"
                title="session memory on"
              >
                <MemoryStick class="h-2.5 w-2.5" />memory
              </span>
            </div>
          </button>
        </template>
        <div v-else class="rounded-xl border border-dashed border-zinc-800 p-5 text-center">
          <Bot class="mx-auto h-6 w-6 text-zinc-600" />
          <p class="mt-2 text-xs leading-relaxed text-zinc-500">
            No agents yet.<br />Install the <b class="text-zinc-300">Research Agent</b> or
            <b class="text-zinc-300">SQL Data Analyst</b> from the gallery.
          </p>
          <a
            href="/templates"
            class="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-violet-500/10 px-3 py-1.5 text-[11px] font-semibold text-violet-300 transition hover:bg-violet-500/20"
          >
            Browse gallery →
          </a>
        </div>
      </aside>

      <!-- playground -->
      <section class="flex min-h-0 flex-col rounded-xl border border-zinc-800 bg-zinc-900/40">
        <template v-if="selected">
          <div class="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-4 py-2.5">
            <div class="flex items-center gap-2 text-sm font-semibold">
              <Bot class="h-4 w-4 text-violet-400" /> {{ selected.name }}
              <span class="rounded-md border border-zinc-700 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
                {{ selected.agent_nodes.join(', ') }}
              </span>
              <span
                class="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-300"
                title="answers stream frame by frame"
              >live</span>
            </div>
            <label class="flex items-center gap-1.5 text-[11px] text-zinc-500">
              session
              <input
                v-model="sessionId"
                class="w-36 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[11px] outline-none focus:border-violet-500/60"
              />
            </label>
          </div>

          <div ref="transcriptEl" class="flex-1 space-y-3 overflow-y-auto px-4 py-4">
            <div v-if="!turns.length" class="flex h-full flex-col items-center justify-center text-center">
              <MessageSquare class="h-7 w-7 text-zinc-700" />
              <p class="mt-2 max-w-xs text-xs leading-relaxed text-zinc-500">
                Say something - the agent streams its reasoning live: every iteration,
                tool call and result lands here as it happens
                ({{ selected.tools.map((t) => t.name).join(', ') || 'no tools' }}).
              </p>
            </div>
            <template v-for="(t, i) in turns" :key="i">
              <div v-if="t.role === 'user'" class="flex justify-end">
                <div class="max-w-[80%] rounded-2xl rounded-br-md bg-violet-500/20 px-3.5 py-2 text-sm">{{ t.text }}</div>
              </div>
              <div v-else class="flex flex-col items-start gap-1.5">
                <!-- assistant answer bubble -->
                <div
                  v-if="t.text || !t.steps.length"
                  class="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-bl-md px-3.5 py-2 text-sm"
                  :class="t.error ? 'border border-rose-500/40 bg-rose-500/10 text-rose-200' : 'border border-zinc-800 bg-zinc-900'"
                >{{ t.text }}<span v-if="t.streaming && !t.text" class="ml-1 inline-block animate-pulse text-zinc-500">▋</span></div>

                <!-- live trace: steps render as their frames arrive -->
                <div v-if="t.steps.length" class="ml-2 w-[85%] space-y-1">
                  <div class="flex flex-wrap items-center gap-1">
                    <span class="text-[10px] font-semibold uppercase tracking-wider text-zinc-600">
                      trace · {{ turnIterations(t) }} iteration{{ turnIterations(t) === 1 ? '' : 's' }}
                    </span>
                    <span
                      v-for="(s, si) in t.steps.filter((x) => x.kind === 'tool_call')"
                      :key="'chip' + si"
                      class="inline-flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300"
                    >
                      <component :is="kindIcon(selected?.tools.find((x) => x.name === s.tool)?.kind || '')" class="h-2.5 w-2.5" />
                      {{ s.tool }}
                    </span>
                  </div>
                  <template v-for="(s, si) in t.steps" :key="'step' + si">
                    <div v-if="s.kind === 'iteration'" class="flex items-center gap-2 pt-1">
                      <span class="text-[10px] font-semibold text-zinc-600">iteration {{ s.iteration }}</span>
                      <span class="h-px flex-1 bg-zinc-800/80" />
                    </div>
                    <details v-else-if="s.kind === 'reply'" class="ml-1">
                      <summary class="flex cursor-pointer items-center gap-1 text-[10px] text-zinc-600 hover:text-zinc-400">
                        <Brain class="h-3 w-3" /> model reply · iter {{ s.iteration }}
                      </summary>
                      <pre class="mt-1 max-w-full overflow-x-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-950 p-2 font-mono text-[10px] leading-relaxed text-zinc-400">{{ pretty(s.reply) }}</pre>
                    </details>
                    <div v-else-if="s.kind === 'tool_call'" class="flex items-center gap-1.5 text-[10px] text-zinc-500">
                      <Loader2 class="h-3 w-3 animate-spin text-cyan-400" />
                      calling <span class="font-mono text-cyan-300">{{ s.tool }}</span>
                      <details class="inline">
                        <summary class="cursor-pointer text-zinc-600 hover:text-zinc-400">args</summary>
                        <pre class="mt-1 max-w-full overflow-x-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-950 p-2 font-mono text-[10px] leading-relaxed text-zinc-400">{{ pretty(s.arguments) }}</pre>
                      </details>
                    </div>
                    <div v-else-if="s.kind === 'tool_result'" class="flex items-center gap-1.5 text-[10px]">
                      <component :is="s.status === 'ok' ? CheckCircle2 : AlertTriangle" class="h-3 w-3" :class="s.status === 'ok' ? 'text-emerald-400' : 'text-rose-400'" />
                      <span class="font-mono" :class="s.status === 'ok' ? 'text-emerald-300' : 'text-rose-300'">{{ s.tool }}</span>
                      <details class="inline">
                        <summary class="cursor-pointer text-zinc-600 hover:text-zinc-400">result</summary>
                        <!-- v40: dataset-shaped results render as a real mini table -->
                        <div v-if="s.data && s.data.rows.length" class="mt-1 max-w-full overflow-x-auto rounded-lg border border-zinc-800 bg-zinc-950">
                          <table class="w-full border-collapse font-mono text-[10px] leading-relaxed">
                            <thead>
                              <tr class="border-b border-zinc-800">
                                <th
                                  v-for="c in s.data.columns"
                                  :key="c"
                                  class="px-2 py-1 text-left font-semibold uppercase tracking-wide text-zinc-500"
                                >{{ c }}</th>
                              </tr>
                            </thead>
                            <tbody>
                              <tr
                                v-for="(row, ri) in s.data.rows"
                                :key="ri"
                                class="border-b border-zinc-800/50 last:border-0"
                              >
                                <td v-for="(cell, ci) in row" :key="ci" class="max-w-[16rem] truncate px-2 py-1 text-zinc-400">{{ cell }}</td>
                              </tr>
                            </tbody>
                          </table>
                          <p class="border-t border-zinc-800 px-2 py-1 text-[9px] text-zinc-600">
                            {{ s.data.rows_shown }} of {{ s.data.total_rows }} rows<template v-if="s.data.columns_shown < s.data.columns_total"> - {{ s.data.columns_shown }} of {{ s.data.columns_total }} columns</template>
                          </p>
                        </div>
                        <pre v-else class="mt-1 max-h-40 max-w-full overflow-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-950 p-2 font-mono text-[10px] leading-relaxed text-zinc-400">{{ pretty(s.preview) }}</pre>
                      </details>
                    </div>
                  </template>
                  <div v-if="t.streaming" class="flex items-center gap-1.5 text-[10px] text-zinc-500">
                    <Loader2 class="h-3 w-3 animate-spin" /> agent is working…
                  </div>
                </div>
              </div>
            </template>
          </div>

          <div class="border-t border-zinc-800 p-3">
            <div class="flex items-end gap-2">
              <textarea
                v-model="draft"
                :rows="1"
                placeholder="Message the agent… (Enter to send)"
                class="max-h-32 min-h-[2.5rem] flex-1 resize-y rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm outline-none transition placeholder:text-zinc-600 focus:border-violet-500/60"
                @keydown.enter.exact.prevent="send"
              />
              <button
                class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-500 text-white shadow-lg shadow-violet-500/25 transition hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-40"
                :disabled="sending || !draft.trim()"
                title="Send"
                @click="send"
              >
                <Send class="h-4 w-4" />
              </button>
            </div>
          </div>
        </template>
        <div v-else-if="!loading" class="flex flex-1 items-center justify-center text-sm text-zinc-600">
          pick an agent on the left to start
        </div>
        <div v-else class="flex flex-1 items-center justify-center">
          <Loader2 class="h-5 w-5 animate-spin text-zinc-600" />
        </div>
      </section>
    </main>
  </div>
</template>
