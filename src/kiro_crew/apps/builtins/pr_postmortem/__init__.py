"""PR Postmortem — links each merged fix PR to the PR that introduced the bug.

Attribution is mechanical: the lines a fix deleted or rewrote are blamed at the
fix's parent commit, and the resulting commits roll up to their pull requests. A
model then explains why review and tests missed the defect, and the findings
aggregate into prevention proposals a human accepts one at a time.

Required re-export: the gateway's startup route registration imports THIS package
and checks ``hasattr(_mod, "register_routes")`` on the package itself, not on the
``backend.routes`` submodule.
"""

from .backend.routes import register_routes  # noqa: F401
