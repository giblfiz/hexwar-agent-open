# Frozen rules-engine source

Exported from the Hex War lab commit `678cc69d5e3777e302bf7cc10ef8b00156853625`,
the source revision recorded by the V14 public-player snapshot.
The tournament reference binary and exact starting states are in `../bundle/`.
A local rebuild may differ in binary hash because of compiler/toolchain/platform.
Participants may use their own builds and players; final judging uses the pinned
reference engine and states.

With the Rust toolchain installed, build using `cargo build --release --locked`
in this directory. The crate produces `target/release/hexwar-engine`. Consult the
JSON line protocol in `src/main.rs` and the adapter in `../engine.py`. The
supplied CLI intentionally uses the pinned reference binary; replacing it
changes its hash and is not an accepted reference-verifier configuration.

Launch verification (2026-10-25): `cargo build --release --locked` reproduced
the reference SHA-256 exactly, and 588 legal-action/state-transition comparisons
across seven full games matched the reference. Reproduction on other toolchains
is not guaranteed.
