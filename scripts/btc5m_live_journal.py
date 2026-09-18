#!/usr/bin/env python3
"""Rebuild local live-trade journal (markdown + json) from skill runtime logs."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests


UTC = timezone.utc


def parse_json_objects(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur: list[str] = []
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        if depth > 0:
            cur.append(ch)
        if ch == '}' and depth > 0:
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(''.join(cur))
                except Exception:
                    obj = None
                cur = []
                if isinstance(obj, dict):
                    out.append(obj)
    return out


def fnum(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return ''
    try:
        return f'{float(v):.{digits}f}'
    except Exception:
        return str(v)


def fetch_winner(slug: str) -> dict[str, Any]:
    info = {'winner': '', 'prices': '', 'closed': None, 'end': ''}
    try:
        r = requests.get('https://gamma-api.polymarket.com/events', params={'slug': slug}, timeout=12)
        r.raise_for_status()
        arr = r.json()
        if not arr:
            return info
        m = (arr[0].get('markets') or [{}])[0]
        prices = m.get('outcomePrices')
        if isinstance(prices, str):
            prices = json.loads(prices)
        info['prices'] = json.dumps(prices) if not isinstance(prices, str) else prices
        info['closed'] = m.get('closed')
        info['end'] = str(m.get('endDate') or '')
        if isinstance(prices, list) and len(prices) >= 2:
            up, dn = float(prices[0]), float(prices[1])
            if up > dn:
                info['winner'] = 'UP'
            elif dn > up:
                info['winner'] = 'DOWN'
            else:
                info['winner'] = 'TIE'
    except Exception:
        pass
    return info


def note_for(row: dict[str, Any]) -> str:
    side = row.get('side') or ''
    winner = row.get('settlement_winner') or ''
    reason = str(row.get('close_reason') or '')
    sl = fnum(row.get('stop_loss_pct')) or 0
    entry = fnum(row.get('entry_price')) or 0
    last = fnum(row.get('last_side_price'))
    pnl = fnum(row.get('realized_pnl_usdc'))
    ok = row.get('close_success') is True

    bits: list[str] = []
    if sl >= 0.8:
        bits.append('hold/90%SL')
    elif sl > 0:
        bits.append(f'{int(round(sl * 100))}%SL')

    if winner in ('UP', 'DOWN'):
        if winner == side:
            bits.append('结算方向对')
        else:
            bits.append('结算方向错')

    if 'stop_loss' in reason:
        if winner == side:
            bits.append('方向对但被止损砍掉')
        else:
            bits.append('中途止损')
    elif 'clob_bid_floor' in reason:
        if ok:
            bits.append('CLOB买价地板卖掉')
        else:
            bits.append('CLOB买价地板触发但没卖掉')
    elif 'time_exit' in reason:
        if ok and pnl is not None and pnl >= 0.5:
            if last is not None and last < 0.75 and entry >= 0.68:
                bits.append('假摔后收到0.99附近')
            elif entry >= 0.95:
                bits.append('买太贵，利润很薄')
            else:
                bits.append('收盘附近卖掉')
        elif ok and pnl is not None and abs(pnl) < 0.05:
            bits.append('买在0.99附近，接近打平')
        elif ok and pnl is not None and pnl < 0:
            bits.append('到期前卖掉仍亏')
        elif not ok:
            if 'hold_to_settlement' in reason:
                if winner == side:
                    bits.append('赢盘拿到结算吃满')
                else:
                    bits.append('到期前像赢盘选择吃满，结算翻了')
            else:
                bits.append('平仓失败，输方token未卖掉')

    if not ok and winner and winner != side:
        bits.append('约亏掉本金')
    return '；'.join(bits)


def collect_sessions(runtime_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    logs = sorted(runtime_dir.glob('btc5m_*.log'))
    for log in logs:
        text = log.read_text(encoding='utf-8', errors='ignore')
        for obj in parse_json_objects(text):
            opened = obj.get('opened')
            if not isinstance(opened, dict) or not opened.get('opened_at'):
                continue
            closed = obj.get('closed') if isinstance(obj.get('closed'), dict) else {}
            params = obj.get('params') if isinstance(obj.get('params'), dict) else {}
            key = '|'.join(
                [
                    str(opened.get('opened_at')),
                    str(opened.get('market_slug') or ''),
                    str(opened.get('side') or ''),
                    str(opened.get('open_order_id') or opened.get('open_tx') or ''),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            cost = fnum(opened.get('cost_usdc'))
            close_usdc = fnum(closed.get('close_usdc'))
            pnl = fnum(obj.get('realized_cashflow_pnl_usdc'))
            if pnl is None and closed.get('close_success') is True and cost is not None and close_usdc is not None:
                pnl = round(close_usdc - cost, 6)
            rows.append(
                {
                    'log': log.name,
                    'opened_at': opened.get('opened_at'),
                    'closed_at': closed.get('closed_at') or obj.get('finished_at'),
                    'market_slug': opened.get('market_slug'),
                    'side': opened.get('side'),
                    'entry_price': fnum(opened.get('entry_price')),
                    'shares': fnum(opened.get('shares')),
                    'cost_usdc': cost,
                    'stop_loss_pct': fnum(params.get('stop_loss_pct')),
                    'stop_loss_price': fnum(obj.get('stop_loss_price')),
                    'last_side_price': fnum(obj.get('last_side_price')),
                    'close_reason': closed.get('close_reason') or '',
                    'close_success': closed.get('close_success'),
                    'close_usdc': close_usdc,
                    'realized_pnl_usdc': pnl,
                    'open_tx': opened.get('open_tx') or '',
                    'close_tx': closed.get('close_tx') or '',
                    'threshold': fnum(params.get('threshold')),
                    'stake_usd': fnum(params.get('stake_usd')),
                    'profile': params.get('profile') or '',
                }
            )
    rows.sort(key=lambda r: str(r.get('opened_at') or ''))
    return rows


def enrich_settlement(rows: list[dict[str, Any]]) -> None:
    cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        slug = str(row.get('market_slug') or '')
        if slug and slug not in cache:
            cache[slug] = fetch_winner(slug)
        info = cache.get(slug) or {}
        row['settlement_winner'] = info.get('winner') or ''
        row['settlement_prices'] = info.get('prices') or ''
        row['market_closed'] = info.get('closed')
        shares = fnum(row.get('shares')) or 0.0
        cost = fnum(row.get('cost_usdc')) or 0.0
        winner = row['settlement_winner']
        side = row.get('side')
        if winner in ('UP', 'DOWN') and shares > 0:
            row['hold_to_settlement_pnl_usdc'] = round((shares * 1.0 - cost) if winner == side else (0.0 - cost), 6)
        else:
            row['hold_to_settlement_pnl_usdc'] = None
        row['note'] = note_for(row)


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'updated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'trade_count': len(rows),
        'trades': rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_md(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%SZ')
    realized = [fnum(r.get('realized_pnl_usdc')) for r in rows]
    realized_ok = [x for x in realized if x is not None]
    hold = [fnum(r.get('hold_to_settlement_pnl_usdc')) for r in rows]
    hold_ok = [x for x in hold if x is not None]
    sl25 = [r for r in rows if (fnum(r.get('stop_loss_pct')) or 0) < 0.5]
    holdm = [r for r in rows if (fnum(r.get('stop_loss_pct')) or 0) >= 0.5]
    wins = [r for r in rows if (fnum(r.get('realized_pnl_usdc')) or 0) > 0.05 and r.get('close_success') is True]
    losses = [
        r
        for r in rows
        if (r.get('close_success') is not True)
        or ((fnum(r.get('realized_pnl_usdc')) is not None) and (fnum(r.get('realized_pnl_usdc')) or 0) < -0.05)
    ]

    def sum_pnl(group: list[dict[str, Any]]) -> float:
        return round(sum((fnum(r.get('realized_pnl_usdc')) or 0) for r in group if r.get('close_success') is True), 4)

    lines: list[str] = []
    lines.append('# BTC 5m 实盘交易记录')
    lines.append('')
    lines.append(f'更新时间（UTC）：{now}')
    lines.append('来源：`runtime/btc5m_*.log` 会话 JSON + Gamma 结算价。不含私钥/地址。')
    lines.append('机器可读副本：`data/live_trades.json`。循环每结束一笔会自动重写本文件。')
    lines.append('')
    lines.append('## 汇总')
    lines.append('')
    lines.append(f'- 总笔数：{len(rows)}')
    lines.append(f'- 有平仓现金流的已实现盈亏合计：{round(sum(realized_ok), 4) if realized_ok else 0} USDC（{len(realized_ok)} 笔有数字）')
    lines.append(f'- 若全部拿到结算的对照盈亏：{round(sum(hold_ok), 4) if hold_ok else 0} USDC')
    lines.append(f'- 25% 止损样本：{len(sl25)} 笔，已实现约 {sum_pnl(sl25)} USDC')
    lines.append(f'- 90% 止损/扛到收盘样本：{len(holdm)} 笔，已实现约 {sum_pnl(holdm)} USDC（平仓失败的输单现金已在买入时扣掉，日志 realized 可能为空）')
    lines.append(f'- 明显盈利笔数：{len(wins)}；明显亏损/未卖掉输单：{len(losses)}')
    lines.append('')
    lines.append('## 逐笔')
    lines.append('')
    lines.append(
        '| # | 开仓UTC | 窗口 | 方向 | 入场 | 份数 | 成本 | SL% | 平仓原因 | 卖回 | 已实现 | 结算 | 拿到结算对照 | 成功平仓 | 备注 |'
    )
    lines.append('|---:|---|---|---|---:|---:|---:|---:|---|---:|---:|---|---:|---|---|')
    for i, r in enumerate(rows, 1):
        slug = str(r.get('market_slug') or '')
        slot = slug.replace('btc-updown-5m-', '')
        opened = str(r.get('opened_at') or '')[:19].replace('T', ' ')
        lines.append(
            '| '
            + ' | '.join(
                [
                    str(i),
                    opened,
                    slot,
                    str(r.get('side') or ''),
                    fmt(r.get('entry_price'), 2),
                    fmt(r.get('shares'), 4),
                    fmt(r.get('cost_usdc'), 4),
                    fmt((fnum(r.get('stop_loss_pct')) or 0) * 100, 0),
                    str(r.get('close_reason') or ''),
                    fmt(r.get('close_usdc'), 4),
                    fmt(r.get('realized_pnl_usdc'), 4),
                    str(r.get('settlement_winner') or ''),
                    fmt(r.get('hold_to_settlement_pnl_usdc'), 4),
                    'yes' if r.get('close_success') is True else 'no',
                    str(r.get('note') or '').replace('|', '/'),
                ]
            )
            + ' |'
        )
    lines.append('')
    lines.append('## 字段说明')
    lines.append('')
    lines.append('- 已实现：卖回 USDC − 买入成本；平仓失败时为空，现金通常已在开仓时减少约 5 USDC。')
    lines.append('- 拿到结算对照：若持有到结算，$1 或 $0，不经过盘口卖出。')
    lines.append('- 90% SL 表示止损价约等于入场价 × 0.10，实际几乎只在到期前 20 秒卖。')
    lines.append('- 进程被强杀且日志未写出 JSON 的成交不会入库。')
    lines.append('')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--runtime-dir', default=str(Path(__file__).resolve().parents[1] / 'runtime'))
    ap.add_argument('--out-md', default=str(Path(__file__).resolve().parents[1] / 'data' / 'live_trades.md'))
    ap.add_argument('--out-json', default=str(Path(__file__).resolve().parents[1] / 'data' / 'live_trades.json'))
    args = ap.parse_args()
    runtime = Path(args.runtime_dir)
    rows = collect_sessions(runtime)
    enrich_settlement(rows)
    write_json(Path(args.out_json), rows)
    write_md(Path(args.out_md), rows)
    print(f'journal_ok trades={len(rows)} md={args.out_md} json={args.out_json}')


if __name__ == '__main__':
    main()
