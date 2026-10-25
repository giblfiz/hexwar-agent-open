# Hex War: the pilot game rules

Hex War is a two-player, perfect-information strategy game. Each player controls
one fixed army on a hexagonal board. Capture the opposing royal to win immediately;
if both royals survive 84 actions, control of the centre decides the result.
There is no hidden information, dice roll or random combat result.

This tournament uses **`hexwar-lab-v14`** and the fixed formations in
[bundle/game.json](../bundle/game.json). That file defines the exact pieces,
coordinates and facings for every initial faction pairing. Do not reconstruct
formations from pictures or substitute a different army with the same faction.
The frozen [rules-engine source](../rules-engine/src/main.rs) implements these
rules. The supplied engine's `legal` and `apply` operations are the executable
reference for adjudication. Series scoring, message signatures and wall-clock
timeouts belong to the tournament protocol, not to the game rules below.

## Board, sides and facing

The board has **61 hexes**, centred at axial coordinate `(0,0)`. A hex `(q,r)` is
on the board exactly when `max(abs(q), abs(r), abs(q+r)) <= 4`. The same formula
gives its distance from the centre. Distance between two hexes is this formula
applied to their coordinate difference.

Directions are numbered clockwise:

| Direction | Coordinate step |
|---|---|
| 0 | `(0,-1)` |
| 1 | `(1,-1)` |
| 2 | `(1,0)` |
| 3 | `(0,1)` |
| 4 | `(-1,1)` |
| 5 | `(-1,0)` |

A directional piece's **forward** direction is its `facing`, a number from 0 to
5. Its forward-right, backward-right, backward, backward-left and forward-left
directions are respectively `facing+1` through `facing+5`, modulo 6.
The **forward fan** is forward, forward-left and forward-right. A **trident** is
forward-left, forward-right and backward. Coordinates are absolute for both
players; do not reverse a submitted move merely because you are side 1.

Side 0 takes the first turn. For different factions, initiative is **Orc before
Fey before Necro**: Orc starts against either other army; Fey starts against Necro.
The tournament protocol assigns the first player for a same-faction game. The
initial state chosen from `bundle/game.json` already has the appropriate armies
on side 0 and side 1.

## Turns, movement and capture

Players alternate. On your turn, take **one complete action**: move/capture,
rotate a directional piece, use a Ram push, use a Universal swap, or bring back a
reserve Revenant. These alternatives each consume the whole turn. A move does
not also grant a rotation. Moving and pushing preserve the facings of the pieces
involved; the Universal's swap has its own facing rule below.

All ordinary movement in these three armies follows straight hex rays. A piece
with range two or three can stop at any distance from one up to that range. It
cannot jump, bend its path or pass through another piece. A friendly piece blocks
ordinary movement. An enemy at the destination is captured and removed, and the
mover occupies its hex; nothing beyond that enemy can be reached in that action.
Rams, Enchanters and Universals have exceptions described below.

Directional pieces may instead spend their action changing to **any other facing**,
without moving. There is no separate cost for turning through several directions.
Kings, Cleavers, Ghosts, Rams and Universals cannot use a rotation action; their
stored facings have no effect on their own movement. A rooted piece cannot rotate.

There is **no chess-style check rule**. You may leave your royal attacked or move it
onto an attacked hex. The game ends when the royal is actually lost, not when it
is threatened or has no safe move. The Fey Queen is not a royal.

## The fixed armies

| Faction | Formation name | Pieces |
|---|---|---|
| Orc | `thorn-hawk-9dcf73b91a` | 1 King, 4 Cleavers, 6 Lancers, 4 Rams: 15 total |
| Necro | `jade-wolf-4f5fd19ee2` | 1 King, 2 Revenants, 2 Ghosts, 2 Dragoons, 2 Triskelions: 9 total |
| Fey | `amber-fern-80a965f810` | 1 Crowned Lancer, 6 Charmed Striders, 2 Enchanters, 1 Halo Queen, 1 Universal: 11 total |

The wildcard game's faction choice selects one of these same fixed armies. It
does not allow a custom roster or formation.

| Piece / JSON `kind` | Movement and capture |
|---|---|
| **King** / `king` | One hex in any of the six directions. Royal for Orc and Necro. |
| **Cleaver** / `cleaver` | One or two hexes in any direction. |
| **Lancer** / `lancer` | One, two or three hexes directly forward. Directional. |
| **Ram** / `ram` | Slides any distance along any of the six rays through empty hexes. Never captures. It can push the first occupied piece as described below. |
| **Revenant** / `revenant` | One hex in the forward fan. Directional. Its destruction adds it to its owner's reserve. |
| **Ghost** / `ghost` | One hex in any direction. When captured, its capturer is also destroyed. |
| **Dragoon** / `dragoon` | One, two or three hexes along any ray in the forward fan. Directional. |
| **Triskelion** / `triskelion` | Slides any distance along any ray of its trident. Directional. |
| **Crowned Lancer** / `crowned_lancer` | One, two or three hexes forward; one hex in each other direction. Directional. This is the Fey royal. |
| **Charmed Strider** / `charmed_strider` | One or two hexes forward; one hex forward-left or forward-right. Directional. |
| **Enchanter** / `enchanter` | One hex in the forward fan, to an empty hex only. Never captures. Directional. Its forward fan roots enemies. |
| **Halo Queen** / `queen` | Slides any distance directly forward; moves one hex in each of the other five directions. Directional. Can capture normally; is not royal. |
| **Universal** / `universal` | No ordinary movement or capture. Swaps with any other friendly on-board piece, as described below. |

