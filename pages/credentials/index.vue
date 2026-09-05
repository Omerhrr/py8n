<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  KeyRound, Plus, Loader2, Pencil, Trash2, Zap, ShieldCheck, ShieldX,
  CheckCircle2, XCircle, Globe, Lock, Mail, MessageSquare, Sparkles, CircleDot,
  RefreshCw, History,
} from 'lucide-vue-next'
import { usePy8nStore } from '~/stores/py8n'
import type { Credential, CredentialEvent, CredentialTestResult, CredentialUsage } from '~/types/node'

const store = usePy8nStore()
const { api } = useApi()

// ------------------------------------------------------------------ type meta
interface FieldDef { key: string; label: string; secret?: boolean; placeholder?: string; widget?: 'text' | 'number' | 'boolean' | 'textarea' }
interface CredTypeDef { label: string; icon: any; color: string; blurb: string; fields: FieldDef[] }

const CRED_TYPES: Record<string, CredTypeDef> = {
  header_auth: {
    label: 'Header Auth', icon: Lock, color: 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30',
    blurb: 'Sends a custom header (e.g. Authorization) with HTTP Request nodes',
    fields: [
      { key: 'header_name', label: 'Header name', placeholder: 'Authorization' },
      { key: 'value', label: 'Header value', secret: true, placeholder: 'Bearer … / token' },
    ],
  },
  basic_auth: {
    label: 'Basic Auth', icon: Lock, color: 'text-sky-400 bg-sky-500/10 border-sky-500/30',
    blurb: 'Username + password for HTTP Request nodes (RFC 7617)',
    fields: [
      { key: 'username', label: 'Username' },
      { key: 'password', label: 'Password', secret: true },
    ],
  },
  openai_compatible: {
    label: 'OpenAI-compatible', icon: Sparkles, color: 'text-violet-400 bg-violet-500/10 border-violet-500/30',
    blurb: 'Base URL + API key for LLM nodes (OpenAI, DeepSeek, Kimi, Qwen, OpenRouter, Groq, local runtimes…)',
    fields: [
      { key: 'provider', label: 'Provider preset', placeholder: 'openai | deepseek | kimi | qwen | openrouter | groq | ...' },
      { key: 'base_url', label: 'Base URL', placeholder: 'https://api.openai.com/v1' },
      { key: 'api_key', label: 'API key', secret: true },
      { key: 'suggested_model', label: 'Suggested model (optional)', placeholder: 'gpt-4o-mini' },
    ],
  },
  anthropic: {
    label: 'Anthropic (Claude)', icon: Sparkles, color: 'text-orange-400 bg-orange-500/10 border-orange-500/30',
    blurb: 'Claude\'s native Messages API - routed natively by py8n (x-api-key + anthropic-version)',
    fields: [
      { key: 'provider', label: 'Provider preset', placeholder: 'anthropic' },
      { key: 'base_url', label: 'Base URL', placeholder: 'https://api.anthropic.com/v1' },
      { key: 'api_key', label: 'API key', secret: true, placeholder: 'sk-ant-…' },
      { key: 'suggested_model', label: 'Suggested model (optional)', placeholder: 'claude-sonnet-4-5' },
    ],
  },
  smtp: {
    label: 'SMTP', icon: Mail, color: 'text-amber-400 bg-amber-500/10 border-amber-500/30',
    blurb: 'Mail server account for Email Send nodes',
    fields: [
      { key: 'host', label: 'Host', placeholder: 'smtp.gmail.com' },
      { key: 'port', label: 'Port', widget: 'number', placeholder: '587' },
      { key: 'username', label: 'Username' },
      { key: 'password', label: 'Password', secret: true },
      { key: 'use_tls', label: 'Use STARTTLS', widget: 'boolean' },
    ],
  },
  slack: {
    label: 'Slack', icon: MessageSquare, color: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
    blurb: 'Incoming webhook URL or bot token (xoxb-…) for Slack nodes',
    fields: [
      { key: 'webhook_url', label: 'Incoming webhook URL', secret: true, widget: 'textarea', placeholder: 'https://hooks.slack.com/services/…' },
      { key: 'token', label: 'Bot token (optional)', secret: true, placeholder: 'xoxb-…' },
    ],
  },
  generic: {
    label: 'Generic', icon: CircleDot, color: 'text-zinc-400 bg-zinc-500/10 border-zinc-500/30',
    blurb: 'Free-form secret payload for nodes without a dedicated type',
    fields: [
      { key: 'token', label: 'Token / secret', secret: true, widget: 'textarea' },
      { key: 'webhook_url', label: 'Webhook URL (optional)', secret: true, widget: 'textarea' },
    ],
  },
}

