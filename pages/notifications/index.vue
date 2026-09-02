<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  BellRing, Plus, Loader2, Trash2, Pencil, Zap, CheckCircle2, XCircle, Webhook, Power,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v44: webhook-on-event rules - POST a JSON payload to your webhook whenever
// a run succeeds / fails / is cancelled. Fire-and-forget: a dead webhook can
// never slow or break a run.
interface NotificationRule {
  id: string
  name: string
  events: string[]
  webhook_url: string
  headers: Record<string, string>
  workflow_id: string | null
  workflow_name: string | null
  enabled: boolean
  created_at: string | null
  last_fired_at: string | null
  fire_count: number
  last_status: string | null
  last_error: string | null
}

const { api } = useApi()
const loading = ref(true)
const rules = ref<NotificationRule[]>([])
const workflows = ref<{ id: string; name: string }[]>([])

const showCreate = ref(false)
const editing = ref<NotificationRule | null>(null)
const formName = ref('')
const formEvents = ref<string[]>(['execution_failed'])
const formUrl = ref('')
const formHeaders = ref('')
const formWorkflowId = ref('')
const formEnabled = ref(true)
const formSaving = ref(false)
const formError = ref('')

const testing = ref<string | null>(null)
const testResult = ref<Record<string, { ok: boolean; status_code: number | null; last_error: string | null }>>({})
const toggling = ref<string | null>(null)
const pageError = ref('')

const EVENT_LABELS: Record<string, string> = {
  execution_succeeded: 'Run succeeded',
  execution_failed: 'Run failed',
  execution_cancelled: 'Run cancelled',
  drift_detected: 'Model drift detected',
}

const EVENT_STYLE: Record<string, string> = {
  execution_succeeded: 'bg-emerald-500/10 text-emerald-400',
  execution_failed: 'bg-rose-500/10 text-rose-400',
  execution_cancelled: 'bg-amber-500/10 text-amber-400',
  drift_detected: 'bg-fuchsia-500/10 text-fuchsia-400',
}

// v48: the catalog is server-truth (GET /notifications/events) so new events
// appear here without a frontend release; labels/styles above are cosmetic.
const eventCatalog = ref<string[]>(Object.keys(EVENT_LABELS))

async function loadAll() {
  loading.value = true
  try {
    rules.value = await api.get<NotificationRule[]>('/notifications')
    workflows.value = await api.get<{ id: string; name: string }[]>('/workflows')
    try {
      const cat = await api.get<{ events: string[] }>('/notifications/events')
      if (Array.isArray(cat.events) && cat.events.length) eventCatalog.value = cat.events
    } catch {
      /* keep the static catalog */
    }
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

function openCreate() {
  editing.value = null
  formName.value = ''
  formEvents.value = ['execution_failed']
  formUrl.value = ''
  formHeaders.value = ''
  formWorkflowId.value = ''
  formEnabled.value = true
  formError.value = ''
  showCreate.value = true
}

function openEdit(rule: NotificationRule) {
  editing.value = rule
  formName.value = rule.name
  formEvents.value = [...rule.events]
  formUrl.value = rule.webhook_url
  formHeaders.value = Object.keys(rule.headers || {}).length ? JSON.stringify(rule.headers, null, 2) : ''
  formWorkflowId.value = rule.workflow_id || ''
  formEnabled.value = rule.enabled
  formError.value = ''
  showCreate.value = true
}

function parseHeaders(): Record<string, string> | null {
  const raw = formHeaders.value.trim()
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || Array.isArray(parsed) || parsed === null) return null
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(parsed)) out[k] = String(v)
    return out
  } catch {
    return null
  }
}

async function saveForm() {
  if (!formName.value.trim() || !formUrl.value.trim() || !formEvents.value.length) return
  const headers = parseHeaders()
  if (headers === null) {
    formError.value = 'Headers must be valid JSON, e.g. {"Authorization": "Bearer ..."}'
    return
  }
  formSaving.value = true
  formError.value = ''
  const body = {
    name: formName.value.trim(),
    events: formEvents.value,
    webhook_url: formUrl.value.trim(),
    headers,
    workflow_id: formWorkflowId.value || null,
    enabled: formEnabled.value,
  }
  try {
    if (editing.value) {
      await api.put(`/notifications/${editing.value.id}`, body)
    } else {
      await api.post('/notifications', body)
    }
    showCreate.value = false
    await loadAll()
  } catch (e: any) {
    formError.value = e?.data?.detail || e?.message || 'Save failed'
  } finally {
    formSaving.value = false
  }
}

