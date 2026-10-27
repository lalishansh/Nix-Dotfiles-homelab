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
  };
}