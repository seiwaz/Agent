"""Playbook: four rule-based 1h strategies (Donchian 48/24 breakout, EMA 50 pullback, liquidity
sweep + structure + FVG / OB, anchored-VWAP pullback), long and short, drawn on the chart with
the current setup, every past setup to its target / stop, and a cost-aware backtest.

Source of the rules: the user's guide "trading_1h_guide_fa" (2026-10-09); where the guide leaves
a choice open, the mechanical reading used here is written next to the rule (docs/PLAYBOOK.md).
"""
