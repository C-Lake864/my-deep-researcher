"""README 용 데모 화면 캡처 (streamlit 이 8501 에서 떠 있어야 한다)."""
import sys

from playwright.sync_api import sync_playwright

RUN = sys.argv[1] if len(sys.argv) > 1 else "afdc76dc"
OFFZ = sys.argv[2] if len(sys.argv) > 2 else "0729b2e2"
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    pg = b.new_page(viewport={"width": 1440, "height": 1000})
    for view in ["report", "trace", "graph", "metrics"]:
        pg.goto(f"http://localhost:8501/?run={RUN}&view={view}")
        pg.wait_for_selector("text=경보", timeout=30000)
        if view == "graph":
            pg.wait_for_selector('[data-testid="stGraphVizChart"] svg', timeout=30000)
        pg.wait_for_timeout(3000)
        pg.screenshot(path=f"docs/screen_{view}.png", full_page=view == "graph")
    pg.goto(f"http://localhost:8501/?run={OFFZ}&view=graph")
    pg.wait_for_selector('[data-testid="stGraphVizChart"] svg', timeout=30000)
    pg.wait_for_timeout(3000)
    pg.screenshot(path="docs/screen_graph_offzones.png", full_page=True)
    pg.goto(f"http://localhost:8501/")
    pg.wait_for_selector("text=경보", timeout=30000)
    pg.get_by_text("나란히 비교").click()
    pg.wait_for_timeout(4000)
    pg.screenshot(path="docs/screen_compare.png")
    b.close()
