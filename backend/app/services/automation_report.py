import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import xlsxwriter


STATUS_LABELS = {
    "file_copying": "文件复制中",
    "waiting": "等待处理",
    "processing": "处理中",
    "retrying": "等待重试",
    "completed": "已完成",
    "failed": "失败",
    "skipped": "已跳过",
}


class AutomationReport:
    def __init__(self, report_path: Path) -> None:
        self.report_path = report_path

    def export(self, config: dict[str, Any], records: list[dict[str, Any]]) -> Path:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.report_path.with_name(
            f".{self.report_path.name}.{uuid.uuid4().hex}.tmp"
        )
        workbook = xlsxwriter.Workbook(str(temporary))
        try:
            worksheet = workbook.add_worksheet("处理记录")
            worksheet.hide_gridlines(2)
            worksheet.freeze_panes(6, 3)

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
                "font_color": "#D1D5DB",
                "bg_color": "#1F2937",
                "align": "center",
                "valign": "vcenter",
                "border": 0,
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
                "bg_color": "#4F46E5",
                "align": "center",
                "valign": "vcenter",
                "border": 0,
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
            completed = workbook.add_format({
                "font_color": "#047857",
                "bg_color": "#ECFDF5",
                "align": "center",
            })
            failed = workbook.add_format({
                "font_color": "#B91C1C",
                "bg_color": "#FEF2F2",
                "align": "center",
            })
            active = workbook.add_format({
                "font_color": "#6D28D9",
                "bg_color": "#F5F3FF",
                "align": "center",
            })
            waiting = workbook.add_format({
                "font_color": "#92400E",
                "bg_color": "#FFFBEB",
                "align": "center",
            })

            worksheet.set_row(0, 30)
            worksheet.merge_range("A1:R1", "StemFlow 自动人声提取处理记录", title)
            worksheet.write("A2", "监控目录", label)
            worksheet.merge_range("B2:F2", config["input_dir"], value)
            worksheet.write("G2", "输出目录", label)
            worksheet.merge_range("H2:L2", config["output_dir"], value)
            worksheet.write("M2", "自动任务", label)
            worksheet.merge_range("N2:R2", "已开启" if config["enabled"] else "已关闭", value)
            schedule_text = (
                f"每天 {config['daily_time']} · {config['timezone']}"
                if config.get("schedule_mode") == "daily"
                else f"每 {config['interval_minutes']} 分钟"
            )
            worksheet.write("A3", "执行计划", label)
            worksheet.merge_range("B3:D3", schedule_text, value)
            worksheet.write("E3", "文件稳定", label)
            worksheet.write("F3", f"{config['stable_seconds']} 秒", value)
            worksheet.write("G3", "失败重试", label)
            worksheet.write("H3", f"{config['max_retries']} 次", value)
            worksheet.write("I3", "分离模型", label)
            worksheet.merge_range("J3:L3", config["model"], value)
            worksheet.write("M3", "报表生成", label)
            worksheet.merge_range(
                "N3:R3",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                value,
            )

            counts: dict[str, int] = {}
            for record in records:
                counts[record["status"]] = counts.get(record["status"], 0) + 1
            summary_values = [
                ("全部", len(records)),
                ("等待", counts.get("waiting", 0) + counts.get("file_copying", 0)),
                ("处理中", counts.get("processing", 0) + counts.get("retrying", 0)),
                ("完成", counts.get("completed", 0)),
                ("失败", counts.get("failed", 0)),
                ("跳过", counts.get("skipped", 0)),
            ]
            for index, (name, count) in enumerate(summary_values):
                col = index * 3
                worksheet.merge_range(3, col, 3, col + 1, name, section)
                worksheet.write_number(3, col + 2, count, section)

            headers = [
                "文件名", "相对路径", "文件类型", "文件大小(B)", "修改时间",
                "发现时间", "开始时间", "完成时间", "状态", "进度(%)",
                "重试次数", "人声文件", "伴奏文件", "使用模型", "处理耗时(秒)",
                "错误信息", "任务ID", "源文件路径",
            ]
            worksheet.write_row(5, 0, headers, header)
            worksheet.set_row(5, 26)

            status_formats = {
                "completed": completed,
                "failed": failed,
                "processing": active,
                "retrying": active,
                "waiting": waiting,
                "file_copying": waiting,
                "skipped": waiting,
            }
            for row_index, record in enumerate(records, start=6):
                modified_at = self._as_datetime(record.get("modified_at"))
                detected_at = self._as_datetime(record.get("detected_at"))
                started_at = self._as_datetime(record.get("started_at"))
                finished_at = self._as_datetime(record.get("finished_at"))
                worksheet.write(row_index, 0, Path(record["source_path"]).name, text)
                worksheet.write(row_index, 1, record["relative_path"], text)
                worksheet.write(row_index, 2, record["media_type"], text)
                worksheet.write_number(row_index, 3, int(record["size"]), integer)
                self._write_datetime(worksheet, row_index, 4, modified_at, datetime_format)
                self._write_datetime(worksheet, row_index, 5, detected_at, datetime_format)
                self._write_datetime(worksheet, row_index, 6, started_at, datetime_format)
                self._write_datetime(worksheet, row_index, 7, finished_at, datetime_format)
                worksheet.write(
                    row_index,
                    8,
                    STATUS_LABELS.get(record["status"], record["status"]),
                    status_formats.get(record["status"], text),
                )
                worksheet.write_number(row_index, 9, float(record.get("progress") or 0), decimal)
                worksheet.write_number(row_index, 10, int(record.get("attempts") or 0), integer)
                worksheet.write(row_index, 11, record.get("vocals_path") or "", text)
                worksheet.write(row_index, 12, record.get("instrumental_path") or "", text)
                worksheet.write(row_index, 13, record.get("model") or "", text)
                if record.get("duration_seconds") is None:
                    worksheet.write_blank(row_index, 14, None, decimal)
                else:
                    worksheet.write_number(
                        row_index,
                        14,
                        float(record["duration_seconds"]),
                        decimal,
                    )
                worksheet.write(row_index, 15, record.get("error") or "", text)
                worksheet.write(row_index, 16, record.get("task_id") or "", text)
                worksheet.write(row_index, 17, record["source_path"], text)

            last_row = max(6, len(records) + 5)
            worksheet.autofilter(5, 0, last_row, len(headers) - 1)
            worksheet.set_column("A:A", 24)
            worksheet.set_column("B:B", 34)
            worksheet.set_column("C:C", 11)
            worksheet.set_column("D:D", 14)
            worksheet.set_column("E:H", 20)
            worksheet.set_column("I:I", 13)
            worksheet.set_column("J:K", 11)
            worksheet.set_column("L:M", 38)
            worksheet.set_column("N:N", 24)
            worksheet.set_column("O:O", 14)
            worksheet.set_column("P:P", 42)
            worksheet.set_column("Q:Q", 34)
            worksheet.set_column("R:R", 52)
        finally:
            workbook.close()
        os.replace(temporary, self.report_path)
        return self.report_path

    @staticmethod
    def _as_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)

    @staticmethod
    def _write_datetime(
        worksheet: Any,
        row: int,
        column: int,
        value: datetime | None,
        cell_format: Any,
    ) -> None:
        if value is None:
            worksheet.write_blank(row, column, None, cell_format)
        else:
            worksheet.write_datetime(row, column, value, cell_format)
