# Log intelligence

The agent follows each running container on a dedicated Docker stream thread. It parses UTC timestamps, joins stack traces, prioritizes warnings and errors under load, and masks emails, bearer tokens, JWTs, AWS access key IDs, and recognized password or token assignments before buffering log text. Masking uses patterns and can miss other sensitive data. Configure the API key before exposing these endpoints. Docker must be able to read the container's logs through its logging driver or local cache.

Each stored row includes the ID of its normalized template. The analyzer keeps up to 500 templates per container in memory. SQLite and TimescaleDB retain the masked rows for 24 hours by default. Templates and statistical history start fresh when the agent restarts.

The container detail page polls the explanation every 30 seconds. The default window is ten minutes. Counts use minute buckets, so the oldest bucket can include part of the preceding minute. The analyzer reads up to 100 lifecycle events and 50 masked log samples. After restart, samples supply a lower bound on the number of matching lines. Missing resource readings appear as null.

## Heuristic verdicts

The first matching rule supplies the verdict. Confidence describes the supporting observations. It is not a calibrated probability.

| Verdict | Evidence | Confidence |
|---|---|---|
| oom_kill_confirmed | Docker OOM or a die event with exit code 137, plus memory at least 90%, growth above 5% per minute, or a Killed or OutOfMemoryError log | High when the event is corroborated |
| crash_loop | At least three die events, or a die followed by a start indicating a rising restart count | High |
| dependency_failure | At least five lines reporting a refused connection, timeout, ECONNREFUSED, no route to host, or name resolution problem | Medium |
| resource_exhaustion | A line reporting too many open files or no space left | Medium |
| error_spike | The statistical detector flags the current minute's error count | Medium |
| none | No rule matched the available evidence | Low |

GET /api/v1/logs/{container_id} returns masked lines in chronological order. GET /api/v1/containers/{container_id}/explain returns the verdict, reasons, matched signals, templates, Docker events and resource readings. Both endpoints use the configured API key and return 503 when analysis is disabled. Alerts can include a short evidence summary when the explanation finishes within two seconds.
