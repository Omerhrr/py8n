<script setup lang="ts">
// v113: the ERP CONSOLE - the company backbone as a working surface.
//
// The ERP Core operator (marketplace) installs the machines and the
// ledgers; this page is the desk the company works from. Everything here
// drives the SAME primitives the rest of py8n serves - the machines are
// BusinessProcesses (advance = POST), the tables are datasets (rows API),
// the chains are the estate map (GET /processes/chains). Nothing is a
// simulation: confirming an order moves a real state machine, picking it
// lands a real stock movement, and (with Finance installed) the receivable
// opens itself on the invoice machine while the books post themselves.
import { useApi } from '~/composables/useApi'

const { api } = useApi()

type ProcessDef = {
  id: string
  name: string
  states: string[]
  definition: {
    initial?: string
    transitions?: { name: string; from: string; to: string; description?: string }[]
  }
}
type Instance = {
  id: string
  ref: string
  title: string
  state: string
  context: Record<string, any>
  due_at: string | null
  is_stuck: boolean
  is_terminal: boolean
}
type Row = Record<string, any>
type ChainLeg = {
  from_process: string
  on_state: string
  to_name: string
  resolved: boolean
  opened: number
  open_now: number
  stuck: number
}
type Chain = { name: string; head_name: string; legs: ChainLeg[] }

const loading = ref(true)
const error = ref('')

// ---- the machines + tables the console is built over ---------------------
const machines = ref<Record<string, ProcessDef>>({})
const instances = ref<Record<string, Instance[]>>({})
const tables = ref<Record<string, Row[]>>({})
const chains = ref<Chain[]>([])
type TBRow = { account: string; kind: string; debits: number; credits: number; balance: number }
type TrialBalance = {
  rows: TBRow[]
  totals: { debits: number; credits: number; balanced: boolean }
  income: { revenue: number; expenses: number; net: number }
  line_count: number
}
const trialBalance = ref<TrialBalance | null>(null)

const ORDER_MACHINE = 'Sales order lifecycle'
const STOCK_MACHINE = 'Inventory replenishment'
const CLOSE_MACHINE = 'Month-end close'
const PO_MACHINE = 'Purchase lifecycle'
const PAYROLL_MACHINE = 'Payroll lifecycle'

const dsIds = ref<Record<string, string>>({})

const hasOrders = computed(() => !!machines.value[ORDER_MACHINE])
const hasStock = computed(() => !!machines.value[STOCK_MACHINE])
const hasClose = computed(() => !!machines.value[CLOSE_MACHINE])
const hasPO = computed(() => !!machines.value[PO_MACHINE])
const hasPayroll = computed(() => !!machines.value[PAYROLL_MACHINE])
const installed = computed(() => hasOrders.value || hasStock.value || hasClose.value)

// ---- tabs -----------------------------------------------------------------
const tab = ref('overview')
const tabs = computed(() => [
  { key: 'overview', label: 'Overview' },
  { key: 'orders', label: 'Order desk', show: hasOrders.value },
  { key: 'stock', label: 'Stock room', show: hasStock.value },
  { key: 'purchasing', label: 'Purchasing' },
  { key: 'people', label: 'People', show: hasPayroll.value },
  { key: 'ledger', label: 'Ledger' },
  { key: 'close', label: 'Close', show: hasClose.value },
].filter(t => t.show !== false))

// ---- derived views --------------------------------------------------------
const STATE_COLORS: Record<string, string> = {
  draft: 'bg-zinc-700/60 text-zinc-300',
  confirmed: 'bg-sky-500/15 text-sky-400',
  picked: 'bg-amber-500/15 text-amber-400',
  shipped: 'bg-violet-500/15 text-violet-400',
  invoiced: 'bg-cyan-500/15 text-cyan-400',
  paid: 'bg-emerald-500/15 text-emerald-400',
  cancelled: 'bg-rose-500/15 text-rose-400',
  healthy: 'bg-emerald-500/15 text-emerald-400',
  low: 'bg-amber-500/15 text-amber-400',
  reorder_placed: 'bg-sky-500/15 text-sky-400',
  replenished: 'bg-violet-500/15 text-violet-400',
  open: 'bg-sky-500/15 text-sky-400',
  reconciling: 'bg-amber-500/15 text-amber-400',
  reviewed: 'bg-violet-500/15 text-violet-400',
  closed: 'bg-emerald-500/15 text-emerald-400',
  calculated: 'bg-amber-500/15 text-amber-400',
  received: 'bg-zinc-700/60 text-zinc-300',
  matched: 'bg-sky-500/15 text-sky-400',
  approved: 'bg-violet-500/15 text-violet-400',
  scheduled: 'bg-cyan-500/15 text-cyan-400',
  ordered: 'bg-sky-500/15 text-sky-400',
  delivered: 'bg-emerald-500/15 text-emerald-400',
}

function stateClass(state: string): string {
  return STATE_COLORS[state] || 'bg-zinc-700/60 text-zinc-300'
}

