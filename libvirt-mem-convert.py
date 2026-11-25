#!/usr/bin/env python3

import argparse
import difflib
import io
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple, Union

DEFAULT_BASE_KIB = 2097152  # 2 GiB
BLOCK_KIB = 2048            # 2 MiB

ANSI_RESET = '\033[0m'
ANSI_BOLD = '\033[1m'
ANSI_RED = '\033[31m'
ANSI_GREEN = '\033[32m'
ANSI_CYAN = '\033[36m'
ANSI_BOLD_BLUE = '\033[1;34m'
MAX_ALIGNMENT_PAIRS = 2500

def supports_color(color_mode: str = 'auto') -> bool:
    if color_mode == 'always':
        return True
    if color_mode == 'never':
        return False
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'

def _highlight_segment(text: str, base_color: str, is_changed: bool) -> str:
    if not text:
        return ''
    nl = ''
    if text.endswith('\n'):
        text = text[:-1]
        nl = '\n'
    if not text:
        return nl
    if is_changed:
        return f'{ANSI_BOLD_BLUE}{text}{ANSI_RESET}{base_color}{nl}'
    return f'{text}{nl}'

def format_inline_diff(old_line: str, new_line: str) -> Tuple[str, str]:
    old_content = old_line[1:]
    new_content = new_line[1:]
    matcher = difflib.SequenceMatcher(None, old_content, new_content, autojunk=False)
    if matcher.ratio() < 0.45:
        return f'{ANSI_RED}{old_line}{ANSI_RESET}', f'{ANSI_GREEN}{new_line}{ANSI_RESET}'
    old_parts = [f'{ANSI_RED}-']
    new_parts = [f'{ANSI_GREEN}+']
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            old_parts.append(_highlight_segment(old_content[i1:i2], ANSI_RED, False))
            new_parts.append(_highlight_segment(new_content[j1:j2], ANSI_GREEN, False))
        elif tag == 'replace':
            old_parts.append(_highlight_segment(old_content[i1:i2], ANSI_RED, True))
            new_parts.append(_highlight_segment(new_content[j1:j2], ANSI_GREEN, True))
        elif tag == 'delete':
            old_parts.append(_highlight_segment(old_content[i1:i2], ANSI_RED, True))
        elif tag == 'insert':
            new_parts.append(_highlight_segment(new_content[j1:j2], ANSI_GREEN, True))
    old_parts.append(ANSI_RESET)
    new_parts.append(ANSI_RESET)
    return ''.join(old_parts), ''.join(new_parts)

def align_hunk_lines(minus_lines: List[str], plus_lines: List[str]) -> Tuple[List[str], List[str]]:
    n = len(minus_lines)
    m = len(plus_lines)
    if n * m > MAX_ALIGNMENT_PAIRS:
        return (
            [f'{ANSI_RED}{line}{ANSI_RESET}' for line in minus_lines],
            [f'{ANSI_GREEN}{line}{ANSI_RESET}' for line in plus_lines],
        )
    sim = [[0.0] * m for _ in range(n)]
    for i in range(n):
        c1 = minus_lines[i][1:].strip()
        for j in range(m):
            c2 = plus_lines[j][1:].strip()
            if c1 == c2:
                sim[i][j] = 1.0
            elif c1 or c2:
                sim[i][j] = difflib.SequenceMatcher(None, c1, c2, autojunk=False).ratio()
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    choice = [[0] * (m + 1) for _ in range(n + 1)]  # 1: match (i,j), 2: skip i, 3: skip j
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            score_match = (sim[i][j] if sim[i][j] >= 0.45 else -1.0) + dp[i + 1][j + 1]
            score_skip_i = dp[i + 1][j]
            score_skip_j = dp[i][j + 1]
            if sim[i][j] >= 0.45 and score_match >= score_skip_i and score_match >= score_skip_j:
                dp[i][j] = score_match
                choice[i][j] = 1
            elif score_skip_i >= score_skip_j:
                dp[i][j] = score_skip_i
                choice[i][j] = 2
            else:
                dp[i][j] = score_skip_j
                choice[i][j] = 3
    colored_minus = [f'{ANSI_RED}{line}{ANSI_RESET}' for line in minus_lines]
    colored_plus = [f'{ANSI_GREEN}{line}{ANSI_RESET}' for line in plus_lines]
    i, j = 0, 0
    while i < n and j < m:
        if choice[i][j] == 1:
            colored_minus[i], colored_plus[j] = format_inline_diff(minus_lines[i], plus_lines[j])
            i += 1
            j += 1
        elif choice[i][j] == 2:
            i += 1
        else:
            j += 1
    return colored_minus, colored_plus

