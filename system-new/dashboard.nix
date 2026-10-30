{ lib, pkgs, port, machine_name, host_addr, host_domain, ... }:
let
  unstable = import (builtins.fetchTarball https://github.com/NixOS/nixpkgs/archive/nixos-unstable.tar.gz) { config = { allowUnfree = true; }; };
  statePath = "/home/${machine_name}/state.json";
  # Prefer live copy so edits apply after systemctl restart (no full rebuild).
  # UI listens on 8080; Glance uses `port` (e.g. 5678). Caddy: /system → :8080, / → :port.
  uiPort = 8080;
  system-dashboard =
    let src = "system-dashboard.py";
    in pkgs.writeShellScriptBin "system-dashboard" ''
      liveScript="/home/${machine_name}/shared/system-new/${src}"
      if [ -f "$liveScript" ]; then
        exec ${lib.getExe pkgs.python3} "$liveScript" "$@"
      else
        exec ${lib.getExe pkgs.python3} ${./. + "/${src}"} "$@"
      fi
    '';
in {
  services.glance = {
    enable = true;
    package = unstable.dynacat;
    settings = {
      server = {
        port = port;
        proxied = true; # behind Caddy; needed for auth brute-force IP
      };
      auth = {
        # ./dynacat secret:make — rotate if this config is public
        "secret-key" = "7mpLsWSfs4jLQT/oaF0sS2fzr388YNtQU3hCtHZCqQyLEGZirbO4H0nBcZlbveDi9BcMsnpI1TFNnB/KNnxs8w==";
        "require-auth" = false; # Home public; Admin still needs login
        users = {
          admin = {
            password = "123412341234";
          };
        };
      };
      pages = [
        {
          name = "Home";
          columns = [
            {
              size = "full";
              widgets = [
                { type = "calendar"; }
                {
                  type = "weather";
                  location = "Bengaluru, India";
                }
              ];
            }
          ];
        }
        {
          name = "Admin";
          "allowed-users" = [ "admin" ];
          columns = [
            {
              size = "full";
              widgets = [
                {
                  type = "iframe";
                  title = "System";
                  # Same origin as dashboard.lan so iframe can sync theme vars
                  # IMPORTANT: Must match Caddy handle_path /system* on dashboard.lan (same-origin iframe).
                  source = "http://dashboard.${host_domain}/system";
                  height = 756;
                }
              ];
            }
          ];
        }
      ];
    };
    openFirewall = true;
  };

  boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

  systemd.tmpfiles.rules = [
    "d /run/system-dashboard 0755 root root -"
    "d /home/${machine_name} 0755 ${machine_name} users -"
  ];

  networking.firewall.allowedTCPPorts = [ uiPort ];

  systemd.services.system-dashboard = {
    description = "System WAP/WiFi + Misc. dashboard";
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
      # Must match Caddy handle_path /system* on dashboard.lan (same-origin iframe).
      # system-dashboard.py reads SYSTEM_DASHBOARD_BASE (fallback: WIRELESS_DASHBOARD_BASE).
      SYSTEM_DASHBOARD_BASE = "/system";
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${lib.getExe system-dashboard} -c ${statePath} -h ${host_addr} --serve ${toString uiPort}";
      Restart = "on-failure";
      RestartSec = 3;
    };
  };
}