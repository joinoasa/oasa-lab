#!/usr/bin/env bash
# Firewall: nothing comes in unless it is WireGuard, or SSH over WireGuard (or from SSH_FROM_LAN_CIDR if you set it).
# The job service and the SDR control ports listen on loopback or the lab network only, so no rule opens them.
#
#   sudo ./03-firewall.sh            apply, with a 2-minute automatic undo
#   sudo ./03-firewall.sh --confirm  keep it (after you checked a NEW ssh session still works)
. "$(dirname "$0")/lib.sh"
need_root; check_os

SAVED=/var/lib/oasa-fw/previous.nft     # the rules that were loaded before we changed anything
PENDING=/var/lib/oasa-fw/pending          # present from "apply" until "--confirm"
ROLLBACK=/usr/local/sbin/oasa-fw-rollback

stop_rollback() {
  run systemctl stop oasa-fw-rollback.timer oasa-fw-rollback.service 2>/dev/null || true
  run systemctl reset-failed oasa-fw-rollback.timer oasa-fw-rollback.service 2>/dev/null || true
}

if [ "${1:-}" = "--confirm" ]; then
  stop_rollback
  run rm -f "$PENDING"
  run systemctl enable --now nftables
  say "Firewall kept and enabled at boot"
  exit 0
fi

lan_ssh=""
if [ -n "$SSH_FROM_LAN_CIDR" ]; then lan_ssh="ip saddr $SSH_FROM_LAN_CIDR tcp dport 22 accept"; fi

say "Writing /etc/nftables.conf (default deny inbound)"
write_file /etc/nftables.conf 0644 root:root <<EOF
#!/usr/sbin/nft -f
# OASA lab firewall. Default: drop everything inbound and forwarded.
flush ruleset

table inet oasa {
  chain input {
    type filter hook input priority 0; policy drop;
    iif "lo" accept
    ct state established,related accept
    ct state invalid drop
    # DHCP replies (the PC gets its address from the router)
    udp sport 67 udp dport 68 accept
    # ping and IPv6 neighbour discovery, rate limited
    ip protocol icmp icmp type { echo-request, destination-unreachable, time-exceeded } limit rate 10/second accept
    meta l4proto ipv6-icmp limit rate 50/second accept
    # WireGuard: the only open port
    udp dport $WG_PORT accept
    # SSH, only through the WireGuard tunnel
    iifname "wg0" tcp dport 22 accept
    $lan_ssh
    # Everything else, including anything arriving on the lab network (Pluto+) and not asked for: dropped.
  }
  chain forward {
    type filter hook forward priority 0; policy drop;
  }
  chain output {
    type filter hook output priority 0; policy accept;
  }
}
EOF

say "Checking the rules"
run nft -c -f /etc/nftables.conf

say "Saving the rules that are loaded now, so an undo puts them back (not an empty firewall)"
run install -d -m 0700 /var/lib/oasa-fw
if [ ! -e "$PENDING" ]; then
  # First apply since the last confirm. If you run this script again before confirming, the saved set stays the original.
  if [ "$DRY_RUN" = 1 ]; then note "DRY: would save nft list ruleset to $SAVED"; else
    nft list ruleset > "$SAVED"
    : > "$PENDING"
    note "saved $(wc -l < "$SAVED") lines to $SAVED"
  fi
fi
write_file "$ROLLBACK" 0750 root:root <<'EOF'
#!/bin/sh
# Undo an unconfirmed firewall change: put back the rules that were loaded before, or none if there were none.
nft flush ruleset
if [ -s /var/lib/oasa-fw/previous.nft ]; then nft -f /var/lib/oasa-fw/previous.nft; fi
EOF

say "Applying (undo timer: 2 minutes)"
stop_rollback                      # running this script again must not fail on an old timer
run systemd-run --unit=oasa-fw-rollback --on-active=120 --description="undo OASA firewall unless confirmed" "$ROLLBACK"
run nft -f /etc/nftables.conf

say "Applied"
owner "Open a NEW terminal and make sure you can still log in (over WireGuard, or the LAN if you set SSH_FROM_LAN_CIDR)."
note "If it works, within 2 minutes run:  sudo ./03-firewall.sh --confirm"
note "If you are locked out, wait 2 minutes: the firewall goes back to what it was before ($SAVED)."
