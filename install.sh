#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"
CONFIG_FILE_LOADED=""

load_local_config() {
  local config_file="${INSTALL_CONFIG_FILE:-}"
  local candidate key current value
  local keys=(CONTAINER_NAME MP_BASE_URL MP_USERNAME MP_PASSWORD MP_TOKEN CONTAINER_REPO_PATH PACKAGE_VERSION UPSTREAM_REMOTE_URL UPSTREAM_BRANCH)

  if [[ -z "$config_file" ]]; then
    for candidate in "$REPO_DIR/install_config.env" "$REPO_DIR/.env"; do
      if [[ -f "$candidate" ]]; then
        config_file="$candidate"
        break
      fi
    done
  fi

  [[ -n "$config_file" ]] || return 0
  [[ -f "$config_file" ]] || { printf 'ERROR: config file not found: %s
' "$config_file" >&2; exit 1; }

  for key in "${keys[@]}"; do
    current="${!key-}"
    if [[ -z "$current" ]]; then
      value="$(bash -c 'set -a; source "$1"; set +a; key="$2"; printf "%s" "${!key-}"' _ "$config_file" "$key")" || {
        printf 'ERROR: failed to read config file: %s
' "$config_file" >&2
        exit 1
      }
      if [[ -n "$value" ]]; then
        printf -v "$key" '%s' "$value"
        export "$key"
      fi
    fi
  done
  CONFIG_FILE_LOADED="$config_file"
}

load_local_config

CONTAINER_NAME="${CONTAINER_NAME:-moviepilot-v2}"
MP_BASE_URL="${MP_BASE_URL:-http://127.0.0.1:3001}"
MP_USERNAME="${MP_USERNAME:-}"
MP_PASSWORD="${MP_PASSWORD:-}"
MP_TOKEN="${MP_TOKEN:-}"
CONTAINER_REPO_PATH="${CONTAINER_REPO_PATH:-/config/plugin_forks/MoviePilot-Plugins}"
PACKAGE_VERSION="${PACKAGE_VERSION:-v2}"
UPSTREAM_REMOTE_URL="${UPSTREAM_REMOTE_URL:-https://github.com/DDSRem-Dev/MoviePilot-Plugins.git}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"

TARGET=""
DRY_RUN=0
VERIFY_ONLY=0
CHECK_UPSTREAM=0
ROLLBACK_MODE=""
COPY_ONLY=0
ALLOW_DIRTY=0
ALLOW_NONFORK_VERSION=0
FORCE=0
AUTH_TOKEN=""

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  if command -v base64 >/dev/null 2>&1; then
    base64 -d <<'USAGE_B64'
TW92aWVQaWxvdCDmnKzlnLAgZm9yayDmj5Lku7blronoo4XohJrmnKwKCueUqOazlToKICAuL2lu
c3RhbGwuc2ggPOebruaghz4gW+mAiemhuV0KICAuL2luc3RhbGwuc2ggLWgKICAuL2luc3RhbGwu
c2ggLS1oZWxwCgrnm67moIc6CiAgYWxsICAgICAgICAgICAgIOWuieijhS/mm7TmlrAgcDExNWRp
c2sg5ZKMIHAxMTVzdHJtaGVscGVyCiAgcDExNXN0cm1oZWxwZXIg5a6J6KOFL+abtOaWsCAxMTUg
U1RSTSDliqnmiYsKICBwMTE1ZGlzayAgICAgICDlronoo4Uv5pu05pawIDExNSDnvZHnm5jlrZjl
gqjmqKHlnZcKCumFjee9ruaWueW8j+S4gO+8muacrOWcsOmFjee9ruaWh+S7tu+8iOaOqOiNkO+8
iQogIGNhdCA+IGluc3RhbGxfY29uZmlnLmVudiA8PCdFT0YnCiAgTVBfVVNFUk5BTUU9J+S9oOea
hCBNb3ZpZVBpbG90IOeUqOaIt+WQjScKICBNUF9QQVNTV09SRD0n5L2g55qEIE1vdmllUGlsb3Qg
5a+G56CBJwogIEVPRgoKICAuL2luc3RhbGwuc2ggYWxsCgrphY3nva7mlrnlvI/kuozvvJrkuLTm
l7bnjq/looPlj5jph48KICBNUF9VU0VSTkFNRT0n5L2g55qEIE1vdmllUGlsb3Qg55So5oi35ZCN
JyBNUF9QQVNTV09SRD0n5L2g55qEIE1vdmllUGlsb3Qg5a+G56CBJyAuL2luc3RhbGwuc2ggYWxs
CgrluLjnlKjlkb3ku6Q6CiAg6aKE6KeI77yM5LiN5L+u5pS55a655ZmoOgogICAgLi9pbnN0YWxs
LnNoIGFsbCAtLWRyeS1ydW4KCiAg5a6J6KOFL+abtOaWsOWFqOmDqDoKICAgIC4vaW5zdGFsbC5z
aCBhbGwKCiAg5Y+q5a6J6KOFL+abtOaWsCBTVFJNIOWKqeaJizoKICAgIC4vaW5zdGFsbC5zaCBw
MTE1c3RybWhlbHBlcgoKICDlj6rlronoo4Uv5pu05pawIFAxMTVEaXNrOgogICAgLi9pbnN0YWxs
LnNoIHAxMTVkaXNrCgogIOmqjOivgeW9k+WJjeWuueWZqOWGheeJiOacrDoKICAgIC4vaW5zdGFs
bC5zaCBhbGwgLS12ZXJpZnkKCiAg5qOA5p+l5Li75LuT5bqT5piv5ZCm5pu05pawOgogICAgLi9p
bnN0YWxsLnNoIGFsbCAtLWNoZWNrLXVwc3RyZWFtCgogIOajgOafpeaMh+WumuaPkuS7tuaYr+WQ
puWPl+S4u+S7k+W6k+abtOaWsOW9seWTjToKICAgIC4vaW5zdGFsbC5zaCBwMTE1c3RybWhlbHBl
ciAtLWNoZWNrLXVwc3RyZWFtCgogIOaMh+WumuS4u+S7k+W6k+WcsOWdgOWSjOWIhuaUrzoKICAg
IC4vaW5zdGFsbC5zaCBhbGwgLS1jaGVjay11cHN0cmVhbSAtLXVwc3RyZWFtLXVybCBodHRwczov
L2dpdGh1Yi5jb20vRERTUmVtLURldi9Nb3ZpZVBpbG90LVBsdWdpbnMuZ2l0IC0tdXBzdHJlYW0t
YnJhbmNoIG1haW4KCiAg5Zue5rua5p+Q5Liq5o+S5Lu25Yiw5pyA6L+R5LiA5qyh5aSH5Lu9Ogog
ICAgLi9pbnN0YWxsLnNoIHAxMTVzdHJtaGVscGVyIC0tcm9sbGJhY2sgbGF0ZXN0CgrpgInpobk6
CiAgLS1kcnktcnVuICAgICAgICAgICAgICAgICDlj6rpooTop4jvvIzkuI3kv67mlLnlrrnlmagK
ICAtLXZlcmlmeSAgICAgICAgICAgICAgICAgIOWPqumqjOivge+8jOS4jeWuieijhQogIC0tY2hl
Y2stdXBzdHJlYW0gICAgICAgICAg5qOA5p+l5Li75LuT5bqT5pu05paw77yM5LiN5a6J6KOF44CB
5LiN5L+u5pS55a655ZmoCiAgLS11cHN0cmVhbS11cmwgVVJMICAgICAgICDmjIflrprkuLvku5Pl
upPlnLDlnYDvvIzpu5jorqTlrpjmlrnku5PlupMKICAtLXVwc3RyZWFtLWJyYW5jaCBCUkFOQ0gg
IOaMh+WumuS4u+S7k+W6k+WIhuaUr++8jOm7mOiupCBtYWluCiAgLS1yb2xsYmFjayBsYXRlc3Qg
ICAgICAgICDlm57mu5rliLDmnIDov5HkuIDmrKHlpIfku70KICAtLWNvcHktb25seSAgICAgICAg
ICAgICAgIOW6lOaApeebtOaOpeWkjeWItuWuieijhQogIC0tZm9yY2UgICAgICAgICAgICAgICAg
ICAg5b+955Wl5Lu75Yqh6L+Q6KGM54q25oCB5qOA5p+lCiAgLS1hbGxvdy1kaXJ0eSAgICAgICAg
ICAgICDlhYHorrggZ2l0IOW3peS9nOWMuuS4jeW5suWHgAogIC0tYWxsb3ctbm9uZm9yay12ZXJz
aW9uICAg5YWB6K646Z2eIDk5LiDlvIDlpLTniYjmnKwKICAtaCwgLS1oZWxwICAgICAgICAgICAg
ICAgIOaYvuekuuW4ruWKqQoK546v5aKD5Y+Y6YePOgogIElOU1RBTExfQ09ORklHX0ZJTEUgICAg
ICAg5oyH5a6a6YWN572u5paH5Lu26Lev5b6ECiAgTVBfVVNFUk5BTUUgICAgICAgICAgICAgICBN
b3ZpZVBpbG90IOeUqOaIt+WQjQogIE1QX1BBU1NXT1JEICAgICAgICAgICAgICAgTW92aWVQaWxv
dCDlr4bnoIEKICBNUF9UT0tFTiAgICAgICAgICAgICAgICAgIE1vdmllUGlsb3QgVG9rZW7vvIzm
nInlroPlsLHkuI3nlKjnlKjmiLflkI3lr4bnoIEKICBNUF9CQVNFX1VSTCAgICAgICAgICAgICAg
IE1vdmllUGlsb3Qg5Zyw5Z2A77yM6buY6K6kIGh0dHA6Ly8xMjcuMC4wLjE6MzAwMQogIENPTlRB
SU5FUl9OQU1FICAgICAgICAgICAg5a655Zmo5ZCN77yM6buY6K6kIG1vdmllcGlsb3QtdjIKICBV
UFNUUkVBTV9SRU1PVEVfVVJMICAgICAgIOS4u+S7k+W6k+WcsOWdgAogIFVQU1RSRUFNX0JSQU5D
SCAgICAgICAgICAg5Li75LuT5bqT5YiG5pSvCg==
USAGE_B64
  else
    printf '%s\n' "Usage: ./install.sh TARGET [options]"
    printf '%s\n' "Targets: all, p115strmhelper, p115disk"
    printf '%s\n' "Options: --dry-run --verify --rollback latest --copy-only --force --allow-dirty --allow-nonfork-version -h --help"
  fi
}


parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        usage
        exit 0
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --verify)
        VERIFY_ONLY=1
        shift
        ;;
      --check-upstream)
        CHECK_UPSTREAM=1
        shift
        ;;
      --upstream-url)
        [[ $# -ge 2 ]] || fail "--upstream-url requires a value"
        UPSTREAM_REMOTE_URL="$2"
        shift 2
        ;;
      --upstream-url=*)
        UPSTREAM_REMOTE_URL="${1#*=}"
        shift
        ;;
      --upstream-branch)
        [[ $# -ge 2 ]] || fail "--upstream-branch requires a value"
        UPSTREAM_BRANCH="$2"
        shift 2
        ;;
      --upstream-branch=*)
        UPSTREAM_BRANCH="${1#*=}"
        shift
        ;;
      --rollback)
        [[ $# -ge 2 ]] || fail "--rollback requires a value"
        ROLLBACK_MODE="$2"
        shift 2
        ;;
      --rollback=*)
        ROLLBACK_MODE="${1#*=}"
        shift
        ;;
      --copy-only)
        COPY_ONLY=1
        shift
        ;;
      --allow-dirty)
        ALLOW_DIRTY=1
        shift
        ;;
      --allow-nonfork-version)
        ALLOW_NONFORK_VERSION=1
        shift
        ;;
      --force)
        FORCE=1
        shift
        ;;
      --*)
        fail "Unknown option: $1"
        ;;
      *)
        if [[ -n "$TARGET" ]]; then
          fail "Only one target is allowed"
        fi
        TARGET="$1"
        shift
        ;;
    esac
  done

  [[ -n "$TARGET" ]] || fail "Missing target. Use --help for usage."
  if [[ -n "$ROLLBACK_MODE" && "$ROLLBACK_MODE" != "latest" ]]; then
    fail "Only '--rollback latest' is supported"
  fi
  if [[ "$VERIFY_ONLY" -eq 1 && -n "$ROLLBACK_MODE" ]]; then
    fail "--verify and --rollback cannot be used together"
  fi
  if [[ "$CHECK_UPSTREAM" -eq 1 && -n "$ROLLBACK_MODE" ]]; then
    fail "--check-upstream and --rollback cannot be used together"
  fi
  if [[ "$CHECK_UPSTREAM" -eq 1 && "$VERIFY_ONLY" -eq 1 ]]; then
    fail "--check-upstream and --verify cannot be used together"
  fi
  if [[ "$CHECK_UPSTREAM" -eq 1 && "$DRY_RUN" -eq 1 ]]; then
    fail "--check-upstream and --dry-run cannot be used together"
  fi
  if [[ "$DRY_RUN" -eq 1 && -n "$ROLLBACK_MODE" ]]; then
    fail "--dry-run and --rollback cannot be used together"
  fi
}

