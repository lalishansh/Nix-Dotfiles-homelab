# Always-on LAN DNS (dnsmasq) + Caddy reverse proxy for *.lan names.
# Wireless radio/DHCP for WAP stays in dashboard.py (DHCP-only dnsmasq child).
#
# Stable VIP = host_addr on dummy0 + Tailscale --advertise-routes=<host_addr>/32.
# No Tailscale IP in Nix — reinstall on new hardware keeps the same names if you
# reuse this config and re-approve the subnet route once.
#
# Tailscale admin (one-time per tailnet, not per device IP):
#   - Approve advertised ${host_addr}/32
#   - Split DNS: domain "${host_domain}" → nameserver ${host_addr}
# Clients need --accept-routes so ${host_addr} is reached via Tailscale.
#
# Wireless UI is at http://dashboard.lan/wireless (same origin as Glance) so the
# iframe can sync theme CSS vars from the parent page.

{ config, lib, pkgs, ... }:
let
  inherit (lib) mapAttrsToList flatten mapAttrs' nameValuePair;

  host_addr = config.host_addr;
  host_domain = config.host_domain;

  # url → http://<url>.lan via Caddy (wireless is a path on dashboard, not its own host)
  home-services = {
    glance = {
      port = 5678;
      url = "dashboard";
    };
    # immich = { port = 2283; url = "photos"; };
    # vaultwarden = { port = 8222; url = "passwords"; };
    # jellyfin = { port = 8096; url = "media"; };
    # qbittorrent = { port = 8097; url = "torrents"; };
    # filebrowser = { port = 8081; url = "files"; };
  };
in
{
  # Keep host_addr always local so Tailscale --advertise-routes=…/32 works on Off
  # (WAP also assigns the same addr /24 on the wifi iface when active).
  boot.kernelModules = [ "dummy" ];
  networking.interfaces.dummy0 = {
    virtual = true;
    ipv4.addresses = [{ address = host_addr; prefixLength = 32; }];
  };

  networking.firewall = {
    allowedUDPPorts = [ 53 67 68 ];
    allowedTCPPorts = [ 53 80 443 ];
    trustedInterfaces = [ config.services.tailscale.interfaceName ];
  };

  services.dnsmasq = {
    enable = true;
    settings = {
      # DNS for LAN + Tailscale (via advertised host_addr/32); WAP DHCP is dashboard child.
      # Must listen on lo — /etc/resolv.conf points at 127.0.0.1.
      bind-dynamic = true;
      local = "/${host_domain}/";
      domain = host_domain;
      cache-size = 1000;
      dhcp-option = [
        "option:dns-server,${host_addr}"
      ];
      address = flatten (mapAttrsToList (_: service: [
        "/${service.url}.${host_domain}/${host_addr}"
        "/${service.url}/${host_addr}"
      ]) home-services);
    };
  };

  services.caddy = {
    enable = true;
    # Plain HTTP — avoids 308→https + untrusted tls internal on .lan / Tailscale.
    virtualHosts = {
      "http://dashboard.${host_domain}, http://dashboard" = {
        extraConfig = ''
          handle_path /wireless* {
            reverse_proxy localhost:8080
          }
          handle {
            reverse_proxy localhost:5678
          }
        '';
      };
    } // mapAttrs' (_name: service:
      nameValuePair "http://${service.url}.${host_domain}, http://${service.url}" {
        extraConfig = ''
          reverse_proxy localhost:${toString service.port}
        '';
      }
    ) (builtins.removeAttrs home-services [ "glance" ]);
  };
}
