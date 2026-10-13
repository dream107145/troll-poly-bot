# Resting-pair risk: variants on the bot's current rules

1972 traded BTC windows (of 1986); per-window net USDC

| variant | split | windows | net $ | per window | losing windows | worst window | 5 % worst avg |
|---|---|---:|---:|---:|---:|---:|---:|
| leg only, no pair | train | 980 | -235.9 | -0.241 | 33% | -50.75 | -37.43 |
| leg only, no pair | validation | 396 | +403.2 | +1.018 | 31% | -50.33 | -34.91 |
| leg only, no pair | test | 298 | +692.6 | +2.324 | 30% | -50.75 | -50.00 |
| leg only, no pair | forward | 298 | -67.7 | -0.227 | 36% | -51.10 | -47.03 |
| pair < 0.95 (now) | train | 980 | -1276.2 | -1.302 | 34% | -30.36 | -15.35 |
| pair < 0.95 (now) | validation | 396 | -489.2 | -1.235 | 36% | -21.42 | -15.71 |
| pair < 0.95 (now) | test | 298 | -341.6 | -1.146 | 35% | -50.13 | -24.71 |
| pair < 0.95 (now) | forward | 298 | -509.9 | -1.711 | 36% | -35.94 | -22.57 |
| pair < 0.97 | train | 980 | -1399.0 | -1.428 | 32% | -26.86 | -13.97 |
| pair < 0.97 | validation | 396 | -569.1 | -1.437 | 34% | -19.61 | -14.56 |
| pair < 0.97 | test | 298 | -443.9 | -1.490 | 34% | -50.13 | -22.47 |
| pair < 0.97 | forward | 298 | -538.9 | -1.808 | 34% | -32.14 | -19.11 |
| pair < 0.99 | train | 980 | -1432.7 | -1.462 | 29% | -18.95 | -11.49 |
| pair < 0.99 | validation | 396 | -618.1 | -1.561 | 30% | -20.41 | -12.57 |
| pair < 0.99 | test | 298 | -566.6 | -1.901 | 28% | -50.13 | -20.56 |
| pair < 0.99 | forward | 298 | -580.1 | -1.947 | 32% | -24.19 | -16.38 |
| pair < 0.95 + one naked leg at a time | train | 980 | -462.8 | -0.472 | 26% | -4.15 | -3.83 |
| pair < 0.95 + one naked leg at a time | validation | 396 | -188.8 | -0.477 | 27% | -4.08 | -3.84 |
| pair < 0.95 + one naked leg at a time | test | 298 | -130.5 | -0.438 | 29% | -4.05 | -3.78 |
| pair < 0.95 + one naked leg at a time | forward | 298 | -131.4 | -0.441 | 28% | -4.03 | -3.78 |
| pair < 0.95 + stop 0.10 | train | 980 | -957.1 | -0.977 | 58% | -13.65 | -6.73 |
| pair < 0.95 + stop 0.10 | validation | 396 | -598.5 | -1.511 | 67% | -9.56 | -7.77 |
| pair < 0.95 + stop 0.10 | test | 298 | -687.2 | -2.306 | 62% | -50.13 | -15.47 |
| pair < 0.95 + stop 0.10 | forward | 298 | -484.4 | -1.625 | 70% | -17.09 | -8.89 |
| pair < 0.95 + stop 0.15 | train | 980 | -1023.1 | -1.044 | 53% | -15.66 | -8.51 |
| pair < 0.95 + stop 0.15 | validation | 396 | -591.1 | -1.493 | 62% | -12.24 | -8.93 |
| pair < 0.95 + stop 0.15 | test | 298 | -657.9 | -2.208 | 59% | -50.13 | -16.73 |
| pair < 0.95 + stop 0.15 | forward | 298 | -533.0 | -1.789 | 65% | -20.29 | -10.97 |
| pair < 0.95 + stop 0.20 | train | 980 | -1065.0 | -1.087 | 48% | -18.10 | -10.04 |
| pair < 0.95 + stop 0.20 | validation | 396 | -598.2 | -1.511 | 55% | -14.29 | -10.29 |
| pair < 0.95 + stop 0.20 | test | 298 | -600.7 | -2.016 | 50% | -50.13 | -18.60 |
| pair < 0.95 + stop 0.20 | forward | 298 | -530.6 | -1.780 | 57% | -20.54 | -12.52 |
| pair < 0.95 + stop 0.30 | train | 980 | -1128.5 | -1.152 | 43% | -24.00 | -11.67 |
| pair < 0.95 + stop 0.30 | validation | 396 | -517.6 | -1.307 | 46% | -17.70 | -11.80 |
| pair < 0.95 + stop 0.30 | test | 298 | -577.2 | -1.937 | 44% | -50.13 | -21.48 |
| pair < 0.95 + stop 0.30 | forward | 298 | -501.4 | -1.683 | 51% | -22.32 | -15.44 |
| pair < 0.95 + chase 0.10 | train | 980 | -957.1 | -0.977 | 58% | -13.65 | -6.73 |
| pair < 0.95 + chase 0.10 | validation | 396 | -598.5 | -1.511 | 67% | -9.56 | -7.77 |
| pair < 0.95 + chase 0.10 | test | 298 | -687.2 | -2.306 | 62% | -50.13 | -15.47 |
| pair < 0.95 + chase 0.10 | forward | 298 | -484.4 | -1.625 | 70% | -17.09 | -8.89 |
| pair < 0.95 + chase 0.15 | train | 980 | -1023.1 | -1.044 | 53% | -15.66 | -8.51 |
| pair < 0.95 + chase 0.15 | validation | 396 | -591.1 | -1.493 | 62% | -12.24 | -8.93 |
| pair < 0.95 + chase 0.15 | test | 298 | -657.9 | -2.208 | 59% | -50.13 | -16.73 |
| pair < 0.95 + chase 0.15 | forward | 298 | -533.0 | -1.789 | 65% | -20.29 | -10.97 |
| pair < 0.95 + chase 0.20 | train | 980 | -1065.0 | -1.087 | 48% | -18.10 | -10.04 |
| pair < 0.95 + chase 0.20 | validation | 396 | -598.2 | -1.511 | 55% | -14.29 | -10.29 |
| pair < 0.95 + chase 0.20 | test | 298 | -600.7 | -2.016 | 50% | -50.13 | -18.60 |
| pair < 0.95 + chase 0.20 | forward | 298 | -530.6 | -1.780 | 57% | -20.54 | -12.52 |
| pair < 0.95 + chase 0.30 | train | 980 | -1128.5 | -1.152 | 43% | -24.00 | -11.67 |
| pair < 0.95 + chase 0.30 | validation | 396 | -517.6 | -1.307 | 46% | -17.70 | -11.80 |
| pair < 0.95 + chase 0.30 | test | 298 | -577.2 | -1.937 | 44% | -50.13 | -21.48 |
| pair < 0.95 + chase 0.30 | forward | 298 | -501.4 | -1.683 | 51% | -22.32 | -15.44 |
| pair < 0.95 + wait + stop 0.10 | train | 980 | -630.9 | -0.644 | 60% | -7.20 | -4.15 |
| pair < 0.95 + wait + stop 0.10 | validation | 396 | -387.5 | -0.979 | 70% | -8.59 | -4.79 |
| pair < 0.95 + wait + stop 0.10 | test | 298 | -385.8 | -1.295 | 70% | -10.18 | -6.44 |
| pair < 0.95 + wait + stop 0.10 | forward | 298 | -300.0 | -1.007 | 72% | -6.50 | -4.83 |
| pair < 0.95 + wait + stop 0.20 | train | 980 | -535.6 | -0.547 | 49% | -10.02 | -4.60 |
| pair < 0.95 + wait + stop 0.20 | validation | 396 | -320.1 | -0.808 | 58% | -6.98 | -4.92 |
| pair < 0.95 + wait + stop 0.20 | test | 298 | -271.3 | -0.910 | 60% | -8.78 | -6.20 |
| pair < 0.95 + wait + stop 0.20 | forward | 298 | -235.5 | -0.790 | 59% | -6.50 | -4.79 |
| variant | split | net $ | per window | losing windows | worst window | 5 % worst avg |
|---|---|---:|---:|---:|---:|---:|
| leg only, cap 5 USDC/window | train | +34.2 | +0.035 | 34% | -5.17 | -3.86 |
| leg only, cap 5 USDC/window | validation | +33.7 | +0.085 | 35% | -4.01 | -3.67 |
| leg only, cap 5 USDC/window | test | +77.2 | +0.259 | 34% | -5.17 | -3.74 |
| leg only, cap 5 USDC/window | forward | +21.7 | +0.073 | 35% | -5.17 | -3.69 |
| pair < 0.95, cap 5 USDC/window | train | -247.3 | -0.252 | 14% | -5.17 | -3.67 |
| pair < 0.95, cap 5 USDC/window | validation | -81.4 | -0.206 | 13% | -4.01 | -3.47 |
| pair < 0.95, cap 5 USDC/window | test | -78.6 | -0.264 | 14% | -5.17 | -3.65 |
| pair < 0.95, cap 5 USDC/window | forward | -52.8 | -0.177 | 12% | -4.01 | -3.39 |
| leg only, cap 10 USDC/window | train | +128.0 | +0.131 | 34% | -10.23 | -9.69 |
| leg only, cap 10 USDC/window | validation | +126.5 | +0.320 | 34% | -10.23 | -9.72 |
| leg only, cap 10 USDC/window | test | +188.4 | +0.632 | 34% | -10.23 | -9.93 |
| leg only, cap 10 USDC/window | forward | +17.6 | +0.059 | 36% | -10.23 | -9.86 |
| pair < 0.95, cap 10 USDC/window | train | -501.0 | -0.511 | 21% | -10.23 | -7.59 |
| pair < 0.95, cap 10 USDC/window | validation | -185.7 | -0.469 | 21% | -9.45 | -7.87 |
| pair < 0.95, cap 10 USDC/window | test | -160.8 | -0.539 | 20% | -9.65 | -8.89 |
| pair < 0.95, cap 10 USDC/window | forward | -177.0 | -0.594 | 23% | -10.13 | -7.90 |
| leg only, cap 20 USDC/window | train | +45.8 | +0.047 | 34% | -20.47 | -19.29 |
| leg only, cap 20 USDC/window | validation | +289.1 | +0.730 | 32% | -20.36 | -19.32 |
| leg only, cap 20 USDC/window | test | +325.4 | +1.092 | 33% | -20.07 | -19.56 |
| leg only, cap 20 USDC/window | forward | +52.0 | +0.175 | 36% | -20.46 | -19.26 |
| pair < 0.95, cap 20 USDC/window | train | -819.5 | -0.836 | 28% | -20.47 | -11.54 |
| pair < 0.95, cap 20 USDC/window | validation | -325.5 | -0.822 | 28% | -17.81 | -12.62 |
| pair < 0.95, cap 20 USDC/window | test | -274.3 | -0.920 | 27% | -18.80 | -14.73 |
| pair < 0.95, cap 20 USDC/window | forward | -283.2 | -0.950 | 31% | -19.63 | -13.06 |
