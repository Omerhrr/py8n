<script setup lang="ts">
// Sign-in / first-run owner registration (v37).
//
// The page reads GET /auth/status: when the instance has no users yet the
// form becomes "Create the owner account" (the first account becomes admin
// and claims all existing resources); afterwards it is a plain login. In the
// default open mode this page exists but nothing forces the user here.
import { LogIn, UserPlus, AlertCircle } from 'lucide-vue-next'

definePageMeta({ layout: 'plain' })

const auth = useAuthStore()
const router = useRouter()

const email = ref('')
const password = ref('')
const name = ref('')
const busy = ref(false)
const error = ref('')

const hasUsers = computed(() => auth.status?.has_users !== false)
const mode = computed(() => (hasUsers.value ? 'signin' : 'register'))

// Fresh instance: the registration form IS the login form, just with the
// owner-account copy. Registering flips has_users, so switch copy instantly.
const title = computed(() => (mode.value === 'register' ? 'Create the owner account' : 'Sign in to Py8n'))
const subtitle = computed(() =>
  mode.value === 'register'
    ? 'The first account becomes the admin and inherits every resource already on this instance.'
    : 'Use your Py8n account to continue.',
)

onMounted(async () => {
  await auth.boot()
  // Already signed in with a valid token? Straight to the dashboard.
  if (auth.token && await auth.fetchMe()) {
    router.replace('/')
  }
})

async function submit() {
  error.value = ''
  if (!email.value.trim() || !password.value) {
    error.value = 'Email and password are required.'
    return
  }
  busy.value = true
  try {
    if (mode.value === 'register') {
      await auth.register(email.value.trim(), password.value, name.value.trim())
    }
    else {
      await auth.login(email.value.trim(), password.value)
    }
    const redirect = router.currentRoute.value.query.redirect
    router.replace(typeof redirect === 'string' ? redirect : '/')
  }
  catch (e: any) {
    const detail = e?.data?.detail || e?.message || ''
    if (typeof detail === 'string' && detail) {
      error.value = detail
    }
    else {
      error.value = 'Could not sign in. Check your credentials and try again.'
    }
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="flex min-h-full items-center justify-center bg-zinc-950 px-4 py-10">
    <div class="w-full max-w-sm">
      <!-- brand -->
      <div class="mb-8 flex flex-col items-center gap-3 text-center">
        <Py8nLogo :size="56" />
        <div>
          <h1 class="text-xl font-bold tracking-tight text-zinc-100">{{ title }}</h1>
          <p class="mt-1 text-xs leading-relaxed text-zinc-500">{{ subtitle }}</p>
        </div>
      </div>

      <form
        class="space-y-3 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5 shadow-2xl shadow-black/40"
        @submit.prevent="submit"
      >
        <div v-if="error" class="flex items-start gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2.5 text-xs text-rose-300">
          <AlertCircle class="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span class="min-w-0 break-words">{{ error }}</span>
        </div>

        <label v-if="mode === 'register'" class="block">
          <span class="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-500">Name</span>
          <input
            v-model="name"
            type="text"
            autocomplete="name"
            placeholder="Ada Lovelace"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2.5 text-sm text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-orange-500/60 focus:ring-2 focus:ring-orange-500/20"
          >
        </label>

        <label class="block">
          <span class="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-500">Email</span>
          <input
            v-model="email"
            type="email"
            required
            autocomplete="email"
            placeholder="you@company.com"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2.5 text-sm text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-orange-500/60 focus:ring-2 focus:ring-orange-500/20"
          >
        </label>

        <label class="block">
          <span class="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-500">Password</span>
          <input
            v-model="password"
            type="password"
            required
            :autocomplete="mode === 'register' ? 'new-password' : 'current-password'"
            placeholder="At least 8 characters"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2.5 text-sm text-zinc-100 placeholder-zinc-600 outline-none transition focus:border-orange-500/60 focus:ring-2 focus:ring-orange-500/20"
          >
        </label>

        <button
          type="submit"
          :disabled="busy"
          class="flex w-full items-center justify-center gap-2 rounded-xl bg-orange-500 px-3 py-2.5 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
        >
          <UserPlus v-if="mode === 'register'" class="h-4 w-4" />
          <LogIn v-else class="h-4 w-4" />
          <span v-if="!busy">{{ mode === 'register' ? 'Create account' : 'Sign in' }}</span>
          <span v-else>Working...</span>
        </button>

        <p v-if="mode === 'register'" class="text-center text-[11px] leading-relaxed text-zinc-600">
          Owner registration is only offered while the instance has no accounts.
        </p>
      </form>

      <p class="mt-6 text-center text-[10px] text-zinc-600">
        Py8n - Python-native workflow automation
      </p>
    </div>
  </div>
</template>
