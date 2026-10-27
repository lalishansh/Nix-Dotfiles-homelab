{ config, lib, pkgs, ... }:
{
  imports = [
    ./options.nix
    ./dashboard.nix
  ];

  networking.networkmanager.enable = false;

  services.glance = {
    enable = true;
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
                  # Via Tailscale HTTPS path proxy (see glance-serve). Local-only:
                  # source = "http://127.0.0.1:8080";
                  source = "/wireless";
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

  systemd.services.glance-serve = {
    description = "Tailscale serve Glance + wireless dashboard";
    after = [ "tailscaled.service" "wireless-dashboard.service" ];
    wants = [ "tailscaled.service" "wireless-dashboard.service" ];
    wantedBy = [ "multi-user.target" ];
    path = with pkgs; [ tailscale ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = pkgs.writeShellScript "glance-serve-start" ''
        ${lib.getExe pkgs.tailscale} serve --bg ${toString 5678}
        ${lib.getExe pkgs.tailscale} serve --bg --set-path=/wireless http://127.0.0.1:8080
      '';
      ExecStop = pkgs.writeShellScript "glance-serve-stop" ''
        ${lib.getExe pkgs.tailscale} serve reset || true
      '';
      Restart = "on-failure";
      RestartSec = 3;
    };
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