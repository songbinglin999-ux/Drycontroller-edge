#!/usr/bin/python3
"""Operator UI for building and writing the DryController OS Installer USB."""

from __future__ import annotations

import datetime as _dt
import os
import plistlib
import queue
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk
from urllib.parse import urlparse

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
CLOUDSERVER_GEN_PROVISIONING = WORKSPACE_ROOT / "Drycontroller-cloudserver" / "scripts" / "gen-provisioning-usb-package.py"
DIST_DIR = REPO_ROOT / "dist"
AUTOINSTALL_DIR = DIST_DIR / "autoinstall"
DEFAULT_BASE_ISO = DIST_DIR / "drycontroller-os-installer-hybrid.iso"
DEFAULT_PASSWORD_ENV = REPO_ROOT / "infra" / "autoinstall" / "password.env"
FIXED_OPERATOR_USER = "dryops"

# ── colour palette ──────────────────────────────────────────────
C = {
    "bg":              "#f0f4f8",
    "card":            "#ffffff",
    "primary":         "#2563eb",
    "primary_fg":      "#ffffff",
    "danger":          "#ef4444",
    "danger_fg":       "#ffffff",
    "text":            "#1e293b",
    "text_soft":       "#64748b",
    "border":          "#e2e8f0",
    "log_bg":          "#0f172a",
    "log_fg":          "#cbd5e1",
    "log_info":        "#38bdf8",
    "log_warn":        "#fbbf24",
    "log_err":         "#f87171",
    "entry_bg":        "#f8fafc",
    "readonly_bg":     "#f1f5f9",
    "accent_light":    "#eff6ff",
}


def _apply_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")

    # ── font setup ──
    default_font = font.nametofont("TkDefaultFont")
    default_font.configure(family="Helvetica Neue", size=12)
    root.option_add("*Font", default_font)

    heading_font = font.Font(family="Helvetica Neue", size=15, weight="bold")
    mono_font = font.Font(family="SF Mono", size=11)

    # ── general styles ──
    style.configure(".", background=C["bg"], foreground=C["text"])
    style.configure("TLabel", background=C["bg"], foreground=C["text"])
    style.configure("TFrame", background=C["bg"])
    style.configure("TLabelframe", background=C["bg"], foreground=C["text"])
    style.configure("TLabelframe.Label", background=C["bg"], foreground=C["text"],
                    font=("Helvetica Neue", 12, "bold"))

    # ── card frame ──
    style.configure("Card.TLabelframe", background=C["card"], relief="solid",
                    borderwidth=1, bordercolor=C["border"])
    style.configure("Card.TLabelframe.Label", background=C["card"],
                    foreground=C["text"], font=("Helvetica Neue", 12, "bold"))
    style.configure("Card.TFrame", background=C["card"])
    style.configure("Card.TLabel", background=C["card"], foreground=C["text"])
    style.configure("Card2.TLabel", background=C["card"], foreground=C["text_soft"],
                    font=("Helvetica Neue", 10))

    # ── entry ──
    style.configure("TEntry", fieldbackground=C["entry_bg"],
                    borderwidth=1, relief="solid", padding=6)
    style.configure("Readonly.TEntry", fieldbackground=C["readonly_bg"])

    # ── primary button ──
    style.configure("Primary.TButton",
                    background=C["primary"], foreground=C["primary_fg"],
                    borderwidth=0, relief="flat", padding=(24, 10),
                    font=("Helvetica Neue", 13, "bold"))
    style.map("Primary.TButton",
              background=[("active", "#1d4ed8"), ("disabled", "#94a3b8")],
              foreground=[("disabled", "#e2e8f0")])

    # ── secondary button ──
    style.configure("Secondary.TButton",
                    background=C["bg"], foreground=C["text"],
                    borderwidth=1, relief="solid", padding=(18, 8),
                    font=("Helvetica Neue", 12))
    style.map("Secondary.TButton",
              background=[("active", "#e2e8f0")])

    # ── small button ──
    style.configure("Small.TButton",
                    background=C["bg"], foreground=C["text"],
                    borderwidth=1, relief="solid", padding=(10, 4))

    # ── checkbutton ──
    style.configure("Card.TCheckbutton", background=C["card"], foreground=C["text"])

    # ── combobox ──
    style.configure("TCombobox", fieldbackground=C["entry_bg"], padding=6)

    return heading_font, mono_font


class UsbMakerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DryController OS Installer USB Maker")
        self.geometry("960x800")
        self.minsize(860, 680)
        self.configure(bg=C["bg"])

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running = False
        self.disk_rows: list[dict[str, str]] = []

        # ── variables ──
        self.base_iso_var = tk.StringVar(value=str(DEFAULT_BASE_ISO))
        timestamp = _dt.datetime.now().strftime("%Y%m%d-%H%M")
        self.output_iso_var = tk.StringVar(
            value=str(DIST_DIR / f"OS{timestamp}.iso")
        )
        self.fixed_user_var = tk.StringVar(value=FIXED_OPERATOR_USER)
        self.password_source_var = tk.StringVar(value="默认密码配置")
        self.wifi_ssid_var = tk.StringVar()
        self.wifi_password_var = tk.StringVar()
        self.wifi_interface_var = tk.StringVar()
        self.disk_var = tk.StringVar()
        self.rebuild_apt_var = tk.BooleanVar(value=False)
        self.write_after_build_var = tk.BooleanVar(value=True)

        # ── Provisioning USB fields ──
        self.prov_cloud_ops_url = tk.StringVar()
        self.prov_cloud_ops_tailnet_url = tk.StringVar()
        self.prov_batch_id = tk.StringVar()
        self.prov_installer_token = tk.StringVar()
        self.prov_ts_login_server = tk.StringVar()
        self.prov_ts_authkey = tk.StringVar()
        self.prov_ts_authkey_expires_at = tk.StringVar()
        self.prov_headscale_ca_path = tk.StringVar()
        self.prov_cosign_pub_path = tk.StringVar(
            value=str(REPO_ROOT / "configs" / "keys" / "cosign.pub")
        )
        self.prov_include_images = tk.BooleanVar(value=True)
        self.prov_disk_var = tk.StringVar()

        self.heading_font, self.mono_font = _apply_theme(self)
        self._build_ui()
        self.refresh_disks()
        self.after(100, self._drain_log_queue)

    # ── UI construction ──────────────────────────────────────────

    def _build_ui(self) -> None:
        # header
        header = tk.Frame(self, bg=C["primary"], height=72)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text="🖥  DryController OS Installer USB Maker",
            bg=C["primary"], fg=C["primary_fg"],
            font=("Helvetica Neue", 18, "bold"),
        ).pack(side=tk.LEFT, padx=24, pady=18)
        tk.Label(
            header, text="v4 · macOS",
            bg=C["primary"], fg="#93c5fd",
            font=("Helvetica Neue", 11),
        ).pack(side=tk.RIGHT, padx=24, pady=22)

        # scrollable main area
        canvas = tk.Canvas(self, bg=C["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=C["bg"])

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw", tags="frame")
        canvas.configure(yscrollcommand=scrollbar.set)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        root = tk.Frame(scroll_frame, bg=C["bg"])
        root.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        # ── row 0: two-column card row ──
        col_left = tk.Frame(root, bg=C["bg"])
        col_left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        col_right = tk.Frame(root, bg=C["bg"])
        col_right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)

        self._card_account(col_left)
        self._card_wifi(col_right)

        # ── row 1: ISO card (full width) ──
        self._card_iso(root)

        # ── row 2: disk card (full width) ──
        self._card_disk(root)

        # ── row 3: actions ──
        actions = tk.Frame(root, bg=C["bg"])
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.run_button = ttk.Button(actions, text="一键制作 USB 安装盘",
                                     style="Primary.TButton",
                                     command=self.start_build)
        self.run_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="仅构建 ISO",
                   style="Secondary.TButton",
                   command=self.start_build_iso_only).pack(side=tk.LEFT, padx=(12, 0))

        # status indicator
        self.status_label = tk.Label(actions, text="● 就绪", bg=C["bg"],
                                     fg=C["text_soft"], font=("Helvetica Neue", 11))
        self.status_label.pack(side=tk.RIGHT, pady=(4, 0))

        # ── row 4: Provisioning USB ──
        self._card_provisioning(root)

        # ── row 5: log ──
        self._card_log(root)

    def _card_account(self, parent: tk.Frame) -> None:
        card = ttk.Labelframe(parent, text="账号配置", style="Card.TLabelframe", padding=16)
        card.pack(fill=tk.BOTH, expand=True)

        self._readonly_row(card, "运维账号", self.fixed_user_var, 0)
        self._readonly_row(card, "登录密码", self.password_source_var, 1)
        ttk.Label(card, text="密码从 infra/autoinstall/password.env 注入 SHA-512 hash",
                  style="Card2.TLabel").grid(row=2, column=0, columnspan=2,
                                             sticky="w", pady=(6, 0))

    def _card_wifi(self, parent: tk.Frame) -> None:
        card = ttk.Labelframe(parent, text="WiFi（选填）", style="Card.TLabelframe", padding=16)
        card.pack(fill=tk.BOTH, expand=True)

        self._entry_row(card, "SSID", self.wifi_ssid_var, 0)
        self._entry_row(card, "密码", self.wifi_password_var, 1, show="*")
        self._entry_row(card, "网卡名", self.wifi_interface_var, 2)
        ttk.Label(card, text="网卡名留空则由 NetworkManager 自动选择",
                  style="Card2.TLabel").grid(row=3, column=0, columnspan=2,
                                             sticky="w", pady=(6, 0))

    def _card_iso(self, parent: tk.Frame) -> None:
        card = ttk.Labelframe(parent, text="安装镜像", style="Card.TLabelframe", padding=16)
        card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill=tk.X)

        self._path_row(inner, "基础 ISO", self.base_iso_var, 0, self._choose_base_iso)
        self._path_row(inner, "输出 ISO", self.output_iso_var, 1, self._choose_output_iso)

        cb_frame = ttk.Frame(card, style="Card.TFrame")
        cb_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(cb_frame, text="重新生成离线 apt 包仓库（网络较慢时不要勾选）",
                        variable=self.rebuild_apt_var,
                        style="Card.TCheckbutton").pack(side=tk.LEFT)

    def _card_disk(self, parent: tk.Frame) -> None:
        card = ttk.Labelframe(parent, text="目标 U 盘", style="Card.TLabelframe", padding=16)
        card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill=tk.X)

        ttk.Label(inner, text="外接磁盘", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        self.disk_combo = ttk.Combobox(inner, textvariable=self.disk_var,
                                       state="readonly", width=50)
        self.disk_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(inner, text="刷新", style="Small.TButton",
                   command=self.refresh_disks).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Checkbutton(card, text="构建完成后写入 U 盘",
                        variable=self.write_after_build_var,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(10, 0))

    def _card_provisioning(self, parent: tk.Frame) -> None:
        card = ttk.Labelframe(parent, text="Provisioning USB（部署/激活 U 盘）",
                              style="Card.TLabelframe", padding=16)
        card.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))

        cols = ttk.Frame(card, style="Card.TFrame")
        cols.pack(fill=tk.X)

        col_left = ttk.Frame(cols, style="Card.TFrame")
        col_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 12))
        col_right = ttk.Frame(cols, style="Card.TFrame")
        col_right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(12, 0))

        # ── left column: Cloud Ops + Tailscale ──
        cloud = ttk.Labelframe(col_left, text="Cloud Ops", style="Card.TLabelframe", padding=10)
        cloud.pack(fill=tk.X, pady=(0, 8))
        self._prov_row(cloud, "公网地址", self.prov_cloud_ops_url, 0)
        self._prov_row(cloud, "Tailnet 地址", self.prov_cloud_ops_tailnet_url, 1)
        self._prov_row(cloud, "Batch ID", self.prov_batch_id, 2)
        self._prov_row(cloud, "Installer Token", self.prov_installer_token, 3, show="*")

        ts = ttk.Labelframe(col_left, text="Tailscale", style="Card.TLabelframe", padding=10)
        ts.pack(fill=tk.X)
        self._prov_row(ts, "Login Server", self.prov_ts_login_server, 0)
        self._prov_row(ts, "Auth Key", self.prov_ts_authkey, 1, show="*")
        self._prov_path_row(ts, "Headscale CA", self.prov_headscale_ca_path, 2, self._choose_headscale_ca)
        self._prov_row(ts, "Auth Key 到期时间", self.prov_ts_authkey_expires_at, 3)

        # ── right column: registry note + cosign + images + disk ──
        registry_note = ttk.Labelframe(col_right, text="Registry 凭据", style="Card.TLabelframe", padding=10)
        registry_note.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            registry_note,
            text="Registry pull token 由 Cloud Ops 统一管理，\n"
                 "通过 Provisioning Batch 绑定下发到 IPC，\n"
                 "不在此手动输入。",
            style="Card2.TLabel",
        ).pack(anchor="w", pady=4)

        other = ttk.Labelframe(col_right, text="其他", style="Card.TLabelframe", padding=10)
        other.pack(fill=tk.X)

        # cosign.pub row
        cosign_frame = ttk.Frame(other, style="Card.TFrame")
        cosign_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(cosign_frame, text="cosign.pub", style="Card.TLabel",
                  font=("Helvetica Neue", 11)).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Entry(cosign_frame, textvariable=self.prov_cosign_pub_path, width=24).pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cosign_frame, text="选择", style="Small.TButton",
                   command=self._choose_cosign_pub).pack(side=tk.LEFT, padx=(6, 0))

        # images checkbox
        ttk.Checkbutton(other, text="包含预构建 Docker 镜像（dist/bundle-images.tar）",
                        variable=self.prov_include_images,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(0, 4))

        # disk selector
        disk_frame = ttk.Frame(other, style="Card.TFrame")
        disk_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(disk_frame, text="目标 U 盘", style="Card.TLabel",
                  font=("Helvetica Neue", 11)).pack(side=tk.LEFT, padx=(0, 8))
        self.prov_disk_combo = ttk.Combobox(disk_frame, textvariable=self.prov_disk_var,
                                            state="readonly", width=22)
        self.prov_disk_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # write button
        ttk.Button(other, text="写入 Provisioning USB",
                   style="Primary.TButton",
                   command=self._start_provisioning_write).pack(pady=(4, 0))

    def _prov_row(self, parent: ttk.Frame, label: str, var: tk.StringVar,
                  row: int, show: str | None = None) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel",
                  font=("Helvetica Neue", 11)).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 10), pady=3)
        entry = ttk.Entry(parent, textvariable=var, show=show, width=22)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        parent.columnconfigure(1, weight=1)

    def _prov_path_row(self, parent: ttk.Frame, label: str, var: tk.StringVar,
                       row: int, chooser) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel",
                  font=("Helvetica Neue", 11)).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 10), pady=3)
        ttk.Entry(parent, textvariable=var, width=22).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="选择", style="Small.TButton",
                   command=chooser).grid(row=row, column=2, padx=(6, 0), pady=3)
        parent.columnconfigure(1, weight=1)

    def _card_log(self, parent: tk.Frame) -> None:
        card = ttk.Labelframe(parent, text="日志", style="Card.TLabelframe", padding=12)
        card.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        parent.rowconfigure(5, weight=1)

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
                   row: int, show: str | None = None) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel",
                  font=("Helvetica Neue", 12)).grid(row=row, column=0, sticky="w",
                                                    padx=(0, 12), pady=5)
        entry = ttk.Entry(parent, textvariable=var, show=show, width=28)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        parent.columnconfigure(1, weight=1)

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

    def _choose_output_iso(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择输出 ISO",
            initialdir=str(DIST_DIR),
            defaultextension=".iso",
            filetypes=[("ISO images", "*.iso"), ("All files", "*.*")],
        )
        if path:
            self.output_iso_var.set(path)

    def _choose_cosign_pub(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 cosign.pub",
            initialdir=str(REPO_ROOT / "configs" / "keys"),
            filetypes=[("Public key", "*.pub"), ("All files", "*.*")],
        )
        if path:
            self.prov_cosign_pub_path.set(path)

    def _choose_headscale_ca(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Headscale CA PEM",
            filetypes=[("Certificate", "*.crt *.pem"), ("All files", "*.*")],
        )
        if path:
            self.prov_headscale_ca_path.set(path)

    # ── disk listing ─────────────────────────────────────────────

    def refresh_disks(self) -> None:
        self.disk_rows = list_external_disks()
        values = [format_disk(row) for row in self.disk_rows]
        self.disk_combo["values"] = values
        self.prov_disk_combo["values"] = values
        if values:
            self.disk_var.set(values[0])
            if not self.prov_disk_var.get():
                self.prov_disk_var.set(values[0])
        else:
            self.disk_var.set("（未检测到外接磁盘）")
            self.prov_disk_var.set("（未检测到外接磁盘）")

    # ── build flow ───────────────────────────────────────────────

    def start_build_iso_only(self) -> None:
        self.write_after_build_var.set(False)
        self.start_build()

    # ── Provisioning USB ─────────────────────────────────────────

    def _start_provisioning_write(self) -> None:
        if self.running:
            return
        try:
            plan = self._collect_provisioning_plan()
        except ValueError as exc:
            messagebox.showerror("配置不完整", str(exc))
            return

        disk = plan["disk"]
        ok = messagebox.askyesno(
            "确认写入 Provisioning U 盘",
            "即将格式化并写入 Provisioning U 盘：\n\n"
            f"{format_disk(disk)}\n\n"
            "确认设备号和容量无误后再继续。\n"
            "此操作会清空 U 盘上所有数据！",
        )
        if not ok:
            return

        self.running = True
        self._append_log("Provisioning USB 制作中…", "info")
        threading.Thread(target=self._write_provisioning_usb, args=(plan,), daemon=True).start()

    def _collect_provisioning_plan(self) -> dict[str, object]:
        cloud_ops_url = self.prov_cloud_ops_url.get().strip()
        cloud_ops_tailnet_url = self.prov_cloud_ops_tailnet_url.get().strip()
        batch_id = self.prov_batch_id.get().strip()
        installer_token = self.prov_installer_token.get().strip()
        ts_login_server = self.prov_ts_login_server.get().strip()
        ts_authkey = self.prov_ts_authkey.get().strip()
        ts_authkey_expires_at = self.prov_ts_authkey_expires_at.get().strip()
        headscale_ca = self.prov_headscale_ca_path.get().strip()
        cosign_path = Path(self.prov_cosign_pub_path.get()).expanduser()

        if not cloud_ops_url:
            raise ValueError("cloud_ops_base_url（公网地址）为必填项")
        if not CLOUDSERVER_GEN_PROVISIONING.is_file():
            raise ValueError(f"Provisioning generator 不存在：{CLOUDSERVER_GEN_PROVISIONING}")
        if not cosign_path.is_file():
            raise ValueError(f"cosign.pub 不存在：{cosign_path}")
        if not batch_id or not installer_token:
            raise ValueError("Provisioning USB 只能使用 Cloud Ops batch 模式：Batch ID 和 Installer Token 均为必填")
        if cloud_ops_tailnet_url and (not ts_login_server or not ts_authkey):
            raise ValueError("Tailnet 地址存在时，Login Server 和 Auth Key 均为必填")
        login_host = urlparse(ts_login_server).hostname or ""
        if login_host.replace(".", "").isdigit() and not headscale_ca:
            raise ValueError("Login Server 使用 IP 地址时必须选择 Headscale CA PEM")
        if headscale_ca and not Path(headscale_ca).expanduser().is_file():
            raise ValueError(f"Headscale CA 不存在：{headscale_ca}")

        selected = self.prov_disk_var.get()
        disk = next((row for row in self.disk_rows if format_disk(row) == selected), None)
        if disk is None:
            raise ValueError("请选择目标 Provisioning U 盘")

        return {
            "cloud_ops_url": cloud_ops_url,
            "cloud_ops_tailnet_url": cloud_ops_tailnet_url,
            "batch_id": batch_id,
            "installer_token": installer_token,
            "ts_login_server": ts_login_server,
            "ts_authkey": ts_authkey,
            "ts_authkey_expires_at": ts_authkey_expires_at,
            "headscale_ca_path": Path(headscale_ca).expanduser() if headscale_ca else None,
            "cosign_pub_path": cosign_path,
            "include_images": self.prov_include_images.get(),
            "disk": disk,
        }

    def _write_provisioning_usb(self, plan: dict[str, object]) -> None:
        try:
            disk = plan["disk"]
            assert isinstance(disk, dict)
            identifier = str(disk["identifier"])
            if not identifier.startswith("disk"):
                raise RuntimeError(f"unexpected disk identifier: {identifier}")

            build_dir = Path(tempfile.mkdtemp(prefix="drycontroller-provisioning-build-"))
            cmd = [
                sys.executable,
                str(CLOUDSERVER_GEN_PROVISIONING),
                "--out",
                str(build_dir),
                "--batch-id",
                str(plan["batch_id"]),
                "--installer-token",
                str(plan["installer_token"]),
                "--cloud-ops-base-url",
                str(plan["cloud_ops_url"]),
                "--cosign-pub",
                str(plan["cosign_pub_path"]),
                "--force",
            ]
            if plan["cloud_ops_tailnet_url"]:
                cmd += ["--cloud-ops-tailnet-base-url", str(plan["cloud_ops_tailnet_url"])]
            if plan["ts_login_server"]:
                cmd += ["--ts-login-server", str(plan["ts_login_server"])]
            if plan["ts_authkey"]:
                cmd += ["--ts-authkey", str(plan["ts_authkey"])]
            if plan["ts_authkey_expires_at"]:
                cmd += ["--ts-authkey-expires-at", str(plan["ts_authkey_expires_at"])]
            if plan["headscale_ca_path"]:
                cmd += ["--headscale-ca-cert", str(plan["headscale_ca_path"])]
            if plan["include_images"]:
                images_src = DIST_DIR / "bundle-images.tar"
                if images_src.is_file():
                    cmd += ["--bootstrap-image-tar", str(images_src)]
                else:
                    self._append_log(f"  WARNING: 镜像 tar 不存在，跳过: {images_src}", "warn")

            run_logged(self, cmd, cwd=WORKSPACE_ROOT / "Drycontroller-cloudserver")
            prov_dir = build_dir / "drycontroller-provisioning"
            if not (prov_dir / "provisioning.json").is_file():
                raise RuntimeError("provisioning generator did not create provisioning.json")

            # build Terminal script
            script = Path(tempfile.gettempdir()) / "drycontroller-write-provisioning-usb.sh"
            vol_name = "DRYPROV"
            script.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        f"diskutil list /dev/{shlex.quote(identifier)}",
                        f"diskutil unmountDisk force /dev/{shlex.quote(identifier)} >/dev/null 2>&1 || true",
                        f"echo 'Formatting /dev/{shlex.quote(identifier)} as FAT32...'",
                        f"diskutil eraseDisk FAT32 {shlex.quote(vol_name)} MBRFormat /dev/{shlex.quote(identifier)}",
                        f"echo 'Copying provisioning files...'",
                        f"cp -a {shlex.quote(str(prov_dir))} /Volumes/{shlex.quote(vol_name)}/",
                        "/bin/sync",
                        f"diskutil eject /dev/{shlex.quote(identifier)}",
                        "echo 'Provisioning USB write complete.'",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            self._append_log(f"Opening Terminal for sudo USB write: {script}")

            osa = (
                'tell application "Terminal"\n'
                "  activate\n"
                f'  do script "sudo bash {shlex.quote(str(script))}"\n'
                "end tell\n"
            )
            subprocess.check_call(["osascript", "-e", osa])
            self._append_log("Provisioning USB Terminal 已打开，请在终端中完成 sudo 授权", "info")
        except Exception as exc:
            self._append_log(f"ERROR: {exc}", "err")
            self.after(0, lambda: messagebox.showerror("Provisioning USB 制作失败", str(exc)))
        finally:
            self.running = False

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
                "即将清空并写入这个外接磁盘：\n\n"
                f"{format_disk(disk)}\n\n"
                "确认设备号和容量无误后再继续。",
            )
            if not ok:
                return

        self.running = True
        self.run_button.configure(state=tk.DISABLED)
        self.status_label.configure(text="● 制作中…", fg=C["primary"])
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
        wifi_ssid = self.wifi_ssid_var.get().strip()
        wifi_password = self.wifi_password_var.get()
        wifi_interface = self.wifi_interface_var.get().strip()
        if bool(wifi_ssid) != bool(wifi_password):
            raise ValueError("WiFi SSID 和 WiFi 密码必须同时填写，或同时留空。")
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
            self.after(0, lambda: self.run_button.configure(state=tk.NORMAL))
            self.after(0, lambda: self.status_label.configure(text="● 就绪", fg=C["text_soft"]))

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
