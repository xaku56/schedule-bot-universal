#!/usr/bin/env bash
# Independent, token-free probes. Run on the VPS, not on a developer's PC.
set -u

interval="${1:-60}"
samples="${2:-240}"
control="${3:-https://example.com}"
if [[ $# -gt 3 || ! "$interval" =~ ^[1-9][0-9]*$ || ! "$samples" =~ ^[1-9][0-9]*$ || ! "$control" =~ ^https://[A-Za-z0-9.-]+/?$ ]]; then
    echo "Usage: bash scripts/diagnose-network.sh [interval_seconds=60] [samples=240] [https://control-host]" >&2
    exit 2
fi
if ! command -v curl >/dev/null; then
    echo "curl is required" >&2
    exit 2
fi

probe() {
    local sample="$1" family="$2" url="$3" started metrics code
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    metrics="$(curl --disable --noproxy '*' "-$family" --silent \
        --connect-timeout 5 --max-time 10 --output /dev/null \
        --write-out 'http_code=%{http_code} remote_ip=%{remote_ip} dns_s=%{time_namelookup} connect_s=%{time_connect} tls_s=%{time_appconnect} ttfb_s=%{time_starttransfer} total_s=%{time_total}' \
        "$url" 2>/dev/null)"
    code=$?
    printf 'network_probe started_at=%s finished_at=%s sample=%s family=IPv%s host=%s curl_exit=%s %s\n' \
        "$started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sample" "$family" "${url#https://}" "$code" "$metrics"
}

for ((sample=1; sample<=samples; sample++)); do
    round_started=$SECONDS
    for family in 4 6; do
        probe "$sample" "$family" https://api.telegram.org &
        probe "$sample" "$family" "$control" &
    done
    wait
    pause=$((interval - SECONDS + round_started))
    if ((sample < samples && pause > 0)); then
        sleep "$pause"
    fi
done
