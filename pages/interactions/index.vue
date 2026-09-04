<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Loader2, MessagesSquare, Send, X, UserRound, Bot, UserCog, Info, Phone, Mail, MessageCircle, Globe, Hash, TerminalSquare, Smartphone } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v68: the interaction layer - channels are interchangeable adapters, the
// conversation is the product. One universal ingress feeds every channel
// (voice, whatsapp, telegram, discord, web, app, api, sms, email); a bound
// handler workflow answers; the transcript records which channel each
// message actually traveled through, so a participant can hop channels
// mid-conversation and the context follows.

interface Channel {
  id: string; label: string; builtin: boolean; description: string
  providers: string[]; adapter: Record<string, string | string[] | undefined>
  conversations: number
}
interface Message {
  id: string; role: string; channel: string; text: string
  payload: Record<string, any>; created_at: string | null
}
interface Conversation {
  id: string; channel: string
  participant: { id: string; name: string }
  state: string; outcome: string; context: Record<string, any>
  handler_workflow_id: string | null; handler_workflow_name: string | null
  channels_used: string[]; message_count: number
  created_at: string | null; updated_at: string | null; last_message_at: string | null
  messages: Message[] | null
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const channels = ref<Channel[]>([])
const conversations = ref<Conversation[]>([])
const selected = ref<Conversation | null>(null)

const workflows = ref<any[]>([])
const simChannel = ref('app')
const simSender = ref('')
const simText = ref('')
const simResult = ref<{ reply: string | null; conversation_id: string } | null>(null)
const simBusy = ref(false)
const sendText = ref('')
const sendBusy = ref(false)
const filterChannel = ref('')
const closeOutcome = ref('')

const channelIcon: Record<string, any> = {
  app: Smartphone, web: Globe, api: TerminalSquare,
  voice: Phone, whatsapp: MessageCircle, telegram: Hash,
  discord: MessageCircle, sms: Smartphone, email: Mail,
}

const roleChip: Record<string, string> = {
  user: 'bg-sky-500/10 text-sky-300 border-sky-500/25',
  agent: 'bg-violet-500/10 text-violet-300 border-violet-500/25',
  human_agent: 'bg-amber-500/10 text-amber-300 border-amber-500/25',
  system: 'bg-zinc-700/20 text-zinc-500 border-zinc-700/40',
}

const visibleConversations = computed(() =>
  filterChannel.value
    ? conversations.value.filter(c => c.channel === filterChannel.value || (c.channels_used || []).includes(filterChannel.value))
    : conversations.value)

async function load() {
  loading.value = true
  try {
    const [ch, convs] = await Promise.all([
      api.get<{ channels: Channel[] }>('/interactions/channels'),
      api.get<{ conversations: Conversation[] }>('/interactions/conversations'),
    ])
    channels.value = ch.channels
    conversations.value = convs.conversations
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load interactions'
  } finally {
    loading.value = false
  }
}

async function open(c: Conversation) {
  try {
    selected.value = await api.get<Conversation>(`/interactions/conversations/${c.id}`)
    closeOutcome.value = ''
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load conversation'
  }
}

async function send(role: 'user' | 'human_agent') {
  if (!selected.value || !sendText.value.trim() || sendBusy.value) return
  sendBusy.value = true
  try {
    await api.post(`/interactions/conversations/${selected.value.id}/messages`, {
      text: sendText.value.trim(), role,
    })
    sendText.value = ''
    await open(selected.value)
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Send failed'
  } finally {
    sendBusy.value = false
  }
}

async function closeConv() {
  if (!selected.value) return
  try {
    await api.post(`/interactions/conversations/${selected.value.id}/close`, { outcome: closeOutcome.value })
    await open(selected.value)
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Close failed'
  }
}

async function bindHandler(ev: Event) {
  if (!selected.value) return
  const value = (ev.target as HTMLSelectElement).value || null
  try {
    await api.post(`/interactions/conversations/${selected.value.id}/handler`, { workflow_id: value })
    await open(selected.value)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Bind failed'
  }
}

async function simulate() {
  if (!simText.value.trim() || simBusy.value) return
  simBusy.value = true
  simResult.value = null
  try {
    const res = await api.post<any>('/interactions/inbound', {
      channel: simChannel.value,
      sender_id: simSender.value.trim() || `sim-${Date.now()}`,
      sender_name: simSender.value.trim() || 'Simulated user',
      text: simText.value.trim(),
      conversation_ref: selected.value?.id || undefined,
    })
    simResult.value = { reply: res.reply, conversation_id: res.conversation_id }
    simText.value = ''
    await load()
    const fresh = conversations.value.find(c => c.id === res.conversation_id)
    if (fresh) await open(fresh)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Inbound failed'
  } finally {
    simBusy.value = false
  }
}

onMounted(async () => {
  await load()
  try {
    workflows.value = await api.get<any[]>('/workflows')
  } catch { workflows.value = [] }
})
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 py-8 lg:px-8">
    <div class="mb-6 flex flex-wrap items-center gap-3">
      <div class="flex h-10 w-10 items-center justify-center rounded-xl bg-cyan-500/15">
        <MessagesSquare class="h-5 w-5 text-cyan-400" />
      </div>
      <div class="min-w-0 flex-1">
        <h1 class="text-xl font-bold text-zinc-100">Interactions</h1>
        <p class="text-xs text-zinc-500">One conversation layer under every channel - voice, WhatsApp, Telegram, Discord, web, app, SMS, email are adapters, not silos.</p>
      </div>
    </div>

