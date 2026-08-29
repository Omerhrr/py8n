<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Trash2, KeyRound, Plus, ChevronDown, AlertTriangle, Settings2, RotateCcw, Ban, Pin, FlaskConical, Download, Loader2 } from 'lucide-vue-next'
import type { Credential, NodeDefinition, NodeSpec, NodeSettings, NodeTestResult, ParamProperty } from '~/types/node'

const props = defineProps<{
  node: NodeSpec | null
  definition: NodeDefinition | null
  credentials: Credential[]
  workflowId?: string
  runTestStep?: (nodeId: string, items: any) => Promise<NodeTestResult>
  loadLastOutput?: (nodeId: string) => Promise<any | null>
}>()

const emit = defineEmits<{
  (e: 'update-param', key: string, value: any): void
  (e: 'update-settings', patch: Partial<NodeSettings>): void
  (e: 'rename', name: string): void
  (e: 'delete'): void
  (e: 'toggle-disabled', value: boolean): void
  (e: 'update-pinned', value: any): void
  (e: 'create-credential', body: { name: string; type: string; data: Record<string, any> }): Promise<Credential> | void
}>()

const store = usePy8nStore()

const jinjaTip = '{{ nodes.<id>.output.<field> }}'

const properties = computed<Record<string, ParamProperty>>(() => props.definition?.parameters_schema?.properties || {})
const requiredFields = computed<string[]>(() => props.definition?.parameters_schema?.required || [])

interface FieldTask {
  key: string
  prop: ParamProperty
  widget: 'text' | 'textarea' | 'code' | 'number' | 'boolean' | 'select' | 'credential' | 'json' | 'workflow'
  options?: string[]
}

function classify(key: string, prop: ParamProperty): FieldTask {
  const type = prop.type || (prop.anyOf ? 'any' : 'string')
  if (prop.widget === 'credential' || key === 'credential_id') return { key, prop, widget: 'credential' }
  if (prop.widget === 'workflow' || (key === 'workflow_id' && type === 'string')) return { key, prop, widget: 'workflow' }
  if (prop.widget === 'select' || prop.enum || prop.options) {
    return { key, prop, widget: 'select', options: prop.options || prop.enum }
  }
  if (prop.widget === 'code') {
    const isPy = prop.language === 'python' || key === 'code'
    return { key, prop, widget: isPy ? 'textarea' : 'json' }
  }
  if (prop.widget === 'textarea') return { key, prop, widget: 'textarea' }
  if (type === 'boolean') return { key, prop, widget: 'boolean' }
  if (type === 'integer' || type === 'number') return { key, prop, widget: 'number' }
  if (type === 'object' || type === 'array') return { key, prop, widget: 'json' }
  return { key, prop, widget: 'text' }
}

const fields = computed<FieldTask[]>(() =>
  Object.entries(properties.value).map(([k, p]) => classify(k, p)),
)

// ---- JSON editing state (per-field raw text + parse errors) --------------
const jsonRaw = ref<Record<string, string>>({})
const jsonError = ref<Record<string, string>>({})

watch(
  () => props.node?.id,
  () => {
    jsonRaw.value = {}
    jsonError.value = {}
  },
  { immediate: true },
)

function jsonTextOf(key: string, value: any): string {
  if (jsonRaw.value[key] !== undefined) return jsonRaw.value[key]
  return value === undefined || value === null ? '' : JSON.stringify(value, null, 2)
}

function onJsonInput(key: string, raw: string) {
  jsonRaw.value[key] = raw
  if (!raw.trim()) {
    emit('update-param', key, {})
    delete jsonError.value[key]
    return
  }
  try {
    emit('update-param', key, JSON.parse(raw))
    delete jsonError.value[key]
  } catch (e: any) {
    jsonError.value[key] = 'Invalid JSON'
  }
}

// ---- credential creation --------------------------------------------------
const showCredForm = ref(false)
const credName = ref('')
const credType = ref<'openai_compatible' | 'header_auth' | 'basic_auth' | 'generic' | 'smtp'>('openai_compatible')
const credData = ref<Record<string, string>>({})