plugin_id_for() {
  case "$1" in
    p115strmhelper|P115StrmHelper) echo "P115StrmHelper" ;;
    p115disk|P115Disk) echo "P115Disk" ;;
    *) echo "" ;;
  esac
}

plugin_dir_for() {
  case "$1" in
    P115StrmHelper) echo "plugins.v2/p115strmhelper" ;;
    P115Disk) echo "plugins.v2/p115disk" ;;
    *) echo "" ;;
  esac
}

plugin_lower_for() {
  case "$1" in
    P115StrmHelper) echo "p115strmhelper" ;;
    P115Disk) echo "p115disk" ;;
    *) echo "" ;;
  esac
}

version_rel_file_for() {
  case "$1" in
    P115StrmHelper) echo "version.py" ;;
    P115Disk) echo "__init__.py" ;;
    *) echo "" ;;
  esac
}

expand_targets() {
  case "$TARGET" in
    all)
      printf '%s\n' "P115Disk" "P115StrmHelper"
      ;;
    *)
      local pid
      pid="$(plugin_id_for "$TARGET")"
      [[ -n "$pid" ]] || fail "Unsupported target: $TARGET"
      printf '%s\n' "$pid"
      ;;
  esac
}

target_paths_for() {
  case "$1" in
    P115StrmHelper)
      printf '%s\n' \
        "plugins.v2/p115strmhelper" \
        "frontend/p115strmhelper" \
        "package.v2.json"
      ;;
    P115Disk)
      printf '%s\n' \
        "plugins.v2/p115disk" \
        "package.v2.json"
      ;;
    *)
      fail "No upstream path mapping for $1"
      ;;
  esac
}

