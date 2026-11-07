#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose() {
    docker compose --project-directory "$project_dir" "$@"
}

if ! command -v docker >/dev/null 2>&1; then
    printf '%s\n' "docker is required to start the Qdrant cluster." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    printf '%s\n' "curl is required to check Qdrant readiness." >&2
    exit 1
fi

wait_for_http() {
    service=$1
    url=$2
    attempt=0
    while [ "$attempt" -lt 60 ]; do
        if curl --fail --silent --show-error "$url/" >/dev/null 2>&1; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    printf 'Qdrant service %s did not become ready at %s\n' "$service" "$url" >&2
    compose logs --tail 80 "$service" >&2
    return 1
}

printf '%s\n' "Starting the Qdrant bootstrap node..."
compose up -d qdrant_node_1
wait_for_http qdrant_node_1 http://127.0.0.1:6333

printf '%s\n' "Starting the two Qdrant joining nodes..."
compose up -d qdrant_node_2 qdrant_node_3
wait_for_http qdrant_node_2 http://127.0.0.1:6336
wait_for_http qdrant_node_3 http://127.0.0.1:6339

attempt=0
while [ "$attempt" -lt 90 ]; do
    if cluster_status=$(curl --fail --silent http://127.0.0.1:6333/cluster 2>/dev/null); then
        peer_count=$(printf '%s' "$cluster_status" | grep -o '"uri"' | wc -l | tr -d '[:space:]')
        if [ "$peer_count" = 3 ] &&
            printf '%s' "$cluster_status" | grep -Eq '"role":"(Leader|Follower)"' &&
            printf '%s' "$cluster_status" | grep -Fq 'qdrant_node_1:6335' &&
            printf '%s' "$cluster_status" | grep -Fq 'qdrant_node_2:6335' &&
            printf '%s' "$cluster_status" | grep -Fq 'qdrant_node_3:6335'; then
            printf '%s\n' "Qdrant cluster is enabled with three peers:"
            printf '%s\n' "$cluster_status"
            exit 0
        fi
    fi
    attempt=$((attempt + 1))
    sleep 1
done

printf '%s\n' "Qdrant nodes are responding, but the cluster does not have three stable DNS peers and an elected leader/follower." >&2
printf '%s\n' "Existing Raft state may contain stale peer IPs. Do not delete storage directories; inspect logs and plan a Qdrant-supported recovery." >&2
curl --fail --silent http://127.0.0.1:6333/cluster >&2 || true
compose logs --tail 80 qdrant_node_1 qdrant_node_2 qdrant_node_3 >&2
exit 1
