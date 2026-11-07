"""Tests for Windows AppContainer Profile Lifecycle and Identity Isolation.

Validates:
- Strict session ID formatting and canonicalization (UUIDv4 and 32-character hex).
- Deterministic profile naming ("Anaya.Worker.<session_id>").
- Real Windows CreateAppContainerProfile / DeleteAppContainerProfile lifecycle.
- Distinct AppContainer SIDs for distinct session identifiers.
- Idempotent creation (DeriveAppContainerSidFromAppContainerName on HRESULT_ALREADY_EXISTS).
- Idempotent deletion (ignoring HRESULT_FILE_NOT_FOUND).
- Native binary PSID ownership and FreeSid deallocation.
- Context manager protocol and resource finalization.
"""

from __future__ import annotations

import sys
import uuid
import pytest

from app.skills.sandbox.appcontainer import (
    AppContainerError,
    AppContainerProfile,
    InvalidSessionIdError,
    ProfileCreationError,
    ProfileDeletionError,
    validate_session_id,
)

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# 1. Session ID Validation Tests (Platform-Independent)
# ---------------------------------------------------------------------------

class TestSessionIdValidation:
    def test_valid_uuidv4_strings(self) -> None:
        valid_uuids = [
            str(uuid.uuid4()),
            "a5c898f2-8924-4f46-9d32-212d308b3ee1",
            "A5C898F2-8924-4F46-9D32-212D308B3EE1",  # uppercase normalized to lowercase
            "00000000-0000-4000-8000-000000000000",
            "ffffffff-ffff-4fff-bfff-ffffffffffff",
        ]
        for sid in valid_uuids:
            validated = validate_session_id(sid)
            assert validated == sid.strip().lower()

    def test_valid_32_char_hex_strings(self) -> None:
        hex_strings = [
            "0123456789abcdef0123456789abcdef",
            "A5C898F289244F469D32212D308B3EE1",
            "0" * 32,
            "f" * 32,
        ]
        for s in hex_strings:
            validated = validate_session_id(s)
            assert validated == s.strip().lower()

    def test_invalid_session_ids_rejected(self) -> None:
        invalid_ids = [
            "",
            "   ",
            "not-a-uuid",
            "../traversal/path",
            "01234567-89ab-3def-0123-456789abcdef",  # version digit is 3, not 4
            "01234567-89ab-4def-0123-456789abcdef",  # variant digit is 0, not [89ab]
            "0123456789abcdef",                       # 16 chars instead of 32
            "0123456789abcdef0123456789abcdefg",      # 33 chars / invalid hex char 'g'
            None,
            12345,
        ]
        for invalid in invalid_ids:
            with pytest.raises(InvalidSessionIdError):
                validate_session_id(invalid)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. Windows AppContainer Profile Lifecycle Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IS_WINDOWS, reason="AppContainer profiles require Windows.")
class TestAppContainerProfileLifecycle:
    def test_profile_naming_and_initial_state(self) -> None:
        session_id = str(uuid.uuid4())
        profile = AppContainerProfile(session_id)
        try:
            assert profile.session_id == session_id
            assert profile.profile_name == f"Anaya.Worker.{session_id}"
            with pytest.raises(AppContainerError, match="not been initialized"):
                _ = profile.sid_string
            with pytest.raises(AppContainerError, match="not initialized"):
                _ = profile.sid
        finally:
            profile.close()

    def test_create_and_delete_profile(self) -> None:
        session_id = str(uuid.uuid4())
        profile = AppContainerProfile(session_id)
        try:
            profile.ensure_created()
            assert profile.sid != 0
            sid_str = profile.sid_string
            assert sid_str.startswith("S-1-15-2-")

            # Deleting profile should succeed
            profile.delete_profile()

            # Second delete is idempotent and succeeds without error
            profile.delete_profile()
        finally:
            profile.close()

    def test_idempotent_creation(self) -> None:
        session_id = str(uuid.uuid4())
        p1 = AppContainerProfile(session_id)
        p2 = AppContainerProfile(session_id)
        try:
            p1.ensure_created()
            sid1 = p1.sid_string

            # p2 creates the same profile name; userenv returns HRESULT_ALREADY_EXISTS
            # and p2 must derive the identical SID
            p2.ensure_created()
            sid2 = p2.sid_string

            assert sid1 == sid2
            assert sid1.startswith("S-1-15-2-")
        finally:
            p1.delete_profile()
            p1.close()
            p2.close()

    def test_distinct_sessions_yield_distinct_sids(self) -> None:
        session_id_1 = str(uuid.uuid4())
        session_id_2 = str(uuid.uuid4())

        p1 = AppContainerProfile(session_id_1)
        p2 = AppContainerProfile(session_id_2)
        try:
            p1.ensure_created()
            p2.ensure_created()

            assert p1.sid_string != p2.sid_string
            assert p1.sid_string.startswith("S-1-15-2-")
            assert p2.sid_string.startswith("S-1-15-2-")
        finally:
            p1.delete_profile()
            p1.close()
            p2.delete_profile()
            p2.close()

    def test_context_manager_protocol(self) -> None:
        session_id = str(uuid.uuid4())
        with AppContainerProfile(session_id) as prof:
            assert prof.sid != 0
            assert prof.sid_string.startswith("S-1-15-2-")
            prof.delete_profile()

        # After exiting context, profile is closed and accessing sid raises
        with pytest.raises(AppContainerError, match="closed"):
            _ = prof.sid

    def test_cannot_create_closed_profile(self) -> None:
        session_id = str(uuid.uuid4())
        prof = AppContainerProfile(session_id)
        prof.close()
        with pytest.raises(AppContainerError, match="closed profile"):
            prof.ensure_created()
