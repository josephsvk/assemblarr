from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

psycopg = types.ModuleType("psycopg")
psycopg_rows = types.ModuleType("psycopg.rows")
psycopg_types = types.ModuleType("psycopg.types")
psycopg_types_json = types.ModuleType("psycopg.types.json")
psycopg_rows.dict_row = object()
psycopg_types_json.Jsonb = lambda value: value
sys.modules.setdefault("psycopg", psycopg)
sys.modules.setdefault("psycopg.rows", psycopg_rows)
sys.modules.setdefault("psycopg.types", psycopg_types)
sys.modules.setdefault("psycopg.types.json", psycopg_types_json)

import queue_prowlarr_download as queue  # noqa: E402


class FakeResult:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return self._value

    def fetchall(self):
        return self._value


class FakeConn:
    def __init__(self, responses):
        self.responses = list(responses)

    def execute(self, _sql, _params):
        return FakeResult(self.responses.pop(0))


def test_candidate_previously_attempted_matches_release_guid():
    conn = FakeConn([{"exists": 1}])
    target = {"source": "radarr", "media_type": "movie_file", "source_id": 42}
    candidate = {"guid": "abc", "title": "Release A"}

    assert queue.candidate_previously_attempted(conn, target, candidate)


def test_candidate_previously_attempted_matches_release_title_without_guid():
    conn = FakeConn([{"exists": 1}])
    target = {"source": "radarr", "media_type": "movie_file", "source_id": 42}
    candidate = {"guid": "", "title": "Release A"}

    assert queue.candidate_previously_attempted(conn, target, candidate)


def test_candidate_previously_attempted_returns_false_for_new_release():
    conn = FakeConn([None])
    target = {"source": "radarr", "media_type": "movie_file", "source_id": 42}
    candidate = {"guid": "new-guid", "title": "Release B"}

    assert not queue.candidate_previously_attempted(conn, target, candidate)


def test_fetch_search_targets_can_read_rss_waitlist():
    conn = FakeConn(
        [[{"source": "radarr", "media_type": "movie_file", "source_id": 42, "title": "Movie", "year": 2024, "metadata": {}}]]
    )

    targets = queue.fetch_search_targets(conn, {"library_checks": {"missing_language": {}}}, "rss_waitlist", 10)

    assert targets == [{"source": "radarr", "media_type": "movie_file", "source_id": 42, "title": "Movie", "year": 2024, "metadata": {}}]
