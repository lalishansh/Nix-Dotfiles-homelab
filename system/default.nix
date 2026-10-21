{ config, lib, pkgs, ... }:
let
  inherit (lib) mkIf mkMerge;
  mode = config.system_mode;
in {
  imports = let for = name: extra: mkIf (mode == name) (import ./${name}.nix ({ inherit config lib pkgs; } // extra)); in [
    ./options.nix
    (for "wap" {eth_interface = "enu1u2"; name = "BloodPoint"; password = "12345678";})
    (for "wifi" {name = "moto g(60)_3483"; password = "1q2w3e4r";})
    (for "eth")
  ];


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