import copy
import json
from pathlib import Path

import pytest

from orkmind.core.company_brain import ADAPTER, validate, validate_catalog

ROOT = Path(__file__).resolve().parents[2]
CORPUS = json.loads((ROOT / 'tests/fixtures/company-brain-v1.json').read_text())


@pytest.mark.parametrize('case', CORPUS['cases'], ids=lambda c: c['name'])
def test_contract(case):
    if case['valid']:
        assert validate(case['value']) == case['value']
    else:
        with pytest.raises(ValueError, match='brain.contract.invalid'):
            validate(case['value'])


def test_schema_is_the_canonical_model():
    stored = json.loads((ROOT / 'src/orkmind/contracts/company-brain.v1.json').read_text())
    generated = ADAPTER.json_schema()
    assert stored['$defs'] == generated['$defs']
    assert stored['anyOf'] == generated['anyOf']


def test_catalog_references_scope_and_no_legacy_reinterpretation():
    catalog = CORPUS['catalog']
    assert validate_catalog(catalog) == catalog
    scope = dict(project_id='proj-example', delivery='initiatives', initiative_ids=['init-example'])
    validate_catalog(catalog, scope)
    for bad in [catalog + [catalog[0]], catalog[1:], [catalog[0], catalog[2]]]:
        with pytest.raises(ValueError): validate_catalog(bad)
    with pytest.raises(ValueError): validate_catalog(catalog, {**scope, 'initiative_ids': []})
    with pytest.raises(ValueError): validate_catalog(catalog, {**scope, 'delivery': 'project'})
    with pytest.raises(ValueError): validate_catalog(catalog, {**scope, 'project_id': 'orgproj-example'})
    other = copy.deepcopy(catalog[-1]); other['id'] = 'init-second'
    bad = copy.deepcopy(catalog) + [other]
    bad[-1]['depends_on'] = ['init-example']; bad[-2]['depends_on'] = ['init-second']
    with pytest.raises(ValueError, match='brain.catalog.cycle'): validate_catalog(bad)
