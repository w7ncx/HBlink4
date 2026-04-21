"""
DMR Link Control (LC) encode/splice helpers for the forwarding hot path.

When translation rewrites outbound addressing (TGID/slot/rf_src), the DMRD
header alone is not enough — MMDVMHost and end-radios also decode LC
embedded in the 33-byte DMR payload, and stale LC in that payload will
eventually contradict the rewritten header and wedge the stream.

Two LC carriers live in the payload:

  * FULL LC in Voice Header (VHEAD) / Voice Terminator (VTERM) data-sync
    frames — 196-bit BPTC(196,96) codeword spanning bits [0:98] and
    [166:264] of the payload; the 68-bit slot-type/sync pattern at
    [98:166] is preserved untouched (it carries colour-code + data-type,
    neither of which change under translation).
  * EMBEDDED LC fragment in voice superframe bursts B/C/D/E (dtype_vseq
    1..4 on voice frames) — 32 bits at [116:148]. A radio reassembles
    four consecutive fragments into the full LC for late entry + display.

Other payload slices (voice burst A, CSBKs, data headers) carry no LC,
so this module only touches the frames listed above. All FEC/interleave
math is delegated to dmr_utils3.

Hot path: encoding is done once per stream per target-addressing tuple
(see StreamState.lc_cache); splicing is per-packet but does nothing more
than a bitarray slice-concat-frombytes round trip.
"""

from typing import Dict, Optional, Tuple

from bitarray import bitarray
from dmr_utils3 import bptc, decode


# Default LC option bytes (FLCO=0 Group Voice, FID=0, service options=0x20).
# Matches hblink3's LC_OPT. Used when we can't snapshot the originator's LC
# from a VHEAD frame — e.g. late-entry streams where we see bursts before
# any data-sync frame, or recovery after a lost VHEAD.
LC_OPT_GROUP_DEFAULT: bytes = b'\x00\x00\x20'

# Full DMR payload is 264 bits (33 bytes). LC windows within it:
_PAYLOAD_BITS = 264
_FULL_LC_LOW = slice(0, 98)      # bits [0:98] — first half of 196-bit BPTC LC
_FULL_LC_SYNC = slice(98, 166)   # bits [98:166] — slot-type + sync pattern (preserved)
_FULL_LC_HIGH = slice(166, 264)  # bits [166:264] — second half of BPTC LC
_EMB_LC_WINDOW = slice(116, 148) # bits [116:148] — 32-bit EMB_LC fragment slot

# bptc.encode_header_lc / encode_terminator_lc produce 196-bit codewords.
# bptc.encode_emblc produces {1,2,3,4} → 32-bit bitarrays for bursts B..E.
_FULL_LC_BITS = 196

# Type aliases
FullLC = bitarray               # 196 bits
EmbLCSet = Dict[int, bitarray]  # {1,2,3,4} → 32-bit bitarray


def build_lc(opts: bytes, dst: bytes, src: bytes) -> bytes:
    """Assemble a 9-byte LC from 3-byte opts, dst, src.

    The LC wire format is: [3B FLCO/FID/service-options] [3B dst] [3B src].
    """
    return opts + dst + src


def synth_lc_base(dst: bytes, src: bytes,
                  opts: bytes = LC_OPT_GROUP_DEFAULT) -> bytes:
    """Build a synthesized 9-byte group-voice LC for a stream.

    Used when we can't decode the originator's VHEAD (late entry, missing
    header, private/unit call not yet supported). `opts` default gives
    FLCO=Group-Voice which is the only call type we currently forward.
    """
    return build_lc(opts, dst, src)


def decode_lc_from_vhead(payload: bytes) -> Optional[bytes]:
    """Extract the 9-byte LC from a Voice Header (VHEAD) payload.

    Called once per stream on a VHEAD frame so we can preserve the
    originator's FLCO / FID / service-option bits when we re-encode for
    translated targets. Returns None on any decode failure — callers fall
    back to a synthesized group-voice LC.
    """
    if len(payload) < 33:
        return None
    try:
        decoded = decode.voice_head_term(payload)
    except Exception:
        return None
    lc = decoded.get('LC') if decoded else None
    if isinstance(lc, (bytes, bytearray)) and len(lc) >= 9:
        return bytes(lc[:9])
    return None


def encode_lc_forms(lc: bytes) -> Tuple[FullLC, FullLC, EmbLCSet]:
    """Encode a 9-byte LC into every form the splicers need.

    Returns:
        h_lc:   196-bit BPTC codeword for VHEAD data-sync frames
        t_lc:   196-bit BPTC codeword for VTERM data-sync frames
        emb_lc: {1,2,3,4} → 32-bit fragments for voice bursts B/C/D/E
    """
    h_lc = bptc.encode_header_lc(lc)
    t_lc = bptc.encode_terminator_lc(lc)
    emb_lc = bptc.encode_emblc(lc)
    return h_lc, t_lc, emb_lc


def splice_full_lc(payload: bytes, full_lc: FullLC) -> bytes:
    """Return a new 33-byte payload with VHEAD/VTERM LC replaced.

    Keeps the 68-bit slot-type/sync window intact — colour code + data
    type (VHEAD vs VTERM) still decode correctly. Only the 196 LC bits
    change.
    """
    bits = bitarray(endian='big')
    bits.frombytes(payload)
    # full_lc is 196 bits; halves are [0:98] and [98:196].
    out = full_lc[0:98] + bits[_FULL_LC_SYNC] + full_lc[98:_FULL_LC_BITS]
    return out.tobytes()


def splice_emb_lc(payload: bytes, fragment: bitarray) -> bytes:
    """Return a new 33-byte payload with the 32-bit EMB_LC fragment replaced.

    AMBE vocoder bits at [0:116] and [148:264] are untouched so audio
    quality is preserved bit-for-bit.
    """
    bits = bitarray(endian='big')
    bits.frombytes(payload)
    out = bits[0:116] + fragment + bits[148:_PAYLOAD_BITS]
    return out.tobytes()


# Frame-type / dtype_vseq dispatch ------------------------------------------
#
# byte 15 encodes: bit7=slot, bit6=call_type, bits5-4=frame_type, bits3-0=dtype_vseq
# frame_type: 0 = voice, 1 = voice sync, 2 = data sync
# dtype_vseq when frame_type == 2:  1 = VHEAD, 2 = VTERM, 3 = CSBK, ... (data types)
# dtype_vseq when frame_type != 2:  0 = burst A, 1..4 = bursts B..E, 5 = burst F

# LC carrier kinds — what (if anything) the splicer should rewrite.
LC_CARRIER_NONE = 0
LC_CARRIER_VHEAD = 1
LC_CARRIER_VTERM = 2
LC_CARRIER_EMB = 3


def classify_lc_carrier(frame_type: int, dtype_vseq: int) -> int:
    """Identify which LC carrier (if any) lives in this payload.

    Returns one of LC_CARRIER_{NONE,VHEAD,VTERM,EMB}. Callers use this
    to decide whether to splice and which encoded form to use.
    """
    if frame_type == 2:
        if dtype_vseq == 1:
            return LC_CARRIER_VHEAD
        if dtype_vseq == 2:
            return LC_CARRIER_VTERM
        # CSBK (3) and other data types carry no LC we rewrite today.
        return LC_CARRIER_NONE
    # Voice / voice-sync frames: bursts B/C/D/E carry EMB_LC fragments.
    if 1 <= dtype_vseq <= 4:
        return LC_CARRIER_EMB
    return LC_CARRIER_NONE
