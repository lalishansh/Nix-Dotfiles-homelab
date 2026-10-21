{ config, lib, pkgs,
  eth_interface ? "enu1u2",
  wpa_interface ? "wlP2p1s0",
  wpa_password ? "12345678",
  ... }:
{
  networking.networkmanager.unmanaged = [ wpa_interface ];
  services.create_ap = {
    enable = true;
    settings = {
      INTERNET_IFACE = eth_interface;
      WIFI_IFACE = wpa_interface;
      PASSPHRASE = wpa_password;
      SSID = config.machine_name;
      GATEWAY = config.host_addr;
      FREQ_BAND = 5;
    };
  };
}