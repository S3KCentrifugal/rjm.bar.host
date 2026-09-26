#!/usr/bin/perl
# Loads the Autopilot plugin with a stub SPADS plugin API and prints its
# guard's decision for each line on stdin, as "<line>\t<decision>":
#   <command words>              the vote policy (voteThreat): reason or 'allowed'
#   exec <user> <command words>  the command gate (preSpadsCommand) as run by
#                                <user>: 'allowed' or 'refused'
#
# Stub world: the autohost is AutoHost, the owner Owner (level 140), a lobby
# moderator Mod (level 110), everyone else level 0. AUTOPILOT_* settings come
# from the environment, as in SPADS.
use strict;
use warnings;

BEGIN {
  package SpadsPluginApi;
  $INC{'SpadsPluginApi.pm'} = __FILE__;
  my @api = qw(slog getUserAccessLevel getSpadsConf getSpadsConfFull addTimer removeTimer addSocket
               removeSocket sayBattle getLobbyState getLobbyInterface getSpringInterface getSpringPid
               getRunningBattle);
  sub import {
    no strict 'refs';
    my $caller = caller;
    *{"${caller}::$_"} = \&{"SpadsPluginApi::$_"} for @api;
    *{"${caller}::LOBBY_STATE_BATTLE_OPENED"} = sub { 6 };
  }
  sub slog {}
  sub getUserAccessLevel { my $u = lc(shift); $u eq 'owner' ? 140 : $u eq 'mod' ? 110 : 0 }
  sub getSpadsConf { { lobbyLogin => 'AutoHost' } }
  sub getSpadsConfFull {}
  sub addTimer {} sub removeTimer {} sub addSocket {} sub removeSocket {}
  sub sayBattle {} sub getLobbyState { 0 } sub getLobbyInterface {} sub getSpringInterface {}
  sub getSpringPid { 0 } sub getRunningBattle {}
}

package main;
our %bosses;
sub executeCommand { 1 }
$ENV{SPADS_OWNER_LOBBY_LOGIN} //= 'Owner';
$ENV{AUTOPILOT_SOCKET} = "/tmp/autopilot-harness-$$.sock";
require($ENV{AUTOPILOT_PM} // '/opt/rjm-host/spads/plugins/Autopilot.pm');
my $p = Autopilot->new;

while (my $line = <STDIN>) {
  chomp $line;
  next if $line eq '';
  my @cmd = split(/ /, $line);
  if ($cmd[0] eq 'exec') {
    my (undef, $user, $name, @args) = @cmd;
    my $ok = $p->preSpadsCommand($name, 'battle', $user, \@args);
    print "$line\t", ($ok ? 'allowed' : 'refused'), "\n";
    next;
  }
  my $why = $p->voteThreat([\@cmd]);
  print "$line\t", ($why eq '' ? 'allowed' : $why), "\n";
}
unlink $ENV{AUTOPILOT_SOCKET};
