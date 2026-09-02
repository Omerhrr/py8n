<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { FileText, UploadCloud, Loader2, ScanText, Table2, FileDigit, ArrowRight, Sparkles } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

interface ExtractTable { page: number; rows: string[][]; n_rows: number; n_cols: number }
interface ExtractResult {
  filename: string
  size_bytes: number
  engine: string
  pages: number
  chars: number
  text: string
  tables: ExtractTable[]
}
interface Engines { ocr: { available: boolean; version: string | null }; formats: string[] }

const loading = ref(true)
const error = ref<string | null>(null)
const engines = ref<Engines | null>(null)

const file = ref<File | null>(null)
const extracting = ref(false)
const result = ref<ExtractResult | null>(null)
const tab = ref<'text' | 'tables'>('text')

const dsName = ref('')
const dsDesc = ref('')
const sending = ref(false)
const sentDataset = ref<{ id: string; name: string; row_count: number } | null>(null)
const dragOver = ref(false)

const formatsLabel = computed(() => (engines.value?.formats || []).map((f) => f.replace('.', '').toUpperCase()).join(' · '))

onMounted(async () => {
  try {
    engines.value = await api.get<Engines>('/documents/engines')
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load engine info'
  } finally {
    loading.value = false
  }
})

function pick(f: File | undefined | null) {
  if (!f) return
  file.value = f
  result.value = null
  sentDataset.value = null
  error.value = null
  if (!dsName.value) dsName.value = f.name.replace(/\.[^.]+$/, '').replace(/[^\w .-]/g, '')
  doExtract()
}

function onDrop(ev: DragEvent) {
  dragOver.value = false
  pick(ev.dataTransfer?.files?.[0])
}

async function doExtract() {
  if (!file.value) return
  extracting.value = true
  error.value = null
  result.value = null
  sentDataset.value = null
  try {
    const form = new FormData()
    form.append('file', file.value)
    result.value = await api.upload<ExtractResult>('/documents/extract', form)
    tab.value = result.value.tables.length ? 'tables' : 'text'
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Extraction failed'
  } finally {
    extracting.value = false
  }
}

async function sendToDataset() {
  if (!file.value || !dsName.value.trim()) return
  sending.value = true
  error.value = null
  try {
    const form = new FormData()
    form.append('file', file.value)
    form.append('name', dsName.value.trim())
    form.append('description', dsDesc.value.trim())
    const res = await api.upload<any>('/documents/to-dataset', form)
    sentDataset.value = { id: res.dataset.id, name: res.dataset.name, row_count: res.dataset.row_count }
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Could not create the dataset'
  } finally {
    sending.value = false
  }
}

const engineBadge = computed(() => {
  const e = result.value?.engine
  if (!e) return null
  const labels: Record<string, string> = {
    pdf: 'PDF · pdfplumber', ocr: 'OCR · tesseract', docx: 'Word · python-docx',
    xlsx: 'Excel · openpyxl', csv: 'CSV', json: 'JSON', text: 'Plain text',
  }
  return labels[e] || e
})

