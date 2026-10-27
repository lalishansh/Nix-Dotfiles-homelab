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

  services.glance = {
    enable = true;
    package = unstable.dynacat; # Fork of glance with additional widgets, per page authentication, etc.
    settings = {
      server.port = 5678;
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
                {
                  type = "iframe";
                  title = "Wireless";
                  # Same origin as Glance (dashboard.lan) so iframe can sync theme vars
                  source = "http://dashboard.${config.host_domain}/wireless";
                  height = 720;
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