#!/usr/bin/env bash
# Sourced by bootstrap. Prefer working host tools over this deployment's tools.
# DANUS_ROOT and RT must already be set. Print absolute paths for runtime.env.
danus_find_host_tool() {
  local name="$1" dir candidate
  local -a dirs
  IFS=: read -r -a dirs <<< "$PATH"
  for dir in "${dirs[@]}"; do
    dir="$(cd "${dir:-.}" 2>/dev/null && pwd -P)" || continue
    case "$dir/" in "$RT/"*|"$DANUS_ROOT/bin/") continue ;; esac
    candidate="$dir/$name"
    [ -x "$candidate" ] && [ ! -d "$candidate" ] || continue
    # Also exclude aliases/symlinks to our wrapper, to avoid recursive launches.
    [ "$name" != codex ] || [ ! "$candidate" -ef "$DANUS_ROOT/bin/codex" ] || continue
    if [ "$name" = node ]; then
      "$candidate" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 16 ? 0 : 1)' >/dev/null 2>&1 || continue
    else
      "$candidate" --version >/dev/null 2>&1 || continue
    fi
    printf '%s\n' "$candidate"
    return 0
  done
  return 1
}

# Desktop ships a CLI even when no standalone CLI is installed on PATH.
# Optional application roots allow callers to probe a specific installation.
danus_find_desktop_codex() {
  [ "$(uname -s)" = Darwin ] || return 1
  local app_root candidate
  if [ "$#" -eq 0 ]; then
    set -- /Applications "$HOME/Applications"
  fi
  for app_root in "$@"; do
    # The app's internal layout is not stable across Desktop releases. Search
    # the bundle instead of assuming a Resources path or a wrapper location.
    while IFS= read -r -d '' candidate; do
      [ -x "$candidate" ] && [ ! -d "$candidate" ] || continue
      [ ! "$candidate" -ef "$DANUS_ROOT/bin/codex" ] || continue
      "$candidate" --version >/dev/null 2>&1 || continue
      printf '%s\n' "$candidate"
      return 0
    done < <(find "$app_root/ChatGPT.app/Contents" \
      \( -type f -o -type l \) -name codex -print0 2>/dev/null)
  done
  return 1
}
