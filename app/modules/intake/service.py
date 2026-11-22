from datetime import datetime, timezone
from hashlib import sha256

from app.config import DATASETS, FILES, MAX_INPUT_BYTES, RULE_VERSION
from app.errors import RegisterError
from app.manifest import canonical_json
from app.modules.intake.catalog import read_catalog
from app.modules.intake.decode import decode_table
from app.modules.intake.gaps import assess_impact, build_receipts, enrich_gaps
from app.modules.intake.reader import read_rows


def acquire_bundle(root, dataset):
    if dataset not in DATASETS:
        raise RegisterError('UNKNOWN_DATASET', '请选择已登记的图册', 404)
    bundle = {}
    for name in FILES:
        try:
            with (root / dataset / name).open('rb') as stream:
                bundle[name] = stream.read(MAX_INPUT_BYTES + 1)
        except OSError as error:
            raise RegisterError('SOURCE_UNAVAILABLE', '图册档件无法读取', 503, name) from error
        if len(bundle[name]) > MAX_INPUT_BYTES:
            raise RegisterError('SOURCE_TOO_LARGE', '档件超过单表上限', 413, name)
    return bundle


def intake(bundle):
    catalog = read_catalog(bundle['catalog.json']).model_dump()
    tables = {}
    for filename in ('pixels.csv', 'ground.csv', 'witness.csv'):
        text, encoding = decode_table(bundle[filename], filename)
        tables[filename] = read_rows(text, filename, encoding)
    enrich_gaps(tables, catalog)
    return {'catalog': catalog, 'tables': tables, 'receipts': build_receipts(tables),
            'impact': assess_impact(catalog, tables)}


def receive_intake(store, dataset, survey):
    """Persist the intake conclusion as a batch. The same fragment re-run reuses its
    batch; a failed supplement raises before this point and leaves earlier batches intact."""
    inputs = {name: sha256(raw).hexdigest() for name, raw in survey.bundle.items()}
    fingerprint = {'dataset': dataset, 'inputs': inputs, 'kind': 'intake-batch-1',
                   'rule_version': RULE_VERSION}
    intake_id = sha256(canonical_json(fingerprint).encode()).hexdigest()
    existing = store.get_intake(intake_id, required=False)
    if existing is not None:
        return existing, True
    manifest = {'intake_id': intake_id, **fingerprint, 'data_version': survey.catalog['version'],
                'created_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds')}
    return store.insert_intake({'manifest': manifest, **survey.intake})
