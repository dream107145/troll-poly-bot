# Exchange benchmark, part 1: coverage and settlement (archive)

11,330 venue-resolved 5-minute windows, 09-22 15:35 → 09-29 18:40 UTC (data/charts). Each exchange's price is the one the bot held when it sampled, about every 200 ms; a missing price means the exchange had no fresh quote then.

## 1. Coverage: share of samples with a live price

| asset | binance | bybit | coinbase |
|---|---:|---:|---:|
| BTC | 99.5% | 99.4% | 100.0% |
| ETH | 99.6% | 99.2% | 86.8% |
| SOL | 99.5% | 98.6% | 85.7% |
| XRP | 99.3% | 99.3% | 89.5% |
| DOGE | 99.4% | 97.6% | 42.4% |
| BNB | 99.1% | 95.0% | 13.8% |
| HYPE | 0.0% | 95.5% | 30.2% |

## 2. Settlement agreement: the venue's rule on each exchange's own price

UP iff the 60 s mean at the close ≥ the 60 s mean before the open. An exchange without a full 60 s of price at either end has no call for that window.

| asset | windows | binance | bybit | coinbase | composite |
|---|---:|---:|---:|---:|---:|
| BTC | 1,518 | 97.57% (1,481) | 97.64% (1,440) | 97.15% (1,508) | 97.89% (1,515) |
| ETH | 1,630 | 98.68% (1,595) | 98.58% (1,482) | 99.67% (301) | 98.77% (1,625) |
| SOL | 1,749 | 98.86% (1,497) | 98.19% (1,274) | 97.03% (236) | 98.77% (1,541) |
| XRP | 1,767 | 98.16% (1,253) | 98.42% (1,205) | 98.36% (428) | 98.27% (1,326) |
| DOGE | 1,785 | 98.60% (1,714) | 98.29% (1,284) | 100.00% (8) | 98.65% (1,782) |
| BNB | 1,727 | 98.65% (1,561) | 98.31% (769) | — | 98.44% (1,727) |
| HYPE | 1,154 | — | 96.19% (578) | 100.00% (2) | 96.44% (1,125) |
| **all** | 11,330 | 98.44% (9,101) | 98.08% (8,032) | 97.66% (2,483) | 98.26% (10,641) |

**Head to head on the 2,306 windows where all three have a call** (BTC, ETH, SOL, XRP almost entirely), by how close the composite's call was:

| |composite margin| | windows | binance | bybit | coinbase | composite |
|---|---:|---:|---:|---:|---:|
| 0–0.5 bps | 75 | 68.0% | 65.3% | 62.7% | 68.0% |
| 0.5–1 bps | 83 | 91.6% | 92.8% | 85.5% | 92.8% |
| 1–2 bps | 161 | 97.5% | 97.5% | 97.5% | 97.5% |
| 2–5 bps | 441 | 98.6% | 98.6% | 98.6% | 98.6% |
| ≥ 5 bps | 1,546 | 99.6% | 99.6% | 99.6% | 99.6% |
| all | 2,306 | 98.0% | 97.9% | 97.6% | 98.0% |

In the **53 windows where the three exchanges disagreed** about the outcome, each was right: binance 32 (60.4%), bybit 31 (58.5%), coinbase 23 (43.4%).

## 3. Against Chainlink (the settlement stream), 09-23 16:47 → 09-24 13:29 UTC

1,193 windows fall inside the Chainlink recording. The rule on Chainlink itself matches the venue in **1,183 (99.16%)** — the ceiling any exchange can reach.

Level basis is exchange − Chainlink at the opening 60 s mean (USDT pairs sit above a USD index). Margin error is the exchange's settlement margin (close mean vs open mean, bps) minus Chainlink's: the part of a call that can flip a close window.

| exchange | windows | level basis, median | margin error, median abs | p90 abs | sd | calls matching Chainlink |
|---|---:|---:|---:|---:|---:|---:|
| binance | 951 | +1.91 bps | 0.22 bps | 0.59 bps | 0.36 bps | 99.58% |
| bybit | 890 | +1.84 bps | 0.32 bps | 0.94 bps | 0.59 bps | 99.21% |
| coinbase | 225 | +0.36 bps | 0.25 bps | 0.69 bps | 0.40 bps | 98.67% |
| composite | 1,114 | +1.95 bps | 0.21 bps | 0.65 bps | 0.46 bps | 99.28% |

Margin error, median abs (bps), by asset:

| asset | binance | bybit | coinbase | composite |
|---|---:|---:|---:|---:|
| BTC | 0.18 (149) | 0.21 (144) | 0.27 (147) | 0.15 (149) |
| ETH | 0.22 (162) | 0.26 (157) | 0.24 (26) | 0.17 (162) |
| SOL | 0.25 (143) | 0.32 (124) | 0.25 (11) | 0.18 (144) |
| XRP | 0.23 (135) | 0.28 (130) | 0.27 (41) | 0.17 (138) |
| DOGE | 0.31 (185) | 0.42 (162) | — | 0.24 (190) |
| BNB | 0.17 (177) | 0.37 (74) | — | 0.22 (184) |
| HYPE | — | 0.68 (99) | — | 0.55 (147) |

**Timing against Chainlink.** Correlation of 1 s log returns, Chainlink second *s* against the exchange price as the bot held it at *s + lag* (receipt time), lag from −3 s to +3 s. A positive peak means Chainlink's timestamp runs ahead of our view of the exchange; a negative one, that Chainlink stamps a price the exchange already showed us earlier.

| asset | binance: peak lag / corr | bybit: peak lag / corr | coinbase: peak lag / corr |
|---|---|---|---|
| BTC | -200 ms / 0.74 | -400 ms / 0.80 | -400 ms / 0.76 |
| ETH | -300 ms / 0.73 | -400 ms / 0.75 | -400 ms / 0.71 |
| SOL | -400 ms / 0.77 | -400 ms / 0.75 | -400 ms / 0.71 |
| XRP | -400 ms / 0.81 | -400 ms / 0.78 | -300 ms / 0.74 |
| DOGE | -400 ms / 0.79 | -400 ms / 0.73 | -100 ms / 0.64 |
| BNB | -200 ms / 0.73 | -300 ms / 0.64 | -100 ms / 0.57 |
