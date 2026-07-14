"""Thin CLI façade over the approved engine.

The engine (`_engine.py`) is the monolith that passed multi-round
cross-model code review (see docs/FINDINGS.md for the empirical safety
contract). Per the packaging plan, it is shipped intact: this module only
provides the console entry point. Domain extraction happens one module per
PR after 0.9, behind this façade, with full regression each time.
"""

from skrepka._engine import main


if __name__ == "__main__":
    main()
