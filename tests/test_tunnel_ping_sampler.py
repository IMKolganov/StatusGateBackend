from app.services.tunnel_ping_sampler import parse_ping_aggregate

FULL_OUTPUT = """\
PING 10.8.0.1 (10.8.0.1) 56(84) bytes of data.
64 bytes from 10.8.0.1: icmp_seq=1 ttl=64 time=31.2 ms
64 bytes from 10.8.0.1: icmp_seq=2 ttl=64 time=29.8 ms

--- 10.8.0.1 ping statistics ---
55 packets transmitted, 53 received, 3.63636% packet loss, time 54081ms
rtt min/avg/max/mdev = 28.911/33.421/95.202/8.877 ms
"""

TOTAL_LOSS_OUTPUT = """\
PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
10 packets transmitted, 0 received, 100% packet loss, time 9216ms
"""

BUSYBOX_OUTPUT = """\
PING 8.8.8.8 (8.8.8.8): 56 data bytes

--- 8.8.8.8 ping statistics ---
30 packets transmitted, 30 packets received, 0% packet loss
round-trip min/avg/max = 41.1/44.9/60.2 ms
"""


def test_parse_full_output_with_rtt() -> None:
    result = parse_ping_aggregate(FULL_OUTPUT)
    assert result is not None
    assert result["samples_sent"] == 55
    assert result["samples_received"] == 53
    assert result["loss_percent"] == 3.64
    assert result["min_ms"] == 28.911
    assert result["avg_ms"] == 33.421
    assert result["max_ms"] == 95.202
    assert result["jitter_ms"] == 8.877


def test_parse_total_loss_has_no_rtt() -> None:
    result = parse_ping_aggregate(TOTAL_LOSS_OUTPUT)
    assert result is not None
    assert result["samples_sent"] == 10
    assert result["samples_received"] == 0
    assert result["loss_percent"] == 100.0
    assert "avg_ms" not in result


def test_parse_busybox_style_counts() -> None:
    result = parse_ping_aggregate(BUSYBOX_OUTPUT)
    assert result is not None
    assert result["samples_sent"] == 30
    assert result["samples_received"] == 30
    assert result["loss_percent"] == 0.0


def test_parse_garbage_returns_none() -> None:
    assert parse_ping_aggregate("") is None
    assert parse_ping_aggregate("ping: connect: Network is unreachable") is None
