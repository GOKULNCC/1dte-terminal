from playwright.sync_api import sync_playwright
import time
import json

def handle_response(response):
    if "statuses" in response.url and response.status == 200:
        try:
            data = response.json()
            print(f"Intercepted {len(data)} statuses from {response.url}!")
            for d in data[:2]:
                print(">>", d.get("content", "")[:100])
        except:
            pass

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False) # Headful might bypass CF
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    page = context.new_page()
    page.on("response", handle_response)
    print("Navigating...")
    page.goto('https://truthsocial.com/@realDonaldTrump', wait_until='networkidle', timeout=30000)
    print("Waiting for statuses...")
    try:
        page.wait_for_selector('div.status__content', timeout=10000)
        statuses = page.query_selector_all('div.status__content')
        print(f'Found {len(statuses)} posts via DOM')
    except Exception as e:
        print(f"Timeout or error: {e}")
        
    time.sleep(2)
    browser.close()
