{ config, lib, pkgs, ... }:
let inherit (lib) mkOption types; in
{
  options = {
    machine_name = mkOption {
      type = types.str;
      default = "homelab";
      description = "Name of the machine, used for Username, SSID for WAP etc.";
    };
    system_mode = mkOption {
      type = lib.types.enum [ "wap" "wifi" "eth" ];
      default = "wap";
      description = "Legacy option; wireless mode is owned by dashboard.py / state.json at runtime.";
    };
    host_addr = mkOption {
      type = types.str;
      default = "192.168.12.1";
      description = "Stable VIP: WAP gateway, *.lan A records, dummy0, Tailscale advertise + Split DNS nameserver.";
    };
    host_domain = mkOption {
      type = types.str;
      default = "lan";
      description = "Local DNS domain for reverse-proxied services (e.g. photos.lan).";
    };
  };
}
