import mimetypes
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.config import get_settings
from app.dependencies import get_automation_service, get_repository, get_task_manager
from app.models import (
    ArtifactRecord,
    AutomationConfigRecord,
    AutomationFileRecord,
    AutomationScanResult,
    AutomationUpdate,
    ItemRecord,
    TaskCreate,
    TaskRecord,
    UploadResponse,
)


router = APIRouter(prefix="/api")


def resolve_user_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def task_or_404(task_id: str) -> dict:
    task = get_repository().get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "task_mode": settings.task_mode,
        "worker_concurrency": settings.worker_concurrency,
    }


@router.get("/automation", response_model=AutomationConfigRecord)
async def get_automation() -> dict:
    return get_automation_service().status()


@router.put("/automation", response_model=AutomationConfigRecord)
async def update_automation(payload: AutomationUpdate) -> dict:
    input_path = resolve_user_path(payload.input_dir)
    output_path = resolve_user_path(payload.output_dir)
    if not input_path.is_dir():
        raise HTTPException(422, f"监控目录不存在或不是文件夹：{input_path}")
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HTTPException(422, f"输出目录不可写：{error}") from error
    return await get_automation_service().update_config({
        **payload.model_dump(),
        "input_dir": str(input_path),
        "output_dir": str(output_path),
    })


@router.post("/automation/scan", response_model=AutomationScanResult)
async def scan_automation() -> dict:
    try:
        return await get_automation_service().scan_now()
    except (FileNotFoundError, OSError, ValueError) as error:
        raise HTTPException(422, str(error)) from error


@router.post("/automation/retry-failed")
async def retry_failed_automation() -> dict:
    count = await get_automation_service().retry_failed()
    return {"reset_files": count}


@router.get("/automation/files", response_model=list[AutomationFileRecord])
async def list_automation_files(
    limit: int = Query(1000, ge=1, le=5000),
) -> list[dict]:
    return get_automation_service().files(limit)


@router.get("/automation/report")
async def download_automation_report() -> FileResponse:
    report_path = get_settings().automation_report_path
    if not report_path.is_file():
        raise HTTPException(404, "Excel状态表尚未生成")
    return FileResponse(
        report_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="processing_status.xlsx",
        content_disposition_type="attachment",
    )


@router.post("/tasks", response_model=TaskRecord, status_code=202)
async def create_task(payload: TaskCreate) -> dict:
    input_path = resolve_user_path(payload.input_dir)
    output_path = resolve_user_path(payload.output_dir)
    if not input_path.exists():
        raise HTTPException(422, f"Input path does not exist: {input_path}")
    if not input_path.is_dir() and not input_path.is_file():
        raise HTTPException(422, "Input path must be a file or directory")
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HTTPException(422, f"Output path is not writable: {error}") from error
    task = get_repository().create_task(str(input_path), str(output_path), payload.model)
    await get_task_manager().submit(task["id"])
    return task


@router.get("/tasks", response_model=list[TaskRecord])
async def list_tasks(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    return get_repository().list_tasks(limit)


@router.get("/tasks/{task_id}", response_model=TaskRecord)
async def get_task(task_id: str) -> dict:
    return task_or_404(task_id)


@router.get("/tasks/{task_id}/items", response_model=list[ItemRecord])
async def get_task_items(task_id: str) -> list[dict]:
    task_or_404(task_id)
    return get_repository().list_items(task_id)


@router.get("/tasks/{task_id}/files", response_model=list[ArtifactRecord])
async def get_task_files(task_id: str) -> list[dict]:
    task_or_404(task_id)
    records = []
    for artifact in get_repository().list_artifacts(task_id):
        artifact["url"] = f"/api/tasks/{task_id}/files/{artifact['id']}"
        artifact["download_url"] = f"/api/tasks/{task_id}/files/{artifact['id']}?download=true"
        records.append(artifact)
    return records


@router.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: str) -> list[dict]:
    task_or_404(task_id)
    return get_repository().list_logs(task_id)


@router.get("/tasks/{task_id}/files/{artifact_id}")
async def serve_artifact(task_id: str, artifact_id: str, download: bool = False) -> FileResponse:
    artifact = get_repository().get_artifact(task_id, artifact_id)
    if not artifact:
        raise HTTPException(404, "Result file not found")
    path = Path(artifact["path"])
    if not path.is_file():
        raise HTTPException(410, "Result file is no longer available")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=artifact["name"] if download else None,
        content_disposition_type="attachment" if download else "inline",
    )


def safe_upload_path(upload_root: Path, relative_name: str) -> Path:
    parts = [part for part in PurePosixPath(relative_name.replace("\\", "/")).parts if part not in {"", ".", ".."}]
    if not parts:
        raise HTTPException(422, "Invalid upload filename")
    target = upload_root.joinpath(*parts).resolve()
    if not target.is_relative_to(upload_root.resolve()):
        raise HTTPException(422, "Invalid upload path")
    return target


@router.post("/uploads", response_model=UploadResponse)
async def upload_folder(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
) -> UploadResponse:
    upload_id = uuid.uuid4().hex
    upload_root = get_settings().data_dir / "uploads" / upload_id
    upload_root.mkdir(parents=True, exist_ok=False)
    count = 0
    for index, upload in enumerate(files):
        relative = relative_paths[index] if index < len(relative_paths) else (upload.filename or f"file-{index}")
        target = safe_upload_path(upload_root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                destination.write(chunk)
        await upload.close()
        count += 1
    return UploadResponse(upload_id=upload_id, input_dir=str(upload_root), file_count=count)
