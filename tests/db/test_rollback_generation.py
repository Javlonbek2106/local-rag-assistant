"""A rollback is a durable user decision: versions that were queued or being indexed before it never replace it.

Real PostgreSQL. A version "finishes" the way a worker would finish it: stage VERIFYING, record_verified, activate.
"""
import hashlib
import threading
import uuid

import pytest

from rag import db, ingest, versioning
from rag.worker import Worker
from tests.conftest import FakeQdrant

pytestmark = pytest.mark.db


def upload(name="a.pdf", content=None):
    """A new queued version of `name` (the content decides the hash)."""
    created = db.create_version(name, 10, hashlib.sha256((content or uuid.uuid4().hex).encode()).hexdigest())
    assert created["created"] and db.enqueue(created["id"])
    return created


def verify(job, worker="w1", chunks=3):
    """The end of a worker's job: the index was checked, now it is recorded."""
    assert db.set_stage(job["id"], worker, "VERIFYING") and db.record_verified(job["id"], worker, chunks)


def finish(job, worker="w1", chunks=3):
    verify(job, worker, chunks)
    return db.activate(job["id"], worker, chunks)


def publish(name="a.pdf", content=None, chunks=3):
    """Upload, claim and activate: a version that is live."""
    created = upload(name, content)
    job = db.claim_next("w1")
    assert job["id"] == created["id"]
    assert finish(job, chunks=chunks) == "activated"
    return job


def two_live_and_a_third_pending(pending_content="three"):
    """Version 2 is active, version 1 was replaced (so it can be rolled back to), version 3 is queued."""
    first = publish(content="one")
    second = publish(content="two")
    third = upload(content=pending_content)
    return first, second, third


def doc(name="a.pdf"):
    return db.get_document(name)


def status(number, name="a.pdf"):
    return db.get_version(name, number)["status"]


# ---------- the generation itself ----------

def test_only_a_rollback_moves_the_generation_not_an_activation():
    first = publish(content="one")
    publish(content="two")
    assert doc()["activation_generation"] == 0  # activations never move it
    assert db.switch_active(first["id"]) == 1
    publish(content="three")
    assert doc()["activation_generation"] == 1
    assert status(1) == "SUPERSEDED"  # the new version replaced it again
    assert db.switch_active(first["id"]) == 2  # a second rollback: the count keeps growing


def test_each_rollback_adds_one_and_an_upload_remembers_the_generation_it_was_made_in():
    first = publish(content="one")
    publish(content="two")
    before = upload(content="uploaded before the rollback")
    assert db.get_version("a.pdf", before["version_number"])["uploaded_generation"] == 0
    db.switch_active(first["id"])
    after = upload(content="uploaded after the rollback")
    assert db.get_version("a.pdf", after["version_number"])["uploaded_generation"] == 1
    assert doc()["activation_generation"] == 1


# ---------- the seven scenarios ----------

def test_rollback_while_a_newer_version_is_queued_does_not_let_it_take_over():
    first, second, third = two_live_and_a_third_pending()
    db.switch_active(first["id"])
    job = db.claim_next("w1")  # the worker only now picks up the version that was queued before the rollback
    assert job["id"] == third["id"]
    assert finish(job, chunks=4) == "superseded"
    assert db.active_version_ids() == [str(first["id"])]
    found = db.get_version("a.pdf", 3)
    assert found["status"] == "SUPERSEDED" and found["chunk_count"] == 4  # kept, indexed, available for roll-forward
    assert (doc()["state"], doc()["version"], doc()["chunks"]) == ("done", 1, 3)


def test_rollback_while_a_newer_version_is_processing_does_not_let_it_take_over():
    first, second, third = two_live_and_a_third_pending()
    job = db.claim_next("w1")  # version 3 is being indexed when the user rolls back
    assert db.set_stage(job["id"], "w1", "EMBEDDING") and doc()["state"] == "processing"
    db.switch_active(first["id"])
    assert doc()["version"] == 1  # the rollback took effect at once, regardless of the running job
    assert finish(job, chunks=4) == "superseded"
    assert db.active_version_ids() == [str(first["id"])] and status(3) == "SUPERSEDED"
    assert status(1) == "READY" and status(2) == "SUPERSEDED"


