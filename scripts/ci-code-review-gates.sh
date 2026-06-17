#!/usr/bin/env bash
# ============================================================================
# ci-code-review-gates.sh
# CI automated code review gates for Drycontroller-edge.
# Any failure exits 1, blocking merge.
#
# Aligns with AGENTS.md Review Discipline.
#
# Usage:  bash scripts/ci-code-review-gates.sh [--quick] [--json]
# ============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BOLD='\033[1m'; NC='\033[0m'
FAILED_CHECKS=0; TOTAL_CHECKS=0
JSON_OUTPUT=false; QUICK_MODE=false; MODE_LABEL=""

for arg in "$@"; do
    case "$arg" in
        --json) JSON_OUTPUT=true ;;
        --quick) QUICK_MODE=true; MODE_LABEL=" (quick)" ;;
    esac
done

json_begin() { $JSON_OUTPUT && echo '{' || true; }
json_end()   { $JSON_OUTPUT && echo '}' || true; }

_report() {
    local status="$1" check="$2" detail="$3"
    case "$status" in
        pass)
            TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
            if $JSON_OUTPUT; then printf '  "%s":{"status":"pass","detail":"%s"},\n' "$check" "$detail"
            else printf "  ${GREEN}[PASS]${NC} ${BOLD}%s${NC} %s\n" "$check" "$detail"; fi
            ;;
        fail)
            TOTAL_CHECKS=$((TOTAL_CHECKS + 1)); FAILED_CHECKS=$((FAILED_CHECKS + 1))
            if $JSON_OUTPUT; then printf '  "%s":{"status":"FAIL","detail":"%s"},\n' "$check" "$detail"
            else printf "  ${RED}[FAIL]${NC} ${BOLD}%s${NC} — %s\n" "$check" "$detail"; fi
            ;;
        skip)
            if $JSON_OUTPUT; then printf '  "%s":{"status":"skip","detail":"%s"},\n' "$check" "$detail"
            else printf "  ${YELLOW}[SKIP]${NC} ${BOLD}%s${NC} %s\n" "$check" "$detail"; fi
            ;;
    esac
}

# Count lines from stdin. Returns 0 for empty input.
_ln() {
    local content
    content=$(cat)
    if [ -z "$content" ]; then
        echo 0
    else
        echo "$content" | wc -l | tr -d ' '
    fi
}

# Grep tracked Python sources (excludes tests / vendored trees). POSIX ERE.
# Pass -i as first arg for case-insensitive. git's own regex engine behaves
# identically on macOS and the Linux CI runner, unlike system grep — and the
# runner image ships git but NOT ripgrep.
_pysrc_grep() {
    local ci=""
    [ "${1:-}" = "-i" ] && { ci="-i"; shift; }
    git grep --no-color $ci -nE -e "$1" -- \
        '*.py' ':!*/tests/*' ':!tests/*' ':!*/.venv/*' ':!*/__pycache__/*' 2>/dev/null || true
}

# List tracked files matching the given pathspec(s).
_ls() { git ls-files -- "$@" 2>/dev/null || true; }

# ============================================================================
# GATE-01: except Exception without # noqa: BLE001
# ============================================================================
check_bare_exception() {
    local name="GATE-01: except Exception without noqa"
    local output
    output=$(_pysrc_grep 'except[[:space:]]+Exception')
    local count
    count=$(echo "$output" | grep -v 'noqa: BLE001' | _ln || true)
    if [ "$count" -eq 0 ]; then
        _report pass "$name" "0 unannotated broad excepts"
    else
        _report fail "$name" "$count unannotated broad except(s). Add # noqa: BLE001 with intent comment"
    fi
}

# ============================================================================
# GATE-02: bare except:
# ============================================================================
check_bare_except() {
    local name="GATE-02: bare except:"
    local output
    output=$(_pysrc_grep 'except[[:space:]]*:')
    local count
    count=$(echo "$output" | _ln)
    if [ "$count" -eq 0 ]; then
        _report pass "$name" "0 bare excepts"
    else
        _report fail "$name" "$count bare except(s). Must specify exception type"
    fi
}

