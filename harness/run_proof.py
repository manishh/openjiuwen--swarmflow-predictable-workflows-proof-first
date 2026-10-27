"""Proof harness: drives a real JiuwenSwarm 0.2.7 server through SwarmFlow scenarios A-D
(see PROOF.md) and checks the pass bar. Exit code 0 only if every check passes in every repeat."""
import argparse, asyncio, glob, json, os, re, shutil, subprocess, sys, tempfile, time, uuid
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sfclient import Server, Client, payload, etype  # noqa: E402
from setup_home import make_home  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIPE = ROOT / "pipeline"
VENV_BIN = Path(sys.executable).parent
FEATURE_ARGS = json.dumps({"feature": "turn blog post titles into URL slugs"})
LONG_TITLE = "How to Keep an AI Workflow Predictable Using JiuwenSwarm's SwarmFlow Checkpoints"
EDIT_REPLY = ("Edit the codegen step: slugify must take a max_len parameter (default 40) "
              "and truncate the slug to that length. Then re-run.")
NO_TOUCH = "Do not edit the script and do not relaunch it yourself."
BIG_BUDGET, SMALL_TEAM_BUDGET = 3_000_000, 100_000  # SMALL_ is only a fallback
WAIT = 900


# ---------------------------------------------------------------- evidence helpers
class Repeat:
    def __init__(self, n, out):
        self.n, self.out = n, out
        self.md, self.checks = [f"# Repeat {n}\n"], []

    def log(self, text=""):
        self.md.append(text)
        print(text if len(text) < 400 else text[:400] + " ...", flush=True)

    def block(self, title, body, lang=""):
        self.md.append(f"**{title}**\n\n```{lang}\n{body}\n```\n")

    def check(self, cid, desc, ok, detail=""):
        ok = bool(ok)
        self.checks.append({"id": cid, "check": desc, "pass": ok, "detail": str(detail)[:600]})
        self.log(f"- [{'PASS' if ok else 'FAIL'}] **{cid}** {desc}" + (f" — {str(detail)[:300]}" if detail else ""))

    @property
    def passed(self):
        return bool(self.checks) and all(c["pass"] for c in self.checks)


def is_sess(f, sid):
    return payload(f).get("session_id") == sid


def wf_of(f):
    return payload(f).get("workflow") or {}


def merged(frames, sid, run_id):
    """workflow.updated events are deltas (often only the phases that changed); fold them
    into one snapshot: top-level fields from the latest event, phases/agents merged by id."""
    top, phases = {}, {}
    for f in frames:
        if not (is_sess(f, sid) and etype(f) == "workflow.updated" and wf_of(f).get("id") == run_id):
            continue
        wf = wf_of(f)
        top.update({k: v for k, v in wf.items() if k != "phases"})
        for ph in wf.get("phases") or []:
            cur = phases.setdefault(ph.get("name"), {"agents": {}})
            cur.update({k: v for k, v in ph.items() if k != "agents"})
            for a in ph.get("agents") or []:
                cur["agents"].setdefault(a.get("id"), {}).update(a)
    top["phases"] = [dict(ph, agents=list(ph["agents"].values())) for ph in phases.values()]
    return top


def agents_of(wf):
    return {a.get("name"): a for ph in wf.get("phases") or [] for a in ph.get("agents") or []}


def run_tree(wf):
    """Text rendering of a workflow.updated snapshot (what a run-tree view shows)."""
    b, w = wf.get("budget") or {}, wf.get("workflow_budget") or {}
    lines = [f"{wf.get('id')}  {wf.get('name')}  [{wf.get('status')}]  run tokens={wf.get('token_count')}",
             f"  budget/session : spent={b.get('spent')} total={b.get('total')} exhausted={b.get('exhausted')}",
             f"  budget/workflow: spent={w.get('spent')} total={w.get('total')} exhausted={w.get('exhausted')}"]
    if wf.get("budget_exhausted_scope"):
        lines.append(f"  budget_exhausted_scope={wf['budget_exhausted_scope']}")
    for ph in wf.get("phases") or []:
        lines.append(f"  ├─ phase {ph.get('name')} [{ph.get('status')}]")
        for a in ph.get("agents") or []:
            lines.append(f"  │   └─ {a.get('name')} [{a.get('status')}] tokens={a.get('token_count')}")
    return "\n".join(lines)


