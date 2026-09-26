# The automated lobby host

How the `bar-<env>` lobby runs itself: a public (or passworded) co-op room on
BAR's lobby server, hosted by your second account, where every human plays
against a team of server-side AI bots, with no operator.

Setup — accounts, secrets, router — is in [`setup.md`](setup.md). Design
background is in [`autohost-design.md`](autohost-design.md) §10.

---

## 1. What runs where

| Part | Where | Role |
| --- | --- | --- |
| SPADS | pod `rjm-bar-host-spads`, container `spads` | logs in as the autohost account, owns the room, runs every command and vote |
| Autopilot plugin | inside SPADS | bot bonus, auto-start, pre-game force start, control socket |
| `spring-headless` | same container, one per game | game server + a spectator client that runs **all the bots** |
| `spadsctl` | same container | command-line client for the control socket |
| Chobby + `spring` | each player's PC | the player's lobby client and game |

Your second account is an ordinary lobby account used by SPADS instead of by
Chobby. You cannot be logged in to it in Chobby at the same time.

## 2. The room's lifecycle, with no operator

1. **Pod starts.** Content is fetched, custom AIs installed, SPADS logs in and
   opens the room with the co-op preset.
2. **Bots.** Humans-vs-bots mode (`nbTeams:1`) keeps `SPADS_AUTO_ADD_BOTS`
   bots on the bot team, named from `SPADS_LOCAL_BOTS`. The autohost owns them,
   so they run on the cluster, not on a player's PC. It does not need to be
   on a team to add them.
3. **Bonus.** Autopilot sets every bot to `AUTOPILOT_BOT_BONUS` percent.
4. **Players join** from Chobby. SPADS's flood rules kick spammers
   (`msgFloodAutoKick`), and its access levels decide who may call which
   vote.
5. **Start.** When at least `AUTOPILOT_MIN_HUMANS` players are in the room and
   all are ready and synced, Autopilot announces a countdown
   (`AUTOPILOT_START_DELAY`) and starts. SPADS balances every human onto one
   team and every bot onto the other.
6. **Pre-game.** Players place start positions. If the pre-game stalls
   (someone AFK), Autopilot force-starts `AUTOPILOT_PREGAME_FORCE` seconds
   after the **last player has finished loading**, and BAR places anyone who
   didn't choose. It never forces while a player is still loading: BAR would
   treat their team as abandoned and, with one human, end the game at once.
   A player who never finishes loading is forced past after
   `AUTOPILOT_PREGAME_MAX`.
7. **Game.** The bots run inside the container's engine.
8. **Game over.** SPADS stops the engine 5 s after the end. The game's
   infolog, start script and replay link are archived under
   `/bar/out/spads/games/`. Everyone is still in the room, and bots, map and
   settings are unchanged. Back to step 5.

The room closes only if the SPADS process stops — see `autohost-design.md`
§10 and the deploy guard, which never restarts SPADS during a game.

## 3. Protection against takeovers

In a public room, players can call votes. In SPADS's stock configuration any
player may call a vote to make someone **boss**. A boss strips every other
non-privileged user's command rights, so a boss vote is how a troll takes a
room over. Votes can also kick or spec the owner, change the room password,
or remove the bots.

Four layers stop that:

| Layer | Where | What it does |
| --- | --- | --- |
| 1. Command rights | `docker/spads/etc/commands.conf` | `!boss` needs owner level (130+) and **cannot be voted at all**. A troll's `!boss` or `!cv boss` gets "not allowed" from SPADS itself. |
| 2. Vote guard, before | Autopilot `onVoteRequest` | refuses a vote before it starts: <br>• any vote on `AUTOPILOT_DENY_VOTES` (default `boss, hSet, rehost, quit, preset, hPreset, bPreset`); <br>• a `kick`/`kickBan`/`ban`/`force`/`spec`/`ring` vote naming a **protected user** — the owner, the autohost, `AUTOPILOT_PROTECTED_USERS`, anyone at `AUTOPILOT_TRUSTED_LEVEL`; <br>• a `!set` vote on anything but `AUTOPILOT_VOTABLE_SETTINGS` (default: only `map`, so nobody can vote the bots away) |
| 3. Vote guard, after | Autopilot `onVoteStart` + tick | any such vote that starts anyway is **cancelled** with `!endVote` immediately. **Boss mode** held by anyone below the trusted level is switched off within seconds. |
| 4. Command gate | Autopilot `preSpadsCommand` | applies the same policy to **every command** run by anyone below the trusted level, however it arrives. This matters because SPADS does not hold a vote when nobody else is eligible to vote: it executes the command at once, and neither vote hook runs (`spads.pl` `hCallVote`: `return executeCommand(…) unless(%remainingVoters)`). A lone troll in a nearly empty room could otherwise `!cv spec <owner>` and have it happen. The e2e test found this. |

A user whose takeover votes are refused `AUTOPILOT_ABUSE_KICK` times (default
3) within 10 minutes is **kicked**. Every attempt is written to SPADS's log
(`Autopilot guard: refused vote …`) and listed by `spadsctl status` under
`guard.recentAttempts`, with user, command, reason and action.

Ordinary votes still work: map, stop, mod options (`bSet`), start, and kicking
players who are not protected.

**Letting a friend be boss.** Boss mode is reserved for trusted users. Add
their name to `AUTOPILOT_PROTECTED_USERS`, then the owner can `!boss friend`.

**Manual cancel.** As owner, `!endVote` (alias `!ev`) cancels any vote, by
private message or in the room. With the control socket:
`spadsctl spads endVote`.

## 4. Controlling the room programmatically

The Autopilot plugin listens on a Unix socket inside the container
(`/tmp/autopilot.sock`, owner-only). It is a file, not a port, because the pod
uses the node's network: a loopback port would be reachable from the whole
node. Reach it with `kubectl exec`:

