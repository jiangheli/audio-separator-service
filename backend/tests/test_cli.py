from app.cli.main import build_parser


def test_cli_exposes_headless_windows_commands() -> None:
    parser = build_parser()

    run = parser.parse_args(["run-once", "--config", "stemflow.json", "--max-files", "2"])
    status = parser.parse_args(["status", "--config", "stemflow.json", "--json"])
    validate = parser.parse_args(["validate", "--config", "stemflow.json"])

    assert run.command == "run-once"
    assert run.max_files == 2
    assert status.command == "status"
    assert status.json_output is True
    assert validate.command == "validate"
