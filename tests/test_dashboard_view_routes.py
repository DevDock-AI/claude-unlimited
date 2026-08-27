"""Each Dashboard view has its own URL, so a refresh or a pasted link opens it.

The daemon is a Dashboard AND a live proxy on one listener: every unknown path
falls through to be forwarded upstream. These routes must therefore be an
exact-match allowlist — a prefix or catch-all would swallow real traffic.
"""
import claude_unlimited.daemon as daemon


def test_every_client_route_is_served_by_the_daemon():
    # The client's VIEW_ROUTES and the server's allowlist must agree, or a
    # refresh on a view the client can reach returns a proxy error.
    js = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")
    block = js.split("const VIEW_ROUTES = {", 1)[1].split("};", 1)[0]
    client_paths = {
        line.split(":", 1)[1].strip().strip("',")
        for line in block.splitlines() if ":" in line and "'" in line
    }
    assert client_paths, "could not parse VIEW_ROUTES from app.js"
    missing = client_paths - set(daemon._VIEW_ROUTES)
    assert not missing, f"client routes the daemon does not serve: {missing}"


def test_routes_are_exact_matches_not_prefixes():
    # '/profiles/extra' must reach the proxy, not the Dashboard.
    for route in daemon._VIEW_ROUTES:
        assert not route.endswith("*")
        if route != "/":
            assert f"{route}/extra" not in daemon._VIEW_ROUTES


def test_no_view_route_shadows_the_api_or_the_proxy():
    for route in daemon._VIEW_ROUTES:
        assert not route.startswith("/api/"), f"{route} shadows the management API"
        assert not route.startswith("/v1/"), f"{route} shadows proxied provider traffic"


def test_the_route_set_is_immutable():
    # A mutable set here could be edited at runtime into a catch-all.
    assert isinstance(daemon._VIEW_ROUTES, frozenset)


def test_a_trailing_slash_still_serves_the_dashboard():
    # '/profiles/' missing the set would fall through to the proxy and show a
    # browser a 401 JSON error instead of the Dashboard.
    for route in daemon._VIEW_ROUTES:
        normalised = (f"{route}/".rstrip("/") or "/")
        assert normalised in daemon._VIEW_ROUTES


def test_view_routes_share_the_csrf_injecting_branch():
    # Serving _INDEX_HTML without substituting __CSRF_TOKEN__ produces a
    # Dashboard where every write 403s — and only on direct navigation, so it
    # passes casual testing and fails for real users.
    src = (daemon.Path(daemon.__file__)).read_text(encoding="utf-8")
    branch = src.split("if (path.rstrip", 1)[1][:400]
    assert "__CSRF_TOKEN__" in branch, "view routes must go through the CSRF-substituting branch"


def test_the_client_normalises_trailing_slashes_like_the_daemon():
    # The daemon serves the Dashboard for "/settings/" (it rstrips the slash),
    # so the client must resolve the same path to the same view. It did not,
    # and "/settings/" rendered Overview while the URL said settings.
    js = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")
    fn = js.split("function viewFromLocation()", 1)[1].split("}", 1)[0]
    assert "replace(" in fn and "pathname" in fn, \
        "viewFromLocation must normalise the path before lookup"
