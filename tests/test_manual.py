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
