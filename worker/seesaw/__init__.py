"""Pipeline runtime, imported by project pipelines as ``seesaw``.

It carries the name ``seesaw`` only because existing ArchiveTeam ``pipeline.py`` files
import it by that name. Compatibility is defined by the wire protocol (Warrior HQ +
Universal Tracker) and the public API these modules expose: project, pipeline, item,
task, externalprocess, tracker, config, util.
"""

# Identity reported to the tracker. These values make our User-Agent identical to the
# ArchiveTeam Warrior's ("ArchiveTeam Warrior/0.10.3 Warrior"), so tracker requests aren't
# treated differently; the version also satisfies projects' StrictVersion gates.
__version__ = "0.10.3"
runner_type = "Warrior"
warrior_build = ""
