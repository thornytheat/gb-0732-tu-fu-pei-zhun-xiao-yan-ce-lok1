from collections import defaultdict

from app.modules.coordinates.numbers import clean


def candidate_fields(line):
    """Best-effort first two fields of a physical line inside a broken segment.

    Every logical record starts on its own physical line, so any record hidden by
    a broken quote still shows its leading sheet/edge code at a line start.
    """
    parts = line.split(',', 2)
    first = clean(parts[0].strip('"')) if parts and parts[0].strip('"').strip() else ''
    second = clean(parts[1].strip('"')) if len(parts) > 1 else ''
    return first, second


def enrich_gaps(tables, catalog):
    sheets = {sheet['code'] for sheet in catalog['sheets']}
    edges = {edge['code'] for edge in catalog['edges']}
    for filename, table in tables.items():
        for gap in table['gaps']:
            sightings = []
            for line in gap['raw']:
                code, name = candidate_fields(line)
                if code:
                    sightings.append({'code': code.upper(), 'name': name})
            gap['sightings'] = sightings
            if filename == 'witness.csv':
                gap['edges'] = sorted({item['code'] for item in sightings} & edges)
            else:
                gap['sheets'] = sorted({item['code'] for item in sightings} & sheets)
    return tables


def source_line(identity):
    return int(identity.rsplit(':', 1)[1])


def build_receipts(tables):
    """Per-table submission receipts: which source lines were received for each sheet/edge."""
    receipts = {}
    for filename, table in tables.items():
        key = 'edge' if filename == 'witness.csv' else 'sheet'
        delivered = defaultdict(list)
        for row in table['rows']:
            delivered[clean(row[key]).upper()].append(row['source']['id'])
        for mark in table['quarantine']:
            if mark.get(key):
                delivered[clean(mark[key]).upper()].append(mark['source']['id'])
        receipts[filename] = {code: sorted(ids, key=source_line)
                              for code, ids in sorted(delivered.items())}
    return receipts


def assess_impact(catalog, tables):
    """Which sheets cannot rule out hidden same-name observations, and which edges
    are unverifiable because their own witness rows or a neighbouring sheet is broken."""
    held, gap_edges = set(), set()
    for filename in ('pixels.csv', 'ground.csv'):
        for gap in tables[filename]['gaps']:
            held.update(gap['sheets'])
    for gap in tables['witness.csv']['gaps']:
        gap_edges.update(gap['edges'])
    affected = gap_edges | {edge['code'] for edge in catalog['edges']
                            if edge['a'] in held or edge['b'] in held}
    return {'held': sorted(held),
            'clear': [sheet['code'] for sheet in catalog['sheets'] if sheet['code'] not in held],
            'edges': sorted(affected)}
