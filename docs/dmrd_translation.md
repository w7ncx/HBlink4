# DMRD Translation

HBlink4 can translate DMRD packets between a repeater's **local** addressing and the network's addressing. The repeater declares its translation rules in its RPTO (OPTIONS) packet; HBlink4 applies them automatically in both directions.

Use it when the address space a repeater's operators want to see on their radios doesn't match the address space the wider network uses — without renumbering either side, and without deploying per-site bridge tools.

This document covers:

1. [When to use translation](#when-to-use-translation)
2. [Requirements](#requirements)
3. [RPTO grammar](#rpto-grammar)
4. [How it works](#how-it-works)
5. [Use cases](#use-cases)
6. [Outbound rf_src override (`SRC=`)](#outbound-rf_src-override-src)
7. [Collisions and specificity](#collisions-and-specificity)
8. [Operational notes](#operational-notes)
9. [Limitations](#limitations)

---

## When to use translation

Typical scenarios:

- **Slot swap**: a site's users are trained to use TS2 for a particular talkgroup, but the rest of the network runs that TG on TS1.
- **Local renumbering**: a site uses a short local TG number (e.g. TG9) that collides with a standard wide-area TG, so locally the site wants to see it as a different ID (e.g. TG32).
- **Range bridging**: a block of network TGIDs (say 3000-3200) is available at a site only on its less-busy slot.
- **Courtesy aliasing**: a bridged or legacy site keeps its historic local TGIDs while joining a new network numbering plan.

If none of these apply — i.e. the repeater already speaks the network's addressing — don't declare any translation rules. The packet forwarding path has a zero-cost fast path when a repeater has no map.

---

## Requirements

- The repeater must be **trusted** (`"trust": true` in its HBlink4 config). Untrusted repeaters have their RPTO translation syntax parsed and silently discarded with a warning, and only the legacy TG subscription is honored.
- The repeater must send an **RPTO packet** containing the extended syntax below. Most MMDVMHost-family stacks let operators set this string via the `Options=` configuration key.
- Group voice only. Unit (private) calls are rejected upstream by HBlink4 today and the `SRC=` override is explicitly gated on group voice.

---

## RPTO grammar

```
Options = directive[;directive...]

directive = TS1 = entry[,entry...]      ; subscriptions/translations on NETWORK TS1
          | TS2 = entry[,entry...]      ; subscriptions/translations on NETWORK TS2
          | SRC = radio_id              ; outbound rf_src override (group voice)

entry          = net_tgid_spec [ : local_slot [ : local_tgid ] ]
net_tgid_spec  = N         ; exact tgid (specificity 3)
               | N-M       ; inclusive range, expanded at parse time (specificity 2)
local_slot     = 1 | 2 | *  ; * = preserve the network slot
local_tgid     = N | *      ; * = preserve the matched network tgid
```

Rules:

- The key (`TS1`, `TS2`) always names the **network** slot. Local slot is named *only* in the optional `:local_slot` clause.
- An entry without a colon is a pure subscription — the repeater subscribes to that network tgid but there is no translation. Same as legacy RPTO.
- `local_slot` must be `1`, `2`, or `*`. `3` or higher is rejected.
- `local_tgid` is a decimal TGID or `*` (preserve).
- Ranges are **expanded** into individual map entries at parse time; no runtime pattern matching. Maximum 10,000 tgids per range.
- Wildcards are **not supported on the network side**. `*` as `net_tgid_spec` is rejected when combined with a remap, as is any `N*` prefix.
- Whitespace is tolerated around separators.

---

## How it works

A DMRD packet carries `(rf_src, dst_id, slot, rf_src, payload)` in its 20-byte HBP header plus a 33-byte DMR payload. Translation rewrites only the header — the payload is either left alone (voice frames) or zeroed (data-sync frames, see below).

Each trusted repeater that declared translation rules gets two lookup tables (built from its RPTO):

- `inbound_map: (local_slot, local_tgid) → (network_slot, network_tgid)`
- `outbound_map: (network_slot, network_tgid) → (local_slot, local_tgid)`

The two tables are inverses. A single entry like `TS1=9:2:32` builds:

- `inbound_map[(2, TG32)] = (1, TG9)`
- `outbound_map[(1, TG9)] = (2, TG32)`

At runtime:

1. **Repeater → network (inbound)**. When the repeater transmits on its local TS2/TG32, HBlink4 looks up `(2, TG32)` in `inbound_map`, gets `(1, TG9)`, and the packet is treated everywhere downstream (ACL check, target selection, StreamState) as though it arrived on network TS1/TG9.
2. **Network → repeater (outbound)**. For every packet being forwarded **to** this repeater, HBlink4 looks up the packet's network `(slot, dst_id)` in `outbound_map`; if found, it rewrites the DMRD header to the local values so the repeater receives traffic on its local TS2/TG32.
3. If a lookup misses (no entry for that key), the packet is left untranslated. This is the correct behavior for talkgroups that aren't part of any remap.

### Processing order

Translation is **the first thing that happens on ingress and the last thing that happens on egress**. Everything else — ACL, contention, hang time, stream tracking, routing — runs in network-side vocabulary. This keeps every downstream check speaking one language regardless of which repeater sent the packet or which repeater it's going to.

**Ingress (packet arrives from a repeater), in order:**

1. Packet enters with source-local `(slot, dst_id)`.
2. **Translation applied**: `inbound_map` rewrites `(slot, dst_id)` to net values. No map = passthrough. If the key misses `inbound_map` but *is* in `outbound_map`, the repeater keyed the net-side address for a TG it already declared a local alias for — the packet is rejected (see [Net-side addressing is rejected](#net-side-addressing-is-rejected) below).
3. Contention / hang time / hijack checks run against the source's stored stream state. *(StreamState still stores source-local values so same-user comparisons work regardless of translation.)*
4. Inbound ACL checked against the repeater's subscription set (network vocabulary).
5. Stream targets calculated; per-target `outbound_map` applied at packet-rewrite time.

**Egress (packet leaves to a repeater), in order:**

1. All routing/ACL/contention decisions have already been made in network vocabulary.
2. Per-target outbound ACL checked (network vocabulary).
3. Per-target slot-busy check runs against the *target-local* slot the packet will actually occupy, using the target's `outbound_map` to translate net → target-local first.
4. **Translation applied**: `outbound_map` rewrites the DMRD header to the target's local `(slot, dst_id)`. No map = passthrough.
5. Optional `rf_src` override (`SRC=`) from the source repeater rewrites bytes 5–7.
6. Packet sent on the wire.

The practical upshot: when you configure subscriptions, contention rules, or ACLs, think in **network vocabulary**. The translation layer makes sure whatever arrives (or leaves) lines up with what the rest of the server expects.

### Net-side addressing is rejected

Once a repeater declares a translation for a `(net_slot, net_tgid)`, that pair becomes a **network-only** key — the repeater must key its local `(local_slot, local_tgid)` instead. A packet arriving on the declared net-side key is rejected.

This is needed because the subscription set still carries the net-side TGID (it has to — that's how traffic gets forwarded *to* this repeater). Without the guard, a packet that arrived on the net-side address would miss `inbound_map`, fall through untranslated, and then pass ACL because the net-side TGID is in the subscription set — silently bypassing the operator's declared local vocabulary.

Example with `Options="TS2=3120:1:9"`:

- Keying local TS1/TG9 → translated to net TS2/TG3120, forwarded. ✅
- Keying net TS2/TG3120 → rejected. ❌

Denial log line:

```
Inbound rejected: repeater=3100001 keyed net-side TS2/TG3120 for a translated TG — local side is TS1/TG9
```

If the repeater legitimately wants the same `(slot, tgid)` pair on both sides (identity map), declare it explicitly — e.g., `TS1=3000-3200:2:*,3120:1:3120` keeps TG3120 on TS1 for both sides while mapping the rest of the range to TS2. Identity-mapped entries land in both `inbound_map` and `outbound_map` and are translated (to themselves) rather than rejected.

### Payload blanking

DMR packets with `frame_type == 2` carry data-sync overhead: LC (link control) headers, terminators, and CSBKs. MMDVMHost reconstructs sync patterns, EMB, slot type, and LC overhead from the DMRD header *unless* its BPTC decode of the payload succeeds, in which case the decoded LC values override the header.

For any packet HBlink4 rewrites (translated repeater target, or whenever the source declared translation), data-sync frames have **bytes 20–52 zeroed** on the way out. That forces MMDVMHost's BPTC decode to fail, causing it to fall back to the DMRD header values we just rewrote. Voice frames (`frame_type` 0 or 1) are left intact — the AMBE vocoder bits live in the payload and MMDVMHost regenerates the voice-frame overhead from scratch.

This is the mechanism that makes translation work over-the-air. The server never decodes or re-encodes DMR — it just rewrites header fields and lets MMDVMHost rebuild the rest.

---

## Use cases

### 1. Slot swap (same talkgroup, different slot)

Local site wants TG9 on TS2; the network runs TG9 on TS1.

```
Options = "TS1=9:2:9"
```

- User transmits on local TS2/TG9 → rest of network sees net TS1/TG9.
- Anyone on net TS1/TG9 is heard on this repeater's local TS2/TG9.

Note that this site's TS1 remains available for other traffic. If the site wanted the inverse slot (everyone else's TS2 landing on its TS1), just flip the direction: `TS2=9:1:9`.

### 2. Local renumbering

Network uses TG9 for a wide-area chat; this site wants to see it as their own local TG32.

```
Options = "TS1=9:*:32"
```

- Local TS1/TG32 → net TS1/TG9.
- Net TS1/TG9 → local TS1/TG32.
- `*` in the slot position means "same as the network slot" — no physical-slot change, only the tgid is renumbered.

### 3. Range bridging

Site wants the whole TG3000-3200 range on its quieter TS2, preserving tgids.

```
Options = "TS1=3000-3200:2:*"
```

This expands to 201 individual map entries at parse time. Each `net TS1/TG3xxx ↔ local TS2/TG3xxx`.

Want a specific tgid in that range to stay on TS1 instead?

```
Options = "TS1=3000-3200:2:*,3120:1:3120"
```

The exact entry for TG3120 (specificity 3) beats the range entry for TG3120 (specificity 2). The range-derived entry for TG3120 is dropped with a warning; the other 200 tgids in the range still route via TS2.

### 4. Preserving legacy numbering on a bridged site

A site has historically used TG22, TG44, TG66 for local chatter on TS2. They're joining a network where those slots carry entirely different things, but management wants the network's TG9 chat available on that site without users having to relearn programming.

```
Options = "TS2=22,44,66;TS1=9:2:99"
```

- TS2 keeps subscribing to TG22/44/66 for legacy use (no remap).
- Net TS1/TG9 is delivered to the site on TS2/TG99 (an intentionally distinct local ID so it doesn't collide with the legacy three).
- Users program their radios for TG22/44/66/99 on TS2 and never see TS1.

### 5. "Site radio" appearance (with `SRC=`)

See [Outbound rf_src override](#outbound-rf_src-override-src) below.

---

## Outbound rf_src override (`SRC=`)

`SRC=radio_id` rewrites the `rf_src` field (bytes 5–7 of the DMRD header) on **every group-voice packet leaving this repeater**, replacing whatever subscriber ID was on the packet with the declared ID.

Properties:

- **One-way.** There is no reverse mapping. Group calls are addressed by destination (the TG) and carry no return-address semantics — nothing on the network needs to reach the original user by source ID, so the override does not need to be undone on return traffic.
- **Group voice only.** Private (unit) calls are rejected upstream today; the override has a defensive group-only gate regardless.
- **Trust-gated.** Like translation, only honored when `trust: true`.
- Applies to all forwarding destinations: other local repeaters and outbound network connections.

Example — make everything from a gateway appear as the site's repeater ID:

```
Options = "TS1=*;TS2=*;SRC=3100001"
```

Useful when:

- A site wants to maintain operator privacy by presenting a single "site radio" to the wider network.
- Downstream dashboards / lastheard systems are expected to index on the repeater rather than per-user.
- Legacy systems upstream have ID filtering that's simpler to manage against a fixed ID.

Do not use `SRC=` when downstream services depend on per-user identity (talker alias displays, per-subscriber ACLs, activity logs by operator). Everything upstream will see one ID.

---

## Collisions and specificity

Each entry has a **specificity score**:

| Entry kind | Example | Specificity |
|-----------|---------|-------------|
| Exact tgid | `9:2:32` | 3 |
| Range | `3000-3200:2:*` | 2 |
| Bare wildcard | `*` (not allowed with remap) | 0 |

When map construction sees two entries competing for the same `(local_slot, local_tgid)` **or** the same `(net_slot, net_tgid)`, the **higher specificity wins**; the lower-specificity entry is dropped with a warning. Equal specificity: first-declared wins, loser warned.

Examples:

- `TS1=3000-3200:2:*,3120:1:3120` — exact TG3120 claims `(1, 3120)` on the net side first; the range-derived `(1, 3120)→(2, 3120)` is dropped. Intended outcome.
- `TS1=9:2:32,10:2:32` — both entries want to deliver to **local TS2/TG32**. Second entry is dropped. Operator needs to rethink the rule.
- `TS1=9:2:9,TS2=9:1:9` with both TGIDs on their own repeater — the inbound_map entries `(2, 9)→(1, 9)` and `(1, 9)→(2, 9)` coexist fine; they are different local keys.

---

## Operational notes

### Logging

On RPTO receipt, HBlink4 logs a summary when translation is active:

```
📋 OPTIONS from 3100001 (W0XYZ): TS1=9:2:32;TS2=3000-3200:1:*;SRC=9990001
  → TS1 TGs: [9]
  → TS2 TGs: [3000..3200]
  → Translation rules: 202 active
      local TS1/TG3000 ↔ net TS2/TG3000
      local TS1/TG3001 ↔ net TS2/TG3001
      ...
      local TS2/TG32   ↔ net TS1/TG9
  → Outbound rf_src override: 9990001 (group voice only)
```

Collisions appear as warnings during the same block.

### Mid-stream RPTO

If an RPTO arrives while this repeater has an active (non-ended) stream on either slot, HBlink4 logs:

```
⚠️  RPTO received during active stream on repeater 3100001 — translation rules updated, takes effect on next stream
```

The new rules are installed immediately. The in-flight stream keeps flowing on whatever routing decisions were cached at its start (stream targets are calculated once per stream), and the in-flight stream will finish within a few seconds.

### Hot-path cost

- A repeater with **no** translation declared pays zero overhead — the `inbound_map` / `outbound_map` lookups short-circuit on empty dicts.
- With translation: each decision point is an O(1) dict lookup and, at most, a 53-byte packet `bytearray` clone + slice assignments.
- No runtime pattern matching, no regex, no range-walk: everything is pre-expanded into concrete keys at RPTO parse time.

### Interaction with talkgroup subscription (`slot1_talkgroups` / `slot2_talkgroups`)

The subscription sets are always in **network-side vocabulary**. After translation, HBlink4 performs the ACL check against the network-side `(slot, tgid)`. You declare network tgids in RPTO's `TS1=` / `TS2=` keys, and those same network tgids land in the repeater's subscription set.

---

## Limitations

- **No network-side wildcards.** Declare ranges explicitly (`N-M`) — no `*`, no `N*` prefix. This is a deliberate design choice to keep the hot path O(1) and forwarding cost bounded.
- **Range cap: 10,000 tgids.** Larger ranges are rejected at parse time with an error. Split into multiple ranges if you truly need a wider span.
- **Unit (private) calls not supported.** They are rejected by HBlink4's stream handler before translation would apply.
- **`SRC=` is one-way.** By design. Group destinations have no return-address semantics. Private-call NAT would require bidirectional state and is not implemented.
- **Translation is per-repeater, declared via RPTO.** There is no server-side way to force a translation on a repeater that doesn't declare it. If you want that, you control both sides anyway — declare it in the repeater's MMDVMHost `Options=` string.

---

## See also

- [configuration.md](configuration.md) — trust flag, talkgroup configuration, connecting repeaters
- [routing.md](routing.md) — general routing behavior
- [protocol.md](protocol.md) — HomeBrew protocol packet layout
