#!/usr/bin/env bash
# =============================================================================
# Danus bootstrap — reuse host tools and provision missing tools under runtime/.
#
#   bash scripts/bootstrap.sh
#
# Idempotent: re-running skips anything already in place. Installs into
# runtime/ (gitignored), so the deployment tree stays clean and the whole
# missing tools are installed locally (no system-wide installs). Provisions:
#   1) Node + npm on PATH, otherwise Node 22 -> runtime/node22 (official tarball)
#   2) Python venv + deps -> runtime/venv             (mcp/fastapi/uvicorn/pydantic/openai/anthropic
#                                                      + the danus package itself, editable)
#   3) codex CLI on PATH or in macOS Desktop, otherwise -> runtime/codex-npm
#   4) node skill deps    -> human-summary/node_modules (markdown-it/katex, soft)
#   5) writes runtime/runtime.env (machine paths read by scripts/env.sh)
#   6) if config/codex.env holds a real BYO key, writes the codex model_provider
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DANUS_ROOT="$(cd "$HERE/.." && pwd)"
RT="$DANUS_ROOT/runtime"
NODE_VERSION="${NODE_VERSION:-v22.14.0}"
ARCH="$(uname -m)"; case "$ARCH" in x86_64) NARCH=x64;; aarch64|arm64) NARCH=arm64;; *) NARCH=x64;; esac
case "$(uname -s)" in
  Linux) NODE_OS=linux;;
  Darwin) NODE_OS=darwin;;
  *) echo "[bootstrap] unsupported OS: $(uname -s)" >&2; exit 1;;
esac
mkdir -p "$RT/logs"
log(){ printf '[bootstrap] %s\n' "$*"; }

# Be polite about IO/CPU (do not saturate a shared host).
NICE="nice -n19"; command -v ionice >/dev/null 2>&1 && NICE="ionice -c3 $NICE"

# --- 1) Reuse host Node/npm, falling back to the local runtime ---------------
. "$HERE/detect-toolchain.sh"
NODE="$(danus_find_host_tool node || true)"
NPM=""
if [ -n "$NODE" ]; then
  NPM="$(PATH="$(dirname "$NODE"):$PATH" danus_find_host_tool npm || true)"
fi
NODE_DIR="$RT/node22"
if [ -n "$NODE" ] && [ -n "$NPM" ]; then
  log "using existing node: $NODE ($("$NODE" --version))"
  log "using existing npm: $NPM"
elif [ -x "$NODE_DIR/bin/node" ]; then
  log "node present: $("$NODE_DIR/bin/node" --version)"
else
  log "installing Node $NODE_VERSION ($NARCH) -> $NODE_DIR"
  TARBALL="node-$NODE_VERSION-$NODE_OS-$NARCH.tar.gz"
  $NICE curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/$TARBALL" -o "$RT/$TARBALL" || true
  [ -s "$RT/$TARBALL" ] || { log "FATAL: could not download node (set NODE_VERSION / check network)"; exit 1; }
  mkdir -p "$NODE_DIR"
  tar -xzf "$RT/$TARBALL" -C "$NODE_DIR" --strip-components=1
  rm -f "$RT/$TARBALL"
  log "node installed: $("$NODE_DIR/bin/node" --version)"
fi
if [ -z "$NODE" ] || [ -z "$NPM" ]; then
  NODE="$NODE_DIR/bin/node"
  NPM="$NODE_DIR/bin/npm"
fi
NODE_BIN="$(dirname "$NODE")"
export PATH="$NODE_BIN:$PATH"

# --- 2) Python venv + deps --------------------------------------------------
# A venv's base interpreter is referenced by absolute path (pyvenv.cfg `home`).
# If that base interpreter ever moves or is removed, the venv can't run even
# though its site-packages survive. Moving the checkout also leaves absolute
# paths in activation and console scripts. Validate both location and imports;
# rebuild a relocated venv even when the old checkout still exists.
VENV="$RT/venv"
export PIP_DISABLE_PIP_VERSION_CHECK=1
DEPS='from danus._mcp import FastMCP; import fastapi,uvicorn,pydantic,openai,anthropic'
# Activation records the creation path even when Python itself still runs.
VENV_LOCATION="$( ( . "$VENV/bin/activate" && printf '%s' "$VIRTUAL_ENV" ) 2>/dev/null || true)"
if [ "$VENV_LOCATION" = "$VENV" ] && "$VENV/bin/python" -c "$DEPS" 2>/dev/null; then
  log "venv present + healthy"
