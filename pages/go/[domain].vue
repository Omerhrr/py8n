<script setup lang="ts">
// The branded system front door (v103) - and, since v105, the system's own
// WORK SURFACE.
//
// A company's people do not browse to "the py8n estate"; they go to THEIR
// system's address. This page consumes the PUBLIC domain door
// (GET /systems/by-domain/{domain}, registered without the auth gate on
// purpose): nothing answers there -> an honest dark page; the deployment
// is live -> the system's OWN face BEFORE login (accent color, tagline,
// login headline, logo glyph, environment) and a sign-in that lands ON the
// system's pending work - not a redirect into the builder's estate.
// v105: after sign-in (or "Continue as <role>") the door fetches
// GET /systems/by-domain/{domain}/work - the machines bound to the system
// with their live operation, and the attention rows (open instances past
// their SLA, most-overdue first, the escalation book per row). Editors
// and the owner acknowledge escalations right here; viewers read. This is
// the company's operations system, not a workflow tool.
definePageMeta({ layout: 'plain' })

import { AlertCircle, CheckCircle2, ChevronDown, ExternalLink, Loader2, LogIn } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { api } = useApi()

interface EscalationBook {
  count: number
  acked: { by: string, at: string, note: string, snooze_until?: string, snooze_remaining_seconds?: number } | null
  reschedule_at: string
  reschedule_remaining_seconds: number
}
interface WorkAttention {
  process_id: string
  process_name: string
  instance_id: string
  ref: string
  title: string
  state: string
  entered_state_at: string | null
  due_at: string | null
  overdue_seconds: number
  escalation: EscalationBook | null
}
interface WorkMachine {
  process_id: string
  name: string
  open: number
  stuck: number
  escalation_summary: string
}
interface Work {
  machines: WorkMachine[]
  attention: WorkAttention[]
  totals: { machines: number, open: number, stuck: number, attention: number }
  now: string
}
interface Identity {
  system: { id: string, name: string, icon: string | null, color: string | null }
  domain: string
  url: string
  environment: string
  status: string
  branding: { accent: string, tagline: string, login_headline: string, logo: string }
  my_role: string | null
}

const identity = ref<Identity | null>(null)
const state = ref<'loading' | 'live' | 'dark' | 'work'>('loading')
const darkDetail = ref('')

const domain = computed(() => String(route.params.domain || ''))
const accent = computed(() => identity.value?.branding?.accent || '#38bdf8')
const headline = computed(() => identity.value?.branding?.login_headline || identity.value?.system.name || 'Operations')
const tagline = computed(() => identity.value?.branding?.tagline || '')
const glyph = computed(() => identity.value?.branding?.logo || identity.value?.system.icon || '◎')

const email = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
const knownRole = ref<string | null>(null)

const work = ref<Work | null>(null)
const myRole = ref<string | null>(null)
const canAct = computed(() => myRole.value === 'owner' || myRole.value === 'editor')
const workError = ref('')
const workBusy = ref(false)

// the ack form (one open row at a time, v96 parity: loan or reschedule)
const ackFor = ref<string | null>(null)
const ackBy = ref('')
const ackNote = ref('')
const ackSnooze = ref<string>('')
const ackReschedule = ref<string>('')
const ackBusy = ref(false)
const ackError = ref('')