check_upstream_updates() {
  local pids=("$@")
  local upstream_ref upstream_head ahead behind paths_text changed_paths commits
  local -a path_args=()

  cd "$REPO_DIR"
  [[ -n "$UPSTREAM_REMOTE_URL" ]] || fail "UPSTREAM_REMOTE_URL is empty"
  [[ -n "$UPSTREAM_BRANCH" ]] || fail "UPSTREAM_BRANCH is empty"

  log "Checking upstream updates"
  printf 'upstream remote: %s\n' "$UPSTREAM_REMOTE_URL"
  printf 'upstream branch: %s\n' "$UPSTREAM_BRANCH"

  GIT_TERMINAL_PROMPT=0 git fetch --quiet --no-tags "$UPSTREAM_REMOTE_URL" "$UPSTREAM_BRANCH" || \
    fail "Failed to fetch upstream branch: $UPSTREAM_REMOTE_URL $UPSTREAM_BRANCH"

  upstream_ref="FETCH_HEAD"
  upstream_head="$(git log -1 --date=format:'%Y-%m-%d %H:%M:%S %z' --format='%h %ad %s' "$upstream_ref")"
  printf 'upstream HEAD: %s\n' "$upstream_head"

  read -r ahead behind < <(git rev-list --left-right --count "HEAD...$upstream_ref")
  printf 'local ahead: %s\n' "$ahead"
  printf 'local behind: %s\n' "$behind"

  if [[ "$behind" == "0" ]]; then
    log "No newer upstream commits found"
    return 0
  fi

  printf '\nupstream commits not in local HEAD:\n'
  git log --date=format:'%Y-%m-%d %H:%M:%S %z' --format='  %h %ad %s' "HEAD..$upstream_ref" | sed -n '1,30p'

  paths_text="$(
    for pid in "${pids[@]}"; do
      target_paths_for "$pid"
    done | awk '!seen[$0]++'
  )"
  while IFS= read -r path; do
    [[ -n "$path" ]] && path_args+=("$path")
  done <<< "$paths_text"

  printf '\nwatched paths:\n'
  printf '  %s\n' "${path_args[@]}"

  changed_paths="$(git diff --name-only "HEAD..$upstream_ref" -- "${path_args[@]}" || true)"
  if [[ -z "$changed_paths" ]]; then
    printf '\nresult: upstream has new commits, but none touched watched paths for this target.\n'
    return 0
  fi

  printf '\nchanged watched paths:\n'
  printf '%s\n' "$changed_paths" | sed 's/^/  /'

  commits="$(git log --date=format:'%Y-%m-%d %H:%M:%S %z' --format='  %h %ad %s' "HEAD..$upstream_ref" -- "${path_args[@]}" || true)"
  if [[ -n "$commits" ]]; then
    printf '\ncommits touching watched paths:\n'
    printf '%s\n' "$commits" | sed -n '1,30p'
  fi

  printf '\nresult: upstream has updates that may affect the selected plugin target. Review before merging or installing.\n'
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

check_repo() {
  cd "$REPO_DIR"
  [[ -d .git ]] || fail "Not a git repository root: $REPO_DIR"
  [[ -f package.v2.json ]] || fail "package.v2.json not found"
  [[ -d plugins.v2 ]] || fail "plugins.v2 not found"

  if [[ "$ALLOW_DIRTY" -ne 1 && "$CHECK_UPSTREAM" -ne 1 ]]; then
    if [[ -n "$(git status --porcelain)" ]]; then
      fail "Git working tree is dirty. Commit changes or pass --allow-dirty."
    fi
  fi
}

check_container() {
  docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME" || fail "Container is not running: $CONTAINER_NAME"
  docker exec "$CONTAINER_NAME" test -x /opt/venv/bin/python || fail "Container python not found: /opt/venv/bin/python"
}

check_plugin_source() {
  local pid="$1"
  local dir
  dir="$(plugin_dir_for "$pid")"
  [[ -n "$dir" ]] || fail "No source mapping for $pid"
  [[ -d "$REPO_DIR/$dir" ]] || fail "Plugin source dir not found: $dir"
  [[ -f "$REPO_DIR/$dir/__init__.py" ]] || fail "Plugin __init__.py not found: $dir/__init__.py"
}

package_version_for() {
  local pid="$1"
  jq -r --arg pid "$pid" '.[$pid].version // empty' "$REPO_DIR/package.v2.json"
}

