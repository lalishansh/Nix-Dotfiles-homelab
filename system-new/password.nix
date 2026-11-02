{ lib, pkgs, port, appdata_location, machine_name, host_domain, ... }:
let
  this_service = lib.removeSuffix ".nix" (baseNameOf __curPos.file);
  envFile = "${appdata_location}/vaultwarden/vaultwarden.env"; 
in
{
  services.vaultwarden = {
    enable = true;
    backupDir = "${appdata_location}/vaultwarden/backups";
    environmentFile = envFile;

    config = {
      ROCKET_PORT = port;
      SIGNUPS_ALLOWED = "true";
      # Do NOT set `domain` — the NixOS module prefixes it with https://,
      # which produced DOMAIN=https://https://password.lan and 404s the vault.
      DOMAIN = "https://${this_service}.${host_domain}";
      # you (most likely) run bitwarden clients with `--ignore-certificate-errors`
    };
  };

  users.users.${machine_name}.extraGroups = [ "vaultwarden" ];
}