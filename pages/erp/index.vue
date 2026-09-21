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
type TBSummary = { kind: string; accounts: number; total: number }
type TBLine = { account: string; balance: number }
type Statements = {
  income: { revenue: TBLine[]; expenses: TBLine[]
            total_revenue: number; total_expenses: number; net: number }
  balance: { assets: TBLine[]; liabilities: TBLine[]; equity: TBLine[]
             memos: TBLine[]
             total_assets: number; total_liabilities: number
             total_equity: number; retained_earnings: number
             equation_side: number; balanced: boolean }
  line_count: number
}
type TrialBalance = {
  rows: TBRow[]
  totals: { debits: number; credits: number; balanced: boolean }
  income: { revenue: number; expenses: number; net: number }
  summary?: TBSummary[]
  line_count: number
}
const trialBalance = ref<TrialBalance | null>(null)
const bookStatements = ref<Statements | null>(null)

// v118: the aging, the cash flow and THE CLOSE - the books grow handles
type AgingLine = { ref: string; open: number; at: string; age_days: number | null; bucket: string }
type AgingBucket = { bucket: string; refs: number; total: number }
type AgingSide = { open_refs: number; total: number; buckets: AgingBucket[]; lines: AgingLine[] }
type Aging = { receivables: AgingSide; payables: AgingSide; as_of: string }
type CashSection = { name: string; inflows: number; outflows: number; net: number }
type CashFlow = { sections: CashSection[]; inflow_total: number; outflow_total: number
                  net_cash_movement: number; cash_accounts: string[] }
type CloseReceipt = { id: string; period: string; net: number; retained_after: number
                      entries: number; closed_at: string }
type CloseHistory = { closed: boolean; closes: CloseReceipt[] }
const bookAging = ref<Aging | null>(null)
const bookCashFlow = ref<CashFlow | null>(null)
const bookCloses = ref<CloseHistory | null>(null)
const closingBooks = ref(false)
const closeError = ref('')

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

// v116: the books get a face - kind badges + the balance chart
const KIND_COLORS: Record<string, string> = {
  asset: '#34d399', liability: '#fb7185', equity: '#a78bfa',
  revenue: '#38bdf8', expense: '#fbbf24', memo: '#8b8b94', other: '#a1a1aa',
}
const KIND_CLASSES: Record<string, string> = {
  asset: 'bg-emerald-500/15 text-emerald-400',
  liability: 'bg-rose-500/15 text-rose-400',
  equity: 'bg-violet-500/15 text-violet-400',
  revenue: 'bg-sky-500/15 text-sky-400',
  expense: 'bg-amber-500/15 text-amber-400',
  memo: 'bg-zinc-700/60 text-zinc-300',
}
function kindClass(kind: string): string {
  return KIND_CLASSES[kind] || 'bg-zinc-700/60 text-zinc-400'
}
const KIND_LABELS: Record<string, string> = {
  asset: 'assets', liability: 'liabilities', equity: 'equity',
  revenue: 'revenue', expense: 'expenses', memo: 'memos', other: 'other',
}

const tbChartOption = computed(() => {
  const tb = trialBalance.value
  if (!tb?.rows?.length) return null
  const rows = [...tb.rows].sort((a, b) => Math.abs(b.balance) - Math.abs(a.balance))
  return {
    backgroundColor: 'transparent',
    grid: { left: 8, right: 56, top: 10, bottom: 10, containLabel: true },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: '#18181b', borderColor: '#3f3f46',
      textStyle: { color: '#e4e4e7', fontSize: 11 },
      valueFormatter: (v: any) => Number(v).toFixed(2),
    },
    xAxis: {
      type: 'value',
      axisLabel: { color: '#71717a', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(63,63,70,0.35)' } },
    },
    yAxis: {
      type: 'category',
      data: rows.map(r => r.account),
      axisLabel: { color: '#a1a1aa', fontSize: 11 },
      axisLine: { lineStyle: { color: '#3f3f46' } },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 14,
      data: rows.map(r => ({
        value: r.balance,
        itemStyle: {
          color: KIND_COLORS[r.kind] || KIND_COLORS.other,
          borderRadius: [0, 3, 3, 0],
        },
      })),
      label: {
        show: true, position: 'right', color: '#d4d4d8', fontSize: 10,
        formatter: (p: any) => Number(p.value).toFixed(2),
      },
    }],
  }
})
const tbChartHeight = computed(() =>
  Math.min(80 + (trialBalance.value?.rows.length || 0) * 26, 340))