source_version_for() {
  local pid="$1"
  local dir file
  dir="$(plugin_dir_for "$pid")"
  case "$pid" in
    P115StrmHelper)
      file="$REPO_DIR/$dir/version.py"
      sed -nE 's/^VERSION[[:space:]]*=[[:space:]]*["'"'"']([^"'"'"']+)["'"'"'].*/\1/p' "$file" | head -n 1
      ;;
    P115Disk)
      file="$REPO_DIR/$dir/__init__.py"
      sed -nE 's/^[[:space:]]*plugin_version[[:space:]]*=[[:space:]]*["'"'"']([^"'"'"']+)["'"'"'].*/\1/p' "$file" | head -n 1
      ;;
    *)
      echo ""
      ;;
  esac
}

validate_versions() {
  local pid="$1"
  local package_version source_version
  package_version="$(package_version_for "$pid")"
  source_version="$(source_version_for "$pid")"
  [[ -n "$package_version" ]] || fail "Missing package version for $pid"
  [[ -n "$source_version" ]] || fail "Missing source version for $pid"
  [[ "$package_version" == "$source_version" ]] || fail "Version mismatch for $pid: package=$package_version source=$source_version"
  if [[ "$ALLOW_NONFORK_VERSION" -ne 1 && "$source_version" != 99.* ]]; then
    fail "Version must start with 99. for $pid. Pass --allow-nonfork-version to override."
  fi
  printf '%s\n' "$source_version"
}

frontend_dir_for() {
  case "$1" in
    P115StrmHelper) echo "frontend/p115strmhelper" ;;
    *) echo "" ;;
  esac
}

build_frontend_assets_if_needed() {
  local pid="$1"
  local frontend_dir plugin_dir frontend_abs plugin_abs
  local package_lock_tmp yarn_lock_tmp build_status
  frontend_dir="$(frontend_dir_for "$pid")"
  [[ -n "$frontend_dir" ]] || return 0

  frontend_abs="$REPO_DIR/$frontend_dir"
  plugin_dir="$(plugin_dir_for "$pid")"
  plugin_abs="$REPO_DIR/$plugin_dir"

  [[ -f "$frontend_abs/package.json" ]] || return 0
  require_cmd node
  require_cmd npm

  package_lock_tmp=""
  yarn_lock_tmp=""
  if [[ -f "$frontend_abs/package-lock.json" ]]; then
    package_lock_tmp="$(mktemp)"
    cp "$frontend_abs/package-lock.json" "$package_lock_tmp"
  fi
  if [[ -f "$frontend_abs/yarn.lock" ]]; then
    yarn_lock_tmp="$(mktemp)"
    cp "$frontend_abs/yarn.lock" "$yarn_lock_tmp"
  fi

  log "Building frontend assets for $pid"
  set +e
  (
    cd "$frontend_abs"
    if [[ -f package-lock.json ]]; then
      npm ci
    else
      npm install
    fi
    npm run build
  )
  build_status=$?
  set -e

  if [[ -n "$package_lock_tmp" ]]; then
    cp "$package_lock_tmp" "$frontend_abs/package-lock.json"
    rm -f "$package_lock_tmp"
  fi
  if [[ -n "$yarn_lock_tmp" ]]; then
    cp "$yarn_lock_tmp" "$frontend_abs/yarn.lock"
    rm -f "$yarn_lock_tmp"
  fi
  [[ "$build_status" -eq 0 ]] || fail "Frontend build failed for $pid"

  [[ -f "$frontend_abs/dist/assets/remoteEntry.js" ]] || fail "Frontend build missing remoteEntry.js for $pid"
  rm -rf "$plugin_abs/dist"
  mkdir -p "$plugin_abs"
  cp -a "$frontend_abs/dist" "$plugin_abs/dist"
  [[ -f "$plugin_abs/dist/assets/remoteEntry.js" ]] || fail "Failed to copy frontend dist for $pid"
  log "Frontend assets copied to $plugin_dir/dist"
}

login_api() {
  if [[ -n "$AUTH_TOKEN" ]]; then
    return 0
  fi
  if [[ -n "$MP_TOKEN" ]]; then
    AUTH_TOKEN="$MP_TOKEN"
    return 0
  fi
  [[ -n "$MP_USERNAME" ]] || fail "MP_USERNAME or MP_TOKEN is required for MoviePilot API operations"
  [[ -n "$MP_PASSWORD" ]] || fail "MP_PASSWORD or MP_TOKEN is required for MoviePilot API operations"

  local response token
  response="$($CURL_BIN -fsS --connect-timeout 5 --max-time 30 \
    -X POST "$MP_BASE_URL/api/v1/login/access-token" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "username=$MP_USERNAME" \
    --data-urlencode "password=$MP_PASSWORD")" || fail "MoviePilot login failed"
  token="$(printf '%s' "$response" | jq -r '.access_token // empty')"
  [[ -n "$token" && "$token" != "null" ]] || fail "MoviePilot login did not return an access token"
  AUTH_TOKEN="$token"
}

api_get() {
  local path="$1"
  login_api
  $CURL_BIN -fsS --connect-timeout 5 --max-time 60 \
    -H "Authorization: Bearer $AUTH_TOKEN" \
    "$MP_BASE_URL$path"
}