```bash
NS=bar-test
ctl() { kubectl exec -n "$NS" deploy/rjm-bar-host-spads -c spads -- spadsctl "$@"; }

ctl status                                   # room, players, bots, game state (JSON)
ctl spads map Comet Catcher Remake 1.8       # any SPADS command, with the autohost's authority
ctl spads set autoAddBotNb 6                 # resize the bot team
ctl spads bSet coop 0                        # a mod option
ctl spads hSet password secret               # make the room private ("*" = public)
ctl spads forceStart                         # start now / force a stalled pre-game
ctl spads stop                               # end the running game
ctl spads kick SomePlayer
ctl say "Server restarting in 10 minutes"
```

`status` returns:

```json
{
  "ok": true, "battleOpen": true, "springPid": 0, "springState": 0,
  "battle": {"title": "rjm-bar-host test room", "map": "All That Glitters v2.2.3", "maxPlayers": 16, "passworded": 0},
  "users": [{"name": "Human1", "player": 1, "ready": 1, "sync": 1, "team": 0}],
  "bots":  [{"name": "SMRTBARb1", "ai": "SMRTBARb", "owner": "<autohost>", "team": 1, "bonus": 50}],
  "autopilot": {"botBonus": 50, "minHumans": 1, "startDelay": 20, "countdownRemaining": null,
                "waiting": "waiting for 1 more player(s)"}
}
```

`springState` is 0 (no game), 1 (pre-game), 2 (running) or 3 (over).
`spadsctl` exits 1 when a command is refused, 3 when the socket is unreachable.

**Commands run through the socket skip SPADS's votes and access levels.** That
is the point — it is the operator's channel — and it is why the socket is only
reachable by `kubectl exec` into the pod.

The other programmatic route needs nothing extra: your main account (the
owner, level 140) sends the same commands by private message to the autohost,
e.g. `!map …`, `!forceStart`, from Chobby or any lobby client.

## 5. Configuration reference

All in [`k8s/base/spads.env`](../k8s/base/spads.env) (a ConfigMap; prod
overrides ports in its overlay). A change redeploys SPADS — between games
only.

| Setting | Default | Meaning |
| --- | --- | --- |
| `SPADS_BATTLE_NAME` | `rjm-bar-host test room` | room title |
| `SPADS_MAP` | All That Glitters v2.2.3 | map at start; players can vote others from `BARHOST_MAPS` |
| `BARHOST_MAPS` | 2 maps | maps downloaded into the content volume — the only maps the room can use |
| `SPADS_GAME` / `BARHOST_GAMETYPE` | `byar:test` | game version; pin `byar:git:<sha>` for a stable public room |
| `SPADS_ALLOWED_AIS` | `SMRTBARb;BARb` | AIs the autohost may run |
| `SPADS_AUTO_ADD_BOTS` | 8 | bot-team size |
| `SPADS_LOCAL_BOTS` | 8 × SMRTBARb | bot names, sides and AI, in order |
| `SPADS_MAX_HUMANS` | 8 | human-team size |
| `AUTOPILOT_BOT_BONUS` | 50 | bot income bonus, % |
| `AUTOPILOT_MIN_HUMANS` | 1 | players before auto-start; 0 disables auto-start |
| `AUTOPILOT_START_DELAY` | 20 | countdown, seconds |
| `AUTOPILOT_REQUIRE_READY` | 1 | all players must be ready and synced |
| `AUTOPILOT_PREGAME_FORCE` | 120 | force a stalled pre-game this many seconds after the last player loaded; 0 disables |
| `AUTOPILOT_PREGAME_MAX` | 600 | force anyway if a player never finishes loading |
| `SPADS_GAME_PORT` | 8452 (prod 8462) | UDP port players connect to; forward it on the router |
| `AUTOPILOT_GUARD` | 1 | the vote guard (§3) |
| `AUTOPILOT_DENY_VOTES` | boss,hset,rehost,quit,preset,hpreset,bpreset | commands that may never be voted |
| `AUTOPILOT_VOTABLE_SETTINGS` | map | `!set` settings players may vote on |
| `AUTOPILOT_PROTECTED_USERS` | (empty) | extra names votes may not kick, ban or spec, and who may be boss |
| `AUTOPILOT_TRUSTED_LEVEL` | 130 | SPADS level trusted with boss mode (owner 140, lobby moderators 110) |
| `AUTOPILOT_ABUSE_KICK` | 3 | refused takeover votes in 10 minutes before a kick; 0 disables |

Mod options (co-op, Legion, draft order, start positions) are in
[`docker/spads/etc/battlePresets.conf`](../docker/spads/etc/battlePresets.conf);
the room preset (humans-vs-bots, balance, votes, flood limits) in
[`docker/spads/etc/spads.conf`](../docker/spads/etc/spads.conf).

## 6. Logs

| What | Where (`bar-<env>` output volume) |
| --- | --- |
| SPADS log | `spads/log/spads.log` |
| room and game chat | `spads/log/chat/` |
| engine console of every game | `spads/log/spring-headless.log` |
| per game: infolog, start script, result, replay link | `spads/games/<time>-<gameId>/` |
| replays | `spads/var/demos*/` |

On the node: `/mnt/data2/k8s-volumes/rjm-bar-host/<env>/out/`. From anywhere:
`kubectl logs -n bar-test deploy/rjm-bar-host-spads -c spads`.

## 7. How it is tested

`tests/e2e/run.sh` runs the whole flow offline — real SPADS, real engine,
real SMRTBARb — against a fake lobby server that plays Teiserver's part and
scripts the owner, a spammer and a human player whose game client is a real
headless engine. See [`testing.md`](testing.md).
