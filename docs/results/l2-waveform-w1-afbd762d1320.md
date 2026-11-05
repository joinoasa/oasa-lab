# l2-waveform-w1, job afbd762d1320

- **Result:** 10 passed, 0 failed, 0 skipped; checker 1.2 **PASS**; **counted**
- **When:** 2026-11-04T22:44:36.902Z to 2026-11-04T22:55:45.373Z (UTC)
- **oasa-lab:** `0904226f9f3935ed86481649606342225d03861b`, seed 0, run id `d9663bc3f3054609bcc6935059999e4f`
- **Job service:** rf_setup `cabled`, wiring confirmed `usrp`
- **What it proves:** OASA's own waveform, milestone W1: the oasa-radio W0 modem between a B200mini and an E310 over cable, both ways, BPSK and QPSK. The bit error rate at a measured Eb/N0 is within 1 dB of theory, noise alone gives no frames, and the wrong demodulator passes no CRC, and frame format v1 is found without the length near 1 and 3 dB (needs a B200mini, an E310 and OASA_RADIO_BIN_DIR)

## Binaries

| Name | SHA-256 | Version |
|---|---|---|
| `oasa-radio` | `b7d10c58031911055a97b6f68c7727fb5b934eed735c1fd3d68ad03265f95935` |  |

## Hardware

| Device | Kind | Backend | Model | Firmware |
|---|---|---|---|---|
| b2000 | b200 | hardware | Ettus B200mini | fw 8.0, fpga 7.0 |
| e3100 | e310 | hardware | Ettus USRP E310 | mpm 5.3, fpga 6.1 |

RF setup **cabled**, wiring `usrp`.
- b200-e310: b2000:tx/rx to e3100:rx2-b, 60.0 dB
- e310-b200: e3100:trx-a to b2000:rx2, 60.0 dB

## Tests