async function toggleRule(rule: NotificationRule) {
  toggling.value = rule.id
  try {
    await api.put(`/notifications/${rule.id}`, { enabled: !rule.enabled })
    rule.enabled = !rule.enabled
  } finally {
    toggling.value = null
  }
}

async function testRule(rule: NotificationRule) {
  testing.value = rule.id
  pageError.value = ''
  try {
    testResult.value = { ...testResult.value, [rule.id]: await api.post<any>(`/notifications/${rule.id}/test`) }
    await loadAll()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Test failed'
  } finally {
    testing.value = null
  }
}

async function removeRule(rule: NotificationRule) {
  if (!confirm(`Delete rule "${rule.name}"? Webhooks stop immediately.`)) return
  try {
    await api.del(`/notifications/${rule.id}`)
    await loadAll()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Delete failed'
  }
}

function fmtDate(iso: string | null) {
  if (!iso) return 'never'
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <BellRing class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Notifications</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Webhook-on-event - POST a payload when runs finish</p>
          </div>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-xl bg-orange-500 px-3.5 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 active:scale-[0.98]"
          @click="openCreate"
        >
          <Plus class="h-4 w-4" /> New rule
        </button>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <!-- how it works -->
      <div class="mb-6 flex items-start gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
        <Webhook class="mt-0.5 h-4 w-4 shrink-0 text-orange-400" />
        <p class="text-xs leading-relaxed text-zinc-400">
          When a matching run finishes, Py8n POSTs a JSON payload
          (<code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-orange-300">event, workflow_id, workflow_name, execution_id, status, error, duration_ms, ts</code>)
          to your webhook URL. Delivery is <span class="text-zinc-200">fire-and-forget with a 10s timeout</span> - a slow
          or dead endpoint never slows or breaks the run. Use
          <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-zinc-300">Test fire</code> to verify wiring instantly.
        </p>
      </div>

      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        <XCircle class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageError }}
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <div v-else-if="!rules.length" class="grid place-items-center rounded-2xl border border-dashed border-zinc-800 py-16 text-center">
        <BellRing class="mb-3 h-8 w-8 text-zinc-700" />
        <p class="text-sm font-medium text-zinc-400">No notification rules yet</p>
        <p class="mt-1 max-w-md text-xs text-zinc-600">
          Create a rule to ping Slack, Discord, n8n or your own endpoint the moment a run fails or finishes.
        </p>
      </div>

      <div v-else class="space-y-2.5">
        <div
          v-for="rule in rules"
          :key="rule.id"
          class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4 transition hover:border-zinc-700"
        >
          <div class="flex flex-wrap items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border" :class="rule.enabled ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400' : 'border-zinc-700 bg-zinc-800 text-zinc-500'">
              <BellRing class="h-4 w-4" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="truncate text-sm font-semibold">{{ rule.name }}</span>
                <span
                  class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                  :class="rule.enabled ? 'bg-emerald-500/15 text-emerald-400' : 'bg-zinc-800 text-zinc-500'"
                >{{ rule.enabled ? 'Enabled' : 'Paused' }}</span>
                <span
                  v-for="e in rule.events"
                  :key="e"
                  class="rounded px-1.5 py-0.5 text-[10px] font-medium"
                  :class="EVENT_STYLE[e] || 'bg-zinc-800 text-zinc-400'"
                >{{ EVENT_LABELS[e] || e }}</span>
                <span v-if="rule.workflow_name" class="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
                  scope: {{ rule.workflow_name }}
                </span>
              </div>
              <div class="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
                <span class="max-w-full truncate font-mono text-[10px]">{{ rule.webhook_url }}</span>
                <span>·</span>
                <span>fired {{ rule.fire_count }}x, last {{ fmtDate(rule.last_fired_at) }}</span>
                <template v-if="rule.last_status">
                  <span>·</span>
                  <span :class="rule.last_status === 'ok' ? 'text-emerald-400/80' : 'text-rose-300'">
                    {{ rule.last_status === 'ok' ? 'delivered' : (rule.last_error || 'error') }}
                  </span>
                </template>
              </div>
              <div
                v-if="testResult[rule.id]"
                class="mt-1.5 flex items-center gap-1.5 rounded-lg border px-2 py-1 text-[11px]"
                :class="testResult[rule.id].ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-rose-500/30 bg-rose-500/10 text-rose-300'"
              >
                <component :is="testResult[rule.id].ok ? CheckCircle2 : XCircle" class="h-3 w-3" />
                Test fire {{ testResult[rule.id].ok ? 'delivered' : 'failed' }}
                <span v-if="testResult[rule.id].status_code" class="opacity-70">(HTTP {{ testResult[rule.id].status_code }})</span>
                <span v-if="testResult[rule.id].last_error" class="opacity-70">{{ testResult[rule.id].last_error }}</span>
              </div>
            </div>

            <div class="flex shrink-0 items-center gap-1.5">
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-cyan-500/40 hover:text-cyan-300 disabled:opacity-50"
                :disabled="testing === rule.id"
                title="Deliver a sample payload right now"
                @click="testRule(rule)"
              >
                <Loader2 v-if="testing === rule.id" class="h-3.5 w-3.5 animate-spin" />
                <Zap v-else class="h-3.5 w-3.5" />
                Test fire
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-100 disabled:opacity-50"
                :disabled="toggling === rule.id"
                :title="rule.enabled ? 'Pause rule' : 'Resume rule'"
                @click="toggleRule(rule)"
              >
                <Loader2 v-if="toggling === rule.id" class="h-3.5 w-3.5 animate-spin" />
                <Power v-else class="h-3.5 w-3.5" :class="rule.enabled ? 'text-emerald-400' : ''" />
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-100"
                title="Edit rule"
                @click="openEdit(rule)"
              >
                <Pencil class="h-3.5 w-3.5" />
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-rose-500/40 hover:text-rose-300"
                title="Delete rule"
                @click="removeRule(rule)"
              >
                <Trash2 class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>

    <!-- create / edit modal -->
    <Teleport to="body">
      <div
        v-if="showCreate"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="showCreate = false"
      >
        <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="border-b border-zinc-800/80 px-5 py-4">
            <h2 class="text-sm font-bold">{{ editing ? 'Edit rule' : 'New notification rule' }}</h2>
            <p class="mt-0.5 text-[11px] text-zinc-500">Pick the events, point at a webhook, test fire.</p>
          </div>

          <div class="space-y-3.5 px-5 py-4">
            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Name</span>
              <input
                v-model="formName"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                placeholder="e.g. Alert ops on failures"
              />
            </label>

            <div>
              <span class="mb-1.5 block text-xs font-medium text-zinc-400">Events</span>
              <div class="flex flex-wrap gap-2">
                <button
                  v-for="ev in eventCatalog"
                  :key="ev"
                  class="rounded-xl border px-3 py-1.5 text-xs font-medium transition"
                  :class="formEvents.includes(ev)
                    ? `${EVENT_STYLE[ev] || 'bg-orange-500/10 text-orange-400'} border-transparent ring-1 ring-orange-400/50`
                    : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:border-zinc-600'"
                  @click="formEvents = formEvents.includes(ev) ? formEvents.filter((x) => x !== ev) : [...formEvents, ev]"
                >
                  {{ EVENT_LABELS[ev] || ev }}
                </button>
              </div>
            </div>

            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Webhook URL</span>
              <input
                v-model="formUrl"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-xs outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                placeholder="https://hooks.slack.com/services/... or any http(s) endpoint"
              />
            </label>

            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Extra headers (JSON, optional)</span>
              <textarea
                v-model="formHeaders"
                rows="2"
                spellcheck="false"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-xs outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                placeholder='{"Authorization": "Bearer ..."}'
              />
            </label>

            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Scope (optional)</span>
              <select
                v-model="formWorkflowId"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none focus:border-orange-500/60"
              >
                <option value="">Every workflow</option>
                <option v-for="w in workflows" :key="w.id" :value="w.id">{{ w.name }}</option>
              </select>
            </label>

            <label class="flex items-center gap-2 text-xs text-zinc-400">
              <input v-model="formEnabled" type="checkbox" class="h-4 w-4 accent-orange-500" />
              Enabled immediately
            </label>

            <p v-if="formError" class="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ formError }}</p>
          </div>

          <div class="flex justify-end gap-2 border-t border-zinc-800/80 px-5 py-3.5">
            <button
              class="rounded-xl border border-zinc-800 px-3.5 py-2 text-sm font-medium text-zinc-400 transition hover:text-zinc-100"
              @click="showCreate = false"
            >
              Cancel
            </button>
            <button
              class="flex items-center gap-1.5 rounded-xl bg-orange-500 px-3.5 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 disabled:opacity-50"
              :disabled="formSaving || !formName.trim() || !formUrl.trim() || !formEvents.length"
              @click="saveForm"
            >
              <Loader2 v-if="formSaving" class="h-3.5 w-3.5 animate-spin" />
              {{ editing ? 'Save changes' : 'Create rule' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
