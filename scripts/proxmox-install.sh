#!/usr/bin/env bash
# mcp-brain — Proxmox VE one-shot installer
#
# Run on a Proxmox VE host shell (not inside an LXC!). This creates a
# new unprivileged Debian LXC with Docker-friendly features enabled,
# then runs scripts/install.sh inside it to bring up mcp-brain.
#
# Usage (interactive, on the Proxmox host):
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/RyKaT07/mcp-brain/main/scripts/proxmox-install.sh)"
#
# Unattended (all defaults, no prompts):
#     ASSUME_YES=1 bash -c "$(curl -fsSL .../proxmox-install.sh)"
#
# Environment overrides (all optional):
#     CTID               next free id from /cluster/nextid
#     CT_HOSTNAME           mcp-brain
#     CORES              1
#     RAM_MB             1024
#     SWAP_MB            512
#     DISK_GB            8
#     STORAGE            first storage that supports rootdir
#     BRIDGE             vmbr0
#     IP                 dhcp   (or CIDR like 10.0.0.42/24 — then also GATEWAY)
#     GATEWAY            (only if IP is not dhcp)
#     NAMESERVER         DNS server for the LXC (default: inherit from host)
#     SEARCHDOMAIN       DNS search domain for the LXC (default: inherit from host)
#     TEMPLATE           debian-13-standard_*.tar.zst  (latest matching one)
#     CT_PASSWORD        random 24-char if unset
#     SSH_PUBKEY         a single public key as a string ('ssh-ed25519 AAAA...')
#     SSH_KEY_FILE       path to an authorized_keys file (no default)
#                        SSH_PUBKEY and SSH_KEY_FILE are mutually exclusive;
#                        if both are set, SSH_KEY_FILE wins.
#     MCP_BRAIN_REPO     RyKaT07/mcp-brain
#     MCP_BRAIN_BRANCH   main
#     ASSUME_YES=1       skip the confirmation prompt

set -euo pipefail

# -----------------------------------------------------------------------------
# cosmetics + logging
# -----------------------------------------------------------------------------

# Cleanup temp files (e.g. materialized SSH_PUBKEY) on any exit.
TMP_PUBKEY=""
cleanup() {
    [ -n "$TMP_PUBKEY" ] && [ -f "$TMP_PUBKEY" ] && rm -f "$TMP_PUBKEY"
}
trap cleanup EXIT

if [ -t 1 ]; then
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_BOLD="" C_RESET=""
fi

