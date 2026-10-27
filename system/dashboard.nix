{ config, lib, pkgs, ... }:
let
  statePath = "/home/${config.machine_name}/state.json";
  # Prefer sshfs live copy so edits apply after systemctl restart (no full rebuild).
  liveScript = "/home/${config.machine_name}/shared/system/dashboard.py";
  dashboard = pkgs.writeShellScriptBin "wireless-dashboard" ''
    if [ -f ${liveScript} ]; then
      exec ${lib.getExe pkgs.python3} ${liveScript} "$@"
      exit 0
    fi
    exec ${lib.getExe pkgs.python3} ${./dashboard.py} "$@"
  '';
in
{
  environment.systemPackages = with pkgs; [
    dashboard
    iw
    hostapd
    wpa_supplicant
    dnsmasq
    iproute2
    iptables
    iputils
    dhcpcd
    python3
  ];

  boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

  systemd.tmpfiles.rules = [
    "d /run/wireless-dashboard 0755 root root -"
    "d /home/${config.machine_name} 0755 ${config.machine_name} users -"
  ];

  networking.firewall.allowedTCPPorts = [ 8080 ];

  systemd.services.wireless-dashboard = {
    description = "Wireless WAP/WiFi dashboard";
    after = [ "network.target" ];
    wantedBy = [ "multi-user.target" ];
    path = with pkgs; [
      iw
      hostapd
      wpa_supplicant
      dnsmasq
      iproute2
      iptables
      iputils
      dhcpcd
      coreutils
      python3
    ];
    environment = {
      # Must match Caddy handle_path /wireless* on dashboard.lan (same-origin iframe).
      WIRELESS_DASHBOARD_BASE = "/wireless";
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${lib.getExe dashboard} -c ${statePath} -h ${config.host_addr} --serve 8080";
      Restart = "on-failure";
      RestartSec = 3;
    };
  };
}
