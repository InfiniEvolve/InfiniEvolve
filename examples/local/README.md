# A local adapter

[`adapter.py`](adapter.py) is a complete adapter in one file. Every job is a process on this machine,
and a session's work is a patch against the task's baseline. It is meant to be copied and changed.

```
adapter.py            the adapter, and the program each job runs
fake_agent.py         a stand-in for a coding agent, so the example runs without one
tasks/golf/           a toy task: make a correct program as short as possible
  task.yaml           instruction, declaration, prompt sentences, how to evaluate
  evaluate.py         prints the checks and metrics of one version
  workspace/          the baseline
campaigns/demo/       a campaign over that task
```

From the repository root:

```sh
infinievolve run examples/local/campaigns/demo
infinievolve report examples/local/campaigns/demo --task golf
```

The stand-in agent makes one mechanical edit per session and then runs out of ideas, so the lineage
improves for five sessions, the next two change nothing (and are noted as duplicates), and the stop
rule ends the task.

To use a real agent, set `options.agent_command` in the campaign's `config.yaml` to a shell command that
hands it the prompt. The command runs in the working copy with `EVOLVE_PROMPT`, `EVOLVE_ARCHIVE`,
`EVOLVE_WORKSPACE` and `EVOLVE_CAMPAIGN` set. A real agent edits files and runs commands in that
directory without asking, so run it somewhere you would let it do that.

What this adapter leaves out, because it depends on where you run: a sandbox around the agent, running
on other machines, reading the agent's token usage, recognising a refused agent (`AgentUnavailable`),
and checks of what the agent did during the session.