# All log helpers go to stderr — never to stdout — so functions that use
# stdout to return a value (e.g. pick_template) can be safely captured
# with $(...) without colour escapes leaking into the captured string.
log()   { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*" >&2; }
ok()    { printf '%s✓%s %s\n'   "$C_GREEN" "$C_RESET" "$*" >&2; }
warn()  { printf '%s!%s %s\n'   "$C_YELLOW" "$C_RESET" "$*" >&2; }
fail()  { printf '%s✗%s %s\n'   "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# preconditions
# -----------------------------------------------------------------------------

preflight() {
    [ "$(id -u)" -eq 0 ] || fail "must run as root on the Proxmox host"

    command -v pveversion >/dev/null 2>&1 || fail "pveversion not found — this script must run on a Proxmox VE host"
    command -v pct >/dev/null 2>&1        || fail "pct not found — is this a Proxmox VE host?"
    command -v pveam >/dev/null 2>&1      || fail "pveam not found — is this a Proxmox VE host?"
    command -v pvesm >/dev/null 2>&1      || fail "pvesm not found — is this a Proxmox VE host?"

    ok "running on $(pveversion | head -1)"
}

# -----------------------------------------------------------------------------
# config resolution
# -----------------------------------------------------------------------------

pick_ctid() {
    if [ -n "${CTID:-}" ]; then
        echo "$CTID"
        return
    fi
    if command -v pvesh >/dev/null 2>&1; then
        pvesh get /cluster/nextid 2>/dev/null && return
    fi
    # Fallback: scan /etc/pve/lxc/
    local id=100
    while [ -f "/etc/pve/lxc/${id}.conf" ]; do
        id=$((id + 1))
    done
    echo "$id"
}

pick_storage() {
    if [ -n "${STORAGE:-}" ]; then
        echo "$STORAGE"
        return
    fi
    # First storage that advertises rootdir content
    pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3 == "active" {print $1; exit}'
}

pick_template() {
    if [ -n "${TEMPLATE:-}" ]; then
        echo "$TEMPLATE"
        return
    fi
    log "refreshing template list (pveam update)"
    pveam update >/dev/null 2>&1 || warn "pveam update failed — continuing with cached list"
    # Pick the newest available debian-13-standard template
    local latest
    latest=$(pveam available --section system 2>/dev/null \
        | awk '/debian-13-standard/ {print $2}' \
        | sort -V \
        | tail -1)
    [ -n "$latest" ] || fail "no debian-13-standard template available — try 'pveam update' manually"
    echo "$latest"
}

ensure_template_downloaded() {
    local template="$1"
    local storage_for_tpl="local"
    # Is it already in the local template storage?
    if pveam list "$storage_for_tpl" 2>/dev/null | awk '{print $1}' | grep -q "${template}\$"; then
        ok "template already present: $template"
        return
    fi
    log "downloading template $template into $storage_for_tpl"
    pveam download "$storage_for_tpl" "$template"
    ok "template downloaded"
}

generate_password() {
    # 24 chars, urlsafe-ish
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 18 | tr -d '/+=\n'
    else
        head -c 18 /dev/urandom | base64 | tr -d '/+=\n'
    fi
}

# -----------------------------------------------------------------------------
# build config (defaults + env)
# -----------------------------------------------------------------------------

build_config() {
    CTID="$(pick_ctid)"
    CT_HOSTNAME="${CT_HOSTNAME:-mcp-brain}"
    CORES="${CORES:-1}"
    RAM_MB="${RAM_MB:-1024}"
    SWAP_MB="${SWAP_MB:-512}"
    DISK_GB="${DISK_GB:-8}"
    STORAGE="$(pick_storage)"
    [ -n "$STORAGE" ] || fail "could not auto-detect a storage with rootdir content — set STORAGE env"
    BRIDGE="${BRIDGE:-vmbr0}"
    IP="${IP:-dhcp}"
    GATEWAY="${GATEWAY:-}"
    NAMESERVER="${NAMESERVER:-}"
    SEARCHDOMAIN="${SEARCHDOMAIN:-}"
    TEMPLATE="$(pick_template)"
    CT_PASSWORD="${CT_PASSWORD:-$(generate_password)}"
    SSH_KEY_FILE="${SSH_KEY_FILE:-}"
    SSH_PUBKEY="${SSH_PUBKEY:-}"

    # Materialize an inline SSH_PUBKEY into a tempfile so the existing
    # SSH_KEY_FILE plumbing handles both paths uniformly. SSH_KEY_FILE
    # wins if both are set.
    if [ -z "$SSH_KEY_FILE" ] && [ -n "$SSH_PUBKEY" ]; then
        case "$SSH_PUBKEY" in
            ssh-rsa\ *|ssh-ed25519\ *|ecdsa-sha2-*\ *|sk-*\ *) ;;
            *) fail "SSH_PUBKEY does not look like a public key (should start with ssh-rsa / ssh-ed25519 / ecdsa-sha2-...)" ;;
        esac
        TMP_PUBKEY="$(mktemp)"
        printf '%s\n' "$SSH_PUBKEY" > "$TMP_PUBKEY"
        chmod 600 "$TMP_PUBKEY"
        SSH_KEY_FILE="$TMP_PUBKEY"
    fi

    MCP_BRAIN_REPO="${MCP_BRAIN_REPO:-RyKaT07/mcp-brain}"
    MCP_BRAIN_BRANCH="${MCP_BRAIN_BRANCH:-main}"

    if [ "$IP" != "dhcp" ] && [ -z "$GATEWAY" ]; then
        fail "static IP requested ($IP) but GATEWAY env is empty"
    fi
}

