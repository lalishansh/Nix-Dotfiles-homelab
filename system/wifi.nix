{ config, lib, pkgs,
  interface ? "wlP2p1s0",
  name ? "BloodPoint",
  password ? "12345678",
  ... }:
{
  networking = {
    networkmanager.unmanaged = [ interface ];
    interfaces."${interface}" = {
      useDHCP = true;
      ipv4.addresses = [{ address = config.host_addr; prefixLength = 24; }];
    };
    wireless = {
      enable = true;
      interfaces = [ interface ];
      networks."${name}".psk = password;
    };
  };
}