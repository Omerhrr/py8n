<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Loader2, Rocket, Plus, XCircle, X, Play, Ban, CirclePlay, Trash2, Copy, Check, Zap } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v67: Deployments - the DEPLOY verb made first-class. A deployment turns a
// registry row into a LIVE serving endpoint: py8n generates the workflow
// (webhook -> lm_generate for LMs, split_out -> model_predict for tabular),
// activates it, and this console operates the result - invoke, disable,
// retire. Serving stats derive from the execution log.

interface Deployment {
  id: string; name: string; serving_mode: string; environment: string
  enabled: boolean; notes: string; created_at: string | null
  model: { id: string; name: string | null; version: number | null; algorithm: string | null; features: string[] }
  workflow: { id: string; name: string; is_active: boolean; webhook_path: string } | null
  status: string
  stats: { runs_7d: number; failures_7d: number; last_call_at: string | null; last_call_status: string | null }
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const deployments = ref<Deployment[]>([])

const showCreate = ref(false)
const newName = ref('')
const newModel = ref('')
const newEnv = ref('dev')
const newNotes = ref('')
const newMaxTokens = ref<number | null>(null)
const newTemp = ref<number | null>(null)
const creating = ref(false)
const models = ref<any[]>([])

const trying = ref<string | null>(null)
const tryBody = ref('')
const tryResult = ref<{ ok: boolean; text: string } | null>(null)
const tryFor = ref<Deployment | null>(null)
const copied = ref(false)

const statusChip: Record<string, string> = {
  live: 'bg-emerald-500/15 text-emerald-300',
  disabled: 'bg-zinc-600/30 text-zinc-400',
  inactive: 'bg-amber-500/15 text-amber-300',
  orphaned: 'bg-rose-500/15 text-rose-300',
}
const envChip: Record<string, string> = {
  dev: 'bg-sky-500/15 text-sky-300',
  staging: 'bg-amber-500/15 text-amber-300',
  prod: 'bg-fuchsia-500/15 text-fuchsia-300',
}

const liveCount = computed(() => deployments.value.filter(d => d.status === 'live').length)
const calls7d = computed(() => deployments.value.reduce((s, d) => s + (d.stats?.runs_7d || 0), 0))

async function load() {
  loading.value = true
  try {
    deployments.value = (await api.get<{ deployments: Deployment[] }>('/deployments')).deployments
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load deployments'
  } finally {
    loading.value = false
  }
}

async function openCreate() {
  showCreate.value = true
  newName.value = ''
  newModel.value = ''
  newEnv.value = 'dev'
  newNotes.value = ''
  newMaxTokens.value = null
  newTemp.value = null
  tryResult.value = null
  try {
    models.value = await api.get<any[]>('/models')
  } catch { models.value = [] }
}

async function create() {
  if (!newName.value.trim() || !newModel.value || creating.value) return
  creating.value = true
  try {
    const body: any = { name: newName.value.trim(), model: newModel.value, environment: newEnv.value, notes: newNotes.value }
    if (newMaxTokens.value != null) body.max_tokens = newMaxTokens.value
    if (newTemp.value != null) body.temperature = newTemp.value
    await api.post('/deployments', body)
    showCreate.value = false
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Deployment failed'
  } finally {
    creating.value = false
  }
}

async function toggle(d: Deployment) {
  try {
    await api.post(`/deployments/${d.id}/toggle`)
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Toggle failed'
  }
}

async function retire(d: Deployment) {
  try {
    await api.delete(`/deployments/${d.id}`)
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Retire failed'
  }
}

function openTry(d: Deployment) {
  tryFor.value = d
  tryBody.value = d.serving_mode === 'generate'
    ? JSON.stringify({ prompt: 'the support agent resolved the' }, null, 2)
    : JSON.stringify({ rows: [Object.fromEntries((d.model.features || []).slice(0, 4).map(f => [f, 0]))] }, null, 2)
  tryResult.value = null
}

async function invoke() {
  if (!tryFor.value?.workflow || trying.value) return
  trying.value = tryFor.value.id
  tryResult.value = null
  try {
    const parsed = JSON.parse(tryBody.value || '{}')
    const res = await api.post<any>(`/webhooks/${tryFor.value.workflow.id}`, parsed)
    const last = res?.last_output
    const text = tryFor.value.serving_mode === 'generate'
      ? (last?.text || JSON.stringify(last))
      : JSON.stringify(last?.items || last, null, 2)
    tryResult.value = { ok: res?.status === 'success', text: String(text).slice(0, 2000) }
  } catch (e: any) {
    tryResult.value = { ok: false, text: e?.data?.detail || e?.message || 'invocation failed' }
  } finally {
    trying.value = null
  }
}

function curlFor(d: Deployment) {
  if (!d.workflow) return ''
  const example = d.serving_mode === 'generate'
    ? '{"prompt": "your prompt text"}'
    : `{"rows": [${JSON.stringify(Object.fromEntries((d.model.features || ['feature']).slice(0, 3).map(f => [f, 0])))}]}`
  return `curl -X POST ${origin()}${d.workflow.webhook_path} -H 'Content-Type: application/json' -d '${example}'`
}

function origin() {
  return typeof location !== 'undefined' ? location.origin : ''
}

async function copyCurl(d: Deployment) {
  try {
    await navigator.clipboard.writeText(curlFor(d))
    copied.value = true
    setTimeout(() => (copied.value = false), 1200)
  } catch { /* clipboard unavailable */ }
}

onMounted(load)
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 py-8 lg:px-8">
    <div class="mb-6 flex flex-wrap items-center gap-3">
      <div class="flex h-10 w-10 items-center justify-center rounded-xl bg-rose-500/15">
        <Rocket class="h-5 w-5 text-rose-400" />
      </div>
      <div class="min-w-0 flex-1">
        <h1 class="text-xl font-bold text-zinc-100">Deployments</h1>
        <p class="text-xs text-zinc-500">Registry rows served as live webhook endpoints - generate or predict over HTTP, stats derived from the run log.</p>
      </div>
      <div v-if="deployments.length" class="flex items-center gap-2 text-xs">
        <span class="rounded-full bg-emerald-500/15 px-2.5 py-1 text-emerald-300">{{ liveCount }} live</span>
        <span class="rounded-full bg-zinc-800/80 px-2.5 py-1 text-zinc-400">{{ calls7d }} calls / 7d</span>
      </div>
      <button
        class="flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-2 text-xs font-semibold text-white hover:bg-rose-500"
        @click="openCreate"
      >
        <Plus class="h-3.5 w-3.5" /> Deploy a model
      </button>
    </div>

