from collections import Counter

from app.modules.m6_reconcile.ledger import settlement_rows

MONEY_COLUMNS = ('allocated_cents', 'refunded_cents', 'net_cents', 'payable_cents', 'unsettled_cents')


def summarize_sides(shows, scans, ledger):
    scan_counts = Counter(row['show'] for row in scans)
    by_show = {key: {**{field: show[field] for field in ('show', 'title', 'venue', 'cancelled_at')},
                     **dict.fromkeys(MONEY_COLUMNS, 0), 'scan_count': scan_counts[key], 'shares': 0}
               for key, show in sorted(shows.items())}
    for row in ledger:
        summary = by_show[row['show']]
        summary['shares'] += 1
        for field in MONEY_COLUMNS:
            summary[field] += row[field]
    by_venue = {}
    for summary in by_show.values():
        summary['gap_cents'] = summary['allocated_cents'] - summary['payable_cents']
        summary['difference_cents'] = summary['net_cents'] - summary['payable_cents'] - summary['unsettled_cents']
        venue = by_venue.setdefault(summary['venue'], {'venue': summary['venue'], 'shows': 0, 'scan_count': 0,
                                                       **dict.fromkeys(MONEY_COLUMNS, 0)})
        for field in (*MONEY_COLUMNS, 'scan_count'):
            venue[field] += summary[field]
        venue['shows'] += 1
    return list(by_show.values()), list(by_venue.values())


def funding_checks(tickets, refunds, ledger, unassigned):
    refunds_by_ticket, payable_by_ticket, pending_by_ticket = Counter(), Counter(), Counter()
    for entry in refunds:
        refunds_by_ticket[entry['ticket']] += -entry['reversal_cents']
    for row in ledger:
        payable_by_ticket[row['ticket']] += row['payable_cents']
        pending_by_ticket[row['ticket']] += row['unsettled_cents']
    for row in unassigned:
        pending_by_ticket[row['ticket']] += row['unsettled_cents']
    entries = []
    for key, ticket in sorted(tickets.items()):
        net = ticket['paid_cents'] - refunds_by_ticket[key]
        entries.append({'ticket': key, 'sale_line': ticket['line'], 'paid_cents': ticket['paid_cents'],
                        'refunded_cents': refunds_by_ticket[key], 'net_cents': net,
                        'payable_cents': payable_by_ticket[key], 'unsettled_cents': pending_by_ticket[key],
                        'difference_cents': net - payable_by_ticket[key] - pending_by_ticket[key]})
    return entries


def count_sources(stats, accepted, isolates):
    issue_count = Counter(entry['source'] for entry in isolates)
    counts = []
    for source, facts in stats.items():
        explained = accepted[source] + issue_count[source] + facts['blank'] + facts['headers']
        counts.append({'source': source, **facts, 'accepted': accepted[source], 'isolated': issue_count[source],
                       'difference': facts['rows'] - explained})
    return counts


def fee_by_venue_unlanded(fees):
    """按场馆拆分未落地服务费（含停演、未核销和无原分账）。"""
    pending = {}
    for row in fees['unlanded']:
        venue = row['venue'] or '（无场馆）'
        bucket = pending.setdefault(venue, {'venue': venue, 'cancelled_cents': 0, 'awaiting_cents': 0,
                                            'no_rights_cents': 0, 'unlanded_cents': 0, 'lines': 0})
        bucket['lines'] += 1
        bucket['unlanded_cents'] += row['share_cents']
        field = {'FEE_CANCELLED_SHOW': 'cancelled_cents', 'FEE_AWAITING_SCAN': 'awaiting_cents',
                 'FEE_NO_RIGHTS': 'no_rights_cents'}[row['reason']]
        bucket[field] += row['share_cents']
    return sorted(pending.values(), key=lambda row: row['venue'])


