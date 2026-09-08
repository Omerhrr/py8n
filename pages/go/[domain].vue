<script setup lang="ts">
// The branded system landing (v103) - the front door of a DEPLOYED system.
//
// A company's people do not browse to "the py8n estate"; they go to THEIR
// system's address. This page consumes the PUBLIC domain door
// (GET /systems/by-domain/{domain}, registered without the auth gate on
// purpose): nothing answers there -> an honest dark page; the deployment
// is live -> the system's OWN face BEFORE login (accent color, tagline,
// login headline, logo glyph, environment) and a sign-in that lands in
// the estate as that member. A visitor who already holds a valid token
// gets "Continue as <role>" - the door answers my_role when a token
// rides along.
definePageMeta({ layout: 'plain' })

import { AlertCircle, Loader2, LogIn } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { api } = useApi()

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
const state = ref<'loading' | 'live' | 'dark'>('loading')
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
    router.replace('/systems')
  }
  catch (e: any) {
    const detail = e?.data?.detail || e?.message || ''
    error.value = typeof detail === 'string' && detail ? detail : 'Could not sign in. Check your credentials and try again.'
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <div
    class="flex min-h-full items-center justify-center px-4 py-10"
    :style="state === 'live'
      ? { background: `radial-gradient(1200px 600px at 50% -10%, ${accent}22, transparent 70%), #09090b` }
      : { background: '#09090b' }"
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

    <!-- the system's own face, before login -->
    <div v-else class="w-full max-w-sm">
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
        </div>
      </div>

      <form
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