# ============================================================================
# GATE-03: Hardcoded secrets
# ============================================================================
check_hardcoded_secrets() {
    local name="GATE-03: hardcoded secrets"
    local total=0
    local filt='os\.environ|os\.getenv|\.env|TODO|FIXME|noqa|example|dummy|placeholder|changeme|your-'

    # Double-quoted assignments
    local out1
    out1=$(_pysrc_grep -i '(api_key|secret_key|password|passwd|token)[[:space:]]*=[[:space:]]*"[a-zA-Z0-9_!@#%^*()+=-]{8,}"')
    local c1
    c1=$(echo "$out1" | grep -vE "$filt" | _ln || true)
    total=$((total + c1))

    # Single-quoted assignments
    local out2
    out2=$(_pysrc_grep -i "(api_key|secret_key|password|passwd|token)[[:space:]]*=[[:space:]]*'[a-zA-Z0-9_!@#%^*()+=-]{8,}'")
    local c2
    c2=$(echo "$out2" | grep -vE "$filt" | _ln || true)
    total=$((total + c2))

    if [ "$total" -eq 0 ]; then
        _report pass "$name" "0 hardcoded secrets"
    else
        _report fail "$name" "$total potential hardcoded secret(s). Secrets must come from env vars"
    fi
}

# ============================================================================
# GATE-04: python syntax
# ============================================================================
check_py_compile() {
    local name="GATE-04: python syntax"
    local py_files errors=0 err_list=""
    py_files=$(_ls '*.py' ':!*/tests/*' ':!tests/*' ':!*/.venv/*' ':!*/__pycache__/*')
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        if ! python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null; then
            errors=$((errors + 1))
            err_list="$err_list $f"
        fi
    done <<< "$py_files"
    if [ "$errors" -eq 0 ]; then
        _report pass "$name" "all files compile cleanly"
    else
        _report fail "$name" "$errors file(s) with syntax errors:$err_list"
    fi
}

# ============================================================================
# GATE-05: no __pycache__ or .pyc
# ============================================================================
check_no_pyc_in_vcs() {
    local name="GATE-05: no __pycache__ or .pyc"
    local out
    out=$(_ls '*.pyc' '*/__pycache__/*' '__pycache__/*')
    local count
    count=$(echo "$out" | _ln)
    if [ "$count" -eq 0 ]; then
        _report pass "$name" "0 compiled artifacts"
    else
        _report fail "$name" "$count compiled artifact(s). Check .gitignore"
    fi
}

# ============================================================================
# GATE-06: no .env files
# ============================================================================
check_no_env_in_vcs() {
    local name="GATE-06: no .env files"
    local out
    out=$(_ls ':(glob)**/.env')
    local count
    count=$(echo "$out" | _ln)
    if [ "$count" -eq 0 ]; then
        _report pass "$name" "0 .env files (only .example allowed)"
    else
        _report fail "$name" "$count .env file(s). Only .env.example should be committed"
    fi
}

# ============================================================================
# GATE-07: control path NaN/Inf guard
# ============================================================================
check_nan_guard() {
    local name="GATE-07: control path NaN/Inf guard"
    local files missing=0
    # Scope = the PLC write layer only (daq/control.py, the last line before the
    # register write per CODE_REVIEW_STANDARDS). edge-api control_core/* are
    # read-side analysis modules whose isinstance() is bool-coercion, not a PLC
    # numeric write — including them produced false positives.
    files=$(_ls '*/daq/control*.py' ':!*/tests/*' ':!tests/*')
    if [ -z "$files" ]; then
        _report skip "$name" "no control files in this repo"
        return
    fi
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        local has_isfinite has_isinstance
        has_isfinite=$(git grep --no-color -lE 'math\.isfinite|numpy\.isfinite|\.isfinite' -- "$f" 2>/dev/null | _ln || true)
        has_isinstance=$(git grep --no-color -lE 'isinstance.*(int.*float|float.*int)' -- "$f" 2>/dev/null | _ln || true)
        if [ "${has_isfinite:-0}" -eq 0 ] && [ "${has_isinstance:-0}" -gt 0 ]; then
            missing=$((missing + 1))
            _report fail "$name" "$f: has isinstance check, missing math.isfinite()"
        fi
    done <<< "$files"
    if [ "$missing" -eq 0 ]; then
        _report pass "$name" "all control files have NaN/Inf guards"
    fi
}

