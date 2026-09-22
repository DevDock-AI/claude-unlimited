"""Guards on the release pipeline's shape. Releases here are immutable once
published, so the order of these steps is not a style choice: publishing
before the HUD is attached ships a release that can never get one."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ci_creates_a_draft_never_a_published_release():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "gh release create" in workflow
    assert "--draft" in workflow


def test_the_publish_script_checks_before_it_publishes():
    script = (ROOT / "scripts" / "publish_release.sh").read_text(encoding="utf-8")
    upload = script.index("gh release upload")
    publish = script.index("--draft=false")
    assert script.index("isDraft") < upload < publish
    assert script.index("gh release download") < publish   # round-trip digest check first
    assert "HUD.sha256" in script and "lipo -archs" in script


def test_the_release_build_is_universal():
    build = (ROOT / "macos-widget" / "build.sh").read_text(encoding="utf-8")
    assert "--arch arm64 --arch x86_64" in build
    # One trap covers everything: a later `trap … EXIT` replaces an earlier one.
    assert "trap cleanup EXIT" in build
    assert 'cleanup() { rm -rf "$GENERATED"' in build
