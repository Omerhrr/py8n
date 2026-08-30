<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, ClipboardList, CircleAlert, CheckCircle2, Database, TriangleAlert, RotateCcw, Rocket,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v30 — standalone shareable form; chrome-less layout (no sidebar/palette)
definePageMeta({ layout: 'plain' })

const { api } = useApi()
const route = useRoute()

interface FormField {
  name: string
  label?: string | null
  required?: boolean
  options?: (string | number | boolean)[] | null
  default?: string | number | boolean | null
  placeholder?: string | null
}

interface FormDesc {
  app: { name: string; slug: string; description: string }
  form: { title: string; submit_label: string; fields: FormField[] }
  dataset: { name: string; row_count: number } | null
}

const loading = ref(true)
const notFound = ref(false)
const loadError = ref<string | null>(null)
const fd = ref<FormDesc | null>(null)

const model = ref<Record<string, any>>({})
const sending = ref(false)
const submitError = ref<string | null>(null)
const warnings = ref<string[]>([])
const sent = ref(false)

const fields = computed<FormField[]>(() => fd.value?.form.fields || [])

function resetModel() {
  const m: Record<string, any> = {}
  for (const f of fields.value) m[f.name] = f.default !== null && f.default !== undefined ? String(f.default) : ''
  model.value = m
}

async function load() {
  loading.value = true
  notFound.value = false
  loadError.value = null
  try {
    fd.value = await api.get<FormDesc>(`/apps/${route.params.slug}/form`)
    resetModel()
  } catch (e: any) {
    if (e?.status === 404 || e?.statusCode === 404) notFound.value = true
    else loadError.value = e?.data?.detail || e?.message || 'Failed to load the form'
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function submit() {
  sending.value = true
  submitError.value = null
  warnings.value = []
  try {
    const res = await api.post<any>(`/apps/${route.params.slug}/form-submit`, { record: model.value })
    warnings.value = res?.warnings || []
    sent.value = true
  } catch (e: any) {
    submitError.value = e?.data?.detail || e?.message || 'Submit failed'
  } finally {
    sending.value = false
  }
}

function submitAnother() {
  sent.value = false
  warnings.value = []
  submitError.value = null
  resetModel()
}
</script>

<template>
  <div class="flex min-h-screen items-start justify-center px-4 pb-16 pt-10 text-zinc-100 sm:pt-20">
    <!-- loading -->
    <div v-if="loading" class="mt-10 text-zinc-500"><Loader2 class="h-6 w-6 animate-spin" /></div>

    <!-- not published / missing -->
    <div v-else-if="notFound" class="mt-10 text-center">
      <span class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-zinc-900">
        <ClipboardList class="h-6 w-6 text-zinc-600" />
      </span>
      <p class="mt-4 text-sm font-medium text-zinc-300">Form not found (or not published)</p>
      <p class="mt-1 text-xs text-zinc-500">Check the link, or ask the app builder to publish it first.</p>
    </div>

    <p v-else-if="loadError" class="mt-10 max-w-md rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">{{ loadError }}</p>

    <template v-else-if="fd">
      <div class="w-full max-w-md">
        <!-- header -->
        <div class="mb-4 text-center">
          <span class="mx-auto flex h-11 w-11 items-center justify-center rounded-2xl bg-violet-500/15">
            <Rocket class="h-5 w-5 text-violet-400" />
          </span>
          <h1 class="mt-3 text-lg font-bold">{{ fd.form.title || 'Submit' }}</h1>
          <p class="mt-0.5 text-xs text-zinc-500">
            {{ fd.form.title ? fd.app.name : (fd.app.description || fd.app.name) }}
          </p>
          <p v-if="fd.dataset" class="mt-1 inline-flex items-center gap-1 text-[11px] text-zinc-600">
            <Database class="h-3 w-3" /> {{ fd.dataset.row_count }} responses so far
          </p>
        </div>

        <!-- success state -->
        <div v-if="sent" class="rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-6 text-center">
          <CheckCircle2 class="mx-auto h-8 w-8 text-emerald-400" />
          <p class="mt-3 text-sm font-semibold text-emerald-300">Response recorded</p>
          <p v-if="warnings.length" class="mt-2 flex items-start justify-center gap-1.5 text-xs text-yellow-300">
            <TriangleAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>with warnings: {{ warnings.join(' · ') }}</span>
          </p>
          <button
            class="mt-4 inline-flex items-center gap-1.5 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-xs font-medium text-emerald-300 transition hover:bg-emerald-500/20"
            @click="submitAnother"
          >
            <RotateCcw class="h-3.5 w-3.5" /> Submit another response
          </button>
        </div>

        <!-- form -->
        <div v-else class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-5">
          <p v-if="submitError" class="mb-3 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
            <CircleAlert class="h-3.5 w-3.5 shrink-0" /> {{ submitError }}
          </p>
          <div class="space-y-3">
            <div v-for="f in fields" :key="f.name">
              <label class="text-[10px] uppercase tracking-wide text-zinc-500">
                {{ f.label || f.name }}<span v-if="f.required" class="text-red-400"> *</span>
              </label>
              <select
                v-if="f.options && f.options.length"
                v-model="model[f.name]"
                class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
              >
                <option value="" disabled>choose…</option>
                <option v-for="o in f.options" :key="String(o)" :value="String(o)">{{ o }}</option>
              </select>
              <input
                v-else
                v-model="model[f.name]"
                :placeholder="f.placeholder || ''"
                class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
              />
            </div>
          </div>
          <button
            class="mt-5 flex w-full items-center justify-center gap-1.5 rounded-xl bg-violet-500 py-2.5 text-sm font-semibold text-white shadow-lg shadow-violet-500/20 transition hover:bg-violet-400 disabled:opacity-40"
            :disabled="sending"
            @click="submit"
          >
            <Loader2 v-if="sending" class="h-4 w-4 animate-spin" />
            {{ fd.form.submit_label || 'Submit' }}
          </button>
          <p class="mt-3 text-center text-[10px] text-zinc-600">Responses land in the app's dataset instantly.</p>
        </div>
      </div>
    </template>
  </div>
</template>