def format_diff_lines(diff_lines: List[str], use_color: bool) -> List[str]:
    if not use_color:
        return diff_lines
    result = []
    i = 0
    n = len(diff_lines)
    while i < n:
        line = diff_lines[i]
        if line.startswith('--- ') or line.startswith('+++ '):
            result.append(f'{ANSI_BOLD}{line}{ANSI_RESET}')
            i += 1
        elif line.startswith('@@ '):
            result.append(f'{ANSI_CYAN}{line}{ANSI_RESET}')
            i += 1
        elif line.startswith('-'):
            minus_block = []
            while i < n and diff_lines[i].startswith('-'):
                minus_block.append(diff_lines[i])
                i += 1
            plus_block = []
            while i < n and diff_lines[i].startswith('+'):
                plus_block.append(diff_lines[i])
                i += 1
            c_minus, c_plus = align_hunk_lines(minus_block, plus_block)
            result.extend(c_minus)
            result.extend(c_plus)
        elif line.startswith('+'):
            result.append(f'{ANSI_GREEN}{line}{ANSI_RESET}')
            i += 1
        else:
            result.append(line)
            i += 1
    return result

def parse_memory_size(size_str: str) -> int:
    s = size_str.strip()
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([a-zA-Z]*)', s)
    if not match:
        raise ValueError(f"Invalid memory size: '{size_str}'. Valid examples: 1G, 1536M, 2GiB, 1048576.")
    val = float(match.group(1))
    unit = match.group(2) or 'KiB'
    return to_kib(val, unit)

def to_kib(value: Optional[Union[str, int, float]], unit: Optional[str]) -> int:
    if value is None:
        raise ValueError('Memory value is missing.')
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError('Memory value cannot be empty.')
    try:
        val = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'Invalid memory value: {value!r}.') from None
    if not math.isfinite(val):
        raise ValueError(f'Invalid memory value: {value!r}.')
    u = (unit or 'KiB').lower()
    multipliers = {
        'b': 1 / 1024, 'bytes': 1 / 1024,
        'k': 1, 'kib': 1, 'kb': 1000 / 1024,
        'm': 1024, 'mib': 1024, 'mb': (1000**2) / 1024,
        'g': 1024**2, 'gib': 1024**2, 'gb': (1000**3) / 1024,
        't': 1024**3, 'tib': 1024**3,
    }
    if u not in multipliers:
        raise ValueError(f'Unsupported memory unit: {unit}')
    return int(val * multipliers[u])

