"""服务费结算单归属。

渠道按整张套票出账。整单金额只读取、不拆分改写：每笔费用按该票的原分账权重
切成场次份额（最大余数法，和原分账同一裁决），再把份额按「场馆是否实际演出」
落到场馆或转入未落地。停演场次与未核销场次的服务费不从任何场馆扣，留在
未落地并带原因，供向渠道追讨。
"""
from collections import defaultdict

from app.contracts import cents, key, timestamp
from app.deduplicate import unique_records
from app.errors import Problem, RowError
from app.modules.m8_fees.rules import NO_RIGHTS, UNLANDED_REASONS, fee_split, landing


def fee_records(rows, tickets, isolates):
    for row in rows:
        values = row.values
        try:
            event = {
                'statement_id': key(values['statement_id']), 'ticket': key(values['ticket']),
                'billed_at': timestamp(values['billed_at']),
                'billed_cents': cents(values['amount'], values['unit']),
            }
            if event['ticket'] not in tickets:
                raise RowError('TICKET_UNKNOWN')
            if event['billed_at'] < tickets[event['ticket']]['paid_at']:
                raise RowError('FEE_TOO_EARLY')
        except RowError as exc:
            isolates.add(row, exc.code, values.get('statement_id') or values.get('ticket'))
            continue
        yield event, row


def attribute(event, originals, shows, scanned):
    """把一笔整单费用按原分账权重切成场次份额，逐份决定落地或未落地。"""
    ticket, billed = event['ticket'], event['billed_cents']
    if not originals:
        return [{'statement_id': event['statement_id'], 'ticket': ticket, 'show': '', 'venue': '',
                 'weight': 0, 'share_cents': billed, 'landed': False, 'reason': NO_RIGHTS,
                 'right_line': None, 'show_line': None}]
    weights = {row['show']: row['weight'] for row in originals}
    by_show = {row['show']: row for row in originals}
    shares = []
    for show, share_cents in fee_split(billed, originals).items():
        definition = shows[show]
        landed, reason = landing(definition, (ticket, show) in scanned)
        shares.append({'statement_id': event['statement_id'], 'ticket': ticket, 'show': show,
                       'venue': definition['venue'], 'weight': weights[show], 'share_cents': share_cents,
                       'landed': landed, 'reason': reason, 'right_line': by_show[show]['right_line'],
                       'show_line': definition['line']})
    return shares


def settle(rows, tickets, shows, publication, scans, isolates):
    originals = defaultdict(list)
    for row in publication['allocations']:
        originals[row['ticket']].append(row)
    scanned = {(row['ticket'], row['show']) for row in scans}
    candidates = unique_records(fee_records(rows, tickets, isolates), lambda item: (item['statement_id'], item['ticket']),
                                'FEE_DUPLICATE', 'FEE_CONFLICT', isolates)
    lines = []
    for event in sorted(candidates, key=lambda item: (item['billed_at'], item['statement_id'], item['ticket'])):
        lines.append({**event, 'shares': attribute(event, originals[event['ticket']], shows, scanned)})
    return summarize(lines)


def summarize(lines, unlanded=None):
    if unlanded is None:
        unlanded = [{**share, 'billed_at': line['billed_at'], 'billed_cents': line['billed_cents'],
                     'line': line.get('line'), 'source': line.get('source'), 'end_line': line.get('end_line'),
                     'raw': line.get('raw')}
                    for line in lines for share in line['shares'] if not share['landed']]
    statements = {}
    venues = {}
    for line in lines:
        summary = statements.setdefault(line['statement_id'], {
            'statement_id': line['statement_id'], 'line_count': 0, 'billed_cents': 0,
            'landed_cents': 0, 'unlanded_cents': 0, 'first_billed_at': line['billed_at']})
        summary['line_count'] += 1
        summary['billed_cents'] += line['billed_cents']
        for share in line['shares']:
            if share['landed']:
                summary['landed_cents'] += share['share_cents']
                venue = venues.setdefault(share['venue'], {'venue': share['venue'], 'fee_cents': 0, 'share_count': 0})
                venue['fee_cents'] += share['share_cents']
                venue['share_count'] += 1
            else:
                summary['unlanded_cents'] += share['share_cents']
    bills = sum(line['billed_cents'] for line in lines)
    landed = sum(share['share_cents'] for line in lines for share in line['shares'] if share['landed'])
    pending = sum(share['share_cents'] for line in lines for share in line['shares'] if not share['landed'])
    reasons = defaultdict(int)
    for share in unlanded:
        reasons[share['reason']] += share['share_cents']
    totals = {'statement_count': len(statements), 'line_count': len(lines), 'billed_cents': bills,
              'landed_cents': landed, 'unlanded_cents': pending}
    checks = {
        'fee_lines': all(sum(share['share_cents'] for share in line['shares']) == line['billed_cents']
                         for line in lines),
        'fee_conservation': bills == landed + pending,
        'fee_statements': all(row['billed_cents'] == row['landed_cents'] + row['unlanded_cents']
                              for row in statements.values()),
        'fee_landing': all(share['landed'] == (share['show'] != '' and share['reason'] == '')
                           for line in lines for share in line['shares']),
    }
    return {'lines': lines, 'unlanded': unlanded, 'statements': list(statements.values()),
            'venues': list(venues.values()), 'totals': totals,
            'reason_cents': {reason: reasons[reason] for reason in UNLANDED_REASONS if reasons[reason]},
            'checks': checks, 'balanced': all(checks.values())}


def preserve_approved(prior, proposed):
    """已入账的服务费不得被遗漏或改写，失败时旧版净额保持可查。"""
    fields = ('statement_id', 'ticket', 'billed_cents', 'billed_at')
    by_id = {(row['statement_id'], row['ticket']): tuple(row[field] for field in fields) for row in proposed}
    for event in prior:
        if by_id.get((event['statement_id'], event['ticket'])) != tuple(event[field] for field in fields):
            raise Problem(409, 'FEE_CHANGED', {'statement_id': event['statement_id'], 'ticket': event['ticket']})
