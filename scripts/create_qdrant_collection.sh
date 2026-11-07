#!/bin/sh
set -eu

qdrant_url=${QDRANT_URL:-http://127.0.0.1:6333}
tei_url=${TEI_URL:-http://127.0.0.1:7997}
collection_name=${QDRANT_COLLECTION:-nvd_incidents}

for command_name in curl jq; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf '%s\n' "$command_name is required to create and verify the Qdrant collection." >&2
        exit 1
    fi
done

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM

cluster_status_file="$temporary_dir/cluster.json"
cluster_http_status=$(curl --silent --show-error \
    --output "$cluster_status_file" \
    --write-out '%{http_code}' \
    "$qdrant_url/cluster") || {
    printf 'Cannot connect to Qdrant at %s\n' "$qdrant_url" >&2
    exit 1
}
if [ "$cluster_http_status" -lt 200 ] || [ "$cluster_http_status" -ge 300 ]; then
    printf 'Qdrant cluster endpoint returned HTTP %s\n' "$cluster_http_status" >&2
    cat "$cluster_status_file" >&2
    exit 1
fi

cluster_mode=$(jq -r '.result.status // "unknown"' "$cluster_status_file")
cluster_role=$(jq -r '.result.raft_info.role // "unknown"' "$cluster_status_file")
if [ "$cluster_mode" = "enabled" ] &&
    [ "$cluster_role" != "Leader" ] &&
    [ "$cluster_role" != "Follower" ]; then
    printf 'Qdrant cluster has no elected leader (current role: %s); refusing collection creation.\n' \
        "$cluster_role" >&2
    exit 1
fi

if [ -n "${EMBEDDING_SIZE:-}" ]; then
    embedding_size=$EMBEDDING_SIZE
else
    embedding_response_file="$temporary_dir/embedding.json"
    embedding_http_status=$(curl --silent --show-error \
        --output "$embedding_response_file" \
        --write-out '%{http_code}' \
        --header 'Content-Type: application/json' \
        --data '{"inputs":["Qdrant collection dimension check"]}' \
        "$tei_url/embed") || {
        printf 'Cannot connect to the embedding service at %s/embed; set EMBEDDING_SIZE to bypass dimension discovery.\n' \
            "$tei_url" >&2
        exit 1
    }
    if [ "$embedding_http_status" -lt 200 ] || [ "$embedding_http_status" -ge 300 ]; then
        printf 'Embedding service returned HTTP %s\n' "$embedding_http_status" >&2
        cat "$embedding_response_file" >&2
        exit 1
    fi
    embedding_size=$(jq -er '.[0] | length' "$embedding_response_file") || {
        printf '%s\n' "Embedding service response did not contain a dense vector." >&2
        exit 1
    }
fi

case "$embedding_size" in
    ''|*[!0-9]*|0)
        printf 'EMBEDDING_SIZE must be a positive integer; received: %s\n' "$embedding_size" >&2
        exit 1
        ;;
esac

collection_url="$qdrant_url/collections/$collection_name"
collection_response_file="$temporary_dir/collection.json"
collection_http_status=$(curl --silent --show-error \
    --output "$collection_response_file" \
    --write-out '%{http_code}' \
    "$collection_url") || {
    printf 'Cannot query collection at %s\n' "$collection_url" >&2
    exit 1
}

if [ "$collection_http_status" = 200 ]; then
    actual_size=$(jq -r '.result.config.params.vectors.dense.size // empty' "$collection_response_file")
    has_sparse=$(jq -r '.result.config.params.sparse_vectors.sparse // empty | length > 0' "$collection_response_file")
    if [ "$actual_size" != "$embedding_size" ] || [ "$has_sparse" != true ]; then
        printf 'Collection %s already exists but its schema does not match (dense size=%s, sparse configured=%s; expected dense size=%s and sparse vector).\n' \
            "$collection_name" "${actual_size:-unknown}" "$has_sparse" "$embedding_size" >&2
        exit 1
    fi
    printf 'Collection %s already exists with the expected dense/sparse vector schema (dense size %s).\n' \
        "$collection_name" "$embedding_size"
    exit 0
fi

if [ "$collection_http_status" != 404 ]; then
    printf 'Qdrant returned HTTP %s while checking collection %s\n' \
        "$collection_http_status" "$collection_name" >&2
    cat "$collection_response_file" >&2
    exit 1
fi

create_response_file="$temporary_dir/create.json"
create_http_status=$(curl --silent --show-error \
    --output "$create_response_file" \
    --write-out '%{http_code}' \
    --request PUT \
    --header 'Content-Type: application/json' \
    --data "{\"vectors\":{\"dense\":{\"size\":$embedding_size,\"distance\":\"Cosine\"}},\"sparse_vectors\":{\"sparse\":{\"index\":{\"on_disk\":false}}}}" \
    "$collection_url") || {
    printf 'Could not create collection %s on Qdrant at %s\n' "$collection_name" "$qdrant_url" >&2
    exit 1
}

if [ "$create_http_status" -lt 200 ] || [ "$create_http_status" -ge 300 ]; then
    printf 'Qdrant returned HTTP %s while creating collection %s\n' \
        "$create_http_status" "$collection_name" >&2
    cat "$create_response_file" >&2
    exit 1
fi

jq -e '.result == true' "$create_response_file" >/dev/null || {
    printf 'Qdrant did not confirm creation of collection %s:\n' "$collection_name" >&2
    cat "$create_response_file" >&2
    exit 1
}

printf 'Created collection %s with dense vector size %s and sparse BM25 vector.\n' \
    "$collection_name" "$embedding_size"
