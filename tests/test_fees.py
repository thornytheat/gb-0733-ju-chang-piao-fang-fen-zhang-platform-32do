import csv
import io
import json
import sqlite3
from zipfile import ZipFile

import pytest

from app.config import ALL_SOURCES
from app.pipeline import compute
from app.sources import fee_sample_sources, sample_sources
from app.store import Store


def fee_snapshot(overrides=None):
    return compute('fee-book', 'unit', {**sample_sources(), **(overrides or {})})


def test_fee_statement_is_split_by_original_weight_without_rewriting_total():
    """整单金额不拆改：场次份额之和必须等于票面整单金额。"""
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00107', '2026-11-20T09:00:00+08:00', '18.00', '元')])})
    line, = snapshot['fees']['lines']
    assert line['billed_cents'] == 1800
    assert [row['share_cents'] for row in line['shares']] == [600, 600, 600]
    assert sum(row['share_cents'] for row in line['shares']) == line['billed_cents']
    assert snapshot['fees']['checks']['fee_lines'] is True


def test_fee_lands_only_on_shows_the_ticket_actually_attended():
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00107', '2026-11-20T09:00:00+08:00', '18.00', '元')])})
    shares = {row['show']: row for row in snapshot['fees']['lines'][0]['shares']}
    # 00107 只核销 A-LAKE、B-RAIN；C-DUSK 停演，未落地。
    assert (shares['A-LAKE']['landed'], shares['B-RAIN']['landed']) == (True, True)
    assert shares['C-DUSK']['landed'] is False
    assert shares['C-DUSK']['reason'] == 'FEE_CANCELLED_SHOW'


def test_cancelled_show_share_is_not_charged_to_any_venue():
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00107', '2026-11-20T09:00:00+08:00', '18.00', '元'),
        ('S2', '00108', '2026-11-20T09:00:00+08:00', '30.00', '元')])})
    cancelled = sum(row['share_cents'] for row in snapshot['fees']['unlanded']
                    if row['reason'] == 'FEE_CANCELLED_SHOW')
    assert cancelled == 2600
    fees_by_venue = {row['venue']: row['fee_cents'] for row in snapshot['fees']['venues']}
    # 北岸小剧场整场停演，一分服务费也不落到该馆。
    assert fees_by_venue.get('北岸小剧场', 0) == 0
    assert snapshot['fees']['reason_cents'] == {'FEE_CANCELLED_SHOW': 2600, 'FEE_AWAITING_SCAN': 1000}


def test_unattended_show_share_stays_unlanded_with_reason():
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00108', '2026-11-20T09:00:00+08:00', '30.00', '元')])})
    shares = {row['show']: row for row in snapshot['fees']['lines'][0]['shares']}
    assert shares['A-LAKE']['landed'] is False and shares['A-LAKE']['reason'] == 'FEE_AWAITING_SCAN'
    assert shares['A-LAKE']['venue'] == '海棠剧场'


def test_venue_net_receivable_subtracts_only_landed_fee():
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00107', '2026-11-20T09:00:00+08:00', '18.00', '元'),
        ('S2', '00108', '2026-11-20T09:00:00+08:00', '30.00', '元'),
        ('S3', '00110', '2026-11-20T09:00:00+08:00', '12.00', '元')])})
    venues = {row['venue']: row for row in snapshot['funding']['fee_venues']}
    assert venues['海棠剧场']['fee_cents'] == 600
    assert venues['海棠剧场']['net_receivable_cents'] == venues['海棠剧场']['payable_cents'] - 600
    assert venues['青瓦剧院']['fee_cents'] == 1800
    assert venues['北岸小剧场']['fee_cents'] == 0
    totals = snapshot['funding']['fee_totals']
    assert totals['billed_cents'] == totals['landed_cents'] + totals['unlanded_cents']


