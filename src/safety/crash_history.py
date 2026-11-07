# -*- coding: utf-8 -*-
"""硬件级意外重启检测：读系统事件日志里的 Kernel-Power 41 / WHEA-Logger 18。

背景（2026-11-05 本机实测案例）：晚上 19:53-20:01 机器连续 3 次瞬间断电重启，
每次都发生在「脚本启动自动化并拉起炉石」后 1~3 分钟。事后排查系统日志发现，
每次重启后都有 WHEA-Logger 18：``致命硬件错误 / Machine Check Exception /
Cache Hierarchy Error``（Ryzen 核心），且 Kernel-Power 41 的 BugcheckCode=0
（连蓝屏都没来得及发生）。结论：CPU 级机器校验错误导致的硬复位——脚本没有
任何会关机/重启系统的代码路径，**软件层不可能造成这种脏断电**；脚本只是每次
重启后被重新拉起的负载，属于触发时机上的巧合。

本模块把这类信号做成可查询的数据源，供环境自检显示，让「硬件坏了」与
「脚本有 bug」在用户面前可以被系统日志自证，避免再次误判。

关注的两类事件（都要求 Provider 匹配，避免误抓其它来源的同号事件）：

1. **Kernel-Power 41**：系统未先正常关机就重启（脏断电/硬复位/蓝屏都会记）。
   EventData 里 BugcheckCode=0 表示「死得太快、连蓝屏都没发生」= 典型硬件级断电；
   非 0 表示发生了蓝屏（也属于意外重启，一并计入）。
2. **WHEA-Logger 18**：致命硬件错误（Machine Check Exception 等）。EventData 里
   的 ErrorType 是**数字枚举**（实测 Cache Hierarchy Error = 9），人类可读的
   错误类型名在事件的消息模板里，只有现代事件 API（PowerShell Get-WinEvent）
   会替我们解析。所以默认读取走 PowerShell 拿已解析的文案；PowerShell 不可用
   时退回 win32evtlog 的 XML 读取（此时显示「错误类型码 N」）。

外部依赖完全可注入：测试传 ``reader`` 即可，不碰真实系统日志与 PowerShell。
"""
from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

KERNEL_POWER_PROVIDER = "Microsoft-Windows-Kernel-Power"
WHEA_PROVIDER = "Microsoft-Windows-WHEA-Logger"
KERNEL_POWER_EVENT_ID = 41
WHEA_FATAL_EVENT_ID = 18

# 默认回看窗口：环境自检报告"近 48h"，覆盖"昨天挂机今天排查"的场景。
DEFAULT_WINDOW = timedelta(hours=48)
# 连续重启判定：这么短的时间内 >=2 次硬重启，视为"机器尚未稳定"。
RECENT_CLUSTER_WINDOW = timedelta(minutes=30)
RECENT_CLUSTER_COUNT = 2
# 事件读取上限（XML 兜底路径）：XPath 新→旧读，防超长日志拖慢自检。
_MAX_EVENTS = 400
# PowerShell 查询超时（秒）：自检里后台跑，超时即放弃走 XML 兜底。
_POWERSHELL_TIMEOUT_SECONDS = 30.0

_EVENT_NS = "http://schemas.microsoft.com/win/2004/08/events/event"
# SystemTime 形如 2026-11-05T11:54:31.1234567Z（7 位小数）；fromisoformat 只吃 6 位。
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
# PowerShell 输出行：本地时间 \t 事件号 \t 来源 \t Bugcheck \t 消息（换行已折成 |）。
_POWERSHELL_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\t(\d+)\t([^\t]+)\t(\d+)\t(.*)$")
# 从已解析的消息里抠出错误类型（中文系统/英文系统两种模板都试）。
_ERROR_TYPE_MESSAGE_RE = re.compile(
    r"(?:错误类型|Error Type)\s*[：:.]\s*([^|\r\n]+?)\s*(?:\||$)",
    re.IGNORECASE)


