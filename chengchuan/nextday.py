"""Compatibility command name for the replacement exact-two-up task.

The former generic forecast is no longer a product entry. Its internal research
modules and immutable historical archives remain available for audit.
"""

from chengchuan.two_up import main

if __name__ == "__main__":
    raise SystemExit(main())
