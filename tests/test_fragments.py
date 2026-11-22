import shutil

import pytest
from fastapi.testclient import TestClient

from app.config import RunOptions, Settings
from app.main import create_app
from app.modules.intake.reader import read_rows
from app.modules.review.service import run_review
from app.pipeline import Survey
from app.store import Store
from conftest import by_key

HEADER = '图幅,点名,像素列,像素行,备注\n'


def fragment_root(settings, tmp_path, edits):
    root = tmp_path / 'fragment'
    shutil.copytree(settings.sample_root, root)
    for name, transform in edits.items():
        path = root / 'field' / name
        path.write_bytes(transform(path.read_bytes()))
    return Settings(sample_root=root, database=tmp_path / 'register.sqlite3')


def break_ground_line(line_number):
    def transform(raw):
        lines = raw.decode('gb18030').split('\n')
        lines[line_number - 1] = lines[line_number - 1].replace('平面簿', '"平面簿', 1)
        return '\n'.join(lines).encode('gb18030')
    return transform


def test_broken_quote_segment_is_registered_and_parsing_resumes():
    text = (HEADER + '0421,P01,115,129,石桩\n'
            '0432,P01,140,90,"石桩\n'
            '0432,P02,280,160,石桩\n'
            '0432,P03,420,230,"断"残\n'
            '0431,P01,115,129,石桩\n')
    table = read_rows(text, 'pixels.csv', 'utf-8')
    assert table['state'] == 'FRAGMENT'
    assert [(row['sheet'], row['source']['line']) for row in table['rows']] == [('0421', 2), ('0431', 6)]
    gap, = table['gaps']
    assert (gap['start_line'], gap['end_line']) == (3, 5)
    assert gap['raw'] == ['0432,P01,140,90,"石桩', '0432,P02,280,160,石桩', '0432,P03,420,230,"断"残']


def test_unterminated_quote_at_end_marks_gap_to_eof():
    table = read_rows(HEADER + '0421,P01,115,129,石桩\n0432,P07,980,510,"石桩', 'pixels.csv', 'utf-8')
    gap, = table['gaps']
    assert (gap['start_line'], gap['end_line'], table['records'], table['state']) == (3, 3, 1, 'FRAGMENT')


def test_fragment_intake_registers_receipts_and_impact(settings, tmp_path):
    frag = fragment_root(settings, tmp_path, {'ground.csv': break_ground_line(44)})
    survey = Survey(frag, RunOptions())
    table = survey.intake['tables']['ground.csv']
    assert table['state'] == 'FRAGMENT' and table['records'] == 42
    gap, = table['gaps']
    assert (gap['start_line'], gap['end_line']) == (44, 50)
    assert gap['sheets'] == ['0432']
    assert [hit['name'] for hit in gap['sightings']] == [f'P0{index}' for index in range(1, 8)]
    assert survey.intake['impact'] == {'held': ['0432'], 'clear': ['0421', '0422', '0431'], 'edges': ['QS-SJ']}
    receipts = survey.intake['receipts']
    assert '0432' not in receipts['ground.csv']
    assert receipts['pixels.csv']['0432'] == [f'pixels.csv:{line}' for line in range(51, 58)]
    assert receipts['ground.csv']['0431'] == [f'ground.csv:{line}' for line in range(32, 44)]
    assert receipts['witness.csv']['HX-QS'] == [f'witness.csv:{line}' for line in range(2, 8)]


def test_fragment_run_holds_only_the_broken_sheet(settings, tmp_path):
    frag = fragment_root(settings, tmp_path, {'ground.csv': break_ground_line(44)})
    store = Store(frag.database)
    store.initialize()
    result, reused = run_review(frag, store, RunOptions())
    assert reused is False
    reviews = {row['code']: row for row in result['reviews']}
    assert (reviews['0431']['status'], reviews['0432']['status']) == ('READY', 'HOLD')
    assert (reviews['0421']['status'], reviews['0422']['status']) == ('RETURN', 'RETURN')
    edge = by_key(result['seams']['edges'], 'QS-SJ')
    assert edge['status'] == 'BLOCKED' and 'EDGE_GAP' in edge['reasons']
    assert by_key(result['seams']['edges'], 'HX-LW')['status'] == 'PASS'
    assert result['ledger']['ground']['missing'] == [{'start': 44, 'end': 50}]
    assert result['ledger']['ground']['state'] == 'FRAGMENT'
    assert result['impact']['held'] == ['0432']


def test_gap_impact_holds_an_otherwise_ready_sheet(settings, tmp_path):
    frag = fragment_root(settings, tmp_path, {'ground.csv': break_ground_line(37)})
    store = Store(frag.database)
    store.initialize()
    result, _ = run_review(frag, store, RunOptions())
    reviews = {row['code']: row for row in result['reviews']}
    assert reviews['0431']['fit_rms_m'] is not None
    assert (reviews['0431']['status'], reviews['0431']['reason']) == ('HOLD', 'GAP_UNRESOLVED')
    edge = by_key(result['seams']['edges'], 'HX-LW')
    assert edge['status'] == 'BLOCKED' and 'EDGE_GAP' in edge['reasons']