    <p v-if="pageError" class="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ pageError }}</p>

    <div v-if="loading" class="flex items-center justify-center py-20 text-zinc-500">
      <Loader2 class="h-5 w-5 animate-spin" />
    </div>

    <div v-else-if="!deployments.length" class="rounded-2xl border border-dashed border-zinc-700/80 p-10 text-center">
      <Rocket class="mx-auto mb-3 h-8 w-8 text-zinc-600" />
      <p class="text-sm font-semibold text-zinc-300">Nothing is deployed yet</p>
      <p class="mx-auto mt-1 max-w-md text-xs leading-relaxed text-zinc-500">
        Train a model (model_train, neural_train or lm_train), then deploy it: py8n generates the
        serving workflow, activates it, and hands you a webhook endpoint that answers with the
        model's output.
      </p>
    </div>

    <div v-else class="space-y-3">
      <div v-for="d in deployments" :key="d.id" class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-sm font-bold text-zinc-100">{{ d.name }}</span>
          <span class="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase" :class="statusChip[d.status] || 'bg-zinc-700/40 text-zinc-300'">{{ d.status }}</span>
          <span class="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase" :class="envChip[d.environment] || 'bg-zinc-700/40 text-zinc-300'">{{ d.environment }}</span>
          <span class="rounded-full bg-zinc-800/80 px-2 py-0.5 text-[10px] text-zinc-400">{{ d.serving_mode === 'generate' ? 'language model' : 'predict' }}</span>
          <div class="ml-auto flex items-center gap-1.5">
            <button
              v-if="d.workflow"
              class="flex items-center gap-1 rounded-lg bg-zinc-800 px-2.5 py-1.5 text-[11px] font-semibold text-zinc-200 hover:bg-zinc-700"
              @click="openTry(d)"
            >
              <Play class="h-3 w-3" /> Try it
            </button>
            <button
              class="flex items-center gap-1 rounded-lg bg-zinc-800 px-2.5 py-1.5 text-[11px] font-semibold text-zinc-200 hover:bg-zinc-700"
              :title="d.enabled ? 'Disable the endpoint' : 'Enable the endpoint'"
              @click="toggle(d)"
            >
              <Ban v-if="d.enabled" class="h-3 w-3" />
              <CirclePlay v-else class="h-3 w-3" />
              {{ d.enabled ? 'Disable' : 'Enable' }}
            </button>
            <button
              class="rounded-lg bg-zinc-800 p-1.5 text-zinc-400 hover:bg-rose-500/20 hover:text-rose-300"
              title="Retire (the workflow survives, deactivated)"
              @click="retire(d)"
            >
              <Trash2 class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-500">
          <span>
            model
            <span class="font-semibold text-zinc-300">{{ d.model.name || 'deleted' }}</span>
            <span v-if="d.model.version != null" class="text-zinc-500">&nbsp;v{{ d.model.version }}</span>
            <span v-if="d.model.algorithm" class="text-zinc-600">&nbsp;· {{ d.model.algorithm }}</span>
          </span>
          <span>{{ d.stats.runs_7d }} calls / 7d</span>
          <span v-if="d.stats.failures_7d" class="text-rose-400">{{ d.stats.failures_7d }} failed</span>
          <span v-if="d.stats.last_call_at">last {{ new Date(d.stats.last_call_at).toLocaleString() }} ({{ d.stats.last_call_status }})</span>
        </div>

