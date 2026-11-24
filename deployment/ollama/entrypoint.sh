#!/bin/bash
set -euo pipefail
OLLAMA_HOST=0.0.0.0:11434 ollama serve &
server=$!
trap 'kill -TERM "$server" 2>/dev/null || true' TERM INT EXIT
for ((second=0; second<60; second++)); do
    if ollama list >/dev/null 2>&1; then break; fi
    kill -0 "$server"
    sleep 1
done
ollama list >/dev/null
name="$DRI_LLM_MODEL"
[[ ${name##*/} == *:* ]] || name+=:latest
if ! ollama list | awk 'NR > 1 {print $1}' | grep -Fx "$name" >/dev/null; then
    for attempt in 1 2 3 4 5; do
        if ollama pull "$DRI_LLM_MODEL"; then break; fi
        if (( attempt == 5 )); then exit 1; fi
        sleep $((2 ** attempt))
    done
fi
wait "$server"
