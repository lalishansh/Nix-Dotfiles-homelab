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
      description = "Mode for system to operate in: wap = Wireless Access Point, eth = Ethernet Node, wifi = WiFi Node";
    };
    host_addr = mkOption {
      type = types.str;
      default = "192.168.1.12";
      description = "Host Gateway IP Address";
    };
  };
}