def align_down(val: int, step: int) -> int:
    return (val // step) * step

def add_child_formatted(
    parent: ET.Element,
    tag: str,
    attrib: Optional[Dict[str, str]] = None,
    text: Optional[str] = None,
    indent_level: int = 1,
    insert_before: Optional[ET.Element] = None,
) -> ET.Element:
    el = ET.Element(tag, attrib or {})
    if text is not None:
        el.text = text
    child_indent = '\n' + ('  ' * indent_level)
    parent_indent = '\n' + ('  ' * (indent_level - 1))
    if insert_before is not None:
        idx = list(parent).index(insert_before)
        el.tail = child_indent
        parent.insert(idx, el)
    else:
        if len(parent) == 0:
            parent.text = child_indent
        else:
            parent[-1].tail = child_indent
        el.tail = parent_indent
        parent.append(el)
    return el

def remove_child_formatted(parent: ET.Element, child: ET.Element, indent_level: int = 1):
    children = list(parent)
    idx = children.index(child)
    parent_indent = '\n' + ('  ' * (indent_level - 1))
    if idx > 0:
        prev_el = children[idx - 1]
        prev_el.tail = child.tail if idx == len(children) - 1 else prev_el.tail
    elif len(children) == 1:
        parent.text = None
    parent.remove(child)
    if len(parent) == 0:
        parent.text = None
    else:
        parent[-1].tail = parent_indent

def format_vcpu_set(root: ET.Element) -> str:
    vcpu_el = root.find('vcpu')
    count_text = (vcpu_el.text or '').strip() if vcpu_el is not None else ''
    try:
        vcpu_count = int(count_text) if count_text else 1
    except ValueError:
        raise ValueError(f"Invalid <vcpu> count: {count_text!r}.") from None
    if vcpu_count < 1:
        raise ValueError('The <vcpu> count must be at least 1.')
    disabled_ids = set()
    vcpus_el = root.find('vcpus')
    if vcpus_el is not None:
        for vcpu in vcpus_el.findall('vcpu'):
            if vcpu.get('enabled') != 'no':
                continue
            vcpu_id = vcpu.get('id')
            try:
                vcpu_id = int(vcpu_id)
            except (TypeError, ValueError):
                raise ValueError("A disabled <vcpus><vcpu> entry must have a valid 'id'.") from None
            if vcpu_id < 0 or vcpu_id >= vcpu_count:
                raise ValueError(f"Disabled vCPU id {vcpu_id} is outside the configured <vcpu> range.")
            disabled_ids.add(vcpu_id)
    enabled_ids = [vcpu_id for vcpu_id in range(vcpu_count) if vcpu_id not in disabled_ids]
    if not enabled_ids:
        raise ValueError('At least one vCPU must be enabled.')
    ranges = []
    start = previous = enabled_ids[0]
    for vcpu_id in enabled_ids[1:]:
        if vcpu_id == previous + 1:
            previous = vcpu_id
            continue
        ranges.append(str(start) if start == previous else f'{start}-{previous}')
        start = previous = vcpu_id
    ranges.append(str(start) if start == previous else f'{start}-{previous}')
    return ','.join(ranges)

def ensure_balloon_stats(balloon_el: ET.Element, default_period: str = '5'):
    stats_el = balloon_el.find('stats')
    if stats_el is None:
        add_child_formatted(balloon_el, 'stats', attrib={'period': default_period}, indent_level=3)
    elif not stats_el.get('period'):
        stats_el.set('period', default_period)

def has_virtiomem(devices_el: Optional[ET.Element]) -> bool:
    if devices_el is None:
        return False
    return any(dev.get('model') == 'virtio-mem' for dev in devices_el.findall('memory'))

def get_virtiomem_base_kib(root: ET.Element) -> int:
    mem_el = root.find('memory')
    if mem_el is None:
        raise ValueError('The <memory> element is missing from the XML file.')
    numa_cells = root.findall('cpu/numa/cell')
    if numa_cells:
        return sum(to_kib(cell.get('memory'), cell.get('unit')) for cell in numa_cells)
    return to_kib(mem_el.text, mem_el.get('unit'))

def configure_virtiomem_balloon(devices_el: ET.Element, keep_balloon: bool) -> None:
    balloon_el = devices_el.find('memballoon')
    if balloon_el is None:
        model = 'virtio' if keep_balloon else 'none'
        balloon_el = add_child_formatted(
            devices_el, 'memballoon', attrib={'model': model}, indent_level=2
        )
    elif not keep_balloon:
        balloon_el.set('model', 'none')
    if balloon_el.get('model') == 'none':
        balloon_el.attrib.pop('autodeflate', None)
        stats_el = balloon_el.find('stats')
        if stats_el is not None:
            remove_child_formatted(balloon_el, stats_el, indent_level=3)
    else:
        balloon_el.attrib.pop('autodeflate', None)
        ensure_balloon_stats(balloon_el, default_period='5')

def convert_to_virtiomem(
    root: ET.Element,
    base_kib: int = DEFAULT_BASE_KIB,
    keep_balloon: bool = False,
    initial_kib: Optional[int] = None,
    max_override_kib: Optional[int] = None,
):
    devices_el = root.find('devices')
    if has_virtiomem(devices_el):
        if max_override_kib is not None:
            raise ValueError('--max-memory requires a domain without a virtio-mem device in to-virtiomem.')
        if initial_kib is not None:
            raise ValueError('--initial-memory requires a domain without a virtio-mem device.')
        base_aligned = align_down(base_kib, BLOCK_KIB)
        if base_aligned <= 0:
            raise ValueError('Base memory must be at least 2 MiB (2048 KiB).')
        if get_virtiomem_base_kib(root) != base_aligned:
            correct_memory_values(root, base_kib=base_kib)
        if devices_el is None:
            devices_el = add_child_formatted(root, 'devices', indent_level=1)
        configure_virtiomem_balloon(devices_el, keep_balloon)
        return
    mem_el = root.find('memory')
    cur_el = root.find('currentMemory')
    if mem_el is None:
        raise ValueError('The <memory> element is missing from the XML file.')
    base_aligned = align_down(base_kib, BLOCK_KIB)
    if base_aligned <= 0:
        raise ValueError('Base memory must be at least 2 MiB (2048 KiB).')
    max_kib = max_override_kib if max_override_kib is not None else to_kib(mem_el.text, mem_el.get('unit'))
    if max_kib <= base_aligned:
        raise ValueError(
            f'Maximum VM memory ({max_kib} KiB) must be greater than base memory ({base_aligned} KiB).'
        )
    if initial_kib is not None:
        if not base_aligned <= initial_kib <= max_kib:
            raise ValueError(
                f'Initial memory must be between base memory ({base_aligned} KiB) '
                f'and maximum VM memory ({max_kib} KiB).'
            )
        start_kib = initial_kib
    else:
        start_kib = to_kib(cur_el.text, cur_el.get('unit')) if cur_el is not None else max_kib
        if max_override_kib is not None and start_kib > max_kib:
            raise ValueError('Existing <currentMemory> exceeds --max-memory; specify --initial-memory.')
    vmem_size_kib = align_down(max_kib - base_aligned, BLOCK_KIB)
    if max_override_kib is not None and vmem_size_kib == 0:
        raise ValueError('--max-memory must leave at least one 2 MiB block above the fixed base.')
    vmem_req_kib = align_down(max(0, start_kib - base_aligned), BLOCK_KIB)
    if initial_kib is not None:
        effective_initial_kib = base_aligned + vmem_req_kib
        if effective_initial_kib != initial_kib:
            print(
                f'Warning: --initial-memory rounded down from {initial_kib} KiB to '
                f'{effective_initial_kib} KiB to align with 2 MiB blocks.',
                file=sys.stderr,
            )
    total_max_kib = base_aligned + vmem_size_kib
    max_mem_el = root.find('maxMemory')
    if max_mem_el is None:
        add_child_formatted(
            root, 'maxMemory',
            attrib={'slots': '16', 'unit': 'KiB'},
            text=str(total_max_kib),
            indent_level=1,
            insert_before=mem_el,
        )
    else:
        max_mem_el.set('slots', max_mem_el.get('slots', '16'))
        max_mem_el.set('unit', 'KiB')
        max_mem_el.text = str(total_max_kib)
    mem_el.set('unit', 'KiB')
    mem_el.text = str(base_aligned)
    if cur_el is None:
        cur_el = add_child_formatted(root, 'currentMemory', attrib={'unit': 'KiB'}, text=str(base_aligned), indent_level=1)
    else:
        cur_el.set('unit', 'KiB')
        cur_el.text = str(base_aligned)
    cpu_range = format_vcpu_set(root)
    cpu_el = root.find('cpu')
    if cpu_el is None:
        cpu_el = add_child_formatted(root, 'cpu', attrib={'mode': 'host-passthrough'}, indent_level=1)
    numa_el = cpu_el.find('numa')
    if numa_el is None:
        numa_el = add_child_formatted(cpu_el, 'numa', indent_level=2)
    cells = numa_el.findall('cell')
    if not cells:
        add_child_formatted(
            numa_el, 'cell',
            attrib={'id': '0', 'cpus': cpu_range, 'memory': str(base_aligned), 'unit': 'KiB'},
            indent_level=3,
        )
    else:
        resize_first_numa_cell(cells, base_aligned)
    if devices_el is None:
        devices_el = add_child_formatted(root, 'devices', indent_level=1)
    vmem_dev = add_child_formatted(devices_el, 'memory', attrib={'model': 'virtio-mem'}, indent_level=2)
    target_el = add_child_formatted(vmem_dev, 'target', indent_level=3)
    add_child_formatted(target_el, 'size', attrib={'unit': 'KiB'}, text=str(vmem_size_kib), indent_level=4)
    add_child_formatted(target_el, 'node', text='0', indent_level=4)
    add_child_formatted(target_el, 'block', attrib={'unit': 'KiB'}, text=str(BLOCK_KIB), indent_level=4)
    add_child_formatted(target_el, 'requested', attrib={'unit': 'KiB'}, text=str(vmem_req_kib), indent_level=4)
    configure_virtiomem_balloon(devices_el, keep_balloon)

def set_balloon_current_memory(
    root: ET.Element, current_kib: int, max_kib: Optional[int] = None
) -> None:
    if max_kib is not None and not 0 < current_kib <= max_kib:
        raise ValueError(f'Initial memory must be positive and no greater than {max_kib} KiB.')
    cur_el = root.find('currentMemory')
    if cur_el is None:
        add_child_formatted(
            root, 'currentMemory', attrib={'unit': 'KiB'}, text=str(current_kib), indent_level=1
        )
    else:
        cur_el.set('unit', 'KiB')
        cur_el.text = str(current_kib)

def convert_to_balloon(
    root: ET.Element,
    initial_kib: Optional[int] = None,
    max_override_kib: Optional[int] = None,
):
    devices_el = root.find('devices')
    mem_el = root.find('memory')
    if max_override_kib is not None and max_override_kib <= 0:
        raise ValueError('Maximum memory must be positive.')
    if not has_virtiomem(devices_el):
        if initial_kib is not None or max_override_kib is not None:
            if mem_el is None:
                raise ValueError('The <memory> element is missing from the XML file.')
            if max_override_kib is not None:
                cur_el = root.find('currentMemory')
                if initial_kib is None and cur_el is not None:
                    if to_kib(cur_el.text, cur_el.get('unit')) > max_override_kib:
                        raise ValueError('Existing <currentMemory> exceeds --max-memory; specify --initial-memory.')
                cells = root.findall('cpu/numa/cell')
                if cells:
                    resize_first_numa_cell(cells, max_override_kib)
                mem_el.set('unit', 'KiB')
                mem_el.text = str(max_override_kib)
            if initial_kib is not None:
                set_balloon_current_memory(root, initial_kib, to_kib(mem_el.text, mem_el.get('unit')))
        if devices_el is None:
            devices_el = add_child_formatted(root, 'devices', indent_level=1)
        balloon_el = devices_el.find('memballoon')
        if balloon_el is None:
            balloon_el = add_child_formatted(devices_el, 'memballoon', attrib={'model': 'virtio'}, indent_level=2)
        elif balloon_el.get('model') == 'none':
            balloon_el.set('model', 'virtio')
        balloon_el.set('autodeflate', 'on')
        ensure_balloon_stats(balloon_el, default_period='5')
        return
    max_mem_el = root.find('maxMemory')
    if mem_el is None:
        raise ValueError('The <memory> element is missing from the XML file.')
    numa_el = root.find('cpu/numa')
    cells = numa_el.findall('cell') if numa_el is not None else []
    cell_memory_kib = [to_kib(cell.get('memory'), cell.get('unit')) for cell in cells]
    base_kib = sum(cell_memory_kib) if cells else to_kib(mem_el.text, mem_el.get('unit'))
    vmem_size_kib = 0
    vmem_req_kib = 0
    for dev in list(devices_el.findall('memory')):
        if dev.get('model') == 'virtio-mem':
            target_el = dev.find('target')
            if target_el is not None:
                s_el = target_el.find('size')
                r_el = target_el.find('requested')
                if max_override_kib is None and s_el is not None and s_el.text:
                    vmem_size_kib += to_kib(s_el.text, s_el.get('unit'))
                if initial_kib is None and r_el is not None and r_el.text:
                    vmem_req_kib += to_kib(r_el.text, r_el.get('unit'))
            remove_child_formatted(devices_el, dev, indent_level=2)
    if max_override_kib is not None:
        max_kib = max_override_kib
    elif vmem_size_kib > 0:
        max_kib = base_kib + vmem_size_kib
    elif max_mem_el is not None and max_mem_el.text:
        max_kib = to_kib(max_mem_el.text, max_mem_el.get('unit'))
    else:
        max_kib = base_kib
    if initial_kib is None:
        current_kib = base_kib + vmem_req_kib
        if max_override_kib is not None and current_kib > max_kib:
            raise ValueError('Calculated <currentMemory> exceeds --max-memory; specify --initial-memory.')
        set_balloon_current_memory(root, current_kib)
    else:
        set_balloon_current_memory(root, initial_kib, max_kib)
    mem_el.set('unit', 'KiB')
    mem_el.text = str(max_kib)
    if max_mem_el is not None:
        remove_child_formatted(root, max_mem_el, indent_level=1)
    cpu_el = root.find('cpu')
    if cpu_el is not None and numa_el is not None:
        if len(cells) <= 1:
            remove_child_formatted(cpu_el, numa_el, indent_level=2)
        else:
            resize_first_numa_cell(cells, max_kib)
    balloon_el = devices_el.find('memballoon')
    if balloon_el is None:
        balloon_el = add_child_formatted(devices_el, 'memballoon', attrib={'model': 'virtio'}, indent_level=2)
    elif balloon_el.get('model') == 'none':
        balloon_el.set('model', 'virtio')
    balloon_el.set('autodeflate', 'on')
    ensure_balloon_stats(balloon_el, default_period='5')

def resize_first_numa_cell(cells: List[ET.Element], total_kib: int) -> None:
    remaining_kib = total_kib - sum(to_kib(cell.get('memory'), cell.get('unit')) for cell in cells[1:])
    if remaining_kib <= 0:
        raise ValueError(f'The {total_kib} KiB memory allocation leaves no room for the first NUMA cell.')
    cells[0].set('memory', str(remaining_kib))
    cells[0].set('unit', 'KiB')

def correct_memory_values(
    root: ET.Element,
    base_kib: Optional[int] = None,
    initial_kib: Optional[int] = None,
    max_override_kib: Optional[int] = None,
) -> None:
    devices_el = root.find('devices')
    with_virtiomem = has_virtiomem(devices_el)
    if with_virtiomem and base_kib is None and get_virtiomem_base_kib(root) != DEFAULT_BASE_KIB:
        base_kib = DEFAULT_BASE_KIB
    if base_kib is None and initial_kib is None and max_override_kib is None:
        return
    mem_el = root.find('memory')
    if mem_el is None:
        raise ValueError('The <memory> element is missing from the XML file.')

    if not with_virtiomem:
        if base_kib is not None:
            raise ValueError('--base-memory only applies to a domain using virtio-mem when no conversion mode is specified.')
        if max_override_kib is not None and max_override_kib <= 0:
            raise ValueError('Maximum memory must be positive.')
        current_max_kib = to_kib(mem_el.text, mem_el.get('unit'))
        new_max_kib = max_override_kib if max_override_kib is not None else current_max_kib
        current_el = root.find('currentMemory')
        current_kib = to_kib(current_el.text, current_el.get('unit')) if current_el is not None else current_max_kib
        if initial_kib is None and current_kib > new_max_kib:
            raise ValueError('Existing <currentMemory> exceeds --max-memory; specify --initial-memory.')
        if max_override_kib is not None:
            cells = root.findall('cpu/numa/cell')
            if cells:
                resize_first_numa_cell(cells, new_max_kib)
            mem_el.set('unit', 'KiB')
            mem_el.text = str(new_max_kib)
        if initial_kib is not None:
            set_balloon_current_memory(root, initial_kib, new_max_kib)
        return

    vmem_devices = [
        dev for dev in devices_el.findall('memory') if dev.get('model') == 'virtio-mem'
    ]
    if len(vmem_devices) != 1:
        raise ValueError('Memory-only correction requires exactly one virtio-mem device.')

    numa_cells = root.findall('cpu/numa/cell')
    old_memory_kib = to_kib(mem_el.text, mem_el.get('unit'))
    old_base_kib = get_virtiomem_base_kib(root)
    max_mem_el = root.find('maxMemory')
    dev = vmem_devices[0]
    target_el = dev.find('target')
    size_el = target_el.find('size') if target_el is not None else None
    requested_el = target_el.find('requested') if target_el is not None else None
    if size_el is None or requested_el is None:
        raise ValueError('The virtio-mem target must contain <size> and <requested> elements.')
    block_el = target_el.find('block')
    block_kib = to_kib(block_el.text, block_el.get('unit')) if block_el is not None else BLOCK_KIB
    if block_kib <= 0:
        raise ValueError('The virtio-mem block size must be positive.')

    old_size_kib = to_kib(size_el.text, size_el.get('unit'))
    old_requested_kib = to_kib(requested_el.text, requested_el.get('unit'))
    old_max_kib = (
        to_kib(max_mem_el.text, max_mem_el.get('unit'))
        if max_mem_el is not None and max_mem_el.text
        else old_base_kib + old_size_kib
    )
    memory_tracks_base = old_memory_kib == old_base_kib
    memory_tracks_max = old_memory_kib == old_max_kib
    old_current_kib = old_base_kib + old_requested_kib
    new_base_kib = align_down(base_kib, BLOCK_KIB) if base_kib is not None else old_base_kib
    if new_base_kib <= 0:
        raise ValueError('Base memory must be at least 2 MiB (2048 KiB).')
    requested_max_kib = max_override_kib if max_override_kib is not None else old_max_kib
    if requested_max_kib <= new_base_kib:
        raise ValueError(
            f'Maximum VM memory ({requested_max_kib} KiB) must be greater than base memory ({new_base_kib} KiB).'
        )
    new_size_kib = align_down(requested_max_kib - new_base_kib, block_kib)
    if new_size_kib <= 0:
        raise ValueError(f'Maximum memory must leave at least one {block_kib} KiB block above the fixed base.')
    new_max_kib = new_base_kib + new_size_kib

    current_total_kib = (
        initial_kib if initial_kib is not None else max(new_base_kib, old_current_kib)
    )
    if not new_base_kib <= current_total_kib <= new_max_kib:
        raise ValueError(
            f'Initial memory must be between base memory ({new_base_kib} KiB) '
            f'and maximum VM memory ({new_max_kib} KiB).'
        )
    requested_total_kib = align_down(current_total_kib - new_base_kib, block_kib)
    if initial_kib is not None:
        effective_initial_kib = new_base_kib + requested_total_kib
        if effective_initial_kib != initial_kib:
            print(
                f'Warning: --initial-memory rounded down from {initial_kib} KiB to '
                f'{effective_initial_kib} KiB to align with {block_kib} KiB blocks.',
                file=sys.stderr,
            )

    current_el = root.find('currentMemory')
    if current_el is None:
        add_child_formatted(
            root, 'currentMemory', attrib={'unit': 'KiB'}, text=str(new_base_kib), indent_level=1
        )
    else:
        current_el.set('unit', 'KiB')
        current_el.text = str(new_base_kib)
    if base_kib is not None and memory_tracks_base:
        mem_el.set('unit', 'KiB')
        mem_el.text = str(new_base_kib)
    if base_kib is not None and numa_cells:
        resize_first_numa_cell(numa_cells, new_base_kib)
    if base_kib is not None or max_override_kib is not None:
        size_el.set('unit', 'KiB')
        size_el.text = str(new_size_kib)
        if max_override_kib is not None:
            if memory_tracks_max:
                mem_el.set('unit', 'KiB')
                mem_el.text = str(new_max_kib)
        if max_mem_el is not None and max_override_kib is not None:
            max_mem_el.set('unit', 'KiB')
            max_mem_el.text = str(new_max_kib)
    requested_el.set('unit', 'KiB')
    requested_el.text = str(requested_total_kib)

class DomainTreeBuilder(ET.TreeBuilder):
    # Python 3.7 has no TreeBuilder insert_comments or insert_pis options.
    _depth = 0

    def start(self, tag, attrs):
        self._depth += 1
        return super().start(tag, attrs)

    def end(self, tag):
        self._depth -= 1
        return super().end(tag)

    def comment(self, text):
        if self._depth:
            super().start(ET.Comment, {})
            self.data(text)
            super().end(ET.Comment)

    def pi(self, target, text):
        if self._depth:
            super().start(ET.ProcessingInstruction, {})
            self.data(f'{target} {text}' if text else target)
            super().end(ET.ProcessingInstruction)

def format_libvirt_style(xml_str: str) -> str:
    def format_tag(match):
        tag = match.group()
        if tag.startswith(('<!--', '<?')):
            return tag
        tag = re.sub(r'"([^"]*)"', lambda attr: "'" + attr[1].replace("'", '&apos;') + "'", tag)
        return tag[:-3] + '/>' if tag.endswith(' />') else tag

    result = re.sub(r'<!--.*?-->|<\?.*?\?>|<[^<>]*>', format_tag, xml_str, flags=re.DOTALL)
    return result if result.endswith('\n') else result + '\n'

def transform_xml_string(
    xml_content: str,
    mode: Optional[str],
    base_kib: Optional[int],
    keep_balloon: bool = False,
    initial_kib: Optional[int] = None,
    max_override_kib: Optional[int] = None,
) -> str:
    for _, (prefix, uri) in ET.iterparse(io.StringIO(xml_content), events=('start-ns',)):
        if prefix and prefix != 'xml' and not re.fullmatch(r'ns\d+', prefix):
            ET.register_namespace(prefix, uri)
    root = ET.fromstring(xml_content, parser=ET.XMLParser(target=DomainTreeBuilder()))
    if mode is None:
        correct_memory_values(
            root, base_kib=base_kib, initial_kib=initial_kib, max_override_kib=max_override_kib
        )
    elif mode == 'to-virtiomem':
        convert_to_virtiomem(
            root, base_kib=base_kib if base_kib is not None else DEFAULT_BASE_KIB,
            keep_balloon=keep_balloon, initial_kib=initial_kib,
            max_override_kib=max_override_kib,
        )
    else:
        convert_to_balloon(root, initial_kib=initial_kib, max_override_kib=max_override_kib)
    raw_xml = ET.tostring(root, encoding='unicode')
    return format_libvirt_style(raw_xml)

def print_diff(orig_xml: str, new_xml: str, label: str, color_mode: str = 'auto') -> bool:
    orig_lines = orig_xml.splitlines(keepends=True)
    new_lines = new_xml.splitlines(keepends=True)
    diff = list(difflib.unified_diff(orig_lines, new_lines, fromfile=f'{label} (before)', tofile=f'{label} (after)'))
    if not diff:
        print(f"[DRY-RUN] No changes needed for '{label}'; the configuration is already up to date.")
        return False
    use_color = supports_color(color_mode)
    formatted_diff = format_diff_lines(diff, use_color)
    sys.stdout.writelines(formatted_diff)
    return True

def run_virsh(args: List[str], connect_uri: Optional[str] = None) -> str:
    cmd = ['virsh']
    if connect_uri:
        cmd.extend(['-c', connect_uri])
    cmd.extend(args)
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        err_msg = res.stderr.strip() or res.stdout.strip()
        raise RuntimeError(f"virsh command failed ({' '.join(cmd)}): {err_msg}")
    return res.stdout

def write_xml_atomically(path: str, content: str) -> None:
    destination = os.path.realpath(path)
    directory = os.path.dirname(destination) or os.curdir
    with tempfile.TemporaryDirectory(prefix='.virtiomem-', dir=directory) as tmp_dir:
        # Keep the staging directory private and usable even under a restrictive umask.
        os.chmod(tmp_dir, 0o700)
        tmp_path = os.path.join(tmp_dir, 'domain.xml')
        with open(tmp_path, 'x', encoding='utf-8') as output_file:
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())

        try:
            mode = stat.S_IMODE(os.stat(destination).st_mode)
        except FileNotFoundError:
            pass  # The new file already has the mode set at creation.
        else:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, destination)