# ============================================================================
# GATE-08: print() in production code
# ============================================================================
check_print_in_prod() {
    if $QUICK_MODE; then
        _report skip "GATE-08: print() in prod" "skipped (quick mode)"
        return
    fi
    local name="GATE-08: print() in production code"
    local out
    out=$(git grep --no-color -nE '(^|[^A-Za-z0-9_])print\(' -- \
        '*.py' ':!*/tests/*' ':!tests/*' ':!*/scripts/*' ':!scripts/*' \
        ':!*/setup.py' ':!setup.py' ':!*/.venv/*' ':!*/__pycache__/*' 2>/dev/null || true)
    local count
    count=$(echo "$out" | grep -v '# noqa' | _ln || true)
    if [ "$count" -eq 0 ]; then
        _report pass "$name" "0 print() calls"
    elif [ "$count" -le 5 ]; then
        _report pass "$name" "$count print() calls (threshold: 5)"
    else
        _report fail "$name" "$count print() calls. Use structlog logger.*()"
    fi
}

# ============================================================================
# GATE-09: PR size limit
# ============================================================================
check_pr_size() {
    local name="GATE-09: PR size (≤400 lines)"
    if [ "${GITHUB_EVENT_NAME:-}" != "pull_request" ]; then
        _report skip "$name" "not a PR context"
        return
    fi
    local base_ref="origin/${GITHUB_BASE_REF:-master}"
    git rev-parse "$base_ref" >/dev/null 2>&1 || base_ref="origin/master"
    git rev-parse "$base_ref" >/dev/null 2>&1 || base_ref="origin/main"
    git rev-parse "$base_ref" >/dev/null 2>&1 || { _report skip "$name" "unknown base ref"; return; }
    local diff_lines
    diff_lines=$(git diff --numstat "$base_ref" 2>/dev/null | awk '{a+=$1+$2} END{print a+0}')
    diff_lines="${diff_lines:-0}"
    if [ "$diff_lines" -eq 0 ]; then _report skip "$name" "0 lines"; return; fi
    if [ "$diff_lines" -le 400 ]; then
        _report pass "$name" "$diff_lines lines"
    else
        _report fail "$name" "$diff_lines lines exceeds 400. Split into smaller PRs"
    fi
}

# ============================================================================
# main
# ============================================================================
main() {
    # Pre-flight: ensure required tools exist (runner ships git + python3, not rg)
    if ! command -v git &>/dev/null; then
        echo "ERROR: git is required but not found in PATH." >&2
        exit 2
    fi
    if ! command -v python3 &>/dev/null; then
        echo "ERROR: python3 is required but not found in PATH." >&2
        exit 2
    fi
    if ! $JSON_OUTPUT; then
        echo ""
        printf "${BOLD}═══ DryController edge CI Review Gates${MODE_LABEL} ═══${NC}\n"
        echo ""
    fi

    json_begin

    check_bare_exception       # GATE-01
    check_bare_except           # GATE-02
    check_hardcoded_secrets     # GATE-03
    check_py_compile            # GATE-04
    check_no_pyc_in_vcs         # GATE-05
    check_no_env_in_vcs         # GATE-06
    check_nan_guard             # GATE-07
    check_print_in_prod         # GATE-08
    check_pr_size               # GATE-09

    json_end

    if ! $JSON_OUTPUT; then
        echo ""
        printf "${BOLD}──────────────────────────────────────────${NC}\n"
        if [ "$FAILED_CHECKS" -eq 0 ]; then
            printf "  ${GREEN}${BOLD}ALL ${TOTAL_CHECKS} GATES PASSED${NC}\n\n"
            exit 0
        else
            printf "  ${RED}${BOLD}${FAILED_CHECKS}/${TOTAL_CHECKS} GATES FAILED${NC}\n\n"
            printf "  Fix per AGENTS.md Review Discipline and retry.\n\n"
            exit 1
        fi
    else
        exit "$FAILED_CHECKS"
    fi
}

main "$@"
