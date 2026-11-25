"""Deferred submits must coexist with multiple running members of one task."""
from dataclasses import replace

import pytest

from infinievolve.core.adapter import SubmitDeferred
from infinievolve.core.loop import Scheduler
from .fake_adapter import FakeAdapter
from .test_loop import campaign, events


@pytest.mark.parametrize('selection', ['greedy', 'probabilistic', 'genetic'])
@pytest.mark.parametrize('change_instruction', [False, True])
def test_deferred_round_member_keeps_siblings_and_other_tasks_running(tmp_path, selection, change_instruction):
    store, config = campaign(tmp_path, {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4]},
                             stop={'max_sessions': 3, 'window_k': 99})
    config = replace(config, max_parallel=3, defaults=replace(config.defaults,
                     agents_per_round=3, selection=selection))
    clock, attempts, batches, completed = [0.0], [], [], []

    class Gated(FakeAdapter):
        def instruction(self, task):
            return super().instruction(task) + (' Updated.' if change_instruction and clock[0] >= 10 else '')

        def submit_session(self, spec, directory, log):
            if spec.task == 'a' and spec.session_id == 's002':
                attempts.append((clock[0], spec))
                if len(attempts) <= 2:
                    raise SubmitDeferred('round gate', retry_after=10)
            return super().submit_session(spec, directory, log)

        def poll(self, jobs, log):
            batches.append([job.key for job in jobs])
            # Keep a sibling in flight while the other task advances.
            baselines = sorted(job.key for job in jobs if job.kind == 'baseline')
            key = baselines[0] if baselines else sorted(job.key for job in jobs)[-1]
            completed.append((clock[0], key))
            return {key}

    def sleep(seconds):
        clock[0] += seconds

    adapter = Gated(config)
    state = Scheduler(store, config, adapter, log=lambda _: None,
                      clock=lambda: clock[0], sleep=sleep).run()
    assert not state['paused'] and not state['waiting'] and state['tasks_stopped'] == 2
    assert state['sessions'] == 6 and max(map(len, batches)) == 3
    assert [time for time, _ in attempts] == [0, 10, 20]
    assert any(time == 0 and key == 'b/s003' for time, key in completed)
    assert any(time == 0 and key == 'a/s001' for time, key in completed)
    specs = [spec for _, spec in attempts]
    assert len({tuple(spec.parent_ids) for spec in specs}) == 1
    assert len({spec.archive_md for spec in specs}) == 1
    assert [spec.session_id for spec in adapter.submitted if spec.task == 'a'] == ['s001', 's002', 's003']
    assert len(events(store, 'submit_deferred')) == len(events(store, 'submit_resumed')) == 1
    refreshed = events(store, 'session_reprepared')
    assert len(refreshed) == int(change_instruction)