def process_vm(
    vm_name: str,
    mode: Optional[str],
    base_kib: Optional[int],
    connect_uri: Optional[str] = None,
    dry_run: bool = False,
    color_mode: str = 'auto',
    keep_balloon: bool = False,
    initial_kib: Optional[int] = None,
    max_override_kib: Optional[int] = None,
):
    state = run_virsh(['domstate', vm_name], connect_uri).strip().lower()
    if state != 'shut off' and not dry_run:
        raise RuntimeError(
            f"VM '{vm_name}' is in state '{state}'. It must be shut off before proceeding."
        )
    domain_info = run_virsh(['dominfo', vm_name], connect_uri)
    persistent = None
    for line in domain_info.splitlines():
        key, separator, value = line.partition(':')
        if separator and key.strip() == 'Persistent':
            persistent = value.strip().lower()
            break
    if persistent != 'yes':
        if persistent == 'no':
            raise RuntimeError(f"VM '{vm_name}' is not persistent; refusing to redefine it.")
        raise RuntimeError(f"Could not determine whether VM '{vm_name}' is persistent; refusing to redefine it.")
    snapshots = run_virsh(['snapshot-list', '--name', vm_name], connect_uri)
    if snapshots.strip():
        raise RuntimeError(f"VM '{vm_name}' has snapshots; refusing to redefine it.")
    orig_xml = run_virsh(['dumpxml', '--inactive', vm_name], connect_uri)
    new_xml = transform_xml_string(
        orig_xml, mode, base_kib, keep_balloon=keep_balloon, initial_kib=initial_kib,
        max_override_kib=max_override_kib,
    )
    if dry_run:
        print_diff(orig_xml, new_xml, f'vm:{vm_name}', color_mode=color_mode)
        return
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', encoding='utf-8', delete=False) as tmp:
            tmp.write(new_xml)
            tmp_path = tmp.name
        run_virsh(['define', tmp_path], connect_uri)
        if mode is None:
            print(f"VM '{vm_name}' memory values updated; existing mode preserved.")
        else:
            print(f"VM '{vm_name}' successfully updated using mode [{mode}].")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

