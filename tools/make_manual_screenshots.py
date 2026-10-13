"""マニュアル用のスクリーンショットを作る。モックモードの開発サーバーを起動してから実行する。

使い方: python tools/make_manual_screenshots.py [出力先 web/manual-img] [URL http://127.0.0.1:8787/]
番号の丸印は、スクリーンショット上に重ねて描く（画面のコードには含まれない）。
"""
import os
import sys

from playwright.sync_api import sync_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "web/manual-img"
URL = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8787/"
CHROME = os.environ.get("XRONIX_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")

BADGE_JS = """
(items) => {
  document.querySelectorAll('.mbadge').forEach(e => e.remove());
  items.forEach(([sel, n, dx, dy], i) => {
    const el = typeof sel === 'string' ? document.querySelector(sel) : null;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const b = document.createElement('div');
    b.className = 'mbadge';
    b.textContent = n;
    b.style.cssText = `position:absolute;z-index:999;width:24px;height:24px;border-radius:50%;background:#e0245e;color:#fff;font:700 14px/24px sans-serif;text-align:center;box-shadow:0 0 0 2px #fff;left:${r.left + scrollX + (dx||0)}px;top:${r.top + scrollY + (dy||0)}px`;
    document.body.append(b);
  });
}
"""


def shot(pg, name, badges=None, full=False, clip_sel=None):
    if name != "toasts":
        pg.evaluate("document.querySelectorAll('.toast').forEach(e => e.remove())")
    pg.evaluate(BADGE_JS, badges or [])
    path = f"{OUT}/{name}.png"
    if clip_sel:
        pg.locator(clip_sel).first.screenshot(path=path)
    else:
        pg.screenshot(path=path, full_page=full)
    print("wrote", path)


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1100, "height": 760}, device_scale_factor=1)
        pg.goto(URL)
        pg.wait_for_selector("#chips .chip")
        # 設定
        pg.click("[data-tab=settings]")
        pg.wait_for_selector("text=X との接続")
        pg.click("text=Xと接続")
        pg.wait_for_selector("text=接続中: @example_mock")
        pg.wait_for_timeout(300)
        shot(pg, "settings", full=True)
        # ジョブ
        pg.click("[data-tab=jobs]")
        pg.wait_for_selector("text=定期処理")
        pg.click("text=今すぐ実行"); pg.wait_for_timeout(600)
        pg.click("text=サンプルのメンションを追加"); pg.wait_for_timeout(300)
        pg.click("text=今すぐ実行"); pg.wait_for_timeout(900)
        shot(pg, "jobs", full=True)
        # 承認待ち
        pg.click("[data-tab=queue]")
        pg.wait_for_selector(".card")
        pg.wait_for_timeout(300)
        shot(pg, "queue", badges=[
            ("#manual-link", 1, 20, -6), ("#kill", 2, -10, -8), ("#tabs button[data-tab=queue]", 3, -6, -8),
            (".card", 4, -8, -8), (".card textarea", 5, -8, -8),
        ], full=True)
        shot(pg, "queue-card", clip_sel=".card")
        # トースト（上部・4段階）
        pg.evaluate("""() => { toast('保存しました'); toast('取得を始めました', 'info'); toast('入力を確認してください', 'warn'); toast('Xとの通信に失敗しました', 'error'); }""")
        pg.wait_for_timeout(500)
        shot(pg, "toasts")
        pg.wait_for_timeout(7500)
        # 履歴・ルール・定型文・統計
        for tab, name in [("history", "history"), ("rules", "rules"), ("templates", "templates"), ("stats", "stats")]:
            pg.click(f"[data-tab={tab}]")
            pg.wait_for_timeout(700)
            shot(pg, name, full=True)
        # ⓘのツールチップ
        pg.click("[data-tab=rules]"); pg.wait_for_timeout(500)
        tip = pg.locator("[title]:has-text('ⓘ'), .tip").first
        if tip.count():
            tip.hover(); pg.wait_for_timeout(1200)
            shot(pg, "tooltip")
        b.close()


main()
