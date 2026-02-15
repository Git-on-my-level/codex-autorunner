from codex_autorunner.core.diagnostics.process_snapshot import (
    ProcessCategory,
    _classify_process,
    parse_ps_output,
)

SAMPLE_PS_OUTPUT = """\
1234 /usr/bin/opencode serve --port 8080
1235 /usr/bin/codex_autorunner run
1236 /usr/bin/codex app-server --port 9000
1237 /usr/bin/some-other-process --arg
1238 /Users/user/.local/bin/opencode
1239 /opt/codex/bin/codex app-server --workspace /tmp/ws
1240 /bin/bash -c /usr/bin/opencode
1241 /usr/bin/other-tool
"""


class TestClassifyProcess:
    def test_opencode_in_command(self):
        assert _classify_process("/usr/bin/opencode serve") == ProcessCategory.OPENCODE

    def test_codex_autorunner_in_command(self):
        assert (
            _classify_process("/usr/bin/codex_autorunner run")
            == ProcessCategory.OPENCODE
        )

    def test_app_server_classic_format(self):
        assert (
            _classify_process("/usr/bin/codex app-server") == ProcessCategory.APP_SERVER
        )

    def test_app_server_alternative_formats(self):
        assert (
            _classify_process("codexapp-server --port 8080")
            == ProcessCategory.APP_SERVER
        )
        assert (
            _classify_process("codex:app-server --workspace /foo")
            == ProcessCategory.APP_SERVER
        )
        assert (
            _classify_process("codex -- app-server --port 9000")
            == ProcessCategory.APP_SERVER
        )

    def test_other_process(self):
        assert _classify_process("/usr/bin/some-other-process") == ProcessCategory.OTHER
        assert _classify_process("bash -c echo hello") == ProcessCategory.OTHER


class TestParsePsOutput:
    def test_parse_sample_output(self):
        snapshot = parse_ps_output(SAMPLE_PS_OUTPUT)
        assert snapshot.opencode_count == 4
        assert snapshot.app_server_count == 2
        assert len(snapshot.other_processes) == 2

    def test_opencode_processes_correct(self):
        snapshot = parse_ps_output(SAMPLE_PS_OUTPUT)
        pids = {p.pid for p in snapshot.opencode_processes}
        assert pids == {1234, 1235, 1238, 1240}

    def test_app_server_processes_correct(self):
        snapshot = parse_ps_output(SAMPLE_PS_OUTPUT)
        pids = {p.pid for p in snapshot.app_server_processes}
        assert pids == {1236, 1239}

    def test_other_processes_correct(self):
        snapshot = parse_ps_output(SAMPLE_PS_OUTPUT)
        pids = {p.pid for p in snapshot.other_processes}
        assert pids == {1237, 1241}

    def test_empty_output(self):
        snapshot = parse_ps_output("")
        assert snapshot.opencode_count == 0
        assert snapshot.app_server_count == 0
        assert len(snapshot.other_processes) == 0

    def test_malformed_lines_skipped(self):
        malformed = """\
1234 good command
not_a_pid bad line
1235 another good command
"""
        snapshot = parse_ps_output(malformed)
        assert len(snapshot.opencode_processes) + len(snapshot.other_processes) == 2

    def test_to_dict(self):
        snapshot = parse_ps_output(SAMPLE_PS_OUTPUT)
        d = snapshot.to_dict()
        assert "collected_at" in d
        assert "opencode" in d
        assert "app_server" in d
        assert "other" in d
        assert len(d["opencode"]) == 4
        assert len(d["app_server"]) == 2


class TestProcessSnapshot:
    def test_counts(self):
        snapshot = parse_ps_output(SAMPLE_PS_OUTPUT)
        assert snapshot.opencode_count == 4
        assert snapshot.app_server_count == 2
