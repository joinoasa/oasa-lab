# The checker

The checker is a separate program that re-verifies an evidence bundle **without trusting the run that made it**. A run counts only after the checker passes it.

```
oasa-lab check runs/<bundle>          # exit 0 = pass, 1 = fail
python -m oasa_lab.checker runs/<bundle>   # the same, no lab code needed
```

Both write the verdict to `runs/<bundle>.check.json`, **beside** the bundle folder, not inside it. Why: the bundle is hashed and signed once and never changes. The verdict is a judgment *about* it. `check.json` records the SHA-256 of the bundle's `SHA256SUMS`, so it can't be paired with a different bundle. `oasa-lab run` runs the checker for you.

## What it will not do

- It imports **only the Python standard library**. Not the runner, the channel, the pcap writer or the pytest plugin. A test in `l0-lab` enforces this.
- It reads every file again. It trusts no number the run wrote about itself.
- A bundle it can't read **fails**; it never crashes or skips.

## The six checks

Each check is `pass` or `fail`, with a reason in plain words.

### 1. Integrity

`SHA256SUMS` must match the folder exactly.
- Every listed file exists and its SHA-256 matches.
- No file is missing. No extra file is present (anything not listed fails).
- No symbolic links, no paths with `..`, no duplicate lines.
- The file is in `sha256sum` format, so `cd bundle && sha256sum -c SHA256SUMS` works too.

### 2. Manifest

`manifest.json` has every required field, with sane values:
- `run_id` is 32 hex characters; `suite` is a plain name; `seed` is an integer.
- `start` and `end` are RFC 3339 UTC times, and `end` is after `start` (and not in the future).
- `lab.git_sha` is 40 lowercase hex characters; `git_dirty` is true or false (a dirty tree is allowed, and reported).
- Python and OS versions are present; the package list includes pytest and oasa-lab.
- Every channel has settings in range (probabilities 0 to 1, no negative delays).
- Every binary has a 64-hex SHA-256.
- Markers are unique strings of 12 or more characters.
- Every `.pcap` in `captures/` is declared. A capture the manifest doesn't mention would escape the scan, so it fails.
- If a `suites/` folder is given (the default for `oasa-lab check`), the suite must exist there.

### 3. Results match the log

- `results.ctrf.json` is well-formed CTRF and its summary counts equal the real test list. At least one test ran.
- `log.jsonl` begins with `run_start` and ends with `run_end`, once each. Both carry the manifest's `run_id`, and the seed matches. Time stamps don't run backwards and fall within the run's start and end.
- Every test in CTRF has one `test_start` and one `test_end` in the log with **the same outcome**, and every test in the log is in CTRF.
- `run_end` counts equal the CTRF counts.
- `junit.xml` is counted from its test cases (not its attributes) and must agree on passed, failed and skipped.

### 4. No readable payload

For every capture the manifest marks `encrypted: true`:
- **Markers:** none of the run's random marker strings appears anywhere in any packet payload.
- **MAVLink:** no packet is made of MAVLink 1 or 2 frames (start byte `0xFE` or `0xFD`, plausible header, and lengths that add up to exactly the packet size). Random bytes can match by chance (about 1 in 65,000 packets), so the limit is the number of matches that chance would give with a 1-in-a-million error rate, not zero. Real plaintext MAVLink hits every packet.
- **Randomness:** a chi-square test over all payload bytes (identical copies counted once, protocol header bytes skipped) must not reject "uniformly random" at p < 1e-6. It runs only with 5,000 or more bytes; with fewer it says so.
- The capture must not be empty. An empty capture proves nothing.

`skip_header_bytes` (set per suite) leaves the first bytes of each packet out of the randomness test only, because headers (type, counter) are not ciphertext. The marker and MAVLink scans always cover the whole packet.

**Negative control.** For a capture marked `encrypted: false, expect_plaintext: true` the checker must **find** the run's marker (and find MAVLink frames if `expect_mavlink`), and must see that the bytes are *not* random. If it doesn't, the check fails. That proves the scanner works. The suite `l1-plaintext-control` does this on every CI run.

**The suite decides what is encrypted.** If the suite's `suite.toml` is available, the manifest's flags must equal it. A run can't relabel a capture to dodge the scan.

### 5. Numbers make sense

From the final `channel_stats` line of each channel in the log, per direction:
- `forwarded` = sent - dropped - too_big - unrouted + duplicated + replayed - abandoned.
- The number of packets for that direction in the pcap equals `forwarded`. No packet goes to a stranger. No packet is larger than the MTU. Nothing was abandoned in flight.
- Each measured rate (loss, duplicate, corrupt, reorder, replay) is within **5 standard deviations** (binomial) of the configured value. A rate of 0 must measure exactly 0. With fewer than 100 packets a rate can't be tested; the check says it skipped it.

### 6. Known-bad cases really ran

`expectations` in the manifest lists tests that must exist **and pass**: the attacker test, the corruption test, the control. Each name is matched against the CTRF results and the log. A missing, failed or **skipped** test fails the check. So a suite that skips its attacker test (for example `l1-oasa-link` without the binaries) does not pass; it shows exactly why.

If the suite file is available, the manifest must list at least the expectations in `suite.toml`. A run can't drop its hardest tests.

## Reading `check.json`

```json
{ "ok": false,
  "bundle": "20261023T042320Z-l1-oasa-link-ec0159e8",
  "sha256sums_sha256": "…",
  "checks": [ {"id": 6, "name": "known_bad_cases_ran", "status": "fail", "reason": "…", "details": {…}} ] }
```

`details` holds the numbers behind each verdict (per-capture chi-square, per-rate counts).

## Limits (honest)

- The checker catches careless and accidental problems and obvious cheating. It does not stop someone who rewrites the whole bundle, the manifest and `SHA256SUMS` together. That needs the signature (milestone M5) and CI provenance (`actions/attest-build-provenance`), which bind the bundle to a build.
- The randomness test shows ciphertext looks random. It does not prove the cipher is sound.
- Channel statistics come from the channel itself and are cross-checked against the pcap it wrote. A second tap (a different program) comes with the radio tiers.