        <div v-if="d.workflow" class="mt-2 flex items-center gap-2 rounded-lg bg-zinc-950/60 px-2.5 py-1.5">
          <code class="min-w-0 flex-1 truncate text-[10px] text-zinc-500">{{ curlFor(d) }}</code>
          <button class="shrink-0 text-zinc-500 hover:text-zinc-300" title="Copy" @click="copyCurl(d)">
            <Check v-if="copied" class="h-3.5 w-3.5 text-emerald-400" />
            <Copy v-else class="h-3.5 w-3.5" />
          </button>
        </div>

        <div v-if="tryFor?.id === d.id" class="mt-3 rounded-xl border border-zinc-700/70 bg-zinc-950/70 p-3">
          <p class="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-zinc-400">Invoke {{ d.serving_mode === 'generate' ? 'with a prompt' : 'with rows' }}</p>
          <textarea
            v-model="tryBody"
            rows="4"
            class="w-full rounded-lg border border-zinc-700/70 bg-zinc-900 px-2.5 py-2 font-mono text-[11px] text-zinc-200 outline-none focus:border-rose-500/60"
          />
          <div class="mt-2 flex items-center gap-2">
            <button
              class="flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-1.5 text-[11px] font-semibold text-white hover:bg-rose-500 disabled:opacity-50"
              :disabled="trying === d.id"
              @click="invoke"
            >
              <Zap v-if="trying !== d.id" class="h-3 w-3" />
              <Loader2 v-else class="h-3 w-3 animate-spin" />
              Send request
            </button>
            <button class="text-[11px] text-zinc-500 hover:text-zinc-300" @click="tryFor = null; tryResult = null">close</button>
          </div>
          <pre
            v-if="tryResult"
            class="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border px-2.5 py-2 font-mono text-[11px]"
            :class="tryResult.ok ? 'border-emerald-500/30 bg-emerald-500/5 text-emerald-200' : 'border-rose-500/30 bg-rose-500/5 text-rose-200'"
          >{{ tryResult.text }}</pre>
        </div>
      </div>
    </div>

