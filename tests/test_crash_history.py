# -*- coding: utf-8 -*-
"""硬件级意外重启检测（src/safety/crash_history）的回归测试。

回归背景：2026-11-05 机器连续 3 次「无蓝屏脏断电」重启（WHEA-Logger 18 /
Cache Hierarchy Error / BugcheckCode=0），用户误以为是脚本导致的。本模块把
这类硬件级信号做成自检项；这里锁定事件解析、窗口过滤、汇总文案与注入语义。
"""

import unittest
from datetime import datetime, timedelta

from src.safety.crash_history import (
    KERNEL_POWER_PROVIDER, WHEA_PROVIDER, FatalRebootEvent,
    crash_history_item, list_fatal_reboot_events, parse_event_xml,
    parse_powershell_line, summarize_events)

LOCAL = datetime(2026, 11, 6, 12, 0, 0)  # 测试用的"现在"（本地 naive）


def _event(time, provider=KERNEL_POWER_PROVIDER, event_id=41,
           bugcheck=0, error_type=""):
    return FatalRebootEvent(time=time, provider=provider, event_id=event_id,
                            bugcheck_code=bugcheck, error_type=error_type)


class ParseEventXmlTests(unittest.TestCase):
    KERNEL_POWER_XML = """
    <Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
      <System>
        <Provider Name="Microsoft-Windows-Kernel-Power" Guid="{...}"/>
        <EventID>41</EventID>
        <Level>1</Level>
        <TimeCreated SystemTime="2026-11-05T11:54:31.1234567Z"/>
      </System>
      <EventData>
        <Data Name="BugcheckCode">0</Data>
        <Data Name="PowerButtonTimestamp">0</Data>
      </EventData>
    </Event>
    """

    WHEA_XML = """
    <Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
      <System>
        <Provider Name="Microsoft-Windows-WHEA-Logger" Guid="{...}"/>
        <EventID>18</EventID>
        <Level>2</Level>
        <TimeCreated SystemTime="2026-11-05T11:54:37.0000000Z"/>
      </System>
      <EventData>
        <Data Name="ErrorSource">3</Data>
        <Data Name="ApicId">5</Data>
        <Data Name="ErrorType">9</Data>
      </EventData>
    </Event>
    """

    def test_kernel_power_41_parses_bugcheck_and_local_time(self):
        event = parse_event_xml(self.KERNEL_POWER_XML)
        self.assertIsNotNone(event)
        self.assertEqual(KERNEL_POWER_PROVIDER, event.provider)
        self.assertEqual(41, event.event_id)
        self.assertEqual(0, event.bugcheck_code)
        # UTC 11:54 → 本机时区（+8）19:54
        self.assertEqual(19, event.time.hour)
        self.assertEqual(5, event.time.day)

    def test_whea_18_xml_has_numeric_error_type_code(self):
        # 真实 WHEA XML 的 ErrorType 是数字枚举（Cache Hierarchy Error = 9），
        # 可读文案要靠 PowerShell 路径；XML 兜底路径透传类型码。
        event = parse_event_xml(self.WHEA_XML)
        self.assertIsNotNone(event)
        self.assertEqual(WHEA_PROVIDER, event.provider)
        self.assertEqual("9", event.error_type)
        self.assertIn("类型码 9", event.description)

    def test_garbage_or_foreign_events_are_ignored(self):
        self.assertIsNone(parse_event_xml("not xml at all"))
        self.assertIsNone(parse_event_xml("<Event><System/></Event>"))
        foreign = self.WHEA_XML.replace("Microsoft-Windows-WHEA-Logger",
                                        "Some-Other-Provider")
        self.assertIsNone(parse_event_xml(foreign))
        # 来源对但事件号不对（WHEA 可纠正错误 19 等）不收
        wrong_id = self.WHEA_XML.replace("<EventID>18</EventID>",
                                         "<EventID>19</EventID>")
        self.assertIsNone(parse_event_xml(wrong_id))


class ParsePowerShellLineTests(unittest.TestCase):
    WHEA_LINE = (
        "2026-11-05 20:01:31\t18\tMicrosoft-Windows-WHEA-Logger\t0\t"
        "出现致命硬件错误。 | 由以下组件报告: 处理器核心 | 错误源: "
        "Machine Check Exception | 错误类型: Cache Hierarchy Error | "
        "处理器 APIC ID: 5 | 该项的详细信息视图包含进一步的信息。")
    KP_LINE = (
        "2026-11-05 20:01:26\t41\tMicrosoft-Windows-Kernel-Power\t0\t"
        "系统已在未先正常关机的情况下重新启动。 | 如果系统停止响应、"
        "发生崩溃或意外断电，则可能会导致此错误。")

    def test_whea_message_error_type_is_extracted(self):
        event = parse_powershell_line(self.WHEA_LINE)
        self.assertIsNotNone(event)
        self.assertEqual(WHEA_PROVIDER, event.provider)
        self.assertEqual(18, event.event_id)
        self.assertEqual("Cache Hierarchy Error", event.error_type)
        self.assertIn("CPU 致命硬件错误：Cache Hierarchy Error",
                      event.description)

    def test_kernel_power_line_uses_bugcheck_column(self):
        event = parse_powershell_line(self.KP_LINE)
        self.assertIsNotNone(event)
        self.assertEqual(41, event.event_id)
        self.assertEqual(0, event.bugcheck_code)
        self.assertIn("未正常关机", event.description)

    def test_bsod_bugcheck_is_reported(self):
        line = self.KP_LINE.replace("\t0\t", "\t292\t")
        event = parse_powershell_line(line)
        self.assertEqual(0x124, event.bugcheck_code)
        self.assertIn("0x124", event.description)

    def test_noise_lines_are_ignored(self):
        self.assertIsNone(parse_powershell_line(""))
        self.assertIsNone(parse_powershell_line("PS C:\\> something"))
        self.assertIsNone(parse_powershell_line(
            "2026-11-05 20:01:31\t19\tMicrosoft-Windows-WHEA-Logger\t0\tmsg"))