function allowedFrom(machine: ProcessDef, state: string) {
  return (machine.definition?.transitions || [])
    .filter(t => t.from === state && t.name !== 'escalate')
}

const orderStates = ['draft', 'confirmed', 'picked', 'shipped', 'invoiced', 'paid', 'cancelled']

const openOrders = computed(() =>
  (instances.value[ORDER_MACHINE] || []).filter(i => !i.is_terminal))
const lowSkus = computed(() =>
  (instances.value[STOCK_MACHINE] || []).filter(i => i.state === 'low'))
const openReceivables = computed(() =>
  (instances.value['Invoice lifecycle'] || []).filter(i => !i.is_terminal))
const glLines = computed(() => tables.value['GL entries'] || [])
const stockMoves = computed(() => tables.value['Stock movements'] || [])
const employees = computed(() => tables.value['Employees'] || [])
const payrollRuns = computed(() =>
  (instances.value[PAYROLL_MACHINE] || []).filter(i => !i.is_terminal))
const monthlyPayroll = computed(() =>
  employees.value
    .filter(e => String(e.status || '') === 'active')
    .reduce((sum, e) => sum + (parseFloat(e.salary) || 0), 0)
    .toFixed(2))

const onHand = computed(() => {
  // seed stock + the appended deltas - the movements ledger is the truth
  const deltas: Record<string, number> = {}
  for (const m of stockMoves.value) {
    const sku = String(m.sku || '')
    deltas[sku] = (deltas[sku] || 0) + (parseFloat(m.delta) || 0)
  }
  return deltas
})

const kpis = computed(() => [
  { label: 'Open orders', value: openOrders.value.length,
    note: `${(instances.value[ORDER_MACHINE] || []).length} tracked` },
  { label: 'Low stock', value: lowSkus.value.length,
    note: 'SKUs below reorder point' },
  { label: 'Open receivables', value: openReceivables.value.length,
    note: openReceivables.value.length || hasPO.value ? 'Invoice lifecycle' : 'install Finance' },
  { label: 'Net income', value: trialBalance.value?.income
      ? trialBalance.value.income.net.toFixed(2) : '-',
    note: trialBalance.value?.totals?.balanced === false
      ? 'the books DO NOT balance' : 'revenue - expenses, off the books' },
])

// the console's two chains - the walks the ERP starts
const erpChains = computed(() =>
  chains.value.filter(c =>
    c.head_name === ORDER_MACHINE || c.head_name === STOCK_MACHINE))

// ---- actions ---------------------------------------------------------------
const busy = ref('')
const actionError = ref('')
const flash = ref('')

async function advance(machineName: string, inst: Instance, transition: string) {
  const m = machines.value[machineName]
  if (!m) return
  busy.value = `${inst.id}:${transition}`
  actionError.value = ''
  flash.value = ''
  try {
    await api.post(`/processes/${m.id}/instances/${inst.id}/advance`,
      { transition, actor: 'erp-console' })
    flash.value = `${inst.ref}: ${transition} -> done`
    await refresh()
  } catch (e: any) {
    actionError.value = e?.data?.detail || e?.message || 'advance refused'
  } finally {
    busy.value = ''
  }
}

// ---- new order -------------------------------------------------------------
const showNewOrder = ref(false)
const newOrder = ref({ order: '', customer: '', sku: '', qty: '1', total: '' })
const newOrderError = ref('')

async function placeOrder() {
  newOrderError.value = ''
  const o = newOrder.value
  if (!o.order.trim() || !o.customer.trim()) {
    newOrderError.value = 'the order needs a ref and a customer'
    return
  }
  try {
    const dsId = dsIds.value['Sales orders']
    if (dsId) {
      await api.post(`/datasets/${dsId}/rows`, {
        rows: [{ order: o.order.trim(), customer: o.customer.trim(),
                 sku: o.sku.trim(), qty: o.qty, total: o.total.trim(),
                 status: 'draft' }],
      })
    }
    const m = machines.value[ORDER_MACHINE]
    if (m) {
      await api.post(`/processes/${m.id}/instances`, {
        ref: o.order.trim(),
        title: `${o.order.trim()} - ${o.customer.trim()}`,
        context: { order: o.order.trim(), customer: o.customer.trim(),
                   sku: o.sku.trim(), qty: o.qty, total: o.total.trim(),
                   status: 'draft' },
      })
    }
    showNewOrder.value = false
    newOrder.value = { order: '', customer: '', sku: '', qty: '1', total: '' }
    flash.value = `${o.order.trim()} placed on the desk`
    await refresh()
  } catch (e: any) {
    newOrderError.value = e?.data?.detail || e?.message || 'the desk refused the order'
  }
}

// ---- open a close period ----------------------------------------------------
const showNewPeriod = ref(false)
const newPeriod = ref({ ref: '', title: '' })
const newPeriodError = ref('')