function typeMeta(t: string): CredTypeDef {
  return CRED_TYPES[t] || CRED_TYPES.generic
}

// ------------------------------------------------------------------ state
const loading = ref(true)
const typeFilter = ref('')
const testing = ref<Record<string, boolean>>({})
const testResults = ref<Record<string, CredentialTestResult>>({})
const usage = ref<Record<string, CredentialUsage>>({})
const showCreate = ref(false)
const editing = ref<Credential | null>(null)
const pageError = ref('')

const credentials = computed(() => store.credentials)

const byType = computed(() => {
  const counts = new Map<string, number>()
  for (const c of credentials.value) counts.set(c.type, (counts.get(c.type) || 0) + 1)
  return [...counts.entries()]
})

const inUseCount = computed(() => Object.values(usage.value).filter((u) => u.workflow_count > 0).length)

const filtered = computed(() =>
  credentials.value.filter((c) => !typeFilter.value || c.type === typeFilter.value)
)

// ------------------------------------------------------------------ form state (create / edit)
const formName = ref('')
const formType = ref('header_auth')
const formData = ref<Record<string, any>>({})
const formSaving = ref(false)
const formError = ref('')

// v74: LLM provider presets (GET /credentials/providers) - one click fills
// provider + base_url + suggested model, so a credential is a preset + a key
const llmProviders = ref<any[]>([])
const llmPresets = computed(() => llmProviders.value.filter(p => !formType.value || p.credential_type === formType.value))

function applyPreset(p: any) {
  formType.value = p.credential_type
  formData.value = blankData(p.credential_type)
  formData.value.provider = p.provider
  formData.value.base_url = p.base_url
  if (p.default_model) formData.value.suggested_model = p.default_model
}

function openCreate() {
  editing.value = null
  formName.value = ''
  formError.value = ''
  formType.value = 'header_auth'
  formData.value = blankData('header_auth')
  showCreate.value = true
}

function blankData(type: string): Record<string, any> {
  const d: Record<string, any> = {}
  for (const f of typeMeta(type).fields) d[f.key] = f.widget === 'boolean' ? true : ''
  return d
}

function onFormTypeChange() {
  formData.value = blankData(formType.value)
}

async function openEdit(cred: Credential) {
  editing.value = cred
  formName.value = cred.name
  formType.value = cred.type
  formError.value = ''
  // Prefill from the edit-time detail view (non-secrets visible, secrets blank)
  try {
    const det = await api.get<{ data: Record<string, any> }>(`/credentials/${cred.id}`)
    const data: Record<string, any> = {}
    for (const f of typeMeta(cred.type).fields) {
      data[f.key] = det.data?.[f.key] ?? ''
    }
    formData.value = data
  } catch {
    formData.value = blankData(cred.type)
  }
  showCreate.value = true
}

