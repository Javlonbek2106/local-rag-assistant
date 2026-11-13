import uuid

import pytest

from rag import storage


def test_a_version_gets_its_own_folder_built_from_ids_only():
    doc, version = uuid.uuid4(), uuid.uuid4()
    assert storage.relative_path(doc, version, "Hisobot 2026.PDF") == f"documents/{doc}/versions/{version}/original.pdf"
    # the file's own name never reaches the disk, whatever it contains
    assert "Hisobot" not in storage.relative_path(doc, version, "Hisobot 2026.PDF")


@pytest.mark.parametrize("name", ["a.pd f", "a." + "x" * 11, "a.päf"])
def test_a_strange_extension_is_refused(name):
    with pytest.raises(ValueError):
        storage.relative_path(uuid.uuid4(), uuid.uuid4(), name)


@pytest.mark.parametrize("name", ["a.p/df", "a.p\\df", "a.../x", "../../etc/passwd", "C:\\x\\..\\y"])
def test_separators_in_a_file_name_never_reach_the_path(name):
    doc, version = uuid.uuid4(), uuid.uuid4()
    try:
        relative = storage.relative_path(doc, version, name)
    except ValueError:  # on some platforms the odd "extension" is refused outright, which is just as safe
        return
    assert relative.startswith(f"documents/{doc}/versions/{version}/original")
    assert ".." not in relative.split("/") and "\\" not in relative and relative.count("/") == 4


def test_save_and_resolve_round_trip(storage_dir, tmp_path):
    temp = tmp_path / "upload.tmp"
    temp.write_bytes(b"matn")
    relative = storage.relative_path(uuid.uuid4(), uuid.uuid4(), "a.txt")
    storage.save(relative, temp)
    assert storage.resolve(relative).read_bytes() == b"matn" and not temp.exists()


@pytest.mark.parametrize("bad", ["../outside.txt", "documents/../../outside.txt", "/etc/passwd"])
def test_a_path_that_would_leave_the_storage_folder_is_refused(storage_dir, bad):
    storage_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        storage.resolve(bad)


def test_two_versions_never_share_a_file(storage_dir, tmp_path):
    doc = uuid.uuid4()
    paths = []
    for content in (b"bir", b"ikki"):
        temp = tmp_path / f"u{content.decode()}"
        temp.write_bytes(content)
        relative = storage.relative_path(doc, uuid.uuid4(), "a.txt")
        storage.save(relative, temp)
        paths.append(relative)
    assert paths[0] != paths[1]
    assert [storage.resolve(p).read_bytes() for p in paths] == [b"bir", b"ikki"]


def test_remove_deletes_every_version_and_the_pre_versioning_folder(storage_dir):
    doc = uuid.uuid4()
    for relative in (f"documents/{doc}/versions/{uuid.uuid4()}/original.txt", f"{doc}/a.txt"):
        target = storage.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    keep = storage.resolve(f"documents/{uuid.uuid4()}/versions/{uuid.uuid4()}/original.txt")
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"other document")
    storage.remove(doc)
    assert not (storage_dir / "documents" / str(doc)).exists() and not (storage_dir / str(doc)).exists()
    assert keep.exists()  # another document's files are not touched
