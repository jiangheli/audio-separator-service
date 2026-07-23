from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

import xlsxwriter

from app.config import ServiceConfig


STATUS_LABELS = {
    "waiting_copy": "等待文件写入完成",
    "pending": "等待处理",
    "processing": "准备处理",
    "extracting_audio": "提取临时音频",
    "separating_vocals": "分离人声",
    "composing_video": "重新合成视频",
    "completed": "已完成",
    "failed": "失败",
    "superseded": "源文件已更新",
}


class ProcessingReport:
    def __init__(self, report_path: Path) -> None:
        self.report_path = report_path

    def export(self, config: ServiceConfig, jobs: list[dict[str, Any]]) -> Path:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(
            f".{self.report_path.name}.{uuid.uuid4().hex}.tmp"
        )
        workbook = xlsxwriter.Workbook(str(temporary))
        try:
            sheet = workbook.add_worksheet("处理记录")
            sheet.hide_gridlines(2)
            sheet.freeze_panes(6, 3)

            title = workbook.add_format({
                "bold": True,
                "font_size": 18,
                "font_color": "#FFFFFF",
                "bg_color": "#111827",
                "align": "left",
                "valign": "vcenter",
            })
            section = workbook.add_format({
                "bold": True,
                "font_color": "#E5E7EB",
                "bg_color": "#1F2937",
                "align": "center",
                "valign": "vcenter",
            })
            label = workbook.add_format({
                "font_color": "#6B7280",
                "bg_color": "#F3F4F6",
                "align": "left",
            })
            value = workbook.add_format({
                "font_color": "#111827",
                "bg_color": "#F9FAFB",
                "align": "left",
            })
            header = workbook.add_format({
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#2563EB",
                "align": "center",
                "valign": "vcenter",
            })
            text = workbook.add_format({"font_color": "#374151", "valign": "vcenter"})
            integer = workbook.add_format({
                "font_color": "#374151",
                "num_format": "#,##0",
                "align": "right",
            })
            decimal = workbook.add_format({
                "font_color": "#374151",
                "num_format": "0.0",
                "align": "right",
            })
            datetime_format = workbook.add_format({
                "font_color": "#374151",
                "num_format": "yyyy-mm-dd hh:mm:ss",
            })
            completed_format = workbook.add_format({
                "font_color": "#047857",
                "bg_color": "#ECFDF5",
                "align": "center",
            })
            failed_format = workbook.add_format({
                "font_color": "#B91C1C",
                "bg_color": "#FEF2F2",
                "align": "center",
            })
            active_format = workbook.add_format({
                "font_color": "#6D28D9",
                "bg_color": "#F5F3FF",
                "align": "center",
            })
            waiting_format = workbook.add_format({
                "font_color": "#92400E",
                "bg_color": "#FFFBEB",
                "align": "center",
            })

            sheet.set_row(0, 30)
            sheet.merge_range("A1:O1", "StemFlow 视频去 BGM 处理记录", title)
            sheet.write("A2", "监控目录", label)
            sheet.merge_range("B2:E2", str(config.input_dir), value)
            sheet.write("F2", "输出目录", label)
            sheet.merge_range("G2:J2", str(config.output_dir), value)
            sheet.write("K2", "执行计划", label)
            sheet.merge_range("L2:O2", f"每天 {config.schedule_time}", value)
            sheet.write("A3", "处理模型", label)
            sheet.merge_range("B3:D3", config.model, value)
            sheet.write("E3", "文件稳定等待", label)
            sheet.write("F3", f"{config.stable_seconds} 秒", value)
            sheet.write("G3", "失败重试", label)
            sheet.write("H3", f"{config.max_retries} 次", value)
            sheet.write("I3", "最终产物", label)
            sheet.merge_range("J3:L3", "原视频画面 + 纯人声音轨", value)
            sheet.write("M3", "报表生成", label)
            sheet.merge_range(
                "N3:O3",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                value,
            )

            counts: dict[str, int] = {}
            for job in jobs:
                counts[job["status"]] = counts.get(job["status"], 0) + 1
            summary = [
                ("全部", len(jobs)),
                ("等待", counts.get("pending", 0) + counts.get("waiting_copy", 0)),
                (
                    "处理中",
                    sum(counts.get(status, 0) for status in (
                        "processing",
                        "extracting_audio",
                        "separating_vocals",
                        "composing_video",
                    )),
                ),
                ("完成", counts.get("completed", 0)),
                ("失败", counts.get("failed", 0)),
            ]
            for index, (name, count) in enumerate(summary):
                column = index * 3
                sheet.merge_range(3, column, 3, column + 1, name, section)
                sheet.write_number(3, column + 2, count, section)

            headers = [
                "文件名",
                "相对路径",
                "状态",
                "尝试次数",
                "文件大小(B)",
                "发现时间",
                "开始时间",
                "完成时间",
                "处理耗时(秒)",
                "模型",
                "画面直拷",
                "输出视频",
                "错误信息",
                "源文件路径",
                "任务指纹",
            ]
            sheet.write_row(5, 0, headers, header)
            sheet.set_row(5, 26)

            status_formats = {
                "completed": completed_format,
                "failed": failed_format,
                "processing": active_format,
                "extracting_audio": active_format,
                "separating_vocals": active_format,
                "composing_video": active_format,
                "pending": waiting_format,
                "waiting_copy": waiting_format,
                "superseded": waiting_format,
            }
            for row, job in enumerate(jobs, start=6):
                source_path = str(job["source_path"])
                source_name = (
                    PureWindowsPath(source_path).name
                    if "\\" in source_path
                    else Path(source_path).name
                )
                sheet.write(row, 0, source_name, text)
                sheet.write(row, 1, job["relative_path"], text)
                sheet.write(
                    row,
                    2,
                    STATUS_LABELS.get(job["status"], job["status"]),
                    status_formats.get(job["status"], text),
                )
                sheet.write_number(row, 3, int(job["attempts"]), integer)
                sheet.write_number(row, 4, int(job["size"]), integer)
                self._write_datetime(sheet, row, 5, job.get("detected_at"), datetime_format)
                self._write_datetime(sheet, row, 6, job.get("started_at"), datetime_format)
                self._write_datetime(sheet, row, 7, job.get("finished_at"), datetime_format)
                if job.get("duration_seconds") is None:
                    sheet.write_blank(row, 8, None, decimal)
                else:
                    sheet.write_number(row, 8, float(job["duration_seconds"]), decimal)
                sheet.write(row, 9, job.get("model") or "", text)
                video_copy = job.get("used_video_copy")
                sheet.write(
                    row,
                    10,
                    "" if video_copy is None else ("是" if video_copy else "否，已转码"),
                    text,
                )
                sheet.write(row, 11, job.get("output_path") or "", text)
                sheet.write(row, 12, job.get("error") or "", text)
                sheet.write(row, 13, job["source_path"], text)
                sheet.write(row, 14, job["fingerprint"], text)

            last_row = max(6, len(jobs) + 5)
            sheet.autofilter(5, 0, last_row, len(headers) - 1)
            sheet.set_column("A:A", 26)
            sheet.set_column("B:B", 34)
            sheet.set_column("C:C", 18)
            sheet.set_column("D:E", 14)
            sheet.set_column("F:H", 20)
            sheet.set_column("I:I", 15)
            sheet.set_column("J:J", 28)
            sheet.set_column("K:K", 13)
            sheet.set_column("L:L", 52)
            sheet.set_column("M:M", 48)
            sheet.set_column("N:N", 52)
            sheet.set_column("O:O", 66, None, {"hidden": True})
        finally:
            workbook.close()
        os.replace(temporary, self.report_path)
        return self.report_path

    @staticmethod
    def _write_datetime(
        sheet: Any,
        row: int,
        column: int,
        value: Any,
        cell_format: Any,
    ) -> None:
        if not value:
            sheet.write_blank(row, column, None, cell_format)
            return
        timestamp = datetime.fromisoformat(str(value)).replace(tzinfo=None)
        sheet.write_datetime(row, column, timestamp, cell_format)
