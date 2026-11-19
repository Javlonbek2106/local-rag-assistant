"""Roadmap M4.1a, decision D-5: secrets live outside the project, in a directory nobody else can read, or not at all.

The Windows tests use the real Windows API on a real directory; the POSIX ones use real modes. Each runs where it can.
"""
import os
import subprocess

import pytest

from rag import config
from rag.auth import secretfiles
from rag.auth.secretfiles import SecretError, sddl_is_private

ME = "S-1-5-21-1111-2222-3333-1001"
OTHER = "S-1-5-21-1111-2222-3333-1002"
windows_only = pytest.mark.skipif(os.name != "nt", reason="uses the Windows security API")
posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX permissions")


# ---------- reading an SDDL text (pure, runs everywhere) ----------

def sddl(owner=ME, *aces, group="SY"):
    return f"O:{owner}G:{group}D:PAI" + "".join(f"({a})" for a in aces)


def test_an_acl_for_the_user_system_and_administrators_is_private():
    text = sddl(ME, f"A;OICI;FA;;;{ME}", "A;OICI;FA;;;SY", "A;OICI;FA;;;BA")
    assert sddl_is_private(text, ME)


def test_an_owner_rights_entry_is_the_owner_and_does_not_open_anything():
    assert sddl_is_private(sddl(ME, f"A;OICI;FA;;;{ME}", "A;OICI;FR;;;OW"), ME)


@pytest.mark.parametrize("trustee", ["WD", "BU", "AU", "IU", "AN", "S-1-1-0", OTHER, "S-1-5-32-545"])
def test_anyone_else_allowed_in_makes_it_not_private(trustee):
    assert not sddl_is_private(sddl(ME, f"A;OICI;FA;;;{ME}", f"A;OICI;FR;;;{trustee}"), ME)


def test_a_deny_entry_for_others_opens_nothing():
    assert sddl_is_private(sddl(ME, f"A;OICI;FA;;;{ME}", "D;OICI;FA;;;WD"), ME)


def test_no_dacl_means_everybody_has_access():
    assert not sddl_is_private(f"O:{ME}G:SY", ME)


def test_an_owner_that_is_somebody_else_is_not_private_even_with_a_tight_acl():
    assert not sddl_is_private(sddl(OTHER, f"A;OICI;FA;;;{ME}"), ME)


def test_administrators_may_own_it():
    assert sddl_is_private(sddl("BA", f"A;OICI;FA;;;{ME}"), ME)


# ---------- real directories ----------

def test_a_location_inside_the_project_is_refused_before_anything_is_created():
    inside = config.ROOT / "secrets-test-should-not-exist"
    with pytest.raises(SecretError) as error:
        secretfiles.ensure_private_dir(inside)
    assert error.value.code == "inside_project" and not inside.exists()
    with pytest.raises(SecretError):
        secretfiles.write_secret("a", "x", inside)


def test_a_directory_that_does_not_exist_is_reported_missing(tmp_path):
    with pytest.raises(SecretError) as error:
        secretfiles.verify_private(tmp_path / "nope")
    assert error.value.code == "missing"


def test_a_directory_made_here_is_private_and_a_secret_survives_a_round_trip(tmp_path):
    directory = tmp_path / "state" / "secrets"
    secretfiles.ensure_private_dir(directory)
    secretfiles.verify_private(directory)
    secretfiles.write_secret("db_password", "s3cr3t-value\n", directory)
    assert secretfiles.read_secret("db_password", directory) == "s3cr3t-value"
    secretfiles.verify_private(directory / "db_password")
    assert not list(directory.glob("*.tmp"))  # the atomic write left nothing behind


def test_an_empty_secret_is_refused(tmp_path):
    secretfiles.write_secret("empty", "", tmp_path / "s")
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("empty", tmp_path / "s")
    assert error.value.code == "empty"


def test_a_missing_secret_is_reported(tmp_path):
    secretfiles.ensure_private_dir(tmp_path / "s")
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("nothing", tmp_path / "s")
    assert error.value.code == "missing"


@pytest.mark.parametrize("name", ["", "../x", "a/b", "..\\x", "..", ".", ".hidden", "a:b", "a b", "C:x", "a\x00b",
                                  "x" * 101])
def test_a_secret_name_cannot_point_somewhere_else(tmp_path, name):
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret(name, tmp_path)
    assert error.value.code == "bad_name"


def test_the_error_never_carries_a_path_or_a_value(tmp_path):
    secretfiles.write_secret("db_password", "very-secret-value", tmp_path / "s")
    with pytest.raises(SecretError) as error:
        secretfiles.read_secret("db_password", config.ROOT / "x")
    assert str(tmp_path) not in str(error.value) and "very-secret-value" not in str(error.value)
    assert str(error.value) == error.value.code


@windows_only
def test_windows_a_directory_that_everyone_can_read_is_refused_and_so_is_a_secret_in_it(tmp_path):
    directory = tmp_path / "secrets"
    secretfiles.write_secret("db_password", "x", directory)
    subprocess.run(["icacls", str(directory), "/grant", "*S-1-1-0:(OI)(CI)R"], capture_output=True, check=True)
    with pytest.raises(SecretError) as error:
        secretfiles.verify_private(directory)
    assert error.value.code == "not_private"
    with pytest.raises(SecretError):
        secretfiles.read_secret("db_password", directory)


@windows_only
def test_windows_the_current_user_sid_is_found_and_is_in_the_acl(tmp_path):
    directory = tmp_path / "secrets"
    secretfiles.ensure_private_dir(directory)
    sid = secretfiles._windows_current_sid()
    assert sid.startswith("S-1-5-") and sid in secretfiles._windows_sddl(directory)


@posix_only
def test_posix_a_directory_open_to_others_is_refused(tmp_path):
    directory = tmp_path / "secrets"
    secretfiles.ensure_private_dir(directory)
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    directory.chmod(0o755)
    with pytest.raises(SecretError) as error:
        secretfiles.verify_private(directory)
    assert error.value.code == "not_private"


@posix_only
def test_posix_a_secret_file_is_written_with_mode_0600(tmp_path):
    secretfiles.write_secret("db_password", "x", tmp_path / "s")
    assert oct((tmp_path / "s" / "db_password").stat().st_mode & 0o777) == "0o600"


@posix_only
@pytest.mark.parametrize("kind", ["drvfs", "9p", "ntfs", "vfat", ""])
def test_posix_a_file_system_whose_permissions_mean_nothing_is_refused(tmp_path, monkeypatch, kind):
    secretfiles.ensure_private_dir(tmp_path / "s")
    monkeypatch.setattr(secretfiles, "_fs_type", lambda path: kind)  # e.g. a Windows drive mounted into WSL2
    with pytest.raises(SecretError) as error:
        secretfiles.verify_private(tmp_path / "s")
    assert error.value.code == "unsupported_fs"