def test_ticket_without_original_allocation_is_fully_unlanded():
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00109', '2026-11-20T09:00:00+08:00', '9.00', '元')])})
    line, = snapshot['fees']['lines']
    assert line['billed_cents'] == 900
    unlanded, = line['shares']
    assert (unlanded['landed'], unlanded['reason'], unlanded['venue']) == (False, 'FEE_NO_RIGHTS', '')
    venues = {row['venue']: row['fee_cents'] for row in snapshot['funding']['fee_venues']}
    assert all(value == 0 for value in venues.values())


def test_fee_before_ticket_payment_is_isolated():
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00107', '2026-11-01T09:00:00+08:00', '18.00', '元')])})
    assert snapshot['fees']['lines'] == []
    issue, = [row for row in snapshot['quarantine'] if row['source'] == 'fees.csv']
    assert issue['code'] == 'FEE_TOO_EARLY'


def test_duplicate_and_conflicting_fee_lines_are_isolated():
    snapshot = fee_snapshot({'fees.csv': _csv('fees.csv', [
        ('S1', '00107', '2026-11-20T09:00:00+08:00', '18.00', '元'),
        ('S1', '00107', '2026-11-20T09:00:00+08:00', '18', '元'),
        ('S2', '00107', '2026-11-20T09:00:00+08:00', '18.00', '元'),
        ('S2', '00107', '2026-11-20T09:00:00+08:00', '19.00', '元')])})
    assert [row['billed_cents'] for row in snapshot['fees']['lines']] == [1800]
    assert snapshot['reason_counts']['FEE_DUPLICATE'] == 1
    assert snapshot['reason_counts']['FEE_CONFLICT'] == 2


def test_same_statement_rerun_does_not_double_charge(tmp_path):
    store = Store(tmp_path / 'fees.sqlite3')
    store.initialize()
    sources = fee_sample_sources()
    first, reused = store.commit('fee-book', sources)
    second, reused_again = store.commit('fee-book', sources)
    assert (reused, reused_again) == (False, True)
    assert first['manifest']['run_id'] == second['manifest']['run_id']
    assert first['funding']['fee_totals'] == second['funding']['fee_totals']
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM runs').fetchone() == (1,)
        assert db.execute('SELECT count(*) FROM run_inputs').fetchone() == (6,)


def test_rerun_endpoint_reuses_fee_batch(client):
    first = client.post('/api/runs/sample-fees')
    assert first.status_code == 201
    run_id = first.json()['run_id']
    replay = client.post(f'/api/runs/{run_id}/rerun')
    assert replay.status_code == 200 and replay.json()['reused'] is True
    assert replay.json()['fee_totals'] == first.json()['fee_totals']


def test_failed_fee_revision_keeps_previous_net_receivable_readable(tmp_path):
    store = Store(tmp_path / 'fees.sqlite3')
    store.initialize()
    sources = fee_sample_sources()
    first, _ = store.commit('fee-book', sources)
    head_before = store.latest('fee-book')
    with pytest.raises(Exception) as failure:
        store.commit('fee-book', {**sources, 'fees.csv': b'statement_id,ticket,billed_at,amount,unit\n'})
    assert getattr(failure.value, 'code', None) == 'FEE_CHANGED'
    head_after = store.latest('fee-book')
    assert head_after['manifest']['run_id'] == head_before['manifest']['run_id']
    assert head_after['funding']['fee_totals'] == first['funding']['fee_totals']
    # 旧版净额仍可按批次查回。
    recovered = store.get(first['manifest']['run_id'])
    assert recovered['funding']['fee_venues'] == first['funding']['fee_venues']


def test_new_revision_may_append_fee_lines(tmp_path):
    store = Store(tmp_path / 'fees.sqlite3')
    store.initialize()
    sources = fee_sample_sources()
    first, _ = store.commit('fee-book', sources)
    extra = sources['fees.csv'] + 'S-1120C,00111,2026-11-20T09:30:00+08:00,9.00,元\n'.encode()
    second, _ = store.commit('fee-book', {**sources, 'fees.csv': extra})
    assert second['manifest']['previous_run'] == first['manifest']['run_id']
    assert second['fees']['totals']['billed_cents'] == 6900
    assert second['funding']['fee_totals']['billed_cents'] == 6900


