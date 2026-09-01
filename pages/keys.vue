<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  KeyRound, Plus, Trash2, Loader2, Copy, Check, Terminal, ShieldCheck,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v41: machine access keys - mint once, shown once, revoke anytime.
interface ApiKeyRow {
  id: string
  name: string
  prefix: string
  created_at: string | null
  last_used_at: string | null
  revoked: boolean
  revoked_at: string | null
  scopes: string[]
  read_only: boolean
}

const { api } = useApi()
const loading = ref(true)
const keys = ref<ApiKeyRow[]>([])

const showCreate = ref(false)
const creating = ref(false)
const newName = ref('')
const newScope = ref<'full' | 'read'>('full')
const createdKey = ref<ApiKeyRow | null>(null)
const fullKey = ref('')
const copied = ref(false)
const revoking = ref<string | null>(null)
const error = ref<string | null>(null)

const CURL_SNIPPET = [
  'curl http://localhost:8000/api/v1/workflows \\',
  '  -H "X-API-Key: <your key>"',
].join('\n')

async function loadKeys() {
  loading.value = true
  try {
    keys.value = await api.get<ApiKeyRow[]>('/keys')
  } finally {
    loading.value = false
  }
}

onMounted(loadKeys)

async function createKey() {
  if (!newName.value.trim()) return
  creating.value = true
  error.value = null
  try {
    const scopes = newScope.value === 'read' ? ['read'] : ['read', 'write']
    const row = await api.post<ApiKeyRow & { key: string }>('/keys', { name: newName.value.trim(), scopes })
    createdKey.value = row
    fullKey.value = row.key
    showCreate.value = false
    newName.value = ''
    newScope.value = 'full'
    await loadKeys()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Key creation failed'
  } finally {
    creating.value = false
  }
}

async function revokeKey(row: ApiKeyRow) {
  if (!confirm(`Revoke key "${row.name}"? Machine clients using it will get 401s.`)) return
  revoking.value = row.id
  try {
    await api.del(`/keys/${row.id}`)
    await loadKeys()
  } finally {
    revoking.value = null
  }
}

function copyKey() {
  navigator.clipboard.writeText(fullKey.value)
  copied.value = true
  setTimeout(() => (copied.value = false), 1500)
}

