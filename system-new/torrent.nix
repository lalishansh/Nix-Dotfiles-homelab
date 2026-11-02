{ lib, pkgs, port, appdata_location, machine_name, ... }:
{
  # sometimes `sudo rm /drives/Blood-Box/.Apps/qbittorrent/qBittorrent/config/lockfile` is needed to restart the service
  services.qbittorrent = {
    enable = true;
    webuiPort = port;
    profileDir = "${appdata_location}/qbittorrent";
    serverConfig = {
      LegalNotice.Accepted = true;
      Preferences = {
        WebUI = {
          Username = "admin";
          Password_PBKDF2 = "@ByteArray(ARQ77eY1NUZaQsuDHbIMCA==:0WMRkYTUWVT9wVvdDtHAjU9b3b7uB8NR1Gur2hmQCvCDpm39Q+PsJRJPaCU51dEiz+dTzh8qbPsL8WkFljQYFQ==)";
        };
        General.Locale = "en";
      };
    };
  };
  users.users.${machine_name}.extraGroups = [ "qbittorrent" ];
}