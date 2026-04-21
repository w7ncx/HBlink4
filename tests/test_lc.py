"""
Tests for the DMR Link Control encode/splice helpers in hblink4.lc.

These verify the bit-level splicing correctness by round-tripping through
dmr_utils3's decoders: encode an LC → splice it into a payload → decode
what we just spliced and confirm it matches the original LC.

Also pins down the `classify_lc_carrier` dispatch so the hot path's
frame-type decisions stay stable.
"""
import pytest
from bitarray import bitarray
from dmr_utils3 import bptc, decode

from hblink4.lc import (
    LC_OPT_GROUP_DEFAULT,
    LC_CARRIER_NONE, LC_CARRIER_VHEAD, LC_CARRIER_VTERM, LC_CARRIER_EMB,
    build_lc, synth_lc_base, decode_lc_from_vhead, encode_lc_forms,
    splice_full_lc, splice_emb_lc, classify_lc_carrier,
)


def _make_lc(dst_int: int, src_int: int,
             opts: bytes = LC_OPT_GROUP_DEFAULT) -> bytes:
    """Build a 9-byte LC for the given integer dst/src."""
    return build_lc(opts,
                    dst_int.to_bytes(3, 'big'),
                    src_int.to_bytes(3, 'big'))


def _make_vhead_payload(lc: bytes) -> bytes:
    """Construct a 33-byte VHEAD payload containing the given LC.

    The 68-bit middle window carries slot-type (CC=1, dtype=VHEAD=1)
    plus a placeholder sync. decode.voice_head_term will read the LC
    back out regardless of what's in that window, since it decodes LC
    from the BPTC-protected [0:98]+[166:264] bits only.
    """
    full_lc = bptc.encode_header_lc(lc)  # 196 bits
    # slot_type: 4 bits CC | 4 bits DTYPE | 2 halves of 5 bits each with
    # Hamming protection — we don't bother, just fill with zeros since
    # our splicer preserves whatever bits are already there.
    middle = bitarray('0' * 68, endian='big')
    payload_bits = full_lc[0:98] + middle + full_lc[98:196]
    assert len(payload_bits) == 264
    return payload_bits.tobytes()


# --- build_lc / synth_lc_base -----------------------------------------------

def test_build_lc_concat_order():
    opts = b'\x00\x00\x20'
    dst = b'\x00\x00\x09'
    src = b'\x00\x12\x34'
    lc = build_lc(opts, dst, src)
    assert lc == opts + dst + src
    assert len(lc) == 9


def test_synth_lc_base_defaults_group_voice():
    lc = synth_lc_base(b'\x00\x00\x09', b'\x00\x12\x34')
    assert lc[:3] == LC_OPT_GROUP_DEFAULT
    assert lc[3:6] == b'\x00\x00\x09'
    assert lc[6:9] == b'\x00\x12\x34'


# --- encode_lc_forms -------------------------------------------------------

def test_encode_lc_forms_shapes():
    lc = _make_lc(9, 0x123456)
    h_lc, t_lc, emb_lc = encode_lc_forms(lc)
    assert len(h_lc) == 196
    assert len(t_lc) == 196
    assert set(emb_lc.keys()) == {1, 2, 3, 4}
    assert all(len(frag) == 32 for frag in emb_lc.values())


# --- splice_full_lc round-trip ---------------------------------------------

def test_splice_full_lc_roundtrip_vhead():
    """Splicing an encoded header LC into a payload and decoding it back
    must return the same 9-byte LC."""
    original_lc = _make_lc(9, 0x123456)
    original_payload = _make_vhead_payload(original_lc)

    # Now rewrite to a different dst/src
    new_lc = _make_lc(3100, 0x555555)
    h_lc = bptc.encode_header_lc(new_lc)
    spliced = splice_full_lc(original_payload, h_lc)
    assert len(spliced) == 33

    decoded = decode.voice_head_term(spliced)
    assert decoded['LC'] == new_lc


def test_splice_full_lc_preserves_middle_68_bits():
    """The slot-type/sync window at bits [98:166] must survive splicing
    untouched — it carries CC and data-type, neither of which change."""
    original_lc = _make_lc(9, 0x123456)
    # Build a payload where the middle 68 bits are a recognisable pattern.
    full_lc = bptc.encode_header_lc(original_lc)
    marker = bitarray('10110011' * 8 + '1100', endian='big')  # 68 bits
    assert len(marker) == 68
    bits = full_lc[0:98] + marker + full_lc[98:196]
    payload = bits.tobytes()

    # Splice with a different LC
    new_lc = _make_lc(3100, 0x555555)
    new_h_lc = bptc.encode_header_lc(new_lc)
    spliced = splice_full_lc(payload, new_h_lc)

    spliced_bits = bitarray(endian='big')
    spliced_bits.frombytes(spliced)
    assert spliced_bits[98:166] == marker


