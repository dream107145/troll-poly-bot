# Walk-forward: re-fit, re-cost, re-select every day; trade the next day untouched

Fixed structure: {'structure': 'D', 'tp_offsets': (0.02,), 'tp_fracs': (1.0,), 'tp_fair': 0.03, 'time_exit_s': 30.0, 'stop': None, 'max_window_usdc': 10.0, 'max_unpaired_usdc': 10.0, 'max_dir_usdc': 10.0, 'max_open': 1, 'max_buys': 3}. Entry grid: k_sig x min_edge x agreement x band x limit offset = 72.

## BTC

| test day | train windows | entry cost | chosen entry (k, edge, agree, band, offset) | IS $/win | val $/win | OOS $/win | OOS net $ | OOS fills | OOS worst window | OOS hold-only $/win |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 09-25 | 233 | 0.037 | 1.5, 0.04, 0.34, 0.15-0.85, 0.03 | +0.272 | +0.101 | -0.023 | -5.06 | 109 | -3.86 | -0.005 |
| 09-26 | 447 | 0.030 | 1.5, 0.04, -1, 0.15-0.85, 0.03 | +0.143 | +0.048 | +0.173 | +36.18 | 50 | -3.91 | +0.186 |
| 09-27 | 672 | 0.035 | 2, 0, -1, 0.3-0.7, 0.02 | +0.118 | +0.128 | -0.013 | -3.28 | 69 | -3.67 | +0.010 |
| 09-28 | 881 | 0.038 | 2, 0.02, 0.34, 0.15-0.85, 0.02 | +0.137 | +0.159 | +0.033 | +7.32 | 74 | -4.11 | +0.028 |
| 09-29 | 1125 | 0.035 | 2, 0.02, -1, 0.3-0.7, 0.02 | +0.110 | +0.092 | -0.060 | -13.36 | 46 | -3.62 | -0.101 |
| 09-30 | 1346 | 0.033 | 1.5, 0.02, 0.34, 0.15-0.85, 0.02 | +0.107 | +0.137 | +0.097 | +21.92 | 78 | -4.06 | +0.105 |
| 10-01 | 1570 | 0.031 | 2, 0.02, -1, 0.15-0.85, 0.03 | +0.133 | +0.262 | -0.042 | -8.33 | 106 | -4.25 | +0.016 |

Out-of-sample, all test days: 1544 windows, net +35.39 USDC, +0.0229 per window (t +0.75), worst window -4.25; the same choices held to settlement instead: +0.0328 per window, net +50.63. (247 s)

