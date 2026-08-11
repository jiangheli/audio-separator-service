from pathlib import Path

from app.models import VideoFile
from app.repository import ProcessingRepository


def make_video(tmp_path: Path) -> VideoFile:
    source = tmp_path / "input" / "demo.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    stat = source.stat()
    return VideoFile(source, Path("demo.mp4"), stat.st_size, stat.st_mtime_ns)


def test_repository_claims_once_and_limits_retries(tmp_path: Path) -> None:
    repository = ProcessingRepository(tmp_path / "processing.db")
    video = make_video(tmp_path)
    job = repository.register(
        video,
        fingerprint="fingerprint",
        model="mdx",
        output_path=tmp_path / "output.mp4",
        status="pending",
    )

    first = repository.claim(job["id"], max_retries=1)
    assert first is not None
    assert first["attempts"] == 1
    assert repository.claim(job["id"], max_retries=1) is None

    repository.update(job["id"], status="failed", error="temporary")
    second = repository.claim(job["id"], max_retries=1)
    assert second is not None
    assert second["attempts"] == 2
    repository.update(job["id"], status="failed", error="again")
    assert repository.eligible(max_retries=1) == []


def test_repository_recovers_interrupted_job(tmp_path: Path) -> None:
    repository = ProcessingRepository(tmp_path / "processing.db")
    video = make_video(tmp_path)
    job = repository.register(
        video,
        fingerprint="other",
        model="mdx",
        output_path=tmp_path / "output.mp4",
        status="pending",
    )
    repository.claim(job["id"], max_retries=3)
    repository.update(job["id"], status="separating_vocals")

    assert repository.recover_interrupted() == 1
    recovered = repository.get(job["id"])
    assert recovered is not None
    assert recovered["status"] == "failed"
    assert "stopped" in recovered["error"]


def test_new_fingerprint_supersedes_unprocessed_source_version(tmp_path: Path) -> None:
    repository = ProcessingRepository(tmp_path / "processing.db")
    video = make_video(tmp_path)
    old = repository.register(
        video,
        fingerprint="old-version",
        model="mdx",
        output_path=tmp_path / "output.mp4",
        status="waiting_copy",
    )
    repository.register(
        video,
        fingerprint="new-version",
        model="mdx",
        output_path=tmp_path / "output.mp4",
        status="pending",
    )

    superseded = repository.get(old["id"])
    assert superseded is not None
    assert superseded["status"] == "superseded"


def test_repository_scopes_jobs_to_selected_input_folder(tmp_path: Path) -> None:
    repository = ProcessingRepository(tmp_path / "processing.db")
    roots = (tmp_path / "input-a", tmp_path / "input-b")
    jobs = []
    for index, root in enumerate(roots):
        source = root / f"episode-{index}.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"video")
        stat = source.stat()
        video = VideoFile(
            source,
            Path(source.name),
            stat.st_size,
            stat.st_mtime_ns,
        )
        jobs.append(
            repository.register(
                video,
                fingerprint=f"fingerprint-{index}",
                model="mdx",
                output_path=tmp_path / "output" / source.name,
                status="pending",
                input_root=root,
            )
        )

    assert [job["id"] for job in repository.eligible(3, roots[0])] == [
        jobs[0]["id"]
    ]
    assert [job["id"] for job in repository.list_jobs(input_root=roots[1])] == [
        jobs[1]["id"]
    ]
    assert repository.counts(roots[0]) == {"pending": 1, "total": 1}

    repository.assign_input_root_many([jobs[0]["id"]], roots[1])
    assert repository.counts(roots[0]) == {"total": 0}
    assert repository.counts(roots[1]) == {"pending": 2, "total": 2}
    repository.assign_input_root(jobs[0]["id"], roots[0])

    repository.claim(jobs[0]["id"], max_retries=3)
    repository.update(jobs[0]["id"], status="separating_vocals")
    assert repository.recover_interrupted(roots[1]) == 0
    assert repository.recover_interrupted(roots[0]) == 1