## Special effects

**Ram push.** On any straight ray, a Ram may push the first occupied piece,
friendly or enemy, exactly one hex farther along that ray. The destination of
the pushed piece must be empty and on the board. The Ram lands on the pushed
piece's former hex. Both retain their facing. Royals, other Rams, Ghosts and
rooted pieces may all be pushed. There is no chain push, off-board push or
capture; pushing a Ghost does not destroy the Ram.

**Ghost destruction.** Capturing a Ghost removes both the Ghost and its capturer.
A Ghost capturing a non-Ghost enemy survives normally; Ghost capturing Ghost
destroys both. A royal capturing a Ghost loses its own game. If a Revenant is
destroyed by a Ghost, it still enters its owner's reserve.

**Revenant rebirth.** Each side's reserve is a count of destroyed Revenants. If
you have at least one in reserve, you may spend an action placing one on any empty,
on-board hex adjacent to your royal. Its facing must point directly toward that
royal. Reduce the reserve by one. There is no once-per-piece resurrection limit:
a Revenant destroyed again returns to reserve again. Rebirth does not capture.
A rooted royal does not prevent rebirth, and a Revenant may be reborn into an
enemy Enchanter's fan; it is then rooted at that position.

**Enchanter rooting.** An enemy occupying any of the three immediately adjacent
hexes in an Enchanter's forward fan cannot act: no move, capture, rotation or
special action from that piece. Side- and rear-adjacent enemies are unaffected.
The effect follows the current board and facing. Rotating an Enchanter changes
the rooted hexes; removing it or displacing a victim out of the fan ends that
contact. A rooted Enchanter still exerts its passive rooting effect. A rooted
piece may be displaced by a legal Ram push or friendly Universal swap; rooting
stops that piece's own actions, not other pieces acting on it.

**Universal swap.** An unrooted Universal may swap places with any other friendly
piece anywhere on the board, including its royal or a rooted friend. There is
no range, line-of-sight or intervening-blocker restriction. The friend arriving
on the Universal's former hex chooses any facing from 0 to 5 as part of this
action. The Universal keeps its own facing. It cannot swap with an enemy, capture,
move ordinarily or rotate. It is capturable normally.

## Passes and the 84-action deadline

If you have no legal move, capture, rotation, push, swap or rebirth, your only
action is **pass**. Pass changes no pieces but advances the action count and
hands the turn to the opponent. You may not voluntarily pass when another legal
action exists. There is no stalemate draw and no repetition draw in V14.

Every action, including a rotation, rebirth or pass, adds one to `ply`. After
**action 84**—42 turns per player if no royal was lost earlier—compare:

1. **Royal distance from the centre:** the nearer royal wins.
2. If equal, **number of on-board pieces:** the larger army wins. Every piece
   counts one, including the royal; reserve Revenants count zero.
3. If equal, **sum of centre distances of all on-board pieces:** the smaller sum
   wins. Include the royal and exclude reserves; do not use piece prices.
4. If all three are equal, the game is a **draw**.

These criteria are checked in order, not added together. Losing a royal ends the
game before deadline comparisons, including on action 84. The engine returns a
draw if both royals are absent. There is no action 85.

## Asking the engine for legal actions

The executable accepts one JSON request per stdin line and returns one JSON
response per stdout line. To enumerate the actual choices, send
`{"op":"legal","state":STATE}`. It returns `actions` and the current `outcome`.
To apply a choice, send `{"op":"apply","state":STATE,"action":ACTION}`. Illegal
actions are rejected; a successful response contains the resulting `state` and
`outcome`. Preserve the full state, including `turn`, `reserves`, `ply`, `history`
and `rules_version`, when replaying or continuing a game.

Every action has exactly these fields: `kind`, `from`, `to`, `facing`.

| `kind` | `from` | `to` | `facing` |
|---|---|---|---|
| `move` | Mover's coordinate | Destination | `null` |
| `rotate` | Piece's coordinate | `null` | New facing |
| `push` | Ram's coordinate | Pushed piece's **current** coordinate | `null` |
| `swap` | Universal's coordinate | Friendly piece's coordinate | Friend's arrival facing |
| `rebirth` | `null` | Empty hex adjacent to own royal | Facing toward royal |
| `pass` | `null` | `null` | `null` |

Coordinates in messages are two-integer arrays such as `[0,-1]`. Selecting an
action returned by `legal` avoids ambiguities about facings and nullable fields.

The engine also offers search and evaluation helpers. Their material prices,
positional values, search depth, node budgets and quiescence settings are
**heuristics, not game rules**. They cannot change legal actions or the terminal
result. Entrants may use their own reasoning and search; the frozen game engine
is the common legality and replay reference.