const credTypeFields: Record<string, { key: string; label: string; placeholder: string; secret?: boolean }[]> = {
  openai_compatible: [
    { key: 'base_url', label: 'Base URL', placeholder: 'https://api.openai.com/v1' },
    { key: 'api_key', label: 'API Key', placeholder: 'sk-…', secret: true },
  ],
  header_auth: [
    { key: 'header_name', label: 'Header name', placeholder: 'Authorization' },
    { key: 'value', label: 'Header value', placeholder: 'Bearer …', secret: true },
  ],
  basic_auth: [
    { key: 'username', label: 'Username', placeholder: 'user' },
    { key: 'password', label: 'Password', placeholder: 'secret', secret: true },
  ],
  generic: [{ key: 'token', label: 'Secret value', placeholder: 'secret', secret: true }],
  smtp: [
    { key: 'host', label: 'SMTP host', placeholder: 'smtp.gmail.com' },
    { key: 'port', label: 'Port', placeholder: '587' },
    { key: 'username', label: 'Username', placeholder: 'user@example.com' },
    { key: 'password', label: 'Password', placeholder: 'app password', secret: true },
    { key: 'use_tls', label: 'Use TLS (true/false)', placeholder: 'true' },
  ],
}

watch(credType, () => (credData.value = {}))

async function submitCredential() {
  if (!credName.value.trim()) return
  const cred = await emit('create-credential', {
    name: credName.value.trim(),
    type: credType.value,
    data: { ...credData.value },
  }) as unknown as Credential
  showCredForm.value = false
  credName.value = ''
  credData.value = {}
  if (cred?.id && props.node) emit('update-param', 'credential_id', cred.id)
}

function prettyType(t: string) {
  return t.replace('_', ' ')
}

// ---- workflow picker (Execute Workflow node) ------------------------------
const workflowOptions = ref<{ id: string; name: string }[]>([])
watch(
  () => props.node?.id,
  async () => {
    if (!fields.value.some((f) => f.widget === 'workflow')) return
    try {
      const { api } = useApi()
      const flows = await api.get('/workflows')
      workflowOptions.value = (flows || []).map((f: any) => ({ id: f.id, name: f.name }))
    } catch {
      workflowOptions.value = []
    }
  },
  { immediate: true },
)

// ---- node settings (retry / continue-on-fail) -----------------------------
const showSettings = ref(false)
const settings = computed<NodeSettings>(
  () => props.node?.settings || { retry_on_fail: false, max_retries: 2, retry_wait_ms: 500, continue_on_fail: false },
)

// ---- v17 pinned output + test step -----------------------------------------
const showPinTest = ref(false)
const isPinned = computed(() => props.node?.pinned_data !== undefined && props.node?.pinned_data !== null)
const pinRaw = ref('')
const pinError = ref('')
const testRaw = ref('{}')
const testError = ref('')
const testing = ref(false)
const loadingLast = ref(false)
const lastOutputNote = ref('')
const testResult = ref<NodeTestResult | null>(null)

const PIN_HINT =
  'Manual runs and the test step return this data without executing the node. Webhook, schedule and sub-workflow production runs always execute for real.'

watch(
  () => props.node?.id,
  () => {
    pinRaw.value = ''
    pinError.value = ''
    testRaw.value = '{}'
    testError.value = ''
    testResult.value = null
    lastOutputNote.value = ''
  },
)

const pinText = computed(() => {
  if (pinRaw.value !== '') return pinRaw.value
  const v = props.node?.pinned_data
  return v === undefined || v === null ? '' : JSON.stringify(v, null, 2)
})

function togglePin() {
  if (!props.node) return
  pinError.value = ''
  lastOutputNote.value = ''
  if (isPinned.value) {
    pinRaw.value = ''
    emit('update-pinned', null)
  } else {
    const initial = [{ example: 'replace me' }]
    pinRaw.value = JSON.stringify(initial, null, 2)
    emit('update-pinned', initial)
  }
}

