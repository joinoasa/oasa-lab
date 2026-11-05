# Published results

Counted runs (tests passed and the checker passed), one summary each, made with `python3 tools/result_summary.py BUNDLE.tar.gz` from the bundle the job service hands out. Serial numbers and addresses are left out; the full bundles, with recordings, stay in the lab's evidence store.

| Suite | Job | When | oasa-lab | What it showed |
|---|---|---|---|---|
| `l2-rx-noise` | [91a7a6de3db9](l2-rx-noise-91a7a6de3db9.md) | 2026-11-03 | `e844572` | Both radios alive and quiet; the E310 draws normal power |
| `l2-usrp-loopback` | [50d5d0ac0cf2](l2-usrp-loopback-50d5d0ac0cf2.md) | 2026-11-03 | `5f917ce` | First cabled TX: tone level follows the gain both ways, crystal offset 2.5 ppm and mirrored, nothing left after TX off |
| `l2-waveform-w1` | [afbd762d1320](l2-waveform-w1-afbd762d1320.md) | 2026-11-04 | `0904226` | OASA's own modem between the radios: BER within ±0.23 dB of theory, frame v1 found without the length |
| `l2-e310-two-channel` | [76da6dfb495c](l2-e310-two-channel-76da6dfb495c.md) | 2026-11-05 | `066418e` | Both E310 channels in one stream; 47 dB channel isolation; `waveform df` agrees with the lab |
| `l2-sdr-link` | [0e21d0f8787f](l2-sdr-link-0e21d0f8787f.md) | 2026-11-05 | `554665d` | The encrypted oasa-sec link over the real radios (R-COM2): handshake in 181 s, every message delivered both ways, ciphertext-only air; a replayed and a changed packet dropped, an untrusted identity refused |

T-BENCH2's remote acceptance (counted runs started through the Claude connector, plus the refusals) is made with `tools/remote_acceptance.py`; its `summary.md` and the per-run pages go here as `remote-acceptance-DATE.md` with their run pages.
