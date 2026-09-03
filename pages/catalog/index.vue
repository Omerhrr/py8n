<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  BookOpen, Loader2, Search, Tag, Database, ShieldCheck, ShieldAlert,
  ArrowDownToLine, ArrowUpFromLine, RefreshCw, CircleDot, X, Plus, Trash2, Save, ListChecks,
  BadgeCheck, Landmark, Award,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v50: the data catalog - one derived inventory of every dataset: identity,
// shape, freshness, contract, and the workflows that produce / consume it.
// v53: contracts are EDITABLE right here - the card's Contract button opens
// an editor (columns, mode, lint) without leaving the catalog.

interface CatalogEntry {
  id: string
  name: string
  description: string
  owner: string | null
  tags: string[]
  source: string
  rows: number
  columns: number
  schema_preview: { name: string; dtype: string }[]
  freshness: { last_write_at: string | null; age_minutes: number | null; tier: string }
  versions: { count: number; latest: number }
  contract: { present: boolean; on_violation: string | null; version: number }
  certified: boolean
  certified_at: string | null
  owner_id: string | null
  claimable: boolean
  producers: string[]
  consumers: string[]
}

interface ContractCol {
  name: string
  dtype: string
  nullable: boolean
  allowedText: string  // comma-separated allowed values ('' = unrestricted)
}

const DTYPES = ['text', 'integer', 'number', 'boolean', 'datetime']

const { api } = useApi()
const loading = ref(true)
const entries = ref<CatalogEntry[]>([])
const q = ref('')
const tag = ref('')
const pageError = ref('')
const refreshing = ref(false)

const allTags = computed(() => {
  const set = new Set<string>()
  for (const e of entries.value) for (const t of e.tags) set.add(t)
  return [...set].sort()
})

// v54 governance: client-side filter chips over the derived cards
const govFilter = ref<'all' | 'certified' | 'unclaimed'>('all')
const visibleEntries = computed(() => {
  if (govFilter.value === 'certified') return entries.value.filter(e => e.certified)
  if (govFilter.value === 'unclaimed') return entries.value.filter(e => e.claimable)
  return entries.value
})

async function quickClaim(e: CatalogEntry) {
  pageError.value = ''
  try {
    await api.post(`/datasets/${e.id}/claim`, {})
    await load()
  } catch (err: any) {
    pageError.value = err?.data?.detail || err?.message || 'Claim failed - sign in first?'
  }
}

const tierMeta: Record<string, { dot: string; label: string }> = {
  fresh: { dot: 'bg-emerald-400', label: 'fresh (< 1h)' },
  hours: { dot: 'bg-lime-400', label: 'today' },
  stale: { dot: 'bg-amber-400', label: 'stale (> 1d)' },
  cold: { dot: 'bg-rose-400', label: 'cold (> 1w)' },
  never: { dot: 'bg-zinc-600', label: 'no writes yet' },
}

function tierOf(e: CatalogEntry) {
  return tierMeta[e.freshness.tier] || tierMeta.never
}

function fmtAge(e: CatalogEntry): string {
  const m = e.freshness.age_minutes
  if (m === null || m === undefined) return 'never written'
  if (m < 1) return 'just now'
  if (m < 60) return `${Math.round(m)} min ago`
  const h = m / 60
  if (h < 24) return `${Math.round(h)}h ago`
  return `${Math.round(h / 24)}d ago`
}

