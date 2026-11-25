import { useState } from 'react';
import { describeEvidenceState } from '../utils/describeEvidenceState';
import { api } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import { IconLog, IconRun } from './icons';

type Props = { containerId: string };

function RecentErrors({ containerId }: Props) {
  const { data, error } = usePolling(() => api.logs(containerId, 10, 'ERROR', 20), 30_000, [containerId]);
  if (error) return <p role="status">{error}</p>;
  if (!data) return <p>Loading error lines…</p>;
  if (!data.total_returned) return <p>No error lines in the last 10 minutes.</p>;
  return <ol>{data.logs.map((line, i) =>
    <li key={i}><code style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{line.message}</code></li>
  )}</ol>;
}

export function LogEvidence({ containerId }: Props) {
  const [pending, setPending] = useState(false);
  const [aiError, setAiError] = useState('');
  const { data, error, refresh } = usePolling(async () => {
    const report = await api.explain(containerId, 10);
    setPending(report.llm_status === 'pending');
    return report;
  }, pending ? 5_000 : 30_000, [containerId]);

  async function analyze() {
    setAiError('');
    try {
      const response = await api.explainAi(containerId);
      setPending(response.status === 'pending');
      if (response.status === 'unavailable') setAiError('Local analysis unavailable. Try again later.');
      refresh();
    } catch (cause) {
      setAiError(cause instanceof Error ? cause.message : 'Analysis request failed');
    }
  }
  const [expanded, setExpanded] = useState(false);
  const badge = data?.verdict === 'none' && !data.active_anomaly ? 'badge-neutral'
    : ['oom_kill_confirmed', 'crash_loop'].includes(data?.verdict ?? '') ? 'badge-critical' : 'badge-warning';

  return <section aria-label="Log Evidence" style={{ marginBottom: 24 }}>
    <div className="section-header"><h3 className="section-title"><IconLog /> Log Evidence</h3></div>
    {error ? <p role="status">{error}</p> : !data ? <p>Loading log evidence…</p> : <>
      <p>{describeEvidenceState(data)}</p>
      <small style={{ color: 'var(--text-muted)' }}>
        Log collection: {(data.coverage.collection?.state ?? data.coverage.logs_status).replace(/_/g, ' ')}
        {data.coverage.collection?.last_line_ts && ` · last line ${data.coverage.collection.last_line_ts.slice(11, 19)} UTC`}
      </small>
      <div>{Object.entries(data.metric_context).filter(([, value]) => value != null).map(([key, value]) =>
        <span key={key} className="badge badge-neutral">{key.replace(/_/g, ' ')} <code>{value?.toFixed(key.includes('growth') ? 2 : 1)}{key.includes('growth') ? '%/min' : '%'}</code></span>
      )}</div>
      <span className={`badge ${badge}`}>Heuristic · {data.verdict.replace(/_/g, ' ')} · {data.confidence} confidence</span>
      <ul>{data.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
      {data.llm_status === 'disabled' && <small>Local AI analysis is disabled in configuration.</small>}
      {data.llm_status !== 'disabled' && <section aria-label="AI analysis">
        <h4>AI analysis</h4>
        {data.llm_status === 'model_missing' || data.llm_status === 'unavailable'
          ? <p style={{ color: 'var(--muted)' }}>{data.llm_status === 'model_missing' ? 'Configured model is not installed yet.' : 'Local inference service is unavailable.'}</p>
          : pending || data.llm_status === 'pending'
          ? <div role="status"><span className="spinner" style={{ display: 'inline-block' }} /> Analyzing…</div>
          : data.llm_summary ? <>
            <p>{data.llm_summary.root_cause} <span className="badge badge-neutral">{data.llm_summary.confidence} confidence</span></p>
            <ul>{data.llm_summary.suggested_actions.map((action, i) => <li key={i}>{action}</li>)}</ul>
            <small>AI-generated, advisory · {data.llm_summary.model} · <code>{data.llm_summary.latency_ms} ms</code></small>
          </> : <button className="badge badge-info" onClick={analyze}><IconRun /> Analyze with AI</button>}
        {aiError && <p role="status">{aiError}</p>}
      </section>}
      {data.top_templates.length ? <div style={{ overflowX: 'auto' }}>
        <table className="data-table">
          <thead><tr><th>Template</th><th>Count</th><th>Level</th><th>First seen (UTC)</th></tr></thead>
          <tbody>{data.top_templates.map(row => <tr key={row.template_id}>
            <td><code style={{ overflowWrap: 'anywhere' }}>{row.template}</code></td>
            <td className="metric-val">{row.count}</td>
            <td><span className={`badge ${row.level === 'ERROR' ? 'badge-critical' : row.level === 'WARN' ? 'badge-warning' : 'badge-neutral'}`}>{row.level}</span></td>
            <td><code>{row.first_seen.replace('T', ' ').slice(0, 19)}</code></td>
          </tr>)}</tbody>
        </table>
      </div> : <p>No log templates observed in the last {data.window_minutes} minutes.</p>}
      <details onToggle={e => setExpanded(e.currentTarget.open)}>
        <summary>Recent error lines</summary>
        {expanded && <RecentErrors containerId={containerId} />}
      </details>
    </>}
  </section>;
}
