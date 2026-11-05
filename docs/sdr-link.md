# The encrypted link over the SDR bench (R-COM2 on OASA's own waveform)

**Status:** Draft v0.1, 2026-11-05 · **Owner:** citruscybersec · Builds on [r-com2-plan.md](r-com2-plan.md) (milestones M3 and M6)

The oasa-sec link already runs through the simulated channel (`l1-oasa-link`). OASA's own modem already runs on the B200mini and the E310 over cable (`l2-waveform-w1`). This plan joins the two: real `oasa-link` processes whose packets cross the bench as W1 frames, so the handshake, the data and the attack cases all go over real RF. It is the SDR version of milestone M3, which was planned for LoRa boards; the lab has the SDRs now and no LoRa boards.

## 1. What it proves

| Claim | How the suite shows it |
|---|---|
| A real handshake and real data cross real RF | Two `oasa-link` processes, Alice and Bob; every packet between them is modulated, sent, recorded and demodulated. Bob's app gets Alice's messages. |
| The air carries only ciphertext | Every frame the receiver decoded goes into an air capture (`[captures] encrypted = true`); the checker's scanner looks for the run's marker and plaintext structure. The SigMF recordings are kept. |
| A stranger gets nothing | A third identity that neither side trusts sends handshakes over the same RF hop: no reply, `untrusted_peer` drops. |
| A recorded packet cannot be replayed | The relay sends a frame it carried before again: `replay` drops, no second delivery. |
| A damaged frame is never delivered | A frame with a changed byte (sent with a valid frame CRC, so the modem passes it) is dropped by the link as `bad_tag`. |
| The scanner would see plaintext | Negative control, as `l2-rf-control` is for LoRa: plaintext frames sent on purpose over the same hop **are** flagged. |

## 2. How the bench carries packets

```
 app A ──UDP── oasa-link Alice ──UDP──┐                         ┌──UDP── oasa-link Bob ──UDP── app B
                                      │      RF relay (lab)      │
                                      └─ queue A→B ─ B200 TX/RX ═60 dB═ E310 RX2-B ─ decode ─┘
                                      ┌─ decode ─ B200 RX2 ═60 dB═ E310 TRX-A ─ queue B→A ─┐
```

The relay replaces `lab.channel(...)` in the link harness. It listens on the two UDP addresses the links send to, queues each datagram, and works in **cycles**, one direction at a time:

1. Take the queued datagrams for one direction (up to a block's worth).
2. Make one frame v1 per datagram (`oasa-radio waveform iq --frame v1`, BPSK, 125 kbaud at 1 MS/s, digital IF 250 kHz, as W1) and join them with gaps into one block.
3. Send the block once from one radio (`tx_samples_from_file`), record it on the other.
4. Decode the recording (`waveform decode --frame v1`). Each frame whose CRC passes goes to the far link's UDP port, and into the air capture.

Then the other direction. Half duplex, one UHD process per radio at a time: UHD cannot open one USRP from two processes, and the lab never links UHD (GPL-3.0) into its own code.

**Sizes.** A v1 frame carries up to 255 bytes. `oasa-link --mtu 200` keeps every packet in one frame (handshakes are 119 and 55 bytes; data is plaintext + 22). One datagram, one frame: nothing is split or joined, so loss stays packet loss, which the link already handles.

**Timing.** A cycle is 14 to 20 s per direction on the bench (the transmit tool's start, the E310's claim, the recording and the decode; first run 2026-11-05). A packet that just missed its cycle waits for it to end and for the other direction's cycle, so a round trip with queueing is **35 to 70 s**. On the simulated bench a cycle takes well under a second to a few seconds.

The link must know this. Both links run with `--handshake-rtt-ms 70000`: oasa-sec's *R* is the worst handshake round trip **including queueing and relays**, not only airtime (security review L1). From it the link sets (oasa-sec `Config::for_airtime`):

| Timer | Value | Why it fits |
|---|---|---|
| Handshake retry | 107 s (1.5 R + 2 s), ±20 %, backing off to 428 s | The answer (≤ 70 s) arrives before the earliest retry (86 s) |
| Stale | 428 s (4 × retry) | A side answers payload after half of it (214 s), far over a round trip |
| Keepalive | off (R > 2 s) | Not needed: the session confirmation and acks are sent anyway, and every keepalive costs a frame in the cycle |
| Rekey / reject | 600 s / 1028 s | Rekey ≥ 4 × retry, as oasa-sec requires |

So a handshake takes one round trip, and only a lost frame costs a retry. Before this (default 5 s retry, `--keepalive-secs 5`) every answer missed its retry: the initiator kept sending new handshakes and re-handshaking sessions the responder had not yet confirmed, and the counted run's handshake took 180.9 s. Do not use a fixed `--stale-secs` instead: it leaves the retry at 5 s.

If the relay gets slower (more frames per cycle, a slower radio), measure the round trip from the `relay_cycle` events and raise `RELAY_RTT_MS` in the test with it.

**Level.** The TX gain is found as W1 does (probe, then set for a clear margin): frames well above threshold, because the point here is the link, not the modem's limits. Eb/N0 is measured from each recording as W1 does.

## 3. The suite: `l2-sdr-link`

`transmits = true`, `requires = ["b200", "e310"]`, wiring `usrp`, needs `OASA_SEC_BIN_DIR` and `OASA_RADIO_BIN_DIR`.

| Test (all in `expectations`) | Passes when |
|---|---|
| `test_encrypted_link_over_the_bench` | Handshake over RF; N marked messages A→B and M B→A; at least 90 % delivered, none twice, none that wasn't sent; the air capture holds no marker or plaintext |
| `test_untrusted_identity_gets_no_session` | A stranger (who knows Bob's key; Bob does not trust it) takes Alice's place: no session, `untrusted_peer` counted |
| `test_replayed_packet_is_dropped` | One carried frame sent again; `replay` counted; delivered once |
| `test_changed_packet_is_dropped` | One byte changed, frame CRC recomputed; `bad_tag` counted; never delivered |

The plaintext control is its own suite, **`l2-sdr-control`**: plaintext frames with a marker and MAVLink heartbeats over the same relay, in a capture declared `expect_plaintext`, which the checker must flag. (A capture is either encrypted or a control, never both.)

Alice sends first: per the link spec the responder sends on a new session only after the initiator's first data packet arrives (anything earlier is dropped as `no_session`).

**Limits** (`suite.toml`, est. until the first runs): delivery ≥ 90 % each way, handshake ≤ 240 s (one round trip expected; room for one lost handshake message), frames decoded ≥ 95 % of frames sent, probe Eb/N0 ≥ 8 dB.

**Starting the links.** The links' ports come from `free_udp_port`, which is free only until another program binds it. A link that cannot bind its port exits and sends nothing: the run then waits out the session time and ends with empty air captures, `no data packet was carried`, and only the stranger test passing (CI, 2026-11-05). The suite waits until each link has bound its sockets and starts it again on new ports after "Address already in use" (the failed try's log is kept as `link-NAME-clashN.jsonl`). If a link exits later, the first test stops at once and names it with its last output line.

**Evidence:** link logs (`link-*.jsonl`, contents never logged), the air capture, one SigMF recording per direction, the relay's per-cycle log (frames sent, found, CRC passes, Eb/N0), the oasa-sec and oasa-radio binaries' hashes.

**On the simulated bench** the same suite runs in CI: the simulated B200 and E310 already carry a transmitted block across the `usrp` paths with noise and the crystal offset.

## 4. Later: a real-time link

The relay proves the security properties but is slow (a round trip of tens of seconds). A link you could fly needs both radios streaming continuously: one process per radio that sends and receives at once, with `waveform rx-stream` decoding as samples arrive (already real time on the VM for one direction).

The UHD example tools cannot send from a stream and receive at the same time in one process. Ways forward, for the owner to choose:

1. A small transmit-and-receive helper linked against UHD, kept as a separate optional program like the UHD tools (it would be GPL-3.0; the rule today allows GPL tools only as separate processes, never shipped).
2. SoapySDR (Boost licence) with its UHD module (GPL-3.0): the same question.
3. A radio whose driver is permissive or LGPL (ADALM-Pluto via libiio, LGPL; bladeRF, LGPL/MIT parts), per the original R-COM2 radio list.

Until then the relay is the bench's encrypted-link test.

## 5. Order of work

1. Relay and suite on the simulated bench; CI.
2. First run on the bench through the job service; tune limits from it.
3. Record the result on oasa-radio#6 / the R-COM2 node; decide the real-time path (section 4).
