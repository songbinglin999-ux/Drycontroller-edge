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
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[1]
DIST_DIR = REPO_ROOT / "dist"
AUTOINSTALL_DIR = DIST_DIR / "autoinstall"
DEFAULT_BASE_ISO = DIST_DIR / "drycontroller-os-installer-hybrid.iso"
DEFAULT_PASSWORD_ENV = REPO_ROOT / "infra" / "autoinstall" / "password.env"
FIXED_OPERATOR_USER = "dryops"


class UsbMakerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DryController OS Installer USB Maker")
        self.geometry("980x720")
        self.minsize(880, 620)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running = False
        self.disk_rows: list[dict[str, str]] = []

        self.base_iso_var = tk.StringVar(value=str(DEFAULT_BASE_ISO))
        timestamp = _dt.datetime.now().strftime("%Y%m%d-%H%M")
        self.output_iso_var = tk.StringVar(
            value=str(DIST_DIR / f"drycontroller-os-installer-{timestamp}.iso")
        )
        self.fixed_user_var = tk.StringVar(value=FIXED_OPERATOR_USER)
        self.password_source_var = tk.StringVar(value="默认密码配置")
        self.wifi_ssid_var = tk.StringVar()
        self.wifi_password_var = tk.StringVar()
        self.wifi_interface_var = tk.StringVar()
        self.disk_var = tk.StringVar()
        self.rebuild_apt_var = tk.BooleanVar(value=False)
        self.write_after_build_var = tk.BooleanVar(value=True)

        self._build_ui()
        self.refresh_disks()
        self.after(100, self._drain_log_queue)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.LabelFrame(root, text="固定安装配置", padding=12)
        top.pack(fill=tk.X)
        self._readonly_row(top, "运维账号", self.fixed_user_var, 0)
        self._readonly_row(top, "登录密码", self.password_source_var, 1)
        ttk.Label(
            top,
            text="密码不会在界面显示；制作时从本机 infra/autoinstall/password.env 读取并注入 hash。",
        ).grid(row=2, column=1, sticky="w", pady=(4, 0))

        iso_frame = ttk.LabelFrame(root, text="安装镜像", padding=12)
        iso_frame.pack(fill=tk.X, pady=(12, 0))
        self._path_row(iso_frame, "基础 ISO", self.base_iso_var, 0, self._choose_base_iso)
        self._path_row(iso_frame, "输出 ISO", self.output_iso_var, 1, self._choose_output_iso)
        ttk.Checkbutton(
            iso_frame,
            text="重新生成离线 apt 包仓库（网络较慢时不要勾选）",
            variable=self.rebuild_apt_var,
        ).grid(row=2, column=1, sticky="w", pady=(8, 0))

        wifi_frame = ttk.LabelFrame(root, text="WiFi（选填）", padding=12)
        wifi_frame.pack(fill=tk.X, pady=(12, 0))
        self._entry_row(wifi_frame, "SSID", self.wifi_ssid_var, 0)
        self._entry_row(wifi_frame, "密码", self.wifi_password_var, 1, show="*")
        self._entry_row(wifi_frame, "网卡名", self.wifi_interface_var, 2)
        ttk.Label(wifi_frame, text="网卡名留空时，安装后由 NetworkManager 自动选择无线网卡。").grid(
            row=3, column=1, sticky="w", pady=(4, 0)
        )

        disk_frame = ttk.LabelFrame(root, text="目标 U 盘", padding=12)
        disk_frame.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(disk_frame, text="外接磁盘").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.disk_combo = ttk.Combobox(disk_frame, textvariable=self.disk_var, state="readonly")
        self.disk_combo.grid(row=0, column=1, sticky="ew")
        ttk.Button(disk_frame, text="刷新", command=self.refresh_disks).grid(row=0, column=2, padx=(8, 0))
        ttk.Checkbutton(
            disk_frame,
            text="构建完成后写入 U 盘",
            variable=self.write_after_build_var,
        ).grid(row=1, column=1, sticky="w", pady=(8, 0))
        disk_frame.columnconfigure(1, weight=1)

        actions = ttk.Frame(root)
        actions.pack(fill=tk.X, pady=(12, 0))
        self.run_button = ttk.Button(actions, text="一键制作", command=self.start_build)
        self.run_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="仅构建 ISO", command=self.start_build_iso_only).pack(side=tk.LEFT, padx=(8, 0))

        log_frame = ttk.LabelFrame(root, text="日志", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.log = tk.Text(log_frame, height=18, wrap=tk.WORD)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.configure(yscrollcommand=scrollbar.set)

    def _readonly_row(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        entry = ttk.Entry(parent, textvariable=var, state="readonly")
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        parent.columnconfigure(1, weight=1)

    def _entry_row(
        self,
        parent: ttk.Frame,
        label: str,
        var: tk.StringVar,
        row: int,
        show: str | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        entry = ttk.Entry(parent, textvariable=var, show=show)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        parent.columnconfigure(1, weight=1)

    def _path_row(
        self,
        parent: ttk.Frame,
        label: str,
        var: tk.StringVar,
        row: int,
        chooser,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="选择", command=chooser).grid(row=row, column=2, padx=(8, 0), pady=4)
        parent.columnconfigure(1, weight=1)

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

    def refresh_disks(self) -> None:
        self.disk_rows = list_external_disks()
        values = [format_disk(row) for row in self.disk_rows]
        self.disk_combo["values"] = values
        if values:
            self.disk_var.set(values[0])
        else:
            self.disk_var.set("")

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
                "即将清空并写入这个外接磁盘：\n\n"
                f"{format_disk(disk)}\n\n"
                "确认设备号和容量无误后再继续。",
            )
            if not ok:
                return

        self.running = True
        self.run_button.configure(state=tk.DISABLED)
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
            self._append_log("DryController OS Installer USB Maker")
            self._append_log(f"Repository: {REPO_ROOT}")
            self._append_log(f"Fixed operator account: {FIXED_OPERATOR_USER}")
            build_bundle(self, plan)
            build_iso(self, plan)
            if plan["write_usb"]:
                disk = plan["disk"]
                assert isinstance(disk, dict)
                open_terminal_writer(self, Path(plan["output_iso"]), disk)
            self._append_log("Done.")
        except Exception as exc:  # noqa: BLE001 - GUI must surface all failures.
            self._append_log(f"ERROR: {exc}")
            self.after(0, lambda: messagebox.showerror("制作失败", str(exc)))
        finally:
            self.running = False
            self.after(0, lambda: self.run_button.configure(state=tk.NORMAL))

    def _append_log(self, line: str) -> None:
        self.log_queue.put(line)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log.insert(tk.END, line + "\n")
            self.log.see(tk.END)
        self.after(100, self._drain_log_queue)


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
                f"diskutil eject /dev/{shlex.quote(identifier)}",
                "echo 'DryController OS Installer USB write complete.'",
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