else
  # Resolve the base BEFORE deleting anything: PATH may contain this venv.
  PYBASE="$(python3 -c 'import sys; print(sys._base_executable)' 2>/dev/null || true)"
  [ -n "$PYBASE" ] && [ -x "$PYBASE" ] || { log "FATAL: no working base python3 to build the venv"; exit 1; }
  case "$PYBASE" in "$VENV"/*) log "FATAL: base Python is inside the venv to rebuild"; exit 1;; esac
  [ -e "$VENV" ] && { log "venv missing/broken or relocated — rebuilding"; rm -rf "$VENV"; }
  log "creating venv ($PYBASE) -> $VENV"
  "$PYBASE" -m venv "$VENV"
  log "installing python deps (mcp/fastapi/uvicorn/pydantic/openai/anthropic)"
  $NICE "$VENV/bin/python" -m pip install --quiet --no-cache-dir --upgrade pip >/dev/null 2>&1 || true
  $NICE "$VENV/bin/python" -m pip install --quiet --no-cache-dir \
    "mcp>=1.0.0" "fastapi>=0.110.0" "uvicorn>=0.30.0" "pydantic>=2.0" "openai>=2.40" \
    "anthropic>=0.92" \
    || { log "FATAL: pip install failed"; exit 1; }
  "$VENV/bin/python" -c "$DEPS" || { log "FATAL: venv still missing deps after install"; exit 1; }
fi

# --- 2b) the danus package itself (editable install) ------------------------
# Workers' MCP gateway, the verify service, and the bin/ wrappers all run
# `python -m danus.*` from arbitrary cwds (worker dirs, codex sessions), so the
# package must live on the venv's sys.path — cwd-on-sys.path only helps at the
# repo root. Editable, so a `git pull` needs no re-install. Validate from a
# neutral cwd: at the repo root a missing install is masked (cwd is sys.path[0]).
danus_install_is_current(){
  (cd / && env -u PYTHONPATH "$VENV/bin/python" -c '
import pathlib, sys, danus
sys.exit(pathlib.Path(danus.__file__).resolve() != (pathlib.Path(sys.argv[1]) / "danus/__init__.py").resolve())
' "$DANUS_ROOT")
}
if danus_install_is_current 2>/dev/null; then
  log "danus package present in venv"
else
  log "installing the danus package (editable) into the venv"
  $NICE "$VENV/bin/python" -m pip install --quiet --no-cache-dir -e "$DANUS_ROOT" \
    || { log "FATAL: pip install -e failed (the danus package)"; exit 1; }
  danus_install_is_current \
    || { log "FATAL: danus does not resolve to this checkout after editable install"; exit 1; }
fi

# --- 3) codex CLI (npm @openai/codex) --------------------------------------
# NB: `find … | head` can exit non-zero under `set -o pipefail` (find errors when
# the dir is absent) — `|| true` keeps that from tripping `set -e`.
CODEX_NPM="$RT/codex-npm"
CODEX_BIN="$(danus_find_host_tool codex || true)"
if [ -z "$CODEX_BIN" ]; then
  CODEX_BIN="$(danus_find_desktop_codex || true)"
fi
case "$CODEX_BIN" in
  */ChatGPT.app/Contents/*)
    log "WARN: using the Codex CLI bundled with Codex Desktop. Desktop updates may change its location; rerun bootstrap if this path stops working." ;;
esac
CODEX_JS=""
if [ -n "$CODEX_BIN" ]; then
  log "using existing codex: $CODEX_BIN ($("$CODEX_BIN" --version))"
else
  CODEX_JS="$(find "$CODEX_NPM" -path '*/@openai/codex/bin/codex.js' 2>/dev/null | head -1 || true)"
  if [ -n "$CODEX_JS" ]; then
    log "codex present: $CODEX_JS"
  else
    log "installing @openai/codex -> $CODEX_NPM"
    mkdir -p "$CODEX_NPM"
    $NICE "$NPM" install -g --prefix "$CODEX_NPM" @openai/codex >/dev/null 2>&1 \
      || { log "FATAL: npm install @openai/codex failed"; exit 1; }
    CODEX_JS="$(find "$CODEX_NPM" -path '*/@openai/codex/bin/codex.js' 2>/dev/null | head -1 || true)"
    [ -n "$CODEX_JS" ] || { log "FATAL: codex.js not found after install"; exit 1; }
  fi

fi

# --- 4) node skill deps (human-summary: markdown-it + katex) ---------------
HS="$DANUS_ROOT/.agents/skills/human-summary"
if [ -d "$HS" ] && [ ! -d "$HS/node_modules/katex" ]; then
  log "installing human-summary node deps (markdown-it/katex)"
  ( cd "$HS" && $NICE "$NPM" install --no-fund --no-audit >/dev/null 2>&1 ) \
    || log "WARN: human-summary npm install failed (PDF render needs it)"
fi

# --- 5) write runtime/runtime.env (machine paths for scripts/env.sh) -------
# Shell-quote paths (including spaces) because env.sh sources this file.
{
  printf '# Auto-generated by scripts/bootstrap.sh; re-run bootstrap to refresh.\n'
  printf 'export DANUS_NODE=%q\n' "$NODE"
  printf 'export DANUS_NODE_BIN=%q\n' "$NODE_BIN"
  printf 'export DANUS_CODEX_BIN=%q\n' "$CODEX_BIN"
  printf 'export DANUS_CODEX_JS=%q\n' "$CODEX_JS"
  printf 'export DANUS_VENV=%q\n' "$VENV"
} > "$RT/runtime.env"
log "wrote $RT/runtime.env"

# --- 6) codex backend = your BYO API key (config/codex.env) ------------------
# Writes the model_provider into $CODEX_HOME/config.toml (no ChatGPT login).
# Guard: only if config/codex.env exists with a real (non-placeholder) key.
. "$DANUS_ROOT/scripts/env.sh" >/dev/null 2>&1 || true
if [ "${CODEX_BACKEND:-api}" = "api" ] \
   && [ -n "${DANUS_CODEX_API_KEY:-}" ] && [ -n "${CODEX_API_BASE_URL:-}" ] \
   && case "$DANUS_CODEX_API_KEY" in *"<"*|*"your"*) false;; *) true;; esac; then
  bash "$DANUS_ROOT/scripts/setup-codex.sh" api 2>&1 | sed 's/^/[bootstrap] /' \
    || log "WARN: could not write the codex api provider config"
else
  log "codex api provider NOT written — fill config/codex.env (cp config/codex.env.example)"
  log "  with your BYO endpoint + key, then re-run bootstrap (or: scripts/setup-codex.sh api)"
fi

log "done. Next:"
log "  1) cp config/danus.env.example config/danus.env   # and edit (optional)"
log "  2) cp config/codex.env.example config/codex.env    # fill BYO endpoint + key"
log "  3) bash scripts/check-codex.sh                     # confirm the codex API is reachable"
log "  4) bash scripts/doctor.sh                          # full health check"
