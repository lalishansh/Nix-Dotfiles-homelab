{ lib, pkgs, port, machine_name, host_domain, media_location, appdata_location, ... }:
let
  this_service = lib.removeSuffix ".nix" (baseNameOf __curPos.file);
  media = "${appdata_location}/immich";
in
{
  services.immich = {
    # Admin: lalishansh@gmail.com, immich#:
    enable = true;
    # Use channel Immich (2.7.x) — it migrates DB dumps from 2.3.1.
    # Pinning 2.3.1 breaks against VectorChord 1.x shipped with current Postgres.
    host = "127.0.0.1";
    port = port;
    openFirewall = false;
    mediaLocation = media;
    accelerationDevices = null;
    machine-learning.enable = true;
    settings = {
      server.externalDomain = "http://${this_service}.${host_domain}";
      trash.days = 120;
      user.deleteDelay = 30;
    };
  };

  systemd.services.immich-server = {
    unitConfig.RequiresMountsFor = [ media ];
    serviceConfig.ReadWritePaths = [ media ];
  };
  systemd.services.immich-machine-learning = {
    unitConfig.RequiresMountsFor = [ media ];
    serviceConfig.ReadWritePaths = [ media ];
  };

  # Owner-only is fine for Immich; keep group readable so wheel/immich can inspect backups.
  systemd.tmpfiles.settings.immich-media.${media}.Z = {
    user = "immich";
    group = "immich";
    mode = "0766";
  };

  users.users.${machine_name}.extraGroups = [ "immich" ];
}
