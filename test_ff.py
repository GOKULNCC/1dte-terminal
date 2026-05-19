from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('https://www.forexfactory.com/calendar?month=this', wait_until='domcontentloaded')
    page.wait_for_timeout(3000)
    rows = page.query_selector_all('table.calendar__table tr.calendar__row')
    print(f'Rows found: {len(rows)}')
    if rows:
        row = rows[0]
        date_el = row.query_selector('td.calendar__date')
        print(f'date_el text: {date_el.inner_text().strip() if date_el else "None"}')
        time_el = row.query_selector('td.calendar__time')
        print(f'time_el text: {time_el.inner_text().strip() if time_el else "None"}')
    browser.close()
