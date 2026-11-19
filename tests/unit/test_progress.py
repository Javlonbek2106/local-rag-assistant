"""Progress-based job timeout (roadmap T0.3): what the pipeline reports, how the guard judges it, how the API shows it.

The database side (persistence, leases, the worker end to end) is in tests/db/test_job_progress.py.
"""
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rag import config, ingest, llm
from rag.worker import ClaimLost, JobGuard, JobProgress
from tests.conftest import FakeQdrant

JOB = {"id": "v1", "name": "a.pdf"}


class Repo:
    """Just the calls a running job makes, recording them."""

    def __init__(self, accept=True):
        self.accept = accept
        self.stages, self.positions, self.failures, self.beats = [], [], [], 0

    def set_stage(self, version_id, worker, status):
        self.stages.append(status)
        return self.accept

    def set_progress(self, version_id, worker, unit, done, total=None):
        self.positions.append((unit, done, total))
        return self.accept

    def heartbeat(self, version_id, worker):
        self.beats += 1
        return True

    def fail(self, version_id, worker, error, retry=True):
        self.failures.append((error, retry))
        return "failed"


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(config, "JOB_HEARTBEAT_S", 0.02)
    monkeypatch.setattr(config, "JOB_STALL_S", 0.4)
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", 60)


# ---------- JobProgress: what a running job says about itself ----------

def test_a_reported_position_is_written_to_the_database_and_counts_as_moving():
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    before = progress.moved_at
    time.sleep(0.01)
    progress.progress("pages", 3, 381)
    assert repo.positions == [("pages", 3, 381)] and progress.moved_at > before


def test_the_same_position_again_is_neither_written_nor_counted_as_moving():
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    progress.progress("pages", 3, 381)
    moved = progress.moved_at
    time.sleep(0.01)
    progress.progress("pages", 3, 381)  # a loop that spins in place must not keep a job alive
    assert repo.positions == [("pages", 3, 381)] and progress.moved_at == moved


def test_moving_to_a_new_stage_counts_as_moving_and_forgets_the_old_position():
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    progress.progress("pages", 3, 381)
    moved = progress.moved_at
    time.sleep(0.01)
    progress("EMBEDDING")
    assert repo.stages == ["EMBEDDING"] and progress.moved_at > moved and progress.position is None
    progress.progress("chunks", 0, 40)  # the same numbers in the new stage are news again
    assert repo.positions[-1] == ("chunks", 0, 40)


def test_a_database_that_refuses_the_position_means_the_claim_is_gone_and_the_job_must_stop():
    progress = JobProgress(Repo(accept=False), JOB, "w1")
    with pytest.raises(ClaimLost):
        progress.progress("pages", 1, 10)
    with pytest.raises(ClaimLost):
        progress("EMBEDDING")


def test_a_database_error_while_recording_the_position_does_not_fail_the_job():
    class Flaky(Repo):
        def set_progress(self, *args, **kwargs):
            raise ConnectionError("connection reset")

    progress = JobProgress(Flaky(), JOB, "w1")
    before = progress.moved_at
    time.sleep(0.01)
    progress.progress("pages", 1, 10)  # only a report: the work goes on, and it still moved
    assert progress.moved_at > before


# ---------- JobGuard: stalled is not the same as slow ----------

def run_job(repo, progress, seconds, tick=None, report=None):
    """A job that lasts `seconds` and, every `tick`, calls report(progress, i). Returns (guard, whether it fired)."""
    fired = threading.Event()
    with JobGuard(repo, JOB, "w1", fired.set, progress) as guard:
        end = time.monotonic() + seconds
        i = 0
        while time.monotonic() < end and not fired.is_set():
            if report:
                report(progress, i)
            i += 1
            time.sleep(tick or 0.02)
    return guard, fired.is_set()


def test_a_job_that_keeps_moving_runs_far_longer_than_the_stall_limit(quick):
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    guard, fired = run_job(repo, progress, 1.4, tick=0.03, report=lambda p, i: p.progress("pages", i, 1000))
    assert not fired and not guard.timed_out and repo.failures == []  # 1.4 s of work against a 0.4 s stall limit
    assert repo.beats >= 5  # and it kept heartbeating


