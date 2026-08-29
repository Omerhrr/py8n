<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  Variable as VariableIcon, Plus, Lock, LockOpen, Trash2, Pencil,
  KeyRound, Loader2, Search,
} from 'lucide-vue-next'
import { usePy8nStore } from '~/stores/py8n'
import type { EnvVariable } from '~/types/node'

const store = usePy8nStore()

// rendered via a script constant — raw braces inside template interpolations
// confuse the Vue parser (it closes at the first `}}`)
const ENV_SNIPPET = '{{ env.KEY }}'

const loading = ref(true)
const search = ref('')

// create/edit modal state
const showEdit = ref(false)
const editing = ref<EnvVariable | null>(null) // null = create mode
const saving = ref(false)
const formKey = ref('')
const formValue = ref('')
const formSecret = ref(false)
const formDesc = ref('')
const error = ref<string | null>(null)
const deleting = ref<string | null>(null)

onMounted(async () => {
  try {
    await store.loadEnvVars(true)
  } finally {
    loading.value = false
  }
})

const rows = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return store.envVars
  return store.envVars.filter(
    (v) => v.key.toLowerCase().includes(q) || (v.description || '').toLowerCase().includes(q),
  )
})

const stats = computed(() => ({
  total: store.envVars.length,
  secrets: store.envVars.filter((v) => v.is_secret).length,
}))

function openCreate() {
  editing.value = null
  formKey.value = ''
  formValue.value = ''
  formSecret.value = false
  formDesc.value = ''
  error.value = null
  showEdit.value = true
}

function openEdit(row: EnvVariable) {
  editing.value = row
  formKey.value = row.key
  // secrets are write-only: the server never echoes the value back
  formValue.value = row.is_secret ? '' : (row.value ?? '')
  formSecret.value = row.is_secret
  formDesc.value = row.description || ''
  error.value = null
  showEdit.value = true
}

async function save() {
  const key = formKey.value.trim()
  if (!editing.value && !key) return
  saving.value = true
  error.value = null
  try {
    if (editing.value) {
      const body: Record<string, any> = { description: formDesc.value, is_secret: formSecret.value }
      // secrets are write-only: blank field = keep the stored value
      if (formValue.value !== '') body.value = formValue.value
      else if (!formSecret.value) body.value = formValue.value // plaintext var cleared intentionally
      else body.value = '__keep__'
      await store.updateEnvVar(editing.value.id, body)
    } else {
      await store.createEnvVar({
        key, value: formValue.value, is_secret: formSecret.value, description: formDesc.value,
      })
    }
    showEdit.value = false
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Save failed'
  } finally {
    saving.value = false
  }
}

async function remove(row: EnvVariable) {
  if (!confirm(`Delete variable "${row.key}"? Workflows using {{ env.${row.key} }} will fail to resolve.`)) return
  deleting.value = row.id
  try {
    await store.deleteEnvVar(row.id)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Delete failed')
  } finally {
    deleting.value = null
  }
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}
</script>

