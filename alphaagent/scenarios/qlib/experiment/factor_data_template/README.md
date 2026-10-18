# How to read files.
For example, if you want to read `filename.h5`
```Python
import pandas as pd
df = pd.read_hdf("filename.h5", key="data")
```
NOTE: **key is always "data" for all hdf5 files **.

# Here is a short description about the data

| Filename       | Description                                                      |
| -------------- | -----------------------------------------------------------------|
| "daily_pv.h5"  | Adjusted daily price, volume and QuantDB pre-computed features.  |


# For different data, We have some basic knowledge for them

## Daily data variables
$open: open price of the stock on that day.
$close: close price of the stock on that day.
$high: high price of the stock on that day.
$low: low price of the stock on that day.
$volume: volume of the stock on that day.
$amount: turnover (成交额) of the stock on that day.
$return: daily return of the stock on that day.

## QuantDB pre-computed features (already available as columns, directly usable in expressions)
These columns are pre-computed by QuantDB and aligned to the same (datetime, instrument)
index as the price columns. You can reference them directly in factor expressions, e.g.
`$rsi_14`, `$turn_20`, `$netflow_5`. They are read-only inputs (do NOT overwrite them).

### Technical indicators / volatility
$rsi_14: 14-day RSI (相对强弱指标).
$macd_hist: MACD histogram (DIF - DEA).
$atr_14: 14-day ATR (平均真实波幅).
$beta_20: 20-day beta against the market.
$parkinson_20: 20-day Parkinson volatility (high-low estimator).
$bb_width: Bollinger band width (布林带带宽).
$bb_pos: position of close within the Bollinger band (0=lower, 1=upper).
$adx_14: 14-day ADX (趋势强度).
$maxdd_20: rolling 20-day maximum drawdown.
$idio_vol_20: 20-day idiosyncratic volatility (style-residual).
$obv_slope: 20-day slope of OBV (能量潮).

### Momentum / turnover / liquidity
$turn_5: 5-day average turnover rate (换手率).
$turn_20: 20-day average turnover rate.
$turn_z_20: 20-day z-score of turnover rate.
$mfi_14: 14-day Money Flow Index (资金流强弱).
$netflow_5: 5-day net money flow (主力净流入近似).
$netflow_20: 20-day net money flow.

### Valuation / fundamentals
$pe_ttm: trailing-twelve-month PE ratio.
$pb: price-to-book ratio.
$ps_ttm: trailing-twelve-month PS ratio.
$div_yield: dividend yield (股息率).
$ep: earnings yield (1/PE).
$bp: book-to-price (1/PB).
$roe: return on equity.
$peg: PEG ratio.
$np_growth: net profit growth.
$np_ttm: trailing-twelve-month net profit.
$total_mv: total market capitalization.
$float_mv: free-float market capitalization.

### Chips / industry / concept
$chip_profit_20: 20-day chip profit ratio (筹码获利比例).
$ind_strength: industry relative strength (行业强度).
$concept_hot: concept hotness score (概念热度).

> Note: all features use past/current information only (no look-ahead). Missing values
> may appear for suspended or newly listed stocks; handle NaN in your factor code.