function onPinInput(raw: string) {
  pinRaw.value = raw
  lastOutputNote.value = ''
  if (!raw.trim()) {
    pinError.value = 'Pinned data is required — an empty list [] is fine'
    return
  }
  try {
    const parsed = JSON.parse(raw)
    pinError.value = ''
    emit('update-pinned', parsed)
  } catch {
    pinError.value = 'Invalid JSON'
  }
}

async function useLastOutput() {
  if (!props.node || !props.loadLastOutput) return
  loadingLast.value = true
  lastOutputNote.value = ''
  try {
    const out = await props.loadLastOutput(props.node.id)
    if (out === null || out === undefined) {
      lastOutputNote.value = 'No output found for this node in the latest execution.'
      return
    }
    pinRaw.value = JSON.stringify(out, null, 2)
    pinError.value = ''
    emit('update-pinned', out)
  } catch {
    lastOutputNote.value = 'Could not load the last output.'
  } finally {
    loadingLast.value = false
  }
}

async function runTest() {
  if (!props.node || !props.runTestStep) return
  testError.value = ''
  testResult.value = null
  let items: any = null
  const raw = testRaw.value.trim()
  if (raw) {
    try {
      items = JSON.parse(raw)
    } catch {
      testError.value = 'Test input is not valid JSON'
      return
    }
  }
  testing.value = true
  try {
    testResult.value = await props.runTestStep(props.node.id, items)
  } catch (e: any) {
    testResult.value = {
      ok: false,
      status: 'error',
      error: e?.data?.detail || e?.message || 'Test request failed',
      duration_ms: 0,
    }
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <aside class="flex h-full w-80 shrink-0 flex-col border-l border-zinc-800 bg-zinc-950/80">
    <div v-if="!node" class="flex flex-1 flex-col items-center justify-center p-6 text-center">
      <div class="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-zinc-900">
        <ChevronDown class="h-5 w-5 text-zinc-600" />
      </div>
      <p class="text-sm font-medium text-zinc-400">No node selected</p>
      <p class="mt-1 text-xs leading-relaxed text-zinc-600">
        Click a node on the canvas to edit its parameters. Forms are generated
        from the backend's Pydantic schemas — no UI code per node type.
      </p>
    </div>

    <template v-else>
      <!-- header -->
      <div class="border-b border-zinc-800/70 p-4">
        <div class="mb-2 flex items-center justify-between">
          <span
            class="rounded-md px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider"
            :style="{ backgroundColor: (definition?.color || '#71717a') + '1f', color: definition?.color }"
          >{{ definition?.type }}</span>
          <button
            class="rounded-lg p-1.5 text-zinc-600 transition hover:bg-rose-500/10 hover:text-rose-400"
            title="Delete node"
            @click="emit('delete')"
          >
            <Trash2 class="h-4 w-4" />
          </button>
        </div>
        <label class="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-zinc-600">Node name</label>
        <input
          :value="node.name"
          class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-sm outline-none transition focus:border-orange-500/60"
          @input="emit('rename', ($event.target as HTMLInputElement).value)"
        />

        <!-- v8: disable node (input passes through at run time) -->
        <div class="mt-2.5 flex items-center justify-between rounded-lg border px-2.5 py-1.5"
          :class="node.disabled ? 'border-amber-500/40 bg-amber-500/5' : 'border-zinc-800'">
          <span class="flex items-center gap-1.5 text-[11px] font-medium" :class="node.disabled ? 'text-amber-300' : 'text-zinc-400'">
            <Ban class="h-3.5 w-3.5" /> Disabled
          </span>
          <button
            class="flex h-5 w-9 items-center rounded-full transition"
            :class="node.disabled ? 'bg-amber-500' : 'bg-zinc-700'"
            :title="node.disabled ? 'Enable node' : 'Disable node — input passes through'"
            @click="emit('toggle-disabled', !node.disabled)"
          >
            <span class="mx-0.5 h-4 w-4 rounded-full bg-white shadow transition" :class="node.disabled ? 'translate-x-4' : ''" />
          </button>
        </div>
        <p v-if="node.disabled" class="mt-1 text-[10px] leading-snug text-amber-500/70">
          Skipped at run time — its input passes through untouched.
        </p>

        <p class="mt-1.5 text-[10px] leading-relaxed text-zinc-600">{{ definition?.description }}</p>
      </div>

      <!-- dynamic schema form -->
      <div class="flex-1 overflow-y-auto p-4">
        <div v-for="field in fields" :key="field.key" class="mb-4">
          <label class="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            {{ field.key.replaceAll('_', ' ') }}
            <span v-if="requiredFields.includes(field.key)" class="text-orange-500">*</span>
          </label>

          <!-- select -->
          <select
            v-if="field.widget === 'select'"
            :value="node.parameters[field.key] ?? field.prop.default ?? ''"
            class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs outline-none transition focus:border-orange-500/60"
            @change="emit('update-param', field.key, ($event.target as HTMLSelectElement).value)"
          >
            <option v-for="opt in field.options" :key="opt" :value="opt">{{ opt }}</option>
          </select>

          <!-- workflow picker (Execute Workflow) -->
          <template v-else-if="field.widget === 'workflow'">
            <select
              :value="node.parameters[field.key] ?? ''"
              class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs outline-none transition focus:border-orange-500/60"
              @change="emit('update-param', field.key, ($event.target as HTMLSelectElement).value || '')"
            >
              <option value="">— pick a workflow —</option>
              <option v-for="wf in workflowOptions.filter(w => w.id !== workflowId)" :key="wf.id" :value="wf.id">
                {{ wf.name }}
              </option>
            </select>
            <p v-if="field.prop.description" class="mt-1 text-[10px] leading-snug text-zinc-600">
              {{ field.prop.description }}
            </p>
          </template>

          <!-- credential -->
          <template v-else-if="field.widget === 'credential'">
            <select
              :value="node.parameters[field.key] ?? ''"
              class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs outline-none transition focus:border-orange-500/60"
              @change="emit('update-param', field.key, ($event.target as HTMLSelectElement).value || null)"
            >
              <option value="">— none —</option>
              <option v-for="cred in credentials" :key="cred.id" :value="cred.id">
                {{ cred.name }} ({{ prettyType(cred.type) }} {{ cred.masked_hint }})
              </option>
            </select>
            <div class="mt-1.5 flex items-center gap-3">
              <button
                class="inline-flex items-center gap-1 text-[10px] text-orange-400 transition hover:text-orange-300"
                @click="showCredForm = !showCredForm"
              >
                <Plus class="h-3 w-3" /> {{ showCredForm ? 'Hide credential form' : 'New credential' }}
              </button>
              <NuxtLink
                to="/credentials"
                class="inline-flex items-center gap-1 text-[10px] text-zinc-500 transition hover:text-zinc-300"
                target="_blank"
              >
                <KeyRound class="h-3 w-3" /> Manage credentials
              </NuxtLink>
            </div>

            <div v-if="showCredForm" class="mt-2 rounded-xl border border-zinc-800 bg-zinc-900/70 p-3">
              <p class="mb-2 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                <KeyRound class="h-3 w-3" /> New credential (encrypted at rest)
              </p>
              <select
                v-model="credType"
                class="mb-2 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs outline-none"
              >
                <option value="openai_compatible">OpenAI-compatible (LLM)</option>
                <option value="header_auth">Header auth (HTTP)</option>
                <option value="basic_auth">Basic auth (HTTP)</option>
                <option value="generic">Generic secret</option>
                <option value="smtp">SMTP (Email)</option>
              </select>
              <input
                v-model="credName"
                placeholder="Credential name"
                class="mb-2 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs outline-none focus:border-orange-500/60"
              />
              <input
                v-for="f in credTypeFields[credType]"
                :key="f.key"
                v-model="credData[f.key]"
                :type="f.secret ? 'password' : 'text'"
                :placeholder="`${f.label} — ${f.placeholder}`"
                class="mb-2 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs outline-none focus:border-orange-500/60"
              />
              <button
                class="w-full rounded-lg bg-orange-500 py-1.5 text-xs font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
                :disabled="!credName.trim()"
                @click="submitCredential"
              >
                Save credential
              </button>
            </div>
          </template>

          <!-- JSON object/array -->
          <template v-else-if="field.widget === 'json'">
            <textarea
              :value="jsonTextOf(field.key, node.parameters[field.key])"
              :rows="field.prop.rows || 6"
              spellcheck="false"
              class="w-full rounded-lg border bg-zinc-900 px-2.5 py-2 font-mono text-[11px] outline-none transition focus:border-orange-500/60"
              :class="jsonError[field.key] ? 'border-rose-500/60' : 'border-zinc-800'"
              @input="onJsonInput(field.key, ($event.target as HTMLTextAreaElement).value)"
            />
            <p v-if="jsonError[field.key]" class="mt-1 flex items-center gap-1 text-[10px] text-rose-400">
              <AlertTriangle class="h-3 w-3" /> {{ jsonError[field.key] }}
            </p>
            <p v-else-if="field.prop.description" class="mt-1 text-[10px] leading-snug text-zinc-600">
              {{ field.prop.description }}
            </p>
          </template>

          <!-- textarea / python code -->
          <template v-else-if="field.widget === 'textarea'">
            <textarea
              :value="node.parameters[field.key] ?? field.prop.default ?? ''"
              :rows="field.prop.rows || 4"
              spellcheck="false"
              class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-2 font-mono text-[11px] outline-none transition focus:border-orange-500/60"
              @input="emit('update-param', field.key, ($event.target as HTMLTextAreaElement).value)"
            />
            <p v-if="field.prop.description" class="mt-1 text-[10px] leading-snug text-zinc-600">
              {{ field.prop.description }}
            </p>
          </template>

          <!-- boolean -->
          <template v-else-if="field.widget === 'boolean'">
            <button
              class="flex h-5 w-9 items-center rounded-full transition"
              :class="(node.parameters[field.key] ?? field.prop.default) ? 'bg-orange-500' : 'bg-zinc-700'"
              @click="emit('update-param', field.key, !(node.parameters[field.key] ?? field.prop.default))"
            >
              <span
                class="mx-0.5 h-4 w-4 rounded-full bg-white shadow transition"
                :class="(node.parameters[field.key] ?? field.prop.default) ? 'translate-x-4' : ''"
              />
            </button>
          </template>

          <!-- number -->
          <input
            v-else-if="field.widget === 'number'"
            type="number"
            :value="node.parameters[field.key] ?? field.prop.default ?? 0"
            class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs outline-none transition focus:border-orange-500/60"
            @input="emit('update-param', field.key, Number(($event.target as HTMLInputElement).value))"
          />

          <!-- text -->
          <template v-else>
            <input
              :value="node.parameters[field.key] ?? ''"
              :placeholder="field.prop.placeholder || (field.prop.default != null ? String(field.prop.default) : '')"
              class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 font-mono text-xs outline-none transition focus:border-orange-500/60"
              @input="emit('update-param', field.key, ($event.target as HTMLInputElement).value)"
            />
            <p v-if="field.prop.description" class="mt-1 text-[10px] leading-snug text-zinc-600">
              {{ field.prop.description }}
            </p>
          </template>
        </div>

        <p v-if="fields.length === 0" class="text-xs text-zinc-600">This node has no parameters.</p>

        <!-- v17: pinned output + test step -->
        <div class="mt-2 border-t border-zinc-800/70 pt-3">
          <button
            class="flex w-full items-center justify-between rounded-lg px-1 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition"
            :class="isPinned ? 'text-amber-300' : 'text-zinc-500 hover:text-zinc-300'"
            @click="showPinTest = !showPinTest"
          >
            <span class="flex items-center gap-1.5">
              <FlaskConical class="h-3.5 w-3.5" /> Pin output &amp; test step
              <Pin v-if="isPinned" class="h-3 w-3 text-amber-400" />
            </span>
            <ChevronDown class="h-3.5 w-3.5 transition" :class="showPinTest ? 'rotate-180' : ''" />
          </button>
          <div v-if="showPinTest" class="mt-2 space-y-2.5 rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
            <!-- pin toggle -->
            <div class="flex items-center justify-between">
              <span class="flex items-center gap-1.5 text-[11px] font-medium" :class="isPinned ? 'text-amber-300' : 'text-zinc-400'">
                <Pin class="h-3.5 w-3.5" :class="isPinned ? 'text-amber-400' : 'text-zinc-500'" /> Pinned output
              </span>
              <button
                class="flex h-5 w-9 items-center rounded-full transition"
                :class="isPinned ? 'bg-amber-500' : 'bg-zinc-700'"
                :title="isPinned ? 'Unpin — node executes normally' : 'Pin output data (mock)'"
                @click="togglePin"
              >
                <span class="mx-0.5 h-4 w-4 rounded-full bg-white shadow transition" :class="isPinned ? 'translate-x-4' : ''" />
              </button>
            </div>
            <p class="text-[10px] leading-snug text-zinc-600">{{ PIN_HINT }}</p>
            <template v-if="isPinned">
              <textarea
                :value="pinText"
                :rows="5"
                spellcheck="false"
                class="w-full rounded-lg border bg-zinc-950 px-2.5 py-2 font-mono text-[11px] outline-none transition focus:border-amber-500/60"
                :class="pinError ? 'border-rose-500/60' : 'border-zinc-800'"
                @input="onPinInput(($event.target as HTMLTextAreaElement).value)"
              />
              <p v-if="pinError" class="flex items-center gap-1 text-[10px] text-rose-400">
                <AlertTriangle class="h-3 w-3" /> {{ pinError }}
              </p>
              <button
                class="inline-flex items-center gap-1 text-[10px] text-zinc-400 transition hover:text-amber-300 disabled:opacity-40"
                :disabled="loadingLast"
                @click="useLastOutput"
              >
                <Loader2 v-if="loadingLast" class="h-3 w-3 animate-spin" />
                <Download v-else class="h-3 w-3" /> Use last run output
              </button>
              <p v-if="lastOutputNote" class="text-[10px] text-zinc-500">{{ lastOutputNote }}</p>
            </template>

            <!-- test step -->
            <div class="border-t border-zinc-800/80 pt-2.5">
              <label class="mb-1 block text-[9px] font-semibold uppercase tracking-wider text-zinc-600">Test input (JSON) — reaches the node as its input</label>
              <textarea
                v-model="testRaw"
                :rows="3"
                spellcheck="false"
                class="w-full rounded-lg border bg-zinc-950 px-2.5 py-2 font-mono text-[11px] outline-none transition"
                :class="testError ? 'border-rose-500/60' : 'border-zinc-800 focus:border-orange-500/60'"
              />
              <p v-if="testError" class="mt-1 flex items-center gap-1 text-[10px] text-rose-400">
                <AlertTriangle class="h-3 w-3" /> {{ testError }}
              </p>
              <button
                class="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-orange-500/50 bg-orange-500/10 py-1.5 text-xs font-semibold text-orange-300 transition hover:bg-orange-500/20 disabled:opacity-40"
                :disabled="testing"
                @click="runTest"
              >
                <Loader2 v-if="testing" class="h-3.5 w-3.5 animate-spin" />
                <FlaskConical v-else class="h-3.5 w-3.5" />
                {{ testing ? 'Running…' : 'Test step' }}
              </button>
              <div
                v-if="testResult"
                class="mt-2 overflow-hidden rounded-lg border"
                :class="testResult.ok ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-rose-500/40 bg-rose-500/5'"
              >
                <div class="flex items-center gap-2 border-b px-2.5 py-1.5 text-[10px] font-semibold" :class="testResult.ok ? 'border-emerald-500/20 text-emerald-300' : 'border-rose-500/20 text-rose-300'">
                  <span>{{ testResult.ok ? 'success' : 'error' }}</span>
                  <span class="font-normal text-zinc-500">{{ testResult.duration_ms ?? 0 }}ms</span>
                  <span v-if="testResult.pinned_used" class="ml-auto flex items-center gap-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-amber-300">
                    <Pin class="h-2.5 w-2.5" /> pinned
                  </span>
                </div>
                <pre class="max-h-44 overflow-auto whitespace-pre-wrap break-all px-2.5 py-2 font-mono text-[10px] leading-relaxed text-zinc-300">{{ testResult.ok ? JSON.stringify(testResult.output ?? null, null, 2) : testResult.error }}</pre>
              </div>
            </div>
          </div>
        </div>

        <!-- node settings: retry / continue-on-fail -->
        <div class="mt-2 border-t border-zinc-800/70 pt-3">
          <button
            class="flex w-full items-center justify-between rounded-lg px-1 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 transition hover:text-zinc-300"
            @click="showSettings = !showSettings"
          >
            <span class="flex items-center gap-1.5"><Settings2 class="h-3.5 w-3.5" /> On-fail settings</span>
            <ChevronDown class="h-3.5 w-3.5 transition" :class="showSettings ? 'rotate-180' : ''" />
          </button>
          <div v-if="showSettings" class="mt-2 space-y-3 rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
            <div class="flex items-center justify-between">
              <span class="text-[11px] text-zinc-400">Retry on fail</span>
              <button
                class="flex h-5 w-9 items-center rounded-full transition"
                :class="settings.retry_on_fail ? 'bg-orange-500' : 'bg-zinc-700'"
                @click="emit('update-settings', { retry_on_fail: !settings.retry_on_fail })"
              >
                <span class="mx-0.5 h-4 w-4 rounded-full bg-white shadow transition" :class="settings.retry_on_fail ? 'translate-x-4' : ''" />
              </button>
            </div>
            <div v-if="settings.retry_on_fail" class="grid grid-cols-2 gap-2">
              <div>
                <label class="mb-1 block text-[9px] font-semibold uppercase tracking-wider text-zinc-600">Max retries</label>
                <input
                  type="number" min="1" max="5"
                  :value="settings.max_retries"
                  class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-xs outline-none focus:border-orange-500/60"
                  @change="emit('update-settings', { max_retries: Number(($event.target as HTMLInputElement).value) || 2 })"
                />
              </div>
              <div>
                <label class="mb-1 block text-[9px] font-semibold uppercase tracking-wider text-zinc-600">Wait (ms)</label>
                <input
                  type="number" min="0" max="10000" step="100"
                  :value="settings.retry_wait_ms"
                  class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-xs outline-none focus:border-orange-500/60"
                  @change="emit('update-settings', { retry_wait_ms: Number(($event.target as HTMLInputElement).value) || 0 })"
                />
              </div>
            </div>
            <div class="flex items-center justify-between">
              <span class="flex items-center gap-1.5 text-[11px] text-zinc-400">
                <RotateCcw class="h-3 w-3" /> Continue on fail
              </span>
              <button
                class="flex h-5 w-9 items-center rounded-full transition"
                :class="settings.continue_on_fail ? 'bg-orange-500' : 'bg-zinc-700'"
                @click="emit('update-settings', { continue_on_fail: !settings.continue_on_fail })"
              >
                <span class="mx-0.5 h-4 w-4 rounded-full bg-white shadow transition" :class="settings.continue_on_fail ? 'translate-x-4' : ''" />
              </button>
            </div>
            <p class="text-[10px] leading-snug text-zinc-600">
              Continue: the failure is emitted as data ({"{ error, failed_node }"}) and the flow keeps going instead of stopping.
            </p>
          </div>
        </div>
      </div>

      <!-- jinja tip -->
      <div class="border-t border-zinc-800/70 p-3">
        <p class="text-[10px] leading-relaxed text-zinc-600">
          <span class="font-mono text-zinc-500">{{ jinjaTip }}</span>
          resolves at run time against the live execution context.
        </p>
      </div>
    </template>
  </aside>
</template>
