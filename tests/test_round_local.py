"""Real LocalAdapter child processes, with a SCRIPTED stand-in (no model calls)."""
import json
import os
from pathlib import Path
import sys

import yaml

from infinievolve.adapters import make_adapter
from infinievolve.config import campaign_config
from infinievolve.core.archive import CampaignStore, read_json
from infinievolve.core.loop import Scheduler
from infinievolve.local import fresh_copy


def test_local_processes_restore_primary_and_deliver_donor(tmp_path, monkeypatch):
    repo = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv('PYTHONPATH', repo + os.pathsep + os.environ.get('PYTHONPATH', ''))
    task = tmp_path / 'task'
    (task / 'workspace').mkdir(parents=True)
    (task / 'workspace' / 'work.txt').write_text('baseline')
    (task / 'evaluate.py').write_text("import json,pathlib\np=pathlib.Path('work.txt')\nprint(json.dumps({'checks': {'exists': p.exists()}, 'metrics': {'score': len(p.read_text())}}))\n")
    (task / 'agent.py').write_text('''import json, os, pathlib, re, time
prompt = pathlib.Path(os.environ['EVOLVE_PROMPT']).read_text()
sid = pathlib.Path(os.environ['EVOLVE_PROMPT']).parents[1].name
p = pathlib.Path('work.txt')
initial = p.read_text()
if 'secondary' in prompt and 'Combine useful parts' in prompt:
    assert 'diff --git a/work.txt b/work.txt' in prompt
    assert initial != 'baseline', initial
p.write_text(initial + ' offspring ' + sid)
pathlib.Path('evidence.json').write_text(json.dumps({'initial': initial, 'donor': 'Combine useful parts' in prompt}))
time.sleep(0.1)
''')
    (task / 'task.yaml').write_text(yaml.safe_dump({'instruction':'scripted integration test', 'evaluate':['{python}','{task}/evaluate.py'], 'metric':{'name':'score','direction':'max','scale':'absolute'}, 'agent': {'command': f'"{sys.executable}" "{task / "agent.py"}"'}, 'time_limit_seconds':15}))
    campaign = task / 'runs' / 'genetic'
    campaign.mkdir(parents=True)
    (campaign / 'config.yaml').write_text(yaml.safe_dump({'campaign':{'adapter':'local','tasks':['task'],'max_parallel':2,'agents_per_round':2,'poll_seconds':.01},'strategy':{'selection':'genetic','elite_count':2,'crossover_rate':1,'mutation_rate':1}, 'stop':{'max_sessions':4,'window_k':9}, 'options':{'task_dir':'../..'}}))
    config = campaign_config(campaign)
    store = CampaignStore(campaign)
    state = Scheduler(store, config, make_adapter(config), log=lambda _:None).run()
    assert not state['paused'] and state['sessions'] == 4
    for node in store.task('task').nodes()[3:]:
        assert node.valid and len(node.parent_ids) == 2
        check = tmp_path / ('verify-' + node.session_id)
        assert fresh_copy(task / 'workspace', check, store.task('task').artifact(node.session_id)) is None
        evidence = json.loads((check / 'evidence.json').read_text())
        assert evidence['donor'] and node.parent_id in evidence['initial']