def test_evidence_bundle_carries_fee_tables_and_reconcilable_checks(client):
    run_id = client.post('/api/runs/sample-fees').json()['run_id']
    archive = ZipFile(io.BytesIO(client.get(f'/api/evidence/{run_id}/export').content))
    with archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['fee_totals']['billed_cents'] == 6000
        assert all(manifest['fee_checks'].values())
        net = list(csv.DictReader(io.StringIO(archive.read('evidence/venue-net-receivable.csv').decode('utf-8-sig'))))
        assert {row['venue']: row['net_receivable_cents'] for row in net}['青瓦剧院'] == '126990'
        unlanded = list(csv.DictReader(io.StringIO(archive.read('evidence/fee-unlanded.csv').decode('utf-8-sig'))))
        assert all(row['landed'] == 'False' and row['reason'] for row in unlanded)
        shares = list(csv.DictReader(io.StringIO(archive.read('evidence/fee-shares.csv').decode('utf-8-sig'))))
        lines = list(csv.DictReader(io.StringIO(archive.read('evidence/fee-lines.csv').decode('utf-8-sig'))))
        for line in lines:
            owned = [row for row in shares if (row['statement_id'], row['ticket']) == (line['statement_id'], line['ticket'])]
            assert sum(int(row['share_cents']) for row in owned) == int(line['billed_cents'])


def test_optional_fee_source_is_accepted_but_not_required(upload, files):
    assert upload(files).status_code == 201
    with_fee = {**files, 'fees.csv': _csv('fees.csv', [
        ('S1', '00107', '2026-11-20T09:00:00+08:00', '18.00', '元')])}
    result = upload(with_fee, book='tour-fees')
    assert result.status_code == 201
    assert result.json()['fee_totals']['billed_cents'] == 1800


def test_unknown_extra_source_is_rejected(upload, files):
    result = upload({**files, 'extra.csv': b'a,b\n1,2\n'})
    assert (result.status_code, result.json()['code']) == (422, 'SOURCE_SET')


def test_legacy_batch_without_fee_fields_still_renders(tmp_path):
    """服务费上线前的旧批次缺少费用字段，读取时补齐空视图而不是报错。"""
    store = Store(tmp_path / 'legacy.sqlite3')
    store.initialize()
    snapshot, _ = store.commit('legacy-book', sample_sources())
    snapshot.pop('fees')
    snapshot['funding'].pop('fee_venues')
    snapshot['funding'].pop('fee_totals')
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE runs SET payload = ? WHERE id = ?',
                   (json.dumps(snapshot, ensure_ascii=False), snapshot['manifest']['run_id']))
        db.commit()
    upgraded = store.get(snapshot['manifest']['run_id'])
    assert upgraded['fees']['totals']['billed_cents'] == 0
    assert {row['venue']: row['net_receivable_cents'] for row in upgraded['funding']['fee_venues']}[
        '青瓦剧院'] == 128790


def test_fee_views_expose_landing_and_unlanded(client):
    settled = client.get('/api/fees/sample', params={'view': 'settled'}).json()
    assert settled['total'] == 6 and settled['totals']['billed_cents'] == 6000
    unlanded = client.get('/api/fees/sample', params={'view': 'unlanded'}).json()
    assert unlanded['total'] == 3
    assert {row['reason'] for row in unlanded['items']} <= {'FEE_CANCELLED_SHOW', 'FEE_AWAITING_SCAN',
                                                            'FEE_NO_RIGHTS'}
    fee_venues = client.get('/api/reconcile/sample', params={'view': 'fee_venues'}).json()
    assert fee_venues['fee_totals']['net_receivable_cents'] == 133146


def _csv(name, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(ALL_SOURCES[name])
    writer.writerows(rows)
    return stream.getvalue().encode()