async function saveForm() {
  formError.value = ''
  const name = formName.value.trim()
  if (!name) { formError.value = 'Name is required'; return }
  formSaving.value = true
  try {
    if (editing.value) {
      // Untouched secret fields are sent as __keep__ - the vault substitutes
      // the stored value without ever exposing it.
      const data: Record<string, any> = {}
      for (const f of typeMeta(formType.value).fields) {
        const v = formData.value[f.key]
        data[f.key] = f.secret && (v === '' || v === undefined) ? '__keep__' : v
      }
      await store.updateCredential(editing.value.id, { name, data })
    } else {
      await store.createCredential({ name, type: formType.value, data: { ...formData.value } })
    }
    showCreate.value = false
    void refreshUsage()
  } catch (e: any) {
    formError.value = e?.data?.detail || e?.message || 'Failed to save credential'
  } finally {
    formSaving.value = false
  }
}

// ------------------------------------------------------------------ actions
async function runTest(cred: Credential) {
  testing.value = { ...testing.value, [cred.id]: true }
  delete testResults.value[cred.id]
  try {
    const result = await store.testCredential(cred.id)
    testResults.value = { ...testResults.value, [cred.id]: result }
  } catch (e: any) {
    testResults.value = {
      ...testResults.value,
      [cred.id]: { ok: false, message: e?.data?.detail || e?.message || 'Test failed', latency_ms: 0, probed_at: '' },
    }
  } finally {
    testing.value = { ...testing.value, [cred.id]: false }
  }
}

async function refreshUsage() {
  // Collect first, assign once - avoids intermediate reactive churn and
  // survives credentials deleted mid-flight (their fetch is caught below).
  const entries: Record<string, CredentialUsage> = {}
  await Promise.all(
    store.credentials.map(async (c) => {
      try {
        entries[c.id] = await api.get<CredentialUsage>(`/credentials/${c.id}/usage`)
      } catch { /* deleted mid-flight - ignore */ }
    })
  )
  usage.value = entries
}

async function removeCred(cred: Credential) {
  const u = usage.value[cred.id]
  const msg = u && u.workflow_count > 0
    ? `Delete "${cred.name}"? It is used by ${u.workflow_count} workflow(s) (${u.workflows.map((w) => w.name).slice(0, 3).join(', ')}). Those nodes will fail until a new credential is attached.`
    : `Delete "${cred.name}"? This cannot be undone.`
  if (!window.confirm(msg)) return
  pageError.value = ''
  try {
    await store.deleteCredential(cred.id)
    delete usage.value[cred.id]
    delete testResults.value[cred.id]
    usage.value = { ...usage.value }
    testResults.value = { ...testResults.value }
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Delete failed'
  }
}

// ------------------------------------------------------------------ rotation + audit (v43)
const rotating = ref<Credential | null>(null)
const rotateData = ref<Record<string, string>>({})
const rotateSaving = ref(false)
const rotateError = ref('')
const rotateDone = ref('')

function openRotate(cred: Credential) {
  rotating.value = cred
  rotateError.value = ''
  rotateData.value = {}
  for (const f of typeMeta(cred.type).fields) {
    if (f.secret) rotateData.value[f.key] = ''
  }
}

async function submitRotate() {
  if (!rotating.value) return
  const secrets: Record<string, string> = {}
  for (const [k, v] of Object.entries(rotateData.value)) {
    if (v !== '') secrets[k] = v
  }
  if (!Object.keys(secrets).length) {
    rotateError.value = 'Type at least one new secret value'
    return
  }
  rotateSaving.value = true
  rotateError.value = ''
  try {
    await api.post(`/credentials/${rotating.value.id}/rotate`, { secrets })
    rotateDone.value = `Rotated ${Object.keys(secrets).join(', ')} on "${rotating.value.name}"`
    rotating.value = null
    await store.loadCredentials()
    void toggleAuditReload()
  } catch (e: any) {
    rotateError.value = e?.data?.detail || e?.message || 'Rotation failed'
  } finally {
    rotateSaving.value = false
  }
}

