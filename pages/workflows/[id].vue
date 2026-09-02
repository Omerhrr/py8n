<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { VueFlow, useVueFlow, type Connection, type Edge, type Node } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import {
  Play, Save, Copy, Check, Zap, Globe, Loader2, AlertTriangle,
  Link2, Trash2, Download, Clock, ShieldAlert, Tag as TagIcon, X,
  History, RotateCcw, Undo2, Redo2, StickyNote, Settings2, Keyboard,
  MessageCircle,
} from 'lucide-vue-next'
import { usePy8nStore } from '~/stores/py8n'
import PNodeCard from '~/components/editor/PNodeCard.vue'
import PStickyNote from '~/components/editor/PStickyNote.vue'
import NodePalette from '~/components/editor/NodePalette.vue'
import NodeContextMenu, { type ContextMenuItem } from '~/components/editor/NodeContextMenu.vue'
import ShortcutsOverlay from '~/components/editor/ShortcutsOverlay.vue'
import ChatPanel from '~/components/editor/ChatPanel.vue'
import ConfigPanel from '~/components/editor/ConfigPanel.vue'
import ExecutionsDrawer from '~/components/editor/ExecutionsDrawer.vue'
import type { NodeDefinition, NodeSpec, Workflow } from '~/types/node'
import { createGraphHistory, type GraphSnapshot } from '~/composables/useGraphHistory'

const route = useRoute()
const store = usePy8nStore()
const { api } = useApi()
const workflowId = computed(() => route.params.id as string)

// ------------------------------------------------------------------
// Vue Flow state
// ------------------------------------------------------------------
const vfNodes = ref<Node[]>([])
const vfEdges = ref<Edge[]>([])
const selectedNodeId = ref<string | null>(null)
const selectedEdgeId = ref<string | null>(null)
const drawerOpen = ref(true)
const copied = ref(false)
// v25: floating editor chat - shown when the canvas contains a Chat Trigger
const chatOpen = ref(false)
const hasChatTrigger = computed(() =>
  vfNodes.value.some((n: any) => n?.data?.spec?.type === 'chat_trigger'),
)
const chatWelcome = computed(() => {
  const spec = vfNodes.value.find((n: any) => n?.data?.spec?.type === 'chat_trigger')
  return spec?.data?.spec?.parameters?.welcome_message || ''
})
const toasts = ref<{ id: number; text: string; kind: 'info' | 'error' | 'success' }[]>([])
let toastSeq = 0

const { addNodes, addEdges, screenToFlowCoordinate, fitView, onConnect, onNodeClick, onEdgeClick, onPaneClick, onNodeDragStop, getSelectedNodes, onNodeContextMenu, onEdgeContextMenu } = useVueFlow()

// ------------------------------------------------------------------
// v18: undo/redo + node copy/paste/duplicate
// ------------------------------------------------------------------
const history = createGraphHistory()
const { canUndo, canRedo } = history
let clipboard: GraphSnapshot | null = null
let pasteSeq = 0
let commitTimer: ReturnType<typeof setTimeout> | null = null

// Vue Flow's v-model write-back is deferred (addNodes/addEdges land in
// vfNodes on the next tick), so commits must be deferred + coalesced -
// a synchronous read right after addNodes() still sees the old graph.
function historyCommit() {
  if (commitTimer) clearTimeout(commitTimer)
  commitTimer = setTimeout(() => {
    commitTimer = null
    history.commit(canvasToGraph())
  }, 40)
}

function undoGraph() {
  if (commitTimer) {
    // A mutation is still uncommitted - undoing means reverting it, so
    // drop the pending commit and schedule a no-op-safe re-commit.
    clearTimeout(commitTimer)
    commitTimer = null
    nextTick(() => history.commit(canvasToGraph()))
  }
  const snap = history.undo()
  if (snap) applySnapshot(snap)
}

function redoGraph() {
  if (commitTimer) {
    clearTimeout(commitTimer)
    commitTimer = null
    nextTick(() => history.commit(canvasToGraph()))
  }
  const snap = history.redo()
  if (snap) applySnapshot(snap)
}

function applySnapshot(snap: GraphSnapshot) {
  graphToCanvas(snap)
  selectedNodeId.value = null
  selectedEdgeId.value = null
  store.markDirty()
}

function captureSelection() {
  const selNodes = (vfNodes.value as any[]).filter((n) => n.selected)
  const ids = new Set<string>(
    selNodes.length ? selNodes.map((n) => n.id) : selectedNodeId.value ? [selectedNodeId.value] : [],
  )
  if (!ids.size) return null
  const specs = (vfNodes.value as any[])
    .filter((n) => ids.has(n.id))
    .map((n) => JSON.parse(JSON.stringify(n.data.spec)))
  const edges = vfEdges.value
    .filter((e: any) => ids.has(e.source) && ids.has(e.target))
    .map((e: any) => JSON.parse(JSON.stringify(e)))
  return { specs, edges }
}

function copySelection() {
  const cap = captureSelection()
  if (!cap) return
  clipboard = cap
  toast(`Copied ${cap.specs.length} node${cap.specs.length > 1 ? 's' : ''}`, 'info')
}

function pasteSelection() {
  if (!clipboard?.specs.length) return
  pasteSeq += 1
  const idMap: Record<string, string> = {}
  const newSpecs = clipboard.specs.map((s: any, i: number) => {
    const id = `p_${uuid()}`
    idMap[s.id] = id
    return {
      ...JSON.parse(JSON.stringify(s)),
      id,
      name: `${s.name || s.type} copy`, // n8n-style suffix; keeps run logs readable
      position: { x: (s.position?.x ?? 0) + 48, y: (s.position?.y ?? 0) + 48 },
    }
  })
  const newEdges = clipboard.edges
    .map((e: any, i: number) => ({
      id: `e_p${pasteSeq}_${i}_${uuid()}`,
      source: idMap[e.source],
      target: idMap[e.target],
      sourceHandle: e.sourceHandle || 'main',
      targetHandle: e.targetHandle || 'main',
      ...branchEdgeExtras(e.sourceHandle),
    }))
    .filter((e: any) => e.source && e.target)
  addNodes(newSpecs.map(specToVfNode))
  if (newEdges.length) addEdges(newEdges)
  selectedNodeId.value = newSpecs[0].id
  selectedEdgeId.value = null
  store.markDirty()
  historyCommit()
  toast(`Pasted ${newSpecs.length} node${newSpecs.length > 1 ? 's' : ''}`, 'success')
}

