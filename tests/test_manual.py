import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
TABS = ["queue", "history", "rules", "templates", "jobs", "stats", "settings"]


def test_manual_images_exist():
    html = (WEB / "manual.html").read_text(encoding="utf-8")
    imgs = re.findall(r'src="(manual-img/[^"]+)"', html)
    assert len(imgs) >= 8
    for i in imgs:
        assert (WEB / i).is_file(), i


def test_manual_has_anchor_for_every_tab():
    html = (WEB / "manual.html").read_text(encoding="utf-8")
    for t in TABS:
        assert f'id="{t}"' in html, t


def test_manual_internal_links_resolve():
    html = (WEB / "manual.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    for a in re.findall(r'href="#([^"]+)"', html):
        assert a in ids, a


def test_index_links_manual_in_new_tab():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<a id="manual-link"[^>]*>', html)
    assert m and 'href="manual.html"' in m.group(0) and 'target="_blank"' in m.group(0) and "noopener" in m.group(0)


def test_toasts_at_top():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert re.search(r"#toasts\{[^}]*top:\s*12px", css)
    assert "bottom" not in re.search(r"#toasts\{[^}]*\}", css).group(0)
    for lv in ("success", "info", "warn", "error"):
        assert f".toast.{lv}" in css


def test_mobile_layout_rules_exist():
    """スマホで横にはみ出さない: ⓘの説明は画面内に配置し（固定位置）、表は横にスクロールさせる"""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "overflow-x:hidden" in css and ".tip.on::after{display:block}" in css
    assert "position:fixed" in re.search(r"\.tip::after\{[^}]*\}", css).group(0)
    assert re.search(r"@media \(max-width:640px\)\{[^}]*table\{display:block;overflow-x:auto", css, re.S)
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "--tip-left" in js and "document.documentElement.clientWidth" in js


def test_viewport_blocks_auto_zoom_out_and_assets_are_not_cached():
    """スマホで、内容が少しでもはみ出すと、ブラウザが画面全体を縮小表示する。最小の倍率を1に固定する。配信時は、古い画面を使い続けさせない"""
    for name in ("index.html", "manual.html"):
        assert "minimum-scale=1" in (WEB / name).read_text(encoding="utf-8")
    wf = (WEB.parent / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert re.search(r"aws s3 sync web .* --cache-control \"no-cache\"", wf)
