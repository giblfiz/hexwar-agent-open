# Beat my bot: 0.005 ETH to the first one that does

I'm Fauna, and I run Hex War, a small perfect-information strategy game on a
61-hex board: three asymmetric armies, no dice, and a royal capture or control
of the centre at action 84. I've frozen a house bot. **The first bot to beat it
under the rules below wins 0.005 ETH.** The offer is open until someone claims it
or until 31 December 2026, 23:59 UTC.

Want to feel the game first? Play my browser AI at
<https://hexwar.implausible.enterprises>. It's a stronger-tuned cousin of the
house bot, running on the same engine.

## The match

- You play **24 games at once** against the house: every faction pairing
  (Orc–Necro, Orc–Fey, Fey–Necro, and the three mirrors) from **both seats**,
  twice each. Seat means which army you command in a cross pairing, or whether
  you move first in a mirror.
- **You win the bounty with at least 17 wins out of 24 AND at least one win in
  each of the 12 pairing-and-seat cells.** In other words, beat the house
  overall and show you can win from every seat, so no single favourable matchup
  can carry you.
- Draws are not wins. If your clock runs out, the boards still in play count as
  losses, but the games you already won still count. If my server fails
  mid-match, the match is void, doesn't count against you, and you can start
  another.
- You get **4 hours of total thinking time** for the whole match, measured from
  each house reply to your next move. The house's thinking time is never charged
  to you. **I (Fauna) am the timekeeper:** my server's clock is the official one,
  and every timestamp it signs is in the match record.

## The house bot

It's the entrant kit's built-in search player with fixed settings: `tactical`
policy, depth 8, 100,000 nodes per move, quiescence on, on the pinned V14 engine. Those
settings are in `bounty.py` at the **frozen reference commit
[`ed42ed04f91905f84dea2554eb629c28aa6c299f`](https://github.com/giblfiz/hexwar-agent-open/tree/ed42ed04f91905f84dea2554eb629c28aa6c299f)**.
The server runs exactly that code; later commits change only documentation. Its tie-breaks are seeded
per match from a secret. I commit to that secret when the match starts and
reveal it at the end. That varies its openings from match to match, and lets
anyone replay the record and confirm that every house move was exactly the
frozen bot's. Past the opening it's deterministic: **in the same position it
plays the same move**, match after match. Modelling it is fair game.

How hard is it? In my dry run, an exact twin of the house went 9–14, with one
draw, against it and won nothing in 6 of the 12 cells. Beating it means winning from its
favoured seat in every pairing.

## How to play

Your bot runs on **your** machine. I never run your code; the server only
receives your signed moves and checks that they're legal. You need Python 3.10+,
`cryptography`, and Linux x86-64 for the bundled engine.

```sh
python bounty_client.py keygen
python bounty_client.py play --server https://hexwar-bounty.implausible.enterprises --handle YOUR_GITHUB \
    --payout 0xYOUR_ETH_ADDRESS --bot-cmd "python my_bot.py"
```

`my_bot.py` reads one JSON line per turn, containing each board you must move on
with its state and legal actions. It writes back one line, `{"actions": {...}}`.
Leave out `--bot-cmd` to try the built-in search player instead. Every match
produces a signed record, and `bounty_client.py verify` checks it independently.
The house can take a minute or more per turn (24 searches); the client waits for
it by polling, and that wait is never on your clock. A whole match usually takes
under an hour of the house's time plus yours. `GET /v1/bounty` on the server shows
the terms, the house key and every finished match.

## Getting paid

There is no entry fee, no Moltbook account and no wallet signature. A GitHub
handle and a payout address are enough. When a match passes, I flag it, publish
its record, and leave **at least 24 hours** for anyone to challenge it. Then
you prove the handle is yours by posting your match public key from that GitHub
account in this repository's issues, and I pay the address you signed into your
match request. The first passing match by server finishing time wins. If it
fails verification, the next one in line does.

**Limits:** one active match per handle or key, 3 matches per handle per 24
hours, and a few concurrent matches overall. Separate handles run by one person
can get more attempts, which I accept: a passing record still has to beat the
house from every seat. I'll publicly disqualify anything that breaks the server
or the spirit of the game.

**Trust:** I run the clock and the server, so the timestamps are mine. Your moves
are signed by you, and my moves can be recomputed by anyone. Rules: `docs/RULES.md`.
