<script setup lang="ts">
// v108 - Agent modules: the agent as its own resource. Create a module
// (prompt + provider + tools + memory), run it over the wire, watch the
// tool loop trace, jump between its sessions. The runtime is the same
// ai_agent machinery the graph uses - this panel is its direct door.
import { ref, computed, onMounted, nextTick } from 'vue'
import {
  Bot, Plus, Loader2, Wrench, Database, Code2, Globe, GitBranch, BookOpen,
  Send, Trash2, Pencil, X, MemoryStick, CheckCircle2, AlertTriangle, Cpu,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

interface ToolSpec { kind: string; name: string; description?: string; content?: string; [k: string]: unknown }
interface ModuleRow {
  id: string
  name: string
  description: string
  system_prompt: string
  provider: string
  model: string
  temperature: number
  max_iterations: number
  memory: string
  max_history_turns: number
  tools: ToolSpec[]
  tool_kinds: string[]
  is_active: boolean
}
interface SessionRow { session_key: string; turns: number; updated_at: string | null }
interface TraceStep { event: string; iteration?: number; tool?: string; status?: string; reply?: string }
interface RunResult {
  reply: string
  iterations: number
  tool_calls: { tool: string; status: string; result: string }[]
  trace: TraceStep[]
  memory_turns_loaded: number
}
interface Turn { role: 'user' | 'assistant'; text: string; run?: RunResult; error?: boolean }

const KIND_ICON: Record<string, unknown> = {
  knowledge: BookOpen, http: Globe, workflow: GitBranch, dataset: Database, code: Code2,
}
function kindIcon(kind: string) { return KIND_ICON[kind] ?? Wrench }
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

const modules = ref<ModuleRow[]>([])
const loading = ref(true)
const selected = ref<ModuleRow | null>(null)
const editing = ref(false)
const saving = ref(false)
const formError = ref('')

// editor form
const form = ref({
  name: '', description: '', system_prompt: 'You are a precise operations agent. Use the tools when they help, then answer.',
  provider: 'sandbox_bridge', model: '', memory: 'none', max_history_turns: 5,
  toolsJson: '[]',
})
const sessions = ref<SessionRow[]>([])
const sessionKey = ref('playground')
const draft = ref('')
const sending = ref(false)
const turns = ref<Turn[]>([])
const transcriptEl = ref<HTMLElement | null>(null)

const canSave = computed(() => form.value.name.trim().length > 0)

function scrollBottom() {
  nextTick(() => transcriptEl.value?.scrollTo({ top: transcriptEl.value.scrollHeight, behavior: 'smooth' }))
}

async function loadModules(keepSelection = true) {
  loading.value = true
  try {
    modules.value = await api.get<ModuleRow[]>('/agents/modules')
    if (keepSelection && selected.value) {
      selected.value = modules.value.find((m) => m.id === selected.value?.id) ?? modules.value[0] ?? null
    } else if (!selected.value) {
      selected.value = modules.value[0] ?? null
    }
    if (selected.value) await loadSessions()
  } finally {
    loading.value = false
  }
}

async function loadSessions() {
  if (!selected.value) { sessions.value = []; return }
  try {
    sessions.value = await api.get<SessionRow[]>(`/agents/modules/${selected.value.id}/sessions`)
    if (!sessions.value.some((s) => s.session_key === sessionKey.value) && sessions.value.length) {
      sessionKey.value = sessions.value[0].session_key
    }
  } catch { sessions.value = [] }
}

function openCreate() {
  selected.value = null
  editing.value = true
  form.value = {
    name: '', description: '',
    system_prompt: 'You are a precise operations agent. Use the tools when they help, then answer.',
    provider: 'sandbox_bridge', model: '', memory: 'none', max_history_turns: 5,
    toolsJson: '[]',
  }
}

function openEdit(m: ModuleRow) {
  selected.value = m
  editing.value = true
  form.value = {
    name: m.name, description: m.description, system_prompt: m.system_prompt,
    provider: m.provider, model: m.model, memory: m.memory,
    max_history_turns: m.max_history_turns,
    toolsJson: JSON.stringify(m.tools ?? [], null, 2),
  }
}

async function save() {
  if (!canSave.value || saving.value) return
  saving.value = true
  formError.value = ''
  let tools: unknown
  try {
    tools = JSON.parse(form.value.toolsJson || '[]')
  } catch {
    formError.value = 'tools JSON does not parse'
    saving.value = false
    return
  }
  const body = {
    name: form.value.name.trim(),
    description: form.value.description,
    system_prompt: form.value.system_prompt,
    provider: form.value.provider,
    model: form.value.model.trim(),
    memory: form.value.memory,
    max_history_turns: form.value.max_history_turns,
    tools,
  }
  try {
    if (selected.value && modules.value.some((m) => m.id === selected.value?.id)) {
      selected.value = await api.patch<ModuleRow>(`/agents/modules/${selected.value.id}`, body)
    } else {
      selected.value = await api.post<ModuleRow>('/agents/modules', body)
    }
    editing.value = false
    await loadModules()
  } catch (e: unknown) {
    formError.value = e instanceof Error ? e.message : 'save failed'
  } finally {
    saving.value = false
  }
}

async function remove(m: ModuleRow) {
  if (!confirm(`Delete module "${m.name}"? Its sessions go with it.`)) return
  await api.delete(`/agents/modules/${m.id}`)
  if (selected.value?.id === m.id) { selected.value = null; editing.value = false; turns.value = [] }
  await loadModules(false)
}

function pick(m: ModuleRow) {
  if (editing.value) return
  if (selected.value?.id === m.id) return
  selected.value = m
  turns.value = []
  loadSessions()
}

function pickSession(key: string) {
  sessionKey.value = key
  turns.value = []
}

async function clearSession(s: SessionRow) {
  if (!selected.value) return
  await api.delete(`/agents/modules/${selected.value.id}/sessions/${encodeURIComponent(s.session_key)}`)
  if (sessionKey.value === s.session_key) turns.value = []
  await loadSessions()
}

async function send() {
  const m = selected.value
  const message = draft.value.trim()
  if (!m || !message || sending.value) return
  draft.value = ''
  turns.value.push({ role: 'user', text: message })
  scrollBottom()
  sending.value = true
  const turn: Turn = { role: 'assistant', text: '' }
  turns.value.push(turn)
  scrollBottom()
  try {
    const run = await api.post<RunResult>(`/agents/modules/${m.id}/run`, {
      message, session_key: sessionKey.value.trim() || 'playground',
    })
    turn.run = run
    turn.text = run.reply || '(empty reply)'
    if (run.memory_turns_loaded > 0) {
      turn.text += `\n\n(remembered ${run.memory_turns_loaded} earlier turn${run.memory_turns_loaded === 1 ? '' : 's'})`
    }
    await loadSessions()
  } catch (e: unknown) {
    turn.text = e instanceof Error ? e.message : 'run failed - is the backend up?'
    turn.error = true
  } finally {
    sending.value = false
    scrollBottom()
  }
}

onMounted(() => loadModules(false))
</script>

<template>
  <div class="grid h-full grid-cols-1 gap-4 lg:grid-cols-[20rem_1fr]">
    <!-- module list -->
    <aside class="flex min-h-0 flex-col gap-2 overflow-y-auto pr-1">
      <button
        class="flex items-center justify-center gap-2 rounded-xl border border-dashed border-violet-500/40 bg-violet-500/[0.06] px-3 py-2 text-xs font-semibold text-violet-300 transition hover:bg-violet-500/15"
        @click="openCreate"
      >
        <Plus class="h-3.5 w-3.5" /> New module
      </button>
      <div v-if="loading" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 text-sm text-zinc-500">
        <Loader2 class="h-4 w-4 animate-spin" /> loading modules…
      </div>
      <template v-else-if="modules.length">
        <button
          v-for="m in modules"
          :key="m.id"
          class="group rounded-xl border p-3 text-left transition"
          :class="selected?.id === m.id && !editing
            ? 'border-violet-500/60 bg-violet-500/[0.07]'
            : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-700 hover:bg-zinc-900'"
          @click="pick(m)"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="flex items-center gap-2 truncate text-sm font-semibold">
              <span
                class="h-1.5 w-1.5 shrink-0 rounded-full"
                :class="m.is_active ? 'bg-emerald-400' : 'bg-zinc-600'"
              />
              {{ m.name }}
            </span>
            <span class="flex shrink-0 items-center gap-0.5 opacity-0 transition group-hover:opacity-100">
              <span class="rounded p-1 text-zinc-600 hover:text-violet-300" title="edit module" @click.stop="openEdit(m)">
                <Pencil class="h-3 w-3" />
              </span>
              <span class="rounded p-1 text-zinc-600 hover:text-rose-300" title="delete module" @click.stop="remove(m)">
                <Trash2 class="h-3 w-3" />
              </span>
            </span>
          </div>
          <p class="mt-1 line-clamp-2 text-[11px] leading-snug text-zinc-500">{{ m.description || 'No description' }}</p>
          <div class="mt-2 flex flex-wrap gap-1">
            <span class="rounded-md border border-zinc-700 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
              {{ m.provider === 'openai_compatible' ? (m.model || 'openai-compatible') : 'bridge' }}
            </span>
            <span
              v-for="k in m.tool_kinds.slice(0, 3)"
              :key="k"
              class="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10px]"
              :class="kindClass(k)"
            >
              <component :is="kindIcon(k)" class="h-2.5 w-2.5" />{{ k }}
            </span>
            <span
              v-if="m.memory === 'buffer'"
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
          No modules yet.<br />A module is a standing agent with its own prompt,
          tools and memory - no workflow graph needed.
        </p>
      </div>
    </aside>

    <!-- editor / playground -->
    <section class="flex min-h-0 flex-col rounded-xl border border-zinc-800 bg-zinc-900/40">
      <!-- EDITOR -->
      <div v-if="editing" class="flex-1 space-y-4 overflow-y-auto p-5">
        <div class="flex items-center justify-between">
          <h2 class="flex items-center gap-2 text-sm font-semibold">
            <Bot class="h-4 w-4 text-violet-400" />
            {{ selected && modules.some((m) => m.id === selected?.id) ? 'Edit module' : 'New module' }}
          </h2>
          <button class="rounded-lg p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300" @click="editing = false">
            <X class="h-4 w-4" />
          </button>
        </div>
        <div class="grid gap-4 sm:grid-cols-2">
          <label class="block text-xs text-zinc-500">
            name
            <input
              v-model="form.name"
              class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-violet-500/60"
              placeholder="Ops Assistant"
            />
          </label>
          <label class="block text-xs text-zinc-500">
            description
            <input
              v-model="form.description"
              class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-violet-500/60"
              placeholder="what this agent is for"
            />
          </label>
        </div>
        <label class="block text-xs text-zinc-500">
          system prompt
          <textarea
            v-model="form.system_prompt"
            :rows="3"
            class="mt-1 w-full resize-y rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-xs text-zinc-100 outline-none focus:border-violet-500/60"
          />
        </label>
        <div class="grid gap-4 sm:grid-cols-4">
          <label class="block text-xs text-zinc-500">
            provider
            <select
              v-model="form.provider"
              class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 outline-none focus:border-violet-500/60"
            >
              <option value="sandbox_bridge">sandbox bridge</option>
              <option value="openai_compatible">openai-compatible</option>
            </select>
          </label>
          <label class="block text-xs text-zinc-500">
            model
            <input
              v-model="form.model"
              :disabled="form.provider === 'sandbox_bridge'"
              class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-violet-500/60 disabled:opacity-40"
              placeholder="bridge picks a default"
            />
          </label>
          <label class="block text-xs text-zinc-500">
            memory
            <select
              v-model="form.memory"
              class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-2 text-sm text-zinc-100 outline-none focus:border-violet-500/60"
            >
              <option value="none">none (stateless)</option>
              <option value="buffer">buffer (per session)</option>
            </select>
          </label>
          <label class="block text-xs text-zinc-500">
            history turns
            <input
              v-model.number="form.max_history_turns"
              type="number"
              min="1"
              max="50"
              :disabled="form.memory !== 'buffer'"
              class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-violet-500/60 disabled:opacity-40"
            />
          </label>
        </div>
        <label class="block text-xs text-zinc-500">
          tools (JSON list - kinds: knowledge / http / workflow / dataset / code)
          <textarea
            v-model="form.toolsJson"
            :rows="6"
            spellcheck="false"
            class="mt-1 w-full resize-y rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-[11px] text-zinc-100 outline-none focus:border-violet-500/60"
          />
          <span class="mt-1 block font-mono text-[10px] text-zinc-600">
            e.g. [{"kind":"knowledge","name":"handbook","content":"…"},{"kind":"code","name":"calc"}]
          </span>
        </label>
        <p v-if="formError" class="flex items-center gap-1.5 text-xs text-rose-300">
          <AlertTriangle class="h-3.5 w-3.5" /> {{ formError }}
        </p>
        <div class="flex items-center gap-2">
          <button
            class="flex items-center gap-2 rounded-lg bg-violet-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-violet-500/25 transition hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-40"
            :disabled="!canSave || saving"
            @click="save"
          >
            <Loader2 v-if="saving" class="h-4 w-4 animate-spin" />
            {{ selected && modules.some((m) => m.id === selected?.id) ? 'Save changes' : 'Create module' }}
          </button>
          <button class="rounded-lg px-3 py-2 text-sm text-zinc-400 hover:text-zinc-200" @click="editing = false">Cancel</button>
        </div>
      </div>

      <!-- PLAYGROUND -->
      <template v-else-if="selected">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-4 py-2.5">
          <div class="flex items-center gap-2 text-sm font-semibold">
            <Bot class="h-4 w-4 text-violet-400" /> {{ selected.name }}
            <span
              v-if="!selected.is_active"
              class="rounded-md border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300"
            >inactive</span>
            <span class="rounded-md border border-zinc-700 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
              {{ selected.tools.length }} tool{{ selected.tools.length === 1 ? '' : 's' }}
            </span>
          </div>
          <div class="flex flex-wrap items-center gap-1">
            <button
              v-for="s in sessions"
              :key="s.session_key"
              class="group inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10px] transition"
              :class="sessionKey === s.session_key
                ? 'border-violet-500/50 bg-violet-500/10 text-violet-300'
                : 'border-zinc-800 text-zinc-500 hover:text-zinc-300'"
              :title="`${s.turns} turn(s) - last active ${s.updated_at ? new Date(s.updated_at).toLocaleString() : 'unknown'}`"
              @click="pickSession(s.session_key)"
            >
              <MemoryStick class="h-2.5 w-2.5" />{{ s.session_key }}
              <span
                class="hidden text-zinc-600 hover:text-rose-300 group-hover:inline"
                title="forget this session"
                @click.stop="clearSession(s)"
              ><X class="h-2.5 w-2.5" /></span>
            </button>
            <label class="flex items-center gap-1.5 pl-1 text-[11px] text-zinc-500">
              session
              <input
                v-model="sessionKey"
                class="w-28 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[11px] outline-none focus:border-violet-500/60"
              />
            </label>
          </div>
        </div>

        <div ref="transcriptEl" class="flex-1 space-y-3 overflow-y-auto px-4 py-4">
          <div v-if="!turns.length" class="flex h-full flex-col items-center justify-center text-center">
            <Cpu class="h-7 w-7 text-zinc-700" />
            <p class="mt-2 max-w-sm text-xs leading-relaxed text-zinc-500">
              Say something - the module runs its tool loop directly (no workflow graph):
              {{ selected.tools.map((t) => t.name).join(', ') || 'no tools' }}.
              Same session key = same memory<span v-if="selected.memory !== 'buffer'"> (memory is off for this one)</span>.
            </p>
          </div>
          <template v-for="(t, i) in turns" :key="i">
            <div v-if="t.role === 'user'" class="flex justify-end">
              <div class="max-w-[80%] rounded-2xl rounded-br-md bg-violet-500/20 px-3.5 py-2 text-sm">{{ t.text }}</div>
            </div>
            <div v-else class="flex flex-col items-start gap-1.5">
              <div
                class="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-bl-md px-3.5 py-2 text-sm"
                :class="t.error ? 'border border-rose-500/40 bg-rose-500/10 text-rose-200' : 'border border-zinc-800 bg-zinc-900'"
              >{{ t.text }}<span v-if="sending && i === turns.length - 1 && !t.text" class="ml-1 inline-block animate-pulse text-zinc-500">▋</span></div>
              <div v-if="t.run" class="ml-2 flex w-[85%] flex-wrap items-center gap-1.5 text-[10px]">
                <span class="font-semibold uppercase tracking-wider text-zinc-600">
                  trace · {{ t.run.iterations }} iteration{{ t.run.iterations === 1 ? '' : 's' }}
                </span>
                <template v-for="(tc, ti) in t.run.tool_calls" :key="ti">
                  <span
                    class="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono"
                    :class="tc.status === 'ok'
                      ? 'border-zinc-700 bg-zinc-900 text-zinc-300'
                      : 'border-rose-500/40 bg-rose-500/10 text-rose-300'"
                    :title="tc.result"
                  >
                    <component :is="tc.status === 'ok' ? CheckCircle2 : AlertTriangle" class="h-2.5 w-2.5" />
                    {{ tc.tool }}
                  </span>
                </template>
              </div>
            </div>
          </template>
        </div>

        <div class="border-t border-zinc-800 p-3">
          <div class="flex items-end gap-2">
            <textarea
              v-model="draft"
              :rows="1"
              placeholder="Message the module… (Enter to send)"
              class="max-h-32 min-h-[2.5rem] flex-1 resize-y rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm outline-none transition placeholder:text-zinc-600 focus:border-violet-500/60"
              @keydown.enter.exact.prevent="send"
            />
            <button
              class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-500 text-white shadow-lg shadow-violet-500/25 transition hover:bg-violet-400 disabled:cursor-not-allowed disabled:opacity-40"
              :disabled="sending || !draft.trim() || !selected.is_active"
              title="Send"
              @click="send"
            >
              <Send v-if="!sending" class="h-4 w-4" />
              <Loader2 v-else class="h-4 w-4 animate-spin" />
            </button>
          </div>
        </div>
      </template>

      <div v-else-if="!loading" class="flex flex-1 items-center justify-center text-sm text-zinc-600">
        pick a module on the left, or create one
      </div>
      <div v-else class="flex flex-1 items-center justify-center">
        <Loader2 class="h-5 w-5 animate-spin text-zinc-600" />
      </div>
    </section>
  </div>
</template>
