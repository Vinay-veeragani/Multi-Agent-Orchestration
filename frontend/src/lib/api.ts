// Server-only client for the orchestrator's HTTP API.
//
// Imported only from Server Components and Route Handlers -- never from a
// "use client" file. That is what keeps ORCHESTRATOR_API_KEY out of the
// browser bundle: every request the browser itself makes goes to one of
// this app's own same-origin routes instead (see `src/app/api/`), which use
// these same functions server-side.

export interface AgentSummary {
  id: string;
  name: string;
  description: string;
}

export interface WorkflowSummary {
  id: string;
  name: string;
  dynamic: boolean;
}

export interface ExecutionSummary {
  id: string;
  workflow_id: string;
  status: string;
  task: string;
  cost_usd: number | null;
  total_tokens: number | null;
  created_at: string;
  completed_at: string | null;
}

export interface NodeState {
  node_id: string;
  status: string;
  attempts: number;
  error: Record<string, unknown> | null;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
}

export interface BudgetUsage {
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  agent_steps: number;
  tool_calls: number;
  retries: number;
  llm_calls: number;
}

export interface Budget {
  max_cost_usd: number | null;
  max_tokens: number | null;
  max_duration_seconds: number | null;
  max_agent_steps: number | null;
  max_tool_calls: number | null;
}

export interface ExecutionState {
  execution_id: string;
  workflow_id: string;
  task: { description: string; success_criteria: string | null };
  status: string;
  current_nodes: string[];
  node_states: Record<string, NodeState>;
  final_output: string | null;
  pending_approval_id: string | null;
  budget: Budget;
  budget_usage: BudgetUsage;
}

export interface ExecutionEvent {
  id: string;
  execution_id: string;
  sequence: number;
  type: string;
  severity: string;
  node_id: string | null;
  agent_id: string | null;
  tool: string | null;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function config() {
  const baseUrl = process.env.ORCHESTRATOR_API_URL ?? "http://127.0.0.1:8000";
  const apiKey = process.env.ORCHESTRATOR_API_KEY ?? "";
  return { baseUrl, apiKey };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { baseUrl, apiKey } = config();
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
      ...init?.headers,
    },
    // Execution state changes frequently; a dashboard should never serve a
    // stale cached read of it.
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.text();
    throw new ApiError(response.status, body || response.statusText);
  }
  return (await response.json()) as T;
}

export function listAgents(): Promise<AgentSummary[]> {
  return request<AgentSummary[]>("/agents");
}

export function listWorkflows(): Promise<WorkflowSummary[]> {
  return request<WorkflowSummary[]>("/workflows");
}

export function listExecutions(params?: {
  limit?: number;
  statusFilter?: string;
}): Promise<ExecutionSummary[]> {
  const query = new URLSearchParams();
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.statusFilter) query.set("status_filter", params.statusFilter);
  const suffix = query.toString() ? `?${query}` : "";
  return request<ExecutionSummary[]>(`/executions${suffix}`);
}

export function getExecution(executionId: string): Promise<ExecutionState> {
  return request<ExecutionState>(`/executions/${executionId}`);
}

export function getExecutionEvents(executionId: string): Promise<ExecutionEvent[]> {
  return request<ExecutionEvent[]>(`/executions/${executionId}/events`);
}

export interface CreateExecutionInput {
  task: string;
  successCriteria?: string;
  workflowId?: string;
}

export function createExecution(
  input: CreateExecutionInput,
): Promise<{ execution_id: string; workflow_id: string; status: string }> {
  return request("/executions", {
    method: "POST",
    body: JSON.stringify({
      task: input.task,
      success_criteria: input.successCriteria,
      workflow_id: input.workflowId,
    }),
  });
}

export function cancelExecution(executionId: string): Promise<unknown> {
  return request(`/executions/${executionId}/cancel`, { method: "POST" });
}

export interface ApprovalRequest {
  id: string;
  execution_id: string;
  node_id: string | null;
  action: string;
  agent_id: string | null;
  tool: string | null;
  parameters: Record<string, unknown>;
  risk_level: string;
  risk_reason: string;
  status: string;
  requested_at: string;
  expires_at: string | null;
}

export function listPendingApprovals(executionId: string): Promise<ApprovalRequest[]> {
  return request<ApprovalRequest[]>(`/executions/${executionId}/approvals`);
}

export interface AgentInvocation {
  id: string;
  node_id: string | null;
  agent_id: string;
  attempt: number;
  status: string;
  model_key: string | null;
  tokens: number;
  cost_usd: number;
  tool_calls: number;
  duration_seconds: number | null;
  confidence: number | null;
}

export interface ToolInvocation {
  id: string;
  node_id: string | null;
  agent_id: string | null;
  tool: string;
  attempt: number;
  status: string;
  policy_effect: string;
  duration_seconds: number | null;
}

export function listAgentInvocations(executionId: string): Promise<AgentInvocation[]> {
  return request<AgentInvocation[]>(`/executions/${executionId}/agent-invocations`);
}

export function listToolInvocations(executionId: string): Promise<ToolInvocation[]> {
  return request<ToolInvocation[]>(`/executions/${executionId}/tool-invocations`);
}

export function decideApproval(
  executionId: string,
  decision: "approve" | "reject",
  input: { approvalId?: string; by: string; note?: string },
): Promise<ApprovalRequest> {
  return request<ApprovalRequest>(`/executions/${executionId}/${decision}`, {
    method: "POST",
    body: JSON.stringify({ approval_id: input.approvalId, by: input.by, note: input.note }),
  });
}

export interface BenchmarkRunSummary {
  id: string;
  git_sha: string | null;
  scenario_count: number;
  started_at: string;
  completed_at: string;
}

export interface ArmMetrics {
  arm: string;
  scenarios_run: number;
  scenarios_passed: number;
  routing_accuracy: number | null;
  tool_selection_accuracy: number | null;
  tool_argument_validity: number | null;
  recovery_success_rate: number | null;
  avg_agent_steps: number;
  avg_latency_seconds: number;
  p50_latency_seconds: number;
  p95_latency_seconds: number;
  p99_latency_seconds: number;
  total_cost_usd: number;
  total_tokens: number;
  avg_max_parallelism: number;
}

export interface ScenarioResult {
  scenario_id: string;
  category: string;
  arm: string;
  passed: boolean;
  final_status: string | null;
  failures: string[];
  latency_seconds: number;
  cost_usd: number;
}

export interface BenchmarkReport {
  id: string;
  started_at: string;
  completed_at: string;
  git_sha: string | null;
  environment: Record<string, unknown>;
  provider_note: string;
  scenario_count: number;
  arms: ArmMetrics[];
  results: ScenarioResult[];
}

export function listBenchmarkRuns(limit = 20): Promise<BenchmarkRunSummary[]> {
  return request<BenchmarkRunSummary[]>(`/benchmarks?limit=${limit}`);
}

export function getBenchmarkRun(reportId: string): Promise<BenchmarkReport> {
  return request<BenchmarkReport>(`/benchmarks/${reportId}`);
}

export { config as orchestratorConfig, ApiError };