function fmtSize(n: number) {
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`
}
</script>

<template>
  <div class="pb-10 text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3.5 lg:px-6">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-orange-500/15">
          <ScanText class="h-4 w-4 text-orange-400" />
        </span>
        <div class="min-w-0 flex-1">
          <h1 class="truncate text-base font-bold leading-tight">Document AI</h1>
          <p class="text-xs text-zinc-500">PDFs, scans, Word and workbooks → text + tables → datasets. All local, no external calls.</p>
        </div>
        <span v-if="engines" class="hidden items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-semibold sm:flex"
          :class="engines.ocr.available ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400' : 'border-amber-500/30 bg-amber-500/10 text-amber-400'">
          <Sparkles class="h-3 w-3" /> {{ engines.ocr.available ? `OCR ready · tesseract ${engines.ocr.version}` : 'OCR unavailable' }}
        </span>
      </div>
    </header>

    <div class="mx-auto max-w-6xl px-4 lg:px-6">
      <p v-if="error" class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">{{ error }}</p>

      <!-- drop zone -->
      <label
        class="mt-5 flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed px-6 py-10 text-center transition"
        :class="dragOver ? 'border-orange-500/60 bg-orange-500/5' : 'border-zinc-800 bg-zinc-900/40 hover:border-zinc-600'"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="onDrop"
      >
        <input type="file" class="hidden" accept=".pdf,.png,.jpg,.jpeg,.bmp,.tif,.tiff,.webp,.docx,.xlsx,.xls,.csv,.tsv,.json,.txt,.md" @change="pick(($event.target as HTMLInputElement).files?.[0])" />
        <Loader2 v-if="extracting" class="h-7 w-7 animate-spin text-orange-400" />
        <UploadCloud v-else class="h-7 w-7 text-zinc-500" />
        <p class="mt-3 text-sm font-medium text-zinc-200">
          {{ extracting ? `Extracting ${file?.name}…` : file ? file.name : 'Drop a document here or click to browse' }}
        </p>
        <p class="mt-1 text-xs text-zinc-500">{{ formatsLabel || 'PDF · OCR · DOCX · XLSX · CSV · JSON · TXT' }} - up to 25 MB</p>
      </label>

      <!-- result -->
      <div v-if="result" class="mt-5">
        <div class="flex flex-wrap items-center gap-2">
          <span class="rounded-full bg-orange-500/15 px-2.5 py-1 text-[10px] font-bold uppercase text-orange-400">{{ engineBadge }}</span>
          <span class="flex items-center gap-1 rounded-full border border-zinc-800 bg-zinc-900/60 px-2.5 py-1 text-[11px] text-zinc-400"><FileText class="h-3 w-3" /> {{ result.pages }} page{{ result.pages === 1 ? '' : 's' }}</span>
          <span class="flex items-center gap-1 rounded-full border border-zinc-800 bg-zinc-900/60 px-2.5 py-1 text-[11px] text-zinc-400"><FileDigit class="h-3 w-3" /> {{ result.chars.toLocaleString() }} chars</span>
          <span class="flex items-center gap-1 rounded-full border border-zinc-800 bg-zinc-900/60 px-2.5 py-1 text-[11px] text-zinc-400"><Table2 class="h-3 w-3" /> {{ result.tables.length }} table{{ result.tables.length === 1 ? '' : 's' }}</span>
          <span class="ml-auto text-[11px] text-zinc-600">{{ fmtSize(result.size_bytes) }}</span>
        </div>

        <div class="mt-3 flex gap-1 rounded-xl border border-zinc-800 bg-zinc-900/40 p-1 text-xs font-medium">
          <button class="flex-1 rounded-lg px-3 py-1.5 transition" :class="tab === 'text' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'" @click="tab = 'text'">Text</button>
          <button class="flex-1 rounded-lg px-3 py-1.5 transition" :class="tab === 'tables' ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-300'" :disabled="!result.tables.length" @click="tab = 'tables'">
            Tables ({{ result.tables.length }})
          </button>
        </div>

        <pre v-if="tab === 'text'" class="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-xl border border-zinc-800 bg-zinc-950/60 p-4 text-xs leading-relaxed text-zinc-300">{{ result.text.slice(0, 20000) }}{{ result.text.length > 20000 ? '\n…' : '' }}</pre>

        <div v-else class="mt-3 space-y-4">
          <div v-for="(t, ti) in result.tables" :key="ti" class="overflow-hidden rounded-xl border border-zinc-800">
            <div class="border-b border-zinc-800 bg-zinc-900/60 px-3 py-1.5 text-[11px] font-semibold text-zinc-400">Table {{ ti + 1 }} · page {{ t.page }} · {{ t.n_rows - 1 }} data rows × {{ t.n_cols }} cols</div>
            <div class="max-h-72 overflow-auto">
              <table class="w-full text-xs">
                <thead class="sticky top-0 bg-zinc-900 text-zinc-400">
                  <tr><th v-for="(h, hi) in t.rows[0]" :key="hi" class="border-b border-zinc-800 px-3 py-1.5 text-left font-semibold">{{ h || `col_${hi + 1}` }}</th></tr>
                </thead>
                <tbody class="text-zinc-300">
                  <tr v-for="(row, ri) in t.rows.slice(1, 13)" :key="ri" class="odd:bg-zinc-900/30">
                    <td v-for="(c, ci) in row.slice(0, 8)" :key="ci" class="border-b border-zinc-800/60 px-3 py-1.5">{{ c }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-if="t.n_rows - 1 > 12" class="bg-zinc-900/60 px-3 py-1 text-[10px] text-zinc-500">+ {{ t.n_rows - 13 }} more rows (all rows land in the dataset)</p>
          </div>
        </div>

        <!-- send to dataset -->
        <div v-if="!sentDataset" class="mt-5 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-sm font-semibold">Send to dataset</p>
          <p class="mt-0.5 text-xs text-zinc-500">The best table becomes a first-class dataset - numeric columns are typed automatically, then it's SQL-queryable, app-buildable, dashboard-able.</p>
          <div class="mt-3 grid gap-2 sm:grid-cols-2">
            <input v-model="dsName" placeholder="Dataset name" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-orange-500/60" />
            <input v-model="dsDesc" placeholder="Description (optional)" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-orange-500/60" />
          </div>
          <button class="mt-3 flex items-center justify-center gap-1.5 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400 disabled:opacity-40"
            :disabled="!dsName.trim() || sending || !result.tables.length" @click="sendToDataset">
            <Loader2 v-if="sending" class="h-4 w-4 animate-spin" />
            <Table2 v-else class="h-4 w-4" />
            Create dataset from extraction
          </button>
        </div>
        <div v-else class="mt-5 flex flex-wrap items-center gap-3 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-4">
          <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-500/20"><Table2 class="h-4 w-4 text-emerald-400" /></span>
          <div class="min-w-0 flex-1">
            <p class="text-sm font-semibold text-emerald-300">Dataset "{{ sentDataset.name }}" created - {{ sentDataset.row_count }} rows</p>
            <p class="text-xs text-emerald-400/70">Query it with SQL, build an app or a dashboard on top.</p>
          </div>
          <button class="flex items-center gap-1.5 rounded-xl bg-emerald-500 px-3.5 py-2 text-xs font-semibold text-white transition hover:bg-emerald-400" @click="navigateTo(`/datasets/${sentDataset.id}`)">
            Open dataset <ArrowRight class="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <!-- capability cards -->
      <div v-else-if="!extracting" class="mt-6 grid gap-3 sm:grid-cols-3">
        <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
          <ScanText class="h-4 w-4 text-orange-400" />
          <p class="mt-2 text-sm font-semibold">Reads anything</p>
          <p class="mt-1 text-xs leading-relaxed text-zinc-500">PDF text and ruled tables, photographed or scanned pages via OCR, Word documents, Excel workbooks, CSV, JSON and plain text.</p>
        </div>
        <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
          <Table2 class="h-4 w-4 text-orange-400" />
          <p class="mt-2 text-sm font-semibold">Tables become datasets</p>
          <p class="mt-1 text-xs leading-relaxed text-zinc-500">The most complete table lands as rows in the dataset store with real dtypes - integers and floats typed, text preserved.</p>
        </div>
        <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
          <ArrowRight class="h-4 w-4 text-orange-400" />
          <p class="mt-2 text-sm font-semibold">Then everything works</p>
          <p class="mt-1 text-xs leading-relaxed text-zinc-500">DuckDB SQL across your library, apps with rules, dashboards, and the document_extract node in any workflow.</p>
        </div>
      </div>
    </div>
  </div>
</template>