function fmtDate(iso: string | null) {
  if (!iso) return 'never'
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function closeCreated() {
  createdKey.value = null
  fullKey.value = ''
  copied.value = false
}
</script>

<template>
  <div class="text-zinc-100">
    <header class="border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur sticky top-0 z-20">
      <div class="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="min-w-0">
          <h1 class="text-lg font-bold tracking-tight">API keys</h1>
          <p class="-mt-0.5 text-[11px] text-zinc-500">Machine access - keys act as you, with your scoping</p>
        </div>
        <button
          class="inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/25 transition hover:bg-orange-400 active:scale-95"
          @click="showCreate = true"
        >
          <Plus class="h-4 w-4" />
          Create key
        </button>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <!-- how it works strip -->
      <div class="mb-6 flex items-start gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
        <ShieldCheck class="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
        <p class="text-xs leading-relaxed text-zinc-400">
          Send the key in the <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-orange-300">X-API-Key</code>
          header on any API call. The key authenticates <span class="text-zinc-200">as your account</span> with the same
          data scoping as your sign-in, and keeps working even when
          <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-zinc-300">PY8N_REQUIRE_AUTH</code> is on.
          The full key is shown once at creation - only a hash is stored.
        </p>
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <div v-else-if="!keys.length" class="grid place-items-center rounded-2xl border border-dashed border-zinc-800 py-16 text-center">
        <KeyRound class="mb-3 h-8 w-8 text-zinc-700" />
        <p class="text-sm font-medium text-zinc-400">No API keys yet</p>
        <p class="mt-1 max-w-sm text-xs text-zinc-600">
          Create one to let scripts, CI jobs or other machines call the Py8n API on your behalf.
        </p>
      </div>

      <div v-else class="overflow-hidden rounded-2xl border border-zinc-800">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-zinc-800 bg-zinc-900/60 text-left text-[10px] uppercase tracking-wider text-zinc-500">
              <th class="px-4 py-2.5 font-semibold">Name</th>
              <th class="px-4 py-2.5 font-semibold">Prefix</th>
              <th class="px-4 py-2.5 font-semibold">Access</th>
              <th class="px-4 py-2.5 font-semibold">Created</th>
              <th class="px-4 py-2.5 font-semibold">Last used</th>
              <th class="px-4 py-2.5 font-semibold">Status</th>
              <th class="px-4 py-2.5" />
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in keys" :key="row.id" class="border-b border-zinc-800/60 transition last:border-0 hover:bg-zinc-900/40">
              <td class="px-4 py-3 font-medium">{{ row.name }}</td>
              <td class="px-4 py-3 font-mono text-xs text-zinc-400">{{ row.prefix }}...</td>
              <td class="px-4 py-3">
                <span
                  class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                  :class="row.read_only ? 'bg-amber-500/15 text-amber-400' : 'bg-sky-500/15 text-sky-400'"
                  :title="row.read_only ? 'Read-only: safe methods (GET) only, writes are rejected with 403' : 'Full access: read and write'"
                >
                  {{ row.read_only ? 'Read-only' : 'Full access' }}
                </span>
              </td>
              <td class="px-4 py-3 text-xs text-zinc-500">{{ fmtDate(row.created_at) }}</td>
              <td class="px-4 py-3 text-xs text-zinc-500">{{ fmtDate(row.last_used_at) }}</td>
              <td class="px-4 py-3">
                <span
                  class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                  :class="row.revoked ? 'bg-rose-500/15 text-rose-400' : 'bg-emerald-500/15 text-emerald-400'"
                >
                  {{ row.revoked ? 'Revoked' : 'Active' }}
                </span>
              </td>
              <td class="px-4 py-3 text-right">
                <button
                  v-if="!row.revoked"
                  class="rounded-lg p-1.5 text-zinc-600 transition hover:bg-rose-500/10 hover:text-rose-400"
                  title="Revoke key"
                  :disabled="revoking === row.id"
                  @click="revokeKey(row)"
                >
                  <Loader2 v-if="revoking === row.id" class="h-4 w-4 animate-spin" />
                  <Trash2 v-else class="h-4 w-4" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </main>

    <!-- create modal -->
    <div
      v-if="showCreate"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="showCreate = false"
    >
      <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <h3 class="mb-1 text-lg font-bold">Create API key</h3>
        <p class="mb-5 text-xs text-zinc-500">Give it a name so you remember what uses it.</p>
        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Name</label>
        <input
          v-model="newName"
          class="mb-4 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none transition focus:border-orange-500"
          placeholder="e.g. CI pipeline"
          @keyup.enter="createKey"
        />

        <label class="mb-1.5 block text-xs font-medium uppercase tracking-wide text-zinc-500">Access</label>
        <div class="mb-4 grid grid-cols-2 gap-2">
          <button
            class="rounded-xl border px-3 py-2.5 text-left transition"
            :class="newScope === 'full' ? 'border-orange-500/60 bg-orange-500/10' : 'border-zinc-700 bg-zinc-950 hover:border-zinc-600'"
            @click="newScope = 'full'"
          >
            <span class="block text-xs font-semibold">Full access</span>
            <span class="mt-0.5 block text-[10px] leading-snug text-zinc-500">Read and write - can create, run and delete</span>
          </button>
          <button
            class="rounded-xl border px-3 py-2.5 text-left transition"
            :class="newScope === 'read' ? 'border-amber-500/60 bg-amber-500/10' : 'border-zinc-700 bg-zinc-950 hover:border-zinc-600'"
            @click="newScope = 'read'"
          >
            <span class="block text-xs font-semibold">Read-only</span>
            <span class="mt-0.5 block text-[10px] leading-snug text-zinc-500">Safe GET calls only - writes are rejected with 403</span>
          </button>
        </div>

        <p v-if="error" class="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ error }}</p>
        <div class="flex justify-end gap-2">
          <button class="rounded-xl px-4 py-2 text-sm text-zinc-400 transition hover:text-zinc-200" @click="showCreate = false">
            Cancel
          </button>
          <button
            class="rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
            :disabled="creating || !newName.trim()"
            @click="createKey"
          >
            <Loader2 v-if="creating" class="mr-1 inline h-4 w-4 animate-spin" />
            Create key
          </button>
        </div>
      </div>
    </div>

    <!-- key-shown-once modal -->
    <div
      v-if="createdKey"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="closeCreated"
    >
      <div class="w-full max-w-lg rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <h3 class="mb-1 text-lg font-bold">Key created</h3>
        <p class="mb-4 text-xs text-zinc-500">
          Copy it now - <span class="font-semibold text-amber-300">this is the only time the full key is shown.</span>
        </p>
        <div class="mb-4 flex items-center gap-2 rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2.5">
          <code class="min-w-0 flex-1 truncate font-mono text-xs text-emerald-300">{{ fullKey }}</code>
          <button
            class="shrink-0 rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200"
            title="Copy key"
            @click="copyKey"
          >
            <Check v-if="copied" class="h-4 w-4 text-emerald-400" />
            <Copy v-else class="h-4 w-4" />
          </button>
        </div>
        <div class="mb-5 rounded-xl border border-zinc-800 bg-zinc-950 p-3">
          <p class="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
            <Terminal class="h-3 w-3" /> Try it
          </p>
          <pre class="overflow-x-auto font-mono text-[11px] leading-relaxed text-zinc-400">{{ CURL_SNIPPET }}</pre>
        </div>
        <div class="flex justify-end">
          <button class="rounded-xl bg-zinc-800 px-4 py-2 text-sm font-medium text-zinc-200 transition hover:bg-zinc-700" @click="closeCreated">
            Done
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
