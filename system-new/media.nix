{ lib, pkgs, port, appdata_location, media_location, machine_name, host_addr, host_domain, ... }:
# assert port == 8096; # NOTE: Make sure this matches the port in the jellyfin configuration.
{
  services.jellyfin = {
    enable = true;
    dataDir = "${appdata_location}/jellyfin";
  };
  users.users.${machine_name}.extraGroups = [ "jellyfin" ];
}