reload_plugin() {
  local pid="$1"
  local response success
  log "Reloading $pid"
  response="$(api_get "/api/v1/plugin/reload/$pid")" || fail "Reload API failed for $pid"
  success="$(printf '%s' "$response" | jq -r '.success // empty' 2>/dev/null || true)"
  [[ "$success" == "true" ]] || fail "Reload API returned failure for $pid: $response"
}

check_scheduler_not_running() {
  local pid="$1"
  [[ "$FORCE" -eq 1 ]] && return 0
  local schedule active_count
  schedule="$(api_get "/api/v1/dashboard/schedule")" || fail "Cannot query MoviePilot schedule"
  active_count="$(printf '%s' "$schedule" | jq --arg pid "$pid" '
    [ .[]? | select((tostring | test($pid; "i")) and ((.status? // .state? // .running? // .job_state? // "") | tostring | test("running|executing|active|true|RUNNING|EXECUTING|ACTIVE|\u8fd0\u884c|\u6267\u884c"))) ] | length
  ')"
  if [[ "$active_count" != "0" ]]; then
    fail "$pid has active scheduler jobs. Pass --force to continue."
  fi
}

stage_repo() {
  log "Staging local repo to $CONTAINER_NAME:$CONTAINER_REPO_PATH"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "Dry run: skip staging"
    return 0
  fi

  tar -C "$REPO_DIR" \
    --exclude='.git' \
    --exclude='frontend' \
    --exclude='dev' \
    --exclude='docs/superpowers' \
    -cf - package.v2.json plugins.v2 | \
    docker exec -i \
      -e CONTAINER_REPO_PATH="$CONTAINER_REPO_PATH" \
      "$CONTAINER_NAME" sh -lc '
        set -eu
        tmp="${CONTAINER_REPO_PATH}.tmp"
        parent="$(dirname "$CONTAINER_REPO_PATH")"
        mkdir -p "$parent"
        rm -rf "$tmp"
        mkdir -p "$tmp"
        tar -xf - -C "$tmp"
        test -f "$tmp/package.v2.json"
        test -f "$tmp/plugins.v2/p115strmhelper/__init__.py"
        test -f "$tmp/plugins.v2/p115disk/__init__.py"
        rm -rf "$CONTAINER_REPO_PATH"
        mv "$tmp" "$CONTAINER_REPO_PATH"
      '
}

make_manual_backup() {
  local pid="$1"
  local lower ts
  lower="$(plugin_lower_for "$pid")"
  ts="$(date '+%Y%m%d-%H%M%S')"
  log "Creating manual backup for $pid: $ts"
  docker exec \
    -e PLUGIN_LOWER="$lower" \
    -e TS="$ts" \
    "$CONTAINER_NAME" sh -lc '
      set -eu
      base="/config/plugin_manual_backups/$PLUGIN_LOWER"
      backup="$base/$TS"
      runtime="/app/app/plugins/$PLUGIN_LOWER"
      persistent="/config/plugins_backup/$PLUGIN_LOWER"
      mkdir -p "$backup"
      if [ -d "$runtime" ]; then
        mkdir -p "$backup/runtime"
        cp -a "$runtime/." "$backup/runtime/"
      fi
      if [ -d "$persistent" ]; then
        mkdir -p "$backup/persistent"
        cp -a "$persistent/." "$backup/persistent/"
      fi
      rm -f "$base/latest"
      ln -s "$backup" "$base/latest"
      printf "%s\n" "$backup"
    '
}

install_with_plugin_helper() {
  local pid="$1"
  local repo_url
  repo_url="local://$pid?path=$CONTAINER_REPO_PATH&version=$PACKAGE_VERSION"
  log "Installing $pid using PluginHelper.install_local"
  docker exec -i \
    -e PID="$pid" \
    -e REPO_URL="$repo_url" \
    -e LOCAL_REPO_PATH="$CONTAINER_REPO_PATH" \
    "$CONTAINER_NAME" /opt/venv/bin/python - <<'PY'
import json
import sys
from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.schemas.types import SystemConfigKey

pid = __import__('os').environ['PID']
repo_url = __import__('os').environ['REPO_URL']
local_repo_path = __import__('os').environ['LOCAL_REPO_PATH']
settings.PLUGIN_LOCAL_REPO_PATHS = local_repo_path
state, msg = PluginHelper().install_local(pid=pid, repo_url=repo_url, force_install=False)
if state:
    oper = SystemConfigOper()
    installed = oper.get(SystemConfigKey.UserInstalledPlugins) or []
    if pid not in installed:
        installed.append(pid)
        oper.set(SystemConfigKey.UserInstalledPlugins, installed)
print(json.dumps({"success": bool(state), "message": msg}, ensure_ascii=True))
sys.exit(0 if state else 1)
PY
}

copy_only_install() {
  local pid="$1"
  local lower dir
  lower="$(plugin_lower_for "$pid")"
  dir="$(plugin_dir_for "$pid")"
  log "Installing $pid using emergency copy-only mode"
  docker exec \
    -e PLUGIN_LOWER="$lower" \
    -e PLUGIN_DIR="$dir" \
    -e CONTAINER_REPO_PATH="$CONTAINER_REPO_PATH" \
    "$CONTAINER_NAME" sh -lc '
      set -eu
      src="$CONTAINER_REPO_PATH/$PLUGIN_DIR"
      tmp="/tmp/mp-plugin-copy-$PLUGIN_LOWER"
      runtime="/app/app/plugins/$PLUGIN_LOWER"
      persistent="/config/plugins_backup/$PLUGIN_LOWER"
      test -d "$src"
      rm -rf "$tmp"
      mkdir -p "$tmp"
      cp -a "$src/." "$tmp/"
      rm -rf "$runtime"
      mkdir -p "$(dirname "$runtime")"
      cp -a "$tmp" "$runtime"
      if [ -f "$runtime/requirements.txt" ]; then
        /opt/venv/bin/python -m pip install -r "$runtime/requirements.txt"
      fi
      rm -rf "$persistent"
      mkdir -p "$(dirname "$persistent")"
      cp -a "$runtime" "$persistent"
      rm -rf "$tmp"
    '
}

restore_static_assets_if_needed() {
  local pid="$1"
  local lower dir
  lower="$(plugin_lower_for "$pid")"
  dir="$(plugin_dir_for "$pid")"
  docker exec \
    -e PLUGIN_LOWER="$lower" \
    -e PLUGIN_DIR="$dir" \
    -e CONTAINER_REPO_PATH="$CONTAINER_REPO_PATH" \
    "$CONTAINER_NAME" sh -lc '
      set -eu
      source_dist="$CONTAINER_REPO_PATH/$PLUGIN_DIR/dist"
      backup_dist="/config/plugin_manual_backups/$PLUGIN_LOWER/latest/runtime/dist"
      runtime_dist="/app/app/plugins/$PLUGIN_LOWER/dist"
      persistent_dist="/config/plugins_backup/$PLUGIN_LOWER/dist"

      if [ -d "$source_dist" ]; then
        exit 0
      fi
      if [ ! -d "$backup_dist" ]; then
        exit 0
      fi

      mkdir -p "$runtime_dist" "$persistent_dist"
      if ! find "$runtime_dist" -type f -print -quit 2>/dev/null | grep -q .; then
        cp -a "$backup_dist/." "$runtime_dist/"
      fi
      cp -a "$runtime_dist/." "$persistent_dist/"
      printf "Restored static assets for %s from latest backup\\n" "$PLUGIN_LOWER"
    '
}

rollback_latest() {
  local pid="$1"
  local lower
  lower="$(plugin_lower_for "$pid")"
  log "Rolling back $pid from latest manual backup"
  docker exec \
    -e PLUGIN_LOWER="$lower" \
    "$CONTAINER_NAME" sh -lc '
      set -eu
      latest="/config/plugin_manual_backups/$PLUGIN_LOWER/latest"
      [ -e "$latest" ] || { echo "latest backup not found" >&2; exit 1; }
      backup="$(readlink -f "$latest")"
      [ -d "$backup/runtime" ] || { echo "runtime backup not found: $backup" >&2; exit 1; }
      runtime="/app/app/plugins/$PLUGIN_LOWER"
      persistent="/config/plugins_backup/$PLUGIN_LOWER"
      rm -rf "$runtime"
      mkdir -p "$(dirname "$runtime")"
      cp -a "$backup/runtime" "$runtime"
      if [ -d "$backup/persistent" ]; then
        rm -rf "$persistent"
        mkdir -p "$(dirname "$persistent")"
        cp -a "$backup/persistent" "$persistent"
      else
        rm -rf "$persistent"
        mkdir -p "$(dirname "$persistent")"
        cp -a "$runtime" "$persistent"
      fi
      printf "%s\n" "$backup"
    '
  reload_plugin "$pid"
  verify_plugin_consistent "$pid"
}

container_version_for() {
  local pid="$1"
  local base="$2"
  local rel
  rel="$(version_rel_file_for "$pid")"
  docker exec \
    -e PID="$pid" \
    -e FILE_PATH="$base/$rel" \
    "$CONTAINER_NAME" sh -lc '
      set -eu
      [ -f "$FILE_PATH" ] || exit 3
      case "$PID" in
        P115StrmHelper)
          sed -nE "s/^VERSION[[:space:]]*=[[:space:]]*[\"'\''\"]([^\"'\''\"]+)[\"'\''\"].*/\1/p" "$FILE_PATH" | head -n 1
          ;;
        P115Disk)
          sed -nE "s/^[[:space:]]*plugin_version[[:space:]]*=[[:space:]]*[\"'\''\"]([^\"'\''\"]+)[\"'\''\"].*/\1/p" "$FILE_PATH" | head -n 1
          ;;
      esac
    '
}

verify_schedule_jobs() {
  local pid="$1"
  [[ "$pid" == "P115StrmHelper" ]] || return 0
  local schedule count
  schedule="$(api_get "/api/v1/dashboard/schedule")" || fail "Cannot query schedule for verification"
  count="$(printf '%s' "$schedule" | jq --arg pid "$pid" '[ .[]? | select(tostring | test($pid; "i")) ] | length')"
  if [[ "$count" == "0" ]]; then
    fail "No P115StrmHelper scheduler jobs found after reload"
  fi
  log "schedule jobs: $count matching P115StrmHelper"
}

verify_static_assets() {
  local pid="$1"
  local lower
  [[ "$pid" == "P115StrmHelper" ]] || return 0
  lower="$(plugin_lower_for "$pid")"
  docker exec \
    -e PLUGIN_LOWER="$lower" \
    "$CONTAINER_NAME" sh -lc '
      set -eu
      test -f "/app/app/plugins/$PLUGIN_LOWER/dist/assets/remoteEntry.js"
      test -f "/config/plugins_backup/$PLUGIN_LOWER/dist/assets/remoteEntry.js"
    ' || fail "Missing frontend static assets for $pid"
  log "static assets ok: $pid"
}

verify_plugin() {
  local pid="$1"
  local lower expected runtime_version backup_version runtime_base backup_base
  lower="$(plugin_lower_for "$pid")"
  expected="$(validate_versions "$pid")"
  runtime_base="/app/app/plugins/$lower"
  backup_base="/config/plugins_backup/$lower"
  runtime_version="$(container_version_for "$pid" "$runtime_base")" || fail "Cannot read runtime version for $pid"
  [[ "$runtime_version" == "$expected" ]] || fail "Runtime version mismatch for $pid: expected=$expected actual=$runtime_version"
  backup_version="$(container_version_for "$pid" "$backup_base")" || fail "Cannot read persistent backup version for $pid"
  [[ "$backup_version" == "$expected" ]] || fail "Persistent backup version mismatch for $pid: expected=$expected actual=$backup_version"
  log "version ok: $pid $expected"
  verify_static_assets "$pid"
  verify_schedule_jobs "$pid"
}

verify_plugin_consistent() {
  local pid="$1"
  local lower runtime_version backup_version runtime_base backup_base
  lower="$(plugin_lower_for "$pid")"
  runtime_base="/app/app/plugins/$lower"
  backup_base="/config/plugins_backup/$lower"
  runtime_version="$(container_version_for "$pid" "$runtime_base")" || fail "Cannot read runtime version for $pid"
  backup_version="$(container_version_for "$pid" "$backup_base")" || fail "Cannot read persistent backup version for $pid"
  [[ "$runtime_version" == "$backup_version" ]] || fail "Rollback version mismatch for $pid: runtime=$runtime_version backup=$backup_version"
  log "rollback version ok: $pid $runtime_version"
  verify_static_assets "$pid"
  verify_schedule_jobs "$pid"
}

print_plan_for_plugin() {
  local pid="$1"
  local version dir lower
  version="$(validate_versions "$pid")"
  dir="$(plugin_dir_for "$pid")"
  lower="$(plugin_lower_for "$pid")"
  printf 'Plugin: %s\n' "$pid"
  printf 'Version: %s\n' "$version"
  printf 'Source: %s\n' "$dir"
  printf 'Runtime: /app/app/plugins/%s\n' "$lower"
  printf 'Backup: /config/plugins_backup/%s\n' "$lower"
}

process_plugin() {
  local pid="$1"
  check_plugin_source "$pid"
  print_plan_for_plugin "$pid"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "Dry run: skip changes for $pid"
    return 0
  fi

  if [[ "$VERIFY_ONLY" -eq 1 ]]; then
    verify_plugin "$pid"
    return 0
  fi

  if [[ -n "$ROLLBACK_MODE" ]]; then
    rollback_latest "$pid"
    return 0
  fi

  check_scheduler_not_running "$pid"
  build_frontend_assets_if_needed "$pid"
  stage_repo
  make_manual_backup "$pid"
  if [[ "$COPY_ONLY" -eq 1 ]]; then
    copy_only_install "$pid"
  else
    install_with_plugin_helper "$pid"
  fi
  restore_static_assets_if_needed "$pid"
  reload_plugin "$pid"
  verify_plugin "$pid"
}

main() {
  require_cmd git

  parse_args "$@"
  check_repo

  local targets_text
  targets_text="$(expand_targets)" || exit 1
  mapfile -t pids <<< "$targets_text"

  if [[ "$CHECK_UPSTREAM" -eq 1 ]]; then
    check_upstream_updates "${pids[@]}"
    return 0
  fi

  CURL_BIN="$(command -v curl || true)"
  [[ -n "$CURL_BIN" ]] || fail "Required command not found: curl"
  require_cmd docker
  require_cmd jq
  require_cmd tar
  check_container

  for pid in "${pids[@]}"; do
    process_plugin "$pid"
  done
}

main "$@"