    <!-- create modal -->
    <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" @click.self="showCreate = false">
      <div class="w-full max-w-md rounded-2xl border border-zinc-700 bg-zinc-900 p-5">
        <div class="mb-4 flex items-center gap-2">
          <Rocket class="h-4 w-4 text-rose-400" />
          <h2 class="text-sm font-bold text-zinc-100">Deploy a model</h2>
          <button class="ml-auto text-zinc-500 hover:text-zinc-300" @click="showCreate = false"><X class="h-4 w-4" /></button>
        </div>
        <label class="mb-3 block">
          <span class="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400">Endpoint name</span>
          <input
            v-model="newName"
            class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 outline-none focus:border-rose-500/60"
            placeholder="churn scorer"
          />
        </label>
        <label class="mb-3 block">
          <span class="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400">Model (ACTIVE version)</span>
          <select
            v-model="newModel"
            class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 outline-none focus:border-rose-500/60"
          >
            <option value="" disabled>pick a registry row</option>
            <option v-for="m in models" :key="m.id" :value="m.name">
              {{ m.name }} v{{ m.version }} · {{ m.algorithm }}{{ m.active ? ' · active' : '' }}
            </option>
          </select>
        </label>
        <div class="mb-3 grid grid-cols-3 gap-2">
          <label>
            <span class="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400">Environment</span>
            <select
              v-model="newEnv"
              class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-200 outline-none focus:border-rose-500/60"
            >
              <option value="dev">dev</option>
              <option value="staging">staging</option>
              <option value="prod">prod</option>
            </select>
          </label>
          <label>
            <span class="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400">Max tokens</span>
            <input
              v-model.number="newMaxTokens"
              type="number"
              min="1"
              max="512"
              placeholder="16"
              class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-200 outline-none focus:border-rose-500/60"
            />
          </label>
          <label>
            <span class="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400">Temp</span>
            <input
              v-model.number="newTemp"
              type="number"
              step="0.1"
              min="0.1"
              max="2"
              placeholder="0.8"
              class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-2 text-sm text-zinc-200 outline-none focus:border-rose-500/60"
            />
          </label>
        </div>
        <label class="mb-4 block">
          <span class="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-zinc-400">Notes</span>
          <input
            v-model="newNotes"
            class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 outline-none focus:border-rose-500/60"
            placeholder="what this endpoint is for (optional)"
          />
        </label>
        <p class="mb-4 text-[11px] leading-relaxed text-zinc-500">
          py8n generates the serving workflow, activates it and pins THIS registry version - retrain and
          deploy again for a new version. Language models take <code class="text-zinc-400">{"prompt": "..."}</code>;
          tabular models take <code class="text-zinc-400">{"rows": [...]}</code>.
        </p>
        <div class="flex justify-end gap-2">
          <button class="rounded-lg px-3 py-2 text-xs font-semibold text-zinc-400 hover:text-zinc-200" @click="showCreate = false">Cancel</button>
          <button
            class="rounded-lg bg-rose-600 px-4 py-2 text-xs font-semibold text-white hover:bg-rose-500 disabled:opacity-50"
            :disabled="!newName.trim() || !newModel || creating"
            @click="create"
          >
            <Loader2 v-if="creating" class="h-3.5 w-3.5 animate-spin" />
            <span v-else>Deploy</span>
          </button>
        </div>
      </div>
    </div>

    <p v-if="!loading && deployments.length" class="mt-6 text-center text-[11px] text-zinc-600">
      Serving workflows are normal py8n workflows - open one to watch its executions or hang monitoring nodes off it.
    </p>
  </div>
</template>
