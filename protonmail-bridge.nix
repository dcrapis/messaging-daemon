# Initialization instructions (one-time, per user)
#
# 1. Enable and start the service for your user
# systemctl --user enable protonmail-bridge
# systemctl --user start protonmail-bridge
#
# 2. Log in to Protonmail
# protonmail-bridge --cli
# >>> login   # repeat for each account
# >>> info 0  # copy the bridge password for use with the email daemon
# >>> exit

{ config, lib, pkgs, ... }:

{
  systemd.user.services.protonmail-bridge = {
    description = "Protonmail Bridge";
    after = [ "network.target" ];
    wantedBy = [ "default.target" ];

    serviceConfig = {
      ExecStart = "${pkgs.protonmail-bridge}/bin/protonmail-bridge --noninteractive";
      Restart = "always";
      RestartSec = "10s";
    };
  };
}
