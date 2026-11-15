"""Our clean-room `seesaw` must export every symbol real ArchiveTeam projects import.

The expected surface is the contract in ARCHITECTURE.md, extracted from five current
projects (reddit/tumblr/telegram/imgur/urls). Reference-free and offline, so it's safe for
CI (unlike the golden generator, which reads reference/).
"""

import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "worker"))

# module -> symbols that project pipeline.py files import from it.
SURFACE = {
    "seesaw": ["__version__"],
    "seesaw.project": ["Project"],
    "seesaw.pipeline": ["Pipeline"],
    "seesaw.item": ["ItemValue", "ItemInterpolation"],
    "seesaw.task": ["SimpleTask", "LimitConcurrent"],
    "seesaw.externalprocess": ["ExternalProcess", "WgetDownload", "RsyncUpload", "CurlUpload"],
    "seesaw.tracker": ["GetItemFromTracker", "PrepareStatsForTracker",
                       "UploadWithTracker", "SendDoneToTracker"],
    "seesaw.config": ["realize", "NumberConfigValue", "StringConfigValue"],
    "seesaw.util": ["find_executable"],
}


def test_every_imported_symbol_exists():
    missing = []
    for module_name, symbols in SURFACE.items():
        module = importlib.import_module(module_name)
        for sym in symbols:
            if not hasattr(module, sym):
                missing.append(f"{module_name}.{sym}")
    assert not missing, f"seesaw surface is missing: {missing}"


def test_version_satisfies_project_gate():
    import seesaw
    from distutils.version import StrictVersion  # projects use this exact check
    assert StrictVersion(seesaw.__version__) >= StrictVersion("0.8.5")


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
