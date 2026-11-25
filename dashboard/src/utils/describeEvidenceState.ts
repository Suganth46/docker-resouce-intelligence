import type { ExplainData } from '../api/client';

export function describeEvidenceState({ coverage, active_anomaly }: Pick<ExplainData, 'coverage' | 'active_anomaly'>) {
  const { logs_status, log_lines_in_window, events_in_window, metrics_available, window_minutes } = coverage;
  const sources = `${log_lines_in_window} log lines · ${events_in_window} Docker events · metrics ${metrics_available ? 'available' : 'unavailable'}`;
  const missing = logs_status === 'no_output' ? `No log output from this container in the last ${window_minutes} min.`
    : logs_status === 'following' ? '' : `Log collection: ${logs_status.replace(/_/g, ' ')}.`;
  return [missing, sources, active_anomaly && `Active anomaly: ${active_anomaly.type} — ${active_anomaly.detail}`].filter(Boolean).join(' ');
}