def _item_with_events(events, now=LOCAL):
    return crash_history_item(now=now, reader=lambda: events)


class CrashHistoryItemTests(unittest.TestCase):
    def test_no_events_reports_ok(self):
        item = _item_with_events([])
        self.assertEqual("ok", item["status"])
        self.assertIn("无硬件级意外重启", item["detail"])
        self.assertFalse(item["required"])

    def test_reader_unavailable_reports_unknown_not_ok(self):
        item = crash_history_item(reader=lambda: None)
        self.assertEqual("warn", item["status"])
        self.assertIn("无法", item["detail"])

    def test_reader_exception_never_escapes(self):
        def boom():
            raise OSError("no permission")
        item = crash_history_item(reader=boom)
        self.assertEqual("warn", item["status"])
        self.assertIn("无法", item["detail"])

    def test_reboots_inside_window_report_warn_with_count(self):
        events = [
            _event(LOCAL - timedelta(hours=41)),
            _event(LOCAL - timedelta(hours=42)),
            _event(LOCAL - timedelta(hours=43)),
        ]
        item = _item_with_events(events)
        self.assertEqual("warn", item["status"])
        self.assertIn("3 次", item["detail"])
        self.assertIn("未正常关机", item["detail"])
        # 处置建议给出：硬件归因 + Curve Optimizer 排查路径
        self.assertIn("Curve Optimizer", item["hint"])
        self.assertIn("脚本", item["hint"])

    def test_whea_cache_hierarchy_error_is_named(self):
        # PowerShell 路径拿到已解析的错误类型名
        events = [
            _event(LOCAL - timedelta(hours=5), provider=WHEA_PROVIDER,
                   event_id=18, error_type="Cache Hierarchy Error"),
            _event(LOCAL - timedelta(hours=5)),
        ]
        item = _item_with_events(events)
        self.assertEqual("warn", item["status"])
        self.assertIn("Cache Hierarchy Error", item["detail"])
        self.assertIn("CPU 致命硬件错误", item["detail"])

    def test_numeric_error_type_falls_back_to_code(self):
        # XML 兜底路径只有数字枚举时，detail 仍可读
        events = [
            _event(LOCAL - timedelta(hours=5), provider=WHEA_PROVIDER,
                   event_id=18, error_type="9"),
            _event(LOCAL - timedelta(hours=5)),
        ]
        item = _item_with_events(events)
        self.assertEqual("warn", item["status"])
        self.assertIn("错误类型码 9", item["detail"])

    def test_events_older_than_window_are_ignored(self):
        events = [
            _event(LOCAL - timedelta(hours=49)),
            _event(LOCAL - timedelta(days=10)),
        ]
        item = _item_with_events(events)
        self.assertEqual("ok", item["status"])

    def test_recent_cluster_gets_urgent_note(self):
        events = [
            _event(LOCAL - timedelta(minutes=10)),
            _event(LOCAL - timedelta(minutes=20)),
        ]
        item = _item_with_events(events)
        self.assertEqual("warn", item["status"])
        self.assertIn("机器尚未稳定", item["detail"])

    def test_bsod_style_reboot_also_counts(self):
        events = [_event(LOCAL - timedelta(hours=3), bugcheck=0x124)]
        item = _item_with_events(events)
        self.assertEqual("warn", item["status"])
        self.assertIn("0x124", item["detail"])

    def test_list_filters_and_sorts_newest_first(self):
        events = [
            _event(LOCAL - timedelta(hours=49)),
            _event(LOCAL - timedelta(hours=2)),
            _event(LOCAL - timedelta(hours=1)),
        ]
        result = list_fatal_reboot_events(now=LOCAL, reader=lambda: events)
        self.assertEqual(2, len(result))
        self.assertGreaterEqual(result[0].time, result[1].time)


class SummarizeTests(unittest.TestCase):
    def test_counts_reboots_not_paired_whea_duplicates(self):
        # 一次重启 = 1 条 41 + 1 条补写的 WHEA 18；应报 3 次而非 6 次
        t = LOCAL - timedelta(hours=40)
        events = []
        for i in range(3):
            events.append(_event(t - timedelta(minutes=10 * i)))
            events.append(_event(t - timedelta(minutes=10 * i) + timedelta(seconds=6),
                                 provider=WHEA_PROVIDER, event_id=18,
                                 error_type="Cache Hierarchy Error"))
        text = summarize_events(events, now=LOCAL)
        self.assertIn("3 次", text)
        self.assertNotIn("6 次", text)
        self.assertIn("Cache Hierarchy Error", text)


if __name__ == "__main__":
    unittest.main()
