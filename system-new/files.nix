{ lib, pkgs, port, appdata_location, media_location, machine_name, ... }:
let
  dataDir = "${appdata_location}/filebrowser-quantum";

  # ── Config ────────────────────────────────────────────────────────
  generatedConfig = (pkgs.formats.yaml { }).generate "filebrowser-quantum-config.yml" {
    http.port = port;
    server = {
      database.path = "${dataDir}/database.sqlite";
      cacheDir = "/var/cache/filebrowser-quantum";
      sources = [
        { path = media_location;
          name = ""; }
      ];
    };
    auth.methods.password = {
      adminUsername = "admin";
      adminPassword = "admin1234"; # will prompt for password change on first login
    };
    frontend.name = "Blood Base";
  };

  # ── Go toolchain ──────────────────────────────────────────────────
  # Quantum 2.x needs Go >= 1.27.0 (final). Your channel only has an rc,
  # so borrow the compiler from unstable. For reproducibility, replace
  # `nixos-unstable` with a commit hash and add a sha256.
  unstable = import (builtins.fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/archive/nixos-unstable.tar.gz";
    # sha256 = "...";
  }) {
    system = pkgs.stdenv.hostPlatform.system;
  };
  go = unstable.go_1_27;

  # ── Package ───────────────────────────────────────────────────────
  version = "2.1.4-beta";

  src = pkgs.fetchFromGitHub {
    owner = "gtsteffaniak";
    repo = "filebrowser";
    tag = "v${version}";
    hash = "sha256-1uiNJi6gDdCcVliXpXLnJQ0J2LoPwAkW0O//1NE2+OQ=";
  };

  frontend = pkgs.buildNpmPackage {
    pname = "filebrowser-quantum-frontend";
    inherit version src;
    sourceRoot = "${src.name}/frontend";
    npmDepsHash = "sha256-rT6PEMsJ/h0htYJuLQttJwXESTTRRbOurh/B8WR9iJQ=";

    # plyr's install script calls `npx only-allow pnpm`, which needs network
    npmRebuildFlags = [ "--ignore-scripts" ];

    # vite writes to ../backend/internal/web/dist, so make the tree writable
    postPatch = ''
      chmod -R u+w ..
    '';

    buildPhase = ''
      runHook preBuild
      npm run build:docker
      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall
      mkdir -p $out
      cp -r ../backend/internal/web/dist/* $out
      runHook postInstall
    '';
  };

  package = (pkgs.buildGoModule.override { inherit go; }) {
    pname = "filebrowser-quantum";
    inherit version src;
    sourceRoot = "${src.name}/backend";
    vendorHash = "sha256-dkErhrXDswQmK7KegbBd4ScCNw0XgVSLuCD7pr1N+P4=";
    doCheck = false; # since we are self hosting, we don't need to run the tests ;)

    preBuild = ''
      mkdir -p internal/web/embed
      cp -r ${frontend}/* internal/web/embed/
      chmod -R u+w internal/web/embed
    '';

    postInstall = ''
      mv $out/bin/backend $out/bin/filebrowser-quantum
    '';

    ldflags = [ "-w" "-s" ];
    meta.mainProgram = "filebrowser-quantum";
  };
in
{
  users.users.filebrowser = {
    isSystemUser = true;
    group = "filebrowser";
  };
  users.groups.filebrowser = { };

  systemd.tmpfiles.rules = [
    "d ${dataDir} 0750 filebrowser filebrowser - -"
  ];

  systemd.services.filebrowser-quantum = {
    description = "FileBrowser Quantum";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" "local-fs.target" ];
    restartTriggers = [ generatedConfig ];
    path = [ pkgs.ffmpeg ];

    serviceConfig = {
      User = "filebrowser";
      Group = "filebrowser";
      WorkingDirectory = dataDir;
      CacheDirectory = "filebrowser-quantum";   # creates /var/cache/filebrowser-quantum, owned by the service user
      ExecStart = "${lib.getExe package} -c ${generatedConfig}";
      Restart = "on-failure";
      NoNewPrivileges = true;
    };
  };

  users.users.${machine_name}.extraGroups = [ "filebrowser" ];
}