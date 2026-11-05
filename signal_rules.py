"""
Vegas 隧道信号规则（纯规则代码，不含通知 / 数据获取）

均线定义
  过滤线 = EMA12          （1H 图）
  中锋   = EMA144 / 169   （4H 图）
  红峰   = EMA576 / 676   （4H 图）

规则（多空对称）
  1. 触碰：价格影线碰到 4H 中锋/红峰区域 -> 进入观察期（不限时间）
  2. 触发：触碰那根 1H 不算；之后任一根 1H 收盘
       空：收盘在 1H 过滤线下方     多：收盘在 1H 过滤线上方
  3. 循环：出信号后本轮结束，下一次触碰开始新一轮
  4. 中锋、红峰同一根 1H 同向触发 -> 合并成一条信号

输入：两个 DataFrame，列 open_time(datetime) / open / high / low / close，
      只放【已收盘】的K线（把正在走的那根去掉）。
"""
import numpy as np
import pandas as pd

TUNNELS = {"中锋": (144, 169), "红峰": (576, 676)}
FILTER_SPAN = 12


def _ema(s: pd.Series, span: int) -> pd.Series:
    """EMA with the same SMA seed and recursive update used by V4.

    The first valid value is the arithmetic mean of the first `span`
    closes. Subsequent values use alpha=2/(span+1). Earlier positions
    remain NaN so signal warm-up boundaries are unchanged.
    """
    if span < 1:
        raise ValueError("EMA span must be a positive integer")
    values = s.to_numpy(dtype=np.float64, copy=False)
    result = np.full(len(values), np.nan, dtype=np.float64)
    if len(values) < span:
        return pd.Series(result, index=s.index, name=s.name)

    # Match V4's ordered sum / span initialization, not pandas ewm's
    # implicit first-observation seed.
    value = sum(values[:span]) / span
    result[span - 1] = value
    alpha = 2.0 / (span + 1.0)
    for index in range(span, len(values)):
        value = values[index] * alpha + value * (1.0 - alpha)
        result[index] = value
    return pd.Series(result, index=s.index, name=s.name)


def _paired_bounds(ea: pd.Series, eb: pd.Series):
    """A Vegas tunnel is valid only when both of its EMA boundaries are valid."""
    valid = ea.notna() & eb.notna()
    bounds = pd.concat([ea, eb], axis=1)
    return bounds.min(axis=1).where(valid), bounds.max(axis=1).where(valid)


def detect_signals(df_4h: pd.DataFrame, df_1h: pd.DataFrame, rearm: bool = True) -> pd.DataFrame:
    """
    返回 DataFrame：time(信号所在1H收盘时间) / direction(空|多，内部用) / tunnels(触发的隧道列表)
    rearm=True：出信号后，价格要先离开该隧道区域，再次触碰才开启新一轮观察期
    """
    d4 = df_4h.sort_values("open_time").reset_index(drop=True).copy()
    d1 = df_1h.sort_values("open_time").reset_index(drop=True).copy()

    # 4H 隧道区域（每根4H收盘后才可用，避免未来函数）
    d4["avail"] = d4["open_time"] + pd.Timedelta(hours=4)
    for name, (a, b) in TUNNELS.items():
        ea, eb = _ema(d4["close"], a), _ema(d4["close"], b)
        d4[f"{name}_lo"], d4[f"{name}_hi"] = _paired_bounds(ea, eb)

    # 1H 过滤线
    d1["ema12"] = _ema(d1["close"], FILTER_SPAN)

    # 每根 1H 对齐到「最近一根已收盘的 4H」的隧道区域
    zone_cols = ["avail"] + [f"{n}_{s}" for n in TUNNELS for s in ("lo", "hi")]
    m = pd.merge_asof(d1, d4[zone_cols], left_on="open_time", right_on="avail", direction="backward")

    high, low, close, ema12 = (m[c].to_numpy() for c in ("high", "low", "close", "ema12"))
    rows = []

    for name in TUNNELS:
        lo, hi = m[f"{name}_lo"].to_numpy(), m[f"{name}_hi"].to_numpy()
        state, armed = None, True  # state: None / "空" / "多"
        for i in range(1, len(m)):
            if np.isnan(lo[i]) or np.isnan(ema12[i]):
                continue
            touch = high[i] >= lo[i] and low[i] <= hi[i]

            if state is None:
                if touch and armed:
                    # 反弹触碰（从下方来）-> 空；回踩触碰（从上方来）-> 多
                    state = "空" if close[i - 1] < (lo[i] + hi[i]) / 2 else "多"
                    # 触碰这根 1H 本身不判定，从下一根开始
                elif not touch:
                    armed = True
            else:
                hit = close[i] < ema12[i] if state == "空" else close[i] > ema12[i]
                if hit:
                    rows.append({"time": m["open_time"].iloc[i] + pd.Timedelta(hours=1),
                                 "direction": state, "tunnel": name})
                    state = None
                    armed = not rearm

    if not rows:
        return pd.DataFrame(columns=["time", "direction", "tunnels"])

    out = (pd.DataFrame(rows)
           .groupby(["time", "direction"])["tunnel"].apply(list)
           .reset_index(name="tunnels")
           .sort_values("time").reset_index(drop=True))
    return out


def new_signals(df_4h, df_1h, since) -> pd.DataFrame:
    """实盘用：只取 since 之后的新信号（调用方自己记录上次处理到的时间）"""
    sig = detect_signals(df_4h, df_1h)
    return sig[sig["time"] > pd.Timestamp(since)].reset_index(drop=True)