<template>
  <div class="pb-10 text-zinc-100">
    <!-- page header -->
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-5xl items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="flex min-w-0 items-center gap-3">
          <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500/25 to-cyan-500/15 text-sky-300">
            <VariableIcon class="h-4 w-4" />
          </span>
          <div class="min-w-0">
            <h1 class="text-lg font-bold tracking-tight">Variables</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Global values — use <code class="rounded bg-zinc-800 px-1 font-mono text-[10px] text-sky-300">{{ ENV_SNIPPET }}</code> in any node field</p>
          </div>
        </div>
        <button
          class="inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/25 transition hover:bg-orange-400 active:scale-95"
          @click="openCreate"
        >
          <Plus class="h-4 w-4" /> New variable
        </button>
      </div>
    </header>

    <main class="mx-auto max-w-5xl px-4 sm:px-6">
      <!-- stats strip -->
      <section class="mb-5 mt-6 grid grid-cols-3 gap-3">
        <div class="rounded-xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
          <p class="text-xl font-bold text-zinc-100">{{ stats.total }}</p>
          <p class="text-[11px] uppercase tracking-wide text-zinc-500">Variables</p>
        </div>
        <div class="rounded-xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
          <p class="flex items-center gap-1.5 text-xl font-bold text-amber-300">
            <KeyRound class="h-4 w-4" /> {{ stats.secrets }}
          </p>
          <p class="text-[11px] uppercase tracking-wide text-zinc-500">Secrets</p>
        </div>
        <div class="rounded-xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
          <p class="truncate font-mono text-xs text-sky-300">{{ ENV_SNIPPET }}</p>
          <p class="text-[11px] uppercase tracking-wide text-zinc-500">Template syntax</p>
        </div>
      </section>

      <!-- search -->
      <div class="mb-4 flex items-center justify-between gap-3">
        <div class="relative">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
          <input
            v-model="search"
            class="w-56 rounded-xl border border-zinc-800 bg-zinc-900/60 py-2 pl-9 pr-3 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500"
            placeholder="Search variables…"
          />
        </div>
      </div>

      <!-- list -->
      <div v-if="loading" class="space-y-2">
        <div v-for="i in 3" :key="i" class="h-16 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900/50" />
      </div>

      <div v-else-if="rows.length === 0" class="rounded-2xl border border-dashed border-zinc-800 p-12 text-center">
        <VariableIcon class="mx-auto mb-3 h-8 w-8 text-zinc-600" />
        <p v-if="store.envVars.length" class="text-zinc-400">No variables match your search.</p>
        <template v-else>
          <p class="text-zinc-400">No variables yet.</p>
          <p class="mx-auto mt-2 max-w-md text-xs leading-relaxed text-zinc-600">
            Store API base URLs, feature flags, or tokens once — then reference them from every
            workflow with <code class="rounded bg-zinc-800 px-1 font-mono">{{ ENV_SNIPPET }}</code>.
            Secret values are encrypted at rest and never echoed back.
          </p>
          <button
            class="mt-4 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400"
            @click="openCreate"
          >
            Create your first variable
          </button>
        </template>
      </div>

      <div v-else class="overflow-hidden rounded-2xl border border-zinc-800">
        <div
          v-for="row in rows"
          :key="row.id"
          class="flex items-center gap-3 border-b border-zinc-800/60 bg-zinc-900/30 px-4 py-3 transition last:border-0 hover:bg-zinc-900/60"
        >
          <span
            class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border"
            :class="row.is_secret ? 'border-amber-500/30 bg-amber-500/10 text-amber-300' : 'border-sky-500/30 bg-sky-500/10 text-sky-300'"
          >
            <Lock v-if="row.is_secret" class="h-3.5 w-3.5" />
            <LockOpen v-else class="h-3.5 w-3.5" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <code class="truncate font-mono text-sm font-semibold text-zinc-100">{{ row.key }}</code>
              <span
                v-if="row.is_secret"
                class="shrink-0 rounded-md border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300"
              >secret</span>
            </div>
            <p v-if="row.description" class="truncate text-xs text-zinc-500">{{ row.description }}</p>
          </div>
          <div class="hidden min-w-0 flex-1 sm:block">
            <span v-if="row.is_secret" class="font-mono text-xs tracking-widest text-zinc-600">••••••••</span>
            <span v-else class="block truncate font-mono text-xs text-zinc-400">{{ row.value }}</span>
          </div>
          <span class="hidden shrink-0 text-[11px] text-zinc-600 md:block">updated {{ fmtDate(row.updated_at) }}</span>
          <div class="flex shrink-0 items-center gap-1">
            <button
              class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200"
              title="Edit variable"
              @click="openEdit(row)"
            >
              <Pencil class="h-3.5 w-3.5" />
            </button>
            <button
              class="rounded-lg p-1.5 text-zinc-600 transition hover:bg-rose-500/10 hover:text-rose-400"
              title="Delete variable"
              :disabled="deleting === row.id"
              @click="remove(row)"
            >
              <Loader2 v-if="deleting === row.id" class="h-3.5 w-3.5 animate-spin" />
              <Trash2 v-else class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </main>

    <!-- create / edit modal -->
    <div
      v-if="showEdit"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="showEdit = false"
    >
      <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <h3 class="mb-1 text-lg font-bold">{{ editing ? `Edit ${editing.key}` : 'New variable' }}</h3>
        <p class="mb-5 text-xs text-zinc-500">
          {{ editing ? 'Value changes apply to every future run.' : ('Reference it anywhere with ' + ENV_SNIPPET + '.') }}
        </p>

        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Key</label>
        <input
          v-model="formKey"
          class="mb-1 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-sm outline-none transition focus:border-orange-500 disabled:opacity-60"
          placeholder="API_BASE_URL"
          :disabled="!!editing"
          @keyup.enter="save"
        />
        <p v-if="!editing" class="mb-3 text-[10px] text-zinc-600">Letters, digits, underscores — used exactly as typed in templates.</p>
        <div v-else class="mb-3" />

        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Value</label>
        <input
          v-model="formValue"
          class="mb-1 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 font-mono text-sm outline-none transition focus:border-orange-500"
          :placeholder="editing && formSecret ? 'Leave blank to keep the current value' : 'value'"
          type="text"
          :autocomplete="formSecret ? 'off' : 'on'"
          @keyup.enter="save"
        />
        <p v-if="editing && formSecret" class="mb-3 text-[10px] text-zinc-600">Secrets are write-only — the stored value is never sent back to the browser.</p>
        <div v-else class="mb-3" />

        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Description</label>
        <input
          v-model="formDesc"
          class="mb-4 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none transition focus:border-orange-500"
          placeholder="What is this value for?"
          @keyup.enter="save"
        />

        <label class="mb-4 flex cursor-pointer items-center gap-2.5 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2.5">
          <input v-model="formSecret" type="checkbox" class="h-4 w-4 accent-orange-500" />
          <span class="flex items-center gap-1.5 text-sm text-zinc-300">
            <Lock class="h-3.5 w-3.5 text-amber-300" /> Secret value
          </span>
          <span class="ml-auto text-[10px] text-zinc-600">masked in UI · write-only</span>
        </label>

        <p v-if="error" class="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ error }}</p>

        <div class="flex justify-end gap-2">
          <button class="rounded-xl px-4 py-2 text-sm text-zinc-400 transition hover:text-zinc-200" @click="showEdit = false">
            Cancel
          </button>
          <button
            class="inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
            :disabled="saving || (!editing && !formKey.trim())"
            @click="save"
          >
            <Loader2 v-if="saving" class="h-4 w-4 animate-spin" />
            {{ editing ? 'Save changes' : 'Create variable' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