async function openPeriod() {
  newPeriodError.value = ''
  const p = newPeriod.value
  if (!p.ref.trim()) {
    newPeriodError.value = 'the period needs a ref (e.g. 2026-08)'
    return
  }
  try {
    const m = machines.value[CLOSE_MACHINE]
    if (m) {
      await api.post(`/processes/${m.id}/instances`, {
        ref: p.ref.trim(),
        title: p.title.trim() || `Close ${p.ref.trim()}`,
        context: { period: p.ref.trim() },
      })
    }
    showNewPeriod.value = false
    newPeriod.value = { ref: '', title: '' }
    flash.value = `${p.ref.trim()} open - the door watches the deadline`
    await refresh()
  } catch (e: any) {
    newPeriodError.value = e?.data?.detail || e?.message || 'the period was refused'
  }
}

// ---- run payroll -------------------------------------------------------------
const showNewRun = ref(false)
const newRun = ref({ ref: '', period: '', gross: '', headcount: '' })
const newRunError = ref('')

async function runPayroll() {
  newRunError.value = ''
  const p = newRun.value
  if (!p.ref.trim() || !p.period.trim()) {
    newRunError.value = 'the run needs a ref and a period'
    return
  }
  try {
    const dsId = dsIds.value['Payroll runs']
    if (dsId) {
      await api.post(`/datasets/${dsId}/rows`, {
        rows: [{ ref: p.ref.trim(), period: p.period.trim(),
                 gross: p.gross.trim(), headcount: p.headcount.trim(),
                 status: 'draft' }],
      })
    }
    const m = machines.value[PAYROLL_MACHINE]
    if (m) {
      await api.post(`/processes/${m.id}/instances`, {
        ref: p.ref.trim(),
        title: `Payroll ${p.ref.trim()} - ${p.period.trim()}`,
        context: { ref: p.ref.trim(), period: p.period.trim(),
                   gross: p.gross.trim(), headcount: p.headcount.trim(),
                   status: 'draft' },
      })
    }
    showNewRun.value = false
    newRun.value = { ref: '', period: '', gross: '', headcount: '' }
    flash.value = `${p.ref.trim()} on the payroll machine - the books see it when it pays`
    await refresh()
  } catch (e: any) {
    newRunError.value = e?.data?.detail || e?.message || 'the run was refused'
  }
}