def test_hidden_same_name_observation_knocks_out_the_received_point(settings, tmp_path):
    edits = {'pixels.csv': lambda raw: raw + '0431,P05,210,458,"复测'.encode(),
             'ground.csv': lambda raw: raw + '0431,P06,LOCAL-ENU,m,390.6750,151.5200,"复测'.encode('gb18030')}
    frag = fragment_root(settings, tmp_path, edits)
    survey = Survey(frag, RunOptions())
    hidden = [mark for mark in survey.pairing['quarantine'] if mark['reason'] == 'GAP_HIDDEN_REPEAT']
    assert [(mark['source']['id'], mark['sheet'], mark['name']) for mark in hidden] == [
        ('ground.csv:37', '0431', 'P06'), ('pixels.csv:43', '0431', 'P05')]
    assert hidden[0]['gap'] == 'ground.csv#51-51' and hidden[1]['gap'] == 'pixels.csv#58-58'
    paired = {(row['sheet'], row['name']) for row in survey.pairing['pairs']}
    assert not {('0431', 'P05'), ('0431', 'P06')} & paired
    assert survey.pairing['gap_sheets'] == ['0431']


def test_witness_gap_blocks_the_edge_and_its_sheets(settings, tmp_path):
    edits = {'witness.csv': lambda raw: raw + b'HX-LW,"unfinished'}
    frag = fragment_root(settings, tmp_path, edits)
    survey = Survey(frag, RunOptions())
    assert survey.intake['impact'] == {'held': [], 'clear': ['0421', '0422', '0431', '0432'],
                                       'edges': ['HX-LW']}
    edge = by_key(survey.seams['edges'], 'HX-LW')
    assert edge['status'] == 'BLOCKED' and edge['reason'] == 'EDGE_GAP'
    reviews = {row['code']: row for row in
               run_review(frag, _initialized_store(frag), RunOptions())[0]['reviews']}
    assert (reviews['0431']['status'], reviews['0431']['reason']) == ('HOLD', 'EDGE_GAP')


def _initialized_store(frag):
    store = Store(frag.database)
    store.initialize()
    return store


def test_fragment_signature_flow(client_fragment):
    payload, client = client_fragment
    reviews = {row['code']: row for row in payload['reviews']}
    assert reviews['0431']['status'] == 'READY' and reviews['0432']['status'] == 'HOLD'
    run = f"/api/review/runs/{payload['manifest']['run_id']}"
    held = client.post(run + '/signatures', json={'sheet': '0432', 'reviewer': '周测绘'})
    assert held.status_code == 409
    signed = client.post(run + '/signatures', json={'sheet': '0431', 'reviewer': '周测绘'})
    assert signed.status_code == 200


@pytest.fixture
def client_fragment(settings, tmp_path):
    frag = fragment_root(settings, tmp_path, {'ground.csv': break_ground_line(44)})
    with TestClient(create_app(frag)) as client:
        payload = client.post('/api/review/runs', json={}).json()
        yield payload, client


def test_same_fragment_rerun_does_not_add_an_intake_batch(client):
    first = client.get('/api/intake?dataset=field').json()
    again = client.get('/api/intake?dataset=field').json()
    assert first['reused'] is False and again['reused'] is True
    assert first['manifest']['intake_id'] == again['manifest']['intake_id']
    batches = client.get('/api/intake/batches?dataset=field').json()['batches']
    assert [batch['intake_id'] for batch in batches] == [first['manifest']['intake_id']]


def test_failed_supplement_keeps_the_previous_intake_conclusion(settings, tmp_path):
    frag = fragment_root(settings, tmp_path, {'ground.csv': break_ground_line(44)})
    with TestClient(create_app(frag)) as client:
        first = client.get('/api/intake?dataset=field').json()
        assert first['reused'] is False and first['impact']['held'] == ['0432']
        path = frag.sample_root / 'field' / 'ground.csv'
        path.write_bytes(b'"' + path.read_bytes())
        failed = client.get('/api/intake?dataset=field')
        assert failed.status_code == 422
        assert failed.json()['detail']['code'] == 'MALFORMED_CSV'
        recovered = client.get(f"/api/intake/batches/{first['manifest']['intake_id']}")
        assert recovered.status_code == 200
        conclusion = recovered.json()
        assert conclusion['impact']['held'] == ['0432']
        assert conclusion['tables']['ground.csv']['gaps'][0]['start_line'] == 44
        batches = client.get('/api/intake/batches?dataset=field').json()['batches']
        assert [batch['intake_id'] for batch in batches] == [first['manifest']['intake_id']]


def test_successful_supplement_creates_a_new_batch(settings, tmp_path):
    frag = fragment_root(settings, tmp_path, {'ground.csv': break_ground_line(44)})
    path = frag.sample_root / 'field' / 'ground.csv'
    broken = path.read_bytes()
    with TestClient(create_app(frag)) as client:
        first = client.get('/api/intake?dataset=field').json()
        path.write_bytes(broken.replace(b'"', b'', 1))
        supplemented = client.get('/api/intake?dataset=field').json()
        assert supplemented['reused'] is False
        assert supplemented['manifest']['intake_id'] != first['manifest']['intake_id']
        assert supplemented['impact']['held'] == []
        batches = client.get('/api/intake/batches?dataset=field').json()['batches']
        assert len(batches) == 2
        assert batches[0]['intake_id'] == supplemented['manifest']['intake_id']
        assert batches[0]['state'] == 'RECEIVED' and batches[1]['state'] == 'FRAGMENT'


def test_intake_batch_endpoints_validate_identifiers(client):
    assert client.get('/api/intake/batches?dataset=unknown').status_code == 404
    missing = client.get('/api/intake/batches/' + '0' * 64)
    assert (missing.status_code, missing.json()['detail']['code']) == (404, 'INTAKE_NOT_FOUND')
