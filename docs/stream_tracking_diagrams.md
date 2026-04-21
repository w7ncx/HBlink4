# HBlink4 Stream Tracking Diagrams

This document provides visual representations of HBlink4's stream tracking system, including packet flows, contention scenarios, and hang time protection mechanisms.

## Table of Contents

1. [Packet Processing Flow](#packet-processing-flow)
2. [Stream State Machine](#stream-state-machine)
3. [Fast Terminator vs Timeout](#fast-terminator-vs-timeout)
4. [Hang Time Protection Scenarios](#hang-time-protection-scenarios)
5. [Partial TG Overlap (A-B-C Network)](#partial-tg-overlap-a-b-c-network)
6. [RX/TX Contention (Route-Cache Removal)](#rxtx-contention-route-cache-removal)
7. [Routing Cache Flow](#routing-cache-flow)

---

## Packet Processing Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                     DMRD Packet Received                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  Extract Fields  │
                    │  ─ repeater_id   │
                    │  ─ rf_src        │
                    │  ─ dst_id        │
                    │  ─ slot          │
                    │  ─ stream_id     │
                    │  ─ frame_type    │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │   Validate       │
                    │   Repeater       │
                    │   State          │
                    └────────┬─────────┘
                             │
                ┌────────────┴────────────┐
                │                         │
           Connected              Not Connected
                │                         │
                ▼                         ▼
    ┌──────────────────────┐    ┌──────────────────┐
    │ Get Current Stream   │    │   Send NAK       │
    │ for Slot             │    │   Drop Packet    │
    └──────────┬───────────┘    └──────────────────┘
               │
               ▼
    ┌─────────────────────┐
    │  Stream Exists?     │
    └──────────┬──────────┘
               │
       ┌───────┴──────────┐
       │                  │
     NO (idle)          YES
       │                  │
       ▼                  ▼
  ┌─────────────┐   ┌─────────────────────┐
  │   Start     │   │  stream_id match?   │
  │   New       │   └──────────┬──────────┘
  │  Stream     │              │
  └──────┬──────┘       ┌──────┴───────┐
         │             YES             NO
         │              │               │
         │              ▼               ▼
         │      ┌──────────────┐   ┌──────────────────┐
         │      │  Update      │   │  Check time since│
         │      │  Stream      │   │  last packet     │
         │      │  ─ last_seen │   └────────┬─────────┘
         │      │  ─ packet++  │            │
         │      └──────┬───────┘    ┌───────┴────────┐
         │             │         <200ms           >200ms
         │             │             │                │
         │             │             ▼                ▼
         │             │      ┌──────────────┐  ┌─────────────┐
         │             │      │ CONTENTION   │  │ Fast        │
         │             │      │ Deny packet  │  │ Terminator  │
         │             │      └──────────────┘  │ Detected    │
         │             │                        └──────┬──────┘
         │             │                               │
         └─────────────┴───────────────────────────────┘
                             │
                             ▼
                  ┌────────────────────┐
                  │ Check DMR          │
                  │ Terminator Frame   │
                  │ (byte 15 analysis) │
                  └─────────┬──────────┘
                            │
                    ┌───────┴────────┐
                    │                │
              Terminator         Normal
                    │                │
                    ▼                │
            ┌───────────────┐        │
            │ End Stream    │        │
            │ immediately   │        │
            │ Enter hang    │        │
            │ time          │        │
            └───────┬───────┘        │
                    │                │
                    └────────┬───────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Check Inbound        │
                  │ Routing (TG allowed?)│
                  └─────────┬────────────┘
                            │
                    ┌───────┴────────┐
                   YES              NO
                    │                │
                    ▼                ▼
          ┌──────────────────┐  ┌──────────┐
          │ Calculate        │  │  Drop    │
          │ Routing Targets  │  │  Packet  │
          │ (once per stream)│  └──────────┘
          └─────────┬────────┘
                    │
                    ▼
          ┌──────────────────┐
          │ Forward to       │
          │ Cached Targets   │
          │ (O(1) lookup)    │
          └──────────────────┘
```

---

## Stream State Machine

```
                         ┌─────────────────────────┐
                         │   Slot Available        │
                         │   (no active stream)    │
                         └───────────┬─────────────┘
                                     │
                         First Packet + TG Allowed
                                     │
                                     ▼
                         ┌─────────────────────────┐
                         │   Stream Active         │
                         │   (is_assumed=False     │
                         │    for RX streams)      │
                         │                         │
                         │  ─ Accepting packets    │
                         │  ─ Updating last_seen   │
                         │  ─ Counting packets     │
                         │  ─ Forwarding to cached │
                         │    targets (if RX)      │
                         └───────────┬─────────────┘
                                     │
         ┌───────────────────────────┼──────────────────────────┐
         │                           │                          │
    Same stream_id            Different stream_id          No packets
    (continue)                                             for 2 seconds
         │                           │                          │
         ▼                           ▼                          ▼
┌──────────────────┐      ┌─────────────────────┐    ┌─────────────────┐
│ Update State     │      │  Time Check:        │    │   Timeout       │
│ ─ last_seen      │      │  ─ <200ms: DENY     │    │   (fallback)    │
│ ─ packet_count++ │      │  ─ >200ms: Fast     │    │                 │
│ Keep Active      │      │    Terminator       │    │  Call _end_     │
└────────┬─────────┘      └──────────┬──────────┘    │  stream()       │
         │                           │                └────────┬────────┘
         │                           ▼                         │
         │                ┌──────────────────────┐            │
         │                │ End old stream       │            │
         │                │ Check hang time      │            │
         │                │ rules for new stream │            │
         │                └──────────┬───────────┘            │
         │                           │                        │
         │                           │                        │
         └───────────────────────────┴────────────────────────┘
                                     │
                                     ▼
                         ┌─────────────────────────┐
                         │   HANG TIME             │
                         │   (ended=True)          │
                         │                         │
                         │  Protection Rules:      │
                         │  ✓ Same rf_src: ALLOW   │
                         │  ✓ Diff src, same TG:   │
                         │    ALLOW                │
                         │  ✗ Diff src, diff TG:   │
                         │    DENY (hijacking)     │
                         └───────────┬─────────────┘
                                     │
                             hang_time expires
                             (10-20 seconds)
                                     │
                                     ▼
                         ┌─────────────────────────┐
                         │   Slot Available        │
                         │   (ready for new        │
                         │    transmission from    │
                         │    any source)          │
                         └─────────────────────────┘
```

---

## Fast Terminator vs Timeout

```
═══════════════════════════════════════════════════════════════════════════
                          TIMING COMPARISON
═══════════════════════════════════════════════════════════════════════════

Timeline (PTT released at t=0):

t=0ms     PTT Released by operator
          ├─ Terminator frame transmitted by radio
          │
t=60ms    ✅ TERMINATOR DETECTED (Primary Method)
          ├─ Server detects terminator frame (byte 15 analysis)
          ├─ stream.ended = True
          ├─ Enter hang time IMMEDIATELY
          ├─ Log: "RX stream ended ... entering hang time"
          └─ Slot reserved for original source
          
          [Slot available to same user or same TG within 60ms!]

═══════════════════════════════════════════════════════════════════════════

Fallback Scenario (terminator packet lost due to network issue):

t=0ms     PTT Released
          ├─ Terminator frame lost in network
          │
t=60ms    No packet received (terminator missing)
          │
t=120ms   Still waiting...
          │
t=180ms   Still waiting...
          │
t=200ms   🔶 FAST TERMINATOR (if new stream starts)
          ├─ New stream arrives with different stream_id
          ├─ Time since last packet > 200ms
          ├─ Assume old stream terminated
          ├─ stream.ended = True
          ├─ Enter hang time
          └─ Check if new stream allowed (hang time rules)
          
          [Only triggered if someone tries to use slot]

═══════════════════════════════════════════════════════════════════════════

t=2000ms  ⏱️  TIMEOUT (Last Resort)
          ├─ _check_stream_timeouts() periodic task
          ├─ No packet received for 2.0 seconds
          ├─ stream.ended = True
          ├─ Enter hang time
          └─ Log: "Stream timeout ... entering hang time"
          
          [Only used if terminator lost AND no new streams attempt slot]

═══════════════════════════════════════════════════════════════════════════

RESULT:  Primary method (terminator): ~60ms slot availability
         Fast terminator (contention): ~200ms
         Fallback (timeout): ~2000ms
         
HBlink4 uses ALL THREE methods for maximum reliability and performance!
═══════════════════════════════════════════════════════════════════════════
```

---

## Hang Time Protection Scenarios

### Scenario 1: Same User Continuing (ALLOWED)

```
┌─────────────────────────────────────────────────────────────────┐
│                     Timeline: Same User                         │
└─────────────────────────────────────────────────────────────────┘

t=0.0s   User 312123 transmits TG 3120
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312123         │
         │ ─ dst_id: 3120           │
         │ ─ State: ACTIVE          │
         └──────────────────────────┘

t=2.5s   User releases PTT (terminator detected)
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312123         │
         │ ─ dst_id: 3120           │
         │ ─ State: HANG TIME       │
         │ ─ Reserved for: 312123   │
         │ ─ Expires: t=12.5s       │
         └──────────────────────────┘

t=4.0s   SAME USER keys up again (TG 3120)
         ┌────────────────────────────────────────────┐
         │ Hang Time Check:                           │
         │  ─ rf_src match? 312123 == 312123  ✓ YES   │
         │  ─ Result: ✅ ALLOWED                      │
         │  ─ Reason: Same user continuing            │
         └────────────────────────────────────────────┘
         
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312123         │
         │ ─ dst_id: 3120           │
         │ ─ State: ACTIVE          │
         │ ─ New stream started     │
         └──────────────────────────┘

LOG: "Same user continuing conversation during hang time: src=312123, dst=3120"
```

### Scenario 2: Fast TG Switching (ALLOWED)

```
┌─────────────────────────────────────────────────────────────────┐
│              Timeline: User Switches Talkgroup                  │
└─────────────────────────────────────────────────────────────────┘

t=0.0s   User 312123 transmits TG 3120
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312123         │
         │ ─ dst_id: 3120           │
         │ ─ State: ACTIVE          │
         └──────────────────────────┘

t=2.5s   User releases PTT (terminator detected)
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ State: HANG TIME       │
         │ ─ Reserved for: 312123   │
         │ ─ TG: 3120               │
         └──────────────────────────┘

t=3.0s   SAME USER keys up on TG 9 (DIFFERENT TG!)
         ┌────────────────────────────────────────────┐
         │ Hang Time Check:                           │
         │  ─ rf_src match? 312123 == 312123  ✓ YES   │
         │  ─ dst_id match? 9 != 3120         ✗ NO    │
         │  ─ Result: ✅ ALLOWED                      │
         │  ─ Reason: Same user can switch TGs        │
         └────────────────────────────────────────────┘
         
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312123         │
         │ ─ dst_id: 9 (NEW!)       │
         │ ─ State: ACTIVE          │
         └──────────────────────────┘

LOG: "Same user switching talkgroup during hang time: 
      src=312123, old_dst=3120, new_dst=9"

BENEFIT: Operators can quickly switch TGs without waiting!
```

### Scenario 3: Multi-User Conversation (ALLOWED)

```
┌─────────────────────────────────────────────────────────────────┐
│        Timeline: Different User Joins Same TG Conversation      │
└─────────────────────────────────────────────────────────────────┘

t=0.0s   User 312123 transmits TG 3120
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312123         │
         │ ─ dst_id: 3120           │
         │ ─ State: ACTIVE          │
         └──────────────────────────┘

t=2.5s   User 312123 releases PTT (terminator detected)
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ State: HANG TIME       │
         │ ─ TG: 3120               │
         └──────────────────────────┘

t=4.0s   DIFFERENT USER 312456 keys up on TG 3120 (SAME TG!)
         ┌────────────────────────────────────────────┐
         │ Hang Time Check:                           │
         │  ─ rf_src match? 312456 != 312123  ✗ NO    │
         │  ─ dst_id match? 3120 == 3120      ✓ YES   │
         │  ─ Result: ✅ ALLOWED                      │
         │  ─ Reason: Joining same TG conversation    │
         └────────────────────────────────────────────┘
         
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312456 (NEW!)  │
         │ ─ dst_id: 3120 (SAME)    │
         │ ─ State: ACTIVE          │
         └──────────────────────────┘

LOG: "Different user joining conversation during hang time: 
      old_src=312123, new_src=312456, dst=3120"

BENEFIT: Natural roundtable conversations work seamlessly!
```

### Scenario 4: Hijacking Prevention (DENIED)

```
┌─────────────────────────────────────────────────────────────────┐
│          Timeline: Hijacking Attempt BLOCKED                    │
└─────────────────────────────────────────────────────────────────┘

t=0.0s   User 312123 transmits TG 3120
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ rf_src: 312123         │
         │ ─ dst_id: 3120           │
         │ ─ State: ACTIVE          │
         └──────────────────────────┘

t=2.5s   User 312123 releases PTT (terminator detected)
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ State: HANG TIME       │
         │ ─ Reserved for TG: 3120  │
         └──────────────────────────┘

t=4.0s   DIFFERENT USER 312456 tries TG 9 (DIFFERENT TG!)
         ┌────────────────────────────────────────────┐
         │ Hang Time Check:                           │
         │  ─ rf_src match? 312456 != 312123  ✗ NO    │
         │  ─ dst_id match? 9 != 3120         ✗ NO    │
         │  ─ Result: ❌ DENIED                       │
         │  ─ Reason: HIJACKING ATTEMPT               │
         └────────────────────────────────────────────┘
         
         ┌──────────────────────────┐
         │ Repeater Slot 1          │
         │ ─ State: HANG TIME       │
         │ ─ Packet DROPPED         │
         │ ─ Still reserved: 3120   │
         └──────────────────────────┘

LOG: "Hang time hijacking blocked: slot reserved for TG 3120, 
      denied src=312456 attempting TG 9"

⚠️  THIS IS THE KEY PROTECTION - prevents slot stealing!
```

---

## Partial TG Overlap (A-B-C Network)

### Network Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Network Configuration                        │
└─────────────────────────────────────────────────────────────────────┘

Repeater A              Repeater B              Repeater C
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│  TGs:        │        │  TGs:        │        │  TGs:        │
│  ─ 3120      │◄──────►│  ─ 3120      │        │  ─ 3121      │
│  ─ 9         │        │  ─ 3121      │◄──────►│  ─ 8         │
└──────────────┘        └──────────────┘        └──────────────┘

    Shares              Bridge Between             Shares
    3120 with B         A and C                    3121 with B

KEY: A and C do NOT share any talkgroups (isolated by B)
```

### Scenario Timeline

```
═══════════════════════════════════════════════════════════════════════════
t=0.0s: User on Repeater A transmits TG 3120
═══════════════════════════════════════════════════════════════════════════

Server Processing:
1. Receives DMRD from A (RX stream)
2. Calculates routing targets for TG 3120:
   ─ Repeater A: Source (skip)
   ─ Repeater B: Has TG 3120 ✓ INCLUDE
   ─ Repeater C: Does not have TG 3120 ✗ SKIP
3. Forwards to B (creates assumed TX stream)

┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│ Repeater A   │◄═══════│ HBlink4      │═══════►│ Repeater B   │
│ Slot 1       │  RX    │ Server       │  TX    │ Slot 1       │
│              │        │              │        │              │
│ 🔴 RX ACTIVE │        │ Routing:     │        │ 🟡 TX ASSUMED│
│ src: 312123  │        │ TG 3120      │        │ dst: 3120    │
│ dst: 3120    │        │ targets: {B} │        │ (forwarded   │
│ is_assumed:  │        │              │        │  from A)     │
│   False      │        │              │        │ is_assumed:  │
│              │        │              │        │   True       │
└──────────────┘        └──────────────┘        └──────────────┘

                                                 ┌──────────────┐
                                                 │ Repeater C   │
                                                 │ Slot 1       │
                                                 │              │
                                                 │ ⚫ IDLE      │
                                                 │ (no TG 3120) │
                                                 │              │
                                                 └──────────────┘

═══════════════════════════════════════════════════════════════════════════
t=2.5s: User on A releases PTT (terminator detected)
═══════════════════════════════════════════════════════════════════════════

┌──────────────┐                                ┌──────────────┐
│ Repeater A   │                                │ Repeater B   │
│ Slot 1       │                                │ Slot 1       │
│              │                                │              │
│ 🟠 HANG TIME │                                │ 🟠 HANG TIME │
│ Reserved for:│                                │ (assumed)    │
│  src=312123  │                                │ dst: 3120    │
│  TG=3120     │                                │              │
│ Expires: t=  │                                │              │
│  12.5s       │                                │              │
└──────────────┘                                └──────────────┘

                                                 ┌──────────────┐
                                                 │ Repeater C   │
                                                 │ Slot 1       │
                                                 │              │
                                                 │ ⚫ IDLE      │
                                                 │              │
                                                 └──────────────┘

═══════════════════════════════════════════════════════════════════════════
t=3.0s: User on Repeater C tries to transmit TG 3121
═══════════════════════════════════════════════════════════════════════════

Server Processing:
1. Receives DMRD from C (RX stream attempt)
2. Calculates routing targets for TG 3121:
   ─ Repeater A: Does not have TG 3121 ✗ SKIP
   ─ Repeater B: Has TG 3121 ✓ CHECK SLOT
   ─ Repeater C: Source (skip)
3. Checks B slot 1 availability:
   ─ Current: Assumed stream (hang time)
   ─ Priority: Real RX > Assumed TX
   ─ Decision: Clear B's assumed stream, allow C's real RX

┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│ Repeater A   │        │ HBlink4      │◄═══════│ Repeater C   │
│ Slot 1       │        │ Server       │  RX    │ Slot 1       │
│              │        │              │        │              │
│ 🟠 HANG TIME │        │ Processing:  │        │ 🔴 RX ACTIVE │
│ Reserved for │        │              │        │ src: 312789  │
│  TG 3120     │        │ B's assumed  │        │ dst: 3121    │
│              │        │ stream       │        │ is_assumed:  │
│ (still       │        │ CLEARED!     │        │   False      │
│  reserved)   │        │              │        │              │
│              │        │ Real RX wins!│        │              │
└──────────────┘        └──────────────┘        └──────────────┘
                                │
                                │ TX (forward)
                                ▼
                        ┌──────────────┐
                        │ Repeater B   │
                        │ Slot 1       │
                        │              │
                        │ 🔴 RX FROM C │
                        │ dst: 3121    │
                        │ (NEW TG!)    │
                        │              │
                        └──────────────┘

Result: ✅ C successfully uses B slot 1 because:
        1. A and C don't share TGs (isolated)
        2. B's slot 1 only had assumed stream (low priority)
        3. Real RX always wins over assumed TX
        4. A's hang time protects A's slot (not B's)

LOG: "Repeater 312102 slot 1 starting RX while we have active assumed 
      TX stream - repeater wins, removing from active route-caches"
     "RX stream started on repeater 312102 slot 1: src=312789, 
      dst=3121, targets=0"
```

---

## RX/TX Contention (Route-Cache Removal)

### The Problem: Wasted Bandwidth

```
Before Route-Cache Removal:
═══════════════════════════════════════════════════════════════════════════

Repeater A                                          Repeater B
(Local User)                                        (Local User)
┌──────────────┐                                    ┌──────────────┐
│ User keys up │                                    │ User keys up │
│ TG 1         │                                    │ TG 2         │
└──────┬───────┘                                    └──────┬───────┘
       │                                                   │
       │ RX                                                │ RX
       ▼                                                   ▼
┌──────────────┐        TX (forwarding)             ┌──────────────┐
│ Repeater A   │═══════════════════════════════════►│ Repeater B   │
│              │   ❌ WASTED BANDWIDTH!             │              │
│ 🔴 RX ACTIVE │   B can't receive (hardware busy)  │ 🔴 RX ACTIVE │
│ from local   │                                    │ from local   │
│ user         │                                    │ user         │
└──────────────┘                                    └──────────────┘

Problem: Server continues sending to B even though B is busy receiving!
Result: Wasted network bandwidth (could be 100s of KB/s on busy network)
```

### The Solution: Automatic Route-Cache Removal

```
After Route-Cache Removal:
═══════════════════════════════════════════════════════════════════════════

Step 1: Detection
─────────────────
Repeater B starts RX while server has assumed TX stream to B

┌───────────────────────────────────────────────────────────────┐
│ _handle_stream_start() on Repeater B Slot 1:                  │
│                                                               │
│ 1. Current stream exists? YES (assumed stream)                │
│ 2. Same stream_id? NO (new local transmission)                │
│ 3. Is current stream assumed? YES (is_assumed=True)           │
│                                                               │
│ ▶ Decision: REPEATER WINS (real RX > assumed TX)              │
└───────────────────────────────────────────────────────────────┘


Step 2: Route-Cache Removal
────────────────────────────
Remove B from ALL active streams' target sets

┌───────────────────────────────────────────────────────────────┐
│ For each repeater in self._repeaters:                         │
│   For each slot (1, 2):                                       │
│     stream = repeater.get_slot_stream(slot)                   │
│     If stream and stream.routing_cached:                      │
│       If B in stream.target_repeaters:                        │
│         ─ stream.target_repeaters.discard(B)                  │
│         ─ LOG: "Removed B from route-cache"                   │
│         ─ ✅ Stop sending to B immediately!                   │
└───────────────────────────────────────────────────────────────┘


Step 3: New State
─────────────────
Repeater A             HBlink4 Server                Repeater B
┌──────────────┐       ┌──────────────┐             ┌──────────────┐
│              │       │ Route cache  │             │              │
│ 🔴 RX ACTIVE │       │ for stream   │             │ �� RX ACTIVE │
│ TG 1         │       │ from A:      │             │ TG 2         │
│              │       │              │             │              │
│              │       │ targets: {}  │             │              │
│              │◄──────│ (empty now!) │             │              │
│              │  RX   │              │  No TX!     │              │
│              │       │ ✅ Bandwidth │────────────►│              │
│              │       │    saved!    │             │              │
└──────────────┘       └──────────────┘             └──────────────┘

Result: ✅ No more packets sent to B (bandwidth saved)
        ✅ B processes its local RX normally
        ✅ Automatic optimization (no configuration needed)
```

### Performance Analysis

```
Complexity: O(R×S) where R = repeaters, S = slots

Example Network:
─ 25 repeaters
─ 2 slots each
─ Total checks: 25 × 2 = 50

Operations per check:
─ Get stream: O(1)
─ Check routing_cached: O(1)
─ Set membership test: O(1)
─ Set discard: O(1)

If 5 streams have this repeater in route-cache:
─ 5 × O(1) discard operations
─ Total: ~50 checks + 5 removals = 55 operations
─ Time: < 1ms (typically ~0.1ms)

Frequency: Only when RX/TX contention detected (rare event)

Bandwidth Saved:
─ Typical DMR packet: ~60 bytes
─ Packet rate: ~17 packets/second (voice)
─ Per repeater: ~1 KB/s
─ 10 busy repeaters: ~10 KB/s saved
─ Large network (50 repeaters): ~50 KB/s saved
```

---

## Routing Cache Flow

### Calculate-Once, Forward-Many

```
═══════════════════════════════════════════════════════════════════════════
                            AT STREAM START
═══════════════════════════════════════════════════════════════════════════

┌──────────────────────────────────────────────────────────────────┐
│ _calculate_stream_targets(source_repeater, slot, dst_id, ...)    │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
           ┌─────────────────────────────────┐
           │ Extract talkgroup from dst_id   │
           │ tgid = int.from_bytes(dst_id)   │
           └────────────┬────────────────────┘
                        │
                        ▼
           ┌──────────────────────────────────────────┐
           │ For each connected repeater:             │
           │   (skip source repeater)                 │
           │                                          │
           │   1. Check outbound routing:             │
           │      ─ Does repeater have this TG?       │
           │      ─ _check_outbound_routing()         │
           │      ─ O(1) set membership test          │
           │                                          │
           │   2. Check slot availability:            │
           │      ─ Is slot idle?           ✅        │
           │      ─ Assumed stream only?    ✅        │
           │      ─ Real RX active?         ❌        │
           │      ─ Real RX in hang time?   ❌        │
           │                                          │
           │   3. If both checks pass:                │
           │      ─ Add to target_repeaters set       │
           │      ─ O(1) set insertion                │
           └────────────┬─────────────────────────────┘
                        │
                        ▼
           ┌──────────────────────────────────────────┐
           │ Store in StreamState:                    │
           │  stream.target_repeaters = {B, C, D}     │
           │  stream.routing_cached = True            │
           │                                          │
           │ ✅ Routing calculated ONCE!              │
           └──────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════
                         FOR EVERY PACKET
═══════════════════════════════════════════════════════════════════════════

┌──────────────────────────────────────────────────────────────────┐
│ _forward_stream(data, source_repeater, ...)                      │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
           ┌─────────────────────────────────┐
           │ Get source stream:              │
           │ stream = source.get_slot_stream │
           └────────────┬────────────────────┘
                        │
                        ▼
           ┌──────────────────────────────────────────┐
           │ stream.routing_cached == True?           │
           └────────────┬─────────────────────────────┘
                        │ YES
                        ▼
           ┌──────────────────────────────────────────┐
           │ Use cached targets:                      │
           │  for target_id in stream.target_repeaters│
           │                                          │
           │    ─ Get target repeater: O(1) dict      │
           │    ─ Get target sockaddr: O(1)           │
           │    ─ Send packet: self._port.write()     │
           │                                          │
           │ ✅ NO routing calculation needed!        │
           │ ✅ O(1) per target                       │
           │ ✅ Same route for entire stream          │
           └──────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════
                            BENEFITS
═══════════════════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────┐
│ ✅ Performance:                                                 │
│   ─ Per-packet forwarding: O(T) where T = targets (~3-10)       │
│   ─ No per-packet TG checks                                     │
│   ─ No per-packet slot checks                                   │
│   ─ Sub-microsecond per packet                                  │
│                                                                 │
│ ✅ Consistency:                                                 │
│   ─ All packets follow same route                               │
│   ─ No mid-stream routing changes                               │
│   ─ Predictable behavior                                        │
│                                                                 │
│ ✅ Scalability:                                                 │
│   ─ 100+ repeaters: same O(1) per-packet cost                   │
│   ─ Multiple simultaneous streams: independent caches           │
│   ─ Linear scaling with network size                            │
│                                                                 │
│ ✅ Bandwidth Optimization:                                      │
│   ─ Route-cache removal when RX/TX contention detected          │
│   ─ Automatic, no configuration needed                          │
│   ─ Significant savings on large networks                       │
└─────────────────────────────────────────────────────────────────┘
```

### Routing Decision Tree

```
                      ┌───────────────────┐
                      │ Calculate Targets │
                      │ for Stream        │
                      └─────────┬─────────┘
                                │
                                ▼
                ┌───────────────────────────────┐
                │ For each potential target:    │
                └───────┬───────────────────────┘
                        │
            ┌───────────┴────────────┐
            │                        │
            ▼                        ▼
  ┌──────────────────┐    ┌──────────────────┐
  │ Outbound Routing │    │ Slot Availability│
  │ Check            │    │ Check            │
  └────────┬─────────┘    └────────┬─────────┘
           │                       │
           │                       │
    ┌──────┴──────┐         ┌──────┴──────┐
    │             │         │             │
  PASS          FAIL      PASS          FAIL
    │             │         │             │
    │             └─────────┴─────────────┘
    │                       │              │
    │                       │              ▼
    │                       │        ┌──────────┐
    │                       │        │ EXCLUDE  │
    │                       │        │ from     │
    │                       │        │ targets  │
    │                       │        └──────────┘
    │                       ▼
    └─────────────►┌──────────────────┐
                   │ INCLUDE in       │
                   │ target_repeaters │
                   │ set              │
                   └──────────────────┘

Outbound Routing Check:
─ TG in repeater's allowed list?
─ Implementation: O(1) set membership
─ Example: if 3120 in {3120, 3121, 3122}

Slot Availability Check:
─ Slot idle? ✅
─ Assumed stream only? ✅ (can override)
─ Real RX active? ❌
─ Real RX in hang time? ❌ (unless hang time rules allow)
```

---

## Multi-Slot Operation

```
┌─────────────────────────────────────────────────────────────────┐
│                    Repeater State                               │
│                   (Independent Slots)                           │
└─────────────────────────────────────────────────────────────────┘

                   Repeater 312100
                 (192.168.1.100:62031)
                         │
         ┌───────────────┼───────────────┐
         │                               │
         ▼                               ▼
  ┌────────────────┐             ┌────────────────┐
  │   Slot 1       │             │   Slot 2       │
  │                │             │                │
  │ slot1_stream:  │             │ slot2_stream:  │
  │                │             │                │
  │ StreamState:   │             │ StreamState:   │
  │ ─ rf_src:      │             │ ─ rf_src:      │
  │   312123       │             │   312456       │
  │ ─ dst_id: 3120 │             │ ─ dst_id: 3121 │
  │ ─ stream_id:   │             │ ─ stream_id:   │
  │   AAAA...      │             │   BBBB...      │
  │ ─ packets: 145 │             │ ─ packets: 89  │
  │ ─ targets: 3   │             │ ─ targets: 5   │
  │ ─ is_assumed:  │             │ ─ is_assumed:  │
  │   False (RX)   │             │   False (RX)   │
  └────────────────┘             └────────────────┘

Key Points:
─ Completely independent operation
─ Different streams can run simultaneously
─ Different target sets per stream
─ Different TGs per slot
─ Separate hang time per slot
─ Independent contention handling
```

---

## Summary: Why HBlink4's Stream Tracking Excels

```
┌─────────────────────────────────────────────────────────────────┐
│                    Feature Comparison                           │
├─────────────────────────┬───────────────────┬───────────────────┤
│ Feature                 │ HBlink4           │ Other versions    │
├─────────────────────────┼───────────────────┼───────────────────┤
│ Stream End Detection    │ ~60ms (primary)   │ ~2000ms (timeout) │
│                         │ +200ms (fast term)│  only             │
│                         │ +2000ms (fallback)│                   │
├─────────────────────────┼───────────────────┼───────────────────┤
│ Hang Time Protection    │ 4 distinct rules  │ Simple timeout or │
│                         │ ─ Same user       │  no protection    │
│                         │ ─ User switch TG  │                   │
│                         │ ─ Join conv       │                   │
│                         │ ─ Hijack prevent  │                   │
├─────────────────────────┼───────────────────┼───────────────────┤
│ Routing Performance     │ O(1) per packet   │ O(n) per packet   │
│                         │ Calculate once    │ Recalculate every │
│                         │                   │  packet           │
├─────────────────────────┼───────────────────┼───────────────────┤
│ RX/TX Contention        │ Automatic route-  │ Continue sending  │
│                         │  cache removal    │  to busy repeaters│
│                         │ Bandwidth saved   │ Wasted bandwidth  │
├─────────────────────────┼───────────────────┼───────────────────┤
│ Real vs Assumed         │ Explicit tracking │May not distinguish│
│                         │ Priority system   │  or handle poorly │
├─────────────────────────┼───────────────────┼───────────────────┤
│ Scalability             │ 100+ repeaters    │ Degrades with     │
│                         │ Sub-ms per packet │  network size     │
└─────────────────────────┴───────────────────┴───────────────────┘

Result: HBlink4 can scale to large networks while maintaining excellent
        performance and correct DMR behavior in complex scenarios.
```

---

**Document Version**: 2.0  
**Last Updated**: October 2025  
**Corresponding Code**: HBlink4 v4.5+