function fmtOverdue(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${Math.max(1, m)}m`
}

async function load() {
  state.value = 'loading'
  try {
    // restore any token FIRST so the door answers my_role for it
    await auth.boot()
    const res = await api.get<Identity>(`/systems/by-domain/${encodeURIComponent(domain.value)}`)
    identity.value = res
    knownRole.value = auth.token && res.my_role ? res.my_role : null
    state.value = 'live'
  }
  catch (e: any) {
    darkDetail.value = e?.data?.detail || e?.message || ''
    state.value = 'dark'
  }
}

onMounted(load)

useHead(() => ({ title: state.value === 'live' ? headline.value : domain.value }))

async function loadWork(): Promise<boolean> {
  workError.value = ''
  try {
    const res = await api.get<any>(`/systems/by-domain/${encodeURIComponent(domain.value)}/work`)
    work.value = res.work as Work
    myRole.value = (res.my_role as string) || null
    state.value = 'work'
    return true
  }
  catch (e: any) {
    workError.value = e?.data?.detail || e?.message || 'Could not load the work surface.'
    return false
  }
}

async function enter() {
  error.value = ''
  if (!knownRole.value && (!email.value.trim() || !password.value)) {
    error.value = 'Email and password are required.'
    return
  }
  busy.value = true
  try {
    if (knownRole.value && !email.value.trim() && !password.value) {
      const ok = await auth.fetchMe() // revalidate the held token before walking in
      if (!ok) {
        error.value = 'Your session expired - sign in below.'
        knownRole.value = null
        return
      }
    }
    else {
      await auth.login(email.value.trim(), password.value)
    }
    // v105: sign-in lands on the system's own work surface, not the estate
    const ok = await loadWork()
    if (!ok) error.value = workError.value || 'Could not load the work surface.'
  }
  catch (e: any) {
    const detail = e?.data?.detail || e?.message || ''
    error.value = typeof detail === 'string' && detail ? detail : 'Could not sign in. Check your credentials and try again.'
  }
  finally {
    busy.value = false
  }
}

function openAck(row: WorkAttention) {
  ackFor.value = ackFor.value === row.instance_id ? null : row.instance_id
  ackError.value = ''
  ackSnooze.value = ''
  ackReschedule.value = ''
  ackNote.value = ''
  ackBy.value = auth.user?.name || auth.user?.email || ''
}

async function submitAck(row: WorkAttention) {
  ackError.value = ''
  if (!ackBy.value.trim()) {
    ackError.value = 'Name the handler taking this (by).'
    return
  }
  ackBusy.value = true
  try {
    const body: Record<string, any> = { by: ackBy.value.trim() }
    if (ackNote.value.trim()) body.note = ackNote.value.trim()
    if (ackSnooze.value !== '' && ackSnooze.value != null) body.snooze_hours = Number(ackSnooze.value)
    if (ackReschedule.value !== '' && ackReschedule.value != null) body.reschedule_in_minutes = Number(ackReschedule.value)
    await api.post(`/processes/${row.process_id}/instances/${row.instance_id}/escalations/ack`, body)
    ackFor.value = null
    await loadWork()
  }
  catch (e: any) {
    ackError.value = e?.data?.detail || e?.message || 'The door refused the take.'
  }
  finally {
    ackBusy.value = false
  }
}

function backToSignIn() {
  state.value = 'live'
  work.value = null
}
</script>

<template>
  <div
    class="flex min-h-full flex-col items-center px-4 py-10"
    :style="state === 'dark'
      ? { background: '#09090b' }
      : { background: `radial-gradient(1200px 600px at 50% -10%, ${accent}22, transparent 70%), #09090b` }"
  >
    <!-- loading -->
    <div v-if="state === 'loading'" class="flex items-center gap-2 text-xs text-zinc-500">
      <Loader2 class="h-3.5 w-3.5 animate-spin" /> Resolving {{ domain }}...
    </div>

    <!-- the honest dark page: nothing answers on this address -->
    <div v-else-if="state === 'dark'" class="w-full max-w-sm text-center">
      <div class="mb-6 flex flex-col items-center gap-3">
        <div class="flex h-14 w-14 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-900/60 text-2xl text-zinc-600">
          ◌
        </div>
        <div>
          <h1 class="text-lg font-bold tracking-tight text-zinc-200">Nothing answers here</h1>
          <p class="mt-1 font-mono text-xs text-zinc-500">{{ domain }}</p>
        </div>
      </div>
      <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4 text-xs leading-relaxed text-zinc-500">
        <p v-if="darkDetail" class="mb-1 text-zinc-400">{{ darkDetail }}</p>
        <p>No live system is deployed on this address. It may have been paused or retired - check with the operator who runs it.</p>
      </div>
      <NuxtLink to="/login" class="mt-5 inline-block text-[11px] text-zinc-600 transition hover:text-zinc-400">
        Sign in to the platform instead
      </NuxtLink>
    </div>

    <!-- the system's own face -->
    <div v-else class="w-full max-w-2xl">
      <div class="mb-8 flex flex-col items-center gap-3 text-center">
        <div
          class="flex h-16 w-16 items-center justify-center rounded-2xl border text-3xl"
          :style="{ borderColor: `${accent}66`, backgroundColor: `${accent}14` }"
        >
          {{ glyph }}
        </div>
        <div>
          <h1 class="text-xl font-bold tracking-tight text-zinc-100">{{ headline }}</h1>
          <p v-if="tagline" class="mt-1 text-xs leading-relaxed text-zinc-500">{{ tagline }}</p>
        </div>
        <div class="flex items-center gap-1.5">
          <span
            class="rounded-full px-2 py-0.5 text-[10px] font-bold"
            :style="{ backgroundColor: `${accent}1f`, color: accent }"
          >{{ identity?.system.name }}</span>
          <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-bold uppercase text-zinc-300">{{ identity?.environment }}</span>
          <span v-if="myRole" class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-bold text-zinc-300">you: {{ myRole }}</span>
        </div>
      </div>

      <!-- ===== the work surface (signed in) ===== -->
      <template v-if="state === 'work' && work">
        <div class="mb-4 grid grid-cols-4 gap-2">
          <div class="rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2.5 text-center">
            <div class="text-lg font-bold text-zinc-100">{{ work.totals.machines }}</div>
            <div class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">machines</div>
          </div>
          <div class="rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2.5 text-center">
            <div class="text-lg font-bold text-zinc-100">{{ work.totals.open }}</div>
            <div class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">open</div>
          </div>
          <div class="rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2.5 text-center">
            <div class="text-lg font-bold" :class="work.totals.stuck > 0 ? 'text-amber-400' : 'text-zinc-100'">{{ work.totals.stuck }}</div>
            <div class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">past SLA</div>
          </div>
          <div class="rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2.5 text-center">
            <div class="text-lg font-bold" :class="work.totals.attention > 0 ? 'text-rose-400' : 'text-zinc-100'">{{ work.totals.attention }}</div>
            <div class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">attention</div>
          </div>
        </div>

        <div v-if="workError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2.5 text-xs text-rose-300">
          <AlertCircle class="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span class="min-w-0 break-words">{{ workError }}</span>
        </div>

        <!-- needs attention -->
        <div class="mb-4 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
          <div class="mb-3 flex items-center justify-between">
            <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Needs attention</h2>
            <button
              class="flex items-center gap-1 text-[11px] text-zinc-500 transition hover:text-zinc-300"
              :disabled="workBusy"
              @click="loadWork()"
            >
              <Loader2 v-if="workBusy" class="h-3 w-3 animate-spin" />
              Refresh
            </button>
          </div>

          <p v-if="work.attention.length === 0" class="flex items-center gap-2 py-3 text-xs text-zinc-500">
            <CheckCircle2 class="h-4 w-4 text-emerald-500" />
            Nothing is past its SLA. The operation is holding its promises.
          </p>

          <ul v-else class="space-y-2">
            <li
              v-for="row in work.attention"
              :key="row.instance_id"
              class="rounded-xl border border-zinc-800 bg-zinc-950/60 p-3"
            >
              <div class="flex flex-wrap items-start justify-between gap-2">
                <div class="min-w-0">
                  <div class="truncate text-sm font-semibold text-zinc-100">{{ row.title || row.ref }}</div>
                  <div class="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-zinc-500">
                    <span class="font-mono">{{ row.ref }}</span>
                    <span>·</span>
                    <span>{{ row.process_name }}</span>
                    <span>·</span>
                    <span class="rounded bg-zinc-800 px-1.5 py-0.5 font-medium text-zinc-300">{{ row.state }}</span>
                  </div>
                </div>
                <div class="text-right">
                  <div class="flex items-center justify-end gap-1 text-xs font-bold text-rose-400">
                    <Clock3 class="h-3.5 w-3.5" />
                    {{ fmtOverdue(row.overdue_seconds) }} over
                  </div>
                  <div v-if="row.escalation && row.escalation.count > 0" class="mt-0.5 text-[10px] text-zinc-500">
                    <template v-if="row.escalation.acked">
                      taken by {{ row.escalation.acked.by }}
                      <span v-if="row.escalation.reschedule_remaining_seconds > 0">
                        · re-knock in {{ fmtOverdue(row.escalation.reschedule_remaining_seconds) }}
                      </span>
                      <span v-else-if="row.escalation.acked.snooze_remaining_seconds">
                        · snoozed
                      </span>
                    </template>
                    <template v-else>
                      door knocked {{ row.escalation.count }}×
                    </template>
                  </div>
                </div>
              </div>

              <!-- the take (editor+) -->
              <div v-if="canAct" class="mt-2">
                <button
                  class="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px] font-semibold transition active:scale-[0.98]"
                  :style="{ backgroundColor: `${accent}1f`, color: accent }"
                  @click="openAck(row)"
                >
                  <CheckCircle2 class="h-3.5 w-3.5" />
                  {{ ackFor === row.instance_id ? 'Close' : 'I have this' }}
                  <ChevronDown class="h-3 w-3 transition" :class="ackFor === row.instance_id ? 'rotate-180' : ''" />
                </button>

                <form
                  v-if="ackFor === row.instance_id"
                  class="mt-2 space-y-2 rounded-lg border border-zinc-800 bg-zinc-900/70 p-3"
                  @submit.prevent="submitAck(row)"
                >
                  <div v-if="ackError" class="rounded-lg border border-rose-500/30 bg-rose-500/10 px-2.5 py-2 text-[11px] text-rose-300">
                    {{ ackError }}
                  </div>
                  <div class="grid grid-cols-2 gap-2">
                    <label class="block">
                      <span class="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Handler</span>
                      <input
                        v-model="ackBy"
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-600"
                        placeholder="who takes this"
                      >
                    </label>
                    <label class="block">
                      <span class="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Note</span>
                      <input
                        v-model="ackNote"
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-600"
                        placeholder="optional"
                      >
                    </label>
                    <label class="block">
                      <span class="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Snooze (hours)</span>
                      <input
                        v-model="ackSnooze"
                        type="number"
                        min="0"
                        step="any"
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-600"
                        placeholder="re-knock after N h"
                      >
                    </label>
                    <label class="block">
                      <span class="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Reschedule (minutes)</span>
                      <input
                        v-model="ackReschedule"
                        type="number"
                        min="0"
                        step="any"
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 outline-none focus:border-zinc-600"
                        placeholder="re-knock at +N min"
                      >
                    </label>
                  </div>
                  <p class="text-[10px] text-zinc-600">One clock per receipt - a snooze OR a reschedule, not both.</p>
                  <button
                    type="submit"
                    :disabled="ackBusy"
                    class="flex w-full items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold transition active:scale-[0.98] disabled:opacity-60"
                    :style="{ backgroundColor: accent, color: '#09090b' }"
                  >
                    <Loader2 v-if="ackBusy" class="h-3.5 w-3.5 animate-spin" />
                    <CheckCircle2 v-else class="h-3.5 w-3.5" />
                    {{ ackBusy ? 'Taking...' : 'Take it' }}
                  </button>
                </form>
              </div>
            </li>
          </ul>
        </div>

        <!-- the machines -->
        <div class="mb-4 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
          <h2 class="mb-3 text-xs font-bold uppercase tracking-wide text-zinc-400">The machines</h2>
          <ul class="space-y-1.5">
            <li
              v-for="m in work.machines"
              :key="m.process_id"
              class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2"
            >
              <span class="truncate text-sm font-medium text-zinc-200">{{ m.name }}</span>
              <span class="flex items-center gap-1.5">
                <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-bold text-zinc-300">{{ m.open }} open</span>
                <span
                  class="rounded-full px-2 py-0.5 text-[10px] font-bold"
                  :class="m.stuck > 0 ? 'bg-amber-500/15 text-amber-400' : 'bg-zinc-800 text-zinc-400'"
                >{{ m.stuck }} stuck</span>
              </span>
            </li>
          </ul>
        </div>

        <div class="flex items-center justify-between">
          <button class="text-[11px] text-zinc-500 transition hover:text-zinc-300" @click="backToSignIn">
            Sign in as someone else
          </button>
          <NuxtLink
            to="/systems"
            class="flex items-center gap-1 text-[11px] text-zinc-500 transition hover:text-zinc-300"
          >
            <ExternalLink class="h-3 w-3" />
            Open the full estate
          </NuxtLink>
        </div>
      </template>

      <!-- ===== the sign-in (not yet signed in) ===== -->
      <form
        v-else
        class="space-y-3 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5 shadow-2xl shadow-black/40"
        @submit.prevent="enter"
      >
        <div v-if="error" class="flex items-start gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2.5 text-xs text-rose-300">
          <AlertCircle class="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span class="min-w-0 break-words">{{ error }}</span>
        </div>

        <!-- the held session: one click back into the system -->
        <template v-if="knownRole">
          <p class="text-center text-[11px] text-zinc-500">
            You hold <span class="font-bold" :style="{ color: accent }">{{ knownRole }}</span> access on this system.
          </p>
          <button
            type="submit"
            :disabled="busy"
            class="flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-sm font-semibold shadow-lg transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
            :style="{ backgroundColor: accent, color: '#09090b', boxShadow: `0 10px 15px -3px ${accent}33` }"
          >
            <LogIn class="h-4 w-4" />
            <span v-if="!busy">Continue as {{ knownRole }}</span>
            <span v-else>Working...</span>
          </button>
          <p class="text-center text-[10px] text-zinc-600">or sign in below with a different account</p>
        </template>

        <label class="block">
          <span class="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-500">Email</span>
          <input
            v-model="email"
            type="email"
            autocomplete="email"
            placeholder="you@company.com"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2.5 text-sm text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-zinc-600"
          >
        </label>

        <label class="block">
          <span class="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-500">Password</span>
          <input
            v-model="password"
            type="password"
            autocomplete="current-password"
            placeholder="Your platform password"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2.5 text-sm text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-zinc-600"
          >
        </label>

        <button
          v-if="!knownRole"
          type="submit"
          :disabled="busy"
          class="flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-sm font-semibold shadow-lg transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
          :style="{ backgroundColor: accent, color: '#09090b', boxShadow: `0 10px 15px -3px ${accent}33` }"
        >
          <LogIn class="h-4 w-4" />
          <span v-if="!busy">Sign in to {{ headline }}</span>
          <span v-else>Working...</span>
        </button>
      </form>

      <p class="mt-6 text-center text-[10px] text-zinc-600">
        {{ identity?.domain }} · powered by Py8n - the Python-native business operations platform
      </p>
    </div>
  </div>
</template>