def wal_records(home, run_id):
    paths = glob.glob(str(home / ".jiuwenswarm" / ".agent_teams" / "**" / "wal" / f"{run_id}.wal"), recursive=True)
    recs = []
    for p in paths:
        for line in Path(p).read_text().splitlines():
            if line.strip():
                recs.append(json.loads(line))
    return recs, paths


def wal_summary(recs):
    out = []
    for r in recs:
        if r.get("type") in ("pause", "seal", "args") or "sig" not in r:
            out.append(json.dumps({k: r.get(k) for k in ("key", "type", "pause_reason", "terminal_status", "args") if r.get(k) is not None}))
        else:
            out.append(f"{r['key']} label={r.get('label')} sig={r['sig'][:12]} tokens={r.get('tokens')}")
    return "\n".join(out)


def wf_log_lines(home, sid):
    lines = {}
    for p in sorted(glob.glob(str(home / ".jiuwenswarm" / "logs" / "**" / "*.log"), recursive=True)):
        try:
            for line in open(p, errors="replace"):
                if sid in line and ("[wf] agent" in line or "[wf] session" in line or "[wf] journal loaded" in line
                                    or "Edit points:" in line or "asked to edit the script" in line):
                    lines.setdefault(line.rstrip()[:700], len(lines))
        except OSError:
            pass
    # stable sort by the timestamp prefix keeps same-second lines in file order
    return sorted(lines, key=lambda l: (l[:19], lines[l]))


def guidance_text(home, run_id):
    """Find the exact budget-exhausted message the Leader received, in the trace store."""
    marker = f"[Swarmflow budget exhausted] run_id={run_id}"

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
        elif isinstance(o, str) and marker in o:
            t = o.strip()
            if t[:1] in "{[":
                try:
                    yield from walk(json.loads(t))
                    return
                except ValueError:
                    pass
            yield o[o.index(marker):]

    for p in glob.glob(str(home / ".jiuwenswarm" / ".trace" / "*.jsonl")):
        for line in open(p, errors="replace"):
            if marker in line:
                try:
                    for s in walk(json.loads(line)):
                        return s
                except ValueError:
                    continue
    return None