    <p v-if="pageError" class="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ pageError }}</p>

    <!-- channel adapter matrix -->
    <div class="mb-6 rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
      <p class="mb-2 text-[11px] font-bold uppercase tracking-wide text-zinc-400">Channel adapters</p>
      <div class="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <button
          v-for="c in channels"
          :key="c.id"
          class="rounded-xl border px-3 py-2.5 text-left transition-colors"
          :class="filterChannel === c.id ? 'border-cyan-500/60 bg-cyan-500/10' : 'border-zinc-800 bg-zinc-950/60 hover:border-zinc-700'"
          :title="c.description"
          @click="filterChannel = filterChannel === c.id ? '' : c.id; simChannel = c.id"
        >
          <div class="flex items-center gap-1.5">
            <component :is="channelIcon[c.id] || Globe" class="h-3.5 w-3.5" :class="c.builtin ? 'text-emerald-400' : 'text-cyan-400'" />
            <span class="text-[11px] font-bold text-zinc-200">{{ c.label }}</span>
            <span class="ml-auto rounded-full bg-zinc-800 px-1.5 py-0.5 text-[9px] text-zinc-500">{{ c.conversations }}</span>
          </div>
          <p class="mt-1 text-[9px] leading-snug text-zinc-600">
            {{ c.builtin ? 'built-in · end-to-end' : c.providers.slice(0, 2).join(' / ') }}
          </p>
        </button>
      </div>
      <p class="mt-2 flex items-start gap-1.5 text-[10px] leading-relaxed text-zinc-600">
        <Info class="mt-0.5 h-3 w-3 shrink-0" />
        Built-in channels deliver end-to-end in-platform. External channels ride provider adapters
        (Twilio, Meta Cloud API, Telegram Bot API, …): the adapter translates provider events into the
        universal ingress and delivers outbound replies - the conversation layer never changes.
      </p>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16 text-zinc-500">
      <Loader2 class="h-5 w-5 animate-spin" />
    </div>

