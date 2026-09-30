import os, json, time, hashlib, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
import requests
import feedparser
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config.json").read_text())
STATE_FILE = ROOT / "data" / "state.json"
STATE = json.loads(STATE_FILE.read_text())

ETH_RPC = os.getenv("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com")
SHIB = Web3.to_checksum_address("0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE")
BURN_ADDRESSES = {
    Web3.to_checksum_address("0x000000000000000000000000000000000000dead"),
    Web3.to_checksum_address("0x0000000000000000000000000000000000000000")
}
TRANSFER_TOPIC = "0x" + Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex().removeprefix("0x")

def save_state():
    STATE["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE["seen_news"] = STATE["seen_news"][-500:]
    STATE["seen_burn_tx"] = STATE["seen_burn_tx"][-500:]
    STATE["posted_hashes"] = STATE["posted_hashes"][-500:]
    STATE_FILE.write_text(json.dumps(STATE, indent=2))

def post_hash(text):
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()

def already_posted(text):
    return post_hash(text) in STATE["posted_hashes"]

def remember_post(text):
    STATE["posted_hashes"].append(post_hash(text))

def shib_number(n):
    return f"{n:,.0f}"

def get_price():
    token_address = "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE"

    r = requests.get(
        f"https://api.dexscreener.com/token-pairs/v1/ethereum/{token_address}",
        timeout=15
    )
    r.raise_for_status()

    pairs = r.json()

    if not isinstance(pairs, list):
        return None

    # Only consider pairs with meaningful liquidity.
    valid_pairs = []

    for p in pairs:
        liquidity = float(
            (p.get("liquidity") or {}).get("usd") or 0
        )

        volume = float(
            (p.get("volume") or {}).get("h24") or 0
        )

        price = float(p.get("priceUsd") or 0)

        if liquidity >= 100000 and price > 0:
            valid_pairs.append({
                "price": price,
                "volume24h": volume,
                "liquidity": liquidity,
                "url": p.get("url")
            })

    if not valid_pairs:
        return None

    # Use the most liquid SHIB pair.
    return max(
        valid_pairs,
        key=lambda x: x["liquidity"]
    )

def get_burns():
    try:
        w3 = Web3(Web3.HTTPProvider(ETH_RPC, request_kwargs={"timeout": 20}))

        if not w3.is_connected():
            return []

        latest_block = w3.eth.block_number

        # Scan only recent blocks and split the request into small ranges.
        scan_blocks = 5000
        chunk_size = 500

        from_block = max(0, latest_block - scan_blocks)
        to_block = latest_block

        transfer_topic = Web3.keccak(
            text="Transfer(address,address,uint256)"
        ).hex()

        if not transfer_topic.startswith("0x"):
            transfer_topic = "0x" + transfer_topic

        burns = []

        for start in range(from_block, to_block + 1, chunk_size):
            end = min(start + chunk_size - 1, to_block)

            try:
                logs = w3.eth.get_logs({
                    "fromBlock": start,
                    "toBlock": end,
                    "address": Web3.to_checksum_address(
                        "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE"
                    ),
                    "topics": [transfer_topic]
                })
            except Exception as e:
                print(f"Burn chunk {start}-{end}: {e}")
                continue

            for log in logs:
                try:
                    topic = log["topics"][2]
                    topic_hex = bytes(topic).hex()

                    to_addr = Web3.to_checksum_address(
                        "0x" + topic_hex[-40:]
                    )

                    if to_addr.lower() not in {
                        BURN_DEAD.lower(),
                        BURN_ZERO.lower()
                    }:
                        continue

                    amount = int(log["data"], 16) / 10**18

                    if amount < MIN_BURN_SHIB:
                        continue

                    burns.append({
                        "tx": log["transactionHash"].hex(),
                        "amount": amount,
                        "block": log["blockNumber"]
                    })

                except Exception:
                    continue

        return burns

    except Exception as e:
        print(f"Burn scanner: {e}")
        return []

def get_news():
    now = datetime.now(timezone.utc)
    results = []
    for feed_cfg in CONFIG["rss_feeds"]:
        feed = feedparser.parse(feed_cfg["url"])
        for item in feed.entries[:15]:
            link = item.get("link", "")
            title = re.sub(r"\s+", " ", item.get("title", "")).strip()
            if not title or not link:
                continue
            uid = hashlib.sha256(link.encode()).hexdigest()
            if uid in STATE["seen_news"]:
                continue
            published = item.get("published_parsed")
            if published:
                ts = datetime(*published[:6], tzinfo=timezone.utc)
                if now - ts > timedelta(hours=CONFIG["news_max_age_hours"]):
                    continue
            results.append({"title": title, "link": link, "source": feed_cfg["name"], "uid": uid})
    return results

def make_burn_post(b):
    return f"🔥 SHIB BURN ALERT\n\n{shib_number(b['amount'])} $SHIB sent to a burn address.\n\nTransaction: https://etherscan.io/tx/{b['tx']}\n\n#SHIB #ShibaInu #SHIBBurn"

def make_news_post(n):
    return f"📰 SHIB NEWS\n\n{n['title']}\n\nSource: {n['source']}\n{n['link']}\n\n#SHIB #ShibaInu #Shibarium"

def make_market_post(m):
    return f"📊 SHIB MARKET UPDATE\n\nPrice: ${m['price']:.10f}\n24h volume: ${m['volume24h']:,.0f}\n\nData: {m['url']}\n\n#SHIB #ShibaInu"

def x_access_token():
    refresh = os.getenv("X_REFRESH_TOKEN")
    client_id = os.getenv("X_CLIENT_ID")
    client_secret = os.getenv("X_CLIENT_SECRET")
    if not all([refresh, client_id, client_secret]):
        return None
    r = requests.post(
        "https://api.x.com/2/oauth2/token",
        data={"refresh_token": refresh, "grant_type": "refresh_token", "client_id": client_id},
        auth=(client_id, client_secret), timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

def publish(text):
    if os.getenv("DRY_RUN", "true").lower() == "true":
        print("\n--- DRY RUN ---\n" + text + "\n--- END ---")
        return True
    token = x_access_token()
    if not token:
        print("Missing X OAuth credentials.")
        return False
    r = requests.post(
        "https://api.x.com/2/tweets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"text": text}, timeout=20)
    print(r.status_code, r.text[:500])
    return r.ok

def main():
    candidates = []
    try:
        candidates += [(1, make_burn_post(b)) for b in get_burns()]
    except Exception as e:
        print("Burn scanner:", e)
    try:
        for n in get_news():
            candidates.append((2, make_news_post(n)))
            STATE["seen_news"].append(n["uid"])
    except Exception as e:
        print("News scanner:", e)
    try:
        m = get_price()
        if m:
            candidates.append((3, make_market_post(m)))
    except Exception as e:
        print("Market scanner:", e)

    candidates.sort(key=lambda x: x[0])
    sent = 0
    for _, text in candidates:
        if sent >= CONFIG["max_posts_per_run"]:
            break
        if already_posted(text):
            continue
        if publish(text):
            remember_post(text)
            sent += 1
            time.sleep(2)
    save_state()
    print("Completed:", sent)

if __name__ == "__main__":
    main()
