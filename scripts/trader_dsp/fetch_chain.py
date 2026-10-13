"""On-chain record of one Polymarket proxy wallet from Polygon event logs (public RPC, eth_getLogs).

Queries (each a separate gzipped JSON-lines file under OUTDIR):
  fills_as_order_maker  exchange OrderFilled with maker = wallet  (every fill of the wallet's own orders; taker
                        orders appear with taker = exchange contract, resting orders with taker = counterparty)
  fills_vs_wallet_taker exchange OrderFilled with taker = wallet  (the resting orders the wallet's taker orders hit)
  pusd_in / pusd_out    pUSD (collateral) Transfer to / from the wallet
  usdce_in / usdce_out  USDC.e Transfer to / from the wallet
  usdc_in / usdc_out    native USDC Transfer to / from the wallet
  ctf_out_single/batch  CTF ERC-1155 TransferSingle / TransferBatch from the wallet (redemption burns, transfers out)
A range that errors or returns >= CAP logs is split in half.
"""
import gzip, json, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

import itertools, os, threading
RPCS = os.environ.get("RPCS", "https://polygon.gateway.tenderly.co").split(",")
MAXSPAN = int(os.environ.get("MAXSPAN", "400000"))      # public endpoints without a key cap eth_getLogs at 10k blocks
_rr = itertools.cycle(RPCS)
_lock = threading.Lock()
W, OUTDIR, B0, B1 = sys.argv[1].lower(), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
ONLY = sys.argv[5].split(",") if len(sys.argv) > 5 else None     # optional subset of the queries below
WT = "0x" + "0" * 24 + W[2:]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ORDER_FILLED = ["0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee",   # current exchange
                "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"]   # legacy CTF exchange
SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
PUSD, USDCE, USDC = ("0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb", "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                     "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359")
CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
Q = {
    "fills_as_order_maker": (None, [ORDER_FILLED, None, WT], 20000),
    "fills_vs_wallet_taker": (None, [ORDER_FILLED, None, None, WT], 20000),
    "pusd_in": (PUSD, [TRANSFER, None, WT], 400000),
    "pusd_out": (PUSD, [TRANSFER, WT], 20000),
    "usdce_in": (USDCE, [TRANSFER, None, WT], 400000),
    "usdce_out": (USDCE, [TRANSFER, WT], 400000),
    "usdc_in": (USDC, [TRANSFER, None, WT], 400000),
    "usdc_out": (USDC, [TRANSFER, WT], 400000),
    "ctf_out_single": (CTF, [SINGLE, None, WT], 200000),
    "ctf_out_batch": (CTF, [BATCH, None, WT], 200000),
    # pUSD leaving the wallet other than to the exchange: burns (to 0x0) and unwraps (to the pUSD contract)
    "pusd_out_burn_unwrap": (PUSD, [TRANSFER, WT, ["0x" + "0" * 64, "0x" + "0" * 24 + PUSD[2:]]], 400000),
}
CAP = 9000


def rpc(params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": [params]}).encode()
    with _lock:
        url = next(_rr)
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "tpb-research"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def logs(addr, topics, a, b, depth=0):
    p = {"fromBlock": hex(a), "toBlock": hex(b), "topics": topics}
    if addr:
        p["address"] = addr
    for k in range(30):
        try:
            d = rpc(p)
            if "result" in d:
                if len(d["result"]) >= CAP and b > a:
                    break
                return d["result"]
            err = str(d.get("error")).lower()
            if "rate" in err or "429" in err:
                time.sleep(min(60, 3 * (k + 1)))      # rate limited: wait, never split
                continue
            if "range" in err or "too many" in err or "exceed" in err or "results" in err:
                break                                   # result set too large: split
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(60, 3 * (k + 1)))
                continue
        except Exception:                              # noqa: BLE001
            pass
        time.sleep(1.5 * (k + 1))
    if b <= a:
        raise RuntimeError(f"cannot fetch block {a}")
    m = (a + b) // 2
    return logs(addr, topics, a, m, depth + 1) + logs(addr, topics, m + 1, b, depth + 1)


for name, (addr, topics, span) in Q.items():
    if ONLY and name not in ONLY:
        continue
    t0 = time.time()
    span = min(span, MAXSPAN)
    chunks = [(a, min(a + span - 1, B1)) for a in range(B0, B1 + 1, span)]
    n = 0
    with gzip.open(f"{OUTDIR}/{name}.jsonl.gz", "wt") as fh, ThreadPoolExecutor(int(os.environ.get('WORKERS', '2'))) as ex:
        for res in ex.map(lambda c: logs(addr, topics, *c), chunks):
            for x in res:
                fh.write(json.dumps({"block": int(x["blockNumber"], 16), "tx": x["transactionHash"],
                                     "li": int(x["logIndex"], 16), "addr": x["address"], "topics": x["topics"],
                                     "data": x["data"]}) + "\n")
                n += 1
    print(f"{name}: {n} logs from {len(chunks)} chunks in {time.time() - t0:.0f} s", flush=True)
print("done", flush=True)
