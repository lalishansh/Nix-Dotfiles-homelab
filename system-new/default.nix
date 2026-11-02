{ config, lib, pkgs, ... }:
let inherit (lib) mkOption types mapAttrsToList flatten mapAttrs' nameValuePair; in
{
  options = {
    machine_name = mkOption {
      type = types.str;
      default = "homelab";
      description = "Name of the machine, used for Username, SSID for WAP etc.";
    };
    host_addr = mkOption {
      type = types.str;
      default = "192.168.12.1";
      description = "Stable VIP: WAP gateway, *.lan A records, dummy0, Tailscale advertise + Split DNS nameserver.";
    };
    host_domain = mkOption {
      type = types.str;
      default = "lan";
      description = "Local DNS domain for reverse-proxied services (e.g. photos.lan).";
    };
    media_location = mkOption {
      type = types.str;
      description = "Location of the media files for the services.";
    };
    appdata_location = mkOption {
      type = types.str;
      description = "Location of the appdata for the services.";
    };

    # home_service.<name>.port = <port>;
    home_services = mkOption {
      type = types.attrsOf (types.submodule {
        options = {
          port = mkOption {
            type = types.int;
            description = "Port to expose the service on.";
          };
        };
      });
      description = "Home services to expose to the network.";
    };
  };

  config = let
    forService = name: import ./${name}.nix {
      inherit lib pkgs;
      inherit (config.home_services.${name}) port;
      inherit (config) machine_name host_addr host_domain media_location appdata_location;
    };
  in lib.mkMerge [
    {
      appdata_location = lib.mkDefault "${config.media_location}/.Apps";

      # Define a this user account. Don't forget to set a password with ‘passwd’.
      services.getty.autologinUser = config.machine_name;
      users.users.${config.machine_name} = {
        isNormalUser = true;
        description = "My Portable Home Server";
        extraGroups = [ "wheel" ];
        packages = with pkgs; [
          git

          (yazi.override {
            extraPackages = [ mediainfo udisks ];
            plugins = { inherit (yaziPlugins) mount mediainfo; };
            settings = {
              keymap.mgr.prepend_keymap = [
                {on = "M"; run = "plugin mount"; desc = "Mount Manager"; }
              ];
              yazi.mgr.show_hidden = true;
            };
          })
        ];
      };

      # Turn off networkmanager
      networking.networkmanager.enable = false;

      # Keep host_addr always local so Tailscale --advertise-routes=…/32 works on Off (kind of like loopback interface)
      # (WAP also assigns the same addr /24 on the wifi iface when active).
      boot.kernelModules = [ "dummy" ];
      networking.interfaces.dummy0 = {
        virtual = true;
        ipv4.addresses = [{ address = config.host_addr; prefixLength = 32; }];
      };

      # Configure DNS for the local network
      services.dnsmasq = {
        enable = true;
        settings = {
          # DNS for LAN + Tailscale (via advertised host_addr/32); WAP DHCP is dashboard child.
          # Must listen on lo — /etc/resolv.conf points at 127.0.0.1.
          bind-dynamic = true;
          # dhcp-option = [ "option:dns-server,${config.host_addr}" ]; # set by system-dashboard.py
          local = "/${config.host_domain}/";
          domain = config.host_domain;
          cache-size = 1000;
          # use the home_services option to add addresses to the DNS
          # home_services.<name>.port = <port>; -> <name>.<host_domain> = <host_addr>:<port>
          address = flatten (mapAttrsToList (name: service: [
            "/${name}.${config.host_domain}/${config.host_addr}"
            "/${name}/${config.host_addr}"
          ]) config.home_services);
        };
      };

      # Configure Reverse Proxy for the local network
      services.caddy = {
        enable = true;
        virtualHosts = {
          "http://dashboard.${config.host_domain}, http://dashboard" = {
            extraConfig = ''
              handle_path /system* {
                reverse_proxy localhost:8080
              }
              handle {
                reverse_proxy localhost:${toString config.home_services.dashboard.port}
              }
            '';
          };
        } // mapAttrs' (name: service:
          nameValuePair "https://${name}.${config.host_domain}, https://${name}" {
            extraConfig = ''
              tls internal
              reverse_proxy localhost:${toString service.port}
            '';
          }) (builtins.removeAttrs config.home_services [ "dashboard" ]);
      };

      networking.firewall = {
        # DNS, DHCP, HTTP(S)
        allowedUDPPorts = [ 53 67 68 ];
        allowedTCPPorts = [ 53 80 443 ];
      };
    }

    # SERVICES
    (forService "dashboard")
    (forService "media")
    (forService "password")
    (forService "photos")
    (forService "torrent")
    (forService "files")
    (forService "git")
  ] # ++ (map forService (builtins.attrNames config.home_services)) # infinite recursion due to config.home_services
  ;
}
