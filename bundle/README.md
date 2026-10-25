# Frozen local rules bundle

Copied unchanged from `../hexwar-play/snapshot/` on 2026-10-25. These artifacts
are local pilot preparation, not a published release or a license grant.

| Artifact | SHA-256 |
|---|---|
| `hexwar-engine` | `2f98bda6690f0a3902d0f8e23f4d8a8ed27e5a693e97087a35db250098a58da2` |
| `game.json` | `c4276d8bd04f9b64acc0b6f8a28c9b1697166b3237394390b5473e7858b7a366` |

The executable is Linux x86-64, dynamically linked to the platform runtime,
approximately 876 KiB. The snapshot is approximately 29 KiB and contains all nine
faction-pair starting states. `engine.py` verifies both artifact hashes before
executing it. Non-Linux builds and source distribution need an explicit release
step; do not bypass the binary pin silently.

The snapshot uses V14, radius four, an 84-action deadline, the locked Orc/Necro/Fey
armies, and the play-site initiative order Orc > Fey > Necro. Entrant identities
are attached by `Series`, not stored in the underlying game states.

Rehearsal: `python scripts/smoke_series.py --output /tmp/hexwar-series.json`.
This uses a first-legal-action smoke player. It is not an entrant, a strong-play
benchmark, a cryptographically authenticated record or a payout claim. The
parent integration supplies signing and final verification separately.
