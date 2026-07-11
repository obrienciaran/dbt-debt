"""Report layer: assemble verdicts into a scorecard and render it.

This is the composition root for the verdict functions, so it may import `verdict` and
`consumption` resolution helpers (it is not the pure layer). Rendering is split from assembly so
the same `Scorecard` drives both the terminal and JSON outputs.
"""
