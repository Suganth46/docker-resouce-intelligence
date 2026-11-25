// API client — typed wrappers for all DRI backend endpoints

const BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';
const API_KEY = import.meta.env.VITE_API_KEY;

async function request<T>(path: string, params?: Record<string, string | number>, method = 'GET'): Promise<T> {
  const url = new URL(`${BASE}${path}`);
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, String(v)));
  }
  const headers: Record<string, string> = {};
  if (API_KEY) {
    headers['X-Api-Key'] = API_KEY;
  }
  const res = await fetch(url.toString(), {
    method,
    headers: Object.keys(headers).length > 0 ? headers : undefined,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.detail ?? `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Types ──────────────────────────────────────────────────────────────────

export interface HealthData {
  status: string;
  version: string;
  timestamp: string;
  uptime_seconds: number;
  metrics_processed: number;
  alerts_generated: number;
  actions_executed: number;
}

export interface ContainerMetrics {
  container_id: string;
  container_name: string;
  timestamp: string;
  cpu_percent: number;
  cpu_percent_5m: number;
  cpu_percent_15m: number;
  cpu_percent_60m: number;
  memory_percent: number;
  memory_percent_5m: number;
  memory_percent_15m: number;
  memory_percent_60m: number;
  memory_growth_rate_1m: number;
  memory_growth_rate_5m: number;
  is_anomaly: boolean;
  anomaly_type?: string;
  anomaly_detail?: string;
  /** "running" | "stopped" — populated by the DRI status-poll loop. */
  container_status?: string;
  net_io_in: number;
  net_io_out: number;
  block_io_read: number;
  block_io_write: number;
  pids: number;
  restart_count?: number;
  log_errors_1m?: number;
  log_errors_5m?: number;
  log_warnings_5m?: number;
  log_crash_signals_5m?: number;
  log_new_templates_5m?: number;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  message: string;
  template_id: string | null;
}

export interface LogsData {
  timestamp: string;
  container_id: string;
  logs: LogEntry[];
  total_returned: number;
}

interface LogTemplate {
  template_id: string;
  template: string;
  level: string;
  count: number;
  first_seen: string;
  sample: string;
}

export type LLMState = 'disabled' | 'unavailable' | 'model_missing' | 'pending' | 'ready';
export interface LLMSummary {
  root_cause: string;
  confidence: 'low' | 'medium' | 'high';
  suggested_actions: string[];
  evidence_used: string[];
  model: string;
  generated_at: string;
  latency_ms: number;
}
export interface LLMStatus {
  enabled: boolean;
  status: LLMState;
  model: string | null;
  base_url_host: string | null;
  queue_depth: number;
  jobs_done: number;
  jobs_dropped: number;
  avg_latency_ms: number;
}
export interface ExplainData {
  llm_status: LLMState;
  llm_summary: LLMSummary | null;
  container_id: string;
  window_minutes: number;
  generated_at: string;
  error_count: number;
  top_templates: LogTemplate[];
  new_templates: LogTemplate[];
  crash_signals: Record<string, number>;
  related_events: { action: string; timestamp: string; attributes: Record<string, unknown> }[];
  coverage: { collection?: { state?: string; last_line_ts?: string | null } | null; logs_status: string; log_lines_in_window: number; events_in_window: number; metrics_available: boolean; window_minutes: number };
  active_anomaly: { type: string; detail: string } | null;
  metric_context: { cpu_percent_5m: number | null; memory_percent_5m: number | null; memory_percent: number | null; memory_growth_rate_5m: number | null; cpu_percent: number | null };
  verdict: string;
  confidence: 'low' | 'medium' | 'high';
  reasons: string[];
}

export interface MetricsList {
  timestamp: string;
  containers: ContainerMetrics[];
  total_count: number;
}

export interface Alert {
  alert_id: string;
  container_id: string;
  container_name: string;
  timestamp: string;
  alert_type: string;
  metric: string;
  current_value: number;
  threshold: number;
  message: string;
  acknowledged: boolean;
}

export interface AlertsData {
  timestamp: string;
  alerts: Alert[];
  total_count: number;
}

export interface ActionRecord {
  action_id: string;
  container_id: string;
  container_name: string;
  timestamp: string;
  action_type: string;
  reason: string;
  success: boolean;
  error_message?: string;
}

export interface ActionsData {
  timestamp: string;
  actions: ActionRecord[];
  total_count: number;
}

export interface TrendEvidence {
  trend_label: string;             // e.g. "Suspected memory leak" | "Memory rising · early observation" | "No sustained leak trend" | "Memory stable" | "Trend evidence unavailable"
  slope_pct_per_hour?: number;     // %/hr — matches LEAK_SLOPE_THRESHOLD
  slope_pct_per_min: number;       // %/min — same unit as ContainerDetail Growth display
  distinct_samples: number;
  observation_minutes: number;
  populated_growing_windows: number;
  populated_windows_total: number;
  consecutive_growing_windows?: number;
  evidence_note: string;
}

export interface Recommendation {
  container_id: string;
  container_name: string;
  recommended_cpu_pct: number;
  recommended_memory_bytes: number;
  p95_cpu: number;
  p95_memory_bytes: number;
  p95_memory_pct: number;
  sample_count: number;
  lookback_days: number;
  lookback_actual_days: number;
  observation_minutes?: number | null;
  safety_margin: number;
  data_completeness?: string;
  confidence_tier: string;   // "low" | "medium" | "high"
  is_memory_leak: boolean;
  memory_leak_slope_pct_per_hour: number;
  /** Phase 7E: null/absent in legacy cached payloads */
  trend_evidence?: TrendEvidence | null;
  is_idle: boolean;
  idle_recommendation?: string;
  computed_at?: string;
}

export interface RecommendationsData {
  timestamp: string;
  recommendations: Recommendation[];
  insufficient: { container_id: string; min_samples_required: number; samples_available: number }[];
  pending: string[];
  total_count: number;
}

export interface ForecastData {
  container_id: string;
  metric: string;
  horizon_hours: number;
  current_value: number;
  predicted_value: number;
  confidence_low: number;
  confidence_high: number;
  predicted_at: string;
  method_used: string;
  sample_count: number;
  computed_at?: string;
}

export interface PolicyRule {
  metric: string;
  operator: string;
  value: number;
  duration_seconds: number;
  action: string;
}

export interface Policy {
  name: string;
  enabled: boolean;
  selector: Record<string, string>;
  rules: PolicyRule[];
}

export interface PoliciesData {
  timestamp: string;
  policies: Policy[];
  total_count: number;
}

export interface DockerEvent {
  id: string;
  container_id: string;
  container_name: string;
  event_type: string;
  action: string;
  timestamp: string;
  attributes: Record<string, any>;
}

export interface EventsData {
  timestamp: string;
  events: DockerEvent[];
  total_count: number;
}

// ── API Functions ──────────────────────────────────────────────────────────

export const api = {
  explainAi: (container_id: string) =>
    request<{status: LLMState}>(`/api/v1/containers/${encodeURIComponent(container_id)}/explain/ai`, undefined, 'POST'),
  llmStatus: () => request<LLMStatus>('/api/v1/llm/status'),
  logs: (container_id: string, minutes = 15, level?: string, limit = 200) =>
    request<LogsData>(`/api/v1/logs/${encodeURIComponent(container_id)}`, { minutes, limit, ...(level ? { level } : {}) }),
  explain: (container_id: string, window_minutes = 10) =>
    request<ExplainData>(`/api/v1/containers/${encodeURIComponent(container_id)}/explain`, { window_minutes }),
  health: () => request<HealthData>('/api/v1/health'),
  metrics: (container_id?: string) =>
    request<MetricsList>('/api/v1/metrics', container_id ? { container_id } : undefined),
  alerts: (limit = 100, offset = 0) =>
    request<AlertsData>('/api/v1/alerts', { limit, offset }),
  actions: (limit = 100, offset = 0) =>
    request<ActionsData>('/api/v1/actions/history', { limit, offset }),
  events: (container_id?: string, limit = 100, offset = 0) =>
    request<EventsData>('/api/v1/events', { ...(container_id ? { container_id } : {}), limit, offset }),
  recommendations: (lookback_days = 7, safety_margin = 0.2) =>
    request<RecommendationsData>('/api/v1/recommendations', { lookback_days, safety_margin }),
  forecast: (container_id: string, horizon_hours = 24, metric = 'memory_percent') =>
    request<ForecastData>(`/api/v1/forecast/${container_id}`, { horizon_hours, metric }),
  policies: () =>
    request<PoliciesData>('/api/v1/policies'),
};