def test_a_job_that_stops_moving_is_failed_without_retry_and_the_error_says_where(quick):
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    progress.progress("pages", 5, 20)
    guard, fired = run_job(repo, progress, 3)  # then nothing: a parser that never returns
    assert fired and guard.timed_out
    [(error, retry)] = repo.failures
    assert retry is False  # it would only stall again
    assert error.startswith("Vaqt tugadi") and "taraqqiyot" in error and "sahifa 5/20" in error
    assert repo.beats >= 3  # it was heartbeating all along: alive, but not moving


def test_a_job_that_spins_in_place_is_a_stalled_job(quick):
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    guard, fired = run_job(repo, progress, 3, tick=0.02, report=lambda p, i: p.progress("pages", 5, 20))
    assert fired and "sahifa 5/20" in repo.failures[0][0]


def test_every_stage_has_its_own_stall_clock(quick):
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")

    start, entered = time.monotonic(), []

    def stages(p, i):  # about 0.25 s in each of three stages: more than the 0.4 s limit in all, less in each
        for name, at in (("EMBEDDING", 0.25), ("INDEXING", 0.5)):
            if name not in entered and time.monotonic() - start >= at:
                entered.append(name)
                p(name)

    guard, fired = run_job(repo, progress, 0.75, tick=0.02, report=stages)
    assert not fired and repo.failures == [] and entered == ["EMBEDDING", "INDEXING"]


def test_a_job_that_never_reports_anything_is_judged_from_its_start(quick):
    repo = Repo()
    guard = run_job(repo, None, 3)[0]  # a guard without a progress object (nothing reports for this job)
    assert guard.timed_out and "taraqqiyot" in repo.failures[0][0]


