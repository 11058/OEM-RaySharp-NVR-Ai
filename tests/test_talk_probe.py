"""SDP parsing for the two-way audio probe."""

from conftest import load_module

tp = load_module("talk_probe")

SDP = """v=0
o=- 0 0 IN IP4 10.1.12.10
s=Session
a=control:*
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
a=recvonly
m=audio 0 RTP/AVP 8
a=rtpmap:8 PCMA/8000
a=recvonly
m=audio 0 RTP/AVP 0
a=rtpmap:0 PCMU/8000
a=sendonly
"""


def test_backchannel_found():
    media = tp.parse_sdp(SDP)
    back = [m for m in media if m["kind"] == "audio" and m["direction"] == "sendonly"]
    assert back and back[0]["codecs"] == ["PCMU/8000"]


def test_session_level_direction_applies():
    media = tp.parse_sdp("v=0\na=recvonly\nm=audio 0 RTP/AVP 8\na=rtpmap:8 PCMA/8000\n")
    assert media[0]["direction"] == "recvonly"
