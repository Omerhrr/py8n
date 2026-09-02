// Py8n global editor store - definitions, credentials, execution progress.
// The canvas graph itself lives in the editor page (Vue Flow owns positions);
// this store holds everything shared across components.
import { defineStore } from 'pinia'
import type {
  Credential,
  CredentialTestResult,
  CredentialUsage,
  EnvVariable,
  ExecEvent,
  ExecutionDetail,
  ExecutionSummary,
  NodeDefinition,
  NodeRunStatus,
  NodeTestResult,
  Workflow,
  WorkflowListItem,
  WorkflowScheduleInfo,
  WorkflowVersionList,
} from '~/types/node'

export const usePy8nStore = defineStore('py8n', () => {
  const { api, wsUrl } = useApi()

  // ------------------------------------------------------------------
  // Static-ish data
  // ------------------------------------------------------------------
  const definitions = ref<NodeDefinition[]>([])
  const definitionsLoaded = ref(false)
  const credentials = ref<Credential[]>([])

  const definitionsByCategory = computed(() => {
    const grouped: Record<string, NodeDefinition[]> = {}
    for (const d of definitions.value) {
      grouped[d.category] = grouped[d.category] || []
      grouped[d.category].push(d)
    }
    return grouped
  })

  function definitionFor(type: string): NodeDefinition | undefined {
    return definitions.value.find((d) => d.type === type)
  }

  async function loadDefinitions(force = false) {
    if (definitionsLoaded.value && !force) return
    const res = await api.get<{ definitions: NodeDefinition[] }>('/node-definitions')
    definitions.value = res.definitions
    definitionsLoaded.value = true
  }

  async function loadCredentials() {
    credentials.value = await api.get<Credential[]>('/credentials')
  }

  async function createCredential(body: { name: string; type: string; data: Record<string, any> }) {
    const cred = await api.post<Credential>('/credentials', body)
    credentials.value.unshift(cred)
    return cred
  }

  async function deleteCredential(id: string) {
    await api.del(`/credentials/${id}`)
    credentials.value = credentials.value.filter((c) => c.id !== id)
  }

  async function updateCredential(
    id: string,
    body: { name?: string; data?: Record<string, any> }
  ) {
    const updated = await api.patch<Credential>(`/credentials/${id}`, body)
    const idx = credentials.value.findIndex((c) => c.id === id)
    if (idx >= 0) credentials.value.splice(idx, 1, updated)
    return updated
  }

  async function testCredential(id: string, testUrl?: string) {
    return await api.post<CredentialTestResult>(`/credentials/${id}/test`, {
      test_url: testUrl || null,
    })
  }

  async function getCredentialUsage(id: string) {
    return await api.get<CredentialUsage>(`/credentials/${id}/usage`)
  }

  // ------------------------------------------------------------------
  // Workflow under edit
  // ------------------------------------------------------------------
  const workflow = ref<Workflow | null>(null)
  const dirty = ref(false)
  const saving = ref(false)
  const lastSaveError = ref<string | null>(null)

  function markDirty() {
    dirty.value = true
  }

  async function loadWorkflow(id: string) {
    workflow.value = await api.get<Workflow>(`/workflows/${id}`)
    dirty.value = false
    return workflow.value
  }

  async function save(graph: { nodes: any[]; edges: any[] }) {
    if (!workflow.value) return
    saving.value = true
    lastSaveError.value = null
    try {
      workflow.value = await api.put<Workflow>(`/workflows/${workflow.value.id}`, {
        name: workflow.value.name,
        description: workflow.value.description,
        is_active: workflow.value.is_active,
        graph,
      })
      dirty.value = false
    } catch (e: any) {
      lastSaveError.value = e?.data?.detail || e?.message || 'Save failed'
      throw e
    } finally {
      saving.value = false
    }
  }

  async function toggleActive() {
    if (!workflow.value) return
    const target = !workflow.value.is_active
    // Dedicated lifecycle endpoints run pre-flight validation (e.g. a broken
    // cron expression blocks activation with a 400) unlike a plain PUT.
    const info = await api.post<WorkflowScheduleInfo>(
      `/workflows/${workflow.value.id}/${target ? 'activate' : 'deactivate'}`,
    )
    workflow.value.is_active = info.is_active
    scheduleInfo.value = info
    return info
  }

  // ------------------------------------------------------------------
  // Schedule introspection (v7)
  // ------------------------------------------------------------------
  const scheduleInfo = ref<WorkflowScheduleInfo | null>(null)

  async function loadScheduleInfo() {
    if (!workflow.value) return
    try {
      scheduleInfo.value = await api.get<WorkflowScheduleInfo>(
        `/workflows/${workflow.value.id}/schedule`,
      )
    } catch {
      scheduleInfo.value = null
    }
    return scheduleInfo.value
  }

  // ------------------------------------------------------------------
  // Workflow list + error-workflow binding (v8)
  // ------------------------------------------------------------------
  const workflows = ref<WorkflowListItem[]>([])

  async function loadWorkflows() {
    workflows.value = await api.get<WorkflowListItem[]>('/workflows')
  }

  async function setErrorWorkflow(handlerId: string | null) {
    if (!workflow.value) return
    // "" clears the binding, null/str binds - server validates existence + self-bind
    workflow.value = await api.put<Workflow>(`/workflows/${workflow.value.id}`, {
      error_workflow_id: handlerId ?? '',
    })
  }

  // ------------------------------------------------------------------
  // Tags (v12) - server normalizes (trim/lower/dedupe, max 10×32)
  // ------------------------------------------------------------------
  async function setTags(tags: string[]) {
    if (!workflow.value) return
    workflow.value = await api.put<Workflow>(`/workflows/${workflow.value.id}`, { tags })
  }

  // ------------------------------------------------------------------
  // Version history (v13)
  // ------------------------------------------------------------------
  const versions = ref<WorkflowVersionList | null>(null)

  async function loadVersions() {
    if (!workflow.value) return
    versions.value = await api.get<WorkflowVersionList>(
      `/workflows/${workflow.value.id}/versions`,
    )
  }

  async function restoreVersion(version: number) {
    if (!workflow.value) return
    workflow.value = await api.post<Workflow>(
      `/workflows/${workflow.value.id}/versions/${version}/restore`,
    )
    await loadVersions()
  }

  // ------------------------------------------------------------------
  // Execution + live progress
  // ------------------------------------------------------------------
  const running = ref(false)
  const activeExecutionId = ref<string | null>(null)
  const nodeStates = ref<Record<string, NodeRunStatus>>({})
  const liveEvents = ref<ExecEvent[]>([])
  const lastRun = ref<ExecutionDetail | null>(null)
  const executions = ref<ExecutionSummary[]>([])
  let ws: WebSocket | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null

  async function loadExecutions(limit = 20) {
    if (!workflow.value) return
    executions.value = await api.get<ExecutionSummary[]>(
      `/executions?workflow_id=${workflow.value.id}&limit=${limit}`,
    )
  }

  async function loadExecution(id: string) {
    lastRun.value = await api.get<ExecutionDetail>(`/executions/${id}`)
    if (lastRun.value.status !== 'running') {
      for (const run of lastRun.value.node_runs || []) {
        nodeStates.value[run.node_id] = run.status as NodeRunStatus
      }
    }
    return lastRun.value
  }

  function resetNodeStates() {
    nodeStates.value = {}
    liveEvents.value = []
  }

  function handleEvent(event: ExecEvent) {
    if (event.event === 'history' && Array.isArray(event.events)) {
      for (const e of event.events) handleEvent(e)
      return
    }
    liveEvents.value.push(event)
    if (event.node_id && event.event === 'node_started') {
      nodeStates.value[event.node_id] = 'running'
    }
    if (event.node_id && event.event === 'node_finished') {
      nodeStates.value[event.node_id] = (event.status as NodeRunStatus) || 'idle'
    }
  }

  function startPolling(executionId: string) {
    stopPolling()
    pollTimer = setInterval(async () => {
      try {
        const detail = await api.get<ExecutionDetail>(`/executions/${executionId}`)
        for (const run of detail.node_runs || []) {
          if (run.status !== 'running') nodeStates.value[run.node_id] = run.status as NodeRunStatus
        }
        if (detail.status !== 'running') {
          stopPolling()
          await finishRun(executionId)
        }
      } catch {
        /* keep polling; backend may still be writing */
      }
    }, 1500)
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  async function finishRun(executionId: string) {
    running.value = false
    await loadExecution(executionId)
    await loadExecutions()
  }

  async function runWorkflow(payload?: Record<string, any> | null) {
    if (!workflow.value) return
    resetNodeStates()
    running.value = true
    try {
      const acc = await api.post<{ execution_id: string }>(
        `/workflows/${workflow.value.id}/run`,
        payload ? { payload } : {},
      )
      activeExecutionId.value = acc.execution_id
      connectProgress(acc.execution_id)
    } catch (e: any) {
      running.value = false
      throw e
    }
  }

  function connectProgress(executionId: string) {
    // a previous run's socket may still be open - drop it or it leaks (each
    // one keeps a slot on the backend event bus)
    closeSocket()
    try {
      ws = new WebSocket(wsUrl(executionId))
    } catch {
      startPolling(executionId)
      return
    }
    const wsOk = ws
    // If the socket never opens (gateway restrictions), fall back to polling.
    const fallback = setTimeout(() => {
      if (!wsOk || wsOk.readyState !== WebSocket.OPEN) startPolling(executionId)
    }, 2500)

    ws.onopen = () => clearTimeout(fallback)
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as ExecEvent
        handleEvent(frame)
        if (frame.event === 'execution_finished') {
          stopPolling()
          finishRun(executionId)
          closeSocket()
        }
      } catch {
        /* ignore malformed frame */
      }
    }
    ws.onerror = () => startPolling(executionId)
    ws.onclose = () => clearTimeout(fallback)
  }

  function closeSocket() {
    if (ws) {
      try {
        ws.close()
      } catch {
        /* noop */
      }
      ws = null
    }
  }

  // ------------------------------------------------------------------
  // Global execution history (Executions page)
  // ------------------------------------------------------------------
  const allExecutions = ref<ExecutionSummary[]>([])
  const rerunning = ref<string | null>(null)

  async function loadAllExecutions(opts?: {
    workflow_id?: string
    status?: string
    limit?: number
  }) {
    const q = new URLSearchParams()
    if (opts?.workflow_id) q.set('workflow_id', opts.workflow_id)
    if (opts?.status) q.set('status', opts.status)
    q.set('limit', String(opts?.limit ?? 50))
    allExecutions.value = await api.get<ExecutionSummary[]>(`/executions?${q.toString()}`)
  }

  async function rerunExecution(id: string) {
    rerunning.value = id
    try {
      return await api.post<{ execution_id: string; rerun_of: string }>(
        `/executions/${id}/rerun`,
      )
    } finally {
      rerunning.value = null
    }
  }

  async function resumeExecution(id: string, token: string, payload: any) {
    return api.post<{ execution_id: string; resume_node?: string }>(
      `/executions/${id}/resume`,
      { token, payload },
    )
  }

  async function deleteExecution(id: string) {
    await api.del(`/executions/${id}`)
    allExecutions.value = allExecutions.value.filter((e) => e.id !== id)
    if (selectedExecution.value?.id === id) selectedExecution.value = null
  }

  const selectedExecution = ref<ExecutionDetail | null>(null)
  const loadingDetail = ref(false)
  const cancelling = ref<string | null>(null)

  async function cancelExecution(id: string) {
    cancelling.value = id
    try {
      return await api.post<{ execution_id: string; status: string }>(
        `/executions/${id}/cancel`,
      )
    } finally {
      cancelling.value = null
    }
  }

  async function loadExecutionDetail(id: string) {
    loadingDetail.value = true
    try {
      selectedExecution.value = await api.get<ExecutionDetail>(`/executions/${id}`)
      return selectedExecution.value
    } finally {
      loadingDetail.value = false
    }
  }

  // ------------------------------------------------------------------
  // Environment variables (v15) - {{ env.KEY }} in any template field
  // ------------------------------------------------------------------
  const envVars = ref<EnvVariable[]>([])
  const envVarsLoaded = ref(false)

  async function loadEnvVars(force = false) {
    if (envVarsLoaded.value && !force) return
    envVars.value = await api.get<EnvVariable[]>('/env-vars')
    envVarsLoaded.value = true
  }

  async function createEnvVar(body: { key: string; value: string; is_secret: boolean; description: string }) {
    const row = await api.post<EnvVariable>('/env-vars', body)
    // keep the list key-sorted like the server does
    const idx = envVars.value.findIndex((v) => v.key.toLowerCase() > row.key.toLowerCase())
    if (idx === -1) envVars.value.push(row)
    else envVars.value.splice(idx, 0, row)
    return row
  }

  async function updateEnvVar(id: string, body: { value?: string; is_secret?: boolean; description?: string }) {
    const updated = await api.put<EnvVariable>(`/env-vars/${id}`, body)
    const idx = envVars.value.findIndex((v) => v.id === id)
    if (idx >= 0) envVars.value.splice(idx, 1, updated)
    return updated
  }

  async function deleteEnvVar(id: string) {
    await api.del(`/env-vars/${id}`)
    envVars.value = envVars.value.filter((v) => v.id !== id)
  }

  // ------------------------------------------------------------------
  // Webhook info
  // ------------------------------------------------------------------
  const webhookUrl = ref<string | null>(null)
  async function loadWebhookUrl() {
    if (!workflow.value) return
    const info = await api.get<{ url: string; has_webhook_node: boolean }>(
      `/workflows/${workflow.value.id}/webhook-url`,
    )
    webhookUrl.value = info.has_webhook_node ? info.url : null
  }

  // ------------------------------------------------------------------
  // v17 test step - run ONE node in isolation, result returned inline.
  // Nothing is persisted server-side (no execution log).
  // ------------------------------------------------------------------
  async function testNodeStep(workflowId: string, nodeId: string, items: any) {
    return await api.post<NodeTestResult>(
      `/workflows/${workflowId}/nodes/${nodeId}/test`,
      { items: items ?? null },
    )
  }

  return {
    definitions,
    definitionsLoaded,
    definitionsByCategory,
    definitionFor,
    loadDefinitions,
    credentials,
    loadCredentials,
    createCredential,
    deleteCredential,
    updateCredential,
    testCredential,
    getCredentialUsage,
    workflow,
    dirty,
    saving,
    lastSaveError,
    markDirty,
    loadWorkflow,
    save,
    toggleActive,
    scheduleInfo,
    loadScheduleInfo,
    running,
    activeExecutionId,
    nodeStates,
    liveEvents,
    lastRun,
    executions,
    loadExecutions,
    loadExecution,
    runWorkflow,
    resetNodeStates,
    loadWebhookUrl,
    webhookUrl,
    allExecutions,
    loadAllExecutions,
    rerunExecution,
    resumeExecution,
    deleteExecution,
    rerunning,
    cancelExecution,
    cancelling,
    selectedExecution,
    loadingDetail,
    loadExecutionDetail,
    workflows,
    loadWorkflows,
    setErrorWorkflow,
    setTags,
    versions,
    loadVersions,
    restoreVersion,
    envVars,
    envVarsLoaded,
    loadEnvVars,
    createEnvVar,
    updateEnvVar,
    deleteEnvVar,
    testNodeStep,
  }
})
