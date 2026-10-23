# Shared helpers for the deploy scripts. Source it; do not run it.
# Every script is safe to run again. DRY_RUN=1 prints what would change and changes nothing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN="${DRY_RUN:-0}"

# Settings (non-secret). Copy setup.env.example to setup.env and edit.
if [ -f "$HERE/setup.env" ]; then
  # shellcheck disable=SC1091
  . "$HERE/setup.env"
fi
: "${LAB_IFACE:=}"            # NIC the Pluto+ is plugged into (not your internet NIC)
: "${LAB_HOST_IP:=192.168.2.10/24}"
: "${PLUTO_IP:=192.168.2.1}"
: "${WG_PORT:=51820}"
: "${WG_SERVER_IP:=10.77.0.1/24}"
: "${OWNER_WG_PUBKEY:=}"
: "${OWNER_WG_IP:=10.77.0.2/32}"
: "${SSH_FROM_LAN_CIDR:=}"    # empty = no SSH from the LAN, only over WireGuard
: "${LAB_REF:=}"              # REQUIRED: the commit (40 hex characters) or release tag of oasa-lab to install
: "${LAB_REPO_URL:=https://github.com/joinoasa/oasa-lab}"

# resolve_ref REF -> prints the 40-character commit id. A full commit id is used as it is; a tag is looked up once
# and the commit is what gets installed (a moved tag later changes nothing). A branch name is refused: it moves.
resolve_ref() {
  local ref="$1" sha
  [ -n "$ref" ] || die "set LAB_REF in deploy/setup.env to the oasa-lab commit or release tag to install (see: git ls-remote $LAB_REPO_URL 'refs/tags/*')"
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then echo "$ref"; return 0; fi
  sha="$(git ls-remote "$LAB_REPO_URL" "refs/tags/$ref^{}" "refs/tags/$ref" | awk 'NR==1{print $1}')"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "LAB_REF='$ref' is not a full commit id or a tag of $LAB_REPO_URL. Branch names are not accepted: they move."
  echo "$sha"
}

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
owner() { printf '\n  >>> OWNER STEP: %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = 1 ]; then printf '    DRY: '; printf '%q ' "$@"; echo; else "$@"; fi
}

need_root() {
  if [ "$DRY_RUN" = 1 ]; then return 0; fi
  [ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
}

check_os() {
  . /etc/os-release
  if [ "${ID:-}" != ubuntu ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    note "warning: written for Ubuntu 24.04 LTS, this is ${PRETTY_NAME:-unknown}"
  fi
}

# write_file PATH MODE OWNER:GROUP  (content on stdin). Replaces only if the content changed.
write_file() {
  local path="$1" mode="$2" own="$3" tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  if [ -f "$path" ] && cmp -s "$tmp" "$path"; then
    note "unchanged: $path"
    rm -f "$tmp"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    note "DRY: would write $path ($mode $own)"
    rm -f "$tmp"
    return 0
  fi
  [ -f "$path" ] && cp -p "$path" "$path.bak-$(date +%Y%m%d%H%M%S)"
  install -D -m "$mode" -o "${own%%:*}" -g "${own##*:}" "$tmp" "$path"
  rm -f "$tmp"
  note "wrote: $path"
}

pkg_install() {
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}
