{ lib, pkgs, ... }:
let machine_name = "homelab"; in
{
  imports = [
    # Include the results of the hardware scan.
    ./hardware-configuration.nix
  ];

  # Bootloader
  boot.loader = {
    systemd-boot.enable = true;
    efi.canTouchEfiVariables = true;
    timeout = 1;
  };

  time.timeZone = "Asia/Kolkata";
  i18n.defaultLocale = "en_IN";

  networking = {
    hostName = machine_name;
    networkmanager.enable = lib.mkDefault true;
  };

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

  # need for ssh access, quality of life
  services.openssh = {
    enable = true;
    ports = [ 22 ];
    settings = {
      PasswordAuthentication = true;
      UseDns = true;
    };
  };

  services.getty.autologinUser = machine_name;
  users.users.${machine_name} = {
    isNormalUser = true;
    description = "My Portable Home Server";
    extraGroups = [ "networkmanager" "wheel" ];
    packages = with pkgs; [
      git

      (yazi.override {
        extraPackages = with pkgs; [
          mediainfo
          udisks
        ];
        plugins = { inherit (pkgs.yaziPlugins) mount mediainfo; };
        settings.keymap.mgr.prepend_keymap = [
          {on = "M"; run = "plugin mount"; desc = "Mount Manager"; }
        ];
        settings.yazi = {
          mgr.show_hidden = true;
          tasks.image_alloc = 1024*1024*1024;
          plugin.prepend_preloaders = [
            {mime = "{audio,video,image}/*";  run = "mediainfo";}
            {mime = "application/subrip";     run = "mediainfo";}
            {mime = "application/postscript"; run = "mediainfo";}
          ];
          plugin.prepend_previewers = [
            {mime = "{audio,video,image}/*";  run = "mediainfo";}
            {mime = "application/subrip";     run = "mediainfo";}
            {mime = "application/postscript"; run = "mediainfo";}
          ];
        };
      })
    ];
  };

  services.create_ap = {
    enable = true;
    settings = {
      INTERNET_IFACE = "enu1u3";
      WIFI_IFACE = "wlP2p1s0";
      SSID = machine_name;
      PASSPHRASE = "12345678";
      IEEE80211N = 1;
      IEEE80211AC = 1;
      IEEE80211AX = 1;
      FREQ_BAND = 5;
    };
  };

  # This value determines the NixOS release from which the default
  # settings for stateful data, like file locations and database versions
  # on your system were taken. It‘s perfectly fine and recommended to leave
  # this value at the release version of the first install of this system.
  # Before changing this value read the documentation for this option
  # (e.g. man configuration.nix or on https://nixos.org/nixos/options.html).
  system.stateVersion = "25.05"; # Did you read the comment?
}