// audit trail: fetched per credential on first expand, refreshed after rotations
const auditOpen = ref<Record<string, boolean>>({})
const auditEvents = ref<Record<string, CredentialEvent[]>>({})
const auditLoading = ref<Record<string, boolean>>({})

const ACTION_STYLE: Record<string, string> = {
  created: 'bg-emerald-500/10 text-emerald-400',
  rotated: 'bg-amber-500/10 text-amber-400',
  updated: 'bg-sky-500/10 text-sky-400',
  renamed: 'bg-violet-500/10 text-violet-400',
  tested: 'bg-cyan-500/10 text-cyan-400',
  used: 'bg-zinc-700/40 text-zinc-300',
  deleted: 'bg-rose-500/10 text-rose-400',
}

function eventLine(e: CredentialEvent): string {
  const d = e.detail || {}
  if (e.action === 'created') return `${d.type || 'credential'}: ${(d.fields || []).join(', ')}`
  if (e.action === 'rotated') return `fields ${(d.fields || []).join(', ')}${(d.changed || []).length ? '' : ' (no change)'}`
  if (e.action === 'updated') return `fields ${(d.fields || []).join(', ')}`
  if (e.action === 'renamed') return `${d.from || '?'} -> ${d.to || '?'}`
  if (e.action === 'tested') return d.ok ? `probe ok${d.message ? `: ${d.message}` : ''}` : `probe failed${d.message ? `: ${d.message}` : ''}`
  if (e.action === 'used') return d.workflow_name ? `workflow "${d.workflow_name}"` : 'workflow run'
  if (e.action === 'deleted') return `fields ${(d.fields || []).join(', ')}`
  return ''
}

async function toggleAudit(cred: Credential) {
  auditOpen.value = { ...auditOpen.value, [cred.id]: !auditOpen.value[cred.id] }
  if (auditOpen.value[cred.id] && !auditEvents.value[cred.id]) await loadAudit(cred.id)
}

async function toggleAuditReload() {
  // after a rotation: refresh any open trail (credentials list reloaded)
  const id = Object.keys(auditOpen.value).find((k) => auditOpen.value[k])
  if (id) await loadAudit(id)
}

async function loadAudit(id: string) {
  auditLoading.value = { ...auditLoading.value, [id]: true }
  try {
    auditEvents.value = { ...auditEvents.value, [id]: await api.get<CredentialEvent[]>(`/credentials/${id}/events`) }
  } catch { /* deleted mid-flight - ignore */ }
  finally {
    auditLoading.value = { ...auditLoading.value, [id]: false }
  }
}

