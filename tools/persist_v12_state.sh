#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: persist_v12_state.sh <commit-message> <path>..." >&2
  exit 2
fi

message="$1"
shift

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -f -- "$@"
if git diff --cached --quiet; then
  exit 0
fi
git commit -m "$message"

for attempt in 1 2 3; do
  if ! git fetch origin main; then
    sleep "$attempt"
    continue
  fi

  if git rebase origin/main; then
    echo "V12 state rebase succeeded on attempt $attempt."
    if git push origin HEAD:main; then
      echo "V12 state push succeeded."
      exit 0
    fi
  else
    # Rebase conflicts are expected when adjacent scans update the same JSON files.
    # Abort first, then merge the two snapshots by V12's state semantics.
    echo "V12 state rebase conflict; performing semantic merge."
    git rebase --abort || true
    local_head="$(git rev-parse HEAD)"
    mapfile -t changed_paths < <(git diff --name-only "origin/main...$local_head")
    echo "V12 state merge paths before requested paths: ${changed_paths[*]}"
    for requested in "$@"; do
      found=false
      for existing in "${changed_paths[@]}"; do
        if [ "$existing" = "$requested" ]; then found=true; break; fi
      done
      if [ "$found" = false ]; then changed_paths+=("$requested"); fi
    done

    echo "V12 state merge paths: ${changed_paths[*]}"
    temp_dir="$(mktemp -d)"
    trap 'rm -rf "$temp_dir"' EXIT
    index=0
    for path in "${changed_paths[@]}"; do
      case "$path" in
        v12/*.json) ;;
        *) echo "Refusing non-V12 JSON state merge: $path" >&2; exit 1 ;;
      esac
      git show "$local_head:$path" > "$temp_dir/local-$index.json" 2>/dev/null || printf '{}\n' > "$temp_dir/local-$index.json"
      git show "origin/main:$path" > "$temp_dir/remote-$index.json" 2>/dev/null || printf '{}\n' > "$temp_dir/remote-$index.json"
      index=$((index + 1))
    done

    git reset --hard origin/main
    index=0
    for path in "${changed_paths[@]}"; do
      python tools/merge_v12_state_json.py "$path" "$temp_dir/local-$index.json" "$temp_dir/remote-$index.json"
      git add -f -- "$path"
      index=$((index + 1))
    done
    if ! git diff --cached --quiet; then
      git commit -m "$message (merged concurrent state)"
    fi
    rm -rf "$temp_dir"
    trap - EXIT
    if git push origin HEAD:main; then
      echo "V12 merged state push succeeded."
      exit 0
    fi
  fi

  # A rejected push or another rebase conflict must not leave a stale rebase directory.
  rebase_merge="$(git rev-parse --git-path rebase-merge)"
  rebase_apply="$(git rev-parse --git-path rebase-apply)"
  if [ -d "$rebase_merge" ] || [ -d "$rebase_apply" ]; then
    git rebase --abort || true
  fi
  sleep "$attempt"
done

echo "Unable to persist V12 state after 3 clean fetch/rebase/push attempts." >&2
exit 1