def test_a_worker_finishing_after_a_rollback_keeps_the_rolled_back_version(tmp_path, monkeypatch, storage_dir):
    first = publish(content="one")
    publish(content="two")
    temp = tmp_path / "three.tmp"
    temp.write_bytes(b"third version")
    queued = ingest.queue_file("a.pdf", temp, 13)  # a real file for the real worker
    assert queued["version_number"] == 3

    def rolled_back_while_indexing(*args, **kwargs):
        db.switch_active(first["id"])  # the user's decision lands in the middle of the job
        return 5

    monkeypatch.setattr(ingest, "index_version", rolled_back_while_indexing)
    monkeypatch.setattr(versioning, "verify", lambda *args: None)
    worker = Worker(worker_id="w1")
    worker.client = worker.converter = worker.chunker = object()
    assert worker.run_once() is True
    assert db.active_version_ids() == [str(first["id"])]
    assert status(3) == "SUPERSEDED" and db.get_version("a.pdf", 3)["chunk_count"] == 5
    assert (doc()["state"], doc()["version"]) == ("done", 1)


def test_a_stale_worker_that_tries_to_activate_after_the_rollback_changes_nothing_but_its_own_status():
    first, second, third = two_live_and_a_third_pending()
    job = db.claim_next("w1")
    verify(job, chunks=4)  # fully verified, about to activate
    assert db.switch_active(first["id"]) == 1  # ... the rollback commits first
    assert db.activate(job["id"], "w1", 4) == "superseded"
    assert db.activate(job["id"], "w1", 4) is None  # the job is released: asking again does nothing
    assert db.activate(job["id"], "someone-else", 4) is None
    assert db.active_version_ids() == [str(first["id"])] and doc()["activation_generation"] == 1


def test_a_version_uploaded_after_the_rollback_can_still_be_processed_and_activated():
    first, second, third = two_live_and_a_third_pending()
    db.switch_active(first["id"])
    fourth = upload(content="four")  # made after the decision
    assert db.get_version("a.pdf", 4)["uploaded_generation"] == 1
    stale = db.claim_next("w1")  # versions of one document run in order: the stale one goes first
    assert stale["id"] == third["id"] and finish(stale) == "superseded"
    fresh = db.claim_next("w1")
    assert fresh["id"] == fourth["id"]
    assert finish(fresh, chunks=6) == "activated"
    assert db.active_version_ids() == [str(fourth["id"])]
    assert (status(1), status(3), status(4)) == ("SUPERSEDED", "SUPERSEDED", "READY")
    assert doc()["activation_generation"] == 1 and doc()["chunks"] == 6  # activating does not move the generation


def test_a_version_uploaded_while_a_stale_one_is_still_being_indexed_activates_when_its_turn_comes():
    first, second, third = two_live_and_a_third_pending()
    stale = db.claim_next("w1")  # version 3 is in flight
    db.switch_active(first["id"])
    fourth = upload(content="four")
    assert db.claim_next("w2") is None  # waits for version 3: one version in flight per document
    verify(stale)
    assert db.activate(stale["id"], "w1", 3) == "superseded"
    fresh = db.claim_next("w2")
    assert fresh["id"] == fourth["id"] and finish(fresh, worker="w2") == "activated"