show_config() {
    echo
    printf '%s%smcp-brain LXC configuration%s\n' "$C_BOLD" "$C_BLUE" "$C_RESET"
    printf '  CTID:      %s\n' "$CTID"
    printf '  hostname:  %s\n' "$CT_HOSTNAME"
    printf '  template:  %s\n' "$TEMPLATE"
    printf '  storage:   %s\n' "$STORAGE"
    printf '  disk:      %s GB\n' "$DISK_GB"
    printf '  cores:     %s\n' "$CORES"
    printf '  ram:       %s MB\n' "$RAM_MB"
    printf '  swap:      %s MB\n' "$SWAP_MB"
    printf '  bridge:    %s\n' "$BRIDGE"
    printf '  ip:        %s\n' "$IP"
    [ -n "$GATEWAY" ]      && printf '  gateway:   %s\n' "$GATEWAY"
    [ -n "$NAMESERVER" ]   && printf '  dns:       %s\n' "$NAMESERVER"
    [ -n "$SEARCHDOMAIN" ] && printf '  search:    %s\n' "$SEARCHDOMAIN"
    printf '  repo:      github.com/%s@%s\n' "$MCP_BRAIN_REPO" "$MCP_BRAIN_BRANCH"
    echo
}

confirm_or_abort() {
    [ "${ASSUME_YES:-0}" = "1" ] && return
    [ -t 0 ] || return  # non-interactive (piped), assume yes
    printf 'Proceed? [Y/n] '
    read -r answer
    case "${answer:-Y}" in
        Y|y|Yes|yes|'') ;;
        *) fail "aborted by user" ;;
    esac
}

# -----------------------------------------------------------------------------
# LXC create + start
# -----------------------------------------------------------------------------

create_lxc() {
    local template_path="local:vztmpl/${TEMPLATE}"

    local net="name=eth0,bridge=${BRIDGE},firewall=1"
    if [ "$IP" = "dhcp" ]; then
        net="${net},ip=dhcp"
    else
        net="${net},ip=${IP},gw=${GATEWAY}"
    fi

    local pct_args=(
        "$CTID" "$template_path"
        --hostname "$CT_HOSTNAME"
        --cores "$CORES"
        --memory "$RAM_MB"
        --swap "$SWAP_MB"
        --rootfs "${STORAGE}:${DISK_GB}"
        --unprivileged 1
        --features "nesting=1,keyctl=1"
        --net0 "$net"
        --ostype debian
        --start 0
        --onboot 1
        --password "$CT_PASSWORD"
    )

    if [ -n "$NAMESERVER" ]; then
        pct_args+=(--nameserver "$NAMESERVER")
    fi

    if [ -n "$SEARCHDOMAIN" ]; then
        pct_args+=(--searchdomain "$SEARCHDOMAIN")
    fi

    if [ -n "$SSH_KEY_FILE" ]; then
        [ -f "$SSH_KEY_FILE" ] || fail "SSH_KEY_FILE not found: $SSH_KEY_FILE"
        pct_args+=(--ssh-public-keys "$SSH_KEY_FILE")
    fi

    log "creating LXC $CTID ($CT_HOSTNAME)"
    pct create "${pct_args[@]}"
    ok "LXC $CTID created"
}

start_lxc() {
    log "starting LXC $CTID"
    pct start "$CTID"
    ok "LXC started"
}

wait_for_network() {
    log "waiting for network inside the LXC"
    local i
    for i in $(seq 1 60); do
        if pct exec "$CTID" -- sh -c 'getent hosts raw.githubusercontent.com >/dev/null 2>&1 && ping -c 1 -W 2 raw.githubusercontent.com >/dev/null 2>&1'; then
            ok "network up after ${i}s"
            return
        fi
        sleep 1
    done
    fail "LXC did not reach the internet within 60s — check bridge/DHCP"
}

# -----------------------------------------------------------------------------
# install mcp-brain inside the LXC
# -----------------------------------------------------------------------------

