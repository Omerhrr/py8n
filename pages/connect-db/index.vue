<script setup lang="ts">
// v148: CONNECT YOUR DATABASE - the guided wizard over the db_source node's
// own connection path (engine/nodes/connectors.py::_build_db_url). Four
// steps, each previewable before anything commits:
//   1. Connect  - backend + sqlite path/full URL, or a vault credential
//   2. Tables   - SQLAlchemy inspection lists tables/views
//   3. Preview  - a small page of real rows before importing anything
//   4. Import   - lands the full result as a real Dataset, optionally with
//                 an App on top in the same step (apps.compose_app() - the
//                 same primitive the Apps builder / System Builder / AI
//                 Composer all create).
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, PlugZap, Database, Table2, CheckCircle2, AlertTriangle,
  ArrowRight, ArrowLeft, ExternalLink, KeyRound, LayoutGrid,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

type Step = 'connect' | 'tables' | 'preview' | 'done'
const step = ref<Step>('connect')

const backend = ref<'sqlite' | 'postgres' | 'mysql'>('sqlite')
const connection = ref('')
const credentialId = ref('')
const credentials = ref<any[]>([])
const dbCredentials = computed(() => credentials.value.filter((c: any) => c.type === 'database'))

const testing = ref(false)
const testError = ref('')
const testOk = ref(false)

const loadingTables = ref(false)
const tables = ref<string[]>([])
const tablesError = ref('')

const selectedTable = ref('')
const customSql = ref('')
const useSql = ref(false)

const loadingPreview = ref(false)
const previewCols = ref<string[]>([])
const previewRows = ref<any[]>([])
const previewError = ref('')

const datasetName = ref('')
const createApp = ref(true)
const importing = ref(false)
const importError = ref('')
const importResult = ref<any>(null)

onMounted(async () => {
  try {
    const creds: any = await api.get('/credentials')
    credentials.value = creds.credentials || creds || []
  } catch {
    credentials.value = []
  }
})

function connBody(extra: Record<string, any> = {}) {
  return {
    backend: backend.value,
    connection: connection.value.trim(),
    credential_id: credentialId.value || null,
    ...extra,
  }
}

async function testConnection() {
  testing.value = true
  testError.value = ''
  testOk.value = false
  try {
    await api.post('/db-connect/test', connBody())
    testOk.value = true
    await loadTables()
  } catch (e: any) {
    testError.value = e?.data?.detail || e?.message || 'connection failed'
  } finally {
    testing.value = false
  }
}

async function loadTables() {
  loadingTables.value = true
  tablesError.value = ''
  try {
    const r: any = await api.post('/db-connect/tables', connBody())
    tables.value = r.tables || []
    step.value = 'tables'
  } catch (e: any) {
    tablesError.value = e?.data?.detail || e?.message || 'could not list tables'
  } finally {
    loadingTables.value = false
  }
}

function pickTable(t: string) {
  selectedTable.value = t
  useSql.value = false
  datasetName.value = t
  loadPreview()
}

async function loadPreview() {
  loadingPreview.value = true
  previewError.value = ''
  previewCols.value = []
  previewRows.value = []
  try {
    const r: any = await api.post('/db-connect/preview', connBody({
      table: useSql.value ? '' : selectedTable.value,
      sql: useSql.value ? customSql.value.trim() : '',
      limit: 20,
    }))
    previewCols.value = r.columns || []
    previewRows.value = r.rows || []
    step.value = 'preview'
  } catch (e: any) {
    previewError.value = e?.data?.detail || e?.message || 'preview failed'
  } finally {
    loadingPreview.value = false
  }
}

async function runImport() {
  importing.value = true
  importError.value = ''
  try {
    importResult.value = await api.post('/db-connect/import', connBody({
      table: useSql.value ? '' : selectedTable.value,
      sql: useSql.value ? customSql.value.trim() : '',
      dataset_name: (datasetName.value || selectedTable.value || 'Imported dataset').trim(),
      create_app: createApp.value,
    }))
    step.value = 'done'
  } catch (e: any) {
    importError.value = e?.data?.detail || e?.message || 'import failed'
  } finally {
    importing.value = false
  }
}

function startOver() {
  step.value = 'connect'
  testOk.value = false
  tables.value = []
  selectedTable.value = ''
  useSql.value = false
  customSql.value = ''
  previewCols.value = []
  previewRows.value = []
  importResult.value = null
  importError.value = ''
}
</script>

