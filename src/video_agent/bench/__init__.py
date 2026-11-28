"""aco-bench: run a story brief through the live aco (local or online brain), with a scripted user,
then score what was delivered and how.

    bench/cases/*.yaml      the cases (our own short stories, and VAHBench's briefs once imported)
    va bench run CASE       run one case against the running daemon → data/bench/runs/<run>/
    va bench score RUN      aco metrics + face consistency + online judge (+ VAHBench C/Q/P)
    va bench report         one table over all scored runs

What is measured (see metrics.py): time (wall, LLM, render, per delivered second, wasted),
loops and repeated calls, self-review before scaling up, LoRA use and base-model fit, failed
submissions, steering latency; faces.py: identity against the casting reference; judge.py: an
online judge on contact sheets (DramaChain-style axes) plus steering compliance and honesty;
vahbench.py: the VAHBench evaluator on the exported run.
"""