| Test | Result |
|---|---|
| `suites/l2-waveform-w1/test_waveform_w1.py::test_ber_matches_theory[bpsk-e310-b200]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_ber_matches_theory[bpsk-b200-e310]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_ber_matches_theory[qpsk-e310-b200]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_ber_matches_theory[qpsk-b200-e310]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_v1_header_finds_frames[bpsk-e310-b200]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_v1_header_finds_frames[bpsk-b200-e310]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_v1_header_finds_frames[qpsk-e310-b200]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_v1_header_finds_frames[qpsk-b200-e310]` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_wrong_modulation_fails_the_crc` | passed |
| `suites/l2-waveform-w1/test_waveform_w1.py::test_noise_only_gives_no_frames` | passed |

## Checker

| Check | Result | Reason |
|---|---|---|
| integrity | pass | all 19 files match SHA256SUMS; none missing or extra |
| manifest | pass | all required fields present and sane; 0 channel(s), 0 capture(s) |
| results_match_log | pass | 10 tests: CTRF, log.jsonl and junit.xml agree (10 passed, 0 failed, 0 skipped) |
| no_readable_payload | pass | no captures in this run; nothing to scan |
| numbers_make_sense | pass | no channels in this run; nothing to check |
| known_bad_cases_ran | pass | 2 expected test(s) ran and passed (confirmed against the suite file) |
| radio_evidence | pass | 2 device(s); RF cabled, 2 path(s) at or above the minimum loss; 7 SigMF recording(s) listed, hashed and valid; 61 measurement(s) inside the suite's limits |

## Measurements

| Metric | Name | Value | Unit | Limits | OK |
|---|---|---|---|---|---|
| `rx_peak_fs` | peak e310-b200 bpsk TX 12.1 | 0.00574 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss e310-b200 bpsk at 1.46 dB | -0.206 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak e310-b200 bpsk TX 14.1 | 0.00581 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss e310-b200 bpsk at 3.77 dB | 0.086 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak e310-b200 bpsk TX 16.1 | 0.00621 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss e310-b200 bpsk at 5.46 dB | -0.141 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak e310-b200 bpsk TX 18.1 | 0.00683 |  | 0.0 to 0.3 | yes |
| `points_measured` | points e310-b200 bpsk | 3 |  | 2 to 100 | yes |
| `rx_peak_fs` | peak b200-e310 bpsk TX 21.3 | 0.00557 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 bpsk at 0.97 dB | 0.228 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak b200-e310 bpsk TX 23.3 | 0.00618 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 bpsk at 2.68 dB | -0.056 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak b200-e310 bpsk TX 25.3 | 0.00618 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 bpsk at 4.61 dB | -0.035 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak b200-e310 bpsk TX 27.3 | 0.00642 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 bpsk at 6.81 dB | 0.15 | dB | -1.0 to 1.0 | yes |
| `points_measured` | points b200-e310 bpsk | 4 |  | 2 to 100 | yes |
| `rx_peak_fs` | peak e310-b200 qpsk TX 14.6 | 0.00565 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss e310-b200 qpsk at 1.03 dB | -0.227 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak e310-b200 qpsk TX 16.6 | 0.00694 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss e310-b200 qpsk at 3.07 dB | -0.194 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak e310-b200 qpsk TX 18.6 | 0.0067 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss e310-b200 qpsk at 4.97 dB | -0.188 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak e310-b200 qpsk TX 20.6 | 0.00704 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss e310-b200 qpsk at 7.06 dB | -0.0 | dB | -1.0 to 1.0 | yes |
| `points_measured` | points e310-b200 qpsk | 4 |  | 2 to 100 | yes |
| `rx_peak_fs` | peak b200-e310 qpsk TX 24.1 | 0.00594 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 qpsk at 0.52 dB | -0.112 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak b200-e310 qpsk TX 26.1 | 0.00594 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 qpsk at 2.67 dB | 0.049 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak b200-e310 qpsk TX 28.1 | 0.00637 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 qpsk at 4.66 dB | -0.017 | dB | -1.0 to 1.0 | yes |
| `rx_peak_fs` | peak b200-e310 qpsk TX 30.1 | 0.00664 |  | 0.0 to 0.3 | yes |
| `ber_loss_db` | loss b200-e310 qpsk at 6.68 dB | -0.227 | dB | -1.0 to 1.0 | yes |
| `points_measured` | points b200-e310 qpsk | 4 |  | 2 to 100 | yes |
| `rx_peak_fs` | peak v1 e310-b200 bpsk TX 12.1 | 0.00577 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header e310-b200 bpsk at 1.46 dB | 0.5 |  | 0.0 to 1.05 | yes |
| `v1_found_fraction` | v1 found e310-b200 bpsk at 1.48 dB | 0.9792 |  | 0.9 to 1.05 | yes |
| `rx_peak_fs` | peak v1 e310-b200 bpsk TX 14.1 | 0.00624 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header e310-b200 bpsk at 3.77 dB | 0.7708 |  | 0.0 to 1.05 | yes |
| `v1_found_fraction` | v1 found e310-b200 bpsk at 3.92 dB | 0.9896 |  | 0.9 to 1.05 | yes |
| `rx_peak_fs` | peak v1 b200-e310 bpsk TX 21.3 | 0.00596 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header b200-e310 bpsk at 0.97 dB | 0.3646 |  | 0.0 to 1.05 | yes |
| `rx_peak_fs` | peak v1 b200-e310 bpsk TX 23.3 | 0.00621 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header b200-e310 bpsk at 2.68 dB | 0.625 |  | 0.0 to 1.05 | yes |
| `v1_found_fraction` | v1 found b200-e310 bpsk at 2.66 dB | 0.9792 |  | 0.9 to 1.05 | yes |
| `rx_peak_fs` | peak v1 e310-b200 qpsk TX 14.6 | 0.00609 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header e310-b200 qpsk at 1.03 dB | 0.4479 |  | 0.0 to 1.05 | yes |
| `v1_found_fraction` | v1 found e310-b200 qpsk at 1.20 dB | 0.9896 |  | 0.9 to 1.05 | yes |
| `rx_peak_fs` | peak v1 e310-b200 qpsk TX 16.6 | 0.00602 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header e310-b200 qpsk at 3.07 dB | 0.6667 |  | 0.0 to 1.05 | yes |
| `v1_found_fraction` | v1 found e310-b200 qpsk at 3.26 dB | 0.9896 |  | 0.9 to 1.05 | yes |
| `rx_peak_fs` | peak v1 b200-e310 qpsk TX 24.1 | 0.0059 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header b200-e310 qpsk at 0.52 dB | 0.2917 |  | 0.0 to 1.05 | yes |
| `v1_found_fraction` | v1 found b200-e310 qpsk at 0.59 dB | 0.9896 |  | 0.9 to 1.05 | yes |
| `rx_peak_fs` | peak v1 b200-e310 qpsk TX 26.1 | 0.00642 |  | 0.0 to 0.3 | yes |
| `w0_header_found_fraction` | W0 read by header b200-e310 qpsk at 2.67 dB | 0.625 |  | 0.0 to 1.05 | yes |
| `v1_found_fraction` | v1 found b200-e310 qpsk at 2.45 dB | 0.9896 |  | 0.9 to 1.05 | yes |
| `wrong_mod_crc_ok` | BPSK decoded as QPSK, good CRCs | 0 |  | 0 to 0 | yes |
| `right_mod_crc_ok` | BPSK decoded as BPSK, good CRCs | 79 |  | 1 to 100000 | yes |
| `noise_only_frames` | frames in noise | 0 |  | 0 to 0 | yes |

## Recordings

7 SigMF recording(s), hashed in the bundle (kept in the lab's evidence store).

Generated by `tools/result_summary.py` from the bundle and its checker verdict. Serial numbers and addresses are left out on purpose.
