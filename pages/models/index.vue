<script setup lang="ts">
// v46: the ML model registry - versioned, activatable, deletable. v47 adds
// the PSI drift check (GET /models/{ref}/drift) rendered inline per row.
import { ref, onMounted } from 'vue'
import { Network, Loader2, Trash2, CheckCircle2, RefreshCw, ChevronDown, Activity, Play } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

interface ModelRow {
  id: string
  name: string
  version: number
  algorithm: string
  task: string
  target: string
  features: string[]
  metrics: Record<string, any>
  dataset_name: string | null
  row_count: number
  active: boolean
  has_reference_stats: boolean  // v47: drift scoring needs training-time reference stats
  created_at: string | null
}

interface DriftReport {
  drift_detected: boolean
  threshold: number
  overall_psi: number | null
  max_feature: string | null
  rows: number
  features: { feature: string; type: string | null; psi: number | null; status: string; missing_in_batch?: boolean }[]
  model: { id: string; name: string; version: number }
  dataset: { id: string; name: string; rows: number }
}

const models = ref<ModelRow[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busyId = ref<string | null>(null)
const expanded = ref<string | null>(null)

function metricSummary(m: ModelRow): string {
  const metrics = m.metrics || {}
  if (m.task === 'classification') {
    const acc = metrics.accuracy != null ? `acc ${(metrics.accuracy * 100).toFixed(1)}%` : null
    const auc = metrics.roc_auc != null ? ` · AUC ${metrics.roc_auc}` : null
    const cv = metrics.cv_mean != null ? ` · cv ${metrics.cv_mean}` : null
    return [acc, auc, cv].filter(Boolean).join('') || 'no metrics'
  }
  const r2 = metrics.r2 != null ? `R² ${metrics.r2}` : null
  const rmse = metrics.rmse != null ? ` · RMSE ${metrics.rmse}` : null
  return [r2, rmse].filter(Boolean).join('') || 'no metrics'
}

async function load() {
  loading.value = true
  error.value = null
  try {
    const rows = await api.get<ModelRow[]>('/models')
    // group by name: active first, then newest version
    const byName = new Map<string, ModelRow[]>()
    for (const r of rows) {
      if (!byName.has(r.name)) byName.set(r.name, [])
      byName.get(r.name)!.push(r)
    }
    const grouped: ModelRow[] = []
    for (const [, versions] of byName) {
      versions.sort((a, b) => (b.active ? 1 : 0) - (a.active ? 1 : 0) || b.version - a.version)
      grouped.push(...versions)
    }
    models.value = grouped
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load models'
  } finally {
    loading.value = false
  }
}

async function activate(m: ModelRow) {
  busyId.value = m.id
  notice.value = null
  try {
    await api.post(`/models/${m.id}/activate`)
    await load()
    notice.value = `${m.name} v${m.version} is now the active version`
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Activate failed'
  } finally {
    busyId.value = null
  }
}

async function remove(m: ModelRow) {
  if (!confirm(`Delete ${m.name} v${m.version} and its artifact?`)) return
  busyId.value = m.id
  notice.value = null
  try {
    await api.del(`/models/${m.id}`)
    await load()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Delete failed'
  } finally {
    busyId.value = null
  }
}

// ------------------------------------------------------------------ v47 drift
// One panel open at a time (like the expanded details). The dataset list is
// fetched once, lazily, on the first Drift panel; 409s surface the server's
// detail message ("no reference stats" / "dataset is empty").
const driftOpenId = ref<string | null>(null)
const driftDatasets = ref<{ id: string; name: string }[]>([])
const driftDatasetsLoaded = ref(false)
const driftSel = ref('')
const driftThreshold = ref(0.25)
const driftReport = ref<DriftReport | null>(null)
const driftLoading = ref(false)
const driftError = ref<string | null>(null)

const DRIFT_STATUS_STYLE: Record<string, string> = {
  stable: 'bg-emerald-500/15 text-emerald-400',
  moderate: 'bg-amber-500/15 text-amber-400',
  drifted: 'bg-red-500/15 text-red-400',
  missing: 'bg-zinc-800 text-zinc-400',
}

function fmtPsi(v: number | null | undefined): string {
  return v === null || v === undefined ? '-' : Number(v).toFixed(4)
}

async function openDrift(m: ModelRow) {
  if (driftOpenId.value === m.id) {
    driftOpenId.value = null
    return
  }
  driftOpenId.value = m.id
  driftReport.value = null
  driftError.value = null
  driftSel.value = ''
  driftThreshold.value = 0.25
  if (!driftDatasetsLoaded.value) {
    try {
      const rows = await api.get<any[]>('/datasets')
      driftDatasets.value = rows.map((d) => ({ id: d.id, name: d.name }))
      driftDatasetsLoaded.value = true
    } catch (e: any) {
      driftError.value = e?.data?.detail || e?.message || 'Failed to load datasets'
    }
  }
}

async function runDrift(m: ModelRow) {
  if (!driftSel.value) return
  driftLoading.value = true
  driftError.value = null
  try {
    const th = Number.isFinite(driftThreshold.value) ? driftThreshold.value : 0.25
    driftReport.value = await api.get<DriftReport>(
      `/models/${m.id}/drift?dataset_id=${encodeURIComponent(driftSel.value)}&threshold=${th}`,
    )
  } catch (e: any) {
    driftReport.value = null
    driftError.value = e?.data?.detail || e?.message || 'Drift check failed'
  } finally {
    driftLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="min-h-screen">
    <header class="sticky top-0 z-10 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3 lg:px-6">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-fuchsia-500/15">
          <Network class="h-4 w-4 text-fuchsia-400" />
        </span>
        <div class="min-w-0 flex-1">
          <h1 class="text-base font-bold leading-tight">Models</h1>
          <p class="text-xs text-zinc-500">Versioned ML registry - trained by the Model Train node, scored by Model Predict</p>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs text-zinc-400 transition hover:border-fuchsia-500/40 hover:text-fuchsia-300"
          @click="load"
        >
          <RefreshCw class="h-3.5 w-3.5" /> Refresh
        </button>
      </div>
    </header>

    <div class="mx-auto max-w-6xl space-y-4 px-4 py-5 lg:px-6">
      <p v-if="notice" class="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-2.5 text-sm text-emerald-300">{{ notice }}</p>
      <p v-if="error" class="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">{{ error }}</p>

      <div v-if="loading" class="flex justify-center py-16 text-zinc-500"><Loader2 class="h-6 w-6 animate-spin" /></div>

      <div v-else-if="!models.length" class="rounded-2xl border border-dashed border-zinc-800 px-6 py-16 text-center">
        <Network class="mx-auto h-8 w-8 text-zinc-700" />
        <p class="mt-3 text-sm font-semibold text-zinc-300">No models yet</p>
        <p class="mx-auto mt-1 max-w-md text-xs leading-relaxed text-zinc-500">
          Add a <b>Model Train</b> node to any workflow - every training run registers a version here.
          Score fresh rows later with <b>Model Predict</b>.
        </p>
      </div>

      <div v-else class="space-y-2">
        <div
          v-for="m in models"
          :key="m.id"
          class="overflow-hidden rounded-2xl border bg-zinc-900/40"
          :class="m.active ? 'border-fuchsia-500/40' : 'border-zinc-800/80'"
        >
          <div class="flex flex-wrap items-center gap-3 px-4 py-3">
            <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-zinc-800/80 text-xs font-bold text-zinc-300">v{{ m.version }}</span>
            <div class="min-w-0 flex-1">
              <p class="flex items-center gap-2 truncate text-sm font-semibold text-zinc-100">
                {{ m.name }}
                <span v-if="m.active" class="flex items-center gap-1 rounded-lg bg-fuchsia-500/15 px-1.5 py-0.5 text-[10px] font-bold text-fuchsia-300">
                  <CheckCircle2 class="h-3 w-3" /> ACTIVE
                </span>
              </p>
              <p class="mt-0.5 text-[11px] text-zinc-500">
                {{ m.algorithm }} · {{ m.task }} · target <span class="font-mono">{{ m.target || '-' }}</span>
                · {{ m.row_count }} rows
                <template v-if="m.dataset_name"> · from <span class="font-mono">{{ m.dataset_name }}</span></template>
              </p>
              <p class="mt-0.5 text-[11px] tabular-nums text-zinc-400">{{ metricSummary(m) }}</p>
            </div>
            <div class="flex items-center gap-1.5">
              <button
                v-if="m.has_reference_stats"
                class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-[11px] font-medium text-zinc-300 transition hover:border-fuchsia-500/40 hover:text-fuchsia-300"
                :class="driftOpenId === m.id && 'border-fuchsia-500/50 text-fuchsia-300'"
                title="PSI drift check against a batch dataset"
                @click="openDrift(m)"
              >
                <Activity class="h-3.5 w-3.5" /> Drift
              </button>
              <span v-else class="max-w-[140px] text-[10px] leading-tight text-zinc-600" title="This version predates reference stats">retrain to capture reference stats</span>
              <button
                class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-500 transition hover:text-zinc-200"
                title="Details"
                @click="expanded = expanded === m.id ? null : m.id"
              >
                <ChevronDown class="h-3.5 w-3.5 transition" :class="expanded === m.id && 'rotate-180'" />
              </button>
              <button
                v-if="!m.active"
                class="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-[11px] font-medium text-zinc-300 transition hover:border-fuchsia-500/40 hover:text-fuchsia-300"
                :disabled="busyId === m.id"
                @click="activate(m)"
              >
                <Loader2 v-if="busyId === m.id" class="h-3 w-3 animate-spin" /> Activate
              </button>
              <button
                class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-500 transition hover:border-amber-500/40 hover:text-amber-400"
                title="Delete version"
                @click="remove(m)"
              >
                <Loader2 v-if="busyId === m.id" class="h-3.5 w-3.5 animate-spin" />
                <Trash2 v-else class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
          <div v-if="expanded === m.id" class="border-t border-zinc-800/80 bg-zinc-950/40 px-4 py-3">
            <p class="text-[11px] font-bold uppercase tracking-wider text-zinc-500">Metrics</p>
            <div class="mt-1.5 flex flex-wrap gap-1.5">
              <span
                v-for="(v, k) in m.metrics"
                :key="k"
                class="rounded bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-300"
              >{{ k }} <b class="text-zinc-100">{{ typeof v === 'number' ? v : JSON.stringify(v).slice(0, 60) }}</b></span>
            </div>
            <p class="mt-3 text-[11px] font-bold uppercase tracking-wider text-zinc-500">Features ({{ m.features.length }})</p>
            <div class="mt-1.5 flex flex-wrap gap-1.5">
              <span v-for="f in m.features" :key="f" class="rounded bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">{{ f }}</span>
            </div>
          </div>

          <!-- v47: PSI drift check panel -->
          <div v-if="driftOpenId === m.id" class="border-t border-zinc-800/80 bg-zinc-950/40 px-4 py-3">
            <p class="text-[11px] font-bold uppercase tracking-wider text-zinc-500">Drift check - PSI vs training reference</p>
            <div class="mt-2 flex flex-wrap items-end gap-2">
              <div class="min-w-[180px] flex-1">
                <label class="block text-[10px] uppercase tracking-wide text-zinc-600">Batch dataset</label>
                <select v-model="driftSel" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-fuchsia-500/60">
                  <option value="" disabled>pick a dataset…</option>
                  <option v-for="d in driftDatasets" :key="d.id" :value="d.id">{{ d.name }}</option>
                </select>
              </div>
              <div>
                <label class="block text-[10px] uppercase tracking-wide text-zinc-600">Threshold</label>
                <input
                  v-model.number="driftThreshold"
                  type="number" min="0" step="0.05"
                  class="mt-1 w-24 rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-fuchsia-500/60"
                />
              </div>
              <button
                class="flex items-center gap-1.5 rounded-lg bg-fuchsia-500 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-fuchsia-400 disabled:opacity-40"
                :disabled="!driftSel || driftLoading"
                @click="runDrift(m)"
              >
                <Loader2 v-if="driftLoading" class="h-3.5 w-3.5 animate-spin" />
                <Play v-else class="h-3.5 w-3.5" /> Run
              </button>
            </div>

            <p v-if="driftError" class="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">{{ driftError }}</p>

            <div v-if="driftReport" class="mt-3">
              <div class="flex flex-wrap items-center gap-2">
                <span
                  class="rounded-lg px-2 py-0.5 text-[10px] font-bold tracking-wide"
                  :class="driftReport.drift_detected ? 'bg-red-500/15 text-red-400' : 'bg-emerald-500/15 text-emerald-400'"
                >{{ driftReport.drift_detected ? 'DRIFTED' : 'STABLE' }}</span>
                <span class="text-[11px] text-zinc-400">overall PSI <b class="tabular-nums text-zinc-200">{{ fmtPsi(driftReport.overall_psi) }}</b> <span class="text-zinc-600">(threshold {{ driftReport.threshold }})</span></span>
                <span v-if="driftReport.max_feature" class="text-[11px] text-zinc-500">max: <span class="font-mono text-zinc-300">{{ driftReport.max_feature }}</span></span>
                <span class="ml-auto text-[10px] text-zinc-600">{{ driftReport.dataset.name }} · {{ driftReport.rows.toLocaleString() }} rows</span>
              </div>
              <div class="mt-2 overflow-hidden rounded-xl border border-zinc-800">
                <table class="w-full text-left text-xs">
                  <thead>
                    <tr class="border-b border-zinc-800/60 bg-zinc-900/60 text-[10px] uppercase tracking-wide text-zinc-500">
                      <th class="px-3 py-2 font-medium">feature</th>
                      <th class="px-3 py-2 font-medium">type</th>
                      <th class="px-3 py-2 text-right font-medium">psi</th>
                      <th class="px-3 py-2 font-medium">status</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="f in driftReport.features" :key="f.feature" class="border-b border-zinc-800/40 last:border-0">
                      <td class="px-3 py-1.5 font-mono text-zinc-300">{{ f.feature }}</td>
                      <td class="px-3 py-1.5 text-zinc-500">{{ f.type || '-' }}</td>
                      <td class="px-3 py-1.5 text-right tabular-nums text-zinc-300">{{ fmtPsi(f.psi) }}</td>
                      <td class="px-3 py-1.5">
                        <span class="rounded px-1.5 py-0.5 text-[10px] font-semibold" :class="DRIFT_STATUS_STYLE[f.status] || 'bg-zinc-800 text-zinc-400'">{{ f.status }}</span>
                      </td>
                    </tr>
                    <tr v-if="!driftReport.features.length">
                      <td colspan="4" class="px-3 py-4 text-center text-zinc-600">No scored features for this model.</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