function duplicateSelection() {
  const cap = captureSelection()
  if (!cap) return
  const saved = clipboard
  clipboard = cap
  pasteSelection()
  clipboard = saved // Ctrl+D must not clobber the user's clipboard
}

function toast(text: string, kind: 'info' | 'error' | 'success' = 'info') {
  const id = ++toastSeq
  toasts.value.push({ id, text, kind })
  setTimeout(() => (toasts.value = toasts.value.filter((t) => t.id !== id)), 4200)
}

// ------------------------------------------------------------------
// Graph <-> canvas mapping
// ------------------------------------------------------------------
function specToVfNode(spec: NodeSpec): Node {
  return {
    id: spec.id,
    type: spec.type === 'sticky_note' ? 'sticky' : 'py8n', // v19: sticky notes render their own card
    position: { x: spec.position?.x ?? 0, y: spec.position?.y ?? 0 },
    data: { spec, definition: store.definitionFor(spec.type) },
  }
}

// v21: branch connection labels - IF true/false, Switch rule N / fallback.
// Labels are DERIVED from sourceHandle on every canvas build, never persisted
// (canvasToGraph only saves sourceHandle), so imports/pastes stay clean.
function branchEdgeExtras(handle?: string | null) {
  const h = handle || 'main'
  if (h === 'main') return {}
  const conf =
    h === 'true'
      ? { text: 'true', fill: '#34d399' }
      : h === 'false'
        ? { text: 'false', fill: '#fb7185' }
        : h === 'fallback'
          ? { text: 'fallback', fill: '#fbbf24' }
          : /^[0-9]+$/.test(h)
            ? { text: `rule ${Number(h) + 1}`, fill: '#fb7185' }
            : { text: h, fill: '#a1a1aa' }
  return {
    label: conf.text,
    labelShowBg: true,
    labelStyle: { fill: conf.fill, fontSize: 10, fontWeight: 700 },
    labelBgStyle: { fill: '#18181b', fillOpacity: 0.92 },
    labelBgPadding: [6, 2] as [number, number],
    labelBgBorderRadius: 4,
  }
}

function graphToCanvas(graph: { nodes: NodeSpec[]; edges: any[] }) {
  vfNodes.value = (graph.nodes || []).map(specToVfNode)
  vfEdges.value = (graph.edges || []).map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle || 'main',
    targetHandle: e.targetHandle || 'main',
    animated: store.nodeStates[e.source] === 'running',
    ...branchEdgeExtras(e.sourceHandle),
  }))
}

function canvasToGraph() {
  return {
    nodes: vfNodes.value.map((n: any) => ({
      id: n.id,
      type: n.data.spec.type,
      name: n.data.spec.name,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
      parameters: n.data.spec.parameters || {},
      settings: n.data.spec.settings || undefined,
      disabled: n.data.spec.disabled || undefined,
      pinned_data: n.data.spec.pinned_data ?? undefined, // v17: omit when unpinned
    })),
    edges: vfEdges.value.map((e: any) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle || 'main',
      targetHandle: e.targetHandle || 'main',
    })),
  }
}

async function saveGraph(silent = false) {
  try {
    await store.save(canvasToGraph())
    if (!silent) toast('Workflow saved', 'success')
    await store.loadWebhookUrl()
    await store.loadScheduleInfo() // refresh next-run previews after save
  } catch {
    toast(store.lastSaveError || 'Save failed', 'error')
  }
}

// ------------------------------------------------------------------
// Schedule preview helpers (v7)
// ------------------------------------------------------------------
function relTime(iso: string) {
  const diff = new Date(iso).getTime() - Date.now()
  if (diff <= 0) return 'now'
  const mins = Math.round(diff / 60000)
  if (mins < 1) return 'in <1m'
  if (mins < 60) return `in ${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `in ${hours}h ${mins % 60}m`
  return `in ${Math.round(hours / 24)}d`
}

const schedulePill = computed(() => {
  const info = store.scheduleInfo
  if (!info || !info.schedules.length) return null
  const first = info.schedules[0]
  if (first.error) return { text: `schedule broken - ${first.error}`, tone: 'error' as const, title: first.error }
  if (info.is_active && info.next_run_at) {
    return {
      text: `${first.summary} · next ${relTime(info.next_run_at)}`,
      tone: 'live' as const,
      title: `Upcoming runs:\n${info.schedules[0].next_runs.map((r) => new Date(r).toLocaleString()).join('\n')}`,
    }
  }
  return { text: `${first.summary} · paused`, tone: 'paused' as const, title: 'Activate the workflow to enable this schedule' }
})

// ------------------------------------------------------------------
// Load
// ------------------------------------------------------------------
onMounted(async () => {
  await Promise.all([store.loadDefinitions(), store.loadCredentials()])
  store.loadEnvVars().catch(() => {}) // v19: expression autocomplete needs env keys
  await store.loadWorkflow(workflowId.value)
  graphToCanvas(store.workflow!.graph || { nodes: [], edges: [] })
  await nextTick()
  fitView({ padding: 0.25, maxZoom: 1.2 })
  history.reset(canvasToGraph()) // v18: fresh undo history per load
  await store.loadExecutions()
  await store.loadWebhookUrl()
  await store.loadScheduleInfo()
  store.loadWorkflows().catch(() => {}) // for the error-workflow selector (v8)
  if (store.executions.length) {
    await store.loadExecution(store.executions[0].id)
  }
})

// ------------------------------------------------------------------
// Canvas interactions
// ------------------------------------------------------------------
// Collision-proof element id - Math.random-based edge ids could (however
// rarely) collide and corrupt the saved graph / undo snapshots.
function uuid(): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  } catch { /* older browsers */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}-${Math.random().toString(36).slice(2, 10)}`
}

onConnect((params: Connection) => {
  if (params.source === params.target) {
    toast('Self-connections are not allowed', 'error')
    return
  }
  const dup = vfEdges.value.some(
    (e) =>
      e.source === params.source &&
      e.target === params.target &&
      (e.sourceHandle || 'main') === (params.sourceHandle || 'main'),
  )
  if (dup) {
    toast('Those ports are already connected', 'error')
    return
  }
  addEdges([
    {
      id: `e_${uuid()}`,
      source: params.source!,
      target: params.target!,
      sourceHandle: params.sourceHandle || 'main',
      targetHandle: params.targetHandle || 'main',
      ...branchEdgeExtras(params.sourceHandle),
    },
  ])
  store.markDirty()
  historyCommit()
})

