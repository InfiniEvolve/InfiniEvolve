# Real-agent parallel-round verification

Codex gpt-6-astra with xhigh reasoning; 2 rounds × 2 agents. Genetic population 2, elites 2, crossover and mutation rates 1. The first round intentionally produces complementary strip/lowercase parents; second-round agents combine them and add whitespace collapsing. No scripted model is used.

| Session | Parents | Agent seconds | Functional checks | Valid |
|---|---|---:|---:|---|
| s001 | s000 | 51.1 | 2/6 | yes |
| s002 | s000 | 60.7 | 2/6 | yes |
| s003 | s001, s002 | 53.6 | 6/6 | yes |
| s004 | s001, s002 | 51.7 | 6/6 | yes |

Independent restoration and evaluation of all four archived patches passed. Both second-round prompts contain the full secondary patch; both restored primaries match their parent and both offspring implement the combined behaviors. Same-round log activity intervals overlap by 49.5 s and 49.1 s.

Synthetic directed integration task. Not evidence of search quality or speedup. Log creation/last-write timestamps bound overlapping activity; not model-server execution timestamps.

The validity gate checks that the function returns strings; the ranking score counts six functional cases. Partial first-generation implementations are intentionally valid but score only 2/6. This report describes the final-code rerun after the review fixes. The earlier independent run also completed successfully.
