// Py8n shared frontend types (mirror of backend schemas)

export interface HandleSpec {
  key: string
  label: string
}

export interface ParamProperty {
  type?: string
  description?: string
  default?: any
  enum?: string[]
  widget?: string
  rows?: number
  language?: string
  placeholder?: string
  options?: string[]
  anyOf?: { type?: string }[]
  [key: string]: any
}

export interface NodeDefinition {
  type: string
  name: string
  description: string
  category: 'triggers' | 'actions' | 'logic' | 'ai'
  icon: string
  color: string
  inputs: HandleSpec[]
  outputs: HandleSpec[]
  parameters_schema: {
    properties?: Record<string, ParamProperty>
    required?: string[]
  }
  defaults: Record<string, any>
}

export interface NodeSettings {
  retry_on_fail: boolean
  max_retries: number
  retry_wait_ms: number
  continue_on_fail: boolean
  // v38 resilience pack
  timeout_ms?: number
  fallback_enabled?: boolean
  fallback_value?: any
}

export interface QueueItem {
  execution_id: string
  workflow_id: string
  workflow_name?: string | null
  trigger_type?: string | null
  status: string
  started_at?: string | null
  duration_ms?: number | null
  nodes_done?: number | null
  nodes_total?: number | null
  current_node?: string | null
}

export interface NodeSpec {
  id: string
  type: string
  name: string
  position: { x: number; y: number }
  parameters: Record<string, any>
  settings?: NodeSettings
  disabled?: boolean // skipped at run time, input passes through
  pinned_data?: any | null // v17: pinned output - returned without executing (manual runs + test step)
}

export interface EdgeSpec {
  id: string
  source: string
  target: string
  sourceHandle: string
  targetHandle: string
}

export interface GraphSpec {
  nodes: NodeSpec[]
  edges: EdgeSpec[]
}

export interface Workflow {
  id: string
  name: string
  description: string
  graph: GraphSpec
  is_active: boolean
  error_workflow_id?: string | null
  tags?: string[]
  folder_id?: string | null
  retention_days?: number | null
  created_at: string
  updated_at: string
}

export interface WorkflowListItem {
  id: string
  name: string
  description: string
  is_active: boolean
  node_count: number
  trigger_types: string[]
  schedule_summary?: string | null
  next_run_at?: string | null
  error_workflow_id?: string | null
  error_workflow_name?: string | null
  tags: string[]
  folder_id?: string | null
  folder_name?: string | null
  created_at: string
  updated_at: string
}

export interface Folder {
  id: string
  name: string
  parent_id: string | null
  workflow_count: number
  total_count: number
  created_at: string
  updated_at: string
}

export interface ScheduleEntry {
  node_id: string
  node_name: string
  mode: string
  cron?: string | null
  interval_seconds?: number | null
  summary: string
  next_runs: string[]
  error?: string | null
}

export interface WorkflowScheduleInfo {
  workflow_id: string
  is_active: boolean
  schedules: ScheduleEntry[]
  next_run_at?: string | null
}

export interface GlobalScheduleEntry extends ScheduleEntry {
  workflow_id: string
  workflow_name: string
  is_active: boolean
}

export interface WorkflowTemplate {
  id: string
  name: string
  description: string
  category: string
  icon: string
  docs: string
  badge?: string | null
  tags?: string[]
  accent?: string
  node_count: number
  node_types: string[]
}

// ------------------------------------------------------------- v11 insights
export interface InsightWindow {
  days: number
  since: string
  until: string
  workflow_id: string | null
}

export interface InsightSummary {
  total: number
  success: number
  error: number
  waiting: number
  cancelled: number
  running: number
  success_rate: number
  avg_duration_ms: number
  node_runs_total: number
}

export interface InsightTimelineBucket {
  date: string
  total: number
  success: number
  error: number
  waiting: number
  cancelled: number
  running: number
}

export interface InsightTopWorkflow {
  workflow_id: string
  workflow_name: string
  runs: number
  success: number
  errors: number
  success_rate: number
  avg_duration_ms: number
}

export interface InsightNodeStat {
  node_type: string
  runs: number
  errors: number
  skipped: number
  error_rate: number
  avg_duration_ms: number
}

export interface InsightsPayload {
  window: InsightWindow
  summary: InsightSummary
  timeline: InsightTimelineBucket[]
  top_workflows: InsightTopWorkflow[]
  node_stats: InsightNodeStat[]
  trigger_breakdown: Record<string, number>
}

// ------------------------------------------------------- v13 version history
export interface WorkflowVersionSummary {
  version: number
  name: string
  description: string
  node_count: number
  created_at: string | null
  is_current: boolean
}

export interface WorkflowVersionList {
  workflow_id: string
  max_versions: number
  latest: number | null
  versions: WorkflowVersionSummary[]
}

export interface WorkflowVersionDetail extends WorkflowVersionSummary {
  workflow_id: string
  graph: GraphSpec
  tags: string[]
}

export interface NodeRun {
  node_id: string
  node_type: string
  node_name: string
  status: 'success' | 'error' | 'skipped' | 'running' | 'waiting'
  started_at?: string
  duration_ms?: number
  input?: any
  output?: any
  error?: string | null
  batch_index?: number // present on loop-body node runs (0-based)
  pinned?: boolean // v17: output came from pinned data (mock, not executed)
}

export interface ExecutionSummary {
  id: string
  workflow_id: string
  workflow_name?: string | null
  status: 'running' | 'success' | 'error' | 'waiting' | 'cancelled'
  trigger_type: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  error: string | null
}

export interface ResumeInfo {
  method: 'POST'
  url: string
  token: string
  node_id?: string
}

export interface ExecutionDetail extends ExecutionSummary {
  node_runs: NodeRun[]
  trigger_payload?: Record<string, any>
  resume?: ResumeInfo | null
}

export interface Credential {
  id: string
  name: string
  type: string
  masked_hint: string
  created_at: string
}

export interface CredentialTestResult {
  ok: boolean
  message: string
  latency_ms: number
  probed_at: string
}

export interface CredentialUsageWorkflow {
  id: string
  name: string
  active: boolean
  nodes: string[]
}

export interface CredentialUsage {
  credential_id: string
  workflow_count: number
  workflows: CredentialUsageWorkflow[]
}

export interface ExecEvent {
  event: string
  execution_id?: string
  node_id?: string
  node_name?: string
  node_type?: string
  status?: string
  duration_ms?: number
  output?: any
  error?: string | null
  ts?: string
  events?: ExecEvent[] // history frames
  [key: string]: any
}

export type NodeRunStatus = 'idle' | 'running' | 'success' | 'error' | 'skipped' | 'waiting'

// ------------------------------------------------------------------ v15 env vars
export interface EnvVariable {
  id: string
  key: string
  /** Plaintext value - null when the row is a secret (write-only). */
  value: string | null
  is_secret: boolean
  description: string
  updated_at: string
}

// ------------------------------------------------------ v17 pin data + test step
export interface NodeTestResult {
  ok: boolean
  status: 'success' | 'error'
  node_id?: string
  node_type?: string
  output?: any
  outputs?: Record<string, any> | null
  error?: string | null
  duration_ms?: number
  pinned_used?: boolean
}
