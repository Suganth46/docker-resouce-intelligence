import assert from 'node:assert/strict';
import { describeEvidenceState } from '../src/utils/describeEvidenceState.ts';
const coverage = {logs_status:'no_output', window_minutes:10, log_lines_in_window:0, events_in_window:2, metrics_available:true};
assert.match(describeEvidenceState({coverage, active_anomaly:null}), /No log output.*0 log lines.*2 Docker events/);
assert.match(describeEvidenceState({coverage, active_anomaly:{type:'memory_spike',detail:'19.8%\/min'}}), /Active anomaly: memory_spike.*19.8/);
