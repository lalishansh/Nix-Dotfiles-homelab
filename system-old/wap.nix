{ config, lib, pkgs,
  ethernet ? "enu1u2",
  interface ? "wlP2p1s0",
  name ? config.machine_name,
  password ? "12345678",
  ... }:
{
  networking.networkmanager.unmanaged = [ interface ];
  services.create_ap = {
    enable = true;
    settings = {
      INTERNET_IFACE = ethernet;
      WIFI_IFACE = interface;
      PASSPHRASE = password;
      SSID = name;
      GATEWAY = config.host_addr;
      # FREQ_BAND = 5; # not working for me right now
    };
  };
}