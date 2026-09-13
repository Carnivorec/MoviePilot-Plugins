#!/usr/bin/env bash
# Exercise the real installer functions in an unmounted, offline container.
set -Eeuo pipefail
TEST_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" != --failure ]]; then
  export AUDIT_CONTAINER="mp-installer-audit-$(date +%s)-$$"
  export INSTALL_CONFIG_FILE
  INSTALL_CONFIG_FILE="$(mktemp)"
  cleanup() {
    docker rm -fv "$AUDIT_CONTAINER" >/dev/null 2>&1 || true
    rm -f -- "$INSTALL_CONFIG_FILE"
  }
  trap cleanup EXIT
  docker create --name "$AUDIT_CONTAINER" --network none --no-healthcheck \
    --entrypoint /bin/sh jxxghp/moviepilot-v2:latest -c 'sleep 600' >/dev/null
  docker start "$AUDIT_CONTAINER" >/dev/null
fi
[[ "${AUDIT_CONTAINER:-}" == mp-installer-audit-* ]] || exit 2
export CONTAINER_NAME="$AUDIT_CONTAINER"
export CONTAINER_REPO_PATH=/config/plugin_forks/MoviePilot-Plugins
source "$TEST_REPO/install.sh"
reload_plugin() { :; }
verify_static_assets() { :; }
verify_schedule_jobs() { :; }
check_scheduler_not_running() { :; }
build_frontend_assets_if_needed() { :; }

if [[ "${1:-}" == --failure ]]; then
  install_with_plugin_helper() {
    docker exec "$CONTAINER_NAME" sh -c 'echo broken > /app/app/plugins/p115disk/__init__.py'
    return 42
  }
  process_plugin P115Disk
  exit 99
fi

docker exec "$CONTAINER_NAME" sh -c '
  set -eu
  repo=/config/plugin_forks/MoviePilot-Plugins
  mkdir -p "$repo/plugins.v2/p115disk" "$repo/plugins.v2/p115strmhelper" \
    /app/app/plugins/p115disk /config/temp/plugin_backup/p115disk
  printf "plugin_version = \"99.1.0\"\n" > /app/app/plugins/p115disk/__init__.py
  cp /app/app/plugins/p115disk/__init__.py /config/temp/plugin_backup/p115disk/__init__.py
  cp /app/app/plugins/p115disk/__init__.py "$repo/plugins.v2/p115disk/__init__.py"
  echo untouched > "$repo/plugins.v2/p115strmhelper/marker"
  printf "%s\n" '\''{"P115Disk":{"version":"99.1.0","history":{}},"P115StrmHelper":{"version":"99.2.0","history":{}}}'\'' > "$repo/package.v2.json"
  mkdir -p /config/not-a-repo
  echo preserve > /config/not-a-repo/marker
  ln -s "$repo" /config/symlink-repo
'
for unsafe in /config /config/not-a-repo /config/symlink-repo; do
  if (CONTAINER_REPO_PATH="$unsafe"; validate_container_repo_path) >/dev/null 2>&1; then
    echo "Unsafe path accepted: $unsafe" >&2
    exit 1
  fi
done
make_manual_backup P115Disk
stage_repo P115Disk
rollback_latest P115Disk
assert_restored() {
  verify_plugin_consistent P115Disk
  [[ "$(container_version_for P115Disk /app/app/plugins/p115disk)" == 99.1.0 ]]
  docker exec "$CONTAINER_NAME" sh -c '
    set -eu
    repo=/config/plugin_forks/MoviePilot-Plugins
    test "$(cat "$repo/plugins.v2/p115strmhelper/marker")" = untouched
    jq -e '\''.P115StrmHelper.version == "99.2.0"'\'' "$repo/package.v2.json" >/dev/null
    test "$(cat /config/not-a-repo/marker)" = preserve
    cmp /app/app/plugins/p115disk/__init__.py /config/temp/plugin_backup/p115disk/__init__.py
    cmp /app/app/plugins/p115disk/__init__.py "$repo/plugins.v2/p115disk/__init__.py"
  '
}
assert_restored
set +e
bash "$TEST_REPO/tests/test_installer.sh" --failure
failure_status=$?
set -e
[[ "$failure_status" -eq 42 ]] || { echo "Wrong failure status: $failure_status" >&2; exit 1; }
assert_restored
echo 'Installer isolation checks passed: unsafe paths, rollback, catalog/source isolation, failed-install recovery'
