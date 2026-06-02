#!/usr/bin/python3
"""Operator UI for building and writing the DryController OS Installer USB."""

from __future__ import annotations

import datetime as _dt
import json
import os
import plistlib
import queue
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[1]
DIST_DIR = REPO_ROOT / "dist"
AUTOINSTALL_DIR = DIST_DIR / "autoinstall"
DEFAULT_BASE_ISO = DIST_DIR / "drycontroller-os-installer-hybrid.iso"
DEFAULT_PASSWORD_ENV = REPO_ROOT / "infra" / "autoinstall" / "password.env"
DEFAULT_TAILNET_ENV = AUTOINSTALL_DIR / "tailnet.env"
DEFAULT_FACTORY_LICENSE = AUTOINSTALL_DIR / "factory-license.json"
FIXED_OPERATOR_USER = "dryops"
AIRPORT_BIN = Path("/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport")

# ── colour palette ──────────────────────────────────────────────
C = {
    "bg":              "#f4f6f3",
    "card":            "#ffffff",
    "card_alt":        "#eef4f0",
    "sidebar":         "#16211d",
    "sidebar_soft":    "#26352f",
    "primary":         "#0f766e",
    "primary_dark":    "#10231f",
    "primary_hover":   "#0b5f59",
    "primary_fg":      "#ffffff",
    "danger":          "#dc2626",
    "danger_light":    "#fef2f2",
    "danger_fg":       "#ffffff",
    "success":         "#15803d",
    "success_light":   "#ecfdf5",
    "warning":         "#b45309",
    "warning_light":   "#fffbeb",
    "text":            "#111827",
    "text_soft":       "#475569",
    "border":          "#d8e0db",
    "border_strong":   "#b9c7c0",
    "log_bg":          "#0f172a",
    "log_fg":          "#cbd5e1",
    "log_info":        "#38bdf8",
    "log_warn":        "#fbbf24",
    "log_err":         "#f87171",
    "entry_bg":        "#ffffff",
    "readonly_bg":     "#f1f5f9",
    "accent":          "#84cc16",
    "accent_light":    "#dcfce7",
}


def _apply_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")

    # ── font setup ──
    default_font = font.nametofont("TkDefaultFont")
    default_font.configure(family="Helvetica Neue", size=12)
    root.option_add("*Font", default_font)

    heading_font = font.Font(family="Helvetica Neue", size=16, weight="bold")
    mono_font = font.Font(family="SF Mono", size=11)

    # ── general styles ──
    style.configure(".", background=C["bg"], foreground=C["text"])
    style.configure("TLabel", background=C["bg"], foreground=C["text"])
    style.configure("TFrame", background=C["bg"])

    # ── card frame ──
    style.configure("Card.TFrame", background=C["card"])
    style.configure("Card.TLabel", background=C["card"], foreground=C["text"])
    style.configure("Card2.TLabel", background=C["card"], foreground=C["text_soft"],
                    font=("Helvetica Neue", 11))

    # ── entry ──
    style.configure("TEntry", fieldbackground=C["entry_bg"],
                    borderwidth=1, relief="solid", padding=8)
    style.configure("Readonly.TEntry", fieldbackground=C["readonly_bg"])

    # ── primary button ──
    style.configure("Primary.TButton",
                    background=C["primary"], foreground=C["primary_fg"],
                    borderwidth=0, relief="flat", padding=(28, 12),
                    font=("Helvetica Neue", 13, "bold"))
    style.map("Primary.TButton",
              background=[("active", C["primary_hover"]), ("disabled", "#94a3b8")],
              foreground=[("disabled", "#e2e8f0")])

    # ── secondary button ──
    style.configure("Secondary.TButton",
                    background=C["card"], foreground=C["text"],
                    borderwidth=1, relief="solid", padding=(18, 8),
                    font=("Helvetica Neue", 12))
    style.map("Secondary.TButton",
              background=[("active", "#f1f5f9"), ("disabled", "#e2e8f0")],
              foreground=[("disabled", "#94a3b8")])

    # ── small button ──
    style.configure("Small.TButton",
                    background=C["card"], foreground=C["text"],
                    borderwidth=1, relief="solid", padding=(12, 6))
    style.map("Small.TButton",
              background=[("active", "#f1f5f9"), ("disabled", "#e2e8f0")],
              foreground=[("disabled", "#94a3b8")])

    # ── checkbutton ──
    style.configure("Card.TCheckbutton", background=C["card"], foreground=C["text"])

    # ── combobox ──
    style.configure("TCombobox", fieldbackground=C["entry_bg"], padding=6)

    return heading_font, mono_font


class UsbMakerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DryController OS Installer USB Maker")
        self.geometry("1080x840")
        self.minsize(940, 700)
        self.configure(bg=C["bg"])

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running = False
        self.disk_rows: list[dict[str, str]] = []

        # ── variables ──
        self.base_iso_var = tk.StringVar(value=str(DEFAULT_BASE_ISO))
        release_date = _dt.datetime.now().strftime("%Y%m%d")
        self.output_iso_var = tk.StringVar(
            value=str(DIST_DIR / f"OS{release_date}-alpha.iso")
        )
        self.fixed_user_var = tk.StringVar(value=FIXED_OPERATOR_USER)
        self.password_source_var = tk.StringVar(value="默认密码配置")
        self.use_wifi_var = tk.BooleanVar(value=False)
        self.wifi_ssid_var = tk.StringVar()
        self.wifi_password_var = tk.StringVar()
        self.wifi_interface_var = tk.StringVar()
        self.wifi_scan_status_var = tk.StringVar(value="勾选后可扫描本机可见 WiFi，也可手动输入。")
        self.disk_var = tk.StringVar()
        self.rebuild_apt_var = tk.BooleanVar(value=False)
        self.include_images_var = tk.BooleanVar(value=True)
        self.include_factory_materials_var = tk.BooleanVar(value=True)
        self.tailnet_env_var = tk.StringVar(value=str(DEFAULT_TAILNET_ENV))
        self.factory_license_var = tk.StringVar(value=str(DEFAULT_FACTORY_LICENSE))
        self.write_after_build_var = tk.BooleanVar(value=True)
        self.readiness_summary_var = tk.StringVar(value="正在检查装机材料...")
        self.next_action_var = tk.StringVar(value="等待检查结果")
        self.readiness_labels: dict[str, tk.Label] = {}
        self.wifi_inputs: list[ttk.Widget] = []
        self.wifi_ssid_combo: ttk.Combobox | None = None
        self.wifi_scan_button: ttk.Button | None = None
        self.secondary_run_button: ttk.Button | None = None

        self.heading_font, self.mono_font = _apply_theme(self)
        self._build_ui()
        self._install_traces()
        self.refresh_disks()
        self._toggle_wifi_inputs()
        self._refresh_readiness()
        self.after(100, self._drain_log_queue)

    # ── UI construction ──────────────────────────────────────────

    def _build_ui(self) -> None:
        # header
        header = tk.Frame(self, bg=C["sidebar"], height=104)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        header_text = tk.Frame(header, bg=C["sidebar"])
        header_text.pack(side=tk.LEFT, padx=28, pady=16)
        tk.Label(
            header_text, text="现场装机工具",
            bg=C["accent"], fg=C["primary_dark"],
            font=("Helvetica Neue", 11, "bold"),
            padx=10, pady=3,
        ).pack(anchor="w", pady=(0, 6))
        tk.Label(
            header_text, text="DryController 装机 U 盘制作",
            bg=C["sidebar"], fg=C["primary_fg"],
            font=("Helvetica Neue", 24, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header_text, text="检查材料、选择 U 盘、生成 ISO、写入安装盘，一屏完成。",
            bg=C["sidebar"], fg="#cbd5e1",
            font=("Helvetica Neue", 12),
        ).pack(anchor="w", pady=(3, 0))
        header_meta = tk.Frame(header, bg=C["sidebar_soft"])
        header_meta.pack(side=tk.RIGHT, padx=28, pady=24)
        tk.Label(header_meta, text="Factory activation", bg=C["sidebar_soft"],
                 fg="#d9f99d", font=("Helvetica Neue", 12, "bold"),
                 padx=14, pady=8).pack()

        footer = tk.Frame(self, bg=C["card"], height=82, highlightthickness=1,
                          highlightbackground=C["border_strong"])
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        footer.pack_propagate(False)
        self._build_footer(footer)

        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill=tk.BOTH, expand=True)

        # scrollable main area
        canvas = tk.Canvas(body, bg=C["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=C["bg"])

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=scroll_frame, anchor="nw", tags="frame")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        root = tk.Frame(scroll_frame, bg=C["bg"])
        root.pack(fill=tk.BOTH, expand=True, padx=24, pady=22)
        root.columnconfigure(0, minsize=330)
        root.columnconfigure(1, weight=1)

        left = tk.Frame(root, bg=C["bg"])
        left.grid(row=0, column=0, sticky="new", padx=(0, 16))
        right = tk.Frame(root, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        root.rowconfigure(0, weight=1)

        self._card_operator_guide(left)
        self._card_readiness(left)
        self._card_disk(left)

        top = tk.Frame(right, bg=C["bg"])
        top.pack(fill=tk.X)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        account_col = tk.Frame(top, bg=C["bg"])
        account_col.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        wifi_col = tk.Frame(top, bg=C["bg"])
        wifi_col.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._card_account(account_col)
        self._card_wifi(wifi_col)

        self._card_factory_materials(right)
        self._card_iso(right)
        self._card_log(right)

    def _build_footer(self, parent: tk.Frame) -> None:
        status = tk.Frame(parent, bg=C["card"])
        status.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=24, pady=12)

        self.status_label = tk.Label(status, text="检查中", bg=C["card"],
                                     fg=C["text_soft"], font=("Helvetica Neue", 12, "bold"))
        self.status_label.pack(anchor="w")
        tk.Label(status, textvariable=self.next_action_var, bg=C["card"],
                 fg=C["text_soft"], font=("Helvetica Neue", 11)).pack(anchor="w", pady=(4, 0))

        buttons = tk.Frame(parent, bg=C["card"])
        buttons.pack(side=tk.RIGHT, padx=24, pady=14)
        self.secondary_run_button = ttk.Button(
            buttons,
            text="只生成 ISO",
            style="Secondary.TButton",
            command=self.start_build_iso_only,
        )
        self.secondary_run_button.pack(side=tk.LEFT, padx=(0, 12))
        self.run_button = ttk.Button(
            buttons,
            text="开始制作并写入 U 盘",
            style="Primary.TButton",
            command=self.start_build,
        )
        self.run_button.pack(side=tk.LEFT)

    def _make_card(self, parent: tk.Frame, title: str, subtitle: str = "",
                   *, fill: str = tk.X, expand: bool = False,
                   subtitle_wrap: int = 360) -> tk.Frame:
        card = tk.Frame(
            parent,
            bg=C["card"],
            highlightthickness=1,
            highlightbackground=C["border"],
        )
        card.pack(fill=fill, expand=expand, pady=(0, 14))

        header = tk.Frame(card, bg=C["card"])
        header.pack(fill=tk.X, padx=16, pady=(14, 8))
        tk.Label(
            header,
            text=title,
            bg=C["card"],
            fg=C["primary_dark"],
            font=("Helvetica Neue", 15, "bold"),
        ).pack(anchor="w")
        if subtitle:
            tk.Label(
                header,
                text=subtitle,
                bg=C["card"],
                fg=C["text_soft"],
                font=("Helvetica Neue", 11),
                wraplength=subtitle_wrap,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(3, 0))

        body = tk.Frame(card, bg=C["card"])
        body.pack(fill=tk.BOTH if expand else tk.X, expand=expand, padx=16, pady=(0, 16))
        return body

    def _card_operator_guide(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "装机流程", "按顺序处理，绿色状态后再写盘。", subtitle_wrap=260)

        steps = [
            ("1", "确认准备状态", "缺失项会显示在下方，先补齐再开始。"),
            ("2", "选择批次材料", "真机安装必须包含 tailnet.env 和 factory-license.json。"),
            ("3", "确认目标 U 盘", "只会列出外接磁盘，但仍要核对容量和盘名。"),
            ("4", "开始制作", "写入会清空 U 盘，完成后按日志提示拔出。"),
        ]
        for index, (number, title, body) in enumerate(steps):
            step = tk.Frame(card, bg=C["card"])
            step.pack(fill=tk.X, pady=(0 if index == 0 else 10, 0))
            badge = tk.Label(
                step,
                text=number,
                width=2,
                bg=C["sidebar"],
                fg=C["primary_fg"],
                font=("Helvetica Neue", 13, "bold"),
            )
            badge.pack(side=tk.LEFT, padx=(0, 10), ipady=3)
            text = tk.Frame(step, bg=C["card"])
            text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            tk.Label(text, text=title, bg=C["card"], fg=C["text"],
                     font=("Helvetica Neue", 12, "bold")).pack(anchor="w")
            tk.Label(text, text=body, bg=C["card"], fg=C["text_soft"],
                     font=("Helvetica Neue", 11), wraplength=245,
                     justify=tk.LEFT).pack(anchor="w", pady=(2, 0))

    def _card_readiness(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "准备状态", "红色项必须处理；黄色项表示可继续但要确认。", subtitle_wrap=260)

        summary = tk.Frame(card, bg=C["card_alt"], highlightthickness=1,
                           highlightbackground=C["border"])
        summary.pack(fill=tk.X, pady=(0, 12))
        tk.Label(summary, textvariable=self.readiness_summary_var,
                 bg=C["card_alt"], fg=C["text"],
                 font=("Helvetica Neue", 13, "bold"),
                 padx=12, pady=9).pack(anchor="w")

        items = [
            ("base_iso", "基础 ISO"),
            ("password_env", "dryops 密码配置"),
            ("factory_materials", "Factory 材料"),
            ("apt_repo", "离线 apt 包"),
            ("images", "业务镜像包"),
            ("target_disk", "目标 U 盘"),
        ]
        for index, (key, label) in enumerate(items):
            item = tk.Frame(card, bg=C["card"])
            item.pack(fill=tk.X, pady=(0, 8))
            tk.Label(item, text=label, bg=C["card"], fg=C["text"],
                     font=("Helvetica Neue", 12)).pack(side=tk.LEFT)
            status = tk.Label(
                item,
                text="待检查",
                bg=C["readonly_bg"],
                fg=C["text_soft"],
                font=("Helvetica Neue", 11, "bold"),
                padx=10,
                pady=3,
            )
            status.pack(side=tk.RIGHT)
            self.readiness_labels[key] = status
        ttk.Button(card, text="重新检查", style="Small.TButton",
                   command=self._refresh_readiness).pack(anchor="w", pady=(4, 0))

    def _card_account(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "账号", "固定运维账号，密码来自本机安全配置。", fill=tk.BOTH, expand=True)

        self._readonly_row(card, "运维账号", self.fixed_user_var, 0)
        self._readonly_row(card, "登录密码", self.password_source_var, 1)
        ttk.Label(card, text="这里通常不用修改。密码从 password.env 注入 SHA-512 hash。",
                  style="Card2.TLabel").grid(row=2, column=0, columnspan=2,
                                             sticky="w", pady=(6, 0))

    def _card_wifi(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "现场网络", "有网线就保持关闭；无网线再预置 WiFi。", fill=tk.BOTH, expand=True)

        ttk.Checkbutton(
            card,
            text="安装现场没有网线，需要预置 WiFi",
            variable=self.use_wifi_var,
            style="Card.TCheckbutton",
            command=self._toggle_wifi_inputs,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.wifi_ssid_combo = self._wifi_ssid_row(card, 1)
        self.wifi_inputs = [
            self.wifi_ssid_combo,
            self._entry_row(card, "WiFi 密码", self.wifi_password_var, 2, show="*"),
            self._entry_row(card, "网卡名", self.wifi_interface_var, 3),
        ]
        ttk.Label(card, textvariable=self.wifi_scan_status_var,
                  style="Card2.TLabel").grid(row=4, column=0, columnspan=3,
                                             sticky="w", pady=(6, 0))
        ttk.Label(card, text="有网线时不用填写；网卡名通常留空。",
                  style="Card2.TLabel").grid(row=5, column=0, columnspan=3,
                                             sticky="w", pady=(6, 0))

    def _card_factory_materials(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "批次材料", "这两个文件决定机器第一次入网和工厂授权。")

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill=tk.X)
        ttk.Checkbutton(
            inner,
            text="真机安装：包含入网配置和工厂授权文件",
            variable=self.include_factory_materials_var,
            style="Card.TCheckbutton",
            command=self._refresh_readiness,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._path_row(inner, "入网配置", self.tailnet_env_var, 1, self._choose_tailnet_env)
        self._path_row(inner, "工厂授权", self.factory_license_var, 2, self._choose_factory_license)
        ttk.Label(
            card,
            text="现场装机默认要包含这两个文件。换 bootstrap key 或 factory license 时，只替换批次文件。",
            style="Card2.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _card_iso(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "镜像设置", "默认只需要确认输出文件名，高级选项不要随手改。")

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill=tk.X)

        self._path_row(inner, "基础 ISO", self.base_iso_var, 0, self._choose_base_iso)
        self._path_row(inner, "输出文件", self.output_iso_var, 1, self._choose_output_iso)

        cb_frame = ttk.Frame(card, style="Card.TFrame")
        cb_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(cb_frame, text="重新生成离线 apt 包仓库（网络较慢时不要勾选）",
                        variable=self.rebuild_apt_var,
                        style="Card.TCheckbutton",
                        command=self._refresh_readiness).pack(anchor="w")
        ttk.Checkbutton(cb_frame, text="包含首版业务 Docker 镜像（dist/bundle-images.tar）",
                        variable=self.include_images_var,
                        style="Card.TCheckbutton",
                        command=self._refresh_readiness).pack(anchor="w", pady=(6, 0))
        ttk.Label(card, text="一般只改输出文件名；基础 ISO 和高级选项默认保持不变。",
                  style="Card2.TLabel").pack(anchor="w", pady=(8, 0))

    def _card_disk(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "目标 U 盘", "写入前必须人工核对盘名和容量。", subtitle_wrap=260)

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill=tk.X)

        ttk.Label(inner, text="选择 U 盘", style="Card.TLabel").pack(anchor="w")
        picker = ttk.Frame(inner, style="Card.TFrame")
        picker.pack(fill=tk.X, pady=(6, 0))
        self.disk_combo = ttk.Combobox(picker, textvariable=self.disk_var,
                                       state="readonly", width=30)
        self.disk_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(picker, text="刷新", style="Small.TButton",
                   command=self.refresh_disks).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Checkbutton(card, text="构建完成后写入 U 盘",
                        variable=self.write_after_build_var,
                        style="Card.TCheckbutton",
                        command=self._refresh_readiness).pack(anchor="w", pady=(10, 0))
        warning = tk.Label(
            card,
            text="写入会清空选中的 U 盘。只选择要给 IPC 装机的那只 U 盘。",
            bg=C["danger_light"],
            fg=C["danger"],
            font=("Helvetica Neue", 11, "bold"),
            padx=10,
            pady=6,
        )
        warning.pack(anchor="w", fill=tk.X, pady=(8, 0))

    def _card_log(self, parent: tk.Frame) -> None:
        card = self._make_card(parent, "制作日志", "失败时先看这里的最后几行。", fill=tk.BOTH, expand=True)

        log_frame = tk.Frame(card, bg=C["log_bg"])
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log = tk.Text(
            log_frame, height=16, wrap=tk.WORD, borderwidth=0,
            bg=C["log_bg"], fg=C["log_fg"],
            insertbackground=C["log_fg"],
            font=self.mono_font,
            padx=12, pady=10,
            selectbackground="#334155",
        )
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scr = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        scr.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.configure(yscrollcommand=scr.set)

        # log tags for colouring
        self.log.tag_configure("info", foreground=C["log_info"])
        self.log.tag_configure("warn", foreground=C["log_warn"])
        self.log.tag_configure("err", foreground=C["log_err"])

    # ── reusable rows ────────────────────────────────────────────

    def _readonly_row(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel",
                  font=("Helvetica Neue", 12)).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 12), pady=5)
        entry = ttk.Entry(parent, textvariable=var, state="readonly",
                          style="Readonly.TEntry", width=32)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)

    def _entry_row(self, parent: ttk.Frame, label: str, var: tk.StringVar,
                   row: int, show: str | None = None) -> ttk.Entry:
        ttk.Label(parent, text=label, style="Card.TLabel",
                  font=("Helvetica Neue", 12)).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 12), pady=5)
        entry = ttk.Entry(parent, textvariable=var, show=show, width=28)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)
        return entry

    def _wifi_ssid_row(self, parent: ttk.Frame, row: int) -> ttk.Combobox:
        ttk.Label(parent, text="WiFi 名称", style="Card.TLabel",
                  font=("Helvetica Neue", 12)).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 12), pady=5)
        picker = ttk.Combobox(parent, textvariable=self.wifi_ssid_var, width=28)
        picker.grid(row=row, column=1, sticky="ew", pady=5)
        self.wifi_scan_button = ttk.Button(parent, text="扫描", style="Small.TButton",
                                           command=self.refresh_wifi_networks)
        self.wifi_scan_button.grid(row=row, column=2, padx=(8, 0), pady=5)
        parent.columnconfigure(1, weight=1)
        return picker

    def _path_row(self, parent: ttk.Frame, label: str, var: tk.StringVar,
                  row: int, chooser) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel",
                  font=("Helvetica Neue", 12)).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 12), pady=5)
        ttk.Entry(parent, textvariable=var, width=48).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(parent, text="选择", style="Small.TButton",
                   command=chooser).grid(row=row, column=2, padx=(8, 0), pady=5)
        parent.columnconfigure(1, weight=1)

    # ── choosers ─────────────────────────────────────────────────

    def _choose_base_iso(self) -> None:
        path = filedialog.askopenfilename(
            title="选择基础 ISO",
            initialdir=str(DIST_DIR),
            filetypes=[("ISO images", "*.iso"), ("All files", "*.*")],
        )
        if path:
            self.base_iso_var.set(path)
            self._refresh_readiness()

    def _choose_output_iso(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择输出 ISO",
            initialdir=str(DIST_DIR),
            defaultextension=".iso",
            filetypes=[("ISO images", "*.iso"), ("All files", "*.*")],
        )
        if path:
            self.output_iso_var.set(path)
            self._refresh_readiness()

    def _choose_tailnet_env(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 tailnet.env",
            initialdir=str(AUTOINSTALL_DIR),
            filetypes=[("Env files", "*.env"), ("All files", "*.*")],
        )
        if path:
            self.tailnet_env_var.set(path)
            self._refresh_readiness()

    def _choose_factory_license(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 factory-license.json",
            initialdir=str(AUTOINSTALL_DIR),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.factory_license_var.set(path)
            self._refresh_readiness()

    # ── disk listing ─────────────────────────────────────────────

    def refresh_disks(self) -> None:
        self.disk_rows = list_external_disks()
        values = [format_disk(row) for row in self.disk_rows]
        self.disk_combo["values"] = values
        if values:
            self.disk_var.set(values[0])
        else:
            self.disk_var.set("（未检测到外接磁盘）")
        self._refresh_readiness()

    def refresh_wifi_networks(self) -> None:
        if not self.use_wifi_var.get():
            return
        self.wifi_scan_status_var.set("正在扫描附近 WiFi...")
        if self.wifi_scan_button is not None:
            self.wifi_scan_button.configure(state=tk.DISABLED)
        threading.Thread(target=self._scan_wifi_networks, daemon=True).start()

    def _scan_wifi_networks(self) -> None:
        try:
            networks = list_detected_wifi_networks()
            message = f"检测到 {len(networks)} 个 WiFi；可下拉选择，也可手动输入。" if networks else "未检测到 WiFi；可手动输入。"
        except Exception as exc:
            networks = []
            message = f"扫描失败，可手动输入 WiFi 名称：{exc}"

        def finish() -> None:
            if self.wifi_ssid_combo is not None:
                self.wifi_ssid_combo["values"] = networks
            self.wifi_scan_status_var.set(message)
            if self.wifi_scan_button is not None:
                self.wifi_scan_button.configure(state=tk.NORMAL if self.use_wifi_var.get() else tk.DISABLED)

        self.after(0, finish)

    def _install_traces(self) -> None:
        for var in (
            self.base_iso_var,
            self.tailnet_env_var,
            self.factory_license_var,
            self.disk_var,
            self.include_factory_materials_var,
            self.include_images_var,
            self.write_after_build_var,
        ):
            var.trace_add("write", lambda *_: self._refresh_readiness())
        self.use_wifi_var.trace_add("write", lambda *_: self._toggle_wifi_inputs())

    def _toggle_wifi_inputs(self) -> None:
        state = tk.NORMAL if self.use_wifi_var.get() else tk.DISABLED
        for widget in self.wifi_inputs:
            widget.configure(state=state)
        if self.wifi_scan_button is not None:
            self.wifi_scan_button.configure(state=state)
        self._refresh_readiness()

    def _set_ready_status(self, key: str, ok: bool, text: str, warn: bool = False) -> None:
        label = self.readiness_labels.get(key)
        if label is None:
            return
        if ok:
            fg = C["success"] if not warn else C["warning"]
            bg = C["success_light"] if not warn else C["warning_light"]
        else:
            fg = C["danger"]
            bg = C["danger_light"]
        label.configure(text=text, fg=fg, bg=bg)

    def _refresh_readiness(self) -> None:
        if not self.readiness_labels:
            return
        blocking: list[str] = []
        warnings: list[str] = []

        base_iso = Path(self.base_iso_var.get()).expanduser()
        base_ok = base_iso.is_file()
        if not base_ok:
            blocking.append("基础 ISO")
        self._set_ready_status("base_iso", base_ok, "已就绪" if base_ok else "缺失")

        password_ok = DEFAULT_PASSWORD_ENV.is_file()
        if not password_ok:
            blocking.append("密码配置")
        self._set_ready_status("password_env", password_ok, "已就绪" if password_ok else "缺失")

        if self.include_factory_materials_var.get():
            tailnet = Path(self.tailnet_env_var.get()).expanduser()
            factory_license = Path(self.factory_license_var.get()).expanduser()
            materials_ok = tailnet.is_file() and factory_license.is_file()
            detail = "已就绪" if materials_ok else "缺失"
            if materials_ok:
                try:
                    validate_tailnet_env(tailnet)
                    validate_factory_license(factory_license)
                except ValueError:
                    materials_ok = False
                    detail = "格式错误"
            if not materials_ok:
                blocking.append("Factory 材料")
            self._set_ready_status("factory_materials", materials_ok, detail)
        else:
            warnings.append("未包含 Factory 材料")
            self._set_ready_status("factory_materials", True, "未包含", warn=True)

        apt_ok = (AUTOINSTALL_DIR / "drycontroller-apt" / "Packages").is_file()
        if not apt_ok:
            warnings.append("离线 apt 包会自动生成")
        self._set_ready_status("apt_repo", apt_ok, "已就绪" if apt_ok else "会生成", warn=not apt_ok)

        if self.include_images_var.get():
            images_ok = (DIST_DIR / "bundle-images.tar").is_file()
            if not images_ok:
                blocking.append("业务镜像包")
            self._set_ready_status("images", images_ok, "已就绪" if images_ok else "缺失")
        else:
            warnings.append("未包含业务镜像")
            self._set_ready_status("images", True, "未包含", warn=True)

        iso_ready = not blocking

        if self.write_after_build_var.get():
            selected = self.disk_var.get()
            disk_ok = any(format_disk(row) == selected for row in self.disk_rows)
            if not disk_ok:
                blocking.append("目标 U 盘")
            self._set_ready_status("target_disk", disk_ok, "已选择" if disk_ok else "未检测到")
        else:
            warnings.append("只生成 ISO，不写 U 盘")
            self._set_ready_status("target_disk", True, "不写入", warn=True)

        ready = not blocking
        if ready:
            if warnings:
                self.readiness_summary_var.set("必需材料已就绪；注意：" + "；".join(warnings[:2]))
            else:
                self.readiness_summary_var.set("全部必需材料已就绪，可以开始制作。")
            if self.write_after_build_var.get():
                self.next_action_var.set("核对目标 U 盘后，点击右侧主按钮开始。")
            else:
                self.next_action_var.set("当前只生成 ISO；需要写盘时再勾选写入 U 盘。")
        else:
            self.readiness_summary_var.set("先处理缺失项：" + "、".join(blocking[:4]))
            self.next_action_var.set("补齐红色状态项后点击“重新检查”。")

        if hasattr(self, "run_button") and not self.running:
            self.run_button.configure(state=tk.NORMAL if ready else tk.DISABLED)
        if self.secondary_run_button is not None and not self.running:
            self.secondary_run_button.configure(state=tk.NORMAL if iso_ready else tk.DISABLED)
        if hasattr(self, "status_label") and not self.running:
            status = "就绪" if ready else "需要处理缺失项"
            color = C["success"] if ready else C["danger"]
            self.status_label.configure(text=status, fg=color)

    # ── build flow ───────────────────────────────────────────────

    def start_build_iso_only(self) -> None:
        self.write_after_build_var.set(False)
        self.start_build()

    def start_build(self) -> None:
        if self.running:
            return
        try:
            plan = self._collect_plan()
        except ValueError as exc:
            messagebox.showerror("配置不完整", str(exc))
            return

        if plan["write_usb"]:
            disk = plan["disk"]
            assert disk is not None
            ok = messagebox.askyesno(
                "确认写入 U 盘",
                "下一步会清空并写入这个外接磁盘：\n\n"
                f"{format_disk(disk)}\n\n"
                "请确认它是装机 U 盘，不是电脑硬盘或其他资料盘。\n"
                "确认无误后再继续。",
            )
            if not ok:
                return

        self.running = True
        self.run_button.configure(state=tk.DISABLED)
        if self.secondary_run_button is not None:
            self.secondary_run_button.configure(state=tk.DISABLED)
        self.status_label.configure(text="制作中", fg=C["primary"])
        self.next_action_var.set("制作过程会写入下方日志，请等待完成提示。")
        self.log.delete("1.0", tk.END)
        threading.Thread(target=self._run_plan, args=(plan,), daemon=True).start()

    def _collect_plan(self) -> dict[str, object]:
        base_iso = Path(self.base_iso_var.get()).expanduser()
        output_iso = Path(self.output_iso_var.get()).expanduser()
        if not base_iso.is_file():
            raise ValueError(f"基础 ISO 不存在：{base_iso}")
        if not DEFAULT_PASSWORD_ENV.is_file():
            raise ValueError(
                "默认密码文件不存在：infra/autoinstall/password.env\n"
                "先在制作机上创建该文件；它已被 .gitignore 忽略，不会进入安装盘源码包。"
            )
        if self.use_wifi_var.get():
            wifi_ssid = self.wifi_ssid_var.get().strip()
            wifi_password = self.wifi_password_var.get()
            wifi_interface = self.wifi_interface_var.get().strip()
            if not wifi_ssid or not wifi_password:
                raise ValueError("已勾选预置 WiFi，请填写 WiFi 名称和密码。")
        else:
            wifi_ssid = ""
            wifi_password = ""
            wifi_interface = ""
        if self.include_images_var.get() and not (DIST_DIR / "bundle-images.tar").is_file():
            raise ValueError("已勾选包含业务镜像，但 dist/bundle-images.tar 不存在。请先构建镜像包或取消勾选。")
        include_factory_materials = self.include_factory_materials_var.get()
        tailnet_env = Path(self.tailnet_env_var.get()).expanduser()
        factory_license = Path(self.factory_license_var.get()).expanduser()
        if include_factory_materials:
            if not tailnet_env.is_file():
                raise ValueError(f"tailnet.env 不存在：{tailnet_env}")
            if not factory_license.is_file():
                raise ValueError(f"factory-license.json 不存在：{factory_license}")
            validate_tailnet_env(tailnet_env)
            validate_factory_license(factory_license)
        disk = None
        if self.write_after_build_var.get():
            selected = self.disk_var.get()
            disk = next((row for row in self.disk_rows if format_disk(row) == selected), None)
            if disk is None:
                raise ValueError("请选择要写入的外接 U 盘。")
        return {
            "base_iso": base_iso,
            "output_iso": output_iso,
            "wifi_ssid": wifi_ssid,
            "wifi_password": wifi_password,
            "wifi_interface": wifi_interface,
            "rebuild_apt": self.rebuild_apt_var.get(),
            "include_images": self.include_images_var.get(),
            "include_factory_materials": include_factory_materials,
            "tailnet_env": tailnet_env,
            "factory_license": factory_license,
            "write_usb": self.write_after_build_var.get(),
            "disk": disk,
        }

    def _run_plan(self, plan: dict[str, object]) -> None:
        try:
            self._append_log("DryController OS Installer USB Maker", "info")
            self._append_log(f"Repository: {REPO_ROOT}")
            self._append_log(f"Fixed operator account: {FIXED_OPERATOR_USER}")
            build_bundle(self, plan)
            build_iso(self, plan)
            if plan["write_usb"]:
                disk = plan["disk"]
                assert isinstance(disk, dict)
                open_terminal_writer(self, Path(plan["output_iso"]), disk)
            self._append_log("Done.", "info")
        except Exception as exc:
            self._append_log(f"ERROR: {exc}", "err")
            self.after(0, lambda: messagebox.showerror("制作失败", str(exc)))
        finally:
            self.running = False
            self.after(0, self._refresh_readiness)

    def _append_log(self, line: str, tag: str = "") -> None:
        self.log_queue.put(f"{tag}:{line}" if tag else line)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                raw = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if raw.startswith("info:"):
                self.log.insert(tk.END, raw[5:] + "\n", "info")
            elif raw.startswith("warn:"):
                self.log.insert(tk.END, raw[5:] + "\n", "warn")
            elif raw.startswith("err:"):
                self.log.insert(tk.END, raw[4:] + "\n", "err")
            else:
                self.log.insert(tk.END, raw + "\n")
            self.log.see(tk.END)
        self.after(100, self._drain_log_queue)


# ── business logic (unchanged) ──────────────────────────────────

def run_logged(app: UsbMakerApp, args: list[str], cwd: Path = REPO_ROOT) -> None:
    app._append_log("$ " + " ".join(shlex.quote(arg) for arg in args))
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        app._append_log(line.rstrip())
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"command failed with exit code {code}: {' '.join(args)}")


