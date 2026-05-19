from playwright.sync_api import sync_playwright
import os

def capture_dashboard_screenshots():
    print("Launching browser to capture screenshots...")
    with sync_playwright() as p:
        # Launch headless Chromium
        browser = p.chromium.launch(headless=True)
        # 430px width matches the max-width in trading_app.jsx (mobile-optimized dashboard)
        context = browser.new_context(viewport={'width': 430, 'height': 932})
        page = context.new_page()
        
        url = "http://localhost:8088"
        try:
            print(f"Navigating to {url}...")
            page.goto(url, wait_until="networkidle", timeout=10000)
            page.wait_for_timeout(3000) # Give it time to load API data
            
            # 1. Capture Home Screen
            out_home = os.path.abspath("dashboard_home.png")
            page.screenshot(path=out_home, full_page=True)
            print(f"Saved Home screenshot: {out_home}")
            
            # 2. Capture Predict Screen
            print("Navigating to Predict panel...")
            page.click("text=Predict")
            page.wait_for_timeout(2000)
            out_predict = os.path.abspath("dashboard_predict.png")
            page.screenshot(path=out_predict, full_page=True)
            print(f"Saved Predict screenshot: {out_predict}")
            
            # 3. Capture News Screen
            print("Navigating to News panel...")
            page.click("text=News")
            page.wait_for_timeout(2000)
            out_news = os.path.abspath("dashboard_news.png")
            page.screenshot(path=out_news, full_page=True)
            print(f"Saved News screenshot: {out_news}")

        except Exception as e:
            print(f"Error capturing screenshots: {e}")
            
        finally:
            browser.close()

if __name__ == "__main__":
    capture_dashboard_screenshots()
