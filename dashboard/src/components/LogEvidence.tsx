import { useState } from 'react';
import { api } from '../api/client';
import { usePolling } from '../hooks/usePolling';
import { IconLog } from './icons';

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
  const { data, error } = usePolling(() => api.explain(containerId, 10), 30_000, [containerId]);
  const [expanded, setExpanded] = useState(false);
  const badge = data?.verdict === 'none' ? 'badge-neutral'
    : ['oom_kill_confirmed', 'crash_loop'].includes(data?.verdict ?? '') ? 'badge-critical' : 'badge-warning';

  return <section aria-label="Log Evidence" style={{ marginBottom: 24 }}>
    <div className="section-header"><h3 className="section-title"><IconLog /> Log Evidence</h3></div>
    {error ? <p role="status">{error}</p> : !data ? <p>Loading log evidence…</p> : <>
      <span className={`badge ${badge}`}>{data.verdict.replace(/_/g, ' ')} · {data.confidence} confidence</span>
      <ul>{data.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
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