<template>
  <div class="max-w-4xl mx-auto px-4 py-8">
    <div class="flex items-start gap-3 mb-6">
      <div class="p-2 rounded-lg bg-sky-500/10 border border-sky-500/20">
        <PlugZap class="w-6 h-6 text-sky-400" />
      </div>
      <div>
        <h1 class="text-xl font-semibold text-zinc-100">Connect your database</h1>
        <p class="text-sm text-zinc-500 mt-0.5">
          Sqlite, Postgres or MySQL - test the connection, browse tables, preview real rows,
          then import as a Dataset (optionally with an App on top). Read-only: SELECT/WITH
          statements only, the same guard the db_source node uses.
        </p>
      </div>
    </div>

    <!-- step indicator -->
    <div class="flex items-center gap-2 mb-6 text-xs">
      <span :class="['px-2.5 py-1 rounded-full border', step === 'connect' ? 'border-sky-500/40 bg-sky-500/10 text-sky-300' : 'border-zinc-800 text-zinc-500']">1. Connect</span>
      <ArrowRight class="w-3 h-3 text-zinc-700" />
      <span :class="['px-2.5 py-1 rounded-full border', step === 'tables' ? 'border-sky-500/40 bg-sky-500/10 text-sky-300' : 'border-zinc-800 text-zinc-500']">2. Tables</span>
      <ArrowRight class="w-3 h-3 text-zinc-700" />
      <span :class="['px-2.5 py-1 rounded-full border', step === 'preview' ? 'border-sky-500/40 bg-sky-500/10 text-sky-300' : 'border-zinc-800 text-zinc-500']">3. Preview + import</span>
      <ArrowRight class="w-3 h-3 text-zinc-700" />
      <span :class="['px-2.5 py-1 rounded-full border', step === 'done' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300' : 'border-zinc-800 text-zinc-500']">4. Done</span>
    </div>

    <!-- step 1: connect -->
    <div v-if="step === 'connect'" class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-5 space-y-4">
      <div>
        <label class="text-xs font-medium text-zinc-400 mb-1.5 block">Database</label>
        <div class="flex gap-2">
          <button v-for="b in ['sqlite', 'postgres', 'mysql']" :key="b"
                  @click="backend = b as any"
                  :class="['px-3 py-1.5 rounded-lg text-xs font-medium border transition',
                          backend === b ? 'border-sky-500/50 bg-sky-500/10 text-sky-300' : 'border-zinc-800 text-zinc-400 hover:border-zinc-700']">
            {{ b }}
          </button>
        </div>
      </div>

      <div v-if="backend === 'sqlite'">
        <label class="text-xs font-medium text-zinc-400 mb-1.5 block">Path (optional)</label>
        <input v-model="connection" class="input" placeholder="data/py8n.db (default) or a full sqlite:/// URL" />
      </div>
      <template v-else>
        <div>
          <label class="text-xs font-medium text-zinc-400 mb-1.5 block flex items-center gap-1.5">
            <KeyRound class="w-3.5 h-3.5" /> Credential (host/port/user/password/database)
          </label>
          <select v-model="credentialId" class="input">
            <option value="">None - use a full connection URL instead</option>
            <option v-for="c in dbCredentials" :key="c.id" :value="c.id">{{ c.name }}</option>
          </select>
          <p v-if="!dbCredentials.length" class="text-[11px] text-zinc-600 mt-1">
            No database credentials yet - add one on the
            <NuxtLink to="/credentials" class="text-sky-400 hover:underline">Credentials</NuxtLink> page, or paste a full URL below.
          </p>
        </div>
        <div>
          <label class="text-xs font-medium text-zinc-400 mb-1.5 block">Full connection URL (overrides the credential)</label>
          <input v-model="connection" class="input font-mono text-xs"
                 :placeholder="backend === 'postgres' ? 'postgresql+psycopg2://user:pass@host:5432/db' : 'mysql+pymysql://user:pass@host:3306/db'" />
        </div>
      </template>

      <div v-if="testError" class="flex items-start gap-2 text-xs text-rose-300 bg-rose-500/5 border border-rose-500/30 rounded-lg px-3 py-2">
        <AlertTriangle class="w-3.5 h-3.5 shrink-0 mt-0.5" /> {{ testError }}
      </div>

      <button class="btn btn-primary" :disabled="testing" @click="testConnection">
        <Loader2 v-if="testing" class="w-4 h-4 animate-spin" />
        <PlugZap v-else class="w-4 h-4" />
        Test connection
      </button>
    </div>

    <!-- step 2: tables -->
    <div v-if="step === 'tables'" class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-5 space-y-4">
      <div class="flex items-center gap-2 text-sm text-emerald-300">
        <CheckCircle2 class="w-4 h-4" /> Connected ({{ backend }})
      </div>

      <div v-if="loadingTables" class="flex items-center gap-2 text-sm text-zinc-400">
        <Loader2 class="w-4 h-4 animate-spin" /> Listing tables...
      </div>
      <div v-else-if="tablesError" class="flex items-start gap-2 text-xs text-rose-300 bg-rose-500/5 border border-rose-500/30 rounded-lg px-3 py-2">
        <AlertTriangle class="w-3.5 h-3.5 shrink-0 mt-0.5" /> {{ tablesError }}
      </div>
      <template v-else>
        <div class="grid gap-1.5 sm:grid-cols-2">
          <button v-for="t in tables" :key="t" @click="pickTable(t)"
                  class="flex items-center gap-2 text-left rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm text-zinc-300 hover:border-sky-500/40 transition">
            <Table2 class="w-3.5 h-3.5 text-zinc-500 shrink-0" /> {{ t }}
          </button>
        </div>
        <p v-if="!tables.length" class="text-xs text-zinc-500">No tables found.</p>

        <div class="pt-2 border-t border-zinc-800">
          <label class="flex items-center gap-2 text-xs text-zinc-400 mb-1.5">
            <input type="checkbox" v-model="useSql" /> Use a custom read-only SQL query instead
          </label>
          <div v-if="useSql" class="flex gap-2">
            <input v-model="customSql" class="input font-mono text-xs" placeholder="SELECT * FROM orders WHERE status = 'open'" />
            <button class="btn btn-ghost btn-xs shrink-0" :disabled="!customSql.trim()" @click="datasetName = 'Query result'; loadPreview()">Preview</button>
          </div>
        </div>
      </template>

      <button class="btn btn-ghost btn-xs" @click="step = 'connect'"><ArrowLeft class="w-3.5 h-3.5" /> Back</button>
    </div>

    <!-- step 3: preview + import -->
    <div v-if="step === 'preview'" class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-5 space-y-4">
      <div v-if="loadingPreview" class="flex items-center gap-2 text-sm text-zinc-400">
        <Loader2 class="w-4 h-4 animate-spin" /> Loading preview...
      </div>
      <div v-else-if="previewError" class="flex items-start gap-2 text-xs text-rose-300 bg-rose-500/5 border border-rose-500/30 rounded-lg px-3 py-2">
        <AlertTriangle class="w-3.5 h-3.5 shrink-0 mt-0.5" /> {{ previewError }}
      </div>
      <template v-else>
        <div class="overflow-x-auto rounded-lg border border-zinc-800">
          <table class="w-full text-xs">
            <thead>
              <tr class="bg-zinc-950/60 text-zinc-500">
                <th v-for="c in previewCols" :key="c" class="text-left font-medium px-3 py-2 whitespace-nowrap">{{ c }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in previewRows" :key="i" class="border-t border-zinc-800/60 text-zinc-300">
                <td v-for="c in previewCols" :key="c" class="px-3 py-1.5 whitespace-nowrap max-w-[220px] truncate">{{ row[c] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="text-[11px] text-zinc-600">Showing up to 20 rows - import pulls the full result (capped at 100,000 rows).</p>

        <div class="grid gap-3 sm:grid-cols-2 pt-2 border-t border-zinc-800">
          <div>
            <label class="text-xs font-medium text-zinc-400 mb-1.5 block">Dataset name</label>
            <input v-model="datasetName" class="input" />
          </div>
          <label class="flex items-center gap-2 text-xs text-zinc-400 self-end pb-2.5">
            <input type="checkbox" v-model="createApp" />
            <LayoutGrid class="w-3.5 h-3.5" /> Also build an App (forms, records, Excel export)
          </label>
        </div>

        <div v-if="importError" class="flex items-start gap-2 text-xs text-rose-300 bg-rose-500/5 border border-rose-500/30 rounded-lg px-3 py-2">
          <AlertTriangle class="w-3.5 h-3.5 shrink-0 mt-0.5" /> {{ importError }}
        </div>

        <div class="flex items-center gap-2">
          <button class="btn btn-ghost btn-xs" @click="step = 'tables'"><ArrowLeft class="w-3.5 h-3.5" /> Back</button>
          <button class="btn btn-primary" :disabled="importing || !datasetName.trim()" @click="runImport">
            <Loader2 v-if="importing" class="w-4 h-4 animate-spin" />
            <Database v-else class="w-4 h-4" />
            Import dataset
          </button>
        </div>
      </template>
    </div>

    <!-- step 4: done -->
    <div v-if="step === 'done' && importResult" class="rounded-2xl border border-emerald-500/30 bg-emerald-500/5 p-5">
      <div class="flex items-center gap-2 mb-3">
        <CheckCircle2 class="w-5 h-5 text-emerald-400" />
        <h2 class="text-sm font-semibold text-emerald-300">Imported {{ importResult.rows }} row(s)</h2>
      </div>
      <div class="grid gap-2 sm:grid-cols-2">
        <NuxtLink :to="`/datasets/${importResult.dataset_id}`" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 transition hover:border-lime-500/40">
          <span class="text-xs font-semibold">Dataset</span>
          <span class="flex items-center gap-1 text-[10px] text-zinc-500">{{ importResult.dataset_name }} <ExternalLink class="h-3 w-3" /></span>
        </NuxtLink>
        <NuxtLink v-if="importResult.app_id" :to="`/apps/${importResult.app_id}`" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 transition hover:border-violet-500/40">
          <span class="text-xs font-semibold">App</span>
          <span class="flex items-center gap-1 text-[10px] text-zinc-500">forms · rules · Excel export <ExternalLink class="h-3 w-3" /></span>
        </NuxtLink>
      </div>
      <button class="btn btn-ghost btn-xs mt-4" @click="startOver">Connect another database</button>
    </div>
  </div>
</template>