def build_bundle(app: UsbMakerApp, plan: dict[str, object]) -> None:
    packages = AUTOINSTALL_DIR / "drycontroller-apt" / "Packages"
    if plan["rebuild_apt"] or not packages.is_file():
        run_logged(
            app,
            [
                "bash",
                "scripts/build-autoinstall-offline-apt-repo.sh",
                "--out",
                str(AUTOINSTALL_DIR / "drycontroller-apt"),
            ],
        )
    else:
        app._append_log(f"Using existing offline apt repo: {packages}")

    run_logged(
        app,
        [
            "bash",
            "scripts/build-autoinstall-bundle.sh",
            "--password-env-file",
            str(DEFAULT_PASSWORD_ENV),
        ],
    )

    write_wifi_env(
        ssid=str(plan["wifi_ssid"]),
        password=str(plan["wifi_password"]),
        interface=str(plan["wifi_interface"]),
    )
    copy_factory_materials(app, plan)

    run_logged(
        app,
        [
            "bash",
            "-lc",
            "test -f dist/autoinstall/drycontroller-apt/Packages && "
            "! rg -q 'REPLACE_WITH_SHA512_CRYPT_HASH' dist/autoinstall/user-data",
        ],
    )


def write_wifi_env(ssid: str, password: str, interface: str) -> None:
    path = AUTOINSTALL_DIR / "wifi.env"
    if not ssid and not password:
        path.unlink(missing_ok=True)
        return
    lines = [f"WIFI_SSID={shell_env_value(ssid)}", f"WIFI_PASSWORD={shell_env_value(password)}"]
    if interface:
        lines.append(f"WIFI_INTERFACE={shell_env_value(interface)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def validate_tailnet_env(path: Path) -> None:
    keys = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    required = {"TS_LOGIN_SERVER", "TS_HOSTNAME", "TS_AUTHKEY", "CLOUD_OPS_BASE_URL"}
    missing = sorted(required - keys)
    if missing:
        raise ValueError(f"tailnet.env 缺少必要字段：{', '.join(missing)}")


def validate_factory_license(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"factory-license.json 不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("factory-license.json 必须是 JSON object")
    if payload.get("schema") != "drycontroller.factory-license.v1":
        raise ValueError("factory-license.json 不是 drycontroller.factory-license.v1")
    if not payload.get("signature"):
        raise ValueError("factory-license.json 缺少 signature")
    if not payload.get("expires_at"):
        raise ValueError("factory-license.json 缺少 expires_at")


def copy_factory_materials(app: UsbMakerApp, plan: dict[str, object]) -> None:
    destinations = [DEFAULT_TAILNET_ENV, DEFAULT_FACTORY_LICENSE]
    if not plan["include_factory_materials"]:
        for dest in destinations:
            if dest.exists():
                dest.unlink()
                app._append_log(f"Removed stale factory material: {dest.relative_to(REPO_ROOT)}", "warn")
        app._append_log("Factory activation materials are not included in this ISO.", "warn")
        return

    pairs = [
        (Path(plan["tailnet_env"]), DEFAULT_TAILNET_ENV, "tailnet.env"),
        (Path(plan["factory_license"]), DEFAULT_FACTORY_LICENSE, "factory-license.json"),
    ]
    for src, dest, label in pairs:
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        dest.chmod(stat.S_IRUSR | stat.S_IWUSR)
        app._append_log(f"Including factory material: {label}")


def shell_env_value(value: str) -> str:
    return shlex.quote(value)


def build_iso(app: UsbMakerApp, plan: dict[str, object]) -> None:
    base_iso = Path(plan["base_iso"])
    output_iso = Path(plan["output_iso"])
    output_iso.parent.mkdir(parents=True, exist_ok=True)
    maps = [
        ("dist/autoinstall/user-data", "/user-data"),
        ("dist/autoinstall/meta-data", "/meta-data"),
        ("dist/autoinstall/post-install.sh", "/post-install.sh"),
        (
            "dist/autoinstall/drycontroller-edge-post-install.service",
            "/drycontroller-edge-post-install.service",
        ),
        ("dist/autoinstall/drycontroller-edge.tar.gz", "/drycontroller-edge.tar.gz"),
        ("dist/autoinstall/drycontroller-apt", "/drycontroller-apt"),
    ]
    if (AUTOINSTALL_DIR / "wifi.env").is_file():
        maps.append(("dist/autoinstall/wifi.env", "/wifi.env"))
    if (AUTOINSTALL_DIR / "tailnet.env").is_file():
        maps.append(("dist/autoinstall/tailnet.env", "/tailnet.env"))
    if (AUTOINSTALL_DIR / "factory-license.json").is_file():
        maps.append(("dist/autoinstall/factory-license.json", "/factory-license.json"))
    if plan["include_images"]:
        maps.append(("dist/bundle-images.tar", "/bundle-images.tar"))

    xorriso_args = [
        "xorriso",
        "-indev",
        str(base_iso),
        "-outdev",
        str(output_iso),
        "-boot_image",
        "any",
        "replay",
        "-overwrite",
        "on",
    ]
    for source, target in maps:
        xorriso_args.extend(["-map", source, target])

    if shutil.which("xorriso"):
        run_logged(app, xorriso_args)
    else:
        docker_xorriso_args = [
            arg.replace(str(REPO_ROOT), "/work", 1) if arg.startswith(str(REPO_ROOT)) else arg
            for arg in xorriso_args
        ]
        docker_script = (
            "apt-get update >/dev/null && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y xorriso >/dev/null && "
            + " ".join(shlex.quote(arg) for arg in docker_xorriso_args)
        )
        run_logged(
            app,
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{REPO_ROOT}:/work",
                "-w",
                "/work",
                "ubuntu:24.04",
                "bash",
                "-lc",
                docker_script,
            ],
        )

    run_logged(
        app,
        [
            "bash",
            "-lc",
            f"ls -lh {shlex.quote(str(output_iso))} && "
            f"bsdtar -xOf {shlex.quote(str(output_iso))} user-data | "
            "rg -n 'password:|REPLACE_WITH_SHA512_CRYPT_HASH|drycontroller-apt'",
        ],
    )


def list_external_disks() -> list[dict[str, str]]:
    try:
        raw = subprocess.check_output(["diskutil", "list", "-plist", "external", "physical"])
        payload = plistlib.loads(raw)
    except Exception:
        return []

    rows: list[dict[str, str]] = []
    for disk in payload.get("AllDisksAndPartitions", []):
        ident = disk.get("DeviceIdentifier")
        if not ident:
            continue
        info = disk_info(str(ident))
        rows.append(
            {
                "identifier": str(ident),
                "name": info.get("MediaName") or disk.get("Content") or "External disk",
                "size": human_size(info.get("TotalSize") or disk.get("Size") or 0),
                "protocol": info.get("BusProtocol") or "",
            }
        )
    return rows


def list_detected_wifi_networks() -> list[str]:
    networks: list[str] = []
    if AIRPORT_BIN.is_file():
        try:
            raw = subprocess.check_output(
                [str(AIRPORT_BIN), "-s"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=8,
            )
            networks.extend(parse_airport_scan(raw))
        except Exception:
            pass
    if not networks:
        networks.extend(list_preferred_wifi_networks())
    return dedupe_strings(networks)


def parse_airport_scan(raw: str) -> list[str]:
    lines = raw.splitlines()
    header_index = next(
        (idx for idx, line in enumerate(lines) if "SSID" in line and "BSSID" in line),
        None,
    )
    if header_index is None:
        return []
    header = lines[header_index]
    bssid_col = header.find("BSSID")
    networks: list[str] = []
    for line in lines[header_index + 1:]:
        if not line.strip():
            continue
        ssid = line[:bssid_col].strip() if bssid_col > 0 else ""
        if not ssid:
            match = re.match(r"\s*(.*?)\s+(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\s+", line)
            ssid = match.group(1).strip() if match else ""
        if ssid:
            networks.append(ssid)
    return networks


def list_preferred_wifi_networks() -> list[str]:
    networks: list[str] = []
    for device in list_wifi_devices():
        try:
            raw = subprocess.check_output(
                ["networksetup", "-listpreferredwirelessnetworks", device],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except Exception:
            continue
        for line in raw.splitlines()[1:]:
            ssid = line.strip()
            if ssid:
                networks.append(ssid)
    return networks


def list_wifi_devices() -> list[str]:
    try:
        raw = subprocess.check_output(
            ["networksetup", "-listallhardwareports"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    devices: list[str] = []
    current_is_wifi = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("Hardware Port:"):
            port = stripped.split(":", 1)[1].strip().lower()
            current_is_wifi = port in {"wi-fi", "wifi", "airport"}
        elif current_is_wifi and stripped.startswith("Device:"):
            device = stripped.split(":", 1)[1].strip()
            if device:
                devices.append(device)
            current_is_wifi = False
    return devices


def dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def disk_info(identifier: str) -> dict[str, object]:
    try:
        raw = subprocess.check_output(["diskutil", "info", "-plist", f"/dev/{identifier}"])
        return plistlib.loads(raw)
    except Exception:
        return {}


def human_size(size: object) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "unknown size"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_disk(row: dict[str, str]) -> str:
    protocol = f", {row['protocol']}" if row.get("protocol") else ""
    return f"/dev/{row['identifier']} - {row['name']} - {row['size']}{protocol}"


def open_terminal_writer(app: UsbMakerApp, iso: Path, disk: dict[str, str]) -> None:
    identifier = disk["identifier"]
    if not identifier.startswith("disk"):
        raise RuntimeError(f"unexpected disk identifier: {identifier}")
    raw_identifier = "r" + identifier
    iso_size = iso.stat().st_size
    script = Path(tempfile.gettempdir()) / "drycontroller-write-os-installer-usb.sh"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"diskutil list /dev/{shlex.quote(identifier)}",
                f"diskutil unmountDisk force /dev/{shlex.quote(identifier)} >/dev/null 2>&1 || true",
                f"/bin/dd if={shlex.quote(str(iso))} of=/dev/{shlex.quote(raw_identifier)} bs=4m status=progress",
                "/bin/sync",
                f"/usr/bin/cmp -n {iso_size} {shlex.quote(str(iso))} /dev/{shlex.quote(raw_identifier)}",
                f"diskutil eject /dev/{shlex.quote(identifier)}",
                "echo 'DryController OS Installer USB write + raw verify complete.'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    app._append_log(f"Opening Terminal for sudo USB write: {script}")
    osa = (
        'tell application "Terminal"\n'
        "  activate\n"
        f'  do script "sudo bash {shlex.quote(str(script))}"\n'
        "end tell\n"
    )
    subprocess.check_call(["osascript", "-e", osa])


if __name__ == "__main__":
    UsbMakerApp().mainloop()
