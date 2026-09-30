import os
import json
import time
import hashlib
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from web3 import Web3


# ============================================================
# PATHS / CONFIG
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

CONFIG = json.loads(
    (ROOT / "config.json").read_text()
)

STATE_FILE = ROOT / "data" / "state.json"

STATE = json.loads(
    STATE_FILE.read_text()
)


# ============================================================
# SHIB / ETH CONFIG
# ============================================================

ETH_RPC = os.getenv(
    "ETH_RPC_URL",
    "https://ethereum-rpc.publicnode.com"
)

SHIB = Web3.to_checksum_address(
    "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE"
)

BURN_ADDRESSES = {
    Web3.to_checksum_address(
        "0x000000000000000000000000000000000000dead"
    ),
    Web3.to_checksum_address(
        "0x0000000000000000000000000000000000000000"
    )
}

TRANSFER_TOPIC = (
    "0x"
    + Web3.keccak(
        text="Transfer(address,address,uint256)"
    ).hex().removeprefix("0x")
)


# Minimum burn to report.
MIN_BURN_SHIB = int(
    CONFIG.get(
        "min_burn_shib",
        1_000_000
    )
)


# Minimum transfer to classify as a whale movement.
WHALE_THRESHOLD_SHIB = int(
    CONFIG.get(
        "whale_threshold_shib",
        1_000_000_000
    )
)


# ============================================================
# STATE
# ============================================================

STATE.setdefault(
    "seen_news",
    []
)

STATE.setdefault(
    "seen_burn_tx",
    []
)

STATE.setdefault(
    "seen_whale_tx",
    []
)

STATE.setdefault(
    "posted_hashes",
    [])