// v118: the cash flow waterfall - the sections step down (or up) from the
// running total, and the final bar is the net movement from zero
const CF_COLORS: Record<string, string> = {
  operating: '#38bdf8', investing: '#a78bfa', financing: '#fbbf24',
  other: '#a1a1aa',
}
const agingSides = [
  { key: 'receivables' as const, title: 'Receivables aging', note: 'who owes the company' },
  { key: 'payables' as const, title: 'Payables aging', note: 'who the company owes' },
]
function ageClass(bucket: string): string {
  if (bucket === 'current') return 'bg-emerald-500/15 text-emerald-400'
  if (bucket === 'd90_plus') return 'bg-rose-500/15 text-rose-400'
  if (bucket === 'undated') return 'bg-zinc-700/60 text-zinc-300'
  return 'bg-amber-500/15 text-amber-400'
}
const cfChartOption = computed(() => {
  const cf = bookCashFlow.value
  if (!cf) return null
  const steps = cf.sections.map(s => ({ name: s.name, value: s.net }))
  steps.push({ name: 'net', value: cf.net_cash_movement })
  let run = 0
  const base: number[] = []
  const bars: { value: number; itemStyle: { color: string } }[] = []
  for (const s of steps) {
    if (s.name === 'net') {
      base.push(0)
      bars.push({ value: s.value,
                  itemStyle: { color: s.value >= 0 ? '#34d399' : '#fb7185' } })
      continue
    }
    const start = run
    run = Math.round((run + s.value) * 100) / 100
    base.push(Math.min(start, run))
    bars.push({ value: Math.abs(Math.round((run - start) * 100) / 100),
                itemStyle: { color: CF_COLORS[s.name] || CF_COLORS.other } })
  }
  return {
    backgroundColor: 'transparent',
    grid: { left: 8, right: 16, top: 20, bottom: 10, containLabel: true },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: '#18181b', borderColor: '#3f3f46',
      textStyle: { color: '#e4e4e7', fontSize: 11 },
      valueFormatter: (v: any) => Number(v).toFixed(2),
    },
    xAxis: {
      type: 'category', data: steps.map(s => s.name),
      axisLabel: { color: '#a1a1aa', fontSize: 10 },
      axisLine: { lineStyle: { color: '#3f3f46' } }, axisTick: { show: false },
    },
    yAxis: {
      type: 'value', axisLabel: { color: '#71717a', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(63,63,70,0.35)' } },
    },
    series: [
      { type: 'bar', stack: 'flow', silent: true,
        itemStyle: { color: 'transparent' },
        emphasis: { itemStyle: { color: 'transparent' } }, data: base },
      { type: 'bar', stack: 'flow', barMaxWidth: 26, data: bars,
        label: { show: true, position: 'top', color: '#d4d4d8', fontSize: 10,
                 formatter: (p: any) => {
                   const s = steps[p.dataIndex]
                   return (s.value >= 0 ? '+' : '') + s.value.toFixed(2)
                 } } },
    ],
  }
})

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
// v117: the books answer back - click an account, see its journal lines
const drillAccount = ref('')
function drill(account: string) {
  drillAccount.value = drillAccount.value === account ? '' : account
}
const shownGlLines = computed(() =>
  drillAccount.value
    ? glLines.value.filter(r => String(r.account || '') === drillAccount.value)
    : glLines.value)
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
      try {
        // v117: the statements - the same book shaped into the two
        // documents a company closes with (income + balance sheet)
        bookStatements.value = await api.get<Statements>(
          `/erp/statements?dataset_id=${encodeURIComponent(glId)}`)
      } catch { bookStatements.value = null }
      try {
        // v118: the aging - who owes the company, who the company owes
        bookAging.value = await api.get<Aging>(
          `/erp/aging?dataset_id=${encodeURIComponent(glId)}`)
      } catch { bookAging.value = null }
      try {
        // v118: the cash flow - where the money moved
        bookCashFlow.value = await api.get<CashFlow>(
          `/erp/cash-flow?dataset_id=${encodeURIComponent(glId)}`)
      } catch { bookCashFlow.value = null }
      try {
        // v118: the close history - is the book locked?
        bookCloses.value = await api.get<CloseHistory>(
          `/erp/close?dataset_id=${encodeURIComponent(glId)}`)
      } catch { bookCloses.value = null }
    } else {
      trialBalance.value = null
      bookStatements.value = null
      bookAging.value = null
      bookCashFlow.value = null
      bookCloses.value = null
    }
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'the console could not reach the books'
  } finally {
    loading.value = false
  }
}