def net_receivable(shows, ledger, fees):
    """场馆净应收 = 场馆实际演出的应收 − 实际演出场次应承担的服务费。

    停演和未入场份额不落到任何场馆，单独列在未落地列，注明去向。
    """
    fee_by_venue = {row['venue']: row['fee_cents'] for row in fees['venues']}
    pending = {row['venue']: row for row in fee_by_venue_unlanded(fees)}
    by_venue = {}
    for row in ledger:
        venue = by_venue.setdefault(row['venue'], {
            'venue': row['venue'], 'shows': 0, 'scan_count': 0, 'allocated_cents': 0, 'refunded_cents': 0,
            'net_cents': 0, 'payable_cents': 0, 'unsettled_cents': 0})
        for field in ('allocated_cents', 'refunded_cents', 'net_cents', 'payable_cents', 'unsettled_cents'):
            venue[field] += row[field]
    for show in shows:
        entry = by_venue.setdefault(show['venue'], {
            'venue': show['venue'], 'shows': 0, 'scan_count': 0, 'allocated_cents': 0, 'refunded_cents': 0,
            'net_cents': 0, 'payable_cents': 0, 'unsettled_cents': 0})
        entry['shows'] += 1
        entry['scan_count'] += show['scan_count']
    rows = []
    for venue in by_venue.values():
        fee_cents = fee_by_venue.get(venue['venue'], 0)
        unlanded = pending.get(venue['venue'], {})
        rows.append({**venue, 'fee_cents': fee_cents,
                     'net_receivable_cents': venue['payable_cents'] - fee_cents,
                     'cancelled_fee_cents': unlanded.get('cancelled_cents', 0),
                     'awaiting_fee_cents': unlanded.get('awaiting_cents', 0),
                     'no_rights_fee_cents': unlanded.get('no_rights_cents', 0),
                     'unlanded_fee_cents': unlanded.get('unlanded_cents', 0)})
    rows.sort(key=lambda row: row['venue'])
    return rows


def reconcile(tickets, shows, scans, refunds, publication, sources, fees=None):
    fees = fees or {'lines': [], 'unlanded': [], 'venues': [], 'statements': [], 'totals': {
        'statement_count': 0, 'line_count': 0, 'billed_cents': 0, 'landed_cents': 0, 'unlanded_cents': 0},
        'checks': {'fee_conservation': True, 'fee_lines': True, 'fee_statements': True, 'fee_landing': True},
        'reason_cents': {}}
    ledger = settlement_rows(publication, shows, scans, refunds)
    unassigned = publication['unassigned']
    by_show, by_venue = summarize_sides(shows, scans, ledger)
    by_ticket = funding_checks(tickets, refunds, ledger, unassigned)
    paid = sum(row['paid_cents'] for row in by_ticket)
    refunded = sum(-row['reversal_cents'] for row in refunds)
    allocated = sum(row['allocated_cents'] for row in publication['allocations'])
    unallocated = sum(row['unsettled_cents'] for row in unassigned)
    payable = sum(row['payable_cents'] for row in by_venue)
    unsettled = sum(row['unsettled_cents'] for row in by_venue) + unallocated
    difference = paid - refunded - payable - unsettled
    checks = {
        'source_rows': all(row['difference'] == 0 for row in sources),
        'original_allocation': paid == allocated + unallocated,
        'reversal_cap': all(0 <= row['refunded_cents'] <= row['allocated_cents'] for row in ledger)
                        and sum(row['refunded_cents'] for row in ledger) == refunded,
        'show_balances': all(row['difference_cents'] == 0 for row in by_show),
        'ticket_balances': all(row['difference_cents'] == 0 for row in by_ticket),
        'funding': difference == 0 and sum(row['payable_cents'] for row in by_ticket) == payable,
        'fees': fees['checks']['fee_conservation'] and fees['checks']['fee_lines'],
    }
    totals = {'paid_cents': paid, 'allocated_cents': allocated, 'unallocated_cents': unallocated,
              'refunded_cents': refunded, 'net_cents': paid - refunded, 'payable_cents': payable,
              'unsettled_cents': unsettled, 'difference_cents': difference,
              'gap_cents': allocated - payable, 'scan_count': len(scans), 'ticket_count': len(tickets)}
    accounts = {'balanced': all(checks.values()), 'checks': checks, 'totals': totals, 'ledger': ledger,
                'shows': by_show, 'venues': by_venue, 'tickets': by_ticket, 'sources': sources}
    apply_fee_views(accounts, fees)
    return accounts


def apply_fee_views(accounts, fees):
    """把服务费净额视图挂到既有账务上，旧批次也能补齐空视图。"""
    unlanded = fee_by_venue_unlanded(fees)
    accounts['fee_totals'] = {**fees['totals'],
                              'net_receivable_cents': accounts['totals']['payable_cents']
                              - fees['totals']['landed_cents'],
                              'cancelled_cents': sum(row['cancelled_cents'] for row in unlanded),
                              'awaiting_cents': sum(row['awaiting_cents'] for row in unlanded),
                              'no_rights_cents': sum(row['no_rights_cents'] for row in unlanded)}
    accounts['fee_venues'] = net_receivable(accounts['shows'], accounts['ledger'], fees)
    return accounts
