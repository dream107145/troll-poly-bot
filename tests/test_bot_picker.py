"""The dashboard shows several bots: every bot's state file and trade log is
reachable with ?bot=<id>, the list says which exist and run, and an id can
never become a path of its own choosing."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import websockets

from troll_poly_bot.history_store import HistoryStore
from troll_poly_bot.web import auth, server as srv

SLUG = "btc-updown-5m-1790260200"


def _state(mode, deploy, strategy="spot_lag", equity=50.0, **kw):
    d = {"mode": mode, "deploy_mode": deploy, "strategy": strategy, "equity": equity, "pnl": 0.0,
         "history_meta": {SLUG: 2}, "live_markets": [], "open_positions": []}
    d.update(kw)
    return d


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "LIVE_STATE", tmp_path / "live_state.json")
    monkeypatch.setattr(srv, "TRADE_LOG", tmp_path / "live_trades.jsonl")
    (tmp_path / "live_state.json").write_text(json.dumps(_state("LIVE-ARMED", "micro-live")))
    (tmp_path / "live_state.spotlag-paper.json").write_text(
        json.dumps(_state("live-paper", "paper", event_driven=True, equity=51.5)))
    old = tmp_path / "live_state.momentum.json"
    old.write_text(json.dumps(_state("LIVE-ARMED", "micro-live", strategy="momentum_taker")))
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    (tmp_path / "live_state.bad.name.json").write_text("{}")          # not a bot id: ignored
    (tmp_path / "live_state.spotlag-paper.history").mkdir()            # the chart store: ignored
    return tmp_path


# ───────────────────────────── ids and paths ─────────────────────────────


def test_ids_map_to_the_tagged_files_only(data_dir):
    assert srv.bot_paths(None) == (data_dir / "live_state.json", data_dir / "live_trades.jsonl")
    assert srv.bot_paths("main") == srv.bot_paths("") == srv.bot_paths(None)
    assert srv.bot_paths("spotlag-paper") == (data_dir / "live_state.spotlag-paper.json",
                                              data_dir / "live_trades.spotlag-paper.jsonl")


@pytest.mark.parametrize("bad", ["../etc", "a/b", ".hidden", "x" * 41, "a b", "a.b", "-x", "..", "%2e%2e"])
def test_an_id_that_could_be_a_path_is_refused(data_dir, bad):
    assert srv.bot_paths(bad) is None


# ─────────────────────────────── the list ────────────────────────────────


def test_the_list_says_which_bots_exist_and_run(data_dir):
    bots = srv._BotIndex().list()
    assert [b["id"] for b in bots] == ["main", "spotlag-paper", "momentum"]   # running first (no start times: by name)
    main, paper, old = bots
    assert main["running"] and main["managed"] and main["deploy_mode"] == "micro-live"
    assert paper["running"] and not paper["managed"] and paper["event_driven"] is True and paper["equity"] == 51.5
    assert not old["running"] and old["stale_s"] >= 7000 and old["strategy"] == "momentum_taker"


def test_the_list_is_in_recent_order(tmp_path, monkeypatch):
    """The user, 2026-10-07: newest first -- the running bots by their start (the newest run on top), then the
    stopped ones by when they stopped (their state file's last write)."""
    monkeypatch.setattr(srv, "LIVE_STATE", tmp_path / "live_state.json")
    monkeypatch.setattr(srv, "TRADE_LOG", tmp_path / "live_trades.jsonl")
    now = time.time()
    running = {"live_state.json": now - 86_400, "live_state.a-new.json": now - 60, "live_state.b-mid.json": now - 3_600}
    for name, started in running.items():
        (tmp_path / name).write_text(json.dumps(_state("live-paper", "paper", started=started)))
    for name, stopped in {"live_state.c-just-stopped.json": now - 200, "live_state.d-old.json": now - 7_200}.items():
        p = tmp_path / name
        p.write_text(json.dumps(_state("live-paper", "paper", started=now - 90_000)))
        os.utime(p, (stopped, stopped))
    bots = srv._BotIndex().list()
    assert [b["id"] for b in bots] == ["a-new", "b-mid", "main", "c-just-stopped", "d-old"]
    assert bots[0]["started"] == pytest.approx(now - 60) and bots[3]["last_ts"] == pytest.approx(now - 200, abs=1)


def test_a_torn_state_keeps_the_last_good_summary(data_dir):
    idx = srv._BotIndex()
    assert idx.list()[1]["equity"] == 51.5
    p = data_dir / "live_state.spotlag-paper.json"
    p.write_text('{"mode": "live-pap')                                        # caught mid-write
    assert next(b for b in idx.list() if b["id"] == "spotlag-paper")["equity"] == 51.5


# ────────────────────────── one bot's payload ────────────────────────────


def test_the_live_payload_is_the_chosen_bots(data_dir):
    state, _ = srv.bot_paths("spotlag-paper")
    st = HistoryStore(state)
    for t in (1, 2):
        st.append(SLUG, {"t": t, "up": 0.5})
    st.flush()
    out = srv.Handler._live_payload(SLUG, None, state_path=state, bot="spotlag-paper")
    assert out["bot"] == "spotlag-paper" and out["equity"] == 51.5
    assert [p["t"] for p in out["price_history"][SLUG]] == [1, 2]
    main = srv.Handler._live_payload(SLUG, None)
    assert main["bot"] == "main" and main["deploy_mode"] == "micro-live"
    assert main["price_history"] == {}                                    # main has no points for it


def test_the_ledger_is_the_chosen_bots(data_dir):
    (data_dir / "live_trades.jsonl").write_text("")
    (data_dir / "live_trades.spotlag-paper.jsonl").write_text(json.dumps(
        {"event": "settle", "slug": SLUG, "pnl": 1.25, "ts": 1.0, "mode": "live-paper"}) + "\n")
    assert srv._history(trade_log=srv.bot_paths("spotlag-paper")[1])["realised_pnl"] == 1.25
    assert srv._history()["settled"] == 0


# ───────────────────────────── over HTTP ─────────────────────────────────


@pytest.fixture()
def http(data_dir, monkeypatch):
    monkeypatch.setattr(srv, "WS_WATCH_S", 0.005)
    monkeypatch.setattr(srv, "SESSIONS", auth.Sessions())
    monkeypatch.setattr(srv, "CREDENTIALS",
                        auth.Credentials("admin@example.com", auth.hash_password("pw", rounds=1000)))
    monkeypatch.setattr(srv, "BOTS", srv._BotIndex())
    cookie = f"{auth.COOKIE}={srv.SESSIONS.create('admin@example.com')}"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"127.0.0.1:{httpd.server_address[1]}"

    def get(path):
        req = urllib.request.Request(f"http://{base}{path}", headers={"Cookie": cookie})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        yield get, base, {"Cookie": cookie}, data_dir
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_list_live_and_history_by_bot(http):
    get, _, _, _ = http
    code, d = get("/api/bots")
    assert code == 200 and [b["id"] for b in d["bots"]][:2] == ["main", "spotlag-paper"]
    code, d = get("/api/live?bot=spotlag-paper")
    assert code == 200 and d["bot"] == "spotlag-paper" and d["deploy_mode"] == "paper"
    code, d = get("/api/live")
    assert code == 200 and d["bot"] == "main"
    assert get("/api/live?bot=..%2Fsecret")[0] == 404
    assert get("/api/history?bot=a%2Fb")[0] == 404
    code, d = get("/api/live?bot=nobody")
    assert code == 200 and d == {"running": False, "bot": "nobody"}      # a valid id with no bot yet


@pytest.mark.asyncio
async def test_the_stream_follows_the_chosen_bot(http):
    _, base, headers, data_dir = http
    state = data_dir / "live_state.spotlag-paper.json"
    stop = threading.Event()

    def writer():                                                          # the bot's state_loop
        while not stop.wait(0.02):
            tmp = state.with_suffix(".tmp")
            tmp.write_text(json.dumps(_state("live-paper", "paper", equity=52.0)))
            tmp.replace(state)

    threading.Thread(target=writer, daemon=True).start()
    try:
        async with websockets.connect(f"ws://{base}/ws?bot=spotlag-paper", additional_headers=headers) as conn:
            msg = json.loads(await asyncio.wait_for(conn.recv(), 5))
            assert msg["bot"] == "spotlag-paper" and msg["deploy_mode"] == "paper"
    finally:
        stop.set()


# ───────────────────────────── the Restart modal's "fresh" choice (2026-10-13) ─────────────────────────────

def test_fresh_paths_are_the_tagged_files_only(data_dir):
    d = data_dir
    for name in ("live_trades.lp-paper.jsonl", "open_positions.lp-paper.json", "leader_pair_state.lp-paper.json",
                 "decisions.lp-paper.jsonl", "live_state.lp-paper.json", "live_trades.other-paper.jsonl", "earnings.jsonl",
                 "live_trades.jsonl"):
        (d / name).write_text("x")
    (d / "live_state.lp-paper.history").mkdir()
    (d / "charts").mkdir()
    got = {p.name for p in srv.fresh_paths("lp-paper")}
    assert got == {"live_trades.lp-paper.jsonl", "open_positions.lp-paper.json", "leader_pair_state.lp-paper.json",
                   "decisions.lp-paper.jsonl", "live_state.lp-paper.json", "live_state.lp-paper.history"}
    assert srv.fresh_paths("../x") == [] and srv.fresh_paths("") == []


def test_fresh_restart_stops_removes_and_restarts_but_never_a_real_money_bot(data_dir, monkeypatch):
    d = data_dir
    (d / "live_trades.lp-paper.jsonl").write_text("row\n")
    (d / "leader_pair_state.lp-paper.json").write_text("{}")
    (d / "earnings.jsonl").write_text("keep\n")
    calls = []
    apps = [{"name": "tpb-lp-paper", "status": "online", "tag": "lp-paper", "cwd": str(srv.REPO_ROOT), "live": False},
            {"name": "tpb-x-live", "status": "online", "tag": "x-live", "cwd": str(srv.REPO_ROOT), "live": True}]
    monkeypatch.setattr(srv, "_pm2_apps", lambda: apps)
    monkeypatch.setattr(srv, "_ecosystem_apps", lambda: [])
    monkeypatch.setattr(srv, "_pm2", lambda *a: (calls.append(a), (0, "ok"))[1])
    out = srv.pm2_control("restart", "lp-paper", fresh=True)
    assert out["ok"] and calls == [("stop", "tpb-lp-paper"), ("restart", "tpb-lp-paper")]
    assert sorted(out["removed"]) == ["leader_pair_state.lp-paper.json", "live_trades.lp-paper.jsonl"]
    assert not (d / "live_trades.lp-paper.jsonl").exists() and (d / "earnings.jsonl").read_text() == "keep\n"
    assert "fresh" in out["reason"]
    # a plain restart deletes nothing
    (d / "live_trades.lp-paper.jsonl").write_text("row\n")
    calls.clear()
    out = srv.pm2_control("restart", "lp-paper")
    assert calls == [("restart", "tpb-lp-paper")] and (d / "live_trades.lp-paper.jsonl").exists() and out["removed"] == []
    # a real-money app is refused the fresh restart and nothing of it is touched
    (d / "live_trades.x-live.jsonl").write_text("real\n")
    calls.clear()
    out = srv.pm2_control("restart", "x-live", fresh=True)
    assert out["ok"] is False and "real money" in out["reason"] and calls == [] and (d / "live_trades.x-live.jsonl").exists()
    # the live flag comes from the app's arguments
    monkeypatch.undo()
    monkeypatch.setattr(srv, "_pm2", lambda *a: (0, json.dumps([
        {"name": "a", "pm2_env": {"status": "online", "args": ["-m", "x", "--mode", "micro-live", "--data-tag", "a-live"], "pm_cwd": "/r"}},
        {"name": "b", "pm2_env": {"status": "online", "args": "-m x --mode paper --data-tag b-paper", "pm_cwd": "/r"}},
        {"name": "c", "pm2_env": {"status": "online", "args": ["--live", "--armed", "--data-tag", "c"], "pm_cwd": "/r"}}])))
    assert {a["name"]: a["live"] for a in srv._pm2_apps()} == {"a": True, "b": False, "c": True}
