"""ShibRadar — SHIB burns, whale moves, news and market data, published to Threads.

Run:  python -m src.main
Env:  THREADS_ACCESS_TOKEN  long-lived Threads token (GitHub Secret)
      DRY_RUN               "true" (default) prints posts instead of publishing
      ETH_RPC_URL           optional private Ethereum RPC, tried before the
                            public ones listed in config.json
"""

import os
import json
import time
import hashlib
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import feedparser
from web3 import Web3


# ============================================================
# PATHS / CONFIG
# ============================================================

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "data" / "state.json"
STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))

DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() != "false"


# ============================================================
# SHIB / ETH CONFIG
# ============================================================

# Public RPCs that answer eth_getLogs (tested 6 Oct 2026). Tried in order,
# per request: one refusing (403/429, common for GitHub's datacenter IPs)
# falls through to the next.
DEFAULT_RPC_URLS = [
    "https://ethereum-rpc.publicnode.com",
    "https://rpc.mevblocker.io",
    "https://gateway.tenderly.co/public/mainnet",
    "https://eth.drpc.org",
]

SHIB = Web3.to_checksum_address("0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE")

# Topic addresses are compared as lowercase hex without 0x.
BURN_ADDRESSES = {
    "000000000000000000000000000000000000dead",
    "0000000000000000000000000000000000000000",
}

TRANSFER_TOPIC = "0x" + Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex().removeprefix("0x")

MIN_BURN_SHIB = int(CONFIG.get("min_burn_shib", 1_000_000))
WHALE_THRESHOLD_SHIB = int(CONFIG.get("whale_threshold_shib", 50_000_000_000))

# ~7200 blocks per day. A run normally scans only the blocks since the
# previous run; after a long pause it jumps ahead instead of flooding.
MAX_SCAN_BLOCKS = int(CONFIG.get("max_scan_blocks", 3000))
FIRST_RUN_BLOCKS = 300
# publicnode answers 403 and drpc 400 to 500-block eth_getLogs ranges;
# 100 blocks works on all of them (measured 6 Oct 2026).
CHUNK_SIZE = 100
# Stay a few blocks behind the head: fallback RPCs may lag slightly, and
# the newest blocks can still be reorganised.
CONFIRMATIONS = 3


# ============================================================
# THREADS CONFIG
# ============================================================

THREADS_API = "https://graph.threads.net/v1.0"
THREADS_MAX_CHARS = 500


# ============================================================
# STATE
# ============================================================

for key in ("seen_news", "seen_burn_tx", "seen_whale_tx", "posted_hashes"):
    STATE.setdefault(key, [])


def save_state():
    STATE["last_run"] = datetime.now(timezone.utc).isoformat()
    for key in ("seen_news", "seen_burn_tx", "seen_whale_tx", "posted_hashes"):
        STATE[key] = STATE[key][-500:]
    STATE_FILE.write_text(json.dumps(STATE, indent=2), encoding="utf-8")


# ============================================================
# POST DEDUPLICATION
# ============================================================

def post_hash(text):
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()


def already_posted(text):
    return post_hash(text) in STATE["posted_hashes"]


def remember_post(text):
    STATE["posted_hashes"].append(post_hash(text))


# ============================================================
# FORMATTING
# ============================================================

def shib_number(n):
    return f"{n:,.0f}"


def short_addr(addr):
    return f"{addr[:6]}…{addr[-4:]}"


def tx_hex(tx_hash):
    h = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    return "0x" + h.removeprefix("0x")