def strip_fences(code):
    code = (code or "").strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*?)\n```$", code, re.S)
    return m.group(1) if m else code


def exec_slugify(code, workdir):
    """Execute model-generated code in a subprocess and call slugify on a long title."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "generated_slugify.py").write_text(strip_fences(code))
    probe = ("import json, inspect, generated_slugify as gen\n"
             f"s = gen.slugify({LONG_TITLE!r})\n"
             "print(json.dumps({'slug': s, 'len': len(s), 'params': list(inspect.signature(gen.slugify).parameters)}))")
    r = subprocess.run([sys.executable, "-c", probe], cwd=workdir, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {"error": r.stderr[-500:]}
    return json.loads(r.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- protocol helpers
async def send(c, rep, sid, msg, budget):
    rep.log(f"\n→ `chat.send` session={sid} mode=team enable_swarmflow=true swarmflow_budget={budget}\n\n> {msg}\n")
    await c.req("chat.send", {"session_id": sid, "mode": "team", "content": msg, "query": msg,
                              "enable_swarmflow": True, "swarmflow_budget": budget})


async def launch(c, rep, sid, msg, budget, script_path):
    """Ask the Leader to call swarmflow; return (run_id, tool_args). Retries only the
    precondition 'the Leader called swarmflow with this script' (recorded)."""
    for attempt in range(1, 4):
        start = len(c.frames)
        await send(c, rep, sid, msg, budget)
        try:
            _, tr = await c.wait_for(lambda f: is_sess(f, sid) and (etype(f) == "chat.error" or (
                etype(f) == "chat.tool_result" and payload(f).get("tool_name") == "swarmflow")), 300, start)
            if etype(tr) == "chat.error":  # e.g. provider quota/auth errors: fail fast, no retry
                raise RuntimeError(f"Leader turn failed: {str(payload(tr).get('error'))[:400]}")
        except TimeoutError:
            rep.log(f"⚠️ precondition retry {attempt}: the Leader did not call swarmflow within 300 s")
            continue
        calls = [payload(f)["tool_call"] for f in c.frames[start:] if is_sess(f, sid) and etype(f) == "chat.tool_call"
                 and (payload(f).get("tool_call") or {}).get("name") == "swarmflow"]
        args = json.loads(calls[-1]["arguments"]) if calls else {}
        rep.log(f"← Leader tool call `swarmflow({json.dumps(args)})`")
        result = str(payload(tr).get("result"))
        m = re.search(r"run_id['\"]?:\s*['\"](wf_[0-9a-f]+)", result)
        if m and str(script_path) == args.get("script_path"):
            rep.log(f"← swarmflow launched run_id={m.group(1)}")
            return m.group(1), args
        rep.log(f"⚠️ precondition retry {attempt}: unexpected swarmflow call/result: {result[:300]}")
    raise RuntimeError("Leader never launched swarmflow with the requested script")


async def wait_human(c, sid, start):
    return await c.wait_for(lambda f: is_sess(f, sid) and etype(f) == "chat.ask_user_question"
                            and payload(f).get("source") == "swarmflow_human", WAIT, start)


async def wait_status(c, sid, run_id, statuses, start):
    return await c.wait_for(lambda f: is_sess(f, sid) and etype(f) == "workflow.updated"
                            and wf_of(f).get("id") == run_id and wf_of(f).get("status") in statuses, WAIT, start)


async def settle(c, sid, start, timeout=240, quiet=20):
    """Wait for the Leader's reaction turns to finish (no new chat.final for `quiet` s),
    so the next step starts clean. Returns all final messages, in order."""
    finals, idx, wait = [], start, timeout
    deadline = time.time() + timeout + 120
    while time.time() < deadline:
        try:
            i, f = await c.wait_for(lambda f: is_sess(f, sid) and etype(f) == "chat.final", wait, idx)
        except TimeoutError:
            break
        finals.append(payload(f).get("content") or "")
        idx, wait = i + 1, quiet
    return "\n---\n".join(finals) if finals else None


async def reply(c, rep, sid, meta, answer):
    rep.log(f"\n→ `chat.swarmflow_reply` run_id={meta['run_id']} correlation_id={meta['correlation_id']}\n\n> {answer}\n")
    await c.req("chat.swarmflow_reply", {"session_id": sid, "run_id": meta["run_id"],
                                         "correlation_id": meta["correlation_id"], "answer": answer})


# ---------------------------------------------------------------- scenarios
async def scenario_a(c, rep, home, scripts, tmp):
    rep.log("\n## Scenario A: human checkpoint, edit request, partial re-run\n")
    sid = "sess_a" + uuid.uuid4().hex[:8]
    script = scripts / "slugify_pipeline.py"
    shutil.copy(PIPE / "slugify_pipeline.py", script)
    v1_text = script.read_text()
    run_id, _ = await launch(c, rep, sid, f"Run swarmflow(script_path='{script}', args='{FEATURE_ARGS}').", BIG_BUDGET, script)
    i, q = await wait_human(c, sid, 0)
    meta = payload(q)["swarmflow_meta"]
    rep.block("Checkpoint question (chat.ask_user_question)", payload(q)["questions"][0]["question"])
    rep.check("A1.question", "human() checkpoint question arrives (source=swarmflow_human) for this run",
              payload(q).get("source") == "swarmflow_human" and meta.get("run_id") == run_id, meta)
    snap1 = merged(c.frames[:i + 1], sid, run_id)
    rep.block("Run tree at the checkpoint", run_tree(snap1))
    ag1 = agents_of(snap1)
    recs, _ = wal_records(home, run_id)
    rep.check("A1.gate", "while waiting for the human, the downstream `docs` stage has not run",
              "docs" not in ag1 and not any(r.get("label") == "docs" for r in recs) and "spec" in ag1 and "codegen" in ag1,
              f"agents so far: {list(ag1)}")
    spec1, code1 = (ag1.get("spec") or {}).get("outcome"), (ag1.get("codegen") or {}).get("outcome")
    r1 = exec_slugify(code1, tmp / "v1")
    rep.block("v1 generated code, executed on a long title", json.dumps(r1))
    rep.check("A1.control", "control: v1 slugify has no max_len parameter and, called with defaults, returns a slug longer than 40 characters",
              "error" not in r1 and "max_len" not in r1.get("params", []) and r1.get("len", 0) > 40, r1)

    # Action 1: ask for an edit
    n = len(c.frames)
    await reply(c, rep, sid, meta, EDIT_REPLY)
    i, f = await wait_status(c, sid, run_id, {"paused", "failed", "completed", "stopped"}, n)
    rep.block("Run tree after the edit request", run_tree(merged(c.frames[:i + 1], sid, run_id)))
    rep.check("A2.paused", "after the edit request the run is `paused` (not failed, not completed)",
              wf_of(f).get("status") == "paused", wf_of(f).get("status"))
    recs, paths = wal_records(home, run_id)  # read at the pause, before the Leader can react
    rep.block(f"Run journal (WAL) after pause: {Path(paths[0]).relative_to(home) if paths else 'missing'}", wal_summary(recs))
    pauses = [r for r in recs if r.get("type") == "pause"]
    rep.check("A2.wal_pause", "run journal holds a resumable pause record with pause_reason=early_return",
              any(r.get("pause_reason") == "early_return" for r in pauses), pauses)
    rep.check("A2.docs_absent", "`docs` has no journal record (it never ran)",
              recs and not any(r.get("label") == "docs" for r in recs), [r.get("label") for r in recs])
    logs = wf_log_lines(home, sid)
    edit_lines = [l for l in logs if "Edit points:" in l]
    rep.block("Engine log: intent classification", "\n".join(edit_lines) or "(none)")
    rep.check("A2.edit_rerun", "the reply was classified as edit_rerun (engine emitted 'Edit points')",
              any(run_id in l or "asked to edit" in l for l in logs) and edit_lines, len(edit_lines))

    # Action 2: SwarmFlow hands the "Edit points" to the Leader, whose job is to edit the on-disk
    # script and relaunch with resume_id. Wait for it to do so; if it only asks for confirmation,
    # confirm once (recorded). The harness itself never touches the script.
    def resume_call(f):
        tc = payload(f).get("tool_call") or {}
        if not (is_sess(f, sid) and etype(f) == "chat.tool_call" and tc.get("name") == "swarmflow"):
            return False
        try:
            return json.loads(tc.get("arguments") or "{}").get("resume_id") == run_id
        except ValueError:
            return False
    n = i
    try:
        k, rc = await c.wait_for(resume_call, 240, n)
        rep.log("\nThe Leader acted on the edit request by itself (no further prompt).")
    except TimeoutError:
        rep.log("\n⚠️ precondition nudge: the Leader did not apply the edit within 240 s; confirming once.")
        await send(c, rep, sid, f"Yes: apply the edit points to the script on disk (edit minimally, keep META name) "
                                f"and relaunch swarmflow with resume_id='{run_id}' and the same script_path.", BIG_BUDGET)
        k, rc = await c.wait_for(resume_call, 300, n)
    edits = [payload(f)["tool_call"] for f in c.frames[n:k] if is_sess(f, sid) and etype(f) == "chat.tool_call"
             and (payload(f).get("tool_call") or {}).get("name") in ("edit_file", "write_file", "bash", "multi_edit")]
    for e in edits:
        rep.log(f"← Leader tool call `{e['name']}({e.get('arguments', '')[:600]})`")
    args2 = json.loads(payload(rc)["tool_call"]["arguments"])
    rep.log(f"← Leader tool call `swarmflow({json.dumps(args2)})`")
    v2_text = script.read_text()
    (rep.out / "slugify_pipeline.after-leader-edit.py").write_text(v2_text)
    diff = subprocess.run(["diff", "-u", "--label", "before", "--label", "after", "-", str(script)], input=v1_text,
                          capture_output=True, text=True).stdout
    rep.block("Script diff made by the Leader (from the Edit points)", diff or "(no change)", "diff")
    rep.check("A3.leader_edit", "the Leader edited the on-disk script and relaunched it with resume_id=<run_id> and the same script_path",
              v2_text != v1_text and args2.get("resume_id") == run_id and args2.get("script_path") == str(script),
              f"edit tool calls={len(edits)} resume_id={args2.get('resume_id')}")
    rep.check("A3.same_run", "the resumed run keeps the same run_id (no new run was started in this session)",
              not any(etype(f) == "workflow.updated" and is_sess(f, sid) and wf_of(f).get("id") not in (None, run_id)
                      for f in c.frames), run_id)
    i, q2 = await wait_human(c, sid, k)
    meta2 = payload(q2)["swarmflow_meta"]
    rep.block("Checkpoint question after resume", payload(q2)["questions"][0]["question"])
    ag2 = agents_of(merged(c.frames[k:i + 1], sid, run_id))
    logs = wf_log_lines(home, sid)
    rep.block("Engine log: journal decisions (both passes)", "\n".join(l for l in logs if "[wf]" in l))
    # Log timestamps have 1 s resolution and lines are spread over several files, so the
    # checks do not rely on line order. spec can only be a CACHE_HIT on a resumed pass.
    reloaded = any("journal loaded" in l and "prior_records=0" not in l for l in logs)
    spec_hits = [l for l in logs if "'spec'" in l and "CACHE_HIT" in l]
    spec_live = [l for l in logs if "'spec'" in l and "CACHE_MISS" in l]
    code_sigs = {re.search(r"sig=(\w+)", l).group(1) for l in logs if "'codegen'" in l and "CACHE_MISS" in l}
    hit_spec = reloaded and len(spec_hits) >= 1 and len(spec_live) == 1
    miss_code = len(code_sigs) >= 2   # live on both passes, with different prompt signatures
    rep.check("A3.cache", "on resume the journal is reloaded: `spec` CACHE_HIT (replayed), `codegen` CACHE_MISS (re-run)",
              hit_spec and miss_code, f"journal reloaded={reloaded} spec live calls={len(spec_live)} spec cache hits={len(spec_hits)} codegen live sigs={sorted(code_sigs)}")
    spec2, code2 = (ag2.get("spec") or {}).get("outcome"), (ag2.get("codegen") or {}).get("outcome")
    rep.check("A3.spec_identical", "`spec` output after resume is byte-identical to the first pass (replayed, not regenerated)",
              spec1 and spec1 == spec2, f"len={len(spec1 or '')}")
    r2 = exec_slugify(code2, tmp / "v2")
    rep.block("v2 generated code, executed on the same long title", json.dumps(r2))
    rep.check("A3.edit_applied", "edited code has a max_len parameter and returns a slug of at most 40 characters",
              "error" not in r2 and "max_len" in r2.get("params", []) and 0 < r2.get("len", 0) <= 40, r2)

    n = len(c.frames)
    await reply(c, rep, sid, meta2, "approve")
    i, f = await wait_status(c, sid, run_id, {"paused", "failed", "completed", "stopped"}, n)
    await asyncio.sleep(2)
    wf = merged(c.frames[n:], sid, run_id)
    rep.block("Final run tree (resumed pass)", run_tree(wf))
    rep.block("Workflow result (as reported)", str(wf.get("result") or "")[:3000])
    recs, _ = wal_records(home, run_id)
    rep.block("Run journal (WAL) at the end", wal_summary(recs))
    rep.check("A4.completed", "after `approve` the run completes and `docs` runs",
              wf.get("status") == "completed" and (agents_of(wf).get("docs") or {}).get("status") == "completed"
              and any(r.get("label") == "docs" for r in recs), wf.get("status"))
    await settle(c, sid, i)


async def budget_run(c, rep, home, scripts, tag, script_name, team_budget):
    sid = f"sess_{tag.lower()}" + uuid.uuid4().hex[:8]
    script = scripts / script_name
    shutil.copy(PIPE / script_name, script)
    run_id, _ = await launch(c, rep, sid, f"Run swarmflow(script_path='{script}', args='{{}}'). {NO_TOUCH}", team_budget, script)
    i, f = await wait_status(c, sid, run_id, {"paused", "failed", "completed", "stopped"}, 0)
    await asyncio.sleep(2)
    wf = merged(c.frames, sid, run_id)
    rep.block(f"Run tree ({script_name}, swarmflow_budget={team_budget})", run_tree(wf))
    recs, _ = wal_records(home, run_id)
    rep.block("Run journal (WAL)", wal_summary(recs))
    leader = await settle(c, sid, i)
    rep.block("Leader's reaction (chat.final)", leader or "(none)")
    return run_id, wf, recs


async def scenario_bcd(c, rep, home, scripts):
    rep.log("\n## Scenario B: control — no per-run limit, large team budget\n")
    run_b, wf, recs = await budget_run(c, rep, home, scripts, "B", "budget_demo_unlimited.py", BIG_BUDGET)
    labels = [r.get("label") for r in recs if "sig" in r]
    # The team ledger also bills the Leader's own model calls, and it gates the Leader too.
    # Measure, from B: the Leader's spend before the run started (first workflow.updated) and
    # one worker call. D's team ceiling = both, so it lands after the first worker.
    first = next((wf_of(f) for f in c.frames if etype(f) == "workflow.updated" and wf_of(f).get("id") == run_b), {})
    leader_pre = (first.get("budget") or {}).get("spent") or 0
    worker = next((r.get("tokens") for r in recs if r.get("label") == "outline"), 0) or 0
    team_budget_d = leader_pre + worker if leader_pre and worker else SMALL_TEAM_BUDGET
    rep.log(f"\nMeasured in B: Leader spend before launch = {leader_pre} tokens, one worker call (`outline`) = {worker} tokens "
            f"→ D uses swarmflow_budget = {team_budget_d}.")
    rep.check("B.completed", "control: without a ceiling the run completes and all three agents are journaled",
              wf.get("status") == "completed" and all(x in labels for x in ("outline", "draft", "headline")), labels)

    rep.log("\n## Scenario C: per-run ceiling (META workflow_token_limit=30000)\n")
    run_id, wf, recs = await budget_run(c, rep, home, scripts, "C", "budget_demo.py", BIG_BUDGET)
    w = wf.get("workflow_budget") or {}
    rep.check("C.failed_workflow_scope", "run ends `failed` with budget_exhausted_scope=workflow",
              wf.get("status") == "failed" and wf.get("budget_exhausted_scope") == "workflow",
              f"{wf.get('status')} / {wf.get('budget_exhausted_scope')}")
    rep.check("C.ledger", "per-run ledger is exhausted (spent >= 30000) while the team ledger is not",
              w.get("exhausted") and w.get("total") == 30000 and (w.get("spent") or 0) >= 30000
              and not (wf.get("budget") or {}).get("exhausted"), w)
    rep.check("C.headline_absent", "`headline` never ran (no journal record)",
              recs and not any(r.get("label") == "headline" for r in recs), [r.get("label") for r in recs])
    g = guidance_text(home, run_id)
    rep.block("Diagnostic delivered to the Leader (from the trace store)", g or "(not found)")
    rep.check("C.guidance", "the Leader receives workflow-layer guidance: redesign the script, relaunch with resume_id",
              g and "Triggered layer: workflow (per-run)" in g and "redesign the workflow" in g and "resume_id" in g, bool(g))

    rep.log(f"\n## Scenario D: team ceiling (swarmflow_budget={team_budget_d}, no per-run limit)\n")
    run_id, wf, recs = await budget_run(c, rep, home, scripts, "D", "budget_demo_unlimited.py", team_budget_d)
    b = wf.get("budget") or {}
    rep.check("D.stopped_session_scope", "run ends `stopped` with budget_exhausted_scope=session",
              wf.get("status") == "stopped" and wf.get("budget_exhausted_scope") == "session",
              f"{wf.get('status')} / {wf.get('budget_exhausted_scope')}")
    rep.check("D.ledger", f"team (session) ledger is exhausted (total={team_budget_d})",
              b.get("exhausted") and b.get("total") == team_budget_d and (b.get("spent") or 0) >= team_budget_d, b)
    rep.check("D.headline_absent", "`headline` never ran (no journal record)",
              not any(r.get("label") == "headline" for r in recs), [r.get("label") for r in recs])
    g = guidance_text(home, run_id)
    rep.block("Diagnostic delivered to the Leader (from the trace store)", g or "(not found)")
    rep.check("D.guidance", "the Leader receives session-layer guidance: raise the ceiling / new session",
              g and "Triggered layer: session (shared)" in g and "raise the ceiling" in g, bool(g))


def scan_for_key(key, roots):
    kb = key.encode()
    hits = []
    for root in roots:
        for dp, _, fns in os.walk(root):
            for fn in fns:
                p = os.path.join(dp, fn)
                try:
                    with open(p, "rb") as fh:
                        if kb in fh.read():
                            hits.append(p)
                except OSError:
                    pass
    return hits


async def one_repeat(n, out_root, provider, model, api_key):
    out = out_root / f"repeat-{n}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rep = Repeat(n, out)
    tmp = Path(tempfile.mkdtemp(prefix=f"swarmflow-proof-{n}-"))
    home, scripts = tmp / "home", tmp / "scripts"
    scripts.mkdir()
    rep.log(f"JiuwenSwarm home: fresh `{home}` · provider `{provider}` · model `{model}` · workswarm {metadata.version('workswarm')} · "
            f"openjiuwen {metadata.version('openjiuwen')}\n")
    make_home(home, provider, model, VENV_BIN)
    srv = Server(home, VENV_BIN, tmp, tmp / "server.log")
    t0 = time.time()
    try:
        srv.start(api_key)
        async with Client(out / "events.jsonl") as c:
            for name, coro in (("A", lambda: scenario_a(c, rep, home, scripts, tmp)),
                               ("B-D", lambda: scenario_bcd(c, rep, home, scripts))):
                try:
                    await coro()
                except Exception as e:  # a crash is a failed check, never a silent pass
                    rep.check(f"{name}.ran", f"scenario {name} ran to the end", False, repr(e))
    except Exception as e:
        rep.check("setup", "server started and client connected", False, repr(e))
    finally:
        srv.stop()
    # keep the journals as evidence, then scan for the key
    for p in glob.glob(str(home / ".jiuwenswarm" / ".agent_teams" / "**" / "wal" / "*.wal"), recursive=True):
        shutil.copy(p, out / Path(p).name)
    hits = scan_for_key(api_key, [home, out, tmp])
    rep.check("secrets", "the API key appears in no file under the JiuwenSwarm home, temp dir or proof output",
              not hits, hits[:5])
    elapsed = round(time.time() - t0)
    rep.log(f"\n**Repeat {n}: {'PASS' if rep.passed else 'FAIL'}** ({sum(c['pass'] for c in rep.checks)}/{len(rep.checks)} checks, {elapsed}s)")
    (out / "transcript.md").write_text("\n".join(rep.md) + "\n")
    report = {"repeat": n, "passed": rep.passed, "provider": provider, "model": model, "elapsed_s": elapsed,
              "versions": {"workswarm": metadata.version("workswarm"), "openjiuwen": metadata.version("openjiuwen")},
              "checks": rep.checks}
    (out / "report.json").write_text(json.dumps(report, indent=2))
    if os.environ.get("KEEP_HOME") != "1":
        shutil.rmtree(tmp, ignore_errors=True)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()
    provider = "OpenAI"  # the proof is recorded against OpenAI (MODEL_PROVIDER=OpenAI in the product)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY is not set")
    model = os.environ.get("MODEL_NAME") or "gpt-5.5"
    out_root = Path(os.environ.get("PROOF_OUT", ROOT / "proof-out")).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    reports = [asyncio.run(one_repeat(n, out_root, provider, model, api_key)) for n in range(1, a.repeat + 1)]
    summary = {"repeats": a.repeat, "passed": sum(r["passed"] for r in reports),
               "all_passed": all(r["passed"] for r in reports), "provider": provider, "model": model,
               "per_repeat": [{"repeat": r["repeat"], "passed": r["passed"],
                               "failed_checks": [c["id"] for c in r["checks"] if not c["pass"]]} for r in reports]}
    (out_root / "report.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary["all_passed"] else 1)


if __name__ == "__main__":
    main()
