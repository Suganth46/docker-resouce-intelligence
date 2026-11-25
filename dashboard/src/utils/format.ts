/**
 * Format bytes into human-readable string (KB, MB, GB, etc.)
 */
export function fmtBytes(b: number | undefined | null): string {
  if (b === undefined || b === null) return '0 B';
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  if (b >= 1e3) return `${(b / 1e3).toFixed(1)} KB`;
  return `${b} B`;
}

export function metricUnit(metric: string): string {
  if (metric.includes('growth_rate')) return '%/min';
  return /^(cpu|memory)/.test(metric) ? '%' : '';
}

/**
 * Format observed coverage in minutes, hours, or days alongside requested lookback.
 */
export function fmtCoverage(rec: {
  observation_minutes?: number | null;
  lookback_actual_days: number;
  lookback_days: number;
  trend_evidence?: { observation_minutes?: number } | null;
  data_completeness?: string | null;
}): string {
  const observationMinutes =
    rec.observation_minutes ??
    rec.trend_evidence?.observation_minutes;

  if (observationMinutes == null && rec.data_completeness) {
    return rec.data_completeness;
  }

  const mins =
    typeof observationMinutes === 'number' && Number.isFinite(observationMinutes)
      ? observationMinutes
      : Number.isFinite(rec.lookback_actual_days)
        ? rec.lookback_actual_days * 1440
        : null;

  let observed: string;
  if (mins === null) {
    observed = '< 1 min observed';
  } else if (mins < 1) {
    observed = '< 1 min observed';
  } else if (mins < 60) {
    observed = `${Math.round(mins)} min observed`;
  } else if (mins < 1440) {
    observed = `${(mins / 60).toFixed(1)} hr observed`;
  } else {
    observed = `${(mins / 1440).toFixed(1)} days observed`;
  }

  return `${observed} · ${rec.lookback_days}-day lookback requested`;
}

/**
 * Map Phase 7E trend_label to dashboard badge CSS class.
 */
export function trendBadgeClass(label: string): string {
  if (label === 'Suspected memory leak') return 'badge-critical';
  if (label === 'Memory rising · early observation' || label === 'No sustained leak trend') {
    return 'badge-warning';
  }
  if (label === 'Memory stable') return 'badge-success';
  return 'badge-neutral';
}