install_inside() {
    log "installing curl + ca-certificates inside the LXC"
    pct exec "$CTID" -- bash -c 'DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends curl ca-certificates' \
        || fail "apt-get failed inside the LXC"

    log "downloading mcp-brain installer from github.com/${MCP_BRAIN_REPO}@${MCP_BRAIN_BRANCH}"
    local raw="https://raw.githubusercontent.com/${MCP_BRAIN_REPO}/${MCP_BRAIN_BRANCH}/scripts/install.sh"
    # Two-step: curl → /tmp/install.sh, then bash /tmp/install.sh. Avoids
    # process substitution (bash <(...)) which can misbehave inside
    # 'pct exec' non-interactive shells on minimal Debian templates.
    pct exec "$CTID" -- bash -c "curl -fsSL '${raw}' -o /tmp/mcp-brain-install.sh" \
        || fail "failed to download install.sh into the LXC"
    pct exec "$CTID" -- bash -c "chmod +x /tmp/mcp-brain-install.sh" \
        || fail "chmod on install.sh failed"

    log "running mcp-brain installer inside the LXC"
    pct exec "$CTID" -- env \
        MCP_BRAIN_REPO="${MCP_BRAIN_REPO}" \
        MCP_BRAIN_BRANCH="${MCP_BRAIN_BRANCH}" \
        bash /tmp/mcp-brain-install.sh install \
        || fail "mcp-brain install.sh failed inside the LXC — see output above"

    pct exec "$CTID" -- rm -f /tmp/mcp-brain-install.sh || true
}

extract_state() {
    LXC_IP="$(pct exec "$CTID" -- bash -c "hostname -I | awk '{print \$1}'" 2>/dev/null | tr -d '[:space:]')"
    MCP_TOKEN="$(pct exec "$CTID" -- awk -F'"' '/^[[:space:]]*token:/ {print $2; exit}' /opt/mcp-brain/data/auth.yaml 2>/dev/null || echo "")"
}

print_summary() {
    echo
    printf '%s%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
    printf '%s%s  mcp-brain is running%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
    printf '%s%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
    echo
    printf '  CTID:       %s\n' "$CTID"
    printf '  hostname:   %s\n' "$CT_HOSTNAME"
    printf '  LXC IP:     %s\n' "${LXC_IP:-unknown}"
    printf '  service:    http://%s:8400/healthz\n' "${LXC_IP:-LXC_IP}"
    echo
    printf '%s  SAVE THESE CREDENTIALS — they will not be shown again:%s\n' "$C_YELLOW" "$C_RESET"
    echo
    printf '  root pw:    %s\n' "$CT_PASSWORD"
    printf '  mcp token:  %s\n' "${MCP_TOKEN:-<could not read auth.yaml — run inside LXC: cat /opt/mcp-brain/data/auth.yaml>}"
    echo
    cat <<EOF
Next steps
==========
1. SSH into the LXC (or 'pct enter ${CTID}') and verify the health check:
     curl http://127.0.0.1:8400/healthz

2. Put a reverse proxy with TLS (Caddy, Traefik, nginx) in front of the
   LXC's port 8400. The bearer token is enforced by mcp-brain itself —
   the reverse proxy only terminates TLS. See docs/Caddyfile.example.

3. Add the server to Claude Code's MCP config (~/.claude.json):

     {
       "mcpServers": {
         "brain": {
           "type": "sse",
           "url": "https://your.domain.tld/sse",
           "headers": { "Authorization": "Bearer ${MCP_TOKEN:-<TOKEN>}" }
         }
       }
     }

4. Update later with (from inside the LXC):
     sudo bash /opt/mcp-brain/scripts/install.sh update

Full walkthrough: https://github.com/${MCP_BRAIN_REPO}/blob/${MCP_BRAIN_BRANCH}/docs/deployment.md
EOF
}

# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

main() {
    preflight
    build_config
    show_config
    confirm_or_abort
    ensure_template_downloaded "$TEMPLATE"
    create_lxc
    start_lxc
    wait_for_network
    install_inside
    extract_state
    print_summary
}

main "$@"
