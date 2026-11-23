# Real-agent parallel-round verification

Codex gpt-6-astra with xhigh reasoning; 2 rounds × 2 agents. Genetic population 2, elites 2, crossover and mutation rates 1. The first round intentionally produces complementary strip/lowercase parents; second-round agents combine them and add whitespace collapsing. No scripted model is used.

| Session | Parents | Agent seconds | Functional checks | Valid |
|---|---|---:|---:|---|
| s001 | s000 | 38.9 | 2/6 | yes |
| s002 | s000 | 45.9 | 2/6 | yes |
| s003 | s001, s002 | 46.5 | 6/6 | yes |
| s004 | s001, s002 | 52.0 | 6/6 | yes |

Independent restoration and evaluation of all four archived patches passed. Both second-round prompts contain the full secondary patch; both restored primaries match their parent and both offspring implement the combined behaviors. Same-round log activity intervals overlap by 37.4 s and 44.9 s.

Synthetic directed integration task. Not evidence of search quality or speedup. Log creation/last-write timestamps bound overlapping activity; not model-server execution timestamps.
