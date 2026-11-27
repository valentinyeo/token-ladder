import importlib.machinery
import importlib.util
import os
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="token-ladder-test-home-")
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ISOLATED_LOCAL_CONFIG = os.path.join(REPO, ".test-missing-config.local.json")
os.environ["TOKEN_LADDER_LOCAL_CONFIG"] = ISOLATED_LOCAL_CONFIG


def load_bin(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), os.path.join(REPO, "bin", name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module
