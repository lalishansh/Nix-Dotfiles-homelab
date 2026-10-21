{ config, lib, pkgs, ... }:
{
  assertions = [{ assertion = false; message = "We are in eth"; }];
}