    <div v-else class="grid gap-4 lg:grid-cols-5">
      <!-- conversation list + simulator -->
      <div class="lg:col-span-2">
        <div class="mb-3 rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
          <p class="mb-2 text-[11px] font-bold uppercase tracking-wide text-zinc-400">Simulate an inbound message</p>
          <div class="mb-2 grid grid-cols-2 gap-2">
            <select
              v-model="simChannel"
              class="rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-2 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
            >
              <option v-for="c in channels" :key="c.id" :value="c.id">{{ c.label }}</option>
            </select>
            <input
              v-model="simSender"
              class="rounded-lg border border-zinc-700 bg-zinc-950 px-2 py-2 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
              placeholder="sender id / phone"
            />
          </div>
          <textarea
            v-model="simText"
            rows="2"
            class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2.5 py-2 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
            :placeholder="selected ? `arrives in the open conversation on ${simChannel}` : 'starts a new conversation'"
            @keydown.enter.exact.prevent="simulate"
          />
          <button
            class="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg bg-cyan-600 px-3 py-2 text-[11px] font-semibold text-white hover:bg-cyan-500 disabled:opacity-50"
            :disabled="simBusy || !simText.trim()"
            @click="simulate"
          >
            <Loader2 v-if="simBusy" class="h-3 w-3 animate-spin" />
            <Send v-else class="h-3 w-3" /> Post inbound
          </button>
          <div v-if="simResult" class="mt-2 rounded-lg border border-cyan-500/30 bg-cyan-500/5 px-2.5 py-2">
            <p class="text-[9px] font-bold uppercase text-cyan-300">agent reply · conversation {{ simResult.conversation_id.slice(0, 8) }}</p>
            <p class="mt-1 text-[11px] text-zinc-300">{{ simResult.reply || '(no handler bound - reply empty)' }}</p>
          </div>
        </div>

        <div class="space-y-2">
          <button
            v-for="c in visibleConversations"
            :key="c.id"
            class="w-full rounded-xl border p-3 text-left transition-colors"
            :class="selected?.id === c.id ? 'border-cyan-500/60 bg-cyan-500/5' : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-700'"
            @click="open(c)"
          >
            <div class="flex flex-wrap items-center gap-1.5">
              <component :is="channelIcon[c.channel] || Globe" class="h-3.5 w-3.5 text-cyan-400" />
              <span class="text-xs font-bold text-zinc-200">{{ c.participant.name || c.participant.id || 'anonymous' }}</span>
              <span class="rounded-full bg-zinc-800 px-1.5 py-0.5 text-[9px] uppercase text-zinc-500">{{ c.channel }}</span>
              <span
                class="rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase"
                :class="c.state === 'open' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-700/40 text-zinc-400'"
              >{{ c.state }}</span>
              <span class="ml-auto text-[9px] text-zinc-600">{{ c.message_count }} msg</span>
            </div>
            <p v-if="c.outcome" class="mt-1 text-[10px] text-zinc-500">outcome: {{ c.outcome }}</p>
            <p v-if="(c.channels_used || []).length > 1" class="mt-0.5 text-[10px] text-cyan-400/80">
              {{ c.channels_used.join(' → ') }}
            </p>
          </button>
          <p v-if="!visibleConversations.length" class="rounded-xl border border-dashed border-zinc-800 p-6 text-center text-[11px] text-zinc-600">
            No conversations yet - post an inbound message above.
          </p>
        </div>
      </div>

      <!-- conversation thread -->
      <div class="lg:col-span-3">
        <div v-if="!selected" class="flex h-64 items-center justify-center rounded-2xl border border-dashed border-zinc-800 text-[11px] text-zinc-600">
          pick a conversation to read the transcript
        </div>
        <div v-else class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <component :is="channelIcon[selected.channel] || Globe" class="h-4 w-4 text-cyan-400" />
            <span class="text-sm font-bold text-zinc-100">{{ selected.participant.name || selected.participant.id || 'anonymous' }}</span>
            <span
              class="rounded-full px-2 py-0.5 text-[9px] font-bold uppercase"
              :class="selected.state === 'open' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-700/40 text-zinc-400'"
            >{{ selected.state }}</span>
            <span v-if="selected.handler_workflow_name" class="rounded-full bg-violet-500/15 px-2 py-0.5 text-[9px] text-violet-300">
              handler: {{ selected.handler_workflow_name }}
            </span>
            <button class="ml-auto text-zinc-500 hover:text-zinc-300" @click="selected = null"><X class="h-4 w-4" /></button>
          </div>