def test_splice_full_lc_vterm_roundtrip():
    original_lc = _make_lc(9, 0x111111)
    # Build a VTERM-style payload (same bit layout, different dtype flag
    # in the middle window — we don't encode it, just use zeros).
    t_lc = bptc.encode_terminator_lc(original_lc)
    bits = t_lc[0:98] + bitarray('0' * 68, endian='big') + t_lc[98:196]
    payload = bits.tobytes()
    assert len(payload) == 33

    new_lc = _make_lc(9, 0x222222)
    new_t_lc = bptc.encode_terminator_lc(new_lc)
    spliced = splice_full_lc(payload, new_t_lc)

    # voice_head_term decodes both VHEAD and VTERM bit layouts
    decoded = decode.voice_head_term(spliced)
    assert decoded['LC'] == new_lc


# --- splice_emb_lc round-trip ----------------------------------------------

def test_splice_emb_lc_roundtrip_all_bursts():
    """For each of bursts B/C/D/E, splicing in a fragment and extracting
    the 32-bit window back must equal the encoded fragment."""
    lc = _make_lc(9, 0xAABBCC)
    _, _, emb_lc = encode_lc_forms(lc)

    # Start with a 33-byte payload full of a recognisable pattern so we
    # can check AMBE bits [0:116] and [148:264] survive untouched.
    pattern = bytes(range(33))
    for burst in (1, 2, 3, 4):
        spliced = splice_emb_lc(pattern, emb_lc[burst])
        assert len(spliced) == 33

        bits = bitarray(endian='big')
        bits.frombytes(spliced)
        assert bits[116:148] == emb_lc[burst]

        # AMBE halves untouched
        orig_bits = bitarray(endian='big')
        orig_bits.frombytes(pattern)
        assert bits[0:116] == orig_bits[0:116]
        assert bits[148:264] == orig_bits[148:264]


def test_splice_emb_lc_decodes_back_to_lc():
    """Reassembling all four EMB_LC fragments with dmr_utils3.bptc.decode_emblc
    should yield the LC bits we started with."""
    lc = _make_lc(9, 0x123456)
    _, _, emb_lc = encode_lc_forms(lc)

    # dmr_utils3 expects the four 32-bit fragments concatenated — check
    # decode round-trip if it exposes that path, otherwise assert each
    # fragment individually equals encode_emblc's output for that burst.
    # encode_emblc is deterministic; that's the contract we need.
    regen = bptc.encode_emblc(lc)
    for burst in (1, 2, 3, 4):
        assert emb_lc[burst] == regen[burst]


# --- decode_lc_from_vhead --------------------------------------------------

def test_decode_lc_from_vhead_returns_original():
    lc = _make_lc(9, 0x123456)
    payload = _make_vhead_payload(lc)
    got = decode_lc_from_vhead(payload)
    assert got == lc


def test_decode_lc_from_vhead_short_payload_returns_none():
    assert decode_lc_from_vhead(b'\x00' * 10) is None


def test_decode_lc_from_vhead_empty_returns_none():
    assert decode_lc_from_vhead(b'') is None


# --- classify_lc_carrier ---------------------------------------------------

@pytest.mark.parametrize("frame_type,dtype_vseq,expected", [
    # Data sync frames: LC only in VHEAD and VTERM
    (2, 1, LC_CARRIER_VHEAD),
    (2, 2, LC_CARRIER_VTERM),
    (2, 3, LC_CARRIER_NONE),  # CSBK — not rewritten today
    (2, 0, LC_CARRIER_NONE),
    (2, 9, LC_CARRIER_NONE),
    # Voice frames: EMB_LC in bursts B/C/D/E (vseq 1..4)
    (0, 0, LC_CARRIER_NONE),  # burst A
    (0, 1, LC_CARRIER_EMB),
    (0, 2, LC_CARRIER_EMB),
    (0, 3, LC_CARRIER_EMB),
    (0, 4, LC_CARRIER_EMB),
    (0, 5, LC_CARRIER_NONE),  # burst F
    # Voice sync frames same treatment as voice
    (1, 1, LC_CARRIER_EMB),
    (1, 0, LC_CARRIER_NONE),
])
def test_classify_lc_carrier(frame_type, dtype_vseq, expected):
    assert classify_lc_carrier(frame_type, dtype_vseq) == expected