@pytest.mark.parametrize("round_number", range(25))
def test_a_rollback_and_an_activation_racing_each_other_always_end_with_the_rolled_back_version(round_number):
    name = f"race{round_number}.pdf"
    first = publish(name, content="one")
    publish(name, content="two")
    third = upload(name, content="three")
    job = db.claim_next("w1")
    assert job["id"] == third["id"]
    verify(job)  # the candidate is ready to activate
    start, errors, results = threading.Barrier(2), [], {}

    def run(label, action):
        try:
            start.wait()
            results[label] = action()
        except Exception as error:  # noqa: BLE001 - a deadlock or a crash must reach the assertion
            errors.append(error)

    threads = [threading.Thread(target=run, args=("activate", lambda: db.activate(job["id"], "w1", 3))),
               threading.Thread(target=run, args=("rollback", lambda: db.switch_active(first["id"])))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == [] and not any(t.is_alive() for t in threads)
    # whichever came first, the rollback is the later or the stronger decision: the user's version is active
    assert db.active_version_ids() == [str(first["id"])]
    assert (status(1, name), status(2, name), status(3, name)) == ("READY", "SUPERSEDED", "SUPERSEDED")
    assert db.get_document(name)["activation_generation"] == 1
    assert results["activate"] in ("activated", "superseded")


def test_a_rollback_after_an_activation_overrides_it_as_the_later_decision():
    first, second, third = two_live_and_a_third_pending()
    job = db.claim_next("w1")
    assert finish(job) == "activated"  # the new version went live first ...
    assert db.active_version_ids() == [str(third["id"])]
    db.switch_active(first["id"])  # ... and the user then chose version 1
    assert db.active_version_ids() == [str(first["id"])]
    assert (status(1), status(2), status(3)) == ("READY", "SUPERSEDED", "SUPERSEDED")


# ---------- a refused rollback changes nothing ----------

def test_a_refused_switch_leaves_the_active_version_the_generation_and_the_candidates_alone():
    first, second, third = two_live_and_a_third_pending()
    with pytest.raises(db.VersionConflict):
        db.switch_active(third["id"])  # queued: never active, so not a valid rollback target
    assert db.active_version_ids() == [str(second["id"])] and doc()["activation_generation"] == 0
    assert (status(1), status(2), status(3)) == ("SUPERSEDED", "READY", "QUEUED")
    job = db.claim_next("w1")  # the pending version was not made stale by the refused attempt
    assert finish(job) == "activated" and db.active_version_ids() == [str(third["id"])]


def test_a_rollback_refused_because_the_old_vectors_are_gone_leaves_everything_as_it_was():
    first, second, third = two_live_and_a_third_pending()
    qdrant = FakeQdrant()  # version 1's chunks are not in the index any more
    with pytest.raises(versioning.RollbackError, match="to'liq emas"):
        versioning.rollback(db, qdrant, "a.pdf", 1)
    assert db.active_version_ids() == [str(second["id"])] and doc()["activation_generation"] == 0
    job = db.claim_next("w1")
    assert finish(job) == "activated"  # the in-flight version still activates normally


def test_a_successful_rollback_through_the_service_records_the_decision():
    first, second, third = two_live_and_a_third_pending()
    qdrant = FakeQdrant()
    found = db.get_version("a.pdf", 1)
    for i in range(found["chunk_count"]):  # version 1's chunks are all there
        qdrant.points[f"v1-{i}"] = ([0.1], {"document_id": str(found["document_id"]), "version_id": str(found["id"]),
                                            "file_hash": found["file_hash"], "chunks_total": found["chunk_count"]})
    versioning.rollback(db, qdrant, "a.pdf", 1)
    assert doc()["activation_generation"] == 1 and doc()["version"] == 1
    job = db.claim_next("w1")
    assert finish(job) == "superseded"  # the candidate from before the rollback does not take over
    assert db.active_version_ids() == [str(first["id"])]


# ---------- audit: cases the first round of tests did not cover ----------

def age_lease(version_id, minutes=10):
    with db.pool().connection() as conn:
        conn.execute("UPDATE document_versions SET heartbeat_at = now() - make_interval(mins => %s) WHERE id = %s",
                     (minutes, version_id))


def test_uploading_the_same_file_again_after_a_rollback_is_a_new_version_that_can_activate():
    first, second, third = two_live_and_a_third_pending(pending_content="THREE")
    db.switch_active(first["id"])
    again = db.create_version("a.pdf", 10, hashlib.sha256(b"THREE").hexdigest())
    assert again["created"] and again["version_number"] == 4  # not swallowed by the stale version 3
    assert db.get_version("a.pdf", 4)["uploaded_generation"] == 1
    assert db.enqueue(again["id"])
    assert finish(db.claim_next("w1")) == "superseded"  # version 3, uploaded before the rollback
    assert finish(db.claim_next("w1")) == "activated"  # the file the user uploaded again after it
    assert db.active_version_ids() == [str(again["id"])]


def test_an_identical_upload_of_the_active_version_is_still_deduplicated_after_rollbacks():
    first = publish(content="one")
    second = publish(content="two")
    db.switch_active(first["id"])
    db.switch_active(second["id"])  # rolled back and forward again: version 2 is active, uploaded in generation 0
    assert doc()["activation_generation"] == 2
    same = db.create_version("a.pdf", 10, hashlib.sha256(b"two").hexdigest())
    assert same["created"] is False and same["version_number"] == 2


def test_a_worker_whose_lease_expired_cannot_finish_even_before_anyone_takes_over():
    publish(content="one")
    upload(content="two")
    job = db.claim_next("w1")
    verify(job, chunks=4)
    age_lease(job["id"])  # no heartbeat for 10 minutes (limit 90 s); nobody has taken the version back yet
    assert not db.heartbeat(job["id"], "w1")  # an expired lease is not renewed ...
    assert not db.set_stage(job["id"], "w1", "VERIFYING") and not db.record_verified(job["id"], "w1", 4)
    assert db.activate(job["id"], "w1", 4) is None  # ... and cannot be used
    assert db.get_version("a.pdf", 2)["status"] == "VERIFYING" and doc()["version"] == 1
    assert db.requeue_stale(90) == 1  # the next poll of any worker takes it back
    retry = db.claim_next("w2")
    assert retry["id"] == job["id"] and retry["attempts"] == 2
    assert finish(retry, worker="w2", chunks=4) == "activated"


def test_a_retry_keeps_the_uploaded_generation_and_cannot_get_around_the_fence():
    first, second, third = two_live_and_a_third_pending()
    job = db.claim_next("w1")
    assert db.fail(job["id"], "w1", "Qdrant to'xtadi") == "queued"  # a temporary failure: back in the queue
    db.switch_active(first["id"])  # the user rolls back while it waits
    retry = db.claim_next("w2")
    assert retry["id"] == third["id"] and retry["attempts"] == 2
    assert retry["uploaded_generation"] == 0  # the retry is the same upload: it keeps its generation
    assert finish(retry, worker="w2") == "superseded"
    assert db.active_version_ids() == [str(first["id"])]


def test_a_retry_after_a_dead_worker_is_just_as_stale_after_a_rollback():
    first, second, third = two_live_and_a_third_pending()
    dead = db.claim_next("w1")
    age_lease(dead["id"])
    db.switch_active(first["id"])
    assert db.requeue_stale(90) == 1
    assert finish(db.claim_next("w2"), worker="w2") == "superseded"
    assert db.active_version_ids() == [str(first["id"])]


def test_a_manual_roll_forward_is_a_decision_too_and_stops_a_stale_worker():
    first, second, third = two_live_and_a_third_pending()
    db.switch_active(first["id"])  # rollback (generation 1)
    assert finish(db.claim_next("w1")) == "superseded"  # version 3 finishes, kept
    fourth = upload(content="four")  # uploaded after the rollback, still in flight below
    in_flight = db.claim_next("w1")
    assert in_flight["id"] == fourth["id"]
    assert db.switch_active(third["id"]) == 2  # the user now picks the kept version 3: a newer decision
    assert db.active_version_ids() == [str(third["id"])]
    assert finish(in_flight) == "superseded"  # version 4 predates this decision, so it does not override it
    assert db.active_version_ids() == [str(third["id"])] and status(4) == "SUPERSEDED"
    fifth = upload(content="five")  # uploaded after the decision: free to activate
    assert finish(db.claim_next("w1")) == "activated" and db.active_version_ids() == [str(fifth["id"])]
    assert doc()["activation_generation"] == 2


@pytest.mark.parametrize("round_number", range(10))
def test_two_simultaneous_rollbacks_are_ordered_by_the_document_lock(round_number):
    name = f"double{round_number}.pdf"
    first = publish(name, "one")
    second = publish(name, "two")
    publish(name, "three")
    same_target = round_number % 2 == 0
    targets = [first["id"], first["id"]] if same_target else [first["id"], second["id"]]
    start, results, errors = threading.Barrier(2), [], []

    def roll(target):
        try:
            start.wait()
            results.append(("ok", db.switch_active(target)))
        except db.VersionConflict:
            results.append(("refused", None))
        except Exception as error:  # noqa: BLE001 - a deadlock must reach the assertion
            errors.append(error)

    threads = [threading.Thread(target=roll, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == [] and not any(t.is_alive() for t in threads)
    succeeded = sorted(r[1] for r in results if r[0] == "ok")
    assert succeeded == list(range(1, len(succeeded) + 1))  # generations are consecutive: one per decision
    assert db.get_document(name)["activation_generation"] == len(succeeded)
    assert sum(v["status"] == "READY" for v in db.list_versions(name)) == 1
    if same_target:
        assert len(succeeded) == 1  # the second one finds the version already active and is refused
    else:
        assert len(succeeded) == 2 and db.active_version_ids() in ([str(first["id"])], [str(second["id"])])


@pytest.mark.parametrize("seed", range(3))
def test_random_interleavings_of_uploads_activations_and_rollbacks_keep_every_invariant(seed):
    import random

    name = "mix.pdf"
    for content in ("one", "two", "three"):
        publish(name, content)
    rollbacks, errors, lock, counter = [], [], threading.Lock(), [0]

    def actor(index):
        rng = random.Random(seed * 100 + index)
        for _ in range(25):
            try:
                operation = rng.choice(["upload", "work", "work", "rollback"])
                if operation == "upload":
                    with lock:
                        counter[0] += 1
                        n = counter[0]
                    upload(name, content=f"{seed}-{n}")
                elif operation == "work":
                    job = db.claim_next(f"w{index}")
                    if job:
                        finish(job, worker=f"w{index}")
                else:
                    candidates = [v for v in db.list_versions(name) if v["status"] == "SUPERSEDED" and v["chunk_count"]]
                    if candidates:
                        target = rng.choice(candidates)
                        try:
                            generation = db.switch_active(target["id"])
                            with lock:
                                rollbacks.append((generation, target["id"]))
                        except db.VersionConflict:
                            pass
            except Exception as error:  # noqa: BLE001 - deadlocks and crashes must reach the assertion
                errors.append(repr(error))

    threads = [threading.Thread(target=actor, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    while (job := db.claim_next("drain")) is not None:  # let everything that is still waiting finish
        finish(job, worker="drain")
    versions = db.list_versions(name)
    found = doc(name)
    ready = [v for v in versions if v["status"] == "READY"]
    assert len(ready) == 1 and found["version"] == ready[0]["version_number"]  # exactly one, and it is the active one
    assert {v["status"] for v in versions} <= {"READY", "SUPERSEDED"}  # nothing left half done
    assert found["activation_generation"] == len(rollbacks)  # only successful rollbacks count
    if rollbacks:
        last_target = max(rollbacks)[1]
        # a version uploaded before the latest rollback is active only if the user picked it in that rollback
        assert ready[0]["id"] == last_target or ready[0]["uploaded_generation"] == found["activation_generation"]


def test_the_generation_stamped_on_an_upload_never_goes_backwards_while_rollbacks_happen():
    name = "stamp.pdf"
    publish(name, "one")
    publish(name, "two")
    stop, errors, rolled = threading.Event(), [], []

    def uploader(index):
        n = 0
        while not stop.is_set():
            n += 1
            try:
                created = db.create_version(name, 1, hashlib.sha256(f"{index}-{n}".encode()).hexdigest())
                db.enqueue(created["id"])
            except Exception as error:  # noqa: BLE001
                errors.append(repr(error))

    def roller():
        for _ in range(25):
            candidates = [v for v in db.list_versions(name) if v["status"] == "SUPERSEDED" and v["chunk_count"]]
            if candidates:
                try:
                    rolled.append(db.switch_active(candidates[0]["id"]))
                except db.VersionConflict:
                    pass
            job = db.claim_next("roller")
            if job:
                finish(job, worker="roller")

    uploaders = [threading.Thread(target=uploader, args=(i,)) for i in range(3)]
    rolling = threading.Thread(target=roller)
    for t in uploaders:
        t.start()
    rolling.start()
    rolling.join(timeout=120)
    stop.set()
    for t in uploaders:
        t.join(timeout=30)
    stamps = [v["uploaded_generation"] for v in reversed(db.list_versions(name))]  # in version order
    assert errors == [] and stamps == sorted(stamps)  # the stamp is read under the same lock that numbers the version
    assert max(stamps) <= doc(name)["activation_generation"] == len(rolled)
