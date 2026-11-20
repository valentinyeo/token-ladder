import json, os, subprocess, types
from _load import tl

cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
assert cfg["push_token_key"] == ["Multiprompt Account", "USAGE_PUSH_TOKEN"]
assert "push_token_file" not in cfg

os.makedirs(os.path.dirname(tl.STATUS), exist_ok=True)
with open(tl.STATUS, "w") as f:
    f.write("{}")

calls, sent = [], []
real_run, real_open = tl.subprocess.run, tl.urllib.request.urlopen


def fake_run(cmd, **kw):
    calls.append(cmd)
    return types.SimpleNamespace(stdout="fake-token\n")


def fake_open(req, timeout):
    sent.append(req.headers["Authorization"])
    return types.SimpleNamespace(status=200)


tl.subprocess.run, tl.urllib.request.urlopen = fake_run, fake_open
try:
    assert tl.push_usage(cfg)
    assert calls == [["yeo-key", "Multiprompt Account", "USAGE_PUSH_TOKEN"]]
    assert sent == ["Bearer fake-token"]
    assert not tl.push_usage({"push_url": cfg["push_url"]})

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    tl.subprocess.run = missing
    assert not tl.push_usage(cfg)

    def failing(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    tl.subprocess.run = failing
    assert not tl.push_usage(cfg)
finally:
    tl.subprocess.run, tl.urllib.request.urlopen = real_run, real_open

assert cfg["key_lookup_every_s"] >= 1800
assert tl.due("push-test", 1800, 1000.0)
assert not tl.due("push-test", 1800, 2799.0)
assert tl.due("push-test", 1800, 2800.0)
assert not tl.due("push-test", 1800, 2801.0)
