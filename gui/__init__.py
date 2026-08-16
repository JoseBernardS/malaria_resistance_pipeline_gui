"""Desktop GUI for the Plasmodium falciparum drug-resistance pipeline.

A PyQt6 layer on top of the existing bash pipeline
(``bin/pf-drug-resistance-pipeline.sh``) and ``src/generate_report.py``.
The pipeline itself is reused unchanged; this package only adds job
management, progress monitoring, persistence and a results dashboard.
"""

__all__ = ["app", "db", "paths", "config_bridge", "runner", "queue"]
