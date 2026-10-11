# analyse-act-agent

Headless [Analyse-Act (AOI-PEV)](../README.md) runner for CI, built on LangGraph. `aa-agent run` goes through the stages and stops at the [P] gate. `aa-agent resume --choice A` continues to [E] and [V]. The checkpoints live in `.aa/agent.sqlite`. See "Headless / CI" in the main README.