async function load() {
  refreshing.value = true
  pageError.value = ''
  try {
    const params = new URLSearchParams()
    if (q.value.trim()) params.set('q', q.value.trim())
    if (tag.value) params.set('tag', tag.value)
    const qs = params.toString()
    const res = await api.get<{ entries: CatalogEntry[]; count: number }>(`/catalog${qs ? `?${qs}` : ''}`)
    entries.value = res.entries
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the catalog'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

onMounted(load)

// ------------------------------------------------------------------ v53: contract editor
const contractFor = ref<CatalogEntry | null>(null)
const cLoading = ref(false)
const cCols = ref<ContractCol[]>([])
const cMode = ref<'warn' | 'error'>('warn')
const cVersion = ref(0)
const cPresent = ref(false)
const cSaving = ref(false)
const cMsg = ref('')
const cErr = ref('')
const cCheck = ref<any>(null)

function _toEditorCols(cols: any[]): ContractCol[] {
  return (cols || []).map(c => ({
    name: c.name || '',
    dtype: DTYPES.includes(c.dtype) ? c.dtype : 'text',
    nullable: c.nullable !== false,
    allowedText: Array.isArray(c.allowed) ? c.allowed.join(', ') : '',
  }))
}

async function openContract(e: CatalogEntry) {
  contractFor.value = e
  cLoading.value = true
  cMsg.value = ''
  cErr.value = ''
  cCheck.value = null
  cVersion.value = e.contract.version || 0
  cPresent.value = e.contract.present
  cMode.value = (e.contract.on_violation as any) || 'warn'
  cCols.value = []
  try {
    const res = await api.get<any>(`/datasets/${e.id}/contract`)
    cPresent.value = !!res.present
    cVersion.value = res.version || 0
    cMode.value = (res.on_violation as any) || 'warn'
    cCols.value = res.present && res.columns.length
      ? _toEditorCols(res.columns)
      : _toEditorCols((e.schema_preview || []).map(c => ({ name: c.name, dtype: c.dtype, nullable: true, allowed: null })))
  } catch (err: any) {
    cErr.value = err?.data?.detail || err?.message || 'Could not load the contract'
  } finally {
    cLoading.value = false
  }
}

function addCol() {
  cCols.value.push({ name: '', dtype: 'text', nullable: true, allowedText: '' })
}

function removeCol(i: number) {
  cCols.value.splice(i, 1)
}

function _payload() {
  return {
    on_violation: cMode.value,
    columns: cCols.value.map(c => {
      const allowed = c.allowedText.trim()
        ? c.allowedText.split(',').map(s => s.trim()).filter(Boolean)
        : null
      return { name: c.name.trim(), dtype: c.dtype, nullable: c.nullable, allowed }
    }).filter(c => c.name),
  }
}

async function saveContract() {
  if (!contractFor.value) return
  cSaving.value = true
  cMsg.value = ''
  cErr.value = ''
  cCheck.value = null
  try {
    const res = await api.put<any>(`/datasets/${contractFor.value.id}/contract`, _payload())
    cVersion.value = res.version
    cPresent.value = true
    cMode.value = res.on_violation
    cCols.value = _toEditorCols(res.columns)
    cMsg.value = `Contract v${res.version} saved (${res.on_violation} mode) - every write is now checked`
    await load()
  } catch (err: any) {
    cErr.value = err?.data?.detail || err?.message || 'Could not save the contract'
  } finally {
    cSaving.value = false
  }
}

async function removeContract() {
  if (!contractFor.value) return
  cSaving.value = true
  cMsg.value = ''
  cErr.value = ''
  cCheck.value = null
  try {
    await api.del(`/datasets/${contractFor.value.id}/contract`)
    cPresent.value = false
    cVersion.value = 0
    cCols.value = []
    cMsg.value = 'Contract removed - writes are no longer gated'
    await load()
  } catch (err: any) {
    cErr.value = err?.data?.detail || err?.message || 'Could not remove the contract'
  } finally {
    cSaving.value = false
  }
}

async function checkCurrent() {
  if (!contractFor.value) return
  cErr.value = ''
  cCheck.value = null
  try {
    cCheck.value = await api.post<any>(`/datasets/${contractFor.value.id}/contract/check`, { rows: [] })
  } catch (err: any) {
    cErr.value = err?.data?.detail || err?.message || 'Check failed'
  }
}

// ------------------------------------------------------------- v54: governance modal
const govorFor = ref<CatalogEntry | null>(null)
const govBusy = ref(false)
const govMsg = ref('')
const govErr = ref('')
const govTransferTo = ref('')
const govDesc = ref('')
const govTags = ref('')

function openGovern(e: CatalogEntry) {
  govorFor.value = e
  govMsg.value = ''
  govErr.value = ''
  govTransferTo.value = ''
  govDesc.value = e.description || ''
  govTags.value = (e.tags || []).join(', ')
}

async function govAct(fn: () => Promise<any>, okMsg: string) {
  if (!govorFor.value) return
  govBusy.value = true
  govMsg.value = ''
  govErr.value = ''
  try {
    await fn()
    govMsg.value = okMsg
    await load()
    const fresh = entries.value.find(x => x.id === govorFor.value?.id)
    if (fresh) govorFor.value = fresh
  } catch (err: any) {
    govErr.value = err?.data?.detail || err?.message || 'Action failed'
  } finally {
    govBusy.value = false
  }
}

const claimHere = () => govAct(() => api.post(`/datasets/${govorFor.value!.id}/claim`, {}), 'Ownership claimed')
const releaseHere = () => govAct(() => api.post(`/datasets/${govorFor.value!.id}/claim`, { owner_id: null }), 'Released - the dataset is unclaimed again')
const transferHere = () => govAct(
  () => api.post(`/datasets/${govorFor.value!.id}/claim`, { owner_id: govTransferTo.value.trim() }),
  'Ownership transferred',
)
const certifyHere = () => govAct(() => api.post(`/datasets/${govorFor.value!.id}/certify`), 'Certified - the catalog shows the steward badge')
const uncertifyHere = () => govAct(() => api.del(`/datasets/${govorFor.value!.id}/certify`), 'Certification removed')
const saveMeta = () => govAct(
  () => api.put(`/datasets/${govorFor.value!.id}`, {
    description: govDesc.value,
    tags: govTags.value.split(',').map(s => s.trim()).filter(Boolean),
  }),
  'Description and tags saved',
)
</script>

<template>
  <div class="text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <BookOpen class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">Data catalog</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Every dataset: freshness, contract, producers and consumers</p>
          </div>
          <div class="flex items-center gap-2">
            <div class="relative">
              <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-600" />
              <input
                v-model="q"
                class="w-52 rounded-xl border border-zinc-800 bg-zinc-950 py-2 pl-8 pr-3 text-xs outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                placeholder="Search name or description..."
                @keyup.enter="load"
              />
            </div>
            <select
              v-model="tag"
              class="rounded-xl border border-zinc-800 bg-zinc-950 px-2.5 py-2 text-xs text-zinc-300 outline-none focus:border-orange-500/60"
              @change="load"
            >
              <option value="">All tags</option>
              <option v-for="t in allTags" :key="t" :value="t">{{ t }}</option>
            </select>
            <!-- v54 governance filters -->
            <div class="flex overflow-hidden rounded-xl border border-zinc-800">
              <button
                v-for="f in ([['all', 'All'], ['certified', 'Certified'], ['unclaimed', 'Unclaimed']] as const)"
                :key="f[0]"
                class="px-2.5 py-2 text-[11px] font-medium transition"
                :class="govFilter === f[0] ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-950 text-zinc-400 hover:text-zinc-200'"
                @click="govFilter = f[0]"
              >{{ f[1] }}</button>
            </div>
            <button
              class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs font-medium text-zinc-300 transition hover:border-orange-500/40 hover:text-orange-300 disabled:opacity-50"
              :disabled="refreshing"
              title="Rebuild the catalog view"
              @click="load"
            >
              <Loader2 v-if="refreshing" class="h-3.5 w-3.5 animate-spin" />
              <RefreshCw v-else class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <div class="mb-5 flex items-start gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
        <CircleDot class="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
        <p class="text-xs leading-relaxed text-zinc-400">
          The catalog is <span class="text-zinc-200">derived, never stored</span> - freshness comes from the version
          timeline, the contract badge from the dataset's data contract, and producers / consumers from lineage and a
          scan of active workflow graphs. It cannot drift from what actually happened. The
          <span class="text-zinc-200">Contract button edits the schema promise inline</span>; open a dataset for its
          full health report.
        </p>
      </div>

      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        {{ pageError }}
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <div v-else-if="!entries.length" class="grid place-items-center rounded-2xl border border-dashed border-zinc-800 py-16 text-center">
        <Database class="mb-3 h-8 w-8 text-zinc-700" />
        <p class="text-sm font-medium text-zinc-400">Nothing in the catalog yet</p>
        <p class="mt-1 max-w-md text-xs text-zinc-600">
          Create a dataset (or land one with a workflow) and it shows up here with freshness, contract and lineage.
        </p>
      </div>

      <div v-else class="grid gap-3 lg:grid-cols-2">
        <div
          v-for="e in visibleEntries"
          :key="e.id"
          class="group cursor-pointer rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4 transition hover:border-orange-500/40 hover:bg-zinc-900"
          @click="navigateTo(`/datasets/${e.id}`)"
        >
          <div class="flex items-start gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-emerald-500/30 bg-emerald-500/10 text-emerald-400">
              <Database class="h-4 w-4" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="truncate text-sm font-semibold group-hover:text-orange-200">{{ e.name }}</span>
                <span v-if="e.certified" class="flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-300" title="Steward-certified dataset">
                  <BadgeCheck class="h-3 w-3" /> certified
                </span>
                <span v-if="e.claimable" class="rounded-full border border-dashed border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-500">unclaimed</span>
                <span class="flex items-center gap-1 rounded-full bg-zinc-800/80 px-2 py-0.5 text-[10px] text-zinc-400">
                  <span class="h-1.5 w-1.5 rounded-full" :class="tierOf(e).dot" />
                  {{ tierOf(e).label }}
                </span>
                <span
                  v-if="e.contract.present"
                  class="flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                  :class="e.contract.on_violation === 'error' ? 'bg-rose-500/15 text-rose-400' : 'bg-amber-500/15 text-amber-400'"
                  :title="`Data contract v${e.contract.version} (${e.contract.on_violation} mode)`"
                >
                  <component :is="e.contract.on_violation === 'error' ? ShieldAlert : ShieldCheck" class="h-3 w-3" />
                  contract · {{ e.contract.on_violation }}
                </span>
                <div class="ml-auto flex shrink-0 items-center gap-1">
                  <button
                    v-if="e.claimable"
                    class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] font-medium text-zinc-400 transition hover:border-emerald-500/40 hover:text-emerald-300"
                    title="Claim ownership of this dataset"
                    @click.stop="quickClaim(e)"
                  >
                    <Landmark class="h-3 w-3" /> claim
                  </button>
                  <button
                    class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] font-medium text-zinc-400 transition hover:border-amber-500/40 hover:text-amber-300"
                    title="Edit this dataset's data contract"
                    @click.stop="openContract(e)"
                  >
                    <ShieldCheck class="h-3 w-3" /> Contract
                  </button>
                  <button
                    class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] font-medium text-zinc-400 transition hover:border-emerald-500/40 hover:text-emerald-300"
                    title="Ownership, certification, description and tags"
                    @click.stop="openGovern(e)"
                  >
                    <Award class="h-3 w-3" /> Govern
                  </button>
                </div>
              </div>
              <p v-if="e.description" class="mt-1 line-clamp-2 text-[11px] text-zinc-500">{{ e.description }}</p>

              <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
                <span>{{ e.rows.toLocaleString() }} rows × {{ e.columns }} cols</span>
                <span>·</span>
                <span>v{{ e.versions.latest }} ({{ e.versions.count }} versions)</span>
                <span>·</span>
                <span>written {{ fmtAge(e) }}</span>
                <span v-if="e.owner">· by {{ e.owner }}</span>
              </div>

              <div v-if="e.schema_preview.length" class="mt-2 flex flex-wrap gap-1">
                <span
                  v-for="c in e.schema_preview"
                  :key="c.name"
                  class="rounded-md bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400"
                  :title="c.dtype"
                >
                  {{ c.name }}<span class="text-zinc-600">:{{ c.dtype[0] }}</span>
                </span>
                <span v-if="e.columns > e.schema_preview.length" class="px-1 text-[10px] text-zinc-600">+{{ e.columns - e.schema_preview.length }}</span>
              </div>

              <div class="mt-2 flex flex-wrap items-center gap-3 text-[11px]">
                <span class="flex items-center gap-1 text-zinc-500" title="Workflows that write this dataset">
                  <ArrowDownToLine class="h-3 w-3 text-lime-400" />
                  {{ e.producers.length ? e.producers.join(', ') : 'no producers' }}
                </span>
                <span class="flex items-center gap-1 text-zinc-500" title="Active workflows that reference this dataset">
                  <ArrowUpFromLine class="h-3 w-3 text-sky-400" />
                  {{ e.consumers.length ? e.consumers.join(', ') : 'no consumers' }}
                </span>
              </div>

              <div v-if="e.tags.length" class="mt-2 flex flex-wrap gap-1">
                <span v-for="t in e.tags" :key="t" class="flex items-center gap-0.5 rounded-full bg-orange-500/10 px-1.5 py-0.5 text-[10px] text-orange-300">
                  <Tag class="h-2.5 w-2.5" /> {{ t }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>

    <!-- v53: contract editor modal -->
    <Teleport to="body">
      <div
        v-if="contractFor"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="contractFor = null"
      >
        <div class="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="flex items-center justify-between border-b border-zinc-800/80 px-5 py-3.5">
            <div>
              <h2 class="flex items-center gap-2 text-sm font-bold">
                <ShieldCheck class="h-4 w-4 text-amber-400" /> Data contract - {{ contractFor.name }}
                <span v-if="cPresent" class="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-400">v{{ cVersion }} · {{ cMode }}</span>
                <span v-else class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">no contract yet</span>
              </h2>
              <p class="mt-0.5 text-[11px] text-zinc-500">The schema this dataset promises - enforced at every write door (error mode hard-stops)</p>
            </div>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="contractFor = null"><X class="h-4 w-4" /></button>
          </div>

          <div class="flex-1 overflow-auto p-4">
            <div v-if="cLoading" class="grid place-items-center py-10 text-zinc-600"><Loader2 class="h-5 w-5 animate-spin" /></div>
            <div v-else>
              <div class="mb-3 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
                <span class="font-semibold text-zinc-400">on violation</span>
                <select
                  v-model="cMode"
                  class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] outline-none focus:border-amber-500/60"
                >
                  <option value="warn">warn - write proceeds with a violations report</option>
                  <option value="error">error - write is BLOCKED</option>
                </select>
                <span class="ml-auto text-[10px]">types are castability-checked ("12" IS an integer)</span>
              </div>

              <div class="space-y-2">
                <div class="grid grid-cols-[1fr_92px_70px_1fr_28px] items-center gap-2 px-1 text-[10px] font-semibold uppercase tracking-wide text-zinc-600">
                  <span>column</span><span>dtype</span><span>nullable</span><span>allowed values (csv)</span><span />
                </div>
                <div
                  v-for="(c, i) in cCols"
                  :key="i"
                  class="grid grid-cols-[1fr_92px_70px_1fr_28px] items-center gap-2"
                >
                  <input
                    v-model="c.name"
                    class="rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-amber-500/60"
                    placeholder="column_name"
                  />
                  <select
                    v-model="c.dtype"
                    class="rounded-lg border border-zinc-800 bg-zinc-950 px-1.5 py-1.5 text-xs outline-none focus:border-amber-500/60"
                  >
                    <option v-for="d in DTYPES" :key="d" :value="d">{{ d }}</option>
                  </select>
                  <label class="flex cursor-pointer items-center justify-center gap-1.5 text-[11px] text-zinc-400">
                    <input v-model="c.nullable" type="checkbox" class="h-3.5 w-3.5 accent-amber-500" />
                  </label>
                  <input
                    v-model="c.allowedText"
                    class="rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 font-mono text-xs outline-none focus:border-amber-500/60"
                    placeholder="e.g. active, inactive"
                  />
                  <button class="grid h-7 w-7 place-items-center rounded-lg text-zinc-600 transition hover:bg-rose-500/10 hover:text-rose-400" title="Remove column" @click="removeCol(i)">
                    <Trash2 class="h-3.5 w-3.5" />
                  </button>
                </div>
                <button
                  class="flex items-center gap-1.5 rounded-lg border border-dashed border-zinc-800 px-3 py-1.5 text-[11px] text-zinc-500 transition hover:border-amber-500/40 hover:text-amber-300"
                  @click="addCol"
                >
                  <Plus class="h-3 w-3" /> add column
                </button>
              </div>

              <!-- lint result over current data -->
              <div v-if="cCheck" class="mt-4 rounded-xl border p-3" :class="cCheck.ok ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-rose-500/40 bg-rose-500/5'">
                <p class="flex items-center gap-1.5 text-xs font-semibold" :class="cCheck.ok ? 'text-emerald-300' : 'text-rose-300'">
                  <ListChecks class="h-3.5 w-3.5" />
                  {{ cCheck.ok ? 'Current data satisfies the contract' : `Current data violates it (${cCheck.violations.length} rule${cCheck.violations.length === 1 ? '' : 's'})` }}
                </p>
                <div v-if="!cCheck.ok" class="mt-2 space-y-1">
                  <p v-for="(v, i) in cCheck.violations" :key="i" class="font-mono text-[10px] text-rose-300/90">
                    {{ v.column }} · {{ v.rule }} × {{ v.count }}
                    <span v-if="v.samples && v.samples.length" class="text-zinc-600">e.g. {{ v.samples.slice(0, 3).join(' | ') }}</span>
                  </p>
                </div>
              </div>

              <p v-if="cMsg" class="mt-3 rounded-lg bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-300">{{ cMsg }}</p>
              <p v-if="cErr" class="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300">{{ cErr }}</p>
            </div>

            <!-- v54: version history + diff (hidden while loading) -->
            <div v-if="!cLoading" class="mt-4">
              <ContractHistory :key="contractFor.id" :dataset-id="contractFor.id" />
            </div>
          </div>

          <div class="flex flex-wrap items-center justify-end gap-2 border-t border-zinc-800/80 px-5 py-3">
            <button
              v-if="cPresent"
              class="mr-auto flex items-center gap-1.5 rounded-xl border border-rose-500/40 px-3 py-2 text-xs font-medium text-rose-300 transition hover:bg-rose-500/10 disabled:opacity-50"
              :disabled="cSaving"
              @click="removeContract"
            >
              <Trash2 class="h-3.5 w-3.5" /> remove
            </button>
            <button
              class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs font-medium text-zinc-300 transition hover:border-amber-500/40 hover:text-amber-300 disabled:opacity-50"
              :disabled="cSaving"
              title="Lint the CURRENT dataset contents against this contract"
              @click="checkCurrent"
            >
              <ListChecks class="h-3.5 w-3.5" /> check current data
            </button>
            <button
              class="flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-amber-500 to-orange-500 px-4 py-2 text-xs font-bold text-zinc-950 shadow-lg shadow-amber-500/20 transition hover:brightness-110 disabled:opacity-50"
              :disabled="cSaving || !cCols.length"
              @click="saveContract"
            >
              <Loader2 v-if="cSaving" class="h-3.5 w-3.5 animate-spin" />
              <Save v-else class="h-3.5 w-3.5" />
              save contract
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- v54: governance modal - ownership, certification, description, tags -->
    <Teleport to="body">
      <div
        v-if="govorFor"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="govorFor = null"
      >
        <div class="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="flex items-center justify-between border-b border-zinc-800/80 px-5 py-3.5">
            <div>
              <h2 class="flex items-center gap-2 text-sm font-bold">
                <Landmark class="h-4 w-4 text-emerald-400" /> Govern - {{ govorFor.name }}
              </h2>
              <p class="mt-0.5 text-[11px] text-zinc-500">Ownership, steward certification and card metadata</p>
            </div>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="govorFor = null"><X class="h-4 w-4" /></button>
          </div>

          <div class="flex-1 overflow-auto p-4">
            <!-- ownership -->
            <div class="rounded-xl border border-zinc-800/80 bg-zinc-900/50 p-3">
              <p class="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-zinc-400">
                <Landmark class="h-3 w-3" /> ownership
              </p>
              <p class="mt-1.5 text-xs text-zinc-300">
                <template v-if="govorFor.owner_id">
                  owned by <b>{{ govorFor.owner || govorFor.owner_id.slice(0, 8) }}</b>
                  <span class="font-mono text-[10px] text-zinc-600">({{ govorFor.owner_id.slice(0, 8) }})</span>
                </template>
                <template v-else>
                  <span class="text-zinc-500">unclaimed</span> - anyone signed in can claim it
                </template>
              </p>
              <div class="mt-2 flex flex-wrap items-center gap-2">
                <button
                  v-if="govorFor.claimable"
                  class="rounded-lg border border-emerald-500/40 px-2.5 py-1.5 text-[11px] font-medium text-emerald-300 transition hover:bg-emerald-500/10 disabled:opacity-50"
                  :disabled="govBusy"
                  @click="claimHere"
                >claim for me</button>
                <template v-else>
                  <input
                    v-model="govTransferTo"
                    class="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 font-mono text-[11px] outline-none focus:border-emerald-500/60"
                    placeholder="transfer to user id..."
                  />
                  <button
                    class="rounded-lg border border-zinc-800 px-2.5 py-1.5 text-[11px] font-medium text-zinc-300 transition hover:border-emerald-500/40 hover:text-emerald-300 disabled:opacity-50"
                    :disabled="govBusy || !govTransferTo.trim()"
                    @click="transferHere"
                  >transfer</button>
                  <button
                    class="rounded-lg border border-zinc-800 px-2.5 py-1.5 text-[11px] font-medium text-zinc-400 transition hover:border-rose-500/40 hover:text-rose-300 disabled:opacity-50"
                    :disabled="govBusy"
                    @click="releaseHere"
                  >release</button>
                </template>
              </div>
            </div>

            <!-- certification -->
            <div class="mt-3 rounded-xl border border-zinc-800/80 bg-zinc-900/50 p-3">
              <p class="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-zinc-400">
                <BadgeCheck class="h-3 w-3" :class="govorFor.certified ? 'text-emerald-400' : ''" /> steward certification
              </p>
              <p class="mt-1.5 text-xs text-zinc-400">
                <template v-if="govorFor.certified">
                  <span class="text-emerald-300">Certified</span>
                  <span v-if="govorFor.certified_at" class="text-zinc-600"> - a human vouched for this dataset</span>
                </template>
                <template v-else>Not certified - the catalog shows no steward badge.</template>
              </p>
              <button
                class="mt-2 rounded-lg border px-2.5 py-1.5 text-[11px] font-medium transition disabled:opacity-50"
                :class="govorFor.certified ? 'border-zinc-800 text-zinc-400 hover:border-rose-500/40 hover:text-rose-300' : 'border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10'"
                :disabled="govBusy"
                @click="govorFor.certified ? uncertifyHere() : certifyHere()"
              >{{ govorFor.certified ? 'remove certification' : 'certify this dataset' }}</button>
            </div>

            <!-- metadata -->
            <div class="mt-3 rounded-xl border border-zinc-800/80 bg-zinc-900/50 p-3">
              <p class="text-[11px] font-bold uppercase tracking-wide text-zinc-400">description & tags</p>
              <textarea
                v-model="govDesc"
                rows="2"
                maxlength="500"
                class="mt-2 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-2 text-xs outline-none focus:border-emerald-500/60"
                placeholder="What is this dataset? Where does it come from?"
              ></textarea>
              <input
                v-model="govTags"
                class="mt-2 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-2 text-xs outline-none focus:border-emerald-500/60"
                placeholder="tags, comma separated"
              />
              <button
                class="mt-2 rounded-lg border border-zinc-800 px-2.5 py-1.5 text-[11px] font-medium text-zinc-300 transition hover:border-emerald-500/40 hover:text-emerald-300 disabled:opacity-50"
                :disabled="govBusy"
                @click="saveMeta"
              >save description & tags</button>
            </div>

            <p v-if="govMsg" class="mt-3 rounded-lg bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-300">{{ govMsg }}</p>
            <p v-if="govErr" class="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300">{{ govErr }}</p>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
