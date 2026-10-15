from drone_oled.monitor import lan_address


def interface(name, ip, state="UP"):
    return dict(ifname=name, operstate=state, addr_info=[dict(local=ip, scope="global")])


def test_ethernet_wins_over_wifi_and_tailscale(config):
    addresses = [
        interface("wlan0", "10.60.14.40"),
        interface("tailscale0", "100.112.205.54"),
        interface("eth0", "192.168.1.50"),
    ]
    assert lan_address(addresses, config["pi"], True) == ("192.168.1.50", "eth0")


def test_wifi_when_ethernet_unplugged(config):
    addresses = [interface("eth0", "192.168.1.50"), interface("wlan0", "10.60.14.40")]
    assert lan_address(addresses, config["pi"], False) == ("10.60.14.40", "wlan0")


def test_no_vpn_fallback_or_disconnected_address(config):
    addresses = [
        interface("tailscale0", "100.112.205.54"),
        interface("eth0", "192.168.1.50", "DOWN"),
    ]
    assert lan_address(addresses, config["pi"], True) == ("--", "")
    assert lan_address([interface("wlan0", "100.112.205.54")], config["pi"], False) == ("--", "")


def test_ignore_ipv6_and_linklocal(config):
    addresses = [interface("wlan0", "fe80::1"), interface("wlan0", "169.254.1.2")]
    assert lan_address(addresses, config["pi"], False) == ("--", "")
