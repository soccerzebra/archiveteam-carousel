"""carousel worker.

One worker process runs exactly one project's pipeline. It is launched by the control
plane's supervisor and reports status back over a line-delimited JSON channel on stdout.

The ``seesaw`` package next to this module is the pipeline runtime that project
``pipeline.py`` files import by that name.
"""