// v118: THE CLOSE - the accounting close posts the closing entries and
// locks the book; the console shows the receipt, the lock chip and the
// door's refusal (unbalanced / already closed / nothing to close)
async function closeBooks() {
  const glId = dsIds.value['GL entries']
  if (!glId || closingBooks.value) return
  closingBooks.value = true
  closeError.value = ''
  try {
    await api.post(`/erp/close?dataset_id=${encodeURIComponent(glId)}`, {})
    await refresh()
  } catch (e: any) {
    closeError.value = e?.data?.detail || e?.message || 'the close refused'
  } finally {
    closingBooks.value = false
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
          <!-- v116: the books get a face - the per-kind buckets, then the chart -->
          <div v-if="trialBalance.summary?.length" class="mt-3 flex flex-wrap gap-1.5">
            <span
              v-for="s in trialBalance.summary.filter(b => b.accounts > 0)"
              :key="s.kind"
              class="rounded-md px-2 py-0.5 text-[10px] font-semibold"
              :class="kindClass(s.kind)"
            >{{ KIND_LABELS[s.kind] || s.kind }} {{ s.total.toFixed(2) }} <span class="font-normal opacity-70">({{ s.accounts }})</span></span>
          </div>
          <ClientOnly v-if="tbChartOption">
            <EChart :option="tbChartOption" :height="tbChartHeight" class="mt-2" />
          </ClientOnly>
          <table class="mt-3 w-full text-left text-xs">
            <thead class="text-zinc-500">
              <tr>
                <th class="pb-2 font-medium">account</th><th class="pb-2 font-medium">kind</th>
                <th class="pb-2 font-medium text-right">debits</th><th class="pb-2 font-medium text-right">credits</th>
                <th class="pb-2 font-medium text-right">balance</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-zinc-800/60">
              <tr
                v-for="r in trialBalance.rows" :key="r.account"
                class="cursor-pointer transition hover:bg-zinc-800/40"
                :class="drillAccount === r.account && 'bg-amber-500/10'"
                :title="`see the journal lines on ${r.account}`"
                @click="drill(r.account)"
              >
                <td class="py-1.5 text-zinc-300">{{ r.account }}</td>
                <td class="py-1.5"><span class="rounded-md px-1.5 py-0.5 text-[10px] font-semibold" :class="kindClass(r.kind)">{{ r.kind }}</span></td>
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
        <!-- v117: the statements speak - the two documents a company closes with -->
        <section v-if="bookStatements" class="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
            <div class="flex items-baseline justify-between">
              <h2 class="text-sm font-semibold">Income statement</h2>
              <span class="text-[10px] uppercase tracking-wide text-zinc-600">revenue - expenses</span>
            </div>
            <p v-if="!bookStatements.income.revenue.length && !bookStatements.income.expenses.length" class="mt-3 rounded-xl border border-dashed border-zinc-700 px-4 py-6 text-center text-xs text-zinc-600">
              No income on the books yet - ship an order and the revenue lands.
            </p>
            <table v-else class="mt-3 w-full text-left text-xs">
              <tbody class="divide-y divide-zinc-800/60">
                <tr
                  v-for="r in bookStatements.income.revenue" :key="r.account"
                  class="cursor-pointer transition hover:bg-zinc-800/40"
                  :class="drillAccount === r.account && 'bg-amber-500/10'"
                  :title="`see the journal lines on ${r.account}`"
                  @click="drill(r.account)"
                >
                  <td class="py-1.5 text-zinc-300">{{ r.account }}</td>
                  <td class="py-1.5 text-right tabular-nums text-sky-400">{{ r.balance.toFixed(2) }}</td>
                </tr>
                <tr
                  v-for="r in bookStatements.income.expenses" :key="r.account"
                  class="cursor-pointer transition hover:bg-zinc-800/40"
                  :class="drillAccount === r.account && 'bg-amber-500/10'"
                  :title="`see the journal lines on ${r.account}`"
                  @click="drill(r.account)"
                >
                  <td class="py-1.5 text-zinc-300">{{ r.account }}</td>
                  <td class="py-1.5 text-right tabular-nums text-amber-400">{{ r.balance.toFixed(2) }}</td>
                </tr>
              </tbody>
              <tfoot class="border-t border-zinc-800">
                <tr>
                  <td class="pt-2 text-[10px] uppercase tracking-wide text-zinc-500">net income</td>
                  <td class="pt-2 text-right text-sm font-semibold tabular-nums" :class="bookStatements.income.net >= 0 ? 'text-emerald-400' : 'text-rose-400'">{{ bookStatements.income.net.toFixed(2) }}</td>
                </tr>
              </tfoot>
            </table>
          </div>
          <div class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h2 class="text-sm font-semibold">Balance sheet</h2>
              <span
                class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase"
                :class="bookStatements.balance.balanced ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
              >{{ bookStatements.balance.balanced ? 'the books tie out' : 'does not tie out' }}</span>
            </div>
            <table class="mt-3 w-full text-left text-xs">
              <tbody class="divide-y divide-zinc-800/60">
                <tr
                  v-for="r in bookStatements.balance.assets" :key="r.account"
                  class="cursor-pointer transition hover:bg-zinc-800/40"
                  :class="drillAccount === r.account && 'bg-amber-500/10'"
                  :title="`see the journal lines on ${r.account}`"
                  @click="drill(r.account)"
                >
                  <td class="py-1.5 text-zinc-300">{{ r.account }}</td>
                  <td class="py-1.5 text-right tabular-nums text-emerald-400">{{ r.balance.toFixed(2) }}</td>
                </tr>
                <tr
                  v-for="r in bookStatements.balance.liabilities" :key="r.account"
                  class="cursor-pointer transition hover:bg-zinc-800/40"
                  :class="drillAccount === r.account && 'bg-amber-500/10'"
                  :title="`see the journal lines on ${r.account}`"
                  @click="drill(r.account)"
                >
                  <td class="py-1.5 text-zinc-300">{{ r.account }}</td>
                  <td class="py-1.5 text-right tabular-nums text-rose-400">{{ r.balance.toFixed(2) }}</td>
                </tr>
                <tr
                  v-for="r in bookStatements.balance.equity" :key="r.account"
                  class="cursor-pointer transition hover:bg-zinc-800/40"
                  :class="drillAccount === r.account && 'bg-amber-500/10'"
                  :title="`see the journal lines on ${r.account}`"
                  @click="drill(r.account)"
                >
                  <td class="py-1.5 text-zinc-300">{{ r.account }}</td>
                  <td class="py-1.5 text-right tabular-nums text-violet-400">{{ r.balance.toFixed(2) }}</td>
                </tr>
                <tr>
                  <td class="py-1.5 text-zinc-500">retained earnings <span class="text-zinc-600">· the net, kept</span></td>
                  <td class="py-1.5 text-right tabular-nums text-zinc-500">{{ bookStatements.balance.retained_earnings.toFixed(2) }}</td>
                </tr>
              </tbody>
              <tfoot class="border-t border-zinc-800 text-zinc-400">
                <tr>
                  <td class="pt-2 text-[10px] uppercase tracking-wide text-zinc-500">assets</td>
                  <td class="pt-2 text-right tabular-nums">{{ bookStatements.balance.total_assets.toFixed(2) }}</td>
                </tr>
                <tr>
                  <td class="text-[10px] uppercase tracking-wide text-zinc-500">liabilities + equity + retained</td>
                  <td class="text-right tabular-nums">{{ bookStatements.balance.equation_side.toFixed(2) }}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </section>
        <!-- v118: the cash flow - where the money moved, by the stated heuristic -->
        <section v-if="bookCashFlow" class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-sm font-semibold">Cash flow</h2>
            <span class="text-[10px] uppercase tracking-wide text-zinc-600">direct method · {{ (bookCashFlow.cash_accounts || []).join(', ') || 'no cash account' }}</span>
          </div>
          <p class="mt-0.5 text-xs text-zinc-500">
            in {{ bookCashFlow.inflow_total.toFixed(2) }} · out {{ bookCashFlow.outflow_total.toFixed(2) }} ·
            <span :class="bookCashFlow.net_cash_movement >= 0 ? 'text-emerald-400' : 'text-rose-400'">net {{ bookCashFlow.net_cash_movement.toFixed(2) }}</span>
          </p>
          <ClientOnly v-if="cfChartOption">
            <EChart :option="cfChartOption" :height="210" class="mt-2" />
          </ClientOnly>
        </section>
        <!-- v118: the aging - the collection desk's first grip -->
        <section v-if="bookAging" class="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div v-for="side in agingSides" :key="side.key"
               class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
            <div class="flex items-baseline justify-between">
              <h2 class="text-sm font-semibold">{{ side.title }}</h2>
              <span class="text-[10px] uppercase tracking-wide text-zinc-600">{{ side.note }}</span>
            </div>
            <p class="mt-0.5 text-xs text-zinc-500">
              {{ bookAging[side.key].open_refs }} open ref{{ bookAging[side.key].open_refs === 1 ? '' : 's' }} ·
              <span class="tabular-nums">{{ bookAging[side.key].total.toFixed(2) }}</span> open
            </p>
            <div class="mt-2 flex flex-wrap gap-1.5">
              <span
                v-for="b in bookAging[side.key].buckets" :key="b.bucket"
                class="rounded-md px-2 py-0.5 text-[10px] font-semibold"
                :class="b.refs > 0 ? 'bg-zinc-800 text-zinc-200' : 'bg-zinc-900 text-zinc-600'"
              >{{ b.bucket }} {{ b.total.toFixed(2) }} <span class="font-normal opacity-70">({{ b.refs }})</span></span>
            </div>
            <table v-if="bookAging[side.key].lines.length" class="mt-3 w-full text-left text-xs">
              <thead class="text-zinc-500">
                <tr>
                  <th class="pb-2 font-medium">ref</th><th class="pb-2 font-medium">bucket</th>
                  <th class="pb-2 font-medium text-right">age days</th>
                  <th class="pb-2 font-medium text-right">open</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-zinc-800/60">
                <tr v-for="l in bookAging[side.key].lines" :key="l.ref">
                  <td class="py-1.5 font-mono text-zinc-300">{{ l.ref }}</td>
                  <td class="py-1.5"><span class="rounded-md px-1.5 py-0.5 text-[10px] font-semibold" :class="ageClass(l.bucket)">{{ l.bucket }}</span></td>
                  <td class="py-1.5 text-right tabular-nums text-zinc-400">{{ l.age_days ?? 'n/a' }}</td>
                  <td class="py-1.5 text-right tabular-nums text-zinc-200">{{ l.open.toFixed(2) }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="mt-3 rounded-xl border border-dashed border-zinc-700 px-4 py-5 text-center text-xs text-zinc-600">Nothing open - every ref is settled.</p>
          </div>
        </section>
        <!-- v118: THE CLOSE - post the period, lock the book -->
        <section v-if="bookCloses" class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-sm font-semibold">Period close</h2>
            <span
              v-if="bookCloses.closed"
              class="rounded-md bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase text-emerald-400"
            >book locked</span>
            <span v-else class="rounded-md bg-zinc-800 px-2 py-0.5 text-[10px] font-semibold uppercase text-zinc-400">open</span>
          </div>
          <p class="mt-0.5 text-xs text-zinc-500">The accounting close sweeps revenue and expense through Income summary into Retained earnings with real journal entries - then locks the book. A fresh period rides a fresh book.</p>
          <p v-if="closeError" class="mt-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ closeError }}</p>
          <div v-if="bookCloses.closes.length" class="mt-3 space-y-2">
            <div v-for="c in bookCloses.closes" :key="c.id"
                 class="rounded-xl border border-zinc-800 bg-zinc-950/40 px-3 py-2.5">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-xs font-medium text-zinc-200">{{ c.period }}</p>
                <span class="text-[10px] text-zinc-500">{{ new Date(c.closed_at).toLocaleString() }}</span>
              </div>
              <p class="mt-1 text-[11px] text-zinc-400">
                net <span class="tabular-nums" :class="c.net >= 0 ? 'text-emerald-400' : 'text-rose-400'">{{ c.net.toFixed(2) }}</span>
                · retained after <span class="tabular-nums">{{ c.retained_after.toFixed(2) }}</span>
                · {{ c.entries }} closing lines
              </p>
            </div>
          </div>
          <button v-else
            class="mt-3 rounded-xl bg-amber-500 px-4 py-2 text-xs font-semibold text-zinc-950 transition hover:bg-amber-400 disabled:opacity-50"
            :disabled="closingBooks"
            @click="closeBooks()"
          >{{ closingBooks ? 'Closing the books...' : 'Close the books' }}</button>
        </section>
        <section class="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-sm font-semibold">GL entries</h2>
            <button
              v-if="drillAccount"
              class="flex items-center gap-1.5 rounded-md bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-400 transition hover:bg-amber-500/25"
              title="clear the account filter"
              @click="drill(drillAccount)"
            >{{ drillAccount }} · {{ shownGlLines.length }} line{{ shownGlLines.length === 1 ? '' : 's' }} ✕</button>
          </div>
          <p class="mt-0.5 text-xs text-zinc-500">One journal line per move - the posters pair every debit with a credit (orders, stock, payroll).</p>
          <div v-if="!shownGlLines.length" class="mt-3 rounded-xl border border-dashed border-zinc-700 px-4 py-6 text-center text-xs text-zinc-600">
            {{ drillAccount ? `No journal lines on ${drillAccount} yet.` : 'The books are blank - boot the ERP system (workflows active) and move an order.' }}
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
              <tr v-for="(r, i) in shownGlLines" :key="i">
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