def fit(text, limit=THREADS_MAX_CHARS):
    """Threads rejects posts over 500 characters."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ============================================================
# MARKET DATA
# ============================================================

def get_price():
    r = requests.get(
        f"https://api.dexscreener.com/token-pairs/v1/ethereum/{SHIB}",
        timeout=15,
    )
    r.raise_for_status()
    pairs = r.json()
    if not isinstance(pairs, list):
        return None

    valid_pairs = []
    for p in pairs:
        liquidity = float((p.get("liquidity") or {}).get("usd") or 0)
        volume = float((p.get("volume") or {}).get("h24") or 0)
        price = float(p.get("priceUsd") or 0)
        change = (p.get("priceChange") or {}).get("h24")
        if liquidity >= 100_000 and price > 0:
            valid_pairs.append({
                "price": price,
                "volume24h": volume,
                "liquidity": liquidity,
                "change24h": float(change) if change is not None else None,
                "url": p.get("url"),
            })

    if not valid_pairs:
        return None
    return max(valid_pairs, key=lambda x: x["liquidity"])


def market_update_is_relevant(market):
    """First update always; then only on >=2% price or >=50% volume change."""
    last_price = STATE.get("last_market_price")
    last_volume = STATE.get("last_market_volume")
    if not last_price or last_volume is None:
        return True

    price_change = abs(market["price"] - last_price) / last_price
    volume_change = abs(market["volume24h"] - last_volume) / max(last_volume, 1)
    return price_change >= 0.02 or volume_change >= 0.50


# ============================================================
# BLOCKCHAIN — burns and whales in one pass
# ============================================================

def rpc_providers():
    """(label, Web3) pairs: the ETH_RPC_URL secret first, then public RPCs."""
    providers = []
    secret = os.getenv("ETH_RPC_URL")
    if secret:
        providers.append(("ETH_RPC_URL", secret))
    for url in CONFIG.get("eth_rpc_urls", DEFAULT_RPC_URLS):
        providers.append((urlparse(url).hostname, url))
    return [
        (label, Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20})))
        for label, url in providers
    ]


def short_error(label, e):
    # The scan report is committed to a public repository. The ETH_RPC_URL
    # secret may carry an API key, and requests puts the URL path in its
    # messages ("url: /v3/<key>"), so for it only the error type is kept.
    if label == "ETH_RPC_URL":
        return type(e).__name__
    return re.sub(r"https?://\S+", "<url>", f"{type(e).__name__}: {e}")[:120]


def topic_addr(topic):
    return bytes(topic).hex()[-40:].lower()


def scan_chain():
    """Returns (burns, whales) from the blocks not yet scanned.

    Remembers the last scanned block in the state, so consecutive runs
    neither miss nor repeat blocks. If a chunk fails on every RPC, the scan
    stops there and the next run resumes from that point. A short report
    is kept in STATE["chain_scan"] (public: readable without logs access).
    """
    providers = rpc_providers()
    rpc_ok, rpc_errors = {}, {}

    def first_ok(call):
        for label, w3 in providers:
            try:
                result = call(w3)
            except Exception as e:
                rpc_errors[label] = short_error(label, e)
                continue
            rpc_ok[label] = rpc_ok.get(label, 0) + 1
            return result
        return None

    head = first_ok(lambda w3: w3.eth.block_number)
    if head is None:
        print("Ethereum RPC: every provider failed:", rpc_errors)
        STATE["chain_scan"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rpc_errors": rpc_errors,
        }
        return [], []

    latest = head - CONFIRMATIONS
    last = STATE.get("last_scanned_block")
    from_block = latest - FIRST_RUN_BLOCKS if last is None else last + 1
    from_block = max(from_block, latest - MAX_SCAN_BLOCKS)

    print(f"Chain scan: blocks {from_block}-{latest}")

    burns, whales = [], []
    total_logs = 0
    scanned_to = from_block - 1

    for start in range(from_block, latest + 1, CHUNK_SIZE):
        end = min(start + CHUNK_SIZE - 1, latest)
        logs = first_ok(lambda w3: w3.eth.get_logs({
            "fromBlock": start,
            "toBlock": end,
            "address": SHIB,
            "topics": [TRANSFER_TOPIC],
        }))
        if logs is None:
            print(f"Chunk {start}-{end} failed on every RPC: {rpc_errors}")
            break

        total_logs += len(logs)
        scanned_to = end

        for log in logs:
            if len(log["topics"]) < 3:
                continue
            amount = int.from_bytes(bytes(log["data"]), "big") / 10**18
            to_addr = topic_addr(log["topics"][2])
            tx = tx_hex(log["transactionHash"])

            if to_addr in BURN_ADDRESSES:
                if amount >= MIN_BURN_SHIB and tx not in STATE["seen_burn_tx"]:
                    burns.append({"tx": tx, "amount": amount})
            elif amount >= WHALE_THRESHOLD_SHIB and tx not in STATE["seen_whale_tx"]:
                whales.append({
                    "tx": tx,
                    "amount": amount,
                    "from": Web3.to_checksum_address("0x" + topic_addr(log["topics"][1])),
                    "to": Web3.to_checksum_address("0x" + to_addr),
                })

    STATE["last_scanned_block"] = scanned_to
    STATE["chain_scan"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "blocks": f"{from_block}-{scanned_to}",
        "complete": scanned_to >= latest,
        "logs": total_logs,
        "burns": len(burns),
        "whales": len(whales),
        "rpc_ok": rpc_ok,
        "rpc_errors": rpc_errors,
    }
    print(f"SHIB Transfer logs: {total_logs} | burns: {len(burns)} | whales: {len(whales)}")
    print(f"RPC calls ok: {rpc_ok} | errors: {rpc_errors}")

    burns.sort(key=lambda x: -x["amount"])
    whales.sort(key=lambda x: -x["amount"])
    return burns, whales


# ============================================================
# NEWS
# ============================================================

PRIORITY_TERMS = [
    "shiba inu", "$shib", "shibarium", "shib burn", "shib burns",
    "shib whale", "shiba whale", "shibaswap", "shib token",
    "shiba inu ecosystem",
]

BLOCKED_TERMS = [
    "price prediction", "price forecast", "price target",
    "best crypto buy", "better crypto buy", "should you buy",
    "should i buy", "will shib reach", "can shib reach", "forecast",
    "prediction", "price outlook", "breakout", "potential",
    "price increase", "price surge", "bullish", "bearish", "rally",
    "target", "millionaire", "100x", "1000x", "next shiba",
]

EVENT_TERMS = [
    "burn", "whale", "shibarium", "exchange", "listing", "delisting",
    "wallet", "transaction", "token", "launch", "upgrade", "update",
    "hack", "exploit", "partnership", "development",
]


def get_news():
    now = datetime.now(timezone.utc)
    max_age = CONFIG.get("news_max_age_hours", 48)
    candidates = {}

    for feed_cfg in CONFIG["rss_feeds"]:
        try:
            feed = feedparser.parse(feed_cfg["url"])
            print(f"News feed '{feed_cfg['name']}': {len(feed.entries)} entries")

            for item in feed.entries[:15]:
                link = item.get("link", "")
                title = re.sub(r"\s+", " ", item.get("title", "")).strip()
                if not title or not link:
                    continue

                title_lower = title.lower()
                if any(term in title_lower for term in BLOCKED_TERMS):
                    continue

                score = sum(10 for t in PRIORITY_TERMS if t in title_lower)
                score += sum(2 for t in EVENT_TERMS if t in title_lower)
                if score == 0:
                    continue

                # Same story from both feeds -> same title -> one candidate.
                uid = hashlib.sha256(link.encode()).hexdigest()
                title_uid = hashlib.sha256(title_lower.encode()).hexdigest()
                if uid in STATE["seen_news"] or title_uid in STATE["seen_news"]:
                    continue

                published = item.get("published_parsed") or item.get("updated_parsed")
                age_hours = None
                if published:
                    ts = datetime(*published[:6], tzinfo=timezone.utc)
                    age_hours = (now - ts).total_seconds() / 3600
                    if age_hours > max_age:
                        continue

                # Google News titles end in " - Publisher"; keep the
                # publisher as the source line.
                publisher = (item.get("source") or {}).get("title")
                if publisher and title.endswith(" - " + publisher):
                    title = title[: -len(" - " + publisher)].strip()

                candidates.setdefault(title_uid, {
                    "title": title,
                    "link": link,
                    "source": publisher or feed_cfg["name"],
                    "uids": [uid, title_uid],
                    "score": score,
                    "age_hours": age_hours,
                })

        except Exception as e:
            print(f"News feed error: {e}")

    return sorted(
        candidates.values(),
        key=lambda x: (-x["score"], x["age_hours"] if x["age_hours"] is not None else 9999),
    )


# ============================================================
# POST GENERATORS
# ============================================================

def make_burn_post(b):
    return (
        "🔥 SHIB BURN ALERT\n\n"
        f"{shib_number(b['amount'])} $SHIB sent to a burn address.\n\n"
        f"Tx: https://etherscan.io/tx/{b['tx']}\n\n"
        "#SHIB #ShibaInu #SHIBBurn"
    )


def make_whale_post(w, price=None):
    usd = f" (~${w['amount'] * price:,.0f})" if price else ""
    return (
        "🐋 SHIB WHALE MOVEMENT\n\n"
        f"{shib_number(w['amount'])} $SHIB{usd} transferred.\n\n"
        f"From: {short_addr(w['from'])}\n"
        f"To: {short_addr(w['to'])}\n\n"
        f"Tx: https://etherscan.io/tx/{w['tx']}\n\n"
        "#SHIB #ShibaInu #SHIBWhale"
    )


def make_news_post(n):
    tail = f"\n\nSource: {n['source']}\n{n['link']}\n\n#SHIB #ShibaInu #Shibarium"
    head = "📰 SHIB NEWS\n\n"
    room = THREADS_MAX_CHARS - len(head) - len(tail)
    title = n["title"] if len(n["title"]) <= room else n["title"][: room - 1].rstrip() + "…"
    return head + title + tail


def make_market_post(m):
    change = ""
    if m.get("change24h") is not None:
        arrow = "📈" if m["change24h"] >= 0 else "📉"
        change = f"24h change: {arrow} {m['change24h']:+.2f}%\n"
    return (
        "📊 SHIB MARKET UPDATE\n\n"
        f"Price: ${m['price']:.10f}\n"
        f"{change}"
        f"24h volume: ${m['volume24h']:,.0f}\n"
        f"Liquidity: ${m['liquidity']:,.0f}\n\n"
        f"Data: {m['url']}\n\n"
        "#SHIB #ShibaInu"
    )


# ============================================================
# THREADS API
# ============================================================

def threads_token():
    return (os.getenv("THREADS_ACCESS_TOKEN") or "").strip() or None


def threads_request(method, path, **params):
    params["access_token"] = threads_token()
    r = requests.request(method, f"{THREADS_API}/{path}", params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Threads {method} /{path}: {r.status_code} {r.text[:300]}")
    return r.json()


def threads_check():
    """Confirms the token works before trying to publish anything."""
    me = threads_request("GET", "me", fields="id,username")
    print(f"Threads account: @{me.get('username')} ({me.get('id')})")
    try:
        quota = threads_request(
            "GET", "me/threads_publishing_limit", fields="quota_usage,config"
        )["data"][0]
        print(f"Threads quota: {quota.get('quota_usage')}/"
              f"{(quota.get('config') or {}).get('quota_total')} posts in 24h")
    except Exception as e:
        print(f"Threads quota check skipped: {e}")


def threads_publish(text):
    """Two steps: create a TEXT container, then publish it."""
    container = threads_request("POST", "me/threads", media_type="TEXT", text=text)["id"]

    # Meta recommends waiting until the container is FINISHED before
    # publishing; text containers are normally ready within seconds.
    for _ in range(10):
        status = threads_request("GET", container, fields="status,error_message")
        if status.get("status") == "FINISHED":
            break
        if status.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Threads container {status}")
        time.sleep(3)

    post = threads_request("POST", "me/threads_publish", creation_id=container)
    print(f"Published on Threads: {post.get('id')}")
    return True


def publish(text):
    text = fit(text)
    if DRY_RUN:
        print("\n--- DRY RUN ---\n" + text + "\n--- END ---")
        return True
    try:
        return threads_publish(text)
    except Exception as e:
        print(f"Publish failed: {e}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    print(f"ShibRadar run — {'DRY RUN' if DRY_RUN else 'LIVE (Threads)'}")

    if not DRY_RUN:
        if not threads_token():
            raise SystemExit("THREADS_ACCESS_TOKEN is missing (GitHub Secret).")
        threads_check()

    # Each candidate: (priority, text, type, state identifiers)
    candidates = []
    market = None

    try:
        market = get_price()
    except Exception as e:
        print("Market scanner:", e)

    try:
        burns, whales = scan_chain()
        price = market["price"] if market else None
        candidates += [(1, make_burn_post(b), "burn", [b["tx"]]) for b in burns]
        candidates += [(2, make_whale_post(w, price), "whale", [w["tx"]]) for w in whales]
    except Exception as e:
        print("Chain scanner:", e)

    try:
        news = get_news()
        print("News found:", len(news))
        candidates += [(3, make_news_post(n), "news", n["uids"]) for n in news]
    except Exception as e:
        print("News scanner:", e)

    if market:
        if market_update_is_relevant(market):
            candidates.append((4, make_market_post(market), "market", []))
        else:
            print("Market update skipped: change not significant enough.")

    candidates.sort(key=lambda x: x[0])

    max_posts = int(CONFIG.get("max_posts_per_run", 3))
    max_per_type = CONFIG.get("max_posts_per_type", {"whale": 1, "news": 2, "market": 1})
    state_key = {"burn": "seen_burn_tx", "whale": "seen_whale_tx", "news": "seen_news"}

    sent = 0
    sent_by_type = {}

    for _, text, post_type, identifiers in candidates:
        if sent >= max_posts:
            break
        if sent_by_type.get(post_type, 0) >= max_per_type.get(post_type, max_posts):
            continue
        if already_posted(text):
            continue
        if not publish(text):
            continue

        remember_post(text)
        if post_type in state_key:
            STATE[state_key[post_type]].extend(identifiers)
        if post_type == "market":
            STATE["last_market_price"] = market["price"]
            STATE["last_market_volume"] = market["volume24h"]

        sent += 1
        sent_by_type[post_type] = sent_by_type.get(post_type, 0) + 1
        if not DRY_RUN:
            time.sleep(5)

    save_state()
    print("Completed:", sent, sent_by_type)


if __name__ == "__main__":
    main()