// ---- loading ----------------------------------------------------------------
async function refresh() {
  error.value = ''
  try {
    const procs = await api.get<{ processes: ProcessDef[] }>('/processes')
    const want = [ORDER_MACHINE, STOCK_MACHINE, CLOSE_MACHINE, PO_MACHINE,
                  PAYROLL_MACHINE, 'Invoice lifecycle', 'Delivery pipeline']
    const found: Record<string, ProcessDef> = {}
    for (const p of procs.processes) {
      if (want.includes(p.name) && !found[p.name]) found[p.name] = p
    }
    machines.value = found

    const dsList = await api.get<{ id: string; name: string }[]>('/datasets')
    const ids: Record<string, string> = {}
    const wantDs = ['Products', 'Sales orders', 'Stock movements', 'GL entries',
                    'Employees', 'Payroll runs']
    for (const d of dsList) {
      if (wantDs.includes(d.name)) ids[d.name] = d.id
    }
    dsIds.value = ids

    const [chainsRes, ...rest] = await Promise.all([
      api.get<{ chains: Chain[] }>('/processes/chains'),
      ...Object.values(ids).map(id =>
        api.get<{ rows: Row[] }>(`/datasets/${id}/rows?limit=200`)),
      ...Object.entries(found).map(([, m]) =>
        api.get<{ instances: Instance[] }>(`/processes/${m.id}/instances?limit=500`)),
    ])
    chains.value = chainsRes.chains || []
    const dsNames = Object.keys(ids)
    const procNames = Object.keys(found)
    rest.forEach((res, i) => {
      if (i < dsNames.length) tables.value[dsNames[i]] = (res as any).rows || []
      else instances.value[procNames[i - dsNames.length]] = (res as any).instances || []
    })

    // the books speak - the trial balance over the GL entries dataset
    // (the posters keep the pairs; this proves it and reads the income)
    const glId = ids['GL entries']
    if (glId) {
      try {
        trialBalance.value = await api.get<TrialBalance>(
          `/erp/trial-balance?dataset_id=${encodeURIComponent(glId)}`)
      } catch { trialBalance.value = null }
    } else {
      trialBalance.value = null
    }
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'the console could not reach the books'
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

useHead({ title: 'ERP - Py8n' })
</script>

<template>
  <div class="mx-auto max-w-7xl px-4 py-6 lg:px-8">
    <!-- header -->
    <div class="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <span class="flex h-8 w-8 items-center justify-center rounded-lg bg-amber-500/15 text-amber-400">
            <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M2 22h20M3 22V9l6 4V9l6 4V4l6 4v14" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </span>
          ERP
        </h1>
        <p class="mt-1 text-sm text-zinc-500">
          The company backbone - the order desk, the stock room, the people
          and the books, run on py8n's machines, datasets and chains.
        </p>
      </div>
      <button
        class="rounded-xl border border-zinc-800 bg-zinc-900/60 px-3 py-1.5 text-xs font-medium text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200"
        @click="refresh()"
      >Refresh</button>
    </div>

    <!-- not installed -->
    <div
      v-if="!loading && !installed"
      class="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-6 text-center"
    >
      <p class="text-sm font-medium text-amber-400">The ERP core is not installed yet.</p>
      <p class="mx-auto mt-1 max-w-md text-xs leading-relaxed text-zinc-500">
        One click on the marketplace installs the whole backbone: the order
        desk and stock room machines, the ledgers, the clerk - and the
        journeys that hand shipped orders to Finance's receivables and stock
        dips to Procurement's purchase orders.
      </p>
      <NuxtLink
        to="/marketplace/erp-operator"
        class="mt-4 inline-block rounded-xl bg-amber-500 px-4 py-2 text-xs font-semibold text-zinc-950 transition hover:bg-amber-400"
      >Install ERP Core</NuxtLink>
    </div>

    <div v-else>
      <!-- KPIs -->
      <div class="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div
          v-for="k in kpis" :key="k.label"
          class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4"
        >
          <p class="text-[11px] font-medium uppercase tracking-wide text-zinc-500">{{ k.label }}</p>
          <p class="mt-1 text-2xl font-bold tabular-nums">{{ k.value }}</p>
          <p class="mt-0.5 text-[10px] text-zinc-600">{{ k.note }}</p>
        </div>
      </div>

      <!-- tabs -->
      <div class="mb-4 flex gap-1 overflow-x-auto rounded-xl border border-zinc-800 bg-zinc-900/50 p-1">
        <button
          v-for="t in tabs" :key="t.key"
          class="whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium transition"
          :class="tab === t.key ? 'bg-amber-500/15 text-amber-400' : 'text-zinc-400 hover:text-zinc-200'"
          @click="tab = t.key"
        >{{ t.label }}</button>
      </div>

      <p v-if="error" class="mb-4 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-2 text-xs text-rose-300">{{ error }}</p>
      <p v-if="actionError" class="mb-4 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-2 text-xs text-rose-300">{{ actionError }}</p>
      <p v-if="flash" class="mb-4 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-xs text-emerald-300">{{ flash }}</p>

      <!-- ============ OVERVIEW ============ -->
      <div v-if="tab === 'overview'" class="space-y-6">
        <section
          v-for="c in erpChains" :key="c.name"
          class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5"
        >
          <h2 class="text-sm font-semibold">{{ c.name }}</h2>
          <div class="mt-3 flex flex-wrap items-center gap-2">
            <template v-for="(leg, li) in c.legs" :key="li">
              <div
                class="rounded-xl px-3 py-2 text-center"
                :class="leg.resolved ? 'bg-zinc-800/80' : 'border border-dashed border-zinc-700 bg-transparent'"
              >
                <p class="text-xs font-semibold" :class="leg.resolved ? 'text-zinc-200' : 'text-zinc-500'">
                  {{ leg.from_process }}
                </p>
                <p class="mt-0.5 text-[10px] text-amber-400">on {{ leg.on_state }}</p>
                <p class="mt-1 text-[10px] text-zinc-500">
                  {{ leg.opened }} opened · {{ leg.open_now }} moving
                  <template v-if="leg.stuck"> · <span class="text-rose-400">{{ leg.stuck }} stuck</span></template>
                </p>
              </div>
              <svg class="h-4 w-4 shrink-0 text-zinc-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M5 12h14m-6-6 6 6-6 6" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <div
                class="rounded-xl px-3 py-2 text-center"
                :class="leg.resolved ? 'bg-zinc-800/80' : 'border border-dashed border-zinc-700 bg-transparent'"
              >
                <p class="text-xs font-semibold" :class="leg.resolved ? 'text-zinc-200' : 'text-zinc-500'">{{ leg.to_name }}</p>
                <p v-if="!leg.resolved" class="mt-0.5 text-[10px] text-zinc-600">pending - install the operator</p>
                <p v-else class="mt-0.5 text-[10px] text-zinc-500">the leg opens it</p>
              </div>
            </template>
          </div>
        </section>

        <section class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <h2 class="text-sm font-semibold">The books, live</h2>
          <p class="mt-0.5 text-xs text-zinc-500">GL lines the ledger workflow posted - the trail behind the numbers.</p>
          <div v-if="!glLines.length" class="mt-3 rounded-xl border border-dashed border-zinc-700 px-4 py-6 text-center text-xs text-zinc-600">
            Nothing posted yet - move an order on the desk (with the system booted and workflows active).
          </div>
          <table v-else class="mt-3 w-full text-left text-xs">
            <thead class="text-zinc-500">
              <tr>
                <th class="pb-2 font-medium">ref</th><th class="pb-2 font-medium">account</th>
                <th class="pb-2 font-medium text-right">debit</th><th class="pb-2 font-medium text-right">credit</th>
                <th class="pb-2 font-medium">memo</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-zinc-800/60">
              <tr v-for="(r, i) in glLines" :key="i">
                <td class="py-1.5 font-mono text-zinc-300">{{ r.ref }}</td>
                <td class="py-1.5 text-zinc-400">{{ r.account }}</td>
                <td class="py-1.5 text-right tabular-nums text-emerald-400">{{ r.debit }}</td>
                <td class="py-1.5 text-right tabular-nums text-sky-400">{{ r.credit }}</td>
                <td class="py-1.5 text-zinc-500">{{ r.memo }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>

      <!-- ============ ORDER DESK ============ -->
      <div v-else-if="tab === 'orders' && hasOrders" class="space-y-4">
        <div class="flex justify-end">
          <button
            class="rounded-xl bg-amber-500 px-4 py-2 text-xs font-semibold text-zinc-950 transition hover:bg-amber-400"
            @click="showNewOrder = !showNewOrder"
          >{{ showNewOrder ? 'Cancel' : 'New order' }}</button>
        </div>
        <form
          v-if="showNewOrder"
          class="grid grid-cols-2 gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4 lg:grid-cols-6"
          @submit.prevent="placeOrder"
        >
          <input v-model="newOrder.order" placeholder="SO-1050" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newOrder.customer" placeholder="customer" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newOrder.sku" placeholder="SKU-1001" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newOrder.qty" placeholder="qty" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newOrder.total" placeholder="total" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <button class="rounded-lg bg-amber-500 px-3 py-2 text-xs font-semibold text-zinc-950 hover:bg-amber-400">Place</button>
          <p v-if="newOrderError" class="col-span-full text-xs text-rose-400">{{ newOrderError }}</p>
        </form>

        <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          <section
            v-for="st in orderStates" :key="st"
            class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-3"
          >
            <div class="mb-2 flex items-center justify-between">
              <span class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide" :class="stateClass(st)">{{ st }}</span>
              <span class="text-[10px] text-zinc-600">{{ (instances[ORDER_MACHINE] || []).filter(i => i.state === st).length }}</span>
            </div>
            <div class="space-y-2">
              <div
                v-for="i in (instances[ORDER_MACHINE] || []).filter(x => x.state === st)"
                :key="i.id"
                class="rounded-xl border border-zinc-800 bg-zinc-950/60 p-2.5"
              >
                <p class="truncate text-xs font-medium text-zinc-200">{{ i.title || i.ref }}</p>
                <p class="mt-0.5 text-[10px] text-zinc-500">
                  {{ i.ref }}
                  <template v-if="i.context?.total"> · {{ i.context.total }}</template>
                  <span v-if="i.is_stuck" class="text-rose-400"> · past SLA</span>
                </p>
                <div v-if="allowedFrom(machines[ORDER_MACHINE], i.state).length" class="mt-2 flex flex-wrap gap-1">
                  <button
                    v-for="t in allowedFrom(machines[ORDER_MACHINE], i.state)" :key="t.name"
                    class="rounded-md border border-zinc-700 px-2 py-1 text-[10px] font-medium text-zinc-300 transition hover:border-amber-500 hover:text-amber-400 disabled:opacity-40"
                    :disabled="busy === `${i.id}:${t.name}`"
                    @click="advance(ORDER_MACHINE, i, t.name)"
                  >{{ t.name }}</button>
                </div>
              </div>
              <p v-if="!(instances[ORDER_MACHINE] || []).some(x => x.state === st)" class="px-1 py-2 text-[10px] text-zinc-700">-</p>
            </div>
          </section>
        </div>
      </div>

      <!-- ============ STOCK ROOM ============ -->
      <div v-else-if="tab === 'stock' && hasStock" class="space-y-4">
        <div class="overflow-x-auto rounded-2xl border border-zinc-800 bg-zinc-900/50">
          <table class="w-full text-left text-xs">
            <thead class="border-b border-zinc-800 text-zinc-500">
              <tr>
                <th class="px-4 py-3 font-medium">SKU</th><th class="px-4 py-3 font-medium">product</th>
                <th class="px-4 py-3 font-medium text-right">seed stock</th>
                <th class="px-4 py-3 font-medium text-right">picked since</th>
                <th class="px-4 py-3 font-medium text-right">reorder at</th>
                <th class="px-4 py-3 font-medium">machine</th>
                <th class="px-4 py-3 font-medium">moves</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-zinc-800/60">
              <tr v-for="i in (instances[STOCK_MACHINE] || [])" :key="i.id">
                <td class="px-4 py-3 font-mono text-zinc-300">{{ i.ref }}</td>
                <td class="px-4 py-3 text-zinc-400">{{ (i.title || '').split(' - ').slice(1).join(' - ') }}</td>
                <td class="px-4 py-3 text-right tabular-nums text-zinc-400">{{ i.context?.stock }}</td>
                <td class="px-4 py-3 text-right tabular-nums text-amber-400">{{ onHand[i.ref] || 0 }}</td>
                <td class="px-4 py-3 text-right tabular-nums text-zinc-500">{{ i.context?.reorder_point }}</td>
                <td class="px-4 py-3">
                  <span class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase" :class="stateClass(i.state)">{{ i.state }}</span>
                </td>
                <td class="px-4 py-3">
                  <div class="flex flex-wrap gap-1">
                    <button
                      v-for="t in allowedFrom(machines[STOCK_MACHINE], i.state)" :key="t.name"
                      class="rounded-md border border-zinc-700 px-2 py-1 text-[10px] font-medium text-zinc-300 transition hover:border-amber-500 hover:text-amber-400 disabled:opacity-40"
                      :disabled="busy === `${i.id}:${t.name}`"
                      @click="advance(STOCK_MACHINE, i, t.name)"
                    >{{ t.name }}</button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <section v-if="stockMoves.length" class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <h2 class="text-sm font-semibold">Stock movements</h2>
          <table class="mt-3 w-full text-left text-xs">
            <thead class="text-zinc-500">
              <tr><th class="pb-2 font-medium">sku</th><th class="pb-2 font-medium text-right">delta</th><th class="pb-2 font-medium">reason</th></tr>
            </thead>
            <tbody class="divide-y divide-zinc-800/60">
              <tr v-for="(m, i) in stockMoves" :key="i">
                <td class="py-1.5 font-mono text-zinc-300">{{ m.sku }}</td>
                <td class="py-1.5 text-right tabular-nums text-amber-400">{{ m.delta }}</td>
                <td class="py-1.5 text-zinc-500">{{ m.reason }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>

      <!-- ============ PURCHASING ============ -->
      <div v-else-if="tab === 'purchasing'" class="space-y-4">
        <div v-if="!hasPO" class="rounded-2xl border border-dashed border-zinc-700 p-6 text-center">
          <p class="text-sm font-medium text-zinc-400">Procurement is not installed.</p>
          <p class="mx-auto mt-1 max-w-md text-xs text-zinc-600">
            Reorder a low SKU on the Stock room tab - the journey will skip
            honestly naming the missing machine until the Procurement operator
            is in. Install it and the purchase orders open themselves.
          </p>
          <NuxtLink to="/marketplace/procurement-operator" class="mt-3 inline-block rounded-xl border border-zinc-700 px-4 py-2 text-xs font-medium text-zinc-300 hover:border-amber-500 hover:text-amber-400">
            Install Procurement
          </NuxtLink>
        </div>
        <template v-else>
          <p class="text-xs text-zinc-500">
            Purchase orders opened by reorders (and the desk) - the Supply
            chain walks them to the delivery and the vendor's bill.
          </p>
          <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            <div
              v-for="i in (instances[PO_MACHINE] || [])"
              :key="i.id"
              class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4"
            >
              <div class="flex items-center justify-between gap-2">
                <p class="truncate text-xs font-medium text-zinc-200">{{ i.title || i.ref }}</p>
                <span class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase" :class="stateClass(i.state)">{{ i.state }}</span>
              </div>
              <p class="mt-0.5 text-[10px] text-zinc-500">
                {{ i.ref }}
                <template v-if="i.context?.journey"> · opened by {{ i.context.journey.from_process }}</template>
                <span v-if="i.is_stuck" class="text-rose-400"> · past SLA</span>
              </p>
              <div v-if="allowedFrom(machines[PO_MACHINE], i.state).length" class="mt-3 flex flex-wrap gap-1">
                <button
                  v-for="t in allowedFrom(machines[PO_MACHINE], i.state)" :key="t.name"
                  class="rounded-md border border-zinc-700 px-2 py-1 text-[10px] font-medium text-zinc-300 transition hover:border-amber-500 hover:text-amber-400 disabled:opacity-40"
                  :disabled="busy === `${i.id}:${t.name}`"
                  @click="advance(PO_MACHINE, i, t.name)"
                >{{ t.name }}</button>
              </div>
            </div>
            <p v-if="!(instances[PO_MACHINE] || []).length" class="rounded-2xl border border-dashed border-zinc-700 px-4 py-6 text-center text-xs text-zinc-600 md:col-span-2 xl:col-span-3">
              No open purchase orders - reorder a low SKU and the machine opens one.
            </p>
          </div>
        </template>
      </div>

      <!-- ============ PEOPLE ============ -->
      <div v-else-if="tab === 'people' && hasPayroll" class="space-y-4">
        <div class="flex justify-end">
          <button
            class="rounded-xl bg-amber-500 px-4 py-2 text-xs font-semibold text-zinc-950 transition hover:bg-amber-400"
            @click="showNewRun = !showNewRun"
          >{{ showNewRun ? 'Cancel' : 'Run payroll' }}</button>
        </div>
        <form
          v-if="showNewRun"
          class="grid grid-cols-2 gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4 lg:grid-cols-5"
          @submit.prevent="runPayroll"
        >
          <input v-model="newRun.ref" placeholder="PR-2026-08" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newRun.period" placeholder="period (2026-08)" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newRun.gross" placeholder="gross" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newRun.headcount" placeholder="headcount" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <button class="rounded-lg bg-amber-500 px-3 py-2 text-xs font-semibold text-zinc-950 hover:bg-amber-400">Run</button>
          <p v-if="newRunError" class="col-span-full text-xs text-rose-400">{{ newRunError }}</p>
        </form>

        <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
          <div
            v-for="i in (instances[PAYROLL_MACHINE] || [])"
            :key="i.id"
            class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4"
          >
            <div class="flex items-center justify-between gap-2">
              <p class="truncate text-xs font-medium text-zinc-200">{{ i.title || i.ref }}</p>
              <span class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase" :class="stateClass(i.state)">{{ i.state }}</span>
            </div>
            <p class="mt-0.5 text-[10px] text-zinc-500">
              {{ i.ref }}
              <template v-if="i.context?.period"> · period {{ i.context.period }}</template>
              <template v-if="i.context?.gross"> · gross {{ i.context.gross }}</template>
              <template v-if="i.context?.headcount"> · {{ i.context.headcount }} people</template>
              <span v-if="i.is_stuck" class="text-rose-400"> · past SLA</span>
            </p>
            <div v-if="allowedFrom(machines[PAYROLL_MACHINE], i.state).length" class="mt-3 flex flex-wrap gap-1">
              <button
                v-for="t in allowedFrom(machines[PAYROLL_MACHINE], i.state)" :key="t.name"
                class="rounded-md border border-zinc-700 px-2 py-1 text-[10px] font-medium text-zinc-300 transition hover:border-amber-500 hover:text-amber-400 disabled:opacity-40"
                :disabled="busy === `${i.id}:${t.name}`"
                @click="advance(PAYROLL_MACHINE, i, t.name)"
              >{{ t.name }}</button>
            </div>
          </div>
          <p v-if="!(instances[PAYROLL_MACHINE] || []).length" class="rounded-2xl border border-dashed border-zinc-700 px-4 py-6 text-center text-xs text-zinc-600 md:col-span-2 xl:col-span-4">
            The payroll calendar is empty - run payroll when the period ends.
          </p>
        </div>

        <section class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <div class="flex items-baseline justify-between">
            <h2 class="text-sm font-semibold">Employees</h2>
            <p class="text-[10px] text-zinc-500">active monthly gross: <span class="tabular-nums text-zinc-300">{{ monthlyPayroll }}</span></p>
          </div>
          <table class="mt-3 w-full text-left text-xs">
            <thead class="text-zinc-500">
              <tr>
                <th class="pb-2 font-medium">emp</th><th class="pb-2 font-medium">name</th>
                <th class="pb-2 font-medium">role</th>
                <th class="pb-2 font-medium text-right">salary</th><th class="pb-2 font-medium">status</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-zinc-800/60">
              <tr v-for="e in employees" :key="String(e.emp)">
                <td class="py-1.5 font-mono text-zinc-300">{{ e.emp }}</td>
                <td class="py-1.5 text-zinc-400">{{ e.name }}</td>
                <td class="py-1.5 text-zinc-500">{{ e.role }}</td>
                <td class="py-1.5 text-right tabular-nums text-zinc-300">{{ e.salary }}</td>
                <td class="py-1.5">
                  <span class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase" :class="String(e.status) === 'active' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-400'">{{ e.status }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>

      <!-- ============ LEDGER ============ -->
      <div v-else-if="tab === 'ledger'" class="space-y-4">
        <section v-if="trialBalance" class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-sm font-semibold">Trial balance</h2>
            <span
              class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase"
              :class="trialBalance.totals.balanced ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
            >{{ trialBalance.totals.balanced ? 'the books balance' : 'DO NOT balance' }}</span>
          </div>
          <p class="mt-0.5 text-xs text-zinc-500">
            {{ trialBalance.line_count }} journal lines over {{ trialBalance.rows.length }} accounts -
            revenue {{ trialBalance.income.revenue.toFixed(2) }} · expenses {{ trialBalance.income.expenses.toFixed(2) }} ·
            net {{ trialBalance.income.net.toFixed(2) }}
          </p>
          <table class="mt-3 w-full text-left text-xs">
            <thead class="text-zinc-500">
              <tr>
                <th class="pb-2 font-medium">account</th><th class="pb-2 font-medium">kind</th>
                <th class="pb-2 font-medium text-right">debits</th><th class="pb-2 font-medium text-right">credits</th>
                <th class="pb-2 font-medium text-right">balance</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-zinc-800/60">
              <tr v-for="r in trialBalance.rows" :key="r.account">
                <td class="py-1.5 text-zinc-300">{{ r.account }}</td>
                <td class="py-1.5 text-zinc-600">{{ r.kind }}</td>
                <td class="py-1.5 text-right tabular-nums text-emerald-400">{{ r.debits ? r.debits.toFixed(2) : '' }}</td>
                <td class="py-1.5 text-right tabular-nums text-sky-400">{{ r.credits ? r.credits.toFixed(2) : '' }}</td>
                <td class="py-1.5 text-right tabular-nums" :class="r.balance >= 0 ? 'text-zinc-200' : 'text-rose-400'">{{ r.balance.toFixed(2) }}</td>
              </tr>
            </tbody>
            <tfoot class="border-t border-zinc-800 text-zinc-400">
              <tr>
                <td class="pt-2 font-medium" colspan="2">totals</td>
                <td class="pt-2 text-right tabular-nums text-emerald-400">{{ trialBalance.totals.debits.toFixed(2) }}</td>
                <td class="pt-2 text-right tabular-nums text-sky-400">{{ trialBalance.totals.credits.toFixed(2) }}</td>
                <td></td>
              </tr>
            </tfoot>
          </table>
        </section>
        <section class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <h2 class="text-sm font-semibold">GL entries</h2>
          <p class="mt-0.5 text-xs text-zinc-500">One journal line per move - the posters pair every debit with a credit (orders, stock, payroll).</p>
          <div v-if="!glLines.length" class="mt-3 rounded-xl border border-dashed border-zinc-700 px-4 py-6 text-center text-xs text-zinc-600">
            The books are blank - boot the ERP system (workflows active) and move an order.
          </div>
          <table v-else class="mt-3 w-full text-left text-xs">
            <thead class="text-zinc-500">
              <tr>
                <th class="pb-2 font-medium">ref</th><th class="pb-2 font-medium">account</th>
                <th class="pb-2 font-medium text-right">debit</th><th class="pb-2 font-medium text-right">credit</th>
                <th class="pb-2 font-medium">memo</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-zinc-800/60">
              <tr v-for="(r, i) in glLines" :key="i">
                <td class="py-1.5 font-mono text-zinc-300">{{ r.ref }}</td>
                <td class="py-1.5 text-zinc-400">{{ r.account }}</td>
                <td class="py-1.5 text-right tabular-nums text-emerald-400">{{ r.debit }}</td>
                <td class="py-1.5 text-right tabular-nums text-sky-400">{{ r.credit }}</td>
                <td class="py-1.5 text-zinc-500">{{ r.memo }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>

      <!-- ============ CLOSE ============ -->
      <div v-else-if="tab === 'close' && hasClose" class="space-y-4">
        <div class="flex justify-end">
          <button
            class="rounded-xl bg-amber-500 px-4 py-2 text-xs font-semibold text-zinc-950 transition hover:bg-amber-400"
            @click="showNewPeriod = !showNewPeriod"
          >{{ showNewPeriod ? 'Cancel' : 'Open a period' }}</button>
        </div>
        <form
          v-if="showNewPeriod"
          class="grid grid-cols-2 gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4 lg:grid-cols-3"
          @submit.prevent="openPeriod"
        >
          <input v-model="newPeriod.ref" placeholder="2026-08" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <input v-model="newPeriod.title" placeholder="August close" class="rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-xs outline-none focus:border-amber-500" />
          <button class="rounded-lg bg-amber-500 px-3 py-2 text-xs font-semibold text-zinc-950 hover:bg-amber-400">Open</button>
          <p v-if="newPeriodError" class="col-span-full text-xs text-rose-400">{{ newPeriodError }}</p>
        </form>
        <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          <div
            v-for="i in (instances[CLOSE_MACHINE] || [])"
            :key="i.id"
            class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4"
          >
            <div class="flex items-center justify-between gap-2">
              <p class="truncate text-xs font-medium text-zinc-200">{{ i.title || i.ref }}</p>
              <span class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase" :class="stateClass(i.state)">{{ i.state }}</span>
            </div>
            <p class="mt-0.5 text-[10px] text-zinc-500">
              {{ i.ref }}
              <span v-if="i.is_stuck" class="text-rose-400"> · past deadline</span>
            </p>
            <div v-if="allowedFrom(machines[CLOSE_MACHINE], i.state).length" class="mt-3 flex flex-wrap gap-1">
              <button
                v-for="t in allowedFrom(machines[CLOSE_MACHINE], i.state)" :key="t.name"
                class="rounded-md border border-zinc-700 px-2 py-1 text-[10px] font-medium text-zinc-300 transition hover:border-amber-500 hover:text-amber-400 disabled:opacity-40"
                :disabled="busy === `${i.id}:${t.name}`"
                @click="advance(CLOSE_MACHINE, i, t.name)"
              >{{ t.name }}</button>
            </div>
          </div>
          <p v-if="!(instances[CLOSE_MACHINE] || []).length" class="rounded-2xl border border-dashed border-zinc-700 px-4 py-6 text-center text-xs text-zinc-600 md:col-span-2 xl:col-span-3">
            The close calendar is empty - open a period when the month ends.
          </p>
        </div>
      </div>
    </div>
  </div>
</template>
