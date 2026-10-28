{ config, lib, pkgs, ... }:
let
  unstable = import (builtins.fetchTarball https://github.com/NixOS/nixpkgs/archive/nixos-unstable.tar.gz) { config = { allowUnfree = true; }; };
in
{
  imports = [
    ./options.nix
    ./dashboard.nix
    ./lan-services.nix
  ];

  networking.networkmanager.enable = false;

  # Dynacat (Glance fork): public Home + password-gated Admin (wireless UI).
  # Docs: https://dynacat.artur.zone/#authentication/per-page-access-control
  services.glance = {
    enable = true;
    package = unstable.dynacat;
    settings = {
      server = {
        port = 5678;
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
                  title = "Wireless";
                  # Same origin as dashboard.lan so iframe can sync theme vars
                  source = "http://dashboard.${config.host_domain}/wireless";
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

  # COMMON SETTINGS
  services.getty.autologinUser = config.machine_name;
  users.users.${config.machine_name} = {
    isNormalUser = true;
    description = "My Portable Home Server";
    extraGroups = [ "networkmanager" "wheel" ];
    packages = with pkgs; [
      git

      (yazi.override {
        extraPackages = [ mediainfo udisks ];
        plugins = { inherit (yaziPlugins) mount mediainfo; };
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
}