def test_the_absolute_ceiling_still_stops_a_job_that_moves_forever(quick, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_S", 0.3)
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    guard, fired = run_job(repo, progress, 3, tick=0.03, report=lambda p, i: p.progress("chunks", i, None))
    assert fired and guard.timed_out
    assert repo.failures[0][0].startswith("Vaqt tugadi") and "jami chegara" in repo.failures[0][0]


class Clock:
    """The worker's clock, with a way to make the whole process 'sleep': time jumps, nothing ran meanwhile."""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return time.monotonic() + self.offset

    sleep = staticmethod(time.sleep)


def test_a_sleeping_computer_is_not_a_stalled_job(quick, monkeypatch):
    """The first real run of a 381-page document failed because the laptop slept for 15 minutes: the guard woke up and
    read the gap as 'no progress for 15 minutes'. Time in which the process could not run must not count."""
    from rag import worker

    clock = Clock()
    monkeypatch.setattr(worker, "time", clock)
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    progress.progress("pages", 217, 381)
    moved = progress.moved_at

    def sleep_once(p, i):
        if i == 3:
            clock.offset += 900  # the computer slept for 15 minutes (the guard's real wait was a few milliseconds)

    guard, fired = run_job(repo, progress, 0.3, tick=0.02, report=sleep_once)
    assert not fired and not guard.timed_out and repo.failures == []
    assert progress.moved_at - moved >= 800  # the stall clock was moved forward by (about) the time that was slept


def test_a_job_that_stalls_after_the_computer_slept_is_still_failed(quick, monkeypatch):
    from rag import worker

    clock = Clock()
    monkeypatch.setattr(worker, "time", clock)
    repo = Repo()
    progress = JobProgress(repo, JOB, "w1")
    progress.progress("pages", 5, 20)
    clock.offset += 900  # slept right away ...
    guard, fired = run_job(repo, progress, 3)  # ... and then really stalls
    assert fired and "sahifa 5/20" in repo.failures[0][0]


def test_the_defaults_let_a_big_document_run_for_hours_while_it_moves():
    assert config.JOB_STALL_S == 600  # ten minutes without any progress is stuck
    assert config.JOB_TIMEOUT_S >= 6 * 3600  # the old 30-minute limit no longer decides the fate of a big document


# ---------- the pipeline reports what it does ----------

class Chunker:
    def __init__(self, n):
        self.n = n

    def chunk(self, document):
        for i in range(self.n):
            yield SimpleNamespace(i=i, meta=SimpleNamespace(
                doc_items=[SimpleNamespace(prov=[SimpleNamespace(page_no=1 + i // 10)])], headings=[f"h{i // 10}"]))

    def contextualize(self, chunk):
        return f"chunk {chunk.i}"


def test_cutting_reports_the_count_so_far_and_returns_exactly_what_it_always_did():
    seen = []
    items = ingest.document_items(None, Chunker(60), lambda *args: seen.append(args))
    assert seen == [("chunks", 0, None), ("chunks", 25, None), ("chunks", 50, None)]
    assert items == ingest.document_items(None, Chunker(60))  # reporting changes nothing about the result
    assert items[0] == ("chunk 0", [1], ["h0"]) and len(items) == 60 and items[59][1] == [6]


def test_embedding_reports_after_each_batch(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 4)
    calls = []
    monkeypatch.setattr(llm, "embed", lambda batch: calls.append(len(batch)) or [[0.1]] * len(batch))
    seen = []
    vectors = ingest._embed([f"t{i}" for i in range(10)], lambda *args: seen.append(args))
    assert len(vectors) == 10 and calls == [4, 4, 2]
    assert seen == [("chunks", 0, 10), ("chunks", 4, 10), ("chunks", 8, 10), ("chunks", 10, 10)]


def test_writing_to_the_index_reports_after_each_batch(monkeypatch):
    monkeypatch.setattr(ingest, "UPLOAD_BATCH", 3)
    qdrant, seen = FakeQdrant(), []
    points = ingest._points([(f"t{i}", [1], []) for i in range(7)], [[0.1]] * 7, "d", "a.txt", "h", "d", "v")
    ingest._upload(qdrant, points, lambda *args: seen.append(args))
    assert len(qdrant.points) == 7
    assert seen == [("chunks", 0, 7), ("chunks", 3, 7), ("chunks", 6, 7), ("chunks", 7, 7)]


class Stage:
    """The `stage` callback of the worker: callable, with a progress() method."""

    def __init__(self):
        self.events = []

    def __call__(self, status):
        self.events.append(status)

    def progress(self, unit, done, total=None):
        self.events.append((unit, done, total))


def test_a_version_reports_stages_and_positions_in_order(monkeypatch):
    monkeypatch.setattr(ingest, "extract_items",
                        lambda converter, chunker, path, progress=None: [(t, [1], []) for t in ("a", "b", "c")])
    monkeypatch.setattr(llm, "embed", lambda batch: [[0.1]] * len(batch))
    stage = Stage()
    count = ingest.index_version(FakeQdrant(), None, None, None, document_id="d", version_id="v", file_name="a.txt",
                                 file_hash="h", stage=stage)
    assert count == 3 and stage.events == [
        "EMBEDDING", ("chunks", 0, 3), ("chunks", 3, 3), "INDEXING", ("chunks", 0, 3), ("chunks", 3, 3)]


def test_a_stage_callback_without_progress_is_still_accepted(monkeypatch):
    monkeypatch.setattr(ingest, "extract_items", lambda converter, chunker, path: [("a", [1], [])])
    monkeypatch.setattr(llm, "embed", lambda batch: [[0.1]] * len(batch))
    seen = []
    ingest.index_version(FakeQdrant(), None, None, None, document_id="d", version_id="v", file_name="a.txt",
                         file_hash="h", stage=seen.append)
    assert seen == ["EMBEDDING", "INDEXING"]


class Converter:
    """Stands in for Docling's DocumentConverter: three pages, then the assembly, as the real one reports them."""

    def __init__(self):
        self.options = None

    def convert(self, source, **options):
        self.options = options
        callback = options.get("progress_callback")
        if callback:
            # Docling's own event classes; only needed (and only installed) where the converter is told to report
            from docling.datamodel.progress import ConversionPhase, PageCompletedProgress, PhaseStartedProgress

            for n in (1, 2, 3):
                callback(PageCompletedProgress(document_index=1, document_name="a.pdf", page_no=n, success=True,
                                               completed_pages=n, total_pages=3))
            callback(PhaseStartedProgress(document_index=1, document_name="a.pdf", phase=ConversionPhase.BUILD))
            callback(PhaseStartedProgress(document_index=1, document_name="a.pdf", phase=ConversionPhase.ASSEMBLE))
        return SimpleNamespace(document="the document")


@pytest.fixture
def docling():
    """The two tests below run the real ingest.convert with a listener, which reads Docling's event classes. CI installs
    only the light requirements, so exactly these two are skipped there (and run wherever Docling is installed)."""
    return pytest.importorskip("docling.datamodel.progress")


def test_pages_parsed_are_reported_as_docling_finishes_them(docling):
    converter, seen = Converter(), []
    assert ingest.convert(converter, Path("a.pdf"), lambda *args: seen.append(args)) == "the document"
    assert seen == [("pages", 1, 3), ("pages", 2, 3), ("pages", 3, 3), ("assembling", 0, None)]


def test_without_a_listener_docling_is_called_exactly_as_before():
    converter = Converter()
    ingest.convert(converter, Path("a.pdf"))
    assert converter.options == {}


def test_plain_text_goes_through_the_same_reporting(tmp_path, docling):
    path = tmp_path / "a.txt"
    path.write_text("matn", encoding="utf-8")
    converter, seen = Converter(), []
    ingest.convert(converter, path, lambda *args: seen.append(args))
    assert seen[0] == ("pages", 1, 3) and "progress_callback" in converter.options


# ---------- what the API shows ----------

def processing(repo, name="a.pdf"):
    version = repo.create_version(name, 10, "h")
    repo.enqueue(version["id"])
    repo.set_status(name, 1, "PROCESSING")
    return version


def test_the_document_list_shows_the_stage_and_the_position(client, repo):
    processing(repo)
    repo.set_progress("a.pdf", 1, "pages", 120, 381)
    row = client.get("/api/documents").json()["documents"][0]
    assert row["state"] == "processing" and row["stage"] == "PROCESSING"
    assert row["progress"] == {"unit": "pages", "done": 120, "total": 381}


def test_the_stage_is_shown_even_before_a_position_is_known(client, repo):
    processing(repo)
    row = client.get("/api/documents").json()["documents"][0]
    assert row["stage"] == "PROCESSING" and row["progress"] is None
    repo.set_status("a.pdf", 1, "EMBEDDING")
    repo.set_progress("a.pdf", 1, "chunks", 64, 4104)
    row = client.get("/api/documents").json()["documents"][0]
    assert row["stage"] == "EMBEDDING" and row["progress"] == {"unit": "chunks", "done": 64, "total": 4104}


def test_a_document_that_is_not_being_indexed_shows_no_stage_or_position(client, repo):
    processing(repo)
    repo.set_progress("a.pdf", 1, "pages", 5, 10)
    repo.set_status("a.pdf", 1, "FAILED", error="boom")
    row = client.get("/api/documents").json()["documents"][0]
    assert row["stage"] is None and row["progress"] is None


def test_a_total_that_is_not_known_yet_is_null(client, repo):
    processing(repo)
    repo.set_progress("a.pdf", 1, "chunks", 50)
    assert client.get("/api/documents").json()["documents"][0]["progress"] == {
        "unit": "chunks", "done": 50, "total": None}


def test_the_version_list_shows_the_position_of_the_version_being_indexed(client, repo):
    processing(repo)
    repo.set_progress("a.pdf", 1, "pages", 7, 9)
    [version] = client.get("/api/documents/a.pdf/versions").json()["versions"]
    assert version["phase"] == "processing" and version["progress"] == {"unit": "pages", "done": 7, "total": 9}
    repo.set_status("a.pdf", 1, "FAILED", error="x")
    assert client.get("/api/documents/a.pdf/versions").json()["versions"][0]["progress"] is None


def test_the_status_endpoint_shows_the_position_and_how_long_ago_it_moved(client, repo, monkeypatch):
    from rag import health

    monkeypatch.setattr(health, "check_ollama", lambda: "ok")
    processing(repo)
    repo.set_progress("a.pdf", 1, "pages", 120, 381)
    [running] = client.get("/api/status").json()["queue"]["in_flight"]
    assert running["stage"] == "PROCESSING"
    assert running["progress"] == {"unit": "pages", "done": 120, "total": 381, "age_seconds": 3}
    assert not {k for k in running if k.startswith("progress_")}  # the raw columns are not leaked


def test_the_status_endpoint_has_a_null_position_before_one_is_reported(client, repo, monkeypatch):
    from rag import health

    monkeypatch.setattr(health, "check_ollama", lambda: "ok")
    processing(repo)
    [running] = client.get("/api/status").json()["queue"]["in_flight"]
    assert running["progress"] is None