def save_state():

    STATE["last_run"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    STATE["seen_news"] = (
        STATE["seen_news"][-500:]
    )

    STATE["seen_burn_tx"] = (
        STATE["seen_burn_tx"][-500:]
    )

    STATE["seen_whale_tx"] = (
        STATE["seen_whale_tx"][-500:]
    )

    STATE["posted_hashes"] = (
        STATE["posted_hashes"][-500:]
    )

    STATE_FILE.write_text(
        json.dumps(
            STATE,
            indent=2
        )
    )


# ============================================================
# POST DEDUPLICATION
# ============================================================

def post_hash(text):

    return hashlib.sha256(
        text.strip()
        .lower()
        .encode()
    ).hexdigest()


def already_posted(text):

    return (
        post_hash(text)
        in STATE["posted_hashes"]
    )


def remember_post(text):

    STATE["posted_hashes"].append(
        post_hash(text)
    )


# ============================================================
# FORMATTING
# ============================================================

def shib_number(n):

    return f"{n:,.0f}"


# ============================================================
# MARKET DATA
# ============================================================

def get_price():

    token_address = (
        "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE"
    )

    r = requests.get(
        f"https://api.dexscreener.com/token-pairs/v1/ethereum/{token_address}",
        timeout=15
    )

    r.raise_for_status()

    pairs = r.json()

    if not isinstance(
        pairs,
        list
    ):
        return None

    valid_pairs = []

    for p in pairs:

        liquidity = float(
            (p.get("liquidity") or {}).get(
                "usd"
            )
            or 0
        )

        volume = float(
            (p.get("volume") or {}).get(
                "h24"
            )
            or 0
        )

        price = float(
            p.get("priceUsd")
            or 0
        )

        if (
            liquidity >= 100_000
            and price > 0
        ):

            valid_pairs.append({
                "price": price,
                "volume24h": volume,
                "liquidity": liquidity,
                "url": p.get("url")
            })

    if not valid_pairs:
        return None

    return max(
        valid_pairs,
        key=lambda x: x["liquidity"]
    )


# ============================================================
# BLOCKCHAIN
# ============================================================

def get_web3():

    w3 = Web3(
        Web3.HTTPProvider(
            ETH_RPC,
            request_kwargs={
                "timeout": 20
            }
        )
    )

    if not w3.is_connected():

        return None

    return w3


def decode_address(topic):

    topic_hex = bytes(
        topic
    ).hex()

    return Web3.to_checksum_address(
        "0x" + topic_hex[-40:]
    )


# ============================================================
# BURN SCANNER
# ============================================================

def get_burns():

    try:

        w3 = get_web3()

        if not w3:

            return []

        latest_block = (
            w3.eth.block_number
        )

        # Recent blocks only.
        scan_blocks = 5000

        # Keep RPC requests small.
        chunk_size = 500

        from_block = max(
            0,
            latest_block - scan_blocks
        )

        to_block = latest_block

        burns = []

        for start in range(
            from_block,
            to_block + 1,
            chunk_size
        ):

            end = min(
                start + chunk_size - 1,
                to_block
            )

            try:

                logs = w3.eth.get_logs({
                    "fromBlock": start,
                    "toBlock": end,
                    "address": SHIB,
                    "topics": [
                        TRANSFER_TOPIC
                    ]
                })

            except Exception as e:

                print(
                    f"Burn chunk "
                    f"{start}-{end}: {e}"
                )

                continue

            for log in logs:

                try:

                    if len(
                        log["topics"]
                    ) < 3:

                        continue

                    to_addr = decode_address(
                        log["topics"][2]
                    )

                    burn_addresses = {
                        x.lower()
                        for x in BURN_ADDRESSES
                    }

                    if (
                        to_addr.lower()
                        not in burn_addresses
                    ):

                        continue

                    tx_hash = log[
                        "transactionHash"
                    ].hex()

                    if (
                        tx_hash
                        in STATE["seen_burn_tx"]
                    ):

                        continue

                    amount = (
                        int(
                            log["data"],
                            16
                        )
                        / 10**18
                    )

                    if (
                        amount
                        < MIN_BURN_SHIB
                    ):

                        continue

                    burns.append({
                        "tx": tx_hash,
                        "amount": amount,
                        "block": log[
                            "blockNumber"
                        ]
                    })

                except Exception:

                    continue

        return burns

    except Exception as e:

        print(
            f"Burn scanner: {e}"
        )

        return []


# ============================================================
# WHALE SCANNER
# ============================================================

def get_whale_moves():

    try:

        w3 = get_web3()

        if not w3:

            return []

        latest_block = (
            w3.eth.block_number
        )

        # Recent blocks only.
        scan_blocks = 1000

        chunk_size = 250

        from_block = max(
            0,
            latest_block - scan_blocks
        )

        to_block = latest_block

        whales = []

        for start in range(
            from_block,
            to_block + 1,
            chunk_size
        ):

            end = min(
                start + chunk_size - 1,
                to_block
            )

            try:

                logs = w3.eth.get_logs({
                    "fromBlock": start,
                    "toBlock": end,
                    "address": SHIB,
                    "topics": [
                        TRANSFER_TOPIC
                    ]
                })

            except Exception as e:

                print(
                    f"Whale chunk "
                    f"{start}-{end}: {e}"
                )

                continue

            for log in logs:

                try:

                    if len(
                        log["topics"]
                    ) < 3:

                        continue

                    amount = (
                        int(
                            log["data"],
                            16
                        )
                        / 10**18
                    )

                    if (
                        amount
                        < WHALE_THRESHOLD_SHIB
                    ):

                        continue

                    tx_hash = log[
                        "transactionHash"
                    ].hex()

                    if (
                        tx_hash
                        in STATE["seen_whale_tx"]
                    ):

                        continue

                    from_addr = decode_address(
                        log["topics"][1]
                    )

                    to_addr = decode_address(
                        log["topics"][2]
                    )

                    burn_addresses = {
                        x.lower()
                        for x in BURN_ADDRESSES
                    }

                    # Burns are handled separately.
                    if (
                        to_addr.lower()
                        in burn_addresses
                    ):

                        continue

                    whales.append({
                        "tx": tx_hash,
                        "amount": amount,
                        "from": from_addr,
                        "to": to_addr,
                        "block": log[
                            "blockNumber"
                        ]
                    })

                except Exception:

                    continue

        return whales

    except Exception as e:

        print(
            f"Whale scanner: {e}"
        )

        return []


# ============================================================
# NEWS
# ============================================================

def get_news():

    now = datetime.now(timezone.utc)

    results = []

    priority_terms = [
        "shiba inu",
        "$shib",
        "shibarium",
        "shib burn",
        "shib burns",
        "shib whale",
        "shiba whale",
        "shibaswap",
        "shib token",
        "shiba inu ecosystem",
    ]

    blocked_terms = [
        "price prediction",
        "price forecast",
        "price target",
        "price prediction:",
        "best crypto buy",
        "better crypto buy",
        "should you buy",
        "should i buy",
        "will shib reach",
        "can shib reach",
        "forecast",
        "prediction",
        "price outlook",
        "breakout",
        "potential",
        "price increase",
        "price surge",
        "bullish",
        "bearish",
        "rally",
        "target",
    ]

    event_terms = [
        "burn",
        "whale",
        "shibarium",
        "exchange",
        "listing",
        "delisting",
        "wallet",
        "transaction",
        "token",
        "launch",
        "upgrade",
        "update",
        "hack",
        "exploit",
        "partnership",
        "development",
    ]

    candidates = []

    for feed_cfg in CONFIG["rss_feeds"]:

        try:

            feed = feedparser.parse(
                feed_cfg["url"]
            )

            print(
                f"News feed '{feed_cfg['name']}': "
                f"{len(feed.entries)} entries received"
            )

            for item in feed.entries[:15]:

                link = item.get(
                    "link",
                    ""
                )

                title = re.sub(
                    r"\s+",
                    " ",
                    item.get(
                        "title",
                        ""
                    )
                ).strip()

                if not title or not link:
                    continue

                title_lower = title.lower()

                # Ignore prediction/opinion articles.
                if any(
                    term in title_lower
                    for term in blocked_terms
                ):
                    continue

                score = 0

                for term in priority_terms:
                    if term in title_lower:
                        score += 10

                for term in event_terms:
                    if term in title_lower:
                        score += 2

                # Ignore articles without meaningful SHIB relevance.
                if score == 0:
                    continue

                uid = hashlib.sha256(
                    link.encode()
                ).hexdigest()

                if uid in STATE["seen_news"]:
                    continue

                published = item.get(
                    "published_parsed"
                )

                if not published:
                    published = item.get(
                        "updated_parsed"
                    )

                age_hours = None

                if published:

                    ts = datetime(
                        *published[:6],
                        tzinfo=timezone.utc
                    )

                    age_hours = (
                        now - ts
                    ).total_seconds() / 3600

                    if age_hours > CONFIG.get(
                        "news_max_age_hours",
                        48
                    ):
                        continue

                candidates.append({
                    "title": title,
                    "link": link,
                    "source": feed_cfg["name"],
                    "uid": uid,
                    "score": score,
                    "age_hours": age_hours
                })

        except Exception as e:

            print(
                f"News feed error: {e}"
            )

    candidates.sort(
        key=lambda x: (
            -x["score"],
            x["age_hours"]
            if x["age_hours"] is not None
            else 9999
        )
    )

    seen_uids = set()

    for item in candidates:

        if item["uid"] in seen_uids:
            continue

        seen_uids.add(
            item["uid"]
        )

        results.append(
            item
        )

    return results

# ============================================================
# POST GENERATORS
# ============================================================

def make_burn_post(b):

    return (
        "🔥 SHIB BURN ALERT\n\n"
        f"{shib_number(b['amount'])} "
        "$SHIB sent to a burn address.\n\n"
        "Transaction:\n"
        f"https://etherscan.io/tx/{b['tx']}\n\n"
        "#SHIB #ShibaInu #SHIBBurn"
    )


def make_whale_post(w):

    return (
        "🐋 SHIB WHALE MOVEMENT\n\n"
        f"{shib_number(w['amount'])} "
        "$SHIB transferred.\n\n"
        "From:\n"
        f"{w['from']}\n\n"
        "To:\n"
        f"{w['to']}\n\n"
        "Transaction:\n"
        f"https://etherscan.io/tx/{w['tx']}\n\n"
        "#SHIB #ShibaInu #SHIBWhale"
    )


def make_news_post(n):

    return (
        "📰 SHIB NEWS\n\n"
        f"{n['title']}\n\n"
        f"Source: {n['source']}\n"
        f"{n['link']}\n\n"
        "#SHIB #ShibaInu #Shibarium"
    )


def make_market_post(m):

    return (
        "📊 SHIB MARKET UPDATE\n\n"
        f"Price: ${m['price']:.10f}\n"
        f"24h volume: "
        f"${m['volume24h']:,.0f}\n\n"
        f"Data: {m['url']}\n\n"
        "#SHIB #ShibaInu"
    )


# ============================================================
# X API
# ============================================================

def x_access_token():

    refresh = os.getenv(
        "X_REFRESH_TOKEN"
    )

    client_id = os.getenv(
        "X_CLIENT_ID"
    )

    client_secret = os.getenv(
        "X_CLIENT_SECRET"
    )

    if not all([
        refresh,
        client_id,
        client_secret
    ]):

        return None

    r = requests.post(
        "https://api.x.com/2/oauth2/token",
        data={
            "refresh_token": refresh,
            "grant_type":
                "refresh_token",
            "client_id":
                client_id
        },
        auth=(
            client_id,
            client_secret
        ),
        timeout=20
    )

    r.raise_for_status()

    return r.json()[
        "access_token"
    ]


def publish(text):

    if os.getenv(
        "DRY_RUN",
        "true"
    ).lower() == "true":

        print(
            "\n--- DRY RUN ---\n"
            + text
            + "\n--- END ---"
        )

        return True

    token = x_access_token()

    if not token:

        print(
            "Missing X OAuth credentials."
        )

        return False

    r = requests.post(
        "https://api.x.com/2/tweets",
        headers={
            "Authorization":
                f"Bearer {token}",
            "Content-Type":
                "application/json"
        },
        json={
            "text": text
        },
        timeout=20
    )

    print(
        r.status_code,
        r.text[:500]
    )

    return r.ok


# ============================================================
# MAIN
# ============================================================

def main():

    candidates = []

    # --------------------------------------------------------
    # Burns
    # --------------------------------------------------------

    try:

        burns = get_burns()

        print(
            "Burns found:",
            len(burns)
        )

        for b in burns:

            candidates.append(
                (
                    1,
                    make_burn_post(b),
                    "burn",
                    b["tx"]
                )
            )

    except Exception as e:

        print(
            "Burn scanner:",
            e
        )

    # --------------------------------------------------------
    # Whales
    # --------------------------------------------------------

    try:

        whales = get_whale_moves()

        print(
            "Whale movements found:",
            len(whales)
        )

        for w in whales:

            candidates.append(
                (
                    2,
                    make_whale_post(w),
                    "whale",
                    w["tx"]
                )
            )

    except Exception as e:

        print(
            "Whale scanner:",
            e
        )

    # --------------------------------------------------------
    # News
    # --------------------------------------------------------

    try:

        news = get_news()

        print(
            "News found:",
            len(news)
        )

        for n in news:

            candidates.append(
                (
                    3,
                    make_news_post(n),
                    "news",
                    n["uid"]
                )
            )

    except Exception as e:

        print(
            "News scanner:",
            e
        )

    # --------------------------------------------------------
    # Market
    # --------------------------------------------------------

    try:

        m = get_price()

        if m:

            candidates.append(
                (
                    4,
                    make_market_post(m),
                    "market",
                    None
                )
            )

    except Exception as e:

        print(
            "Market scanner:",
            e
        )

    # --------------------------------------------------------
    # Sort by priority
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x: x[0]
    )

    sent = 0

    max_posts = int(
        CONFIG.get(
            "max_posts_per_run",
            3
        )
    )

    # --------------------------------------------------------
    # Publish
    # --------------------------------------------------------

    for (
        _,
        text,
        post_type,
        identifier
    ) in candidates:

        if sent >= max_posts:

            break

        if already_posted(text):

            continue

        if publish(text):

            remember_post(text)

            if (
                post_type == "burn"
                and identifier
            ):

                STATE[
                    "seen_burn_tx"
                ].append(
                    identifier
                )

            elif (
                post_type == "whale"
                and identifier
            ):

                STATE[
                    "seen_whale_tx"
                ].append(
                    identifier
                )

            elif (
                post_type == "news"
                and identifier
            ):

                STATE[
                    "seen_news"
                ].append(
                    identifier
                )

            sent += 1

            time.sleep(2)

    save_state()

    print(
        "Completed:",
        sent
    )


if __name__ == "__main__":
    main()
