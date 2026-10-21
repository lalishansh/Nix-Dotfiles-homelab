{ config, lib, pkgs, ... }:
{

  imports = [
    # Include the results of the hardware scan.
    ./hardware-configuration.nix
    ./system
  ];

  machine_name = "homelab";
  system_mode = "wifi"; # "eth" "wap" "wifi"
  host_addr = "192.168.1.12";

  # Bootloader
  boot.loader = {
    systemd-boot.enable = true;
    efi.canTouchEfiVariables = true;
    timeout = 1;
  };

  time.timeZone = "Asia/Kolkata";
  i18n.defaultLocale = "en_IN";

  # htop, Changing some settings for being Router specific
  programs.htop = {
    enable = true;
    settings = {
      hide_kernel_threads = false;
      hide_userland_threads = false;
      show_cpu_frequency = true;
      show_cpu_temprature = true;
    };
  };

  services.tailscale = {
    enable = true;
    useRoutingFeatures = "server";
    openFirewall = true;
    authKeyFile = "/etc/tailscale_psk";
    extraUpFlags = [
      "--reset"
      "--ssh=true" # TODO: Disable SSH via Tailscale
    ];
  };

  # This value determines the NixOS release from which the default
  # settings for stateful data, like file locations and database versions
  # on your system were taken. It‘s perfectly fine and recommended to leave
  # this value at the release version of the first install of this system.
  # Before changing this value read the documentation for this option
  # (e.g. man configuration.nix or on https://nixos.org/nixos/options.html).
  system.stateVersion = "25.05"; # Did you read the comment?
}
