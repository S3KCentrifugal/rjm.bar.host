# Autopilot: runs an rjm-host SPADS room without a human operator.
#
#   * Bot bonus      keeps every autohost bot at AUTOPILOT_BOT_BONUS (%) income
#                    bonus (SPADS's localBots setting has no bonus field).
#   * Auto-start     SPADS's own autoStart never starts a room with bots in it;
#                    this starts the game once enough humans are present and
#                    ready, after a countdown announced in the room.
#   * Pre-game       with choose-in-game start positions, a player who never
#                    places stalls the game; after AUTOPILOT_PREGAME_FORCE
#                    seconds the game is force-started (BAR places them).
#   * Control socket a Unix socket (AUTOPILOT_SOCKET) taking one command per
#                    connection -- `status`, `spads <command>`, `say <text>` --
#                    and answering with one JSON line. `spadsctl` is its client;
#                    automation reaches it with `kubectl exec`. It is a file in
#                    the container, not a port: with hostNetwork a loopback port
#                    would be reachable from the whole node.
#
# Configuration is read from the environment (k8s/base/spads.env):
#   AUTOPILOT_BOT_BONUS       0-100, default 0
#   AUTOPILOT_MIN_HUMANS      humans needed before auto-start, default 1; 0 disables auto-start
#   AUTOPILOT_START_DELAY     countdown in seconds once conditions hold, default 20
#   AUTOPILOT_REQUIRE_READY   1 (default): every human player must be ready and synced
#   AUTOPILOT_PREGAME_FORCE   seconds before forcing a stalled pre-game, default 120; 0 disables
#   AUTOPILOT_DEFAULT_SPLIT   start boxes for maps BAR's mapBoxes.conf does not
#                             know, as !split arguments, default "h 25"; empty disables
#   AUTOPILOT_SOCKET          default /tmp/autopilot.sock
#   AUTOPILOT_GUARD           1 (default): refuse and cancel takeover votes, remove untrusted bosses
#   AUTOPILOT_DENY_VOTES      commands that may never be voted, default "boss,hset,rehost,quit,preset,hpreset,bpreset"
#   AUTOPILOT_VOTABLE_SETTINGS  !set settings players may vote on, default "map"
#   AUTOPILOT_PROTECTED_USERS comma-separated names that votes may not kick, ban or spec
#                             (the owner and anyone at AUTOPILOT_TRUSTED_LEVEL always are)
#   AUTOPILOT_TRUSTED_LEVEL   SPADS access level trusted with boss mode, default 130 (the owner is 140)
#   AUTOPILOT_ABUSE_KICK      refused takeover votes within 10 minutes before a kick, default 3; 0 disables
package Autopilot;

use strict;
use warnings;

use IO::Socket::UNIX;
use JSON::PP;
use SpadsPluginApi;

my $pluginVersion = '1.1';
my $requiredSpadsVersion = '0.13.35';

sub getVersion { return $pluginVersion }
sub getRequiredSpadsVersion { return $requiredSpadsVersion }

sub envInt {
  my ($name, $default) = @_;
  my $v = $ENV{$name};
  return $default unless defined $v && $v =~ /^\d+$/;
  return $v + 0;
}