          <div class="mb-3 max-h-96 space-y-2 overflow-auto pr-1">
            <div
              v-for="m in selected.messages || []"
              :key="m.id"
              class="rounded-lg border px-3 py-2"
              :class="roleChip[m.role] || roleChip.system"
            >
              <div class="mb-0.5 flex items-center gap-1.5 text-[9px] font-bold uppercase tracking-wide">
                <UserRound v-if="m.role === 'user'" class="h-2.5 w-2.5" />
                <Bot v-else-if="m.role === 'agent'" class="h-2.5 w-2.5" />
                <UserCog v-else-if="m.role === 'human_agent'" class="h-2.5 w-2.5" />
                <Info v-else class="h-2.5 w-2.5" />
                {{ m.role.replace('_', ' ') }}
                <span class="font-normal normal-case text-zinc-500">· via {{ m.channel }}</span>
                <span v-if="m.created_at" class="ml-auto font-normal normal-case text-zinc-600">{{ new Date(m.created_at).toLocaleString() }}</span>
              </div>
              <p class="whitespace-pre-wrap text-[11px] leading-relaxed text-zinc-200">{{ m.text }}</p>
            </div>
          </div>

          <div v-if="selected.state === 'open'" class="space-y-2">
            <div class="flex items-center gap-2">
              <input
                v-model="sendText"
                class="min-w-0 flex-1 rounded-lg border border-zinc-700 bg-zinc-950 px-2.5 py-2 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
                placeholder="type as the customer (runs the handler) or as a human agent…"
                @keydown.enter="send('user')"
              />
              <button
                class="flex shrink-0 items-center gap-1 rounded-lg bg-cyan-600 px-3 py-2 text-[11px] font-semibold text-white hover:bg-cyan-500 disabled:opacity-50"
                :disabled="sendBusy || !sendText.trim()"
                @click="send('user')"
              >
                <Send class="h-3 w-3" /> customer
              </button>
              <button
                class="shrink-0 rounded-lg bg-amber-600/90 px-3 py-2 text-[11px] font-semibold text-white hover:bg-amber-500 disabled:opacity-50"
                :disabled="sendBusy || !sendText.trim()"
                @click="send('human_agent')"
              >
                human
              </button>
            </div>
            <div class="flex flex-wrap items-center gap-2 text-[10px] text-zinc-600">
              <select
                class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[10px] text-zinc-400 outline-none"
                :value="selected.handler_workflow_id || ''"
                @change="bindHandler"
              >
                <option value="">bind a handler workflow…</option>
                <option v-for="w in workflows" :key="w.id" :value="w.id">{{ w.name }}</option>
              </select>
              <input
                v-model="closeOutcome"
                class="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[10px] text-zinc-400 outline-none"
                placeholder="outcome (e.g. order confirmed)"
              />
              <button
                class="rounded-lg border border-zinc-700 px-2.5 py-1.5 text-[10px] font-semibold text-zinc-300 hover:bg-rose-500/20 hover:text-rose-300"
                @click="closeConv"
              >
                close conversation
              </button>
            </div>
          </div>
          <p v-else-if="selected.outcome" class="text-[11px] text-zinc-500">
            closed · outcome: <span class="text-zinc-300">{{ selected.outcome }}</span>
          </p>
        </div>
      </div>
    </div>

    <p v-if="!loading" class="mt-6 text-center text-[11px] text-zinc-600">
      The universal adapter contract: POST /api/v1/interactions/inbound with {channel, sender, text} - any provider webhook can become a py8n channel.
    </p>
  </div>
</template>
