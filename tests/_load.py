import importlib.machinery
import importlib.util
import os
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="token-ladder-test-home-")
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def load_bin(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), os.path.join(REPO, "bin", name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


tl = load_bin("token-ladder")
assert tl.HOME == os.environ["HOME"]