sub new {
  my $class = shift;
  my $self = {
    botBonus      => envInt('AUTOPILOT_BOT_BONUS', 0),
    minHumans     => envInt('AUTOPILOT_MIN_HUMANS', 1),
    startDelay    => envInt('AUTOPILOT_START_DELAY', 20),
    requireReady  => envInt('AUTOPILOT_REQUIRE_READY', 1),
    pregameForce  => envInt('AUTOPILOT_PREGAME_FORCE', 120),
    defaultSplit  => $ENV{AUTOPILOT_DEFAULT_SPLIT} // 'h 25',
    socketPath    => $ENV{AUTOPILOT_SOCKET} || '/tmp/autopilot.sock',
    countdownFrom => 0,     # time the start conditions first held; 0 = not counting
    lastStartTry  => 0,
    pregameSince  => 0,     # time the engine entered pre-game; 0 = not in pre-game
    pregameForced => 0,
    waitingMsg    => '',
    clients       => {},    # fileno -> { sock, buf }
    guard         => envInt('AUTOPILOT_GUARD', 1),
    denyVotes     => { map { lc($_) => 1 } grep { $_ ne '' } split(/[\s,;]+/, $ENV{AUTOPILOT_DENY_VOTES}
                        // 'boss,hset,rehost,quit,preset,hpreset,bpreset') },
    votableSettings => { map { lc($_) => 1 } grep { $_ ne '' } split(/[\s,;]+/, $ENV{AUTOPILOT_VOTABLE_SETTINGS} // 'map') },
    protectedUsers  => { map { lc($_) => 1 } grep { $_ ne '' } split(/[\s,;]+/,
                          join(',', $ENV{AUTOPILOT_PROTECTED_USERS} // '', $ENV{SPADS_OWNER_LOBBY_LOGIN} // '')) },
    trustedLevel  => envInt('AUTOPILOT_TRUSTED_LEVEL', 130),
    abuseKick     => envInt('AUTOPILOT_ABUSE_KICK', 3),
    attempts      => [],
    strikes       => {},
  };
  $self->{botBonus} = 100 if $self->{botBonus} > 100;
  bless($self, $class);

  $self->openSocket();
  addTimer('tick', 2, 2, sub { $self->tick() });
  slog("Autopilot $pluginVersion: guard=$self->{guard} bonus=$self->{botBonus}% minHumans=$self->{minHumans} "
       . "startDelay=$self->{startDelay}s requireReady=$self->{requireReady} "
       . "pregameForce=$self->{pregameForce}s socket=$self->{socketPath}", 3);
  return $self;
}

sub onUnload {
  my $self = shift;
  removeTimer('tick');
  foreach my $c (values %{$self->{clients}}) {
    removeSocket($c->{sock});
    close($c->{sock});
  }
  if ($self->{listener}) {
    removeSocket($self->{listener});
    close($self->{listener});
    unlink($self->{socketPath});
  }
  slog('Autopilot unloaded', 3);
}

# --------------------------------------------------------------------------
# Room state
# --------------------------------------------------------------------------

sub myLogin { return getSpadsConf()->{lobbyLogin} }

sub battle {
  my $lobby = getLobbyInterface();
  return undef unless getLobbyState() >= LOBBY_STATE_BATTLE_OPENED;
  return undef unless $lobby->{battle} && %{$lobby->{battle}};
  return $lobby->{battle};
}

# Human players: battle members other than the autohost, in player mode.
sub humans {
  my $b = shift;
  my $me = myLogin();
  my @players;
  foreach my $u (keys %{$b->{users}}) {
    next if $u eq $me;
    my $s = $b->{users}{$u}{battleStatus};
    next unless $s && $s->{mode};
    push @players, { name => $u, ready => $s->{ready} ? 1 : 0, sync => ($s->{sync} // 0) == 1 ? 1 : 0 };
  }
  return @players;
}

sub localBots {
  my $b = shift;
  my $me = myLogin();
  return grep { ($b->{bots}{$_}{owner} // '') eq $me } keys %{$b->{bots}};
}

# --------------------------------------------------------------------------
# Periodic work
# --------------------------------------------------------------------------

sub tick {
  my $self = shift;
  my $b = battle();
  my $springState = getSpringInterface()->getState();   # 0 none, 1 pre-game, 2 running, 3 over

  if (!$b) {
    $self->{countdownFrom} = 0;
    return;
  }

  $self->enforceBosses();
  $self->enforceBotBonus($b) if getSpringPid() == 0;

  if ($springState == 1) {
    $self->pregame();
  } else {
    $self->{pregameSince} = 0;
    $self->{pregameForced} = 0;
  }

  $self->autoStart($b) if getSpringPid() == 0;
}

sub enforceBotBonus {
  my ($self, $b) = @_;
  foreach my $bot (sort(localBots($b))) {
    my $s = $b->{bots}{$bot}{battleStatus} or next;
    next if ($s->{bonus} // 0) == $self->{botBonus};
    $self->runSpadsCommand(['force', "%$bot", 'bonus', $self->{botBonus}]);
  }
}

sub autoStart {
  my ($self, $b) = @_;
  return if $self->{minHumans} == 0;

  my @humans = humans($b);
  my $wantBots = getSpadsConf()->{autoAddBotNb} // 0;
  my @bots = localBots($b);
  my $why = '';
  if (@humans < $self->{minHumans}) {
    $why = 'waiting for ' . ($self->{minHumans} - @humans) . ' more player(s)';
  } elsif (@bots < $wantBots) {
    $why = 'waiting for bots';
  } elsif ($self->{requireReady}) {
    my @notReady = map { $_->{name} } grep { !$_->{ready} || !$_->{sync} } @humans;
    $why = 'waiting for ' . join(', ', @notReady) . ' to be ready' if @notReady;
  }

  if ($why) {
    if ($self->{countdownFrom}) {
      sayBattle("Autopilot: start cancelled, $why.");
    }
    $self->{countdownFrom} = 0;
    $self->{waitingMsg} = $why;
    return;
  }
  $self->{waitingMsg} = '';

  my $now = time;
  if (!$self->{countdownFrom}) {
    $self->{countdownFrom} = $now;
    sayBattle("Autopilot: everyone is ready, starting in $self->{startDelay} seconds.") if $self->{startDelay} > 0;
  }
  return if $now - $self->{countdownFrom} < $self->{startDelay};
  return if $now - $self->{lastStartTry} < 15;   # a refused start is retried, not hammered

  $self->{lastStartTry} = $now;
  # Choose-in-game start positions need start boxes. BAR's mapBoxes.conf has
  # them for BAR's maps; for any other map, split it.
  if (!%{$b->{startRects} // {}} && $self->{defaultSplit}) {
    $self->runSpadsCommand(['split', split(/\s+/, $self->{defaultSplit})]);
  }
  my $r = $self->runSpadsCommand(['start']);
  slog('Autopilot: start ' . ($r->{ok} ? 'issued' : 'refused: ' . join(' / ', @{$r->{answers}})), 3);
  $self->{countdownFrom} = 0 if $r->{ok};
}

sub pregame {
  my $self = shift;
  my $now = time;
  $self->{pregameSince} ||= $now;
  return if $self->{pregameForce} == 0 || $self->{pregameForced};
  return if $now - $self->{pregameSince} < $self->{pregameForce};
  $self->{pregameForced} = 1;
  sayBattle("Autopilot: pre-game has lasted $self->{pregameForce}s, forcing the start.");
  $self->runSpadsCommand(['forceStart']);
}

# --------------------------------------------------------------------------
# Lobby guard: votes and bosses
# --------------------------------------------------------------------------
#
# A public room is taken over by vote: make yourself boss (a boss strips every
# other non-privileged user's command rights), kick or spec the owner, change
# the room password, strip the bots. commands.conf already makes !boss
# owner-only; this is the rest, in three layers:
#
#   preSpadsCommand  refuses a takeover command however it arrives (direct,
#                    vote, or a vote SPADS auto-passes because nobody else can
#                    vote) from anyone below the trusted level
#   onVoteRequest    refuses a takeover vote before it starts
#   onVoteStart      cancels (!endVote) any that starts anyway
#   tick             removes a boss below AUTOPILOT_TRUSTED_LEVEL
#
# Repeat offenders are kicked. Every attempt is logged and kept in `status`.

# SPADS's own command aliases (spads.pl %SPADS_CORE_CMD_ALIASES), so an alias
# cannot slip a vote past the guard.
my %ALIAS = (map => ['set', 'map'], spec => ['force', '%1%', 'spec'], fb => ['force', '*'],
             cv => ['callvote'], ev => ['endvote']);

# Commands whose target is a user: refused when that user is protected.
my %TARGETS_USER = map { $_ => 1 } qw(kick kickban bkick gkick ban banip force ring);

sub normalizeCommand {
  my @cmd = @{$_[0]};
  @cmd = @{$cmd[0]} if @cmd == 1 && ref $cmd[0] eq 'ARRAY';
  my $name = lc($cmd[0] // '');
  if (my $a = $ALIAS{$name}) {
    my @expanded = map { $_ eq '%1%' ? ($cmd[1] // '') : $_ } @{$a};
    my $consumed = (grep { $_ eq '%1%' } @{$a}) ? 2 : 1;   # alias word, plus the %1% argument
    @cmd = (@expanded, @cmd[$consumed .. $#cmd]);
    $name = lc($cmd[0]);
  }
  return ($name, @cmd[1 .. $#cmd]);
}

sub isProtectedUser {
  my ($self, $name) = @_;
  return 0 unless defined $name && $name ne '';
  $name =~ s/^%//;
  my $lc = lc($name);
  return 1 if $lc eq lc(myLogin());
  return 1 if exists $self->{protectedUsers}{$lc};
  return getUserAccessLevel($name) >= $self->{trustedLevel} ? 1 : 0;
}

# Returns a reason string if this vote must not happen, '' if it may.
sub voteThreat {
  my ($self, $r_cmd) = @_;
  my ($name, @args) = normalizeCommand($r_cmd);
  return "!$name votes are not allowed here" if $self->{denyVotes}{$name};
  if ($TARGETS_USER{$name}) {
    foreach my $a (@args) {
      return "$a is protected" if $self->isProtectedUser($a);
    }
  }
  if ($name eq 'set' && @args) {
    my $setting = lc($args[0]);
    return "voting on \"$setting\" is not allowed here" unless $self->{votableSettings}{$setting};
  }
  return '';
}

sub recordAttempt {
  my ($self, $user, $r_cmd, $why, $action) = @_;
  my ($name, @args) = normalizeCommand($r_cmd);
  my $entry = { time => time, user => $user, command => join(' ', $name, @args), reason => $why, action => $action };
  push @{$self->{attempts}}, $entry;
  shift @{$self->{attempts}} while @{$self->{attempts}} > 50;
  slog("Autopilot guard: $action vote \"$entry->{command}\" by $user ($why)", 2);
  return if lc($user) eq lc(myLogin());

  my $key = lc($user);
  my $now = time;
  my @recent = grep { $now - $_ < 600 } (@{$self->{strikes}{$key} // []}, $now);
  $self->{strikes}{$key} = \@recent;
  if ($self->{abuseKick} && @recent >= $self->{abuseKick} && !$self->isProtectedUser($user)) {
    sayBattle("Autopilot: kicking $user after " . scalar(@recent) . " refused takeover votes.");
    $self->runSpadsCommand(['kick', $user]);
    delete $self->{strikes}{$key};
  }
}

# The layer that cannot be bypassed. SPADS executes a "vote" immediately,
# without onVoteRequest or onVoteStart, when nobody else is eligible to vote
# (spads.pl hCallVote: `return executeCommand(...) unless(%remainingVoters)`),
# so a lone troll could spec the owner that way. preSpadsCommand runs before
# every command however it arrives -- direct, callvote, auto-passed or passed
# vote -- so the same policy is applied here to everyone below the trusted
# level. The owner, trusted users and the autohost itself are unaffected.
sub preSpadsCommand {
  my ($self, $command, $source, $user, $r_params) = @_;
  return 1 unless $self->{guard};
  return 1 if lc($user // '') eq lc(myLogin()) || $self->isProtectedUser($user);
  my @cmd = lc($command) eq 'callvote' ? @{$r_params} : ($command, @{$r_params});
  @cmd = @{$cmd[0]} if @cmd == 1 && ref $cmd[0] eq 'ARRAY';
  return 1 unless @cmd;
  my $why = $self->voteThreat([\@cmd]);
  return 1 if $why eq '';
  sayBattle("Autopilot: refused, $why.");
  $self->recordAttempt($user, [\@cmd], $why, 'refused');
  return 0;
}

sub onVoteRequest {
  my ($self, $source, $user, $r_cmd, $r_remainingVoters) = @_;
  return 1 unless $self->{guard};
  my $why = $self->voteThreat($r_cmd);
  return 1 if $why eq '';
  sayBattle("Autopilot: vote refused, $why.");
  $self->recordAttempt($user, $r_cmd, $why, 'refused');
  return 0;
}

# Defence in depth: if a takeover vote starts anyway (another plugin allowed
# it, or a command this guard did not anticipate), end it. It cannot be done
# inside the callback -- the vote is still being set up -- so a zero-delay
# timer runs !endVote right after.
sub onVoteStart {
  my ($self, $user, $r_cmd) = @_;
  return unless $self->{guard};
  my $why = $self->voteThreat($r_cmd);
  return if $why eq '';
  $self->recordAttempt($user, $r_cmd, $why, 'cancelled');
  addTimer('cancelVote', 0, 0, sub {
    removeTimer('cancelVote');
    sayBattle("Autopilot: cancelling the vote, $why.");
    $self->runSpadsCommand(['endVote']);
  });
}

# Boss mode set by anyone below the trusted level is switched off.
sub enforceBosses {
  my $self = shift;
  return unless $self->{guard};
  my @untrusted = grep { !$self->isProtectedUser($_) } keys %::bosses;
  return unless @untrusted;
  sayBattle('Autopilot: boss mode is reserved for the room owner; disabling it (' . join(', ', sort @untrusted) . ').');
  push @{$self->{attempts}}, { time => time, user => join(',', sort @untrusted), command => 'boss',
                               reason => 'untrusted boss', action => 'removed' };
  $self->runSpadsCommand(['boss']);
}

# --------------------------------------------------------------------------
# Start boxes
# --------------------------------------------------------------------------

# SPADS looks map boxes up by its nbTeams setting, which is 1 in humans-vs-bots
# mode, while BAR's mapBoxes.conf defines them for 2 (or more) teams. So in
# that mode supply the map's 2-team boxes, or boxes from AUTOPILOT_DEFAULT_SPLIT
# for a map BAR's file does not know.
sub setMapStartBoxes {
  my ($self, $r_boxes, $mapName, $nbTeams, $nbExtraBox) = @_;
  return 0 if @{$r_boxes} || $nbTeams != 1;
  my $smf = $mapName =~ /\.smf$/ ? $mapName : "$mapName.smf";
  my $r_twoTeams = getSpadsConfFull()->getMapBoxes($smf, 2, $nbExtraBox // 0);
  if ($r_twoTeams && @{$r_twoTeams}) {
    slog("Autopilot: using the 2-team start boxes of $mapName", 4);
    return [1, $r_twoTeams];
  }
  my @split = $self->splitBoxes();
  return 0 unless @split;
  slog("Autopilot: no known start boxes for $mapName, using split \"$self->{defaultSplit}\"", 3);
  return [1, \@split];
}

# "h N": top and bottom bands N% high; "v N": left and right bands N% wide.
# Coordinates are SPADS's 0..200 box space.
sub splitBoxes {
  my $self = shift;
  my ($type, $pct) = split(/\s+/, $self->{defaultSplit} // '');
  return () unless defined $pct && $pct =~ /^\d+$/ && $pct > 0 && $pct <= 50;
  my $n = 2 * $pct;
  my $far = 200 - $n;
  return ("0 0 200 $n", "0 $far 200 200") if $type eq 'h';
  return ("0 0 $n 200", "$far 0 200 200") if $type eq 'v';
  return ();
}

# --------------------------------------------------------------------------
# Running SPADS commands as the autohost
# --------------------------------------------------------------------------

# Runs a SPADS command with the autohost's authority (no vote, no access-level
# check) and returns what it said. SPADS answers a battle-sourced command
# through sayBattle, resolved at call time, so a dynamic override of that one
# function captures the answers while still showing them in the room.
sub runSpadsCommand {
  my ($self, $cmd) = @_;
  my @answers;
  my $realSayBattle = \&main::sayBattle;
  my $ok;
  {
    no warnings 'redefine';
    local *main::sayBattle = sub { push @answers, $_[0]; $realSayBattle->(@_) };
    $ok = eval { ::executeCommand('battle', myLogin(), $cmd) };
    push @answers, "error: $@" if $@;
  }
  return { ok => ($ok ? JSON::PP::true : JSON::PP::false), answers => \@answers };
}

# --------------------------------------------------------------------------
# Control socket
# --------------------------------------------------------------------------

sub openSocket {
  my $self = shift;
  my $path = $self->{socketPath};
  unlink($path);
  my $old = umask(0077);   # owner-only: the container user, via kubectl exec
  my $listener = IO::Socket::UNIX->new(Type => SOCK_STREAM(), Local => $path, Listen => 5);
  umask($old);
  if (!$listener) {
    slog("Autopilot: cannot open control socket $path: $!", 1);
    return;
  }
  $listener->blocking(0);
  $self->{listener} = $listener;
  addSocket($listener, sub { $self->acceptClient() });
}

sub acceptClient {
  my $self = shift;
  my $client = $self->{listener}->accept() or return;
  $client->blocking(0);
  $self->{clients}{fileno($client)} = { sock => $client, buf => '' };
  addSocket($client, sub { $self->readClient($client) });
}

sub closeClient {
  my ($self, $client) = @_;
  removeSocket($client);
  delete $self->{clients}{fileno($client)};
  close($client);
}

sub readClient {
  my ($self, $client) = @_;
  my $c = $self->{clients}{fileno($client)} or return;
  my $n = sysread($client, my $chunk, 4096);
  if (!$n) {   # EOF or error without a full line
    $self->closeClient($client);
    return;
  }
  $c->{buf} .= $chunk;
  if (length($c->{buf}) > 8192) {
    $self->closeClient($client);
    return;
  }
  return unless $c->{buf} =~ /\n/;
  my ($line) = split(/\n/, $c->{buf}, 2);
  $line =~ s/\r$//;
  my $reply = eval { $self->handleControl($line) } // { ok => JSON::PP::false, error => "$@" };
  $client->blocking(1);
  print {$client} JSON::PP->new->canonical->encode($reply) . "\n";
  $self->closeClient($client);
}

sub handleControl {
  my ($self, $line) = @_;
  my ($verb, $rest) = split(/\s+/, $line, 2);
  $verb = lc($verb // '');
  $rest //= '';

  if ($verb eq 'status') {
    return $self->status();
  }
  if ($verb eq 'spads') {
    return { ok => JSON::PP::false, error => 'usage: spads <command>' } if $rest eq '';
    $rest =~ s/^!//;
    my ($lcCmd, @cmd) = ::parseSpadsCmd($rest);
    return $self->runSpadsCommand(\@cmd);
  }
  if ($verb eq 'say') {
    sayBattle($rest);
    return { ok => JSON::PP::true };
  }
  return { ok => JSON::PP::false, error => "unknown command '$verb' (status | spads <command> | say <text>)" };
}

sub status {
  my $self = shift;
  my $lobby = getLobbyInterface();
  my $b = battle();
  my %s = (
    ok           => JSON::PP::true,
    lobbyState   => getLobbyState(),
    battleOpen   => $b ? JSON::PP::true : JSON::PP::false,
    springPid    => getSpringPid(),
    springState  => getSpringInterface()->getState(),
    autopilot    => {
      botBonus => $self->{botBonus}, minHumans => $self->{minHumans}, startDelay => $self->{startDelay},
      requireReady => $self->{requireReady}, pregameForce => $self->{pregameForce},
      countdownRemaining => $self->{countdownFrom} ? $self->{startDelay} - (time - $self->{countdownFrom}) : undef,
      waiting => $self->{waitingMsg},
    },
    guard => {
      enabled => $self->{guard} ? JSON::PP::true : JSON::PP::false,
      denyVotes => [sort keys %{$self->{denyVotes}}],
      votableSettings => [sort keys %{$self->{votableSettings}}],
      protectedUsers => [sort keys %{$self->{protectedUsers}}],
      trustedLevel => $self->{trustedLevel},
      bosses => [sort keys %::bosses],
      recentAttempts => [ @{$self->{attempts}}[-(@{$self->{attempts}} < 20 ? @{$self->{attempts}} : 20) .. -1] ],
    },
  );
  if ($b) {
    my $info = $lobby->{battles}{$b->{battleId}} // {};
    $s{battle} = {
      id => $b->{battleId}, title => $info->{title}, map => $info->{map}, game => $info->{mod},
      maxPlayers => $info->{maxPlayers}, passworded => $info->{passworded},
    };
    $s{users} = [ map {
      my $st = $b->{users}{$_}{battleStatus} // {};
      { name => $_, player => $st->{mode} ? 1 : 0, ready => $st->{ready} ? 1 : 0,
        sync => $st->{sync} // 0, team => $st->{team}, id => $st->{id} }
    } sort keys %{$b->{users}} ];
    $s{bots} = [ map {
      my $bt = $b->{bots}{$_};
      my $st = $bt->{battleStatus} // {};
      { name => $_, ai => $bt->{aiDll}, owner => $bt->{owner}, team => $st->{team}, id => $st->{id}, bonus => $st->{bonus} }
    } sort keys %{$b->{bots}} ];
  }
  return \%s;
}

1;
