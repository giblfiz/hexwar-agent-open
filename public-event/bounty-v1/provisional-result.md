# Bounty v1: provisional result

**Status: provisional. Nothing has been paid.**

Match `m20261102T094401-48498739`, handle `youngeboxs-ai`, finished at
2026-11-02T10:03:14Z with 22 wins, 2 losses and 0 draws, and at least one win
in all 12 pairing-and-seat cells. The bounty needs 17 wins and every cell, so
this match passes.

I re-ran the verifier on the record with every house move recomputed. It came
back verified and passed. The record is in this folder, and you can check it
yourself:

```sh
python bounty_client.py verify \
  --record public-event/bounty-v1/m20261102T094401-48498739.json \
  --house-key cbca75c9a46d2e5c3041f7649789cdca1a8b637dc9e7cabd9ddc09aa548cf828
```

Record digest (`signing.digest`):
`f883c562a50b25db2e2686b1f4df1ba1c0f3b90f8b7164e8f38eda352f09ecf6`

Challenges stay open for at least 24 hours from publication, in this
repository's issues. To complete the claim, the handle owner posts the match
public key from that GitHub account:
`45f190989d5a7a879be01f487773fe30d3e84c608fa1dbb09178e190a152b2ec`

Payment goes only to the address signed into the match request:
`0x1739370A525e06680d33Bf6a8212895167cE2519`

— Fauna
