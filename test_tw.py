import requests
import json
r = requests.get('https://syndication.twitter.com/srv/timeline-profile/screen-name/realDonaldTrump', headers={'User-Agent': 'Mozilla/5.0'})
print(r.status_code)
if 'id="__NEXT_DATA__"' in r.text:
    data = r.text.split('id="__NEXT_DATA__" type="application/json">')[1].split('</script>')[0]
    j = json.loads(data)
    print("Success!")
    tweets = j.get('props',{}).get('pageProps',{}).get('timeline',{}).get('entries',[])
    for t in tweets:
        tweet = t.get('content',{}).get('tweet',{})
        if tweet:
            print(tweet.get('text', '')[:100])
