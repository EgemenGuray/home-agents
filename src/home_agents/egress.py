"""Egress route verification (S1.3.5): a job refuses to start on the wrong route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class RouteMismatch(Exception):
    pass


@dataclass(frozen=True)
class RouteCheck:
    route: str
    ip: str
    country: str


# A lookup takes a route name and returns (public_ip, country_code) as seen through that route.
Lookup = Callable[[str], tuple[str, str]]


def verify_route(route: str, routes_cfg: dict, lookup: Lookup, expected_ip: str | None = None) -> RouteCheck:
    if route not in routes_cfg:
        raise RouteMismatch(f"unknown route {route!r}")
    try:
        ip, country = lookup(route)
    except Exception as exc:  # a dead route fails closed
        raise RouteMismatch(f"route {route!r} unreachable: {exc}") from exc
    want = routes_cfg[route].get("country")
    if want and country.upper() != want.upper():
        raise RouteMismatch(f"route {route!r} exits from {country}, expected {want}")
    if expected_ip and ip != expected_ip:
        raise RouteMismatch(f"route {route!r} exits from an unexpected IP")
    return RouteCheck(route, ip, country.upper())


def proxy_lookup(proxies: dict[str, str], timeout: float = 10.0) -> Lookup:
    """Lookup that asks an IP-info service through each route's proxy.

    proxies maps a route name to its proxy URL, e.g. {"nord-de": "http://egress-gateway:18082"}.
    """
    import httpx

    def lookup(route: str) -> tuple[str, str]:
        with httpx.Client(proxy=proxies[route], timeout=timeout) as c:
            data = c.get("https://ipinfo.io/json").json()
        return data["ip"], data.get("country", "")

    return lookup