@dataclass(frozen=True)
class FatalRebootEvent:
    """一条硬件级意外重启相关事件（时间为本地 naive 时间）。"""

    time: datetime
    provider: str
    event_id: int
    bugcheck_code: int = 0
    error_type: str = ""

    @property
    def is_kernel_power(self) -> bool:
        return self.provider == KERNEL_POWER_PROVIDER

    @property
    def description(self) -> str:
        if self.is_kernel_power:
            if self.bugcheck_code:
                return f"意外重启（蓝检 0x{self.bugcheck_code:X}）"
            return "意外重启（未正常关机即断电，未发生蓝屏）"
        if self.error_type:
            if self.error_type.isdigit():
                return f"CPU 致命硬件错误（错误类型码 {self.error_type}）"
            return f"CPU 致命硬件错误：{self.error_type}"
        return "CPU 致命硬件错误（机器校验异常）"


# ---------------------------------------------------------------- XML 解析
def _parse_system_time(raw: str) -> Optional[datetime]:
    """解析事件 XML 的 SystemTime（UTC，可能 7 位小数）→ 本地 naive 时间。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        text = _FRACTION_RE.sub(r"\1", text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().replace(tzinfo=None)


def parse_event_xml(xml: str) -> Optional[FatalRebootEvent]:
    """解析一条事件的 XML（EvtRenderEventXml 输出）；解析失败返回 None。

    注意：XML 的 EventData 里 WHEA ErrorType 是数字枚举（如 9），本路径只能
    透传数字，人类可读的错误类型名要靠 PowerShell 读取路径补全。
    """
    try:
        root = ET.fromstring(str(xml or ""))
    except ET.ParseError:
        return None
    ns = {"e": _EVENT_NS}
    system = root.find("e:System", ns)
    if system is None:
        return None
    provider_node = system.find("e:Provider", ns)
    provider = str(provider_node.get("Name") or "") if provider_node is not None else ""
    if provider not in (KERNEL_POWER_PROVIDER, WHEA_PROVIDER):
        return None
    id_node = system.find("e:EventID", ns)
    try:
        event_id = int(str(id_node.text or "").strip())
    except (ValueError, TypeError, AttributeError):
        return None
    if event_id not in (KERNEL_POWER_EVENT_ID, WHEA_FATAL_EVENT_ID):
        return None
    time_node = system.find("e:TimeCreated", ns)
    event_time = _parse_system_time(
        time_node.get("SystemTime") if time_node is not None else "")
    if event_time is None:
        return None

    data: dict = {}
    event_data = root.find("e:EventData", ns)
    if event_data is not None:
        for node in event_data.findall("e:Data", ns):
            name = node.get("Name")
            if name:
                data[str(name)] = str(node.text or "").strip()
    try:
        bugcheck = int(data.get("BugcheckCode") or 0)
    except ValueError:
        bugcheck = 0

    error_type = ""
    for key, value in data.items():
        if "errortype" in str(key).lower() and value:
            error_type = str(value).strip()
            break

    return FatalRebootEvent(
        time=event_time, provider=provider, event_id=event_id,
        bugcheck_code=bugcheck, error_type=error_type)


# ---------------------------------------------------------------- PowerShell 读取
def parse_powershell_line(line: str) -> Optional[FatalRebootEvent]:
    """解析 PowerShell 读取路径的一行输出；格式不符返回 None。"""
    match = _POWERSHELL_LINE_RE.match(str(line or "").strip())
    if not match:
        return None
    time_text, id_text, provider, bugcheck_text, message = match.groups()
    try:
        event_time = datetime.strptime(time_text, "%Y-%m-%d %H:%M:%S")
        event_id = int(id_text)
        bugcheck = int(bugcheck_text)
    except ValueError:
        return None
    if event_id not in (KERNEL_POWER_EVENT_ID, WHEA_FATAL_EVENT_ID):
        return None
    if provider not in (KERNEL_POWER_PROVIDER, WHEA_PROVIDER):
        return None
    error_type = ""
    if event_id == WHEA_FATAL_EVENT_ID:
        found = _ERROR_TYPE_MESSAGE_RE.search(message or "")
        if found:
            error_type = found.group(1).strip()
    return FatalRebootEvent(
        time=event_time, provider=provider, event_id=event_id,
        bugcheck_code=bugcheck, error_type=error_type)


def _powershell_reader(oldest: datetime) -> Optional[list]:
    """用 PowerShell Get-WinEvent 读事件（消息里的枚举已解析成可读文案）。

    返回事件列表（可能为空 = 读取成功但没有记录）；``None`` = PowerShell
    不可用/失败，调用方应退回 XML 兜底路径。
    """
    import shutil

    if shutil.which("powershell") is None:
        return None
    start_text = oldest.strftime("%Y-%m-%d %H:%M:%S")
    # 行内用 [char]9（TAB）拼接：反引号转义在 PS 单引号串里不生效，别用 `t。
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$evts = Get-WinEvent -FilterHashtable @{LogName='System';"
        "ProviderName=@('Microsoft-Windows-Kernel-Power',"
        "'Microsoft-Windows-WHEA-Logger');Id=@(41,18);"
        f"StartTime='{start_text}'}} -ErrorAction Stop;"
        "foreach ($e in $evts) {"
        "$xml=[xml]$e.ToXml();$bc='0';"
        "foreach ($d in $xml.Event.EventData.Data) {"
        "if ($d.Name -eq 'BugcheckCode') {$bc=[string]$d.'#text'}}"
        "$msg=($e.Message -replace \"\\r?\\n\", ' | ');"
        "$fields=@($e.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'),"
        "[string]$e.Id,[string]$e.ProviderName,$bc,$msg);"
        "Write-Output ($fields -join [char]9)}")
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, timeout=_POWERSHELL_TIMEOUT_SECONDS)
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    try:
        text = completed.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None

    events: list = []
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in raw_lines:
        parsed = parse_powershell_line(line)
        if parsed is not None:
            events.append(parsed)
    if not events and raw_lines:
        # stdout 有内容但一行都解析不出 = 输出格式漂移，宁可退回兜底路径，
        # 也不要把"没解析到"误当成"没有硬件级重启"。
        return None
    return events


# ---------------------------------------------------------------- XML 兜底读取
def _win32_event_reader() -> Optional[list]:
    """win32evtlog XML 读取兜底；环境不可用时返回 None 表示"无法检测"。"""
    try:
        import win32evtlog
    except Exception:
        return None
    events: list = []
    query_xpath = (
        "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power' or "
        "@Name='Microsoft-Windows-WHEA-Logger'] "
        "and (EventID=41 or EventID=18)]]")
    try:
        query = win32evtlog.EvtQuery(
            "System",
            win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
            query_xpath)
        while len(events) < _MAX_EVENTS:
            batch = win32evtlog.EvtNext(query, 50)
            if not batch:
                break
            for handle in batch:
                try:
                    xml = win32evtlog.EvtRender(
                        handle, win32evtlog.EvtRenderEventXml)
                except Exception:
                    continue
                parsed = parse_event_xml(xml)
                if parsed is not None:
                    events.append(parsed)
    except Exception:
        # 读到一半失败也要把已读到的用上（partial > 全丢）。
        return events
    return events


def _default_reader(oldest: datetime) -> Optional[list]:
    """默认读取：PowerShell 优先（文案可读），失败退回 win32evtlog XML。"""
    result = _powershell_reader(oldest)
    if result is not None:
        return result
    return _win32_event_reader()


def list_fatal_reboot_events(
        window: timedelta = DEFAULT_WINDOW,
        now: Optional[datetime] = None,
        reader: Optional[Callable[[], Optional[Iterable]]] = None,
) -> Optional[list]:
    """窗口内（默认近 48h）的硬件级意外重启事件，新→旧排序。

    返回 ``None`` = 无法检测（非 Windows / 读不了日志）；返回列表（可能为空）
    = 检测成功。``reader`` 为零参可调用（测试注入用）；默认读取会带上窗口
    起点（读不到窗口外的事件就早停）。
    """
    now = now or datetime.now()
    if reader is None:
        oldest = now - window
        reader = lambda: _default_reader(oldest)  # noqa: E731
    try:
        raw = reader()
    except Exception:
        return None
    if raw is None:
        return None
    events = [e for e in raw if isinstance(e, FatalRebootEvent)]
    events.sort(key=lambda e: e.time, reverse=True)
    oldest = now - window
    return [e for e in events if oldest <= e.time <= now]


def _format_error_types(whea_events: list) -> str:
    """把 WHEA 事件的错误类型值渲染成可读文案（数字枚举标注为类型码）。"""
    types = sorted({e.error_type for e in whea_events if e.error_type})
    return "、".join(
        f"错误类型码 {t}" if t.isdigit() else t for t in types)


def summarize_events(events: list, window: timedelta = DEFAULT_WINDOW,
                     now: Optional[datetime] = None) -> str:
    """把事件列表压成一句人话（供自检 detail 用）。"""
    reboots = [e for e in events if e.is_kernel_power]
    whea = [e for e in events if not e.is_kernel_power]
    now = now or datetime.now()
    hours = max(1, int(window.total_seconds() // 3600))
    count = len(reboots) or len(whea)
    latest = events[0].time
    parts = [f"近 {hours}h 检测到 {count} 次硬件级意外重启，最近 "
             f"{latest:%m-%d %H:%M}"]
    if whea:
        types = _format_error_types(whea)
        detail = f"伴随 CPU 致命硬件错误（{types}）" if types \
            else "伴随 CPU 致命硬件错误（机器校验异常）"
        parts.append(detail)
    else:
        parts.append(reboots[0].description if reboots else "")
    cluster = [e for e in events
               if e.time >= now - RECENT_CLUSTER_WINDOW]
    if len(cluster) >= RECENT_CLUSTER_COUNT:
        parts.append("30 分钟内连续多次——机器尚未稳定，请先排查硬件再挂机")
    return "；".join(p for p in parts if p)


# 自检建议（发现硬件级重启时的处置指引）。
REBOOT_HINT = (
    "这是 CPU/主板/供电级硬件信号：脚本没有任何关机/重启系统的代码路径，"
    "不可能在软件层造成这种断电。建议按顺序排查：① 若为 AMD 平台，BIOS 里"
    "回退 Curve Optimizer 负压或关闭 PBO；② 更新主板 BIOS；③ 检查电源功率/"
    "供电接口与 CPU 散热（清灰换硅脂）；④ MemTest86 验证内存。确认稳定后再"
    "使用无人值守挂机。")


def crash_history_item(
        window: timedelta = DEFAULT_WINDOW,
        now: Optional[datetime] = None,
        reader: Optional[Callable[[], Optional[Iterable]]] = None,
) -> dict:
    """环境自检项：近期硬件级意外重启（供 selfcheck.environment_items 使用）。

    任何情况下都不抛异常、不阻塞启动；required=False（提示性质，不算失败）。
    """
    # 延迟 import，避免 selfcheck 在无 src 包的环境下不可用。
    from selfcheck import STATUS_OK, STATUS_WARN, _item

    purpose = ("整机稳定性排查：区分硬件级断电与脚本问题"
               "（脚本无软件层关机路径）")
    label = f"硬件级意外重启（近 {max(1, int(window.total_seconds() // 3600))}h）"
    events = list_fatal_reboot_events(window=window, now=now, reader=reader)
    if events is None:
        return _item("crash-history", label, STATUS_WARN,
                     "无法读取系统事件日志，跳过检查", "", False, purpose)
    if not events:
        return _item("crash-history", label, STATUS_OK,
                     "无硬件级意外重启记录", "", False, purpose)
    return _item("crash-history", label, STATUS_WARN,
                 summarize_events(events, window=window, now=now),
                 REBOOT_HINT, False, purpose)
