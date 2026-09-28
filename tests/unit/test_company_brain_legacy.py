import copy
import json
from pathlib import Path

from orkmind.core.company_brain_migration import BrainMigration


def test_plan_never_writes_or_reinterprets_legacy_memory():
    catalog=json.loads((Path(__file__).resolve().parents[1]/'fixtures/company-brain-v1.json').read_text())['catalog']
    legacy=dict(project='legacy-project',collection='project',id='memory-unchanged')
    snapshot=copy.deepcopy(legacy)
    class Connection:
        def execute(self,sql,args):
            assert sql.startswith('SELECT * FROM brain_projection')
            return self
        def fetchone(self): return None
    from orkmind.store.company_brain import BrainStore
    class Store(BrainStore):
        connection=Connection()
        def __init__(self): pass
        def _authorize(self,*args): pass
    plan=BrainMigration(Store()).plan(catalog,'batch-test','a'*64,'2026-09-13T20:00:00Z')
    assert [o['id'] for o in plan['operations']]==['prod-example','proj-example','init-example']
    assert all(o['after']['owner']['principal'] is None for o in plan['operations'])
    assert legacy==snapshot