def main():
    parser = argparse.ArgumentParser(
        prog='libvirt-mem-convert',
        description='Convert libvirt XML between virtio-balloon and virtio-mem.')
    parser.add_argument(
        'positionals', nargs='*', metavar='MODE_OR_INPUT',
        help='Optional conversion mode, followed by input and output XML paths',
    )
    parser.add_argument(
        '-c', '--connect',
        metavar='URI',
        help='Libvirt connection URI passed to virsh (e.g. qemu:///system)',
    )
    parser.add_argument(
        '-V', '--vm',
        metavar='VM_NAME',
        help='Name of the libvirt VM to update directly via virsh (the VM must be shut off)',
    )
    parser.add_argument(
        '-b', '--base-memory',
        help='Minimum guaranteed base memory (e.g. 1G, 1536M, 2GiB). Default: 2GiB for conversion',
    )
    parser.add_argument(
        '-i', '--initial-memory',
        metavar='SIZE',
        help='Initial total guest memory (overrides the source or calculated initial memory)',
    )
    parser.add_argument(
        '-m', '--max-memory',
        metavar='SIZE',
        help='Maximum total guest memory (overrides the source or calculated maximum memory)',
    )
    parser.add_argument(
        '-k', '--keep-balloon',
        action='store_true',
        help='Leave the existing memballoon model unchanged when converting to virtio-mem',
    )
    parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Preview the conversion and show a diff without applying changes or writing files',
    )
    parser.add_argument(
        '-L', '--color',
        choices=['auto', 'always', 'never'],
        default='auto',
        help='Control diff output colors (default: auto)',
    )
    args = parser.parse_args()
    mode = None
    paths = args.positionals
    if paths and paths[0] in ('to-virtiomem', 'to-balloon'):
        mode = paths.pop(0)
    if len(paths) > 2:
        parser.error('Specify at most an input_xml and output_xml.')
    input_xml = paths[0] if paths else None
    output_xml = paths[1] if len(paths) > 1 else None
    if args.keep_balloon and mode != 'to-virtiomem':
        parser.error('--keep-balloon can only be used with to-virtiomem.')
    try:
        base_kib = (
            parse_memory_size(args.base_memory)
            if args.base_memory is not None
            else (None if mode is None else DEFAULT_BASE_KIB)
        )
        initial_kib = parse_memory_size(args.initial_memory) if args.initial_memory is not None else None
        max_override_kib = parse_memory_size(args.max_memory) if args.max_memory is not None else None
        if args.vm:
            if input_xml or output_xml:
                parser.error('Do not specify positional XML files when using --vm.')
            process_vm(
                args.vm,
                mode,
                base_kib,
                connect_uri=args.connect,
                dry_run=args.dry_run,
                color_mode=args.color,
                keep_balloon=args.keep_balloon,
                initial_kib=initial_kib,
                max_override_kib=max_override_kib,
            )
        else:
            if not input_xml:
                parser.error('Specify input_xml or use --vm VM_NAME.')
            if not args.dry_run and not output_xml:
                parser.error('Specify output_xml, or use --dry-run to preview changes without saving.')
            with open(input_xml, 'r', encoding='utf-8') as f:
                orig_xml = f.read()
            new_xml = transform_xml_string(
                orig_xml, mode, base_kib, keep_balloon=args.keep_balloon, initial_kib=initial_kib,
                max_override_kib=max_override_kib,
            )
            if args.dry_run:
                print_diff(orig_xml, new_xml, input_xml, color_mode=args.color)
            else:
                write_xml_atomically(output_xml, new_xml)
                if mode is None:
                    print(f'Memory values corrected, existing mode preserved -> {output_xml}')
                else:
                    print(f'Conversion [{mode}] completed successfully -> {output_xml}')
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