// ------------------------------------------------------------------ helpers
function relTime(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

onMounted(async () => {
  try {
    await store.loadCredentials()
    await refreshUsage()
    llmProviders.value = (await api.get<{ providers: any[] }>('/credentials/providers')).providers || []
  } catch {
    loading.value = false
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="text-zinc-100">
    <!-- page header -->
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <KeyRound class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Credentials</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Fernet-encrypted vault - secrets never leave the server</p>
          </div>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-xl bg-orange-500 px-3.5 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 active:scale-[0.98]"
          @click="openCreate"
        >
          <Plus class="h-4 w-4" /> New credential
        </button>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <!-- stats strip -->
      <section class="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
          <div class="text-2xl font-bold tabular-nums">{{ credentials.length }}</div>
          <div class="text-[11px] uppercase tracking-wide text-zinc-500">Total</div>
        </div>
        <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
          <div class="text-2xl font-bold tabular-nums text-emerald-400">{{ inUseCount }}</div>
          <div class="text-[11px] uppercase tracking-wide text-zinc-500">In use</div>
        </div>
        <div class="col-span-2 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
          <div class="mb-1.5 text-[11px] uppercase tracking-wide text-zinc-500">By type</div>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="[t, n] in byType"
              :key="t"
              class="flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs font-medium transition"
              :class="[typeMeta(t).color, typeFilter === t ? 'ring-1 ring-orange-400/60' : '', 'hover:brightness-125']"
              @click="typeFilter = typeFilter === t ? '' : t"
            >
              <component :is="typeMeta(t).icon" class="h-3 w-3" />
              {{ typeMeta(t).label }} <span class="tabular-nums opacity-70">{{ n }}</span>
            </button>
            <span v-if="!byType.length" class="text-xs text-zinc-600">-</span>
          </div>
        </div>
      </section>

      <!-- page-level error (delete conflicts) -->
      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-300">
        <ShieldX class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageError }}
      </div>

      <!-- list -->
      <div v-if="loading" class="flex items-center justify-center py-20 text-zinc-500">
        <Loader2 class="mr-2 h-5 w-5 animate-spin" /> Loading vault…
      </div>

      <div v-else-if="!filtered.length" class="rounded-2xl border border-dashed border-zinc-800 bg-zinc-900/30 py-16 text-center">
        <KeyRound class="mx-auto mb-3 h-10 w-10 text-zinc-700" />
        <p class="text-sm font-medium text-zinc-400">
          {{ credentials.length ? 'No credentials match this filter' : 'No credentials yet' }}
        </p>
        <p class="mx-auto mt-1 max-w-sm text-xs text-zinc-600">
          Credentials store API keys, tokens and mail accounts - encrypted at rest and referenced from node parameters.
        </p>
        <button
          class="mt-4 inline-flex items-center gap-1.5 rounded-xl bg-orange-500 px-3.5 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400"
          @click="openCreate"
        >
          <Plus class="h-4 w-4" /> New credential
        </button>
      </div>

      <div v-else class="space-y-2.5">
        <div
          v-for="cred in filtered"
          :key="cred.id"
          class="group rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4 transition hover:border-zinc-700"
        >
          <div class="flex flex-wrap items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border" :class="typeMeta(cred.type).color">
              <component :is="typeMeta(cred.type).icon" class="h-4 w-4" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="truncate text-sm font-semibold">{{ cred.name }}</span>
                <span class="rounded-md border px-1.5 py-0.5 text-[10px] font-medium" :class="typeMeta(cred.type).color">
                  {{ typeMeta(cred.type).label }}
                </span>
                <span
                  v-if="usage[cred.id]"
                  class="rounded-md px-1.5 py-0.5 text-[10px] font-medium"
                  :class="usage[cred.id].workflow_count > 0 ? 'bg-emerald-500/10 text-emerald-400' : 'bg-zinc-800 text-zinc-500'"
                >
                  {{ usage[cred.id].workflow_count }} workflow{{ usage[cred.id].workflow_count === 1 ? '' : 's' }}
                </span>
              </div>
              <div class="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
                <span class="font-mono">{{ cred.masked_hint }}</span>
                <span>·</span>
                <span>created {{ relTime(cred.created_at) }}</span>
                <template v-if="cred.rotated_at">
                  <span>·</span>
                  <span class="text-amber-400/80">rotated {{ relTime(cred.rotated_at) }}</span>
                </template>
                <span>·</span>
                <span class="truncate">{{ typeMeta(cred.type).blurb }}</span>
              </div>
            </div>

            <!-- test result -->
            <div
              v-if="testResults[cred.id]"
              class="flex max-w-full items-start gap-1.5 rounded-xl border px-3 py-1.5 text-xs lg:max-w-md"
              :class="testResults[cred.id].ok
                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                : 'border-rose-500/30 bg-rose-500/10 text-rose-300'"
            >
              <component :is="testResults[cred.id].ok ? CheckCircle2 : XCircle" class="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span class="min-w-0">
                {{ testResults[cred.id].message }}
                <span v-if="testResults[cred.id].ok" class="opacity-60">({{ testResults[cred.id].latency_ms }}ms)</span>
              </span>
            </div>

            <!-- actions -->
            <div class="flex shrink-0 items-center gap-1.5">
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-amber-500/40 hover:text-amber-300 disabled:opacity-50"
                title="Replace secret values without touching the rest of the config"
                @click="openRotate(cred)"
              >
                <RefreshCw class="h-3.5 w-3.5" />
                Rotate
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-100"
                :class="auditOpen[cred.id] ? 'border-zinc-600 text-zinc-100' : ''"
                title="Audit trail"
                @click="toggleAudit(cred)"
              >
                <History class="h-3.5 w-3.5" />
              </button>
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-emerald-500/40 hover:text-emerald-300 disabled:opacity-50"
                :disabled="testing[cred.id]"
                title="Run a live connection test"
                @click="runTest(cred)"
              >
                <Loader2 v-if="testing[cred.id]" class="h-3.5 w-3.5 animate-spin" />
                <Zap v-else class="h-3.5 w-3.5" />
                Test
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-100"
                title="Edit credential"
                @click="openEdit(cred)"
              >
                <Pencil class="h-3.5 w-3.5" />
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-rose-500/40 hover:text-rose-300"
                title="Delete credential"
                @click="removeCred(cred)"
              >
                <Trash2 class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>

          <!-- audit trail (expandable) -->
          <div v-if="auditOpen[cred.id]" class="mt-3 border-t border-zinc-800/70 pt-3">
            <div v-if="auditLoading[cred.id]" class="flex items-center gap-2 px-1 py-2 text-xs text-zinc-500">
              <Loader2 class="h-3.5 w-3.5 animate-spin" /> Loading audit trail…
            </div>
            <div v-else-if="!(auditEvents[cred.id] || []).length" class="px-1 py-1 text-xs text-zinc-600">No audit events yet</div>
            <ol v-else class="space-y-1.5">
              <li v-for="ev in auditEvents[cred.id]" :key="ev.id" class="flex items-start gap-2 text-xs">
                <span
                  class="w-16 shrink-0 rounded-md px-1.5 py-0.5 text-center text-[10px] font-semibold uppercase tracking-wide"
                  :class="ACTION_STYLE[ev.action] || 'bg-zinc-800 text-zinc-400'"
                >{{ ev.action }}</span>
                <span class="min-w-0 flex-1 truncate text-zinc-400">{{ eventLine(ev) }}</span>
                <span class="shrink-0 tabular-nums text-zinc-600">{{ relTime(ev.created_at) }}</span>
              </li>
            </ol>
            <p class="mt-2 px-1 text-[10px] text-zinc-600">Secret values are never recorded - only field names and workflow references.</p>
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
            <h2 class="flex items-center gap-2 text-sm font-bold">
              <Globe class="h-4 w-4 text-orange-400" />
              {{ editing ? 'Edit credential' : 'New credential' }}
            </h2>
            <p class="mt-0.5 text-[11px] text-zinc-500">Encrypted at rest (Fernet) - secrets are never returned by the API</p>
          </div>

          <div class="space-y-3.5 px-5 py-4">
            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Name</span>
              <input
                v-model="formName"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                placeholder="e.g. Prod Slack webhook"
              />
            </label>

            <label v-if="!editing" class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Type</span>
              <select
                v-model="formType"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none focus:border-orange-500/60"
                @change="onFormTypeChange"
              >
                <option v-for="(meta, t) in CRED_TYPES" :key="t" :value="t">{{ meta.label }}</option>
              </select>
            </label>

            <div v-if="!editing && llmPresets.length" class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Provider presets (one click fills the endpoint)</span>
              <div class="flex flex-wrap gap-1.5">
                <button
                  v-for="p in llmPresets"
                  :key="p.provider"
                  type="button"
                  class="rounded-full border px-2.5 py-1 text-[11px] transition"
                  :class="formData.provider === p.provider ? 'border-orange-500/60 bg-orange-500/10 text-orange-300' : 'border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-200'"
                  @click="applyPreset(p)"
                >{{ p.label }}</button>
              </div>
            </div>

            <div class="space-y-2.5">
              <label v-for="f in typeMeta(formType).fields" :key="f.key" class="block">
                <span class="mb-1 flex items-center justify-between text-xs font-medium text-zinc-400">
                  {{ f.label }}
                  <span v-if="editing && f.secret" class="text-[10px] font-normal text-zinc-600">leave blank to keep current</span>
                </span>
                <textarea
                  v-if="f.widget === 'textarea'"
                  v-model="formData[f.key]"
                  rows="2"
                  class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-xs outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                  :placeholder="f.placeholder || ''"
                />
                <input
                  v-else-if="f.widget === 'boolean'"
                  v-model="formData[f.key]"
                  type="checkbox"
                  class="h-4 w-4 accent-orange-500"
                />
                <input
                  v-else
                  v-model="formData[f.key]"
                  :type="f.widget === 'number' ? 'number' : f.secret ? 'password' : 'text'"
                  class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                  :placeholder="f.placeholder || ''"
                />
              </label>
            </div>

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
              :disabled="formSaving"
              @click="saveForm"
            >
              <Loader2 v-if="formSaving" class="h-3.5 w-3.5 animate-spin" />
              <ShieldCheck v-else class="h-3.5 w-3.5" />
              {{ editing ? 'Save changes' : 'Create credential' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- rotate modal (v43) -->
    <Teleport to="body">
      <div
        v-if="rotating"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="rotating = null"
      >
        <div class="w-full max-w-md rounded-2xl border border-amber-500/30 bg-zinc-950 shadow-2xl">
          <div class="border-b border-zinc-800/80 px-5 py-4">
            <h2 class="flex items-center gap-2 text-sm font-bold">
              <RefreshCw class="h-4 w-4 text-amber-400" />
              Rotate "{{ rotating.name }}"
            </h2>
            <p class="mt-0.5 text-[11px] text-zinc-500">Only the fields you fill in are replaced - everything else (URLs, usernames, header names) carries over. The old value stops working the moment you save.</p>
          </div>

          <div class="space-y-3.5 px-5 py-4">
            <label v-for="f in typeMeta(rotating.type).fields.filter((x) => x.secret)" :key="f.key" class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">{{ f.label }}</span>
              <input
                v-model="rotateData[f.key]"
                type="password"
                autocomplete="off"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-sm outline-none transition placeholder:text-zinc-600 focus:border-amber-500/60"
                placeholder="leave blank to keep the current value"
              />
            </label>
            <p v-if="rotateError" class="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ rotateError }}</p>
          </div>

          <div class="flex justify-end gap-2 border-t border-zinc-800/80 px-5 py-3.5">
            <button
              class="rounded-xl border border-zinc-800 px-3.5 py-2 text-sm font-medium text-zinc-400 transition hover:text-zinc-100"
              @click="rotating = null"
            >
              Cancel
            </button>
            <button
              class="flex items-center gap-1.5 rounded-xl bg-amber-500 px-3.5 py-2 text-sm font-semibold text-zinc-950 shadow-lg shadow-amber-500/20 transition hover:bg-amber-400 disabled:opacity-50"
              :disabled="rotateSaving"
              @click="submitRotate"
            >
              <Loader2 v-if="rotateSaving" class="h-3.5 w-3.5 animate-spin" />
              <RefreshCw v-else class="h-3.5 w-3.5" />
              Rotate secret
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- rotation success toast -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="rotateDone"
          class="fixed bottom-6 right-6 z-50 flex items-center gap-2 rounded-xl border border-emerald-500/40 bg-zinc-950 px-4 py-3 text-sm text-emerald-300 shadow-2xl"
        >
          <CheckCircle2 class="h-4 w-4" /> {{ rotateDone }}
          <button class="ml-2 text-xs text-zinc-500 hover:text-zinc-200" @click="rotateDone = ''">dismiss</button>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
