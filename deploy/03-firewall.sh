#!/usr/bin/env bash
# Firewall: nothing comes in unless it is WireGuard, or SSH over WireGuard (or from SSH_FROM_LAN_CIDR if you set it).
# The job service and the SDR control ports listen on loopback or the lab network only, so no rule opens them.
#
#   sudo ./03-firewall.sh            apply, with a 2-minute automatic undo
#   sudo ./03-firewall.sh --confirm  keep it (after you checked a NEW ssh session still works)
. "$(dirname "$0")/lib.sh"
need_root; check_os

if [ "${1:-}" = "--confirm" ]; then
  run systemctl stop oasa-fw-rollback.timer 2>/dev/null || true
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

say "Applying (undo timer: 2 minutes)"
run systemd-run --unit=oasa-fw-rollback --on-active=120 --description="undo OASA firewall unless confirmed" /usr/sbin/nft flush ruleset
run nft -f /etc/nftables.conf

say "Applied"
owner "Open a NEW terminal and make sure you can still log in (over WireGuard, or the LAN if you set SSH_FROM_LAN_CIDR)."
note "If it works, within 2 minutes run:  sudo ./03-firewall.sh --confirm"
note "If you are locked out, wait 2 minutes: the firewall undoes itself."
