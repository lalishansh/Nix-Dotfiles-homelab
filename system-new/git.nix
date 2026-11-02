{ lib, pkgs, port, appdata_location, media_location, machine_name, host_domain, ... }:
let this_service = lib.removeSuffix ".nix" (baseNameOf __curPos.file); in
{
  # Use this command to create an admin user: 
  # sudo -u forgejo env FORGEJO_WORK_DIR=/var/lib/forgejo FORGEJO_CUSTOM=/var/lib/forgejo/custom "$FORGEJO" admin user create --admin --username superuser --password "superuser" --email superuser@example.com --must-change-password=false

  services.forgejo = {
    enable = true;
    lfs.enable = true;
    settings = {
      server = {
        DOMAIN = "${this_service}.${host_domain}";
        ROOT_URL = "http://${this_service}.${host_domain}";
        HTTP_PORT = port;
      };
      service.DISABLE_REGISTRATION = true; 
      # Add support for actions, based on act: https://github.com/nektos/act
      # TODO: need to setup a forgejo-runner service to run the actions
      actions = {
        ENABLED = true;
        DEFAULT_ACTIONS_URL = "github";
      };
    };
  };
  users.users.${machine_name}.extraGroups = [ "forgejo" ];
}