onNodeClick(({ node }) => {
  selectedNodeId.value = node.id
  selectedEdgeId.value = null
})
onEdgeClick(({ edge }) => {
  selectedEdgeId.value = edge.id
  selectedNodeId.value = null
})
onPaneClick(() => {
  selectedNodeId.value = null
  selectedEdgeId.value = null
})
onNodeDragStop(() => {
  store.markDirty()
  historyCommit()
})

const selectedNodeSpec = computed<NodeSpec | null>(() => {
  if (!selectedNodeId.value) return null
  const vf = vfNodes.value.find((n) => n.id === selectedNodeId.value) as any
  return vf ? vf.data.spec : null
})
const selectedDefinition = computed(() =>
  selectedNodeSpec.value ? store.definitionFor(selectedNodeSpec.value.type) || STICKY_DEF : null,
)

// v19: sticky notes are hidden from /node-definitions - the page supplies
// a local definition so the ConfigPanel can render their fields.
const STICKY_DEF = {
  type: 'sticky_note',
  name: 'Sticky Note',
  description: 'Canvas annotation - never executes; documents your workflow.',
  category: 'actions',
  icon: 'sticky-note',
  color: '#fbbf24',
  inputs: [],
  outputs: [],
  parameters_schema: {
    properties: {
      text: { type: 'string', widget: 'textarea', rows: 5, default: 'Note something down…' },
      color: { type: 'string', widget: 'select', options: ['amber', 'emerald', 'sky', 'rose', 'violet'], default: 'amber' },
    },
  },
  defaults: { text: 'Note something down…', color: 'amber' },
} as any

// v19: multi-selection state (marquee / shift-click) for the floating bar
const selectedCount = computed(() => getSelectedNodes.value.length)

// ------------------------------------------------------------------
// v20: right-click context menus (nodes + connections)
// ------------------------------------------------------------------
const contextMenu = ref<{ kind: 'node' | 'edge'; x: number; y: number; nodeId?: string; edgeId?: string } | null>(null)

onNodeContextMenu(({ event, node }) => {
  event.preventDefault()
  selectedNodeId.value = node.id
  selectedEdgeId.value = null
  contextMenu.value = { kind: 'node', x: (event as MouseEvent).clientX, y: (event as MouseEvent).clientY, nodeId: node.id }
})
onEdgeContextMenu(({ event, edge }) => {
  event.preventDefault()
  selectedEdgeId.value = edge.id
  selectedNodeId.value = null
  contextMenu.value = { kind: 'edge', x: (event as MouseEvent).clientX, y: (event as MouseEvent).clientY, edgeId: edge.id }
})

const contextMenuItems = computed<ContextMenuItem[]>(() => {
  const menu = contextMenu.value
  if (!menu) return []
  if (menu.kind === 'edge') {
    return [{ label: 'Delete connection', hint: 'Del', danger: true, action: () => deleteSelectedEdge() }]
  }
  const spec = (vfNodes.value as any[]).find((n) => n.id === menu.nodeId)?.data?.spec
  const isSticky = spec?.type === 'sticky_note'
  const items: ContextMenuItem[] = [
    { label: 'Open settings', hint: 'click', action: () => { selectedNodeId.value = menu.nodeId! } },
    { label: 'Duplicate', hint: 'Ctrl+D', action: () => duplicateSelection() },
    { label: 'Copy', hint: 'Ctrl+C', action: () => copySelection() },
  ]
  if (spec && !isSticky) {
    items.push({
      label: spec.disabled ? 'Enable node' : 'Disable node',
      action: () => {
        const vf = (vfNodes.value as any[]).find((n) => n.id === menu.nodeId)
        if (vf) {
          vf.data.spec.disabled = !vf.data.spec.disabled
          store.markDirty()
          historyCommit()
        }
      },
    })
  }
  items.push({ label: 'Delete', hint: 'Del', danger: true, action: () => deleteSelectedNode() })
  return items
})

// ------------------------------------------------------------------
// v20: workflow settings modal (description + retention override)
// ------------------------------------------------------------------
const showSettings = ref(false)
const savingSettings = ref(false)
const globalRetentionDays = ref<number | null>(null)
const settingsDraft = ref<{ description: string; retentionMode: 'inherit' | 'keep' | 'days'; retentionDays: number } | null>(null)

async function openSettings() {
  const wf = store.workflow
  settingsDraft.value = {
    description: wf?.description || '',
    retentionMode: wf?.retention_days == null ? 'inherit' : wf.retention_days === 0 ? 'keep' : 'days',
    retentionDays: wf?.retention_days && wf.retention_days > 0 ? wf.retention_days : 30,
  }
  showSettings.value = true
  if (globalRetentionDays.value == null) {
    try {
      globalRetentionDays.value = (await api.get('/settings/retention')).retention_days
    } catch {
      globalRetentionDays.value = null
    }
  }
}

async function saveSettings() {
  const d = settingsDraft.value
  if (!d) return
  savingSettings.value = true
  try {
    const body: Record<string, any> = { description: d.description }
    if (d.retentionMode === 'inherit') body.retention_days = null
    else if (d.retentionMode === 'keep') body.retention_days = 0
    else body.retention_days = Math.max(1, Math.round(d.retentionDays || 30))
    const updated = await api.put<Workflow>(`/workflows/${workflowId.value}`, body)
    if (store.workflow) {
      store.workflow.description = updated.description
      store.workflow.retention_days = updated.retention_days
    }
    toast('Workflow settings saved', 'success')
    showSettings.value = false
  } catch (e: any) {
    toast(e?.data?.detail || 'Settings save failed', 'error')
  } finally {
    savingSettings.value = false
  }
}

// ------------------------------------------------------------------
// v20: shortcuts cheat sheet
// ------------------------------------------------------------------
const showShortcuts = ref(false)

const canvasNodeNames = computed(() =>
  (vfNodes.value as any[])
    .filter((n) => n.data?.spec?.type && n.data.spec.type !== 'sticky_note')
    .map((n) => n.data.spec.name)
    .filter(Boolean),
)
const envKeys = computed(() => store.envVars.map((v: any) => v.key))

function updateParam(key: string, value: any) {
  const vf = vfNodes.value.find((n) => n.id === selectedNodeId.value) as any
  if (!vf) return
  vf.data.spec.parameters = { ...vf.data.spec.parameters, [key]: value }
  store.markDirty()
  historyCommit()
}

function updateSettings(patch: Record<string, any>) {
  const vf = vfNodes.value.find((n) => n.id === selectedNodeId.value) as any
  if (!vf) return
  const current = vf.data.spec.settings || {
    retry_on_fail: false, max_retries: 2, retry_wait_ms: 500, continue_on_fail: false,
    timeout_ms: 0, fallback_enabled: false, fallback_value: null,
  }
  vf.data.spec.settings = { ...current, ...patch }
  store.markDirty()
  historyCommit()
}

function toggleDisabled(value: boolean) {
  const vf = vfNodes.value.find((n) => n.id === selectedNodeId.value) as any
  if (!vf) return
  vf.data.spec.disabled = value
  store.markDirty()
  historyCommit()
}

function updatePinned(value: any) {
  const vf = vfNodes.value.find((n) => n.id === selectedNodeId.value) as any
  if (!vf) return
  vf.data.spec.pinned_data = value ?? undefined // null/undefined = unpinned
  store.markDirty()
  historyCommit()
}

// ------------------------------------------------------------------
// v17 test step + "use last run output" - the panel calls these so the
// canvas is auto-saved first (the backend test endpoint reads the saved graph)
// ------------------------------------------------------------------
async function runTestStep(nodeId: string, items: any) {
  if (store.dirty || !store.workflow?.graph?.nodes?.length) await saveGraph(true)
  return await store.testNodeStep(workflowId.value, nodeId, items)
}

async function loadLastOutput(nodeId: string) {
  if (!store.executions.length) await store.loadExecutions()
  const latest = store.executions[0]
  if (!latest) return null
  const detail = store.lastRun?.id === latest.id ? store.lastRun : await store.loadExecution(latest.id)
  const runs = (detail?.node_runs || []).filter(
    (r) => r.node_id === nodeId && r.status === 'success' && r.output !== undefined,
  )
  return runs.length ? runs[runs.length - 1].output : null
}


// ------------------------------------------------------------------
// Tags editor (v12) - instant-save popover in the header
// ------------------------------------------------------------------
const showTags = ref(false)
const tagInput = ref('')
const savingTags = ref(false)

const workflowTags = computed(() => store.workflow?.tags || [])

const knownTags = computed(() => {
  const set = new Set<string>()
  for (const w of store.workflows) for (const t of w.tags || []) set.add(t)
  return Array.from(set)
})

const tagSuggestions = computed(() => {
  const q = tagInput.value.trim().toLowerCase()
  if (!q) return []
  return knownTags.value.filter((t) => t.includes(q) && !workflowTags.value.includes(t)).slice(0, 4)
})

async function addTag(raw: string) {
  const t = raw.trim().toLowerCase()
  tagInput.value = ''
  if (!t || workflowTags.value.includes(t)) return
  if (workflowTags.value.length >= 10) {
    toast('Max 10 tags per workflow', 'error')
    return
  }
  await persistTags([...workflowTags.value, t])
}

async function removeTag(tag: string) {
  await persistTags(workflowTags.value.filter((t) => t !== tag))
}

async function persistTags(tags: string[]) {
  savingTags.value = true
  try {
    await store.setTags(tags)
    toast('Tags saved', 'success')
  } catch (e: any) {
    toast(e?.data?.detail || e?.message || 'Tag save failed', 'error')
  } finally {
    savingTags.value = false
  }
}

// ------------------------------------------------------------------
// Version history (v13) - bounded snapshot list + one-click restore
// ------------------------------------------------------------------
const showHistory = ref(false)
const restoring = ref<number | null>(null)

async function openHistory() {
  showHistory.value = true
  try {
    await store.loadVersions()
  } catch {
    toast('Could not load version history', 'error')
  }
}

function fmtVersionTime(iso: string | null) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

async function doRestore(version: number) {
  if (!confirm(`Restore version ${version}? The current canvas is replaced - the restore itself is saved as a new version, so this can be undone.`)) return
  restoring.value = version
  try {
    await store.restoreVersion(version)
    // re-render the canvas from the restored graph
    graphToCanvas(store.workflow?.graph || { nodes: [], edges: [] })
    await nextTick()
    fitView({ padding: 0.25, maxZoom: 1.2 })
    history.reset(canvasToGraph()) // v18: fresh undo history after restore
    store.markDirty()
    toast(`Restored version ${version} - saved as a new version`, 'success')
  } catch (e: any) {
    toast(e?.data?.detail || e?.message || 'Restore failed', 'error')
  } finally {
    restoring.value = null
  }
}

const otherWorkflows = computed(() =>
  store.workflows.filter((w) => w.id !== workflowId.value),
)

async function onErrorWorkflowChange(event: Event) {
  const handlerId = (event.target as HTMLSelectElement).value || null
  try {
    await store.setErrorWorkflow(handlerId)
    toast(
      handlerId
        ? `On error → ${store.workflows.find((w) => w.id === handlerId)?.name || 'workflow'}`
        : 'Error workflow unbound',
      'success',
    )
  } catch (e: any) {
    toast(e?.data?.detail || e?.message || 'Could not bind error workflow', 'error')
  }
}

function renameNode(name: string) {
  const vf = vfNodes.value.find((n) => n.id === selectedNodeId.value) as any
  if (!vf) return
  vf.data.spec.name = name
  store.markDirty()
  historyCommit()
}

function deleteSelectedNode() {
  if (!selectedNodeId.value) return
  vfNodes.value = vfNodes.value.filter((n) => n.id !== selectedNodeId.value)
  vfEdges.value = vfEdges.value.filter((e) => e.source !== selectedNodeId.value && e.target !== selectedNodeId.value)
  selectedNodeId.value = null
  store.markDirty()
  historyCommit()
}

function deleteSelectedEdge() {
  if (!selectedEdgeId.value) return
  vfEdges.value = vfEdges.value.filter((e) => e.id !== selectedEdgeId.value)
  selectedEdgeId.value = null
  store.markDirty()
  historyCommit()
}

// v19: delete EVERY selected node (marquee / shift-click multi-select)
function deleteSelectedNodesMulti() {
  const ids = new Set((vfNodes.value as any[]).filter((n) => n.selected).map((n) => n.id))
  if (!ids.size) return
  vfNodes.value = vfNodes.value.filter((n: any) => !ids.has(n.id))
  vfEdges.value = vfEdges.value.filter((e: any) => !ids.has(e.source) && !ids.has(e.target))
  if (selectedNodeId.value && ids.has(selectedNodeId.value)) selectedNodeId.value = null
  store.markDirty()
  historyCommit()
  toast(`Deleted ${ids.size} node${ids.size > 1 ? 's' : ''}`, 'info')
}

function onKeydown(e: KeyboardEvent) {
  const tag = (e.target as HTMLElement)?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if ((vfNodes.value as any[]).some((n) => n.selected)) deleteSelectedNodesMulti()
    else if (selectedEdgeId.value) deleteSelectedEdge()
    else if (selectedNodeId.value) deleteSelectedNode()
  }
  const mod = e.metaKey || e.ctrlKey
  if (e.key === '?') {
    e.preventDefault()
    showShortcuts.value = !showShortcuts.value
    return
  }
  if (mod && e.key.toLowerCase() === 'z') {
    e.preventDefault()
    if (e.shiftKey) redoGraph()
    else undoGraph()
    return
  }
  if (mod && e.key.toLowerCase() === 'y') {
    e.preventDefault()
    redoGraph()
    return
  }
  if (mod && e.key.toLowerCase() === 'c') {
    copySelection()
    return
  }
  if (mod && e.key.toLowerCase() === 'v') {
    pasteSelection()
    return
  }
  if (mod && e.key.toLowerCase() === 'd') {
    e.preventDefault()
    duplicateSelection()
    return
  }
  if (mod && e.key === 's') {
    e.preventDefault()
    saveGraph()
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

// ------------------------------------------------------------------
// Add node (palette click / drop)
// ------------------------------------------------------------------
let addCounter = 0
function addNodeFromDef(def: NodeDefinition, position?: { x: number; y: number }) {
  addCounter += 1
  const id = `${def.type.split('_')[0]}_${Date.now().toString(36).slice(-4)}${addCounter}`
  const spec: NodeSpec = {
    id,
    type: def.type,
    name: def.name,
    position: position || {
      x: Math.random() * 300 + 120,
      y: Math.random() * 200 + 80,
    },
    parameters: JSON.parse(JSON.stringify(def.defaults || {})),
  }
  addNodes([specToVfNode(spec)])
  selectedNodeId.value = id
  store.markDirty()
  historyCommit()
  toast(`${def.name} added`, 'success')
}

function onDrop(event: DragEvent) {
  const type = event.dataTransfer?.getData('application/py8n-node')
  if (!type) return
  const def = store.definitionFor(type)
  if (!def) return
  const position = screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
  addNodeFromDef(def, position)
}

// ------------------------------------------------------------------
// v19: sticky notes - toolbar-added annotations (hidden from the palette)
// ------------------------------------------------------------------
let noteSeq = 0
function addStickyNote() {
  noteSeq += 1
  const id = `note_${Date.now().toString(36).slice(-4)}${noteSeq}`
  const spec: NodeSpec = {
    id,
    type: 'sticky_note',
    name: 'Sticky note',
    position: { x: 120 + Math.random() * 260, y: 80 + Math.random() * 160 },
    parameters: { text: 'Note something down…', color: 'amber' },
  }
  addNodes([specToVfNode(spec)])
  selectedNodeId.value = id
  store.markDirty()
  historyCommit()
  toast('Sticky note added', 'success')
}

// ------------------------------------------------------------------
// Run
// ------------------------------------------------------------------
async function runWorkflow() {
  try {
    if (store.dirty || !store.workflow?.graph?.nodes?.length) await saveGraph(true)
    await store.runWorkflow()
    drawerOpen.value = true
  } catch (e: any) {
    toast(e?.data?.detail || 'Run failed - does the workflow have a trigger?', 'error')
  }
}

async function copyWebhook() {
  if (!store.webhookUrl) return
  try {
    await navigator.clipboard.writeText(store.webhookUrl)
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  } catch {
    toast('Could not access clipboard', 'error')
  }
}

async function toggleActive() {
  try {
    await store.toggleActive()
    toast(store.workflow?.is_active ? 'Triggers activated' : 'Triggers paused', 'success')
  } catch (e: any) {
    toast(e?.data?.detail || e?.message || 'Activation failed', 'error')
  }
}

async function selectRun(id: string) {
  await store.loadExecution(id)
}

async function createCredential(body: any) {
  return await store.createCredential(body)
}

// ------------------------------------------------------------------
// Export / duplicate
// ------------------------------------------------------------------
async function exportWorkflow() {
  try {
    const doc = await api.get(`/workflows/${workflowId.value}/export`)
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${(doc.name || 'workflow').replace(/[^\w\-]+/g, '_').toLowerCase()}.py8n.json`
    a.click()
    URL.revokeObjectURL(url)
    toast('Workflow exported', 'success')
  } catch {
    toast('Export failed', 'error')
  }
}

async function duplicateWorkflow() {
  try {
    const copy = await api.post(`/workflows/${workflowId.value}/duplicate`)
    toast('Duplicated - opening copy…', 'success')
    await navigateTo(`/workflows/${copy.id}`)
  } catch {
    toast('Duplicate failed', 'error')
  }
}

// animate edges out of nodes that are currently running
watch(
  () => ({ ...store.nodeStates }),
  () => {
    vfEdges.value = vfEdges.value.map((e) => ({
      ...e,
      animated: store.nodeStates[e.source] === 'running',
    }))
  },
  { deep: true },
)

const runningCount = computed(
  () => Object.values(store.nodeStates).filter((s) => s === 'running').length,
)
</script>

<template>
  <div class="flex h-full min-h-0 flex-col overflow-hidden bg-zinc-950 text-zinc-100">
    <!-- top bar (app nav lives in the sidebar) -->
    <header class="flex h-14 shrink-0 items-center gap-3 border-b border-zinc-800 bg-zinc-950/95 px-3">
      <input
        v-if="store.workflow"
        :value="store.workflow.name"
        class="min-w-0 max-w-64 flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1 text-sm font-semibold outline-none transition hover:border-zinc-800 focus:border-orange-500/50 focus:bg-zinc-900"
        @change="((store.workflow!.name = ($event.target as HTMLInputElement).value), store.markDirty())"
      />
      <span v-if="store.dirty" class="h-2 w-2 shrink-0 rounded-full bg-orange-500" title="Unsaved changes" />

      <!-- tags editor (v12) -->
      <div class="relative hidden md:block">
        <button
          class="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-medium transition"
          :class="workflowTags.length
            ? 'border-zinc-700 bg-zinc-900 text-zinc-200'
            : 'border-zinc-800 text-zinc-500 hover:text-zinc-300'"
          :title="workflowTags.length ? 'Edit tags' : 'Add tags to organize this workflow'"
          @click="showTags = !showTags"
        >
          <TagIcon class="h-3.5 w-3.5" />
          <span v-if="workflowTags.length" class="max-w-48 truncate">{{ workflowTags.join(', ') }}</span>
          <span v-else>Tags</span>
        </button>

        <div
          v-if="showTags"
          class="absolute left-0 top-full z-40 mt-2 w-72 rounded-xl border border-zinc-800 bg-zinc-900 p-3 shadow-2xl"
        >
          <div class="mb-2 flex items-center justify-between">
            <p class="text-[11px] font-semibold uppercase tracking-wide text-zinc-500">Tags</p>
            <button class="rounded p-0.5 text-zinc-500 hover:text-zinc-200" title="Close" @click="showTags = false">
              <X class="h-3.5 w-3.5" />
            </button>
          </div>
          <div v-if="workflowTags.length" class="mb-2 flex flex-wrap gap-1.5">
            <span
              v-for="tag in workflowTags"
              :key="tag"
              class="inline-flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-200"
            >
              {{ tag }}
              <button class="text-zinc-500 transition hover:text-rose-400" :title="`Remove “${tag}”`" @click="removeTag(tag)">
                <X class="h-3 w-3" />
              </button>
            </span>
          </div>
          <p v-else class="mb-2 text-[11px] text-zinc-600">No tags yet - add one below.</p>
          <div class="relative">
            <input
              v-model="tagInput"
              class="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none transition focus:border-orange-500"
              placeholder="Type a tag and press Enter…"
              :disabled="savingTags"
              @keydown.enter.prevent="addTag(tagInput)"
            />
            <div v-if="tagSuggestions.length" class="absolute left-0 right-0 top-full z-10 mt-1 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900 shadow-xl">
              <button
                v-for="s in tagSuggestions"
                :key="s"
                class="block w-full px-2.5 py-1.5 text-left text-xs text-zinc-300 transition hover:bg-zinc-800"
                @click="addTag(s)"
              >
                {{ s }}
              </button>
            </div>
          </div>
          <p class="mt-2 text-[10px] leading-relaxed text-zinc-600">Lowercase, max 10 · saved instantly. Tags power the dashboard filter.</p>
        </div>
      </div>

      <!-- webhook pill -->
      <button
        v-if="store.webhookUrl"
        class="hidden items-center gap-1.5 rounded-lg border border-orange-500/30 bg-orange-500/10 px-2.5 py-1 font-mono text-[10px] text-orange-300 transition hover:bg-orange-500/20 lg:flex"
        title="Copy webhook URL"
        @click="copyWebhook"
      >
        <Globe class="h-3 w-3" />
        {{ copied ? 'Copied!' : 'Webhook URL' }}
        <Copy v-if="!copied" class="h-3 w-3" />
        <Check v-else class="h-3 w-3" />
      </button>

      <div class="ml-auto flex items-center gap-2">
        <!-- v18: undo / redo -->
        <button
          class="rounded-lg border border-zinc-800 p-1.5 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200 disabled:pointer-events-none disabled:opacity-30"
          title="Undo (Ctrl+Z)"
          :disabled="!canUndo"
          @click="undoGraph()"
        >
          <Undo2 class="h-3.5 w-3.5" />
        </button>
        <button
          class="rounded-lg border border-zinc-800 p-1.5 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200 disabled:pointer-events-none disabled:opacity-30"
          title="Redo (Ctrl+Shift+Z)"
          :disabled="!canRedo"
          @click="redoGraph()"
        >
          <Redo2 class="h-3.5 w-3.5" />
        </button>
        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-800 px-2.5 py-1.5 text-[11px] font-medium text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200"
          title="Download workflow as JSON"
          @click="exportWorkflow"
        >
          <Download class="h-3.5 w-3.5" /> <span class="hidden md:inline">Export</span>
        </button>
        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-800 px-2.5 py-1.5 text-[11px] font-medium text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200"
          title="Duplicate this workflow"
          @click="duplicateWorkflow"
        >
          <Copy class="h-3.5 w-3.5" /> <span class="hidden md:inline">Duplicate</span>
        </button>

        <!-- version history (v13) -->
        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-800 px-2.5 py-1.5 text-[11px] font-medium text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200"
          title="Version history - restore any previous save"
          @click="openHistory"
        >
          <History class="h-3.5 w-3.5" /> <span class="hidden md:inline">History</span>
        </button>

        <!-- v20: workflow settings -->
        <button
          class="rounded-lg border border-zinc-800 p-1.5 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200"
          title="Workflow settings - description & data retention"
          @click="openSettings"
        >
          <Settings2 class="h-3.5 w-3.5" />
        </button>
        <!-- v20: shortcut cheat sheet -->
        <button
          class="rounded-lg border border-zinc-800 p-1.5 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200"
          title="Keyboard & mouse shortcuts (?)"
          @click="showShortcuts = true"
        >
          <Keyboard class="h-3.5 w-3.5" />
        </button>

        <button
          class="flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-medium transition"
          :class="store.workflow?.is_active
            ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400'
            : 'border-zinc-700 text-zinc-500 hover:text-zinc-300'"
          :title="store.workflow?.is_active ? 'Triggers active - click to pause' : 'Click to activate triggers'"
          @click="toggleActive"
        >
          <Link2 class="h-3.5 w-3.5" />
          {{ store.workflow?.is_active ? 'Triggers on' : 'Triggers off' }}
        </button>

        <!-- error workflow binding (v8) -->
        <div
          class="hidden items-center gap-1 rounded-lg border px-2 py-1.5 transition lg:flex"
          :class="store.workflow?.error_workflow_id
            ? 'border-rose-500/40 bg-rose-500/5 text-rose-300'
            : 'border-zinc-800 text-zinc-500'"
          title="Runs with a structured error payload when this workflow fails"
        >
          <ShieldAlert class="h-3.5 w-3.5 shrink-0" />
          <select
            class="max-w-40 cursor-pointer truncate bg-transparent text-[11px] font-medium outline-none"
            :value="store.workflow?.error_workflow_id || ''"
            @change="onErrorWorkflowChange"
          >
            <option value="" class="bg-zinc-900">On error: stop</option>
            <option v-for="w in otherWorkflows" :key="w.id" :value="w.id" class="bg-zinc-900">
              On error → {{ w.name }}
            </option>
          </select>
        </div>

        <!-- schedule preview pill (v7) -->
        <div
          v-if="schedulePill"
          class="hidden items-center gap-1.5 rounded-lg border px-2.5 py-1 font-mono text-[10px] xl:flex"
          :class="schedulePill.tone === 'live'
            ? 'border-emerald-500/30 bg-emerald-500/5 text-emerald-300'
            : schedulePill.tone === 'error'
              ? 'border-rose-500/40 bg-rose-500/10 text-rose-300'
              : 'border-zinc-800 bg-zinc-900 text-zinc-500'"
          :title="schedulePill.title"
        >
          <Clock class="h-3 w-3" />
          {{ schedulePill.text }}
        </div>

        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-700 px-2.5 py-1.5 text-[11px] font-medium text-zinc-300 transition hover:border-zinc-500 disabled:opacity-40"
          :disabled="store.saving"
          @click="saveGraph()"
        >
          <Save class="h-3.5 w-3.5" /> Save
        </button>

        <button
          class="flex items-center gap-1.5 rounded-lg bg-orange-500 px-3.5 py-1.5 text-[11px] font-bold text-white shadow-lg shadow-orange-500/25 transition hover:bg-orange-400 active:scale-95 disabled:opacity-50"
          :disabled="store.running"
          @click="runWorkflow"
        >
          <Loader2 v-if="store.running" class="h-3.5 w-3.5 animate-spin" />
          <Play v-else class="h-3.5 w-3.5" />
          {{ store.running ? (runningCount ? 'Running…' : 'Starting…') : 'Run' }}
        </button>
      </div>
    </header>

    <!-- editor body -->
    <div class="flex min-h-0 flex-1">
      <NodePalette :definitions="store.definitions" @add="(d) => addNodeFromDef(d)" />

      <div class="relative min-w-0 flex-1" @drop="onDrop" @dragover.prevent @dragenter.prevent @contextmenu.prevent>
        <ClientOnly>
          <VueFlow
            v-model:nodes="vfNodes"
            v-model:edges="vfEdges"
            class="bg-zinc-950"
            :delete-key-code="null"
            :min-zoom="0.2"
            :max-zoom="2"
            :connection-radius="24"
            :default-edge-options="{ type: 'smoothstep' }"
            fit-view-on-init
            selection-key-code
            :pan-on-drag="false"
          >
            <Background :gap="18" pattern-color="#1f2937" />
            <Controls position="bottom-left" />
            <MiniMap
              position="bottom-right"
              pannable
              zoomable
              node-color="#34343e"
              mask-color="rgba(9, 9, 11, 0.72)"
              node-border-radius="6"
            />
            <template #node-py8n="nodeProps">
              <PNodeCard :id="nodeProps.id" :data="nodeProps.data" />
            </template>
            <template #node-sticky="nodeProps">
              <PStickyNote :id="nodeProps.id" :data="nodeProps.data" :selected="nodeProps.selected" />
            </template>
          </VueFlow>
          <template #fallback>
            <div class="grid h-full place-items-center text-xs text-zinc-600">Loading canvas…</div>
          </template>
        </ClientOnly>

        <!-- selected edge hint -->
        <div
          v-if="selectedEdgeId"
          class="absolute left-1/2 top-3 z-10 flex -translate-x-1/2 items-center gap-2 rounded-full border border-zinc-700 bg-zinc-900/95 px-3 py-1.5 text-[11px] text-zinc-300 shadow-xl"
        >
          <Trash2 class="h-3 w-3 text-rose-400" />
          Connection selected - press Delete to remove
          <button class="font-semibold text-rose-400 hover:text-rose-300" @click="deleteSelectedEdge">Remove</button>
        </div>

        <!-- v19: multi-selection floating bar -->
        <div
          v-if="selectedCount >= 2"
          class="absolute bottom-4 left-1/2 z-10 flex -translate-x-1/2 items-center gap-3 rounded-full border border-orange-500/40 bg-zinc-900/95 px-3.5 py-1.5 text-[11px] text-zinc-300 shadow-xl"
        >
          <span class="font-semibold text-orange-300">{{ selectedCount }} nodes selected</span>
          <span class="text-zinc-600">Del removes · Ctrl+C copy · Ctrl+D duplicate · drag to move</span>
          <button class="font-semibold text-rose-400 hover:text-rose-300" @click="deleteSelectedNodesMulti">Delete all</button>
        </div>

        <!-- v19: sticky note toolbar button -->
        <button
          class="absolute left-3 top-3 z-10 flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-[11px] font-medium text-amber-300 transition hover:bg-amber-500/20"
          title="Add a sticky note (annotation - never executes)"
          @click="addStickyNote"
        >
          <StickyNote class="h-3.5 w-3.5" /> Sticky
        </button>

        <!-- v25: floating chat button (workflows with a Chat Trigger) -->
        <button
          v-if="hasChatTrigger"
          class="absolute bottom-3 right-3 z-10 flex h-11 w-11 items-center justify-center rounded-full bg-emerald-600 text-white shadow-lg shadow-emerald-950/60 transition hover:bg-emerald-500 hover:shadow-emerald-900/60 active:scale-95"
          :class="chatOpen && 'ring-2 ring-emerald-400/60 ring-offset-2 ring-offset-zinc-950'"
          title="Open chat - talk to this workflow (requires activation)"
          @click="chatOpen = !chatOpen"
        >
          <MessageCircle class="h-5 w-5" />
        </button>
      </div>

      <ConfigPanel
        :node="selectedNodeSpec"
        :definition="selectedDefinition"
        :credentials="store.credentials"
        :workflow-id="workflowId"
        :canvas-node-names="canvasNodeNames"
        :env-keys="envKeys"
        :run-test-step="runTestStep"
        :load-last-output="loadLastOutput"
        @update-param="updateParam"
        @update-settings="updateSettings"
        @toggle-disabled="toggleDisabled"
        @update-pinned="updatePinned"
        @rename="renameNode"
        @delete="deleteSelectedNode"
        @create-credential="createCredential"
      />
    </div>

    <!-- v20: right-click context menu -->
    <NodeContextMenu
      v-if="contextMenu"
      :x="contextMenu.x"
      :y="contextMenu.y"
      :items="contextMenuItems"
      @close="contextMenu = null"
    />

    <!-- v20: workflow settings modal -->
    <Teleport to="body">
      <div
        v-if="showSettings"
        class="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 backdrop-blur-sm"
        @click.self="showSettings = false"
      >
        <div class="w-[440px] max-w-[92vw] rounded-2xl border border-zinc-700 bg-zinc-900 p-5 shadow-2xl">
          <div class="mb-4 flex items-center justify-between">
            <h2 class="flex items-center gap-2 text-sm font-semibold text-zinc-100">
              <Settings2 class="h-4 w-4 text-orange-400" /> Workflow settings
            </h2>
            <button class="rounded-lg p-1 text-zinc-500 transition hover:text-zinc-200" title="Close" @click="showSettings = false">
              <X class="h-4 w-4" />
            </button>
          </div>

          <label class="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Description</label>
          <textarea
            v-model="settingsDraft!.description"
            :rows="3"
            placeholder="What does this workflow do? Shows on the dashboard and in search."
            class="mb-4 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-2 text-xs outline-none transition focus:border-orange-500/60"
          />

          <label class="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Execution data retention</label>
          <select
            v-model="settingsDraft!.retentionMode"
            class="mb-2 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none transition focus:border-orange-500/60"
          >
            <option value="inherit">Inherit global policy{{ globalRetentionDays != null ? ` (keep ${globalRetentionDays === 0 ? 'forever' : globalRetentionDays + ' days'})` : '' }}</option>
            <option value="keep">Keep forever (never purge)</option>
            <option value="days">Custom - purge after N days</option>
          </select>
          <div v-if="settingsDraft!.retentionMode === 'days'" class="mb-2 flex items-center gap-2">
            <input
              v-model.number="settingsDraft!.retentionDays"
              type="number"
              min="1"
              max="3650"
              class="w-24 rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-orange-500/60"
            />
            <span class="text-[11px] text-zinc-500">days (finished runs only)</span>
          </div>
          <p class="mb-4 text-[10px] leading-relaxed text-zinc-600">
            Overrides the global policy (Insights → Execution data retention). Running executions are never purged.
          </p>

          <div class="flex justify-end gap-2">
            <button
              class="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-zinc-500"
              @click="showSettings = false"
            >
              Cancel
            </button>
            <button
              class="rounded-lg bg-orange-500 px-3.5 py-1.5 text-xs font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
              :disabled="savingSettings"
              @click="saveSettings"
            >
              {{ savingSettings ? 'Saving…' : 'Save settings' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- v20: shortcut cheat sheet -->
    <ShortcutsOverlay v-if="showShortcuts" @close="showShortcuts = false" />

    <!-- v25: editor chat panel -->
    <ChatPanel
      :workflow-id="workflowId"
      :open="chatOpen && hasChatTrigger"
      :welcome-message="chatWelcome"
      @close="chatOpen = false"
    />

    <!-- executions drawer -->
    <ExecutionsDrawer
      v-model:open="drawerOpen"
      :executions="store.executions"
      :last-run="store.lastRun"
      :live-events="store.liveEvents"
      @select-run="selectRun"
      :style="{ height: drawerOpen ? '280px' : '40px' }"
    />

    <!-- toasts -->
    <div class="pointer-events-none fixed bottom-4 right-4 z-50 space-y-2">
      <div
        v-for="t in toasts"
        :key="t.id"
        class="pointer-events-auto flex items-center gap-2 rounded-xl border px-3.5 py-2 text-xs shadow-2xl backdrop-blur"
        :class="t.kind === 'error'
          ? 'border-rose-500/40 bg-rose-950/80 text-rose-200'
          : t.kind === 'success'
            ? 'border-emerald-500/40 bg-emerald-950/80 text-emerald-200'
            : 'border-zinc-700 bg-zinc-900/90 text-zinc-200'"
      >
        <AlertTriangle v-if="t.kind === 'error'" class="h-3.5 w-3.5" />
        <Check v-else-if="t.kind === 'success'" class="h-3.5 w-3.5" />
        <Zap v-else class="h-3.5 w-3.5" />
        {{ t.text }}
      </div>
    </div>

    <!-- version history modal (v13) -->
    <div
      v-if="showHistory"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="showHistory = false"
    >
      <div class="flex max-h-[80vh] w-full max-w-lg flex-col rounded-2xl border border-zinc-800 bg-zinc-900 shadow-2xl">
        <div class="flex items-center justify-between border-b border-zinc-800 px-5 py-4">
          <div>
            <h3 class="flex items-center gap-2 text-sm font-bold">
              <History class="h-4 w-4 text-orange-400" /> Version history
            </h3>
            <p v-if="store.versions" class="mt-0.5 text-[11px] text-zinc-500">
              {{ store.versions.versions.length }} of max {{ store.versions.max_versions }} snapshots · newest first
            </p>
          </div>
          <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200" title="Close" @click="showHistory = false">
            <X class="h-4 w-4" />
          </button>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto p-3">
          <div v-if="!store.versions" class="flex h-32 items-center justify-center text-sm text-zinc-500">
            <Loader2 class="mr-2 h-4 w-4 animate-spin" /> Loading…
          </div>
          <div v-else-if="store.versions.versions.length === 0" class="py-10 text-center text-sm text-zinc-500">
            No versions yet - they're created on every save.
          </div>
          <div v-else class="space-y-1.5">
            <div
              v-for="v in store.versions.versions"
              :key="v.version"
              class="flex items-center gap-3 rounded-xl border px-3.5 py-2.5 transition"
              :class="v.is_current
                ? 'border-orange-500/40 bg-orange-500/5'
                : 'border-zinc-800 bg-zinc-950/50 hover:border-zinc-700'"
            >
              <span
                class="w-10 shrink-0 rounded-lg px-1.5 py-1 text-center font-mono text-[11px] font-bold"
                :class="v.is_current ? 'bg-orange-500/15 text-orange-300' : 'bg-zinc-800 text-zinc-400'"
              >
                v{{ v.version }}
              </span>
              <div class="min-w-0 flex-1">
                <p class="truncate text-xs font-semibold" :class="v.is_current && 'text-orange-200'">
                  {{ v.name }}
                  <span v-if="v.is_current" class="ml-1.5 rounded-full bg-orange-500/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-orange-300">current</span>
                </p>
                <p class="mt-0.5 text-[10px] text-zinc-600">
                  {{ fmtVersionTime(v.created_at) }} · {{ v.node_count }} node{{ v.node_count === 1 ? '' : 's' }}
                </p>
              </div>
              <button
                v-if="!v.is_current"
                class="flex shrink-0 items-center gap-1 rounded-lg border border-zinc-700 px-2 py-1 text-[10px] font-semibold text-zinc-300 transition hover:border-orange-500/50 hover:text-orange-300 disabled:opacity-40"
                :disabled="restoring !== null"
                @click="doRestore(v.version)"
              >
                <Loader2 v-if="restoring === v.version" class="h-3 w-3 animate-spin" />
                <RotateCcw v-else class="h-3 w-3" />
                Restore
              </button>
            </div>
          </div>
        </div>

        <p class="border-t border-zinc-800 px-5 py-3 text-[10px] leading-relaxed text-zinc-600">
          Snapshots are taken on create and every content save (graph, name, description).
          Restoring never destroys history - it lands as a new version on top.
        </p>
      </div>
    </div>
  </div>
</template>
