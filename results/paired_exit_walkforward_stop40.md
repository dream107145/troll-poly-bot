# Walk-forward: re-fit, re-cost, re-select every day; trade the next day untouched

Fixed structure: {'structure': 'D', 'tp_offsets': (0.02,), 'tp_fracs': (1.0,), 'tp_fair': 0.03, 'time_exit_s': 30.0, 'stop': 0.4, 'max_window_usdc': 10.0, 'max_unpaired_usdc': 10.0, 'max_dir_usdc': 10.0, 'max_open': 1, 'max_buys': 3}. Entry grid: k_sig x min_edge x agreement x band x limit offset = 72.

## BTC

| test day | train windows | entry cost | chosen entry (k, edge, agree, band, offset) | IS $/win | val $/win | OOS $/win | OOS net $ | OOS fills | OOS worst window | OOS hold-only $/win |
|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 09-25 | 233 | 0.037 | 1.5, 0.04, 0.34, 0.15-0.85, 0.03 | +0.227 | +0.157 | -0.086 | -19.34 | 114 | -4.58 | -0.066 |
| 09-26 | 447 | 0.030 | 1.5, 0.04, 0.34, 0.15-0.85, 0.02 | +0.139 | +0.037 | +0.100 | +21.00 | 28 | -2.29 | +0.123 |
| 09-27 | 672 | 0.035 | 2, 0.04, 0.34, 0.15-0.85, 0.03 | +0.077 | +0.104 | +0.020 | +4.94 | 45 | -3.16 | +0.042 |
| 09-28 | 881 | 0.038 | 1.5, 0.02, 0.34, 0.15-0.85, 0.02 | +0.109 | +0.109 | +0.040 | +8.76 | 102 | -3.82 | +0.043 |
| 09-29 | 1125 | 0.035 | 2, 0.02, -1, 0.3-0.7, 0.02 | +0.078 | +0.053 | -0.107 | -23.90 | 46 | -4.95 | -0.163 |
| 09-30 | 1346 | 0.033 | 1.5, 0.04, -1, 0.15-0.85, 0.02 | +0.060 | +0.063 | +0.020 | +4.50 | 12 | -2.58 | +0.036 |
| 10-01 | 1570 | 0.031 | 2, 0.02, 0.34, 0.15-0.85, 0.02 | +0.079 | +0.168 | -0.103 | -20.29 | 87 | -3.20 | -0.092 |

Out-of-sample, all test days: 1544 windows, net -24.33 USDC, -0.0158 per window (t -0.65), worst window -4.95; the same choices held to settlement instead: -0.0102 per window, net -15.75. (297 s)

