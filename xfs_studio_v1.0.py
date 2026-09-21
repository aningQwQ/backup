#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XFS Studio — XFS 结构与性能分析器（插件化）

运行: python3 xfs_studio.py

依赖:
    pip install PySide6

架构:
    ┌─ Worker 侧 (root)         只采集，JSON 到 stdout
    ├─ 插件系统                 TabPlugin + @register_tab
    └─ GUI 侧 (用户态)
       ├─ 主窗口动态加载所有已注册 Tab
       ├─ 概览       (order=10)
       ├─ AG 结构    (order=20)
       └─ 空闲可视化 (order=30)
"""
import os, re, sys, json, math, time, shutil, subprocess

from PySide6.QtCore import Qt, QPointF, QRectF, QThread, Signal, Slot, QTimer
from PySide6.QtGui import (QPainter, QColor, QFont, QPen, QAction,
                           QFontDatabase)
from PySide6.QtWidgets import (QApplication, QWidget, QMainWindow, QVBoxLayout,
                               QHBoxLayout, QGridLayout, QPushButton, QLabel,
                               QComboBox, QScrollBar, QMenu, QMessageBox,
                               QSplitter, QTreeWidget, QTreeWidgetItem,
                               QTabWidget, QTableWidget, QTableWidgetItem,
                               QPlainTextEdit, QTextEdit, QHeaderView,
                               QGroupBox, QStatusBar, QProgressBar)


# =============================================================================
# Part 1 · Worker（root 侧，只采集，JSON 到 stdout）
# =============================================================================
def _kv(text):
    d = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            d[k.strip()] = v.strip()
    return d


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, '', str(e))


def collect_full(dev):
    if shutil.which('xfs_db') is None:
        print("未安装 xfs_db（请 dnf install xfsprogs）", file=sys.stderr)
        sys.exit(2)

    info = _run(['xfs_info', dev]).stdout
    sb = _kv(_run(['xfs_db', '-r', '-c', 'sb 0', '-c', 'p', dev]).stdout)
    try:
        agcount = int(sb.get('agcount', '0'))
    except ValueError:
        agcount = 0

    ags = []
    for ag in range(agcount):
        agf = _kv(_run(['xfs_db', '-r', '-c', f'agf {ag}',
                        '-c', 'p', dev]).stdout)
        agi = _kv(_run(['xfs_db', '-r', '-c', f'agi {ag}',
                        '-c', 'p', dev]).stdout)
        r = _run(['xfs_db', '-r', '-c', f'agf {ag}', '-c', 'addr bnoroot',
                  '-c', 'btdump', dev])
        recs = [[int(m.group(1)), int(m.group(2))]
                for m in re.finditer(r'\[\s*(\d+)\s*,\s*(\d+)\s*\]',
                                     r.stdout + r.stderr)]
        ags.append({'ag': ag, 'agf': agf, 'agi': agi, 'bnobt': recs})

    return {'dev': dev, 'info': info, 'sb': sb, 'ags': ags}


# 采集动作注册表：动作名 → 处理函数
COLLECTORS = {
    'full': collect_full,
}


def worker_main(args):
    if not args:
        print(f"usage: --worker <{'|'.join(COLLECTORS)}> <dev>", file=sys.stderr)
        sys.exit(2)
    action = args[0]
    fn = COLLECTORS.get(action)
    if fn is None:
        print(f"unknown action: {action}", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(json.dumps(fn(args[1])))
    sys.stdout.flush()


# =============================================================================
# Part 2 · 通用工具
# =============================================================================
def list_xfs_devices():
    try:
        out = subprocess.run(
            ['lsblk', '-o', 'PATH,FSTYPE,UUID,LABEL,MOUNTPOINT,SIZE', '-J'],
            capture_output=True, text=True).stdout
        tree = json.loads(out)
    except Exception:
        return []

    devs = []

    def walk(node):
        if node.get('fstype') == 'xfs' and node.get('path'):
            devs.append({
                'dev': node['path'],
                'uuid': node.get('uuid') or '-',
                'label': node.get('label') or '-',
                'mount': node.get('mountpoint') or '-',
                'size': node.get('size') or '-',
            })
        for c in node.get('children', []):
            walk(c)

    for d in tree.get('blockdevices', []):
        walk(d)
    return devs


def human_size(n):
    for u in ('B', 'K', 'M', 'G', 'T', 'P'):
        if abs(n) < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} E"


def nice_step(raw):
    if raw <= 0:
        return 1
    exp = math.floor(math.log10(raw))
    base = raw / (10 ** exp)
    if base < 1.5:
        nice = 1
    elif base < 3:
        nice = 2
    elif base < 7:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exp)


def fmt_tick(v):
    a = abs(v)
    if a >= 1e12: return f"{v/1e12:.1f}T"
    if a >= 1e9:  return f"{v/1e9:.1f}G"
    if a >= 1e6:  return f"{v/1e6:.1f}M"
    if a >= 1e3:  return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


def read_diskstats(name):
    try:
        with open('/proc/diskstats') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 14 and parts[2] == name:
                    return {
                        'reads': int(parts[3]),
                        'sectors_read': int(parts[5]),
                        'ms_reading': int(parts[6]),
                        'writes': int(parts[7]),
                        'sectors_written': int(parts[9]),
                        'ms_writing': int(parts[10]),
                        'ios_in_progress': int(parts[11]),
                        'ms_io': int(parts[12]),
                    }
    except Exception:
        pass
    return None


# =============================================================================
# Part 3 · 插件系统
# =============================================================================
_TAB_REGISTRY = []


def register_tab(cls):
    """Tab 插件注册装饰器：被装饰的类会在主窗口初始化时自动挂载"""
    _TAB_REGISTRY.append(cls)
    return cls


def get_registered_tabs():
    """按 TAB_ORDER 返回所有已注册的 Tab 类"""
    return sorted(_TAB_REGISTRY, key=lambda c: getattr(c, 'TAB_ORDER', 100))


class TabPlugin(QWidget):
    """
    Tab 插件基类。子类需声明：
        TAB_ID     唯一标识（字符串）
        TAB_TITLE  显示名（字符串）
        TAB_ORDER  排序权重（数字，越小越靠前）
        NEEDS      需要的采集动作元组，例如 ('full',)
    并实现:
        update_data(payloads)   payloads = {动作名: 结果dict, ...}
        clear()
    """
    TAB_ID = ""
    TAB_TITLE = ""
    TAB_ORDER = 100
    NEEDS = ('full',)

    def update_data(self, payloads):
        pass

    def clear(self):
        pass


# =============================================================================
# Part 4 · 图表 Widget（QPainter）
# =============================================================================
class BnobtChart(QWidget):
    ML, MR, MT, MB = 80, 110, 52, 48
    ROW_HEIGHT = 44

    C_BG      = QColor('#ffffff')
    C_ROW_ALT = QColor('#fafafa')
    C_USED    = QColor('#3b82f6')
    C_FREE    = QColor('#e3e3e3')
    C_FREE_BD = QColor('#cfcfcf')
    C_TEXT    = QColor('#1a1a1a')
    C_DIM     = QColor('#666666')
    C_EDGE    = QColor('#999999')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(400, 300)
        self.setStyleSheet("background: white;")

        self.dev = None
        self.ags = []
        self.agsize = 1

        self.view_start = 0.0
        self.view_end = 1.0
        self.v_offset = 0.0

        self._drag_pos = None
        self._drag_view = None
        self._hover_pos = None
        self._hover_info = None

        self.vbar = None
        self.hbar = None

    def attach_scrollbars(self, vbar: QScrollBar, hbar: QScrollBar):
        self.vbar = vbar
        self.hbar = hbar
        vbar.valueChanged.connect(self._on_v_scroll)
        hbar.valueChanged.connect(self._on_h_scroll)
        self._sync_scrollbars()

    def set_payload(self, payload):
        self.dev = payload['dev']
        self.ags = [(a['ag'], [tuple(r) for r in a.get('bnobt', [])])
                    for a in payload['ags']]
        try:
            self.agsize = int(payload['sb'].get('agblocks', '1'))
        except ValueError:
            self.agsize = 1
        self.view_start = 0.0
        self.view_end = float(self.agsize)
        self.v_offset = 0.0
        self._hover_pos = None
        self._hover_info = None
        self._sync_scrollbars()
        self.update()

    def clear(self):
        self.dev = None
        self.ags = []
        self.v_offset = 0.0
        self._hover_pos = None
        self._hover_info = None
        self._sync_scrollbars()
        self.update()

    def _layout(self, W, H):
        return (self.ML, self.MT,
                max(1, W - self.ML - self.MR),
                max(1, H - self.MT - self.MB))

    def _x_to_px(self, x, W, H):
        dx, _, dw, _ = self._layout(W, H)
        span = self.view_end - self.view_start
        return dx + (x - self.view_start) / span * dw

    def _px_to_x(self, px, W, H):
        dx, _, dw, _ = self._layout(W, H)
        span = self.view_end - self.view_start
        return self.view_start + (px - dx) / dw * span

    def _sync_scrollbars(self):
        if self.vbar is None:
            return
        W, H = self.width(), self.height()
        dh = max(1, H - self.MT - self.MB)

        n = len(self.ags)
        total_h = n * self.ROW_HEIGHT
        vmax = max(0, total_h - dh)
        self.vbar.blockSignals(True)
        self.vbar.setRange(0, vmax)
        self.vbar.setPageStep(dh)
        self.vbar.setSingleStep(self.ROW_HEIGHT)
        if self.v_offset > vmax:
            self.v_offset = vmax
        self.vbar.setValue(int(round(self.v_offset)))
        self.vbar.blockSignals(False)
        self.vbar.setVisible(n > 0 and vmax > 0)

        span = self.view_end - self.view_start
        hmax = max(0, int(round(self.agsize - span)))
        self.hbar.blockSignals(True)
        self.hbar.setRange(0, hmax)
        self.hbar.setPageStep(int(round(span)))
        self.hbar.setSingleStep(max(1, int(span / 20)))
        self.hbar.setValue(int(round(self.view_start)))
        self.hbar.blockSignals(False)
        self.hbar.setVisible(n > 0)

    def _on_v_scroll(self, value):
        self.v_offset = float(value)
        self.update()

    def _on_h_scroll(self, value):
        span = self.view_end - self.view_start
        self.view_start = float(value)
        self.view_end = self.view_start + span
        if self.view_end > self.agsize:
            self.view_end = float(self.agsize)
            self.view_start = self.view_end - span
        self.update()

    def resizeEvent(self, event):
        self._sync_scrollbars()
        super().resizeEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, self.C_BG)

        if not self.ags:
            p.setPen(self.C_DIM)
            f = QFont(); f.setPointSizeF(12); p.setFont(f)
            p.drawText(QRectF(0, 0, W, H), Qt.AlignCenter,
                       "请从左侧选择一个 XFS 设备")
            return

        dx, dy, dw, dh = self._layout(W, H)
        n = len(self.ags)
        row_h = self.ROW_HEIGHT
        bar_h = row_h * 0.70
        bar_pad = (row_h - bar_h) / 2

        total_free = sum(sum(c for _, c in r) for _, r in self.ags)
        total = self.agsize * n
        ratio = total_free / total * 100 if total else 0

        f = QFont(); f.setPointSizeF(12); f.setBold(True)
        p.setFont(f); p.setPen(self.C_TEXT)
        p.drawText(QRectF(0, 8, W, 28), Qt.AlignCenter,
                   f"{self.dev}   总空闲 {ratio:.2f}%  "
                   f"({total_free:,} / {total:,} 块)")

        p.save()
        p.setClipRect(QRectF(dx, dy, dw, dh))

        first = max(0, int(self.v_offset // row_h))
        last  = min(n - 1, int((self.v_offset + dh) // row_h))

        for i in range(first, last + 1):
            ag_no, recs = self.ags[i]
            row_y = dy + i * row_h - self.v_offset
            bar_y = row_y + bar_pad

            if i % 2 == 0:
                p.fillRect(QRectF(dx, row_y, dw, row_h), self.C_ROW_ALT)

            p.setPen(Qt.NoPen)
            p.fillRect(QRectF(dx, bar_y, dw, bar_h), self.C_USED)

            p.setBrush(self.C_FREE)
            for s, c in recs:
                sx = self._x_to_px(s, W, H)
                ex = self._x_to_px(s + c, W, H)
                if ex - sx < 0.75:
                    ex = sx + 0.75
                vx1 = max(sx, dx)
                vx2 = min(ex, dx + dw)
                if vx2 <= vx1:
                    continue
                p.drawRect(QRectF(vx1, bar_y, vx2 - vx1, bar_h))

            f2 = QFont(); f2.setPointSizeF(10); f2.setBold(True)
            p.setFont(f2); p.setPen(self.C_TEXT)
            p.drawText(QRectF(0, row_y, dx - 8, row_h),
                       Qt.AlignVCenter | Qt.AlignRight, f"AG {ag_no}")

        p.restore()

        for i in range(first, last + 1):
            ag_no, recs = self.ags[i]
            row_y = dy + i * row_h - self.v_offset
            free = sum(c for _, c in recs)
            pct = free / self.agsize * 100 if self.agsize else 0

            f3 = QFont(); f3.setPointSizeF(10); f3.setBold(True)
            p.setFont(f3); p.setPen(self.C_TEXT)
            p.drawText(QRectF(dx + dw + 6, row_y, self.MR - 10, row_h),
                       Qt.AlignVCenter | Qt.AlignRight, f"{pct:.2f}%")

        span = self.view_end - self.view_start
        raw_step = span / 8
        step = nice_step(raw_step)
        tick = math.floor(self.view_start / step) * step
        axis_y = dy + dh
        p.setPen(QPen(self.C_EDGE, 1))
        p.drawLine(QPointF(dx, axis_y), QPointF(dx + dw, axis_y))

        f4 = QFont(); f4.setPointSizeF(9); p.setFont(f4)
        while tick <= self.view_end + step * 0.001:
            if tick >= self.view_start - step * 0.001:
                px = self._x_to_px(tick, W, H)
                if dx - 1 <= px <= dx + dw + 1:
                    p.setPen(QPen(self.C_EDGE, 1))
                    p.drawLine(QPointF(px, axis_y),
                               QPointF(px, axis_y + 4))
                    p.setPen(self.C_DIM)
                    p.drawText(QRectF(px - 50, axis_y + 6, 100, 20),
                               Qt.AlignCenter, fmt_tick(tick))
            tick += step

        p.setPen(self.C_DIM)
        p.drawText(QRectF(dx, axis_y + 24, dw, 20),
                   Qt.AlignCenter, "AG 内块号 (block)")

        self._draw_legend(p, W)
        self._draw_hover_box(p, W, H)

    def _draw_legend(self, p, W):
        x0 = W - self.MR + 6
        y0 = self.MT + 4
        box = 11
        f = QFont(); f.setPointSizeF(8.5); p.setFont(f)
        fm = p.fontMetrics()
        gap = 6

        items = [(self.C_USED, '已使用'), (self.C_FREE, '空闲')]
        x = x0
        for color, label in items:
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRect(QRectF(x, y0, box, box))
            if color is self.C_FREE:
                p.setPen(QPen(self.C_FREE_BD, 1))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x, y0, box, box))
            p.setPen(self.C_TEXT)
            tw = fm.horizontalAdvance(label)
            p.drawText(QRectF(x + box + 3, y0 - 2, tw + 2, box + 4),
                       Qt.AlignVCenter, label)
            x += box + 3 + tw + gap

    def wheelEvent(self, event):
        if not self.ags:
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta == 0:
            return
        mods = event.modifiers()

        if mods & Qt.ControlModifier:
            self._zoom(event.position().x(), 1.25 if delta > 0 else 1 / 1.25)
        elif mods & Qt.ShiftModifier:
            self._pan_pixels(delta * 0.5)
        else:
            if self.vbar:
                v = self.vbar.value() - delta / 2
                self.vbar.setValue(
                    int(round(max(0, min(v, self.vbar.maximum())))))

    def _zoom(self, mouse_px, factor):
        W, H = self.width(), self.height()
        anchor = self._px_to_x(mouse_px, W, H)

        span = (self.view_end - self.view_start) / factor
        min_span = max(1.0, self.agsize / 1e6)
        span = max(min_span, min(span, float(self.agsize)))

        ratio = (anchor - self.view_start) / (self.view_end - self.view_start)
        self.view_start = anchor - ratio * span
        self.view_end = self.view_start + span

        if self.view_start < 0:
            self.view_start = 0
            self.view_end = span
        if self.view_end > self.agsize:
            self.view_end = float(self.agsize)
            self.view_start = self.agsize - span

        self._sync_scrollbars()
        self.update()

    def _pan_pixels(self, px):
        W, H = self.width(), self.height()
        dx, _, dw, _ = self._layout(W, H)
        span = self.view_end - self.view_start
        d_data = -px / dw * span
        self.view_start += d_data
        self.view_end += d_data
        if self.view_start < 0:
            self.view_end -= self.view_start
            self.view_start = 0
        if self.view_end > self.agsize:
            self.view_start -= (self.view_end - self.agsize)
            self.view_end = float(self.agsize)
        self._sync_scrollbars()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.ags:
            self._drag_pos = event.position()
            self._drag_view = (self.view_start, self.view_end)
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self.setCursor(Qt.ArrowCursor)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            W, H = self.width(), self.height()
            dx, _, dw, _ = self._layout(W, H)
            delta_px = event.position().x() - self._drag_pos.x()
            span = self._drag_view[1] - self._drag_view[0]
            d_data = -delta_px / dw * span
            self.view_start = self._drag_view[0] + d_data
            self.view_end = self._drag_view[1] + d_data
            if self.view_start < 0:
                self.view_end -= self.view_start
                self.view_start = 0
            if self.view_end > self.agsize:
                self.view_start -= (self.view_end - self.agsize)
                self.view_end = self.agsize
            self._sync_scrollbars()
            self.update()
            return

        self._hover_pos = event.position()
        self._hover_info = self._compute_hover(event.position())
        self.update()

    def leaveEvent(self, event):
        self._hover_pos = None
        self._hover_info = None
        self.update()

    def mouseDoubleClickEvent(self, event):
        if self.ags:
            self.view_start = 0.0
            self.view_end = float(self.agsize)
            self.v_offset = 0.0
            self._sync_scrollbars()
            self.update()

    def contextMenuEvent(self, event):
        menu = QMenu(self)

        def reset_all():
            self.view_start = 0.0
            self.view_end = float(self.agsize)
            self.v_offset = 0.0
            self._sync_scrollbars()
            self.update()

        act = QAction("重置缩放 / 回到顶部", self)
        act.triggered.connect(reset_all)
        menu.addAction(act)
        menu.exec(event.globalPos())

    def _compute_hover(self, pos):
        if not self.ags:
            return None
        W, H = self.width(), self.height()
        dx, dy, dw, dh = self._layout(W, H)
        x, y = pos.x(), pos.y()
        if not (dx <= x <= dx + dw and dy <= y <= dy + dh):
            return None

        rel_y = y - dy + self.v_offset
        row = int(rel_y // self.ROW_HEIGHT)
        n = len(self.ags)
        if not (0 <= row < n):
            return None

        ag_no, recs = self.ags[row]
        block = self._px_to_x(x, W, H)

        free = sum(c for _, c in recs)
        cnt = len(recs)
        pct = free / self.agsize * 100 if self.agsize else 0

        lo, hi = 0, len(recs) - 1
        hit = None
        while lo <= hi:
            mid = (lo + hi) // 2
            s, c = recs[mid]
            if block < s:
                hi = mid - 1
            elif block >= s + c:
                lo = mid + 1
            else:
                hit = (s, c)
                break

        title = f"AG {ag_no}   ·   空闲 {pct:.2f}%   ·   {cnt} 段"

        if hit:
            s, c = hit
            lines = [
                ("当前块号", f"{int(block):,}"),
                ("起始块号", f"{s:,}"),
                ("长度",     f"{c:,} 块   ({human_size(c * 4096)})"),
                ("结束块号", f"{s + c - 1:,}"),
            ]
            color = self.C_FREE_BD
        else:
            lines = [
                ("当前块号", f"{int(block):,}"),
                ("状态",     "已使用"),
            ]
            color = self.C_USED

        return {'title': title, 'lines': lines, 'color': color}

    def _draw_hover_box(self, p, W, H):
        hp = self._hover_pos
        info = self._hover_info
        if hp is None or info is None:
            return

        f_title = QFont(); f_title.setPointSizeF(9.5); f_title.setBold(True)
        f_body  = QFont(); f_body.setPointSizeF(9)

        p.setFont(f_body)
        fm_body = p.fontMetrics()
        label_w = max(fm_body.horizontalAdvance(k) for k, _ in info['lines'])
        value_w = max(fm_body.horizontalAdvance(v) for _, v in info['lines'])
        gap = 14
        content_w = label_w + gap + value_w

        p.setFont(f_title)
        fm_title = p.fontMetrics()
        title_w = fm_title.horizontalAdvance(info['title'])

        box_w = max(content_w, title_w) + 24
        title_h = fm_title.height() + 2
        line_h  = fm_body.height() + 3
        box_h = title_h + 8 + len(info['lines']) * line_h + 12

        x = hp.x() + 16
        y = hp.y() + 16
        if x + box_w > W - 6:
            x = hp.x() - box_w - 16
        if y + box_h > H - 6:
            y = hp.y() - box_h - 16
        x = max(6, x)
        y = max(6, y)

        box = QRectF(x, y, box_w, box_h)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 28))
        p.drawRoundedRect(box.translated(2, 2), 6, 6)

        p.setBrush(QColor(255, 255, 255, 245))
        p.setPen(QPen(QColor('#7a7a7a'), 1))
        p.drawRoundedRect(box, 6, 6)

        p.setBrush(info['color'])
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(x + 2, y + 2, 3, box_h - 4), 1.5, 1.5)

        p.setFont(f_title)
        p.setPen(QColor('#1a1a1a'))
        p.drawText(QRectF(x + 14, y + 6, box_w - 20, title_h),
                   Qt.AlignLeft | Qt.AlignVCenter, info['title'])

        sep_y = y + 6 + title_h + 4
        p.setPen(QPen(QColor('#e0e0e0'), 1))
        p.drawLine(QPointF(x + 14, sep_y), QPointF(x + box_w - 8, sep_y))

        p.setFont(f_body)
        for i, (k, v) in enumerate(info['lines']):
            ly = sep_y + 4 + i * line_h
            p.setPen(QColor('#777777'))
            p.drawText(QRectF(x + 14, ly, label_w, line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, k)
            p.setPen(QColor('#1a1a1a'))
            p.drawText(QRectF(x + 14 + label_w + gap, ly,
                              value_w + 4, line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, v)


# =============================================================================
# Part 5 · Tab 插件（按 TAB_ORDER 顺序加载）
# =============================================================================

# --------------------------- 概览 ---------------------------
@register_tab
class OverviewTab(TabPlugin):
    TAB_ID = "overview"
    TAB_TITLE = "概览"
    TAB_ORDER = 10
    NEEDS = ('full',)

    SB_FIELDS = [
        ('magicnum',   'Magic'),
        ('versionnum', 'Version'),
        ('blocksize',  '块大小'),
        ('sectsize',   '扇区大小'),
        ('inodesize',  'inode 大小'),
        ('agcount',    'AG 数量'),
        ('agblocks',   '每 AG 块数'),
        ('dblocks',    '数据块总数'),
        ('logblocks',  '日志块数'),
        ('icount',     'inode 总数'),
        ('ifree',      '空闲 inode'),
        ('fdblocks',   '空闲数据块'),
        ('uuid',       'UUID'),
        ('fname',      '卷标'),
    ]

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        self.title = QLabel("请从左侧选择一个 XFS 设备")
        self.title.setStyleSheet(
            "font-size:16px; font-weight:bold; padding:6px;")
        layout.addWidget(self.title)

        gb_sb = QGroupBox("超级块 (Superblock)")
        g = QGridLayout(gb_sb)
        self.sb_labels = {}
        for i, (k, label) in enumerate(self.SB_FIELDS):
            g.addWidget(QLabel(f"{label}:"), i, 0)
            v = QLabel("-")
            v.setStyleSheet("font-family: monospace;")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            g.addWidget(v, i, 1)
            self.sb_labels[k] = v
        layout.addWidget(gb_sb)

        gb_info = QGroupBox("xfs_info 输出")
        li = QVBoxLayout(gb_info)
        self.info_text = QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setFont(QFont("Monospace", 10))
        li.addWidget(self.info_text)
        layout.addWidget(gb_info)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        sb = payload.get('sb', {})
        self.title.setText(f"设备: {payload.get('dev')}")
        for k, _ in self.SB_FIELDS:
            self.sb_labels[k].setText(sb.get(k, '-'))
        self.info_text.setPlainText(payload.get('info', ''))

    def clear(self):
        self.title.setText("请从左侧选择一个 XFS 设备")
        for v in self.sb_labels.values():
            v.setText('-')
        self.info_text.clear()


# --------------------------- AG 结构 ---------------------------
@register_tab
class AgTab(TabPlugin):
    TAB_ID = "ag"
    TAB_TITLE = "AG 结构"
    TAB_ORDER = 20
    NEEDS = ('full',)

    COLS = ['AG', '长度(块)', '空闲块', '空闲%',
            'inode 总数', '空闲 inode', 'inode 使用率', 'bnobt 段数']

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if not payload:
            return
        ags = payload.get('ags', [])
        self.table.setRowCount(len(ags))
        for row, ag in enumerate(ags):
            agf = ag.get('agf', {})
            agi = ag.get('agi', {})
            bnobt = ag.get('bnobt', [])

            try:
                length = int(agf.get('length', '0'))
            except ValueError:
                length = 0
            free = sum(c for _, c in bnobt)
            freepct = free / length * 100 if length else 0

            try:
                icount = int(agi.get('count', '0'))
                ifree = int(agi.get('freecount', '0'))
            except ValueError:
                icount = ifree = 0
            ipct = (icount - ifree) / icount * 100 if icount else 0

            vals = [
                f"AG {ag['ag']}",
                f"{length:,}",
                f"{free:,}",
                f"{freepct:.2f}%",
                f"{icount:,}",
                f"{ifree:,}",
                f"{ipct:.2f}%",
                f"{len(bnobt)}",
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, c, item)

    def clear(self):
        self.table.setRowCount(0)


# --------------------------- 空闲可视化 ---------------------------
@register_tab
class PlotTab(TabPlugin):
    TAB_ID = "plot"
    TAB_TITLE = "空闲可视化"
    TAB_ORDER = 30
    NEEDS = ('full',)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        grid = QGridLayout()
        grid.setSpacing(0)

        self.chart = BnobtChart()
        self.vbar = QScrollBar(Qt.Vertical)
        self.hbar = QScrollBar(Qt.Horizontal)

        vbar_holder = QWidget()
        vl = QVBoxLayout(vbar_holder)
        vl.setContentsMargins(0, BnobtChart.MT, 0, BnobtChart.MB)
        vl.setSpacing(0)
        vl.addWidget(self.vbar)

        hbar_holder = QWidget()
        hl = QHBoxLayout(hbar_holder)
        hl.setContentsMargins(BnobtChart.ML, 0, BnobtChart.MR, 0)
        hl.setSpacing(0)
        hl.addWidget(self.hbar)

        grid.addWidget(self.chart,      0, 0)
        grid.addWidget(vbar_holder,     0, 1)
        grid.addWidget(hbar_holder,     1, 0)
        grid.setRowStretch(0, 1)
        grid.setColumnStretch(0, 1)
        layout.addLayout(grid)

        self.chart.attach_scrollbars(self.vbar, self.hbar)

    def update_data(self, payloads):
        payload = payloads.get('full')
        if payload:
            self.chart.set_payload(payload)

    def clear(self):
        self.chart.clear()




# =============================================================================
# Part 6 · 采集线程（根据 Tab 声明的 NEEDS 收集动作）
# =============================================================================
class CaptureThread(QThread):
    done = Signal(dict, str)  # {action: result}, stderr

    def __init__(self, dev, actions, parent=None):
        super().__init__(parent)
        self.dev = dev
        self.actions = actions

    def run(self):
        try:
            py = sys.executable
            script = os.path.abspath(__file__)
            prefix = ['pkexec'] if shutil.which('pkexec') else ['sudo']

            results = {}
            for action in self.actions:
                r = subprocess.run(
                    prefix + [py, script, '--worker', action, self.dev],
                    capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    self.done.emit(results,
                                   f"{action}: {r.stderr.strip()}")
                    return
                try:
                    results[action] = json.loads(r.stdout)
                except Exception as e:
                    self.done.emit(results, f"{action} 解析失败: {e}")
                    return
            self.done.emit(results, '')
        except Exception as e:
            self.done.emit({}, str(e))


# =============================================================================
# Part 7 · 主窗口
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XFS Studio · 结构与性能分析")
        self.resize(1500, 950)

        self.current_dev = None
        self.current_mount = None
        self.capture_thread = None
        self.tab_instances = []

        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # 左：设备树
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        ll.addWidget(QLabel("XFS 设备"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['设备', '挂载点'])
        self.tree.itemClicked.connect(self._on_device_clicked)
        ll.addWidget(self.tree)
        btn_refresh = QPushButton("🔄 刷新设备")
        btn_refresh.clicked.connect(self.refresh_devices)
        ll.addWidget(btn_refresh)
        splitter.addWidget(left)

        # 右：Tabs（插件自动加载）
        self.tabs = QTabWidget()
        self._build_tabs()
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 1200])

        # 状态栏
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(200)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)

        self.refresh_devices()
        self.status.showMessage(
            f"就绪 · 已加载 {len(self.tab_instances)} 个 Tab")

    # ---------------- 插件加载 ----------------
    def _build_tabs(self):
        """按 TAB_ORDER 加载所有注册的 Tab"""
        for cls in get_registered_tabs():
            try:
                w = cls()
                self.tabs.addTab(w, cls.TAB_TITLE)
                self.tab_instances.append(w)
            except Exception as e:
                print(f"加载 Tab {cls.__name__} 失败: {e}", file=sys.stderr)

    def _collect_actions(self):
        """收集所有 Tab 声明的采集动作（去重，保持顺序）"""
        seen = set()
        actions = []
        for cls in get_registered_tabs():
            for a in cls.NEEDS:
                if a not in seen and a in COLLECTORS:
                    seen.add(a)
                    actions.append(a)
        return actions

    # ---------------- 设备 ----------------
    def refresh_devices(self):
        self.tree.clear()
        devs = list_xfs_devices()
        for d in devs:
            item = QTreeWidgetItem([d['dev'], d['mount']])
            item.setData(0, Qt.UserRole, d)
            self.tree.addTopLevelItem(item)
        self.status.showMessage(f"检测到 {len(devs)} 个 XFS 设备")

    def _on_device_clicked(self, item, _col):
        d = item.data(0, Qt.UserRole)
        if not d:
            return
        self.current_dev = d['dev']
        self.current_mount = d['mount']
        self._start_capture(d['dev'])

    # ---------------- 采集 ----------------
    def _start_capture(self, dev):
        if self.capture_thread and self.capture_thread.isRunning():
            self.status.showMessage("已有采集任务在运行，请稍候")
            return

        actions = self._collect_actions()
        if not actions:
            self.status.showMessage("没有 Tab 声明需要采集的数据")
            return

        self.status.showMessage(
            f"正在采集 {dev}（{'/'.join(actions)}，可能需要授权）…")
        self.progress.setVisible(True)
        self.tabs.setEnabled(False)

        self.capture_thread = CaptureThread(dev, actions, self)
        self.capture_thread.done.connect(self._on_capture_done)
        self.capture_thread.start()

    @Slot(dict, str)
    def _on_capture_done(self, payloads, stderr):
        self.progress.setVisible(False)
        self.tabs.setEnabled(True)

        if not payloads:
            self.status.showMessage("采集失败")
            QMessageBox.critical(self, "采集失败", stderr or "无数据返回")
            return

        # 把挂载点注入支持 set_mount 的 Tab（可选接口）
        for w in self.tab_instances:
            if hasattr(w, 'set_mount'):
                try:
                    w.set_mount(self.current_mount)
                except Exception:
                    pass

        # 分发数据
        for w in self.tab_instances:
            try:
                w.update_data(payloads)
            except Exception as e:
                print(f"{type(w).__name__}.update_data 出错: {e}",
                      file=sys.stderr)

        # 汇总状态
        dev = payloads.get('full', {}).get('dev', self.current_dev)
        n_ag = len(payloads.get('full', {}).get('ags', []))
        msg = f"采集完成: {dev}"
        if n_ag:
            msg += f" · {n_ag} 个 AG"
        if stderr:
            msg += f" · {stderr.strip()}"
        self.status.showMessage(msg)


# =============================================================================
# Part 8 · 入口
# =============================================================================
def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--worker':
        worker_main(sys.argv[2:])
        return

    if shutil.which('xfs_db') is None:
        print("未找到 xfs_db，请先安装: sudo dnf install xfsprogs")
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    families = ['Noto Sans CJK SC', 'Noto Sans SC', 'Source Han Sans SC',
                'WenQuanYi Zen Hei', 'WenQuanYi Micro Hei']
    available = set(QFontDatabase.families())
    for fam in families:
        if fam in available:
            f = QFont(fam); f.setPointSize(10)
            app.setFont(f)
            break

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
