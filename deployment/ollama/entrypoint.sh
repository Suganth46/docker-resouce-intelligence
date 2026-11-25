#!/usr/bin/env bash
set -uo pipefail
export OLLAMA_HOST=127.0.0.1:11434
OLLAMA_HOST=0.0.0.0:11434 ollama serve &
server=$!
prepare_model() {
    echo "[dri-ollama] waiting for server"
    until ollama list >/dev/null 2>&1; do sleep 1; done
    local name="$DRI_LLM_MODEL" delay=2 attempt=1
    [[ ${name##*/} == *:* ]] || name+=:latest
    until ollama list | awk 'NR > 1 {print $1}' | grep -Fx "$name" >/dev/null; do
        echo "[dri-ollama] pulling $name attempt $attempt"
        if ollama pull "$name"; then break; fi
        sleep "$delay"
        delay=$((delay < 30 ? 2 * delay : 60))
        ((attempt++))
    done
    echo "[dri-ollama] model ready: $name"
}
prepare_model &
loader=$!
trap 'kill "$server" "$loader" 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT
wait "$server"
